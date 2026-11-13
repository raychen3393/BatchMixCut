"""任务调度：扫描 → 探测 → 预处理(缓存) → 规划组合 → 并行拼接 → 写历史/CSV

与界面无关：GUI 和命令行都调用这里，通过回调拿日志和进度。
"""
import csv
import json
import os
import random
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from fractions import Fraction
from typing import Callable, List, Optional

from . import concat as concat_mod
from . import ffmpeg_tools as fft
from .combo import ComboPlanner, code_to_indices, total_combos
from .ffmpeg_tools import (ENCODER_LABELS, Cancelled, HwDecode, MediaInfo, ProcessRunner, check_ffmpeg,
                           hw_decode_for, probe, resolve_encoder)
from .history import History
from .preprocess import CachedClip, ClipCache, DirectFixer, DirectTarget, TargetSpec, cache_root
from .utils import (data_root, dir_size, disk_kind, file_sig, free_space, human_size, is_network_path,
                    list_videos, norm_path, short_hash)


@dataclass
class JobConfig:
    folders: List[str] = field(default_factory=list)
    output_dir: str = ""
    prefix: str = "混剪_"
    csv_name: str = "【恺研发定制】"   # 清单文件名（后面会自动加上日期时间，避免覆盖）
    count: int = 10
    resolution: str = "first"      # "first" 或 "1080x1920"
    fps: str = "30"                # "first" 或 "30" / "29.97" ...
    encoder: str = "auto"          # auto / libx264 / h264_nvenc / h264_amf / h264_qsv
    quality: int = 20              # 越小越清晰，文件越大
    speed: str = "normal"          # normal 标准 / fast 更快（文件更大）
    auto_workers: bool = True      # 并行数按 CPU 核心数和硬盘类型自动设置
    low_priority: bool = True      # 后台低优先级运行，不卡电脑
    direct: bool = True            # 素材规格一致时直接拼接原文件（不预处理、不转码，最快）
    force_direct: bool = False     # 强制直接拼接：规格不一致也不转换（成品是否正常由用户自己负责）
    auto_clean: bool = True        # 换了公式（分镜文件夹变了）时自动清理缓存
    prep_workers: int = 3
    concat_workers: int = 4
    mode: str = "fast"             # fast 极速 / slow 慢速（转场）
    transition: str = "fade"       # 转场类型或 random
    trans_dur: float = 0.5
    use_history: bool = True
    seed: Optional[int] = None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Callbacks:
    log: Callable[[str], None] = print
    progress: Callable[[str, int, int], None] = lambda stage, done, total: None
    stats: Callable[[dict], None] = lambda d: None
    # 数量超过剩余组合时询问：返回 "remaining"（只生成剩余）/ "repeat"（允许重复）/ "cancel"
    ask_exceed: Callable[[int, int], str] = lambda requested, remaining: "remaining"


def parse_fps(s: str) -> Fraction:
    s = str(s).strip()
    table = {"23.976": Fraction(24000, 1001), "29.97": Fraction(30000, 1001),
             "59.94": Fraction(60000, 1001)}
    if s in table:
        return table[s]
    return Fraction(s).limit_denominator(1001)


def _check_duration(path, job, slow=False, trans_dur=0.0):
    """读成品文件头里记录的时长，和素材时长之和比较。明显偏长（时间戳异常导致的空白）就返回说明，
    正常返回空字符串。只读文件开头几十 KB，不启动 ffmpeg，几乎不花时间。读不出来就不判断。"""
    actual = concat_mod.mp4_duration(path)
    if actual is None:
        return ""
    expect = sum(c.duration for c in job)
    if slow:
        expect -= max(0, len(job) - 1) * min(trans_dur, 0.5)
    if actual > expect * 1.1 + 2:
        return f"应约 {expect:.0f} 秒，实际 {actual:.0f} 秒"
    return ""


