"""预处理 + 缓存：把所有素材统一成完全相同的规格，后续拼接时视频流直接复制。

每个素材缓存为两个文件：
  .ts  只含 H.264 视频，帧数 = round(时长 × 帧率)
  .wav PCM 音频，采样数精确等于 帧数/帧率 × 44100（短了补静音，长了裁掉）
音频不存 AAC 是因为 AAC 以 1024 采样为一帧，并带有编码器起始延迟，每段都会比视频长出
几十毫秒，多段拼接后误差累积；PCM 则可以精确到采样。

缓存一致性：缓存目录按“编码参数指纹”区分（分辨率、帧率、编码器、画质、音频参数、流程版本），
参数一变就换一个目录，旧目录作废并删除，所以绝不可能把不同参数的片段混在一起拼。
单个文件是否处理过：用“文件路径 + 大小 + 修改时间”做键。
"""
import json
import os
import shutil
from dataclasses import dataclass
from fractions import Fraction

from .ffmpeg_tools import (AUDIO_ARGS, FFMPEG, Cancelled, HwDecode, MediaInfo, ProcessRunner, probe,
                           video_encoder_args)
from .utils import data_root, file_sig, norm_path, short_hash

PIPELINE_VERSION = "v4"
AUDIO_SR = 44100
# 缩放算法：bilinear 比 ffmpeg 默认的 bicubic 快，手机/竖屏视频缩放后肉眼看不出区别
SCALE_FLAGS = "bilinear"


@dataclass
class TargetSpec:
    width: int
    height: int
    fps: Fraction
    encoder: str
    quality: int = 20
    speed: str = "normal"

    def fingerprint(self) -> str:
        fp = (f"{PIPELINE_VERSION}|{self.width}x{self.height}|{self.fps}|{self.encoder}|"
              f"q{self.quality}|aac{AUDIO_SR}s2c128k|yuv420p|ts90k")
        if self.speed != "normal":   # 标准档保持旧指纹，已有缓存继续可用
            fp += f"|{self.speed}"
        return fp

    def describe(self) -> str:
        return f"{self.width}x{self.height} @ {float(self.fps):g}fps, 编码器 {self.encoder}"


@dataclass
class CachedClip:
    src: str
    path: str        # 缓存视频 .ts（只有 H.264 视频流）
    audio: str       # 缓存音频 .wav（PCM，采样数与视频时长严格一致）
    duration: float  # 秒（= 帧数 / 帧率）
    size: int        # 视频片段字节数
    mode: str        # transcode / remux / cached
    moov: int = 0    # 直接拼接时：这条素材在成品文件头里大约要占多少字节（用于预留位置）


def cache_root() -> str:
    p = os.path.join(data_root(), "cache")
    os.makedirs(p, exist_ok=True)
    return p


