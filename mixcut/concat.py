"""拼接：极速模式（concat demuxer + 流复制，不重新编码）与慢速模式（转场，需要重新编码）"""
import os
import random
from fractions import Fraction

from .ffmpeg_tools import AUDIO_ARGS, FFMPEG, Cancelled, ProcessRunner, video_encoder_args

TRANSITIONS = ["fade", "dissolve", "wipeleft", "wiperight", "slideleft", "slideright",
               "slideup", "slidedown", "circleopen", "smoothleft", "smoothright"]


def _concat_escape(path: str) -> str:
    """concat 列表里的路径：统一用正斜杠，单引号转义为 '\\''"""
    p = os.path.abspath(path).replace("\\", "/")
    return p.replace("'", "'\\''")


def write_concat_list(paths_durations, list_path):
    """写 concat 列表；显式写出每段 duration，保证下一段从精确的时间点开始"""
    with open(list_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("ffconcat version 1.0\n")
        for path, dur in paths_durations:
            f.write(f"file '{_concat_escape(path)}'\n")
            f.write(f"duration {dur:.6f}\n")


def _finish(runner, args, tmp_out, final_out):
    try:
        code, _o, err = runner.run(args)
    except Cancelled:
        _rm(tmp_out)
        raise
    if code != 0 or _input_error(err) or not os.path.isfile(tmp_out) or os.path.getsize(tmp_out) == 0:
        _rm(tmp_out)
        raise RuntimeError((err.strip().splitlines() or ["拼接失败"])[-1])
    os.replace(tmp_out, final_out)


def _head_args(reserve):
    """成品的“边下边播”做法：
    reserve > 0：在文件开头预留这么多字节放索引，写完数据后直接填进去（只写一遍硬盘）；
    reserve = 0：传统做法 faststart，写完后把整个文件再搬一遍（多读写一遍，慢）。"""
    if reserve and reserve > 0:
        return ["-moov_size", str(int(reserve))]
    return ["-movflags", "+faststart"]


def reserve_for(clips):
    r = sum(getattr(c, "moov", 0) or 0 for c in clips)
    return r + 16384 if r > 0 and all(getattr(c, "moov", 0) for c in clips) else 0


def concat_direct(runner: ProcessRunner, clips, out_path, work_dir, reserve=0):
    """直接拼接原始素材：音视频都只复制数据流，完全不编码（要求素材规格一致）"""
    base = os.path.splitext(os.path.basename(out_path))[0]
    lst = os.path.join(work_dir, base + ".txt")
    with open(lst, "w", encoding="utf-8", newline="\n") as f:
        f.write("ffconcat version 1.0\n")
        for c in clips:
            f.write(f"file '{_concat_escape(c.path)}'\n")
    tmp_out = out_path + ".part"
    args = [FFMPEG, "-hide_banner", "-v", "error", "-nostdin", "-y",
            "-f", "concat", "-safe", "0", "-i", lst,
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            *_head_args(reserve), "-f", "mp4", tmp_out]
    try:
        _finish(runner, args, tmp_out, out_path)
    finally:
        _rm(lst)


def concat_direct_batch(runner: ProcessRunner, items, work_dir, use_reserve=True):
    """一个 ffmpeg 进程一次拼多条成品（省掉反复启动 ffmpeg 的开销，实测快约 20%）。
    items: [(clips, out_path), ...]。任何一条出问题都会整批失败并清理，由调用方改为逐条重试。"""
    args = [FFMPEG, "-hide_banner", "-v", "error", "-nostdin", "-y"]
    lists, tmps = [], []
    for clips, out_path in items:
        base = os.path.splitext(os.path.basename(out_path))[0]
        lst = os.path.join(work_dir, base + ".txt")
        with open(lst, "w", encoding="utf-8", newline="\n") as f:
            f.write("ffconcat version 1.0\n")
            for c in clips:
                f.write(f"file '{_concat_escape(c.path)}'\n")
        lists.append(lst)
        args += ["-f", "concat", "-safe", "0", "-i", lst]
    for j, (_clips, out_path) in enumerate(items):
        tmp = out_path + ".part"
        tmps.append(tmp)
        args += ["-map", f"{j}:v:0", "-map", f"{j}:a:0?", "-c", "copy",
                 *_head_args(reserve_for(_clips) if use_reserve else 0), "-f", "mp4", tmp]
    try:
        try:
            code, _o, err = runner.run(args)
        except Cancelled:
            for t in tmps:
                _rm(t)
            raise
        ok = code == 0 and not _input_error(err) and all(os.path.isfile(t) and os.path.getsize(t) > 0 for t in tmps)
        if not ok:
            for t in tmps:
                _rm(t)
            raise RuntimeError((err.strip().splitlines() or ["拼接失败"])[-1])
        for t, (_clips, out_path) in zip(tmps, items):
            os.replace(t, out_path)
    finally:
        for lst in lists:
            _rm(lst)


def concat_fast(runner: ProcessRunner, clips, out_path, work_dir):
    """极速拼接：视频流直接复制（不重新编码，速度取决于硬盘）；
    音频是 PCM，拼接后编一次 AAC（音频编码极快，换来采样级精确的音画同步）"""
    base = os.path.splitext(os.path.basename(out_path))[0]
    vlist = os.path.join(work_dir, base + ".v.txt")
    alist = os.path.join(work_dir, base + ".a.txt")
    write_concat_list([(c.path, c.duration) for c in clips], vlist)
    write_concat_list([(c.audio, c.duration) for c in clips], alist)
    tmp_out = out_path + ".part"
    args = [FFMPEG, "-hide_banner", "-v", "error", "-nostdin", "-y",
            "-f", "concat", "-safe", "0", "-i", vlist,
            "-f", "concat", "-safe", "0", "-i", alist,
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *AUDIO_ARGS,
            "-movflags", "+faststart", "-video_track_timescale", "90000",
            "-f", "mp4", tmp_out]
    try:
        _finish(runner, args, tmp_out, out_path)
    finally:
        _rm(vlist)
        _rm(alist)


def concat_with_transitions(runner: ProcessRunner, clips, out_path, encoder: str, fps: Fraction,
                            quality: int, transition: str, trans_dur: float, rng: random.Random):
    """慢速模式：片段之间加转场（xfade + acrossfade），整条重新编码"""
    if len(clips) == 1:
        return concat_fast(runner, clips, out_path, os.path.dirname(out_path))
    min_d = min(c.duration for c in clips)
    td = max(0.1, min(trans_dur, min_d / 2 - 0.05))
    inputs = []
    for c in clips:
        inputs += ["-i", c.path, "-i", c.audio]   # 第 k 段：视频 2k，音频 2k+1
    parts = []
    vprev, aprev = "[0:v]", "[1:a]"
    acc = clips[0].duration
    for k in range(1, len(clips)):
        t = rng.choice(TRANSITIONS) if transition == "random" else transition
        offset = acc - td
        vout, aout = f"[v{k}]", f"[a{k}]"
        parts.append(f"{vprev}[{2 * k}:v]xfade=transition={t}:duration={td:.3f}:offset={offset:.3f}{vout}")
        parts.append(f"{aprev}[{2 * k + 1}:a]acrossfade=d={td:.3f}{aout}")
        vprev, aprev = vout, aout
        acc = offset + clips[k].duration
    parts.append(f"{vprev}format=yuv420p[vo]")
    tmp_out = out_path + ".part"
    args = [FFMPEG, "-hide_banner", "-v", "error", "-nostdin", "-y", *inputs,
            "-filter_complex", ";".join(parts), "-map", "[vo]", "-map", aprev,
            *video_encoder_args(encoder, fps, quality), "-pix_fmt", "yuv420p", *AUDIO_ARGS,
            "-movflags", "+faststart", "-f", "mp4", tmp_out]
    _finish(runner, args, tmp_out, out_path)


def _input_error(err: str) -> bool:
    return any(k in err for k in ("Impossible to open", "Error during demuxing",
                                  "Error retrieving a packet"))


def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass
