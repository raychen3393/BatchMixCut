"""命令行版本（不需要界面，也方便批处理/自测）

示例：
  python cli.py --parent "D:\\素材\\项目A" --out "D:\\成品" --count 800
  python cli.py --folders "D:\\A" "D:\\B" "D:\\C" --out "D:\\成品" --count 100 --encoder h264_amf
"""
import argparse
import sys

from mixcut.engine import Callbacks, JobConfig, MixEngine
from mixcut.utils import list_subfolders


def main(argv=None):
    ap = argparse.ArgumentParser(description="批量混剪（命令行）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--folders", nargs="+", help="分镜文件夹，按顺序")
    g.add_argument("--parent", help="父文件夹：自动读取所有子文件夹并自然排序")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--prefix", default="混剪_")
    ap.add_argument("--resolution", default="first", help="first 或 1080x1920")
    ap.add_argument("--fps", default="30", help="first 或 24/25/30/50/60")
    ap.add_argument("--encoder", default="auto",
                    choices=["auto", "libx264", "h264_nvenc", "h264_amf", "h264_qsv"])
    ap.add_argument("--quality", type=int, default=20)
    ap.add_argument("--speed", default="normal", choices=["normal", "fast"], help="转码速度")
    ap.add_argument("--prep-workers", type=int, default=2)
    ap.add_argument("--workers", type=int, default=4, help="拼接并行数")
    ap.add_argument("--auto", action="store_true", help="并行数按 CPU 和硬盘类型自动设置")
    ap.add_argument("--force-direct", action="store_true", help="强制直接拼接：规格不一致也不转换")
    ap.add_argument("--no-auto-clean", action="store_true", help="换公式时不自动清理缓存")
    ap.add_argument("--no-direct", action="store_true", help="即使素材规格一致也先统一转码")
    ap.add_argument("--slow", action="store_true", help="慢速模式（转场，需重新编码）")
    ap.add_argument("--transition", default="fade")
    ap.add_argument("--no-history", action="store_true", help="不做跨批次去重")
    ap.add_argument("--on-exceed", default="remaining", choices=["remaining", "repeat", "cancel"],
                    help="数量超过剩余组合时的处理")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args(argv)

    folders = a.folders or list_subfolders(a.parent)
    cfg = JobConfig(folders=folders, output_dir=a.out, prefix=a.prefix, count=a.count,
                    resolution=a.resolution, fps=a.fps, encoder=a.encoder, quality=a.quality, speed=a.speed, direct=not a.no_direct, force_direct=a.force_direct, auto_clean=not a.no_auto_clean, auto_workers=a.auto,
                    prep_workers=a.prep_workers, concat_workers=a.workers,
                    mode="slow" if a.slow else "fast", transition=a.transition,
                    use_history=not a.no_history, seed=a.seed)

    def ask(req, rem):
        print(f"[提示] 要求 {req} 条，但剩余可用组合只有 {rem} 个 → {a.on_exceed}")
        return a.on_exceed

    cb = Callbacks(log=lambda m: print(m, flush=True), ask_exceed=ask)
    summary = MixEngine(cfg, cb).run()
    print(f"完成 {summary['done']} 条，失败 {summary['failed']} 条")
    return 1 if summary.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
