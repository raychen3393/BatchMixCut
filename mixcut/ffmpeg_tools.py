"""FFmpeg 相关：定位可执行文件、可中断地运行、探测素材、检测硬件编码器"""
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

from .utils import app_dir

IS_WIN = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0


def _find_exe(name):
    exe = name + (".exe" if IS_WIN else "")
    cands = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        cands.append(os.path.join(sys._MEIPASS, "ffmpeg", exe))
    cands.append(os.path.join(app_dir(), "ffmpeg", exe))
    cands.append(os.path.join(app_dir(), "ffmpeg", "bin", exe))
    cands.append(os.path.join(app_dir(), exe))
    for c in cands:
        if os.path.isfile(c):
            return c
    return shutil.which(name)


FFMPEG = _find_exe("ffmpeg")
FFPROBE = _find_exe("ffprobe")


def check_ffmpeg():
    """返回 (是否可用, 说明)"""
    if not FFMPEG or not FFPROBE:
        return False, ("没有找到 ffmpeg / ffprobe。请把 ffmpeg.exe 和 ffprobe.exe 放到程序目录下的 "
                       "ffmpeg 文件夹里，或加入系统 PATH。")
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-version"], capture_output=True,
                             creationflags=_CREATE_NO_WINDOW, timeout=20)
        first = out.stdout.decode("utf-8", "replace").splitlines()[0]
        return True, first
    except Exception as e:  # noqa
        return False, f"ffmpeg 无法运行：{e}"


# 运行时设置（由 engine 在任务开始时写入）
LOW_PRIORITY = True     # ffmpeg 子进程用“低于正常”优先级：后台干活时不抢前台程序的 CPU
X264_THREADS = 0        # CPU 转码时每个 ffmpeg 用几个线程；0 = 让 ffmpeg 自己决定
_BELOW_NORMAL = 0x00004000


def _popen_extra():
    if IS_WIN:
        return {"creationflags": _CREATE_NO_WINDOW | (_BELOW_NORMAL if LOW_PRIORITY else 0)}
    if LOW_PRIORITY:
        return {"preexec_fn": lambda: os.nice(10)}
    return {}


class Cancelled(Exception):
    pass


class ProcessRunner:
    """运行子进程；stop() 时能杀掉所有正在跑的 ffmpeg"""

    def __init__(self, stop_event: threading.Event):
        self.stop_event = stop_event
        self._procs = set()
        self._lock = threading.Lock()

    def run(self, args, timeout=None):
        if self.stop_event.is_set():
            raise Cancelled()
        p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, **_popen_extra())
        with self._lock:
            self._procs.add(p)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            return 124, out, err.decode("utf-8", "replace") + "\n[超时]"
        finally:
            with self._lock:
                self._procs.discard(p)
        if self.stop_event.is_set():
            raise Cancelled()
        return p.returncode, out, err.decode("utf-8", "replace")

    def kill_all(self):
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.kill()
            except Exception:  # noqa
                pass


# ---------------- 素材探测 ----------------

@dataclass
class MediaInfo:
    path: str
    ok: bool = False
    error: str = ""
    width: int = 0              # 显示宽（已考虑旋转）
    height: int = 0             # 显示高
    rotation: int = 0
    vcodec: str = ""
    vprofile: str = ""
    pix_fmt: str = ""
    fps: Optional[Fraction] = None
    avg_fps: Optional[Fraction] = None
    sar: str = ""
    v_duration: float = 0.0
    v_extradata: str = ""
    v_timebase: str = ""        # 视频流时间基，如 1/15360
    has_audio: bool = False
    acodec: str = ""
    aprofile: str = ""
    sample_rate: int = 0
    channels: int = 0
    a_duration: float = 0.0
    extra: dict = field(default_factory=dict)


def _frac(s):
    try:
        if not s or s in ("0/0", "N/A"):
            return None
        f = Fraction(s)
        return f if f > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def _float(s):
    try:
        v = float(s)
        return v if v > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _mkv_tag_duration(tags):
    # mkv 常把时长写在标签里：DURATION=00:00:05.000000000
    for k, v in (tags or {}).items():
        if k.upper().startswith("DURATION"):
            try:
                h, m, s = v.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
            except ValueError:
                pass
    return 0.0


