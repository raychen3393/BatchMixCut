"""批量混剪 - 程序入口（双击桌面快捷方式 / python main.py）"""
import multiprocessing
import os
import sys
import traceback


def _error_log_path():
    try:
        from mixcut.utils import data_root
        return os.path.join(data_root(), "error.log")
    except Exception:  # noqa
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log")


def _report(exc_type, exc, tb):
    """无黑窗口运行时，异常不会显示在任何地方：写入 error.log 并弹窗提示"""
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    path = _error_log_path()
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass
    msg = f"程序出错了：\n\n{exc}\n\n详细信息已保存到：\n{path}\n\n可以把这个文件发给开发者。"
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance():
            QMessageBox.critical(None, "批量混剪 - 出错", msg)
            return
    except Exception:  # noqa
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "批量混剪 - 出错", 0x10)
            return
        except Exception:  # noqa
            pass
    print(text, file=sys.__stderr__ or sys.stdout)


def main():
    sys.excepthook = _report
    try:
        from mixcut.gui import run_app
    except ImportError as e:
        _report(type(e), ImportError(f"缺少依赖：{e}\n请重新双击“双击运行.bat”完成安装。"), None)
        sys.exit(1)
    run_app()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