class MixEngine:
    def __init__(self, cfg: JobConfig, cb: Callbacks = None):
        self.cfg = cfg
        self.cb = cb or Callbacks()
        self.stop_event = threading.Event()
        self.runner = ProcessRunner(self.stop_event)
        self._lock = threading.Lock()

    def stop(self):
        self.stop_event.set()
        self.runner.kill_all()

    def log(self, msg):
        self.cb.log(msg)

    def _check_stop(self):
        if self.stop_event.is_set():
            raise Cancelled()

    # ------------------------------------------------------------------
    def run(self) -> dict:
        summary = {"done": 0, "failed": 0, "stopped": False, "outputs": [], "csv": None,
                   "error": None}
        try:
            self._run(summary)
        except Cancelled:
            summary["stopped"] = True
            self.log("⏹ 已停止。已经完成的成品都保留了。")
        except Exception as e:  # noqa  任何异常都不让程序崩溃
            summary["error"] = str(e)
            self.log(f"❌ {e}")
        return summary

    def _run(self, summary):
        cfg = self.cfg = replace(self.cfg)   # 用副本，自动调整并行数时不改动界面传来的对象
        folders = list(cfg.folders)  # 冻结顺序：开始后不再改变
        fft.LOW_PRIORITY = bool(cfg.low_priority)
        if cfg.auto_workers:
            self._auto_tune(folders)
        self._set_threads()
        ok, msg = check_ffmpeg()
        if not ok:
            raise RuntimeError(msg)
        self.log(f"FFmpeg：{msg}")
        if not folders:
            raise RuntimeError("请先添加分镜文件夹")
        if not cfg.output_dir:
            raise RuntimeError("请先选择输出路径")
        os.makedirs(cfg.output_dir, exist_ok=True)
        old_copy = os.path.join(cache_root(), "netcopy")   # 旧版本留下的网络素材副本，已不再使用
        if os.path.isdir(old_copy):
            shutil.rmtree(old_copy, ignore_errors=True)
            self.log("已清理旧版本留下的网络素材本地副本")
        self._clean_on_formula_change(folders)
        if any(is_network_path(f) for f in folders):
            self.log("素材在网络盘上：读取速度取决于网络。需要转换的素材只从网络读一次，之后用本地缓存。")
        if is_network_path(cfg.output_dir):
            self.log("提示：输出路径在网络盘上，写入会比较慢。建议先输出到本地硬盘，混完再整批复制过去。")

        # 1. 扫描
        raw = [list_videos(f) for f in folders]
        for i, (f, files) in enumerate(zip(folders, raw), 1):
            self.log(f"分镜 {i}：{f}（{len(files)} 个视频文件）")
            if not files:
                raise RuntimeError(f"分镜 {i} 文件夹里没有视频：{f}")

        # 2. 检查素材规格。极速模式下“按需检查”：先定好这批的组合，只检查用得到的素材
        #    （文件夹里素材很多、或者素材在网络盘上时，能省掉不少等待）
        all_files = [p for files in raw for p in files]
        fast_path = cfg.mode == "fast" and (cfg.direct or cfg.force_direct)
        infos = {}
        if fast_path:
            valid = [list(files) for files in raw]
        else:
            self._probe_some(all_files, infos)
            valid = [[p for p in files if infos[p].ok] for files in raw]
            for i, files in enumerate(valid, 1):
                if not files:
                    raise RuntimeError(f"分镜 {i} 没有可用的视频素材")

        # 3. 先估算组合数，超出时提前询问用户
        decision, remaining = self._decide_exceed(folders, valid)
        if decision == "cancel":
            self.log("已取消。")
            return
        if decision != "repeat" and remaining <= 0:
            self.log("所有组合都已生成过（可在界面上“清空历史”后重来）。")
            return

        # 3.5 直接拼接：规格一致的素材直接复制原文件的数据流，只转换少数不一致的
        if fast_path:
            if self._try_direct(folders, valid, infos, decision, summary):
                return
            self._probe_some(all_files, infos)      # 退回统一转码流程：需要全部素材的规格
            valid = [[p for p in files if infos[p].ok] for files in raw]
            for i, files in enumerate(valid, 1):
                if not files:
                    raise RuntimeError(f"分镜 {i} 没有可用的视频素材")

        # 4. 目标规格
        first = infos[valid[0][0]]
        if cfg.resolution == "first":
            W, H = first.width, first.height
        else:
            W, H = (int(x) for x in cfg.resolution.lower().split("x"))
        W, H = W - W % 2, H - H % 2
        if cfg.fps == "first":
            fps = first.avg_fps if (first.avg_fps and first.avg_fps <= 120) else first.fps
            fps = Fraction(fps).limit_denominator(1001) if fps else Fraction(30)
            if fps > 60:
                fps = Fraction(60)
        else:
            fps = parse_fps(cfg.fps)
        enc = resolve_encoder(self.runner, cfg.encoder, self.log, cfg.speed)
        self._cap_for_gpu(enc)
        spec = TargetSpec(W, H, fps, enc, cfg.quality, cfg.speed)
        self.log(f"统一规格：{spec.describe()}（{ENCODER_LABELS.get(enc, enc)}）")

        # 5. 预处理 + 缓存
        cache = ClipCache(spec, self.runner, self.log, HwDecode(hw_decode_for(enc), self.log))
        removed = cache.purge_stale()
        if removed:
            self.log(f"编码参数变化，已作废并删除 {removed} 份旧缓存")
        cache.build_reference()

        # 6. 先规划组合，再只预处理“这批真正用得到”的素材。
        #    有素材预处理失败时，把它剔除后重新规划（最多重试几轮）。
        history = History(folders)
        materials = [list(m) for m in valid]
        clips = {}
        failed = set()
        codes, counts = [], []
        for _attempt in range(6):
            materials = [[p for p in m if p not in failed] for m in materials]
            for i, files in enumerate(materials, 1):
                if not files:
                    raise RuntimeError(f"分镜 {i} 没有可用的素材（全部损坏或预处理失败）")
            counts = [len(m) for m in materials]
            if cfg.use_history:
                used, usage = history.load_codes(materials)
            else:
                used, usage = set(), None
            planner = ComboPlanner(counts, used, usage, seed=cfg.seed)
            n = cfg.count
            if n > planner.remaining and decision != "repeat":
                if planner.remaining == 0:
                    self.log("所有组合都已生成过（可在界面上“清空历史”后重来）。")
                    return
                n = planner.remaining
            codes = planner.plan(n, allow_repeat=(decision == "repeat"))
            needed = set()
            for code in codes:
                for k, i in enumerate(code_to_indices(code, counts)):
                    needed.add(materials[k][i])
            todo = [p for m in materials for p in m if p in needed and p not in clips]
            total_mat = sum(len(m) for m in materials)
            self.log(f"本批用到 {len(needed)} 条素材（共 {total_mat} 条），"
                     f"其中需要预处理或检查缓存 {len(todo)} 条")
            new_clips = self._preprocess(cache, infos, todo)
            clips.update(new_clips)
            newly_failed = {p for p in todo if p not in new_clips}
            if not newly_failed:
                break
            failed |= newly_failed
            self.log(f"有 {len(newly_failed)} 条素材处理失败，已剔除并重新规划组合")
        else:
            raise RuntimeError("素材反复处理失败，请检查日志")
        if n < cfg.count and decision != "repeat":
            self.log(f"剩余可用组合 {n} 个，只生成这些")
        self.log(f"最多组合 {total_combos(counts):,}，历史已用 {len(planner.used_sorted):,}，"
                 f"本批生成 {len(codes):,} 条")

        self._log_ts_bad([p for p in needed if infos.get(p) and infos[p].ts_bad], infos)
        jobs = []
        for code in codes:
            idx = code_to_indices(code, counts)
            jobs.append([clips[materials[k][i]] for k, i in enumerate(idx)])

        # 7. 输出盘空间检查（视频流直接复制，所需空间≈片段大小之和）
        # 视频片段大小之和 + AAC 128kbps 音频
        need = sum(sum(c.size + c.duration * 16000 for c in job) for job in jobs)
        free = free_space(cfg.output_dir)
        self.log(f"预计占用输出空间 {human_size(need)}，输出盘剩余 {human_size(free)}")
        if need * 1.05 + 100 * 1024 * 1024 > free:
            raise RuntimeError(f"输出盘空间不足：需要约 {human_size(need * 1.05)}，"
                               f"剩余 {human_size(free)}")

        # 8. 并行拼接
        self._generate(jobs, folders, history, summary, spec)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _auto_tune(self, folders):
        """按 CPU 核心数和硬盘类型自动设置两个并行数"""
        cfg = self.cfg
        cpu = os.cpu_count() or 4
        names = {"ssd": "固态", "hdd": "机械", "unknown": "未识别"}
        src = {disk_kind(f) for f in folders}
        src_kind = "hdd" if "hdd" in src else ("ssd" if src == {"ssd"} else "unknown")
        out_kind = disk_kind(cfg.output_dir)
        # 预处理（转码）吃 CPU：大约每 4 个线程开 1 路
        cfg.prep_workers = max(1, min(6, cpu // 4))
        # 拼接吃硬盘：机械硬盘并行多了磁头来回跳反而慢；固态可以多开
        if "hdd" in (src_kind, out_kind):
            cfg.concat_workers = 2
        elif src_kind == "ssd" and out_kind == "ssd":
            cfg.concat_workers = max(4, min(8, cpu // 2))
        else:
            cfg.concat_workers = 4
        self.log(f"自动并行：CPU {cpu} 线程，素材盘 {names[src_kind]}，输出盘 {names[out_kind]} → "
                 f"预处理并行 {cfg.prep_workers}，拼接并行 {cfg.concat_workers}")

    def _set_threads(self):
        # 多条同时转码时，平分 CPU 线程，避免几十上百个线程互相抢（更快，也更不卡）
        cpu = os.cpu_count() or 4
        workers = max(1, self.cfg.prep_workers)
        fft.X264_THREADS = max(2, cpu // workers) if workers > 1 else 0

    def _cap_for_gpu(self, enc):
        """显卡编码同时能开的路数有限（旧驱动只有 3 路），自动模式下不超过 3"""
        if self.cfg.auto_workers and enc != "libx264" and self.cfg.prep_workers > 3:
            self.cfg.prep_workers = 3
            self.log("使用显卡编码，预处理并行调整为 3")

    def _clean_on_formula_change(self, folders):
        """换了公式（分镜文件夹和上次不一样）时清空缓存。只调整顺序不算换公式。"""
        key = short_hash("\n".join(sorted(norm_path(f) for f in folders)), 20)
        mark = os.path.join(data_root(), "cache_owner.txt")
        try:
            with open(mark, "r", encoding="utf-8") as f:
                last = f.read().strip()
        except OSError:
            last = ""
        if last and last != key and self.cfg.auto_clean:
            root = cache_root()
            size = dir_size(root)
            if size > 0:
                for name in os.listdir(root):
                    path = os.path.join(root, name)
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                self.log(f"换了公式，已自动清理上一个公式的缓存（释放 {human_size(size)}）")
        try:
            with open(mark, "w", encoding="utf-8") as f:
                f.write(key)
        except OSError:
            pass

    def _probe_some(self, paths, infos):
        """检查还没检查过的素材，结果写进 infos；返回其中损坏/无法识别的"""
        todo = [p for p in dict.fromkeys(paths) if p not in infos]
        if not todo:
            return set()
        infos.update(self._probe_all(todo))
        bad = {p for p in todo if not infos[p].ok}
        for p in todo:
            if p in bad:
                self.log(f"⚠ 跳过损坏/无法识别的素材：{p}（{infos[p].error}）")
        return bad

    def _probe_all(self, all_files):
        cache_path = os.path.join(data_root(), "probe_cache.json")
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                pc = json.load(f)
        except (OSError, ValueError):
            pc = {}

        def key(p):
            try:
                size, mtime = file_sig(p)
                return f"v4|{os.path.abspath(p)}|{size}|{mtime}"
            except OSError:
                return None

        infos, todo, keys = {}, [], {}
        for p in all_files:
            k = keys[p] = key(p)
            d = pc.get(k) if k else None
            if d:
                try:
                    d = dict(d)
                    for fk in ("fps", "avg_fps"):
                        d[fk] = Fraction(d[fk]) if d.get(fk) else None
                    d["path"] = p
                    infos[p] = MediaInfo(**d)
                    continue
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            todo.append(p)
        total = len(all_files)
        done = total - len(todo)
        self.cb.progress("probe", done, total)
        if todo:
            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = [ex.submit(probe, self.runner, p) for p in todo]
                for fu in as_completed(futs):
                    self._check_stop()
                    info = fu.result()
                    infos[info.path] = info
                    k = keys.get(info.path)
                    if k and info.ok:
                        d = asdict(info)
                        d["fps"] = str(info.fps) if info.fps else None
                        d["avg_fps"] = str(info.avg_fps) if info.avg_fps else None
                        pc[k] = d
                    done += 1
                    self.cb.progress("probe", done, total)
            try:
                if len(pc) > 50000:
                    pc = {keys[p]: pc[keys[p]] for p in all_files if keys[p] in pc}
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(pc, f, ensure_ascii=False)
            except OSError:
                pass
        return infos

    @staticmethod
    def _vkey(info):
        """视频能否直接复制拼接：编码、分辨率、像素格式、旋转、帧率都必须一致
        （实测帧率不同时直接拼会出现时间戳倒退，播放卡顿）"""
        fps = info.avg_fps or info.fps
        fps = str(Fraction(fps).limit_denominator(1001)) if fps else "?"
        return (info.vcodec, info.width, info.height, info.pix_fmt, info.rotation, fps)

    @staticmethod
    def _akey(info):
        return (info.acodec, info.sample_rate, info.channels) if info.has_audio else None

    @staticmethod
    def _vtext(k):
        try:
            fps = f"{float(Fraction(k[5])):g}fps"
        except (ValueError, ZeroDivisionError):
            fps = "帧率未知"
        return f"{k[0]} {k[1]}x{k[2]} {fps} {k[3]}" + (f" 旋转{k[4]}°" if k[4] else "")

    @staticmethod
    def _atext(k):
        return "无音轨" if k is None else f"{k[0]} {k[1]}Hz {k[2]}声道"

    def _try_direct(self, folders, valid, infos, decision, summary):
        cfg = self.cfg
        force = bool(cfg.force_direct)
        history = History(folders)
        materials = [list(m) for m in valid]
        for _attempt in range(10):
            for i, files in enumerate(materials, 1):
                if not files:
                    raise RuntimeError(f"分镜 {i} 没有可用的视频素材")
            counts = [len(m) for m in materials]
            used, usage = history.load_codes(materials) if cfg.use_history else (set(), None)
            planner = ComboPlanner(counts, used, usage, seed=cfg.seed)
            n = cfg.count
            if n > planner.remaining and decision != "repeat":
                n = planner.remaining
            codes = planner.plan(n, allow_repeat=(decision == "repeat"))
            needed = {}
            for code in codes:
                for k, i in enumerate(code_to_indices(code, counts)):
                    needed[materials[k][i]] = None
            needed = list(needed)
            bad = self._probe_some(needed, infos)       # 只检查这批用得到的素材
            if not bad:
                break
            materials = [[p for p in m if p not in bad] for m in materials]   # 去掉坏素材，重新排组合
        else:
            raise RuntimeError("损坏的素材太多，请检查素材文件夹")
        if not codes:
            self.log("所有组合都已生成过（可在界面上“清空历史”后重来）。")
            return True

        # 以多数素材的规格为准（只在“能直接拼接”的规格里选：H.264、无旋转、帧率已知、8 位色）
        keys = [self._vkey(infos[p]) for p in needed]
        usable = Counter(k for k in keys if k[0] == "h264" and k[4] == 0 and k[5] != "?"
                         and k[3] in ("yuv420p", "yuvj420p"))
        if usable:
            dom_v = usable.most_common(1)[0][0]
        else:
            # 没有任何素材能直接拼（例如全是 H.265、手机竖拍带旋转）：定一个目标规格，全部转一次，
            # 之后同样走“直接拼接”，比旧的统一转码流程快得多
            sizes = Counter((infos[p].width - infos[p].width % 2, infos[p].height - infos[p].height % 2)
                            for p in needed)
            W, H = sizes.most_common(1)[0][0]
            fpsc = Counter(k[5] for k in keys if k[5] != "?")
            fps_s = fpsc.most_common(1)[0][0] if fpsc else "30"
            try:
                if not (1 <= Fraction(fps_s) <= 60):
                    fps_s = "30"
            except (ValueError, ZeroDivisionError):
                fps_s = "30"
            dom_v = ("h264", W, H, "yuv420p", 0, fps_s)
            if not force:
                self.log("素材的编码不能直接拼接（常见于 H.265 或手机竖拍视频），需要先全部转换一次，之后会直接用缓存")
        acount = Counter(self._akey(infos[p]) for p in needed
                         if infos[p].has_audio and infos[p].acodec == "aac")
        dom_a = acount.most_common(1)[0][0] if acount else ("aac", 44100, 2)
        dom_fps = Fraction(dom_v[5])

        tbcount = Counter(infos[p].v_timebase for p in needed if self._vkey(infos[p]) == dom_v)
        dom_tb = tbcount.most_common(1)[0][0] if tbcount else ""
        try:
            timescale = Fraction(dom_tb).denominator if dom_tb else 0
        except (ValueError, ZeroDivisionError):
            timescale = 0
        if timescale <= 1:
            timescale, dom_tb = 90000, "1/90000"     # 定不下来就统一成 90000，不一致的重新封装

        # 实测：帧率或时间基不同的文件直接拼会出现时间戳倒退（播放卡顿/花屏）
        direct, audio_fix, full_fix = [], [], []
        for p in needed:
            info = infos[p]
            if self._vkey(info) != dom_v:
                full_fix.append(p)
            elif self._akey(info) != dom_a or info.v_timebase != dom_tb or info.ts_bad:
                audio_fix.append(p)   # 视频流直接复制，只重新封装（统一时间基）+ 重做音频
            else:
                direct.append(p)
        self.log(f"本批用到 {len(needed)} 条素材（共 {sum(counts)} 条）。以多数规格为准："
                 f"{self._vtext(dom_v)} / {self._atext(dom_a)}")

        def why(p):
            info = infos[p]
            if info.ts_bad:
                return "时间戳异常"
            if self._vkey(info) != dom_v:
                return self._vtext(self._vkey(info))
            if info.v_timebase != dom_tb:
                return "时间基 " + info.v_timebase
            return self._atext(self._akey(info))

        if force:
            odd = audio_fix + full_fix
            if odd:
                self.log(f"⚠ 强制直接拼接：有 {len(odd)} 条素材的规格和多数不一样，没有转换。"
                         f"用到它们的成品可能花屏、卡顿或音画不同步，请重点检查：")
                for p in odd[:30]:
                    self.log(f"    · {os.path.basename(os.path.dirname(p))}\\{os.path.basename(p)}（{why(p)}）")
                if len(odd) > 30:
                    self.log(f"    … 还有 {len(odd) - 30} 条")
            # 时间戳异常、或者时间基（时间的计数单位）和多数素材不一样的，即使强制也要重新封装：
            # 不封装的话，成品时长会被放大几百倍（两分钟的成品显示 20 多小时、后面全是空白）
            # 没有声音的素材也要补一条静音：否则它排在第一段时，整条成品都会没有声音
            any_audio = any(infos[p].has_audio for p in needed)
            audio_fix = [p for p in needed if infos[p].ts_bad or infos[p].v_timebase != dom_tb
                         or (any_audio and not infos[p].has_audio)]
            bad_ts = set(audio_fix)
            direct, full_fix = [p for p in needed if p not in bad_ts], []
            if audio_fix:
                self.log(f"  · 其中 {len(audio_fix)} 条的时间基/时间戳和多数素材不一样，或者没有声音，仍然会重新封装"
                         f"（画面不重新编码，很快；不封装的话成品会显示几十个小时或没有声音）")
        else:
            self.log(f"  · 直接使用原文件：{len(direct)} 条")
            if audio_fix:
                reasons = Counter(why(p) for p in audio_fix)
                self.log(f"  · 只需重新封装（画面不重新编码，很快）：{len(audio_fix)} 条（" +
                         "，".join(f"{k} {v} 条" for k, v in reasons.most_common(4)) + "）")
            if full_fix:
                reasons = Counter(why(p) for p in full_fix)
                self.log(f"  · 画面规格不同需要转码：{len(full_fix)} 条（" +
                         "，".join(f"{k} {v} 条" for k, v in reasons.most_common(4)) + "）")

        clip_path = {p: p for p in direct}
        fixers = []      # 用到时才创建；后面“成品时长不对 → 自动重做”也用它

        def get_fixer(enc="libx264", hw=False):
            if not fixers:
                target = DirectTarget(dom_v[1], dom_v[2], dom_fps, dom_v[3], dom_a[1], dom_a[2], enc,
                                      cfg.quality, cfg.speed, timescale)
                fixers.append(DirectFixer(target, self.runner,
                                          HwDecode(hw_decode_for(enc) if hw else "", self.log)))
            return fixers[0]

        if audio_fix or full_fix:
            enc = resolve_encoder(self.runner, cfg.encoder, self.log, cfg.speed) if full_fix else "libx264"
            self._cap_for_gpu(enc)
            fixer = get_fixer(enc, bool(full_fix))
            if fixer.purge_stale():
                self.log("规格变化，已清理旧的修正缓存")
            todo = [(p, "audio") for p in audio_fix] + [(p, "full") for p in full_fix]
            total = len(todo)
            t0 = time.time()
            self.cb.progress("prep", 0, total)
            failed = []

            def work(item):
                self._check_stop()
                return item[0], fixer.fix(infos[item[0]], item[1])

            with ThreadPoolExecutor(max_workers=max(1, cfg.prep_workers)) as ex:
                futs = {ex.submit(work, it): it for it in todo}
                try:
                    for k, fu in enumerate(as_completed(futs), 1):
                        p = futs[fu][0]
                        try:
                            _p, out = fu.result()
                            clip_path[p] = out
                        except Cancelled:
                            raise
                        except Exception as e:  # noqa
                            failed.append(p)
                            self.log(f"⚠ 素材处理失败：{p}（{e}）")
                        self.cb.progress("prep", k, total)
                except Cancelled:
                    ex.shutdown(wait=False, cancel_futures=True)
                    raise
            self._check_stop()
            self.log(f"素材转换完成，用时 {time.time() - t0:.1f}s")
            if failed:
                self.log("有素材处理失败，改为统一转码流程")
                return False
        else:
            self.cb.progress("prep", 1, 1)
            self.log("强制直接拼接：不转换任何素材" if force
                     else "✔ 全部素材规格一致，直接拼接：不预处理、不转码")

        if n < cfg.count and decision != "repeat":
            self.log(f"剩余可用组合 {n} 个，只生成这些")
        self.log(f"最多组合 {total_combos(counts):,}，历史已用 {len(planner.used_sorted):,}，"
                 f"本批生成 {len(codes):,} 条")
        clips = {}
        for p in needed:
            info = infos[p]
            path = clip_path[p]
            try:    # 直接用的原文件：大小在扫描文件夹时已经拿到，不用再问一次硬盘/网络
                size = file_sig(p)[0] if path == p else os.path.getsize(path)
            except OSError:
                size = 0
            dur = max(info.v_duration, info.a_duration)
            try:   # 估算这条素材在成品文件头（索引）里占多少字节，宁多勿少；估不出来就用传统做法
                fps = max(float(info.avg_fps or info.fps or 30), float(dom_fps))
                sr = max(info.sample_rate or 0, dom_a[1], 44100)
                moov = int(dur * fps * 32 + dur * sr / 1024 * 16) + 2048
            except (TypeError, ValueError, ZeroDivisionError):
                moov = 0
            clips[p] = CachedClip(p, path, "", dur, size, "direct", moov)
        self._log_ts_bad([p for p in needed if infos[p].ts_bad], infos)
        jobs = [[clips[materials[k][i]] for k, i in enumerate(code_to_indices(code, counts))]
                for code in codes]
        need = sum(sum(c.size for c in job) for job in jobs)
        free = free_space(cfg.output_dir)
        self.log(f"预计占用输出空间 {human_size(need)}，输出盘剩余 {human_size(free)}")
        if need * 1.02 + 100 * 1024 * 1024 > free:
            raise RuntimeError(f"输出盘空间不足：需要约 {human_size(need * 1.02)}，"
                               f"剩余 {human_size(free)}")

        def repair(job, level):
            """成品时长不对时的兜底：把这一条用到的素材处理后再拼一次。
            level 1：原样使用的素材重新封装（统一时间基、时间归零，画面不重新编码）；
            level 2：全部重新编码成统一规格（慢，但最稳）。"""
            fixer = get_fixer()
            new = []
            for c in job:
                self._check_stop()
                if level == 1 and c.path != c.src:
                    new.append(c)           # 已经处理过的不重复处理
                    continue
                path = fixer.fix(infos[c.src], "audio" if level == 1 else "full")
                new.append(CachedClip(c.src, path, "", c.duration, os.path.getsize(path), "direct", c.moov))
            return new

        self._generate(jobs, folders, history, summary, None, repair)
        return True

    def _log_ts_bad(self, paths, infos):
        if not paths:
            return
        self.log(f"⚠ 有 {len(paths)} 条素材时间戳异常（画面和声音的起点对不上），已自动修正：")
        for p in paths[:20]:
            self.log(f"    · {os.path.basename(os.path.dirname(p))}\\{os.path.basename(p)}"
                     f"（相差 {infos[p].ts_gap:.0f} 秒）")
        if len(paths) > 20:
            self.log(f"    … 还有 {len(paths) - 20} 条")

    def _decide_exceed(self, folders, valid):
        counts = [len(v) for v in valid]
        total = total_combos(counts)
        used = 0
        if self.cfg.use_history:
            used_codes, _ = History(folders).load_codes(valid)
            used = len(used_codes)
        remaining = total - used
        if self.cfg.count <= remaining:
            return "ok", remaining
        return self.cb.ask_exceed(self.cfg.count, remaining), remaining

    def _preprocess(self, cache, infos, todo):
        clips = {}
        stat = {"transcode": 0, "remux": 0, "cached": 0, "failed": 0}
        total = len(todo)
        t0 = time.time()
        self.cb.progress("prep", 0, total)

        def work(p):
            self._check_stop()
            return cache.process(infos[p])

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.prep_workers)) as ex:
            futs = {ex.submit(work, p): p for p in todo}
            try:
                for n, fu in enumerate(as_completed(futs), 1):
                    p = futs[fu]
                    try:
                        c = fu.result()
                        clips[p] = c
                        stat[c.mode] += 1
                    except Cancelled:
                        raise
                    except Exception as e:  # noqa
                        stat["failed"] += 1
                        self.log(f"⚠ 素材处理失败，已跳过：{p}（{e}）")
                    self.cb.progress("prep", n, total)
            except Cancelled:
                ex.shutdown(wait=False, cancel_futures=True)
                raise
        self._check_stop()
        if total == 0:
            return clips
        self.log(f"预处理完成，用时 {time.time() - t0:.1f}s：转码 {stat['transcode']}，"
                 f"规格一致直接复用 {stat['remux']}，命中缓存 {stat['cached']}，失败 {stat['failed']}")
        return clips

    def _next_index(self):
        pat = re.compile(r"^" + re.escape(self.cfg.prefix) + r"(\d+)\.mp4$", re.IGNORECASE)
        mx = 0
        for name in os.listdir(self.cfg.output_dir):
            m = pat.match(name)
            if m:
                mx = max(mx, int(m.group(1)))
        return mx + 1

    def _generate(self, jobs, folders, history, summary, spec, repair=None):
        cfg = self.cfg
        total = len(jobs)
        if total == 0:
            return
        start = self._next_index()
        width = max(4, len(str(start + total - 1)))
        work_dir = os.path.join(data_root(), "work", f"{os.getpid()}_{int(time.time() * 1000)}")
        os.makedirs(work_dir, exist_ok=True)
        rng = random.Random(cfg.seed)
        rows = []
        done = 0
        t0 = time.time()
        slow = cfg.mode == "slow"
        self.cb.progress("gen", 0, total)
        if slow:
            self.log("慢速模式：每条都要重新编码，速度会明显变慢")

        def out_of(i):
            name = f"{cfg.prefix}{start + i:0{width}d}.mp4"
            return name, os.path.join(cfg.output_dir, name)

        def one(i):
            job = jobs[i]
            name, out = out_of(i)
            if slow:
                concat_mod.concat_with_transitions(self.runner, job, out, spec.encoder, spec.fps,
                                                   spec.quality, cfg.transition, cfg.trans_dur,
                                                   random.Random(rng.random()))
            elif job[0].mode == "direct":
                r = concat_mod.reserve_for(job)
                if r:
                    try:
                        concat_mod.concat_direct(self.runner, job, out, work_dir, r)
                        return
                    except Cancelled:
                        raise
                    except Exception:  # noqa  预留的位置不够等：退回传统做法再试一次
                        pass
                concat_mod.concat_direct(self.runner, job, out, work_dir, 0)
            else:
                concat_mod.concat_fast(self.runner, job, out, work_dir)

        def work(idxs):
            """处理一小批，返回 [(序号, 错误信息或 None)]"""
            self._check_stop()
            if len(idxs) > 1:
                try:
                    concat_mod.concat_direct_batch(self.runner, [(jobs[i], out_of(i)[1]) for i in idxs],
                                                   work_dir)
                    return [(i, None) for i in idxs]
                except Cancelled:
                    raise
                except Exception:  # noqa  整批失败 → 逐条重试，找出真正有问题的那条
                    pass
            res = []
            for i in idxs:
                self._check_stop()
                try:
                    one(i)
                    res.append((i, None))
                except Cancelled:
                    raise
                except Exception as e:  # noqa
                    res.append((i, str(e)))
            return res

        workers = max(1, cfg.concat_workers)
        # 直接拼接模式下，一个 ffmpeg 进程一次拼多条（最多 4 条）；数量少时不合批，保证并行
        # 机械硬盘例外：一个进程拼 4 条 = 同时读写好几个文件，磁头来回跳反而慢，所以一次只拼 1 条
        batch = 1
        on_hdd = "hdd" in {disk_kind(f) for f in folders} | {disk_kind(cfg.output_dir)}
        if not slow and jobs[0][0].mode == "direct" and not on_hdd:
            batch = max(1, min(4, total // workers))
        chunks = [list(range(k, min(total, k + batch))) for k in range(0, total, batch)]
        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(work, ch) for ch in chunks]
                try:
                    for fu in as_completed(futs):
                        for i, err in fu.result():
                            if err is not None:
                                summary["failed"] += 1
                                self.log(f"⚠ 生成失败：{err}")
                                continue
                            name, out = out_of(i)
                            job = jobs[i]
                            bad = _check_duration(out, job, slow, cfg.trans_dur)
                            if bad and repair is not None:
                                # 兜底：先把素材重新封装后重拼；还不行就把素材重新编码后再拼
                                first_bad = bad
                                for level in (1, 2):
                                    try:
                                        job2 = repair(job, level)
                                        concat_mod.concat_direct(self.runner, job2, out, work_dir, 0)
                                        bad = _check_duration(out, job2, slow, cfg.trans_dur)
                                    except Cancelled:
                                        raise
                                    except Exception as e:  # noqa
                                        bad = f"重做失败：{e}"
                                    if not bad:
                                        summary["repaired"] = summary.get("repaired", 0) + 1
                                        self.log(f"  · {name} 第一次拼出来时长不对（{first_bad}），已自动"
                                                 + ("重新封装素材" if level == 1 else "重新编码素材")
                                                 + "后重做，现在正常。素材："
                                                 + "、".join(os.path.basename(c.src) for c in job))
                                        break
                            if bad:
                                concat_mod._rm(out)
                                summary["failed"] += 1
                                self.log(f"⚠ {name} 时长不对（{bad}），已删除。用到的素材："
                                         + "、".join(os.path.basename(c.src) for c in job))
                                continue
                            names = [os.path.basename(c.src) for c in job]
                            history.append(names, name)
                            rows.append((start + i, name, [c.src for c in job]))
                            summary["outputs"].append(out)
                            done += 1
                            summary["done"] = done
                            elapsed = time.time() - t0
                            self.cb.progress("gen", done, total)
                            self.cb.stats({"done": done, "total": total, "elapsed": elapsed,
                                           "avg": elapsed / done})
                            if done % 50 == 0 or done == total:
                                self.log(f"已生成 {done}/{total}，平均每条 {elapsed / done:.2f}s")
                except Cancelled:
                    for f in futs:
                        f.cancel()
                    raise
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            if rows:
                summary["csv"] = self._write_csv(rows, folders)

    def _write_csv(self, rows, folders):
        rows.sort(key=lambda r: r[0])
        ts = time.strftime("%Y%m%d_%H%M%S")
        base = re.sub(r'[\\/:*?"<>|\r\n]', "_", (self.cfg.csv_name or "").strip()) or "清单"
        path = os.path.join(self.cfg.output_dir, f"{base}_{ts}.csv")
        header = ["序号", "成品文件"] + [f"分镜{k + 1}（{os.path.basename(os.path.normpath(f))}）"
                                        for k, f in enumerate(folders)] + ["素材完整路径"]
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            for idx, name, srcs in rows:
                w.writerow([idx, name] + [os.path.basename(s) for s in srcs] + [" | ".join(srcs)])
        self.log(f"清单已导出：{path}")
        return path