def _packet_duration(runner: ProcessRunner, path: str, stream_index: int) -> float:
    """扫描视频数据包（只解封装、不解码）求真实时长 = 最后一帧结束 - 第一帧开始"""
    code, out, _err = runner.run([FFPROBE, "-v", "error", "-select_streams", str(stream_index),
                                  "-show_entries", "packet=pts_time,dts_time,duration_time",
                                  "-of", "csv=p=0", path], timeout=300)
    if code != 0:
        return 0.0
    lo, hi = None, None
    for line in out.decode("utf-8", "replace").splitlines():
        f = line.split(",")
        if len(f) < 3:
            continue
        t = f[0] if f[0] not in ("", "N/A") else f[1]
        try:
            t = float(t)
        except ValueError:
            continue
        try:
            d = float(f[2])
        except ValueError:
            d = 0.0
        lo = t if lo is None else min(lo, t)
        hi = t + d if hi is None else max(hi, t + d)
    return (hi - lo) if lo is not None else 0.0


def probe(runner: ProcessRunner, path: str) -> MediaInfo:
    info = MediaInfo(path=path)
    args = [FFPROBE, "-v", "error", "-show_data_hash", "MD5", "-print_format", "json",
            "-show_format", "-show_streams", path]
    try:
        code, out, err = runner.run(args, timeout=60)
    except Cancelled:
        raise
    except Exception as e:  # noqa
        info.error = f"ffprobe 无法运行：{e}"
        return info
    if code != 0:
        info.error = (err.strip().splitlines() or ["无法解析"])[-1]
        return info
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        info.error = "ffprobe 输出无法解析"
        return info
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        info.error = "没有视频轨"
        return info
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if w <= 0 or h <= 0:
        info.error = "视频分辨率无效"
        return info
    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                pass
    if not rot and (v.get("tags") or {}).get("rotate"):
        try:
            rot = int(v["tags"]["rotate"])
        except ValueError:
            pass
    rot = rot % 360
    if rot in (90, 270):
        w, h = h, w
    info.width, info.height, info.rotation = w, h, rot
    info.vcodec = v.get("codec_name", "")
    info.vprofile = v.get("profile", "")
    info.pix_fmt = v.get("pix_fmt", "")
    info.fps = _frac(v.get("r_frame_rate"))
    info.avg_fps = _frac(v.get("avg_frame_rate"))
    info.sar = v.get("sample_aspect_ratio", "") or ""
    info.v_extradata = v.get("extradata_hash", "") or ""
    info.v_timebase = v.get("time_base", "") or ""
    vd = _float(v.get("duration"))
    fmt_name = fmt.get("format_name", "")
    if vd <= 0 or "avi" in fmt_name or "flv" in fmt_name:
        # 视频流没写时长（mkv/flv 常见），或容器头信息不可靠（avi/flv）：
        # 不能用整个文件的时长（会被更长的音频撑大），改为扫描视频包
        vd = _packet_duration(runner, path, v.get("index", 0)) or vd
    if vd <= 0:
        vd = _mkv_tag_duration(v.get("tags")) or _float(fmt.get("duration"))
    info.v_duration = vd
    if a is not None:
        info.has_audio = True
        info.acodec = a.get("codec_name", "")
        info.aprofile = a.get("profile", "")
        info.sample_rate = int(a.get("sample_rate") or 0)
        info.channels = int(a.get("channels") or 0)
        info.a_duration = (_float(a.get("duration")) or _mkv_tag_duration(a.get("tags"))
                           or _float(fmt.get("duration")))
    if vd <= 0.05:
        info.error = "无法确定视频时长（文件可能损坏）"
        return info
    if info.fps is None and info.avg_fps is None:
        info.error = "无法确定帧率"
        return info
    info.ok = True
    return info


def quick_decode_check(runner: ProcessRunner, path: str) -> str:
    """快速解码开头 1 秒，能发现大部分损坏文件。返回空字符串表示正常。"""
    code, _out, err = runner.run([FFMPEG, "-hide_banner", "-v", "error", "-nostdin", "-t", "1",
                                  "-i", path, "-f", "null", "-"], timeout=60)
    if code != 0:
        return (err.strip().splitlines() or ["解码失败"])[-1]
    return ""


# ---------------- 编码器 ----------------

ENCODER_LABELS = {
    "auto": "自动检测（优先显卡）",
    "libx264": "CPU (libx264)",
    "h264_nvenc": "NVIDIA 显卡 (h264_nvenc)",
    "h264_amf": "AMD 显卡 (h264_amf)",
    "h264_qsv": "Intel 核显 (h264_qsv)",
}
HW_ORDER = ["h264_nvenc", "h264_amf", "h264_qsv"]


def encoder_test(runner: ProcessRunner, enc: str, speed: str = "normal"):
    """用和正式转码完全相同的参数试编一小段。返回 (是否可用, 失败原因)"""
    fps = Fraction(30)
    args = [FFMPEG, "-hide_banner", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
            "color=c=black:s=1280x720:r=30:d=0.3", "-pix_fmt", "yuv420p",
            *video_encoder_args(enc, fps, 20, speed), "-f", "null", "-"]
    try:
        code, _o, err = runner.run(args, timeout=30)
    except Cancelled:
        raise
    except Exception as e:  # noqa
        return False, str(e)
    if code == 0:
        return True, ""
    lines = [l.strip() for l in err.strip().splitlines() if l.strip()]
    # 挑出最有用的几行（驱动版本、找不到 DLL 等）
    key = [l for l in lines if any(k in l.lower() for k in
                                   ("driver", "version", "nvenc", "amf", "dll", "cannot", "not ", "fail"))]
    reason = " | ".join((key or lines)[-3:]) or f"退出码 {code}"
    return False, reason[:400]


def resolve_encoder(runner: ProcessRunner, choice: str, log=print, speed: str = "normal") -> str:
    if choice == "libx264":
        return "libx264"
    tried = [choice] if choice in HW_ORDER else HW_ORDER
    failures = []
    for enc in tried:
        ok, reason = encoder_test(runner, enc, speed)
        if ok:
            return enc
        failures.append((enc, reason))
    for enc, reason in failures:
        log(f"  · {ENCODER_LABELS[enc]} 测试失败：{reason}")
    log("⚠ 没有可用的显卡编码，改用 CPU 编码（预处理会慢一些）。")
    log("  提示：NVIDIA 显卡请到 nvidia.cn 更新到最新驱动；AMD 显卡请到 amd.com 更新 Adrenalin 驱动；"
        "更新后重启电脑再试。型号带 F 的 Intel CPU 没有核显，Intel 那一项失败是正常的。")
    return "libx264"


def video_encoder_args(enc: str, fps: Fraction, quality: int = 20, speed: str = "normal"):
    """所有缓存片段都用这一套参数，保证拼接时 SPS/PPS 一致。
    speed: normal 标准 / fast 更快（CPU 用 ultrafast：约快一倍，文件约大一倍）"""
    gop = str(max(1, int(round(float(fps) * 2))))
    fast = speed == "fast"
    if enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p1" if fast else "p3", "-rc", "vbr",
                "-cq", str(quality + 1), "-b:v", "0", "-profile:v", "high", "-g", gop, "-bf", "2"]
    if enc == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp",
                "-qp_i", str(quality), "-qp_p", str(quality + 2), "-profile:v", "high", "-g", gop]
    if enc == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", str(quality + 2),
                "-profile:v", "high", "-g", gop]
    args = ["-c:v", "libx264", "-preset", "ultrafast" if fast else "veryfast", "-crf", str(quality),
            "-profile:v", "high", "-g", gop, "-bf", "2"]
    if X264_THREADS > 0:
        args += ["-threads", str(X264_THREADS)]
    return args


AUDIO_ARGS = ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
