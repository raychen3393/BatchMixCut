"""生成自测素材：3 个分镜文件夹 × 3 条不同分辨率/帧率/格式的视频 + 干扰文件。
每个文件夹一种底色（红/绿/蓝），每条在 t=1.0s 有 0.1s 白闪，有音轨的同时有 0.1s 的 1kHz 哔声。"""
import os
import subprocess
import sys

root = sys.argv[1]
FOLDERS = [("新建文件夹 (10)", "blue"), ("新建文件夹", "red"), ("新建文件夹 (2)", "0x008000")]
# (文件名, 宽, 高, fps, 视频时长, 音频: None 或 (时长, 采样率, 声道))
CLIPS = [
    ("素材 1.mp4", 1920, 1080, 30, 2.5, (2.5, 44100, 2)),
    ("it's 竖屏.mov", 1080, 1920, 25, 3.0, (2.0, 48000, 1)),   # 音频比视频短
    ("第三条", 1280, 720, 24, 2.2, (3.5, 22050, 2)),            # 音频比视频长
]
EXTS = {"新建文件夹": ".mkv", "新建文件夹 (2)": ".avi", "新建文件夹 (10)": ".flv"}


def run(args):
    r = subprocess.run(args, capture_output=True)
    if r.returncode:
        print(r.stderr.decode()[-800:])
        raise SystemExit(1)


for folder, color in FOLDERS:
    d = os.path.join(root, folder)
    os.makedirs(d, exist_ok=True)
    for name, w, h, fps, vd, aud in CLIPS:
        if name == "第三条":
            name = name + EXTS[folder]
            if folder == "新建文件夹":
                aud = None  # 这一条没有音轨
        out = os.path.join(d, name)
        v = (f"color=c={color}:s={w}x{h}:r={fps}:d={vd},"
             f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t,1,1.1)'")
        args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", v]
        if aud:
            ad, sr, ch = aud
            expr = "if(between(t,1,1.1),0.8*sin(2*PI*1000*t),0)"
            expr = expr if ch == 1 else f"{expr}|{expr}"
            args += ["-f", "lavfi", "-i", f"aevalsrc='{expr}':s={sr}:d={ad}"]
        ext = os.path.splitext(name)[1]
        if ext == ".avi":
            args += ["-c:v", "mjpeg", "-q:v", "3", "-pix_fmt", "yuvj420p"] + (["-c:a", "pcm_s16le"] if aud else [])
        elif ext == ".flv":
            args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"] + (["-c:a", "aac", "-ar", "44100"] if aud else [])
        else:
            args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"] + (["-c:a", "aac"] if aud else [])
        run(args + [out])

# 干扰：损坏文件 + 非视频文件
with open(os.path.join(root, "新建文件夹 (2)", "坏文件.mp4"), "wb") as f:
    f.write(os.urandom(50000))
with open(os.path.join(root, "新建文件夹", "说明.txt"), "w") as f:
    f.write("不是视频")
print("ok")
