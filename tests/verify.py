"""校验成品：顺序 / 不重复 / 无解码错误 / 音画同步（白闪 vs 哔声）/ 片段时长
用法：python verify.py 输出目录 清单.csv [fps]"""
import csv
import os
import re
import subprocess
import sys
import tempfile

out_dir, csv_path = sys.argv[1], sys.argv[2]
FPS = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
POS_TOL = float(sys.argv[4]) if len(sys.argv) > 4 else 1.01 / FPS
FOLDER_COLOR = {"新建文件夹": "R", "新建文件夹 (2)": "G", "新建文件夹 (10)": "B"}
EXPECT_ORDER = ["R", "G", "B"]
NO_AUDIO = {("新建文件夹", "第三条.mkv")} | {(f, "d_noaudio.mp4") for f in ("新建文件夹", "新建文件夹 (2)", "新建文件夹 (10)")}


def sh(args):
    return subprocess.run(args, capture_output=True, text=True)


def src_duration(path):
    """真实视频时长：解码数帧（与被测程序的算法无关，作为独立的“标准答案”）"""
    r = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames,r_frame_rate", "-of", "csv=p=0", path])
    rate, n = r.stdout.strip().splitlines()[0].split(",")[:2]   # 带旋转信息的文件会多出几列
    a, b = rate.split("/")
    return int(n) * int(b) / int(a)


def frame_stats(path):
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as t:
        tmp = t.name
    sh(["ffmpeg", "-v", "error", "-i", path, "-vf",
        f"signalstats,metadata=mode=print:file={tmp}", "-an", "-f", "null", "-"])
    frames, cur = [], {}
    for line in open(tmp, encoding="utf-8"):
        m = re.match(r"frame:\s*(\d+)\s+pts:\s*\S+\s+pts_time:([\d.]+)", line)
        if m:
            cur = {"t": float(m.group(2))}
            frames.append(cur)
            continue
        m = re.match(r"lavfi\.signalstats\.(YMAX|UAVG|VAVG)=([\d.]+)", line)
        if m:
            cur[m.group(1)] = float(m.group(2))
    os.remove(tmp)
    return frames


def classify(f):
    if f["YMAX"] > 200:
        return "W"
    if f["VAVG"] > 140:
        return "R"
    if f["UAVG"] > 140:
        return "B"
    if f["UAVG"] < 120 and f["VAVG"] < 120:
        return "G"
    return "?"


def beep_starts(path):
    r = sh(["ffmpeg", "-v", "info", "-i", path, "-af", "silencedetect=n=-30dB:d=0.05",
            "-vn", "-f", "null", "-"])
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    dur = float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                    "default=nw=1:nk=1", path]).stdout)
    return [e for e in ends if e < dur - 0.05]  # 文件结尾处的 silence_end 不是哔声


rows = list(csv.reader(open(csv_path, encoding="utf-8-sig")))
header, rows = rows[0], rows[1:]
folder_names = [re.search(r"（(.*)）", h).group(1) for h in header[2:-1]]
combos = [tuple(r[2:-1]) for r in rows]
print(f"清单 {len(rows)} 条；分镜顺序 {folder_names}")
assert [FOLDER_COLOR[f] for f in folder_names] == EXPECT_ORDER, "文件夹顺序错误"
assert len(set(combos)) == len(combos), "有重复组合！"
print("✔ 文件夹顺序正确（自然排序），组合无重复")

worst_sync = worst_pos = 0.0
for r in rows:
    name = r[1]
    path = os.path.join(out_dir, name)
    srcs = r[-1].split(" | ")
    # 1) 解码错误（花屏/损坏）
    err = sh(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"]).stderr.strip()
    assert not err, f"{name} 解码报错：{err[:200]}"
    # 2) 逐帧颜色 → 片段顺序与边界
    frames = frame_stats(path)
    labels = [classify(f) for f in frames]
    assert "?" not in labels, f"{name} 有无法识别的帧（花屏?）"
    segs = []  # [颜色, 起始时间, 闪白时间]
    for f, lab in zip(frames, labels):
        if lab == "W":
            if segs and segs[-1][2] is None:
                segs[-1][2] = f["t"]
            continue
        if not segs or segs[-1][0] != lab:
            segs.append([lab, f["t"], None])
    assert [s[0] for s in segs] == EXPECT_ORDER, f"{name} 片段顺序错误：{[s[0] for s in segs]}"
    # 3) 片段时长 = 素材时长（按 30fps 取整）
    exp_starts, acc = [], 0.0
    for s in srcs:
        exp_starts.append(acc)
        acc += round(src_duration(s) * FPS) / FPS
    for (lab, st, flash), es in zip(segs, exp_starts):
        assert flash is not None, f"{name} 片段 {lab} 没检测到白闪"
        worst_pos = max(worst_pos, abs(flash - (es + 1.0)))
        assert abs(flash - (es + 1.0)) <= POS_TOL, f"{name} 片段 {lab} 位置偏移 {flash - es - 1:.3f}s"
    # 4) 音画同步：每个有音频的片段，哔声开始时间 ≈ 白闪时间
    beeps = beep_starts(path)
    need = [seg for seg, s in zip(segs, srcs)
            if (os.path.basename(os.path.dirname(s)), os.path.basename(s)) not in NO_AUDIO]
    assert len(beeps) == len(need), f"{name} 哔声数量 {len(beeps)} != {len(need)}"
    for seg, b in zip(need, beeps):
        d = b - seg[2]
        worst_sync = max(worst_sync, abs(d))
        assert abs(d) <= 0.045, f"{name} 音画不同步 {d * 1000:.0f}ms"
    # 5) 总时长：音频与视频
    du = sh(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
             "-of", "csv=p=0", path]).stdout.split()
    d = {x.split(",")[0]: float(x.split(",")[1]) for x in du}
    print(f"  {name}: 视频 {d['video']:.3f}s 音频 {d['audio']:.3f}s 片段 {''.join(s[0] for s in segs)} 哔声 {len(beeps)} ✔")

print(f"✔ 全部 {len(rows)} 条：无解码错误，顺序正确，"
      f"片段位置最大偏差 {worst_pos * 1000:.0f}ms，音画最大偏差 {worst_sync * 1000:.0f}ms")