class ClipCache:
    def __init__(self, spec: TargetSpec, runner: ProcessRunner, log=print, hw: HwDecode = None):
        self.spec = spec
        self.runner = runner
        self.log = log
        self.hw = hw or HwDecode()
        self.fp = spec.fingerprint()
        self.dir = os.path.join(cache_root(), short_hash(self.fp, 16))
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "_spec.json"), "w", encoding="utf-8") as f:
            json.dump({"fingerprint": self.fp}, f, ensure_ascii=False)
        self.ref_extradata = None

    # ---------- 缓存目录管理 ----------
    def purge_stale(self):
        """删除其它参数生成的旧缓存（已作废）"""
        root = cache_root()
        removed = 0
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if (os.path.isdir(p) and not name.startswith("direct_")
                    and os.path.abspath(p) != os.path.abspath(self.dir)):
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
        return removed

    @staticmethod
    def _key(src: str) -> str:
        size, mtime = file_sig(src)
        return short_hash(f"{norm_path(src)}|{size}|{mtime}", 24)

    def lookup(self, src: str):
        try:
            key = self._key(src)
        except OSError:
            return None
        ts = os.path.join(self.dir, key + ".ts")
        wav = os.path.join(self.dir, key + ".wav")
        meta = os.path.join(self.dir, key + ".json")
        if not (os.path.isfile(ts) and os.path.isfile(wav) and os.path.isfile(meta)):
            return None
        try:
            with open(meta, "r", encoding="utf-8") as f:
                m = json.load(f)
            if (m.get("fingerprint") != self.fp or os.path.getsize(ts) != m.get("size")
                    or os.path.getsize(wav) != m.get("audio_size")):
                return None
            return CachedClip(src, ts, wav, float(m["duration"]), int(m["size"]), "cached")
        except (OSError, ValueError, KeyError):
            return None

    # ---------- 参考参数（用于“规格一致就跳过转码”的严格判断） ----------
    def build_reference(self):
        """用当前参数编一小段样片，记下 H.264 的 SPS/PPS 指纹。
        素材的指纹必须和它完全一样才允许跳过转码（否则拼接时可能花屏）。"""
        s = self.spec
        tmp = os.path.join(self.dir, "_ref.mp4")
        args = [FFMPEG, "-hide_banner", "-v", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", f"color=c=black:s={s.width}x{s.height}:r={s.fps}:d=0.5",
                "-pix_fmt", "yuv420p", *video_encoder_args(s.encoder, s.fps, s.quality, s.speed), tmp]
        code, _o, err = self.runner.run(args, timeout=60)
        if code == 0:
            info = probe(self.runner, tmp)
            self.ref_extradata = info.v_extradata or None
        try:
            os.remove(tmp)
        except OSError:
            pass

    def can_skip_transcode(self, info: MediaInfo):
        """视频流参数全部一致（含 SPS/PPS 指纹）才跳过视频转码；音频总是重新生成精确长度的 wav"""
        s = self.spec
        frames = info.v_duration * float(s.fps)
        checks = [
            info.vcodec == "h264",
            info.vprofile == "High",
            info.pix_fmt == "yuv420p",
            info.rotation == 0,
            info.width == s.width and info.height == s.height,
            info.fps == s.fps and info.avg_fps == s.fps,
            info.sar in ("", "1:1", "0:1", "N/A"),
            bool(self.ref_extradata) and info.v_extradata == self.ref_extradata,
            abs(frames - round(frames)) < 0.02,   # 时长必须是整数帧
        ]
        return all(checks)

    # ---------- 处理单个素材 ----------
    def process(self, info: MediaInfo) -> CachedClip:
        hit = self.lookup(info.path)
        if hit:
            return hit
        key = self._key(info.path)
        ts = os.path.join(self.dir, key + ".ts")
        wav = os.path.join(self.dir, key + ".wav")
        tmp_ts = os.path.join(self.dir, key + ".part.ts")
        tmp_wav = os.path.join(self.dir, key + ".part.wav")
        meta = os.path.join(self.dir, key + ".json")
        s = self.spec
        W, H = s.width, s.height
        skip = self.can_skip_transcode(info)
        if skip:
            n_frames = int(round(info.v_duration * float(s.fps)))
        else:
            n_frames = max(1, int(round(info.v_duration * float(s.fps))))
        duration = n_frames / float(s.fps)
        n_samples = int(round(duration * AUDIO_SR))

        # 音频：重采样为 44100Hz 双声道，短了补静音、长了裁掉，采样数精确等于视频时长
        extra_in = []
        if info.has_audio:
            af = (f"[0:a:0]aresample={AUDIO_SR}:async=1:first_pts=0,"
                  f"aformat=sample_fmts=s16:channel_layouts=stereo,"
                  f"apad,atrim=end_sample={n_samples},asetpts=PTS-STARTPTS[a]")
        else:
            extra_in = ["-f", "lavfi", "-i", f"anullsrc=r={AUDIO_SR}:cl=stereo"]
            af = (f"[1:a]aformat=sample_fmts=s16:channel_layouts=stereo,"
                  f"atrim=end_sample={n_samples},asetpts=PTS-STARTPTS[a]")
        audio_out = ["-map", "[a]", "-c:a", "pcm_s16le", "-ar", str(AUDIO_SR), "-ac", "2",
                     "-f", "wav", tmp_wav]

        if skip:
            mode = "remux"
            # 视频直接复制（规格与缓存完全一致），只生成音频
            def build(_hw):     # 视频不解码，用不上显卡解码
                return [FFMPEG, "-hide_banner", "-v", "error", "-nostdin", "-y",
                        "-i", info.path, *extra_in, "-filter_complex", af,
                        "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "h264_mp4toannexb", "-an",
                        "-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", tmp_ts, *audio_out]
        else:
            mode = "transcode"
            vf = (f"[0:v:0]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2"
                  f":flags={SCALE_FLAGS},"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={s.fps},format=yuv420p,"
                  f"tpad=stop_mode=clone:stop_duration=1,trim=end_frame={n_frames},"
                  f"setpts=PTS-STARTPTS[v]")

            def build(hw):
                return [FFMPEG, "-hide_banner", "-v", "error", "-nostdin", "-y",
                        *hw, "-i", info.path, *extra_in, "-filter_complex", f"{vf};{af}",
                        "-map", "[v]", *video_encoder_args(s.encoder, s.fps, s.quality, s.speed),
                        "-pix_fmt", "yuv420p", "-an",
                        "-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", tmp_ts, *audio_out]
        try:
            if skip:
                code, _o, err = self.runner.run(build([]))
            else:
                code, _o, err = self.hw.run(self.runner, build)
        except Cancelled:
            _silent_remove(tmp_ts)
            _silent_remove(tmp_wav)
            raise
        ok = (code == 0 and os.path.isfile(tmp_ts) and os.path.getsize(tmp_ts) > 0
              and os.path.isfile(tmp_wav) and os.path.getsize(tmp_wav) > 44)
        if not ok:
            _silent_remove(tmp_ts)
            _silent_remove(tmp_wav)
            msg = (err.strip().splitlines() or ["未知错误"])[-1]
            raise RuntimeError(f"预处理失败：{msg}")
        os.replace(tmp_ts, ts)
        os.replace(tmp_wav, wav)
        size, asize = os.path.getsize(ts), os.path.getsize(wav)
        with open(meta, "w", encoding="utf-8") as f:
            json.dump({"fingerprint": self.fp, "src": info.path, "duration": duration,
                       "frames": n_frames, "size": size, "audio_size": asize, "mode": mode},
                      f, ensure_ascii=False)
        return CachedClip(info.path, ts, wav, duration, size, mode)


