"""界面主题：Fusion 风格 + 一套浅色配色；支持随窗口大小整体缩放"""
import re

BASE_FONT_PT = 9.0
BASE_W, BASE_H = 1180, 800      # 缩放比例 1.0 对应的窗口大小
MIN_SCALE, MAX_SCALE = 0.75, 1.6

ACCENT = "#2563eb"      # 主色（蓝）
SUCCESS = "#16a34a"     # 绿
WARN = "#d97706"        # 橙
DANGER = "#dc2626"      # 红

QSS = """
QMainWindow, QDialog { background: #f3f5f9; }
QWidget { color: #1f2937; }
QGroupBox {
    background: #ffffff; border: 1px solid #e2e6ee; border-radius: 10px;
    margin-top: 16px; padding: 12px 10px 8px 10px;
}
QGroupBox::title {
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 12px; top: 2px; padding: 0 4px; color: #2563eb;
}
QPushButton {
    background: #ffffff; border: 1px solid #cfd6e4; border-radius: 6px;
    padding: 5px 12px; font-weight: normal;
}
QPushButton:hover { background: #eef3ff; border-color: #2563eb; }
QPushButton:pressed { background: #dbe6ff; }
QPushButton:disabled { color: #a3acb9; background: #f1f3f7; border-color: #e2e6ee; }
QPushButton#primary {
    background: #2563eb; color: #ffffff; border: none; font-weight: bold; padding: 8px 26px;
}
QPushButton#primary:hover { background: #1d4fd8; }
QPushButton#primary:disabled { background: #a9c0f5; color: #f1f5ff; }
QPushButton#danger { color: #dc2626; border-color: #f0b4b4; padding: 8px 18px; }
QPushButton#danger:hover { background: #fdecec; border-color: #dc2626; }
QPushButton#danger:disabled { color: #a3acb9; border-color: #e2e6ee; }
QPushButton#game { color: #7c3aed; border-color: #d6c7fb; padding: 8px 14px; }
QPushButton#game:hover { background: #f3edff; border-color: #7c3aed; }
QLineEdit, QPlainTextEdit {
    background: #ffffff; border: 1px solid #cfd6e4; border-radius: 6px; padding: 4px 6px;
    selection-background-color: #2563eb; selection-color: #ffffff; font-weight: normal;
}
QLineEdit:focus { border-color: #2563eb; }
QLineEdit:disabled { background: #f1f3f7; color: #8a94a3; }
QPlainTextEdit#log {
    background: #0f172a; color: #d6e2f5; border: 1px solid #0f172a; border-radius: 8px;
    font-family: Consolas, "Microsoft YaHei UI", monospace;
}
QTableWidget {
    background: #ffffff; border: 1px solid #e2e6ee; border-radius: 6px;
    gridline-color: #eef1f6; font-weight: normal;
    selection-background-color: #dbe6ff; selection-color: #1f2937;
}
QHeaderView::section {
    background: #f3f5f9; border: none; border-bottom: 1px solid #e2e6ee;
    padding: 6px; font-weight: bold; color: #475569;
}
QProgressBar {
    background: #e6eaf2; border: none; border-radius: 8px; min-height: 18px;
    text-align: center; color: #1f2937; font-weight: normal;
}
QProgressBar::chunk { background: #2563eb; border-radius: 8px; }
QProgressBar#gen::chunk { background: #16a34a; }
QScrollArea { border: none; background: #f3f5f9; }
QWidget#rightPanel { background: #f3f5f9; }
QFrame#header {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2563eb, stop:1 #7c3aed);
    border-radius: 10px;
}
QLabel#title { color: #ffffff; font-size: 17px; font-weight: bold; background: transparent; }
QLabel#subtitle { color: #dbe6ff; background: transparent; }
QTableWidget { alternate-background-color: #f7f9fc; }
QTableWidget::item { padding: 2px 6px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #c5cedd; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #9aa7bd; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #c5cedd; border-radius: 4px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QSplitter::handle { background: transparent; }
QLabel#hint { color: #8a94a3; font-weight: normal; }
QLabel#stats { color: #334155; font-weight: bold; }
QToolTip { background: #1f2937; color: #ffffff; border: none; padding: 4px 6px; }
"""


def apply_theme(app):
    """应用主题；任何一步失败都不影响程序启动"""
    try:
        app.setStyle("Fusion")
    except Exception:  # noqa
        pass
    try:
        from PySide6.QtGui import QColor, QPalette
        pal = QPalette()
        pal.setColor(QPalette.Window, QColor("#f3f5f9"))
        pal.setColor(QPalette.WindowText, QColor("#1f2937"))
        pal.setColor(QPalette.Base, QColor("#ffffff"))
        pal.setColor(QPalette.AlternateBase, QColor("#f7f9fc"))
        pal.setColor(QPalette.Text, QColor("#1f2937"))
        pal.setColor(QPalette.Button, QColor("#ffffff"))
        pal.setColor(QPalette.ButtonText, QColor("#1f2937"))
        pal.setColor(QPalette.Highlight, QColor(ACCENT))
        pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
        pal.setColor(QPalette.ToolTipBase, QColor("#1f2937"))
        pal.setColor(QPalette.ToolTipText, QColor("#ffffff"))
        app.setPalette(pal)
    except Exception:  # noqa
        pass
    try:
        app.setStyleSheet(QSS)
    except Exception:  # noqa
        pass


def scaled_qss(f: float) -> str:
    """把样式表里所有像素尺寸（边距、圆角、字号等）按比例缩放，并统一设置字号"""
    def px(m):
        return f"{max(1, int(round(int(m.group(1)) * f)))}px"
    body = re.sub(r"(\d+)px", px, QSS)
    return f"QWidget {{ font-size: {BASE_FONT_PT * f:.1f}pt; }}\n" + body


def scale_for(width: int, height: int) -> float:
    f = min(width / BASE_W, height / BASE_H)
    f = max(MIN_SCALE, min(MAX_SCALE, f))
    return round(f * 20) / 20          # 以 0.05 为一档，避免拖动时频繁重排


def apply_scale(app, f: float):
    try:
        from PySide6.QtGui import QFont
        font = QFont(app.font())
        font.setPointSizeF(BASE_FONT_PT * f)
        app.setFont(font)
    except Exception:  # noqa
        pass
    try:
        app.setStyleSheet(scaled_qss(f))
    except Exception:  # noqa
        pass