def _silent_remove(p):
    try:
        os.remove(p)
    except OSError:
        pass


# ======================================================================
# 直接拼接模式的“少数派修正”：以多数素材的规格为准，只把不一致的少数素材转成相同规格
# ======================================================================

@dataclass
class DirectTarget:
    width: int
    height: int
    fps: Fraction
    pix_fmt: str
    sample_rate: int
    channels: int
    encoder: str
    quality: int = 20
    speed: str = "normal"
    timescale: int = 15360

    def fingerprint(self):
        return (f"d2|{self.width}x{self.height}|{self.fps}|{self.pix_fmt}|{self.sample_rate}|"
                f"{self.channels}|{self.encoder}|q{self.quality}|{self.speed}|ts{self.timescale}")


class DirectFixer:
    def __init__(self, target: DirectTarget, runner: ProcessRunner, hw: HwDecode = None):
        self.t = target
        self.runner = runner
        self.hw = hw or HwDecode()
        self.dir = os.path.join(cache_root(), "direct_" + short_hash(target.fingerprint(), 16))
        os.makedirs(self.dir, exist_ok=True)

    def purge_stale(self):
        root = cache_root()
        removed = 0
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if (os.path.isdir(p) and name.startswith("direct_")
                    and os.path.abspath(p) != os.path.abspath(self.dir)):
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
        return removed

    def fix(self, info: MediaInfo, kind: str) -> str:
        """kind: audio = 视频直接复制、只重做音频；full = 视频也转成多数素材的规格。返回修正后文件路径"""
        size, mtime = file_sig(info.path)
        key = short_hash(f"{norm_path(info.path)}|{size}|{mtime}|{kind}", 24)
        out = os.path.join(self.dir, key + ".mp4")
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
        tmp = os.path.join(self.dir, key + ".part.mp4")
        t = self.t
        layout = "mono" if t.channels == 1 else "stereo"
        extra_in = []
        if info.has_audio:
            a_src = "[0:a:0]"
        else:
            extra_in = ["-f", "lavfi", "-t", f"{info.v_duration + 1:.3f}", "-i",
                        f"anullsrc=r={t.sample_rate}:cl={layout}"]
            a_src = "[1:a]"
        af = (f"{a_src}aresample={t.sample_rate}:async=1:first_pts=0,"
              f"aformat=sample_fmts=fltp:channel_layouts={layout},apad[a]")
        # 注意：apad 是无限长的，不能用 -shortest（滤镜图里会一直写下去），按视频时长精确截止
        dur = f"{info.v_duration:.6f}"
        common_tail = ["-c:a", "aac", "-b:a", "128k", "-ar", str(t.sample_rate), "-ac", str(t.channels),
                       "-t", dur, "-video_track_timescale", str(t.timescale),
                       "-movflags", "+faststart", "-f", "mp4", tmp]
        try:
            if kind == "audio":     # 视频不解码，用不上显卡解码
                code, _o, err = self.runner.run(
                    [FFMPEG, "-hide_banner", "-v", "error", "-nostdin", "-y", "-i", info.path, *extra_in,
                     "-filter_complex", af, "-map", "0:v:0", "-map", "[a]", "-c:v", "copy",
                     *common_tail])
            else:
                W, H = t.width, t.height
                vf = (f"[0:v:0]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2"
                      f":flags={SCALE_FLAGS},"
                      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={t.fps},"
                      f"format={t.pix_fmt}[v]")

                def build(hw):
                    return [FFMPEG, "-hide_banner", "-v", "error", "-nostdin", "-y",
                            *hw, "-i", info.path, *extra_in,
                            "-filter_complex", f"{vf};{af}", "-map", "[v]", "-map", "[a]",
                            *video_encoder_args(t.encoder, t.fps, t.quality, t.speed),
                            "-pix_fmt", t.pix_fmt, *common_tail]
                code, _o, err = self.hw.run(self.runner, build)
        except Cancelled:
            _silent_remove(tmp)
            raise
        if code != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
            _silent_remove(tmp)
            raise RuntimeError((err.strip().splitlines() or ["转换失败"])[-1])
        os.replace(tmp, out)
        return out
