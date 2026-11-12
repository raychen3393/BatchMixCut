"""PySide6 图形界面"""
import json
import os
import shutil
import threading

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox,
                               QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QRadioButton, QScrollArea, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from . import __version__
from .combo import total_combos
from .concat import TRANSITIONS
from .engine import Callbacks, JobConfig, MixEngine
from .ffmpeg_tools import ENCODER_LABELS, check_ffmpeg
from .history import History
from .preprocess import cache_root
from .theme import DANGER, SUCCESS, WARN, apply_scale, apply_theme, scale_for
from .utils import data_root, dir_size, human_size, list_subfolders, list_videos

class _NoWheelMixin:
    """鼠标滚轮不改变数值/选项（避免路过时误改设置）；滚轮事件交给外层，用来滚动页面"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.setFocusPolicy(Qt.StrongFocus)
        except Exception:  # noqa
            pass

    def wheelEvent(self, e):
        e.ignore()


class _SpinBox(_NoWheelMixin, QSpinBox):
    pass


class _DoubleSpinBox(_NoWheelMixin, QDoubleSpinBox):
    pass


class _ComboBox(_NoWheelMixin, QComboBox):
    pass


RESOLUTIONS = [("以第一条素材为准", "first"), ("1080×1920 竖屏", "1080x1920"),
               ("720×1280 竖屏", "720x1280"), ("1920×1080 横屏", "1920x1080"),
               ("1280×720 横屏", "1280x720"), ("1080×1080 方形", "1080x1080")]
FPS_CHOICES = [("30", "30"), ("25", "25"), ("24", "24"), ("50", "50"), ("60", "60"),
               ("以第一条素材为准", "first")]
TRANSITION_CHOICES = [("随机", "random")] + [(t, t) for t in TRANSITIONS]


class Worker(QObject):
    log = Signal(str)
    progress = Signal(str, int, int)
    stats = Signal(dict)
    ask = Signal(int, int)
    finished = Signal(dict)

    def __init__(self, cfg: JobConfig):
        super().__init__()
        self.cfg = cfg
        self._answer = "remaining"
        self._event = threading.Event()
        self.engine = MixEngine(cfg, Callbacks(
            log=self.log.emit,
            progress=self.progress.emit,
            stats=self.stats.emit,
            ask_exceed=self._ask_exceed,
        ))

    def _ask_exceed(self, requested, remaining):
        # 在后台线程里调用：发信号让界面弹窗，然后等用户选择
        self._answer = "cancel"
        self._event.clear()
        self.ask.emit(requested, remaining)
        while not self._event.wait(0.2):
            if self.engine.stop_event.is_set():
                return "cancel"
        if self.engine.stop_event.is_set():
            return "cancel"
        return self._answer

    def answer(self, value):
        self._answer = value
        self._event.set()

    @Slot()
    def run(self):
        summary = self.engine.run()
        self.finished.emit(summary)

    def stop(self):
        self.engine.stop()
        self._event.set()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._base_title = f"批量混剪 v{__version__}"
        self.setWindowTitle(self._base_title)
        self._stage_text = {}
        self._eta = ""
        self._game = None
        self._scale = 1.0
        self._scale_timer = QTimer(self)      # 停止拖动 0.12 秒后再缩放，拖动过程不卡
        self._scale_timer.setSingleShot(True)
        self._scale_timer.setInterval(120)
        self._scale_timer.timeout.connect(self._apply_scale)
        self.setMinimumSize(720, 460)
        w, h = 1180, 800
        try:   # 初始大小不超过屏幕可用区域（小屏幕 / 系统缩放 125%~150% 时很重要）
            from PySide6.QtGui import QGuiApplication
            g = QGuiApplication.primaryScreen().availableGeometry()
            w, h = min(w, int(g.width() * 0.92)), min(h, int(g.height() * 0.9))
        except Exception:  # noqa
            pass
        self.resize(w, h)
        self.setAcceptDrops(True)
        self.folders = []
        self._thread = None
        self.worker = None
        self._build_ui()
        self._load_last_config()
        self._refresh_folders()
        self._refresh_cache_size()
        ok, msg = check_ffmpeg()
        self._log(f"FFmpeg：{msg}" if ok else f"❌ {msg}")

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # 顶部标题栏
        header = QFrame()
        header.setObjectName("header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 8, 16, 8)
        t1 = QLabel("批量混剪")
        t1.setObjectName("title")
        t2 = QLabel("分镜顺序固定 · 组合不重复 · 极速拼接")
        t2.setObjectName("subtitle")
        t3 = QLabel(f"v{__version__}")
        t3.setObjectName("subtitle")
        hl.addWidget(t1)
        hl.addSpacing(12)
        hl.addWidget(t2)
        hl.addStretch(1)
        hl.addWidget(t3)
        root.addWidget(header)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # 左：分镜文件夹列表
        left = QGroupBox("分镜文件夹（严格按此顺序拼接：第 1 行 → 第 2 行 → …）")
        lv = QVBoxLayout(left)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["序号", "路径", "视频数量"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        lv.addWidget(self.table, 1)
        tip = QLabel("提示：可以把文件夹直接拖进窗口")
        tip.setObjectName("hint")
        lv.addWidget(tip)
        row1, row2 = QHBoxLayout(), QHBoxLayout()
        self.btn_add = QPushButton("＋ 添加文件夹")
        self.btn_parent = QPushButton("📂 从父文件夹导入子文件夹")
        self.btn_up = QPushButton("↑ 上移")
        self.btn_down = QPushButton("↓ 下移")
        self.btn_del = QPushButton("删除")
        self.btn_clear = QPushButton("清空")
        for b in (self.btn_add, self.btn_parent):
            row1.addWidget(b)
        for b in (self.btn_up, self.btn_down, self.btn_del, self.btn_clear):
            row2.addWidget(b)
        lv.addLayout(row1)
        lv.addLayout(row2)
        splitter.addWidget(left)

        # 右：参数
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)

        g1 = QGroupBox("生成设置")
        f1 = QFormLayout(g1)
        self.spin_count = _SpinBox()
        self.spin_count.setRange(1, 10_000_000)
        self.spin_count.setValue(10)
        self.spin_count.setGroupSeparatorShown(True)
        f1.addRow("生成数量：", self.spin_count)
        self.lbl_combo = QLabel("-")
        self.lbl_combo.setWordWrap(True)
        self.lbl_combo.setStyleSheet(f"color:{SUCCESS};font-weight:bold")
        f1.addRow("组合数：", self.lbl_combo)
        hist_row = QHBoxLayout()
        self.chk_history = QCheckBox("跨批次去重（避开以前生成过的组合）")
        self.chk_history.setChecked(True)
        self.btn_clear_hist = QPushButton("清空历史")
        hist_row.addWidget(self.chk_history)
        hist_row.addWidget(self.btn_clear_hist)
        f1.addRow(hist_row)
        self.edit_prefix = QLineEdit("混剪_")
        f1.addRow("文件名前缀：", self.edit_prefix)
        rv.addWidget(g1)

        g2 = QGroupBox("统一规格（预处理）")
        f2 = QFormLayout(g2)
        self.cmb_res = _ComboBox()
        for label, val in RESOLUTIONS:
            self.cmb_res.addItem(label, val)
        f2.addRow("分辨率：", self.cmb_res)
        self.cmb_fps = _ComboBox()
        for label, val in FPS_CHOICES:
            self.cmb_fps.addItem(label, val)
        f2.addRow("帧率：", self.cmb_fps)
        self.cmb_enc = _ComboBox()
        for key, label in ENCODER_LABELS.items():
            self.cmb_enc.addItem(label, key)
        f2.addRow("编码器：", self.cmb_enc)
        self.spin_quality = _SpinBox()
        self.spin_quality.setRange(14, 32)
        self.spin_quality.setValue(20)
        self.spin_quality.setToolTip("数值越小越清晰、文件越大。18~23 比较常用")
        f2.addRow("画质（越小越清晰）：", self.spin_quality)
        self.cmb_speed = _ComboBox()
        self.cmb_speed.addItem("标准", "normal")
        self.cmb_speed.addItem("更快（CPU 转码约快一倍，但文件约大一倍）", "fast")
        self.cmb_speed.setToolTip("只影响预处理阶段。切换后需要重新预处理一次")
        f2.addRow("转码速度：", self.cmb_speed)
        self.spin_prep = _SpinBox()
        self.spin_prep.setRange(1, 8)
        self.spin_prep.setValue(3)
        f2.addRow("预处理并行数：", self.spin_prep)
        rv.addWidget(g2)

        g3 = QGroupBox("拼接")
        f3 = QFormLayout(g3)
        self.chk_direct = QCheckBox("素材规格一致时直接拼接（最快：不预处理、不转码）")
        self.chk_direct.setChecked(True)
        self.chk_direct.setToolTip("素材的分辨率、帧率、编码都相同时，直接复制拼接原文件。\n"
                                   "规格不一致时会自动改为先统一转码（上面的“统一规格”设置才会生效）。")
        f3.addRow(self.chk_direct)
        self.chk_force = QCheckBox("强制直接拼接（规格不一致也不转换，素材规格自己把关）")
        self.chk_force.setStyleSheet(f"color:{DANGER}")
        self.chk_force.setToolTip("打开后完全不转换素材，所有素材都直接拼接，速度最快。\n"
                                  "规格和多数不一样的素材会在日志里列出来；用到它们的成品可能花屏、卡顿或音画不同步。")
        f3.addRow(self.chk_force)
        self.chk_auto = QCheckBox("并行数自动设置（按 CPU 核心数和硬盘类型，推荐）")
        self.chk_auto.setChecked(True)
        self.chk_auto.setToolTip("勾选后，“预处理并行数”和“拼接并行数”在开始时自动决定，日志里会写明用了多少。\n"
                                 "想自己指定就取消勾选。")
        f3.addRow(self.chk_auto)
        self.chk_lowprio = QCheckBox("后台低优先级运行（处理时电脑不卡，可以照常做别的事）")
        self.chk_lowprio.setChecked(True)
        f3.addRow(self.chk_lowprio)
        self.spin_workers = _SpinBox()
        self.spin_workers.setRange(1, 16)
        self.spin_workers.setValue(4)
        self.spin_workers.setToolTip("同时拼接几条。机械硬盘建议 2~4，固态硬盘可以 4~8")
        f3.addRow("拼接并行数：", self.spin_workers)
        mode_row = QHBoxLayout()
        self.rb_fast = QRadioButton("极速模式（推荐）")
        self.rb_slow = QRadioButton("慢速模式（加转场）")
        self.rb_fast.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_fast)
        grp.addButton(self.rb_slow)
        mode_row.addWidget(self.rb_fast)
        mode_row.addWidget(self.rb_slow)
        f3.addRow("模式：", mode_row)
        self.lbl_slow_warn = QLabel("⚠ 慢速模式开启后速度会明显变慢（每条都要重新编码）")
        self.lbl_slow_warn.setStyleSheet(f"color:{DANGER}")
        f3.addRow(self.lbl_slow_warn)
        self.cmb_trans = _ComboBox()
        for label, val in TRANSITION_CHOICES:
            self.cmb_trans.addItem(label, val)
        f3.addRow("转场效果：", self.cmb_trans)
        self.spin_tdur = _DoubleSpinBox()
        self.spin_tdur.setRange(0.2, 2.0)
        self.spin_tdur.setSingleStep(0.1)
        self.spin_tdur.setValue(0.5)
        self.spin_tdur.setSuffix(" 秒")
        f3.addRow("转场时长：", self.spin_tdur)
        rv.addWidget(g3)

        g4 = QGroupBox("缓存与配置")
        f4 = QVBoxLayout(g4)
        cr = QHBoxLayout()
        self.lbl_cache = QLabel("缓存占用：-")
        self.btn_cache_refresh = QPushButton("刷新")
        self.btn_cache_clean = QPushButton("清理缓存")
        cr.addWidget(self.lbl_cache, 1)
        cr.addWidget(self.btn_cache_refresh)
        cr.addWidget(self.btn_cache_clean)
        f4.addLayout(cr)
        self.chk_autoclean = QCheckBox("换公式（换了分镜文件夹）时自动清理缓存")
        self.chk_autoclean.setChecked(True)
        self.chk_autoclean.setToolTip("分镜文件夹和上一次不一样时，开始前自动清空缓存。只调整顺序不算换公式。")
        f4.addWidget(self.chk_autoclean)
        tr = QHBoxLayout()
        self.btn_save_cfg = QPushButton("保存配置模板")
        self.btn_load_cfg = QPushButton("读取配置模板")
        tr.addWidget(self.btn_save_cfg)
        tr.addWidget(self.btn_load_cfg)
        self.btn_reset = QPushButton("恢复默认设置")
        self.btn_reset.setToolTip("把右侧所有参数恢复成默认值。分镜文件夹、输出路径、生成历史和缓存都不受影响")
        tr.addWidget(self.btn_reset)
        f4.addLayout(tr)
        rv.addWidget(g4)
        rv.addStretch(1)
        # 右侧设置放进滚动区：窗口缩小时出现滚动条，而不是把下面的按钮挤出屏幕
        right.setObjectName("rightPanel")
        scroll = QScrollArea()
        scroll.setObjectName("rightScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(right)
        scroll.setMinimumWidth(360)
        left.setMinimumWidth(300)
        splitter.addWidget(scroll)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([640, 520])

        # 底部
        bottom = QGroupBox("输出")
        bv = QVBoxLayout(bottom)
        orow = QHBoxLayout()
        orow.addWidget(QLabel("输出路径："))
        self.edit_out = QLineEdit()
        self.btn_out = QPushButton("浏览…")
        self.btn_open_out = QPushButton("打开")
        orow.addWidget(self.edit_out, 1)
        orow.addWidget(self.btn_out)
        orow.addWidget(self.btn_open_out)
        self.btn_start = QPushButton("▶ 开始处理")
        self.btn_stop = QPushButton("■ 停止处理")
        self.btn_start.setObjectName("primary")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.setEnabled(False)
        self.btn_game = QPushButton("🎮 玩会儿游戏")
        self.btn_game.setObjectName("game")
        self.btn_game.setToolTip("等待时玩一局贪吃蛇，或和同事局域网联机对战，不影响混剪")
        orow.addWidget(self.btn_game)
        orow.addWidget(self.btn_start)
        orow.addWidget(self.btn_stop)
        bv.addLayout(orow)
        prow = QHBoxLayout()
        prow.addWidget(QLabel("预处理："))
        self.pb_prep = QProgressBar()
        self.pb_prep.setFormat("%v / %m")
        prow.addWidget(self.pb_prep, 1)
        prow.addWidget(QLabel("生成："))
        self.pb_gen = QProgressBar()
        self.pb_gen.setFormat("%v / %m")
        self.pb_gen.setObjectName("gen")
        prow.addWidget(self.pb_gen, 1)
        self.lbl_stats = QLabel("平均每条：-")
        self.lbl_stats.setMinimumWidth(260)
        self.lbl_stats.setObjectName("stats")
        prow.addWidget(self.lbl_stats)
        bv.addLayout(prow)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("log")
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setMinimumHeight(70)
        bv.addWidget(self.log_view)
        root.addWidget(bottom)

        # 信号
        self.btn_add.clicked.connect(self._add_folder)
        self.btn_parent.clicked.connect(self._add_parent)
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down.clicked.connect(lambda: self._move(1))
        self.btn_del.clicked.connect(self._delete)
        self.btn_clear.clicked.connect(self._clear_folders)
        self.btn_clear_hist.clicked.connect(self._clear_history)
        self.chk_history.toggled.connect(lambda _v: self._refresh_combo())
        self.spin_count.valueChanged.connect(lambda _v: self._refresh_combo())
        self.btn_cache_refresh.clicked.connect(self._refresh_cache_size)
        self.btn_cache_clean.clicked.connect(self._clean_cache)
        self.btn_save_cfg.clicked.connect(self._save_cfg)
        self.btn_load_cfg.clicked.connect(self._load_cfg)
        self.btn_out.clicked.connect(self._choose_out)
        self.btn_open_out.clicked.connect(self._open_out)
        self.btn_reset.clicked.connect(self._reset_defaults)
        self.btn_start.clicked.connect(self._start)
        self.btn_game.clicked.connect(self._open_game)
        self.btn_stop.clicked.connect(self._stop)
        self.rb_slow.toggled.connect(self._update_mode)
        self.chk_auto.toggled.connect(lambda _v: self._update_auto())
        self.chk_force.clicked.connect(self._force_clicked)
        self._update_auto()
        self._update_mode()

    # ------------------------------------------------------------ 随窗口等比缩放
    def resizeEvent(self, e):
        super().resizeEvent(e)
        try:
            self._scale_timer.start()
        except Exception:  # noqa
            pass

    def _apply_scale(self):
        try:
            f = scale_for(self.width(), self.height())
            if abs(f - self._scale) < 0.049:
                return
            self._scale = f
            apply_scale(QApplication.instance(), f)
            self.table.verticalHeader().setDefaultSectionSize(int(round(30 * f)))
        except Exception:  # noqa  缩放失败不影响使用
            pass

    # ------------------------------------------------------------ 文件夹
    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "选择分镜文件夹")
        if d:
            self._append_folders([d])

    def _add_parent(self):
        d = QFileDialog.getExistingDirectory(self, "选择父文件夹（自动读取所有子文件夹，按名称自然排序）")
        if not d:
            return
        subs = list_subfolders(d)
        if not subs:
            QMessageBox.information(self, "提示", "这个文件夹里没有子文件夹")
            return
        if self.folders:
            r = QMessageBox.question(self, "导入方式", "替换现有的分镜列表吗？\n选“否”则追加到末尾。",
                                     QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Yes:
                self.folders = []
        self._append_folders(subs)

    def _append_folders(self, paths):
        existing = {os.path.normcase(os.path.abspath(p)) for p in self.folders}
        for p in paths:
            key = os.path.normcase(os.path.abspath(p))
            if os.path.isdir(p) and key not in existing:
                self.folders.append(os.path.abspath(p))
                existing.add(key)
        self._refresh_folders()

    def _move(self, delta):
        r = self.table.currentRow()
        j = r + delta
        if r < 0 or not (0 <= j < len(self.folders)):
            return
        self.folders[r], self.folders[j] = self.folders[j], self.folders[r]
        self._refresh_folders(rescan=False)
        self.table.selectRow(j)

    def _delete(self):
        r = self.table.currentRow()
        if r >= 0:
            del self.folders[r]
            self._refresh_folders(rescan=False)
            if self.folders:
                self.table.selectRow(min(r, len(self.folders) - 1))

    def _clear_folders(self):
        if self.folders and QMessageBox.question(self, "确认", "清空分镜列表？") == QMessageBox.Yes:
            self.folders = []
            self._refresh_folders()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls()]
        dirs = [p for p in paths if os.path.isdir(p)]
        if dirs and not self._running():
            self._append_folders(sorted(dirs))

    def _refresh_folders(self, rescan=True):
        self.table.setRowCount(len(self.folders))
        self._materials = []
        if rescan or not hasattr(self, "_listing"):
            self._listing = {}
        for i, f in enumerate(self.folders):
            if f not in self._listing:
                self._listing[f] = list_videos(f)
            vids = self._listing[f]
            self._materials.append(vids)
            items = [QTableWidgetItem(str(i + 1)), QTableWidgetItem(f), QTableWidgetItem(str(len(vids)))]
            items[0].setTextAlignment(Qt.AlignCenter)
            items[2].setTextAlignment(Qt.AlignCenter)
            if not vids:
                items[2].setForeground(QBrush(QColor("#d33")))
            for c, it in enumerate(items):
                self.table.setItem(i, c, it)
        self._refresh_combo()

    def _refresh_combo(self):
        counts = [len(m) for m in getattr(self, "_materials", [])]
        total = total_combos(counts)
        if not counts:
            self.lbl_combo.setText("请先添加分镜文件夹")
            return
        if total == 0:
            self.lbl_combo.setText("有文件夹是空的，无法组合")
            self.lbl_combo.setStyleSheet(f"color:{DANGER};font-weight:bold")
            return
        text = f"最多 {total:,} 种（{' × '.join(map(str, counts))}）"
        remaining = total
        if self.chk_history.isChecked():
            try:
                used, _u = History(self.folders).load_codes(self._materials)
                remaining = total - len(used)
                text += f"\n历史已用 {len(used):,}，剩余可用 {remaining:,}"
            except Exception as e:  # noqa
                text += f"\n（读取历史失败：{e}）"
        ok = self.spin_count.value() <= remaining
        if not ok:
            text += "\n⚠ 生成数量超过剩余可用组合，开始时会让你选择"
        self.lbl_combo.setText(text)
        self.lbl_combo.setStyleSheet(f"color:{SUCCESS if ok else WARN};font-weight:bold")

    def _clear_history(self):
        if not self.folders:
            return
        if QMessageBox.question(self, "确认", "清空当前这组文件夹的生成历史？\n清空后，以前生成过的组合可能会再次出现。") \
                == QMessageBox.Yes:
            History(self.folders).clear()
            self._refresh_combo()
            self._log("已清空历史记录")

    # ------------------------------------------------------------ 缓存
    def _refresh_cache_size(self):
        self.lbl_cache.setText(f"缓存占用：{human_size(dir_size(cache_root()))}   位置：{cache_root()}")

    def _clean_cache(self):
        if self._running():
            return
        if QMessageBox.question(self, "确认", "删除所有预处理缓存？下次生成时会重新转码。") == QMessageBox.Yes:
            shutil.rmtree(cache_root(), ignore_errors=True)
            self._refresh_cache_size()
            self._log("缓存已清理")

    # ------------------------------------------------------------ 配置
    def _collect(self) -> JobConfig:
        return JobConfig(
            folders=list(self.folders), output_dir=self.edit_out.text().strip(),
            prefix=self.edit_prefix.text() or "混剪_", count=self.spin_count.value(),
            resolution=self.cmb_res.currentData(), fps=self.cmb_fps.currentData(),
            encoder=self.cmb_enc.currentData(), quality=self.spin_quality.value(),
            speed=self.cmb_speed.currentData(), direct=self.chk_direct.isChecked(), low_priority=self.chk_lowprio.isChecked(), auto_workers=self.chk_auto.isChecked(), force_direct=self.chk_force.isChecked(),
            auto_clean=self.chk_autoclean.isChecked(),
            prep_workers=self.spin_prep.value(), concat_workers=self.spin_workers.value(),
            mode="slow" if self.rb_slow.isChecked() else "fast",
            transition=self.cmb_trans.currentData(), trans_dur=self.spin_tdur.value(),
            use_history=self.chk_history.isChecked())

    def _apply(self, cfg: JobConfig):
        self.folders = [f for f in cfg.folders if f]
        self.edit_out.setText(cfg.output_dir)
        self.edit_prefix.setText(cfg.prefix)
        self.spin_count.setValue(int(cfg.count))
        for cmb, val in ((self.cmb_res, cfg.resolution), (self.cmb_fps, cfg.fps),
                         (self.cmb_enc, cfg.encoder), (self.cmb_trans, cfg.transition),
                         (self.cmb_speed, cfg.speed)):
            i = cmb.findData(val)
            if i >= 0:
                cmb.setCurrentIndex(i)
        self.spin_quality.setValue(int(cfg.quality))
        self.spin_prep.setValue(int(cfg.prep_workers))
        self.spin_workers.setValue(int(cfg.concat_workers))
        (self.rb_slow if cfg.mode == "slow" else self.rb_fast).setChecked(True)
        self.spin_tdur.setValue(float(cfg.trans_dur))
        self.chk_history.setChecked(bool(cfg.use_history))
        self.chk_direct.setChecked(bool(cfg.direct))
        self.chk_lowprio.setChecked(bool(cfg.low_priority))
        self.chk_auto.setChecked(bool(cfg.auto_workers))
        self.chk_force.setChecked(bool(cfg.force_direct))
        self.chk_autoclean.setChecked(bool(cfg.auto_clean))
        self._update_auto()
        self._refresh_folders()

    def _reset_defaults(self):
        if self._running():
            return
        if QMessageBox.question(self, "恢复默认设置",
                                "把右侧所有参数恢复成默认值吗？\n"
                                "分镜文件夹、输出路径、生成历史和缓存都不会变。") != QMessageBox.Yes:
            return
        d = JobConfig()
        d.folders = list(self.folders)
        d.output_dir = self.edit_out.text().strip()
        self._apply(d)
        self._log("已恢复默认设置")

    def _templates_dir(self):
        d = os.path.join(data_root(), "templates")
        os.makedirs(d, exist_ok=True)
        return d

    def _save_cfg(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存配置模板", self._templates_dir(), "配置 (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._collect().to_dict(), f, ensure_ascii=False, indent=2)
            self._log(f"配置已保存：{path}")

    def _load_cfg(self):
        path, _ = QFileDialog.getOpenFileName(self, "读取配置模板", self._templates_dir(), "配置 (*.json)")
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._apply(JobConfig.from_dict(json.load(f)))
                self._log(f"已读取配置：{path}")
            except Exception as e:  # noqa
                QMessageBox.warning(self, "读取失败", str(e))

    def _last_cfg_path(self):
        return os.path.join(data_root(), "last_config.json")

    def _load_last_config(self):
        try:
            with open(self._last_cfg_path(), "r", encoding="utf-8") as f:
                self._apply(JobConfig.from_dict(json.load(f)))
        except Exception:  # noqa
            pass

    def closeEvent(self, e):
        if self._running():
            if QMessageBox.question(self, "确认", "正在处理，确定要停止并退出吗？") != QMessageBox.Yes:
                e.ignore()
                return
            self._stop()
            if self._thread:
                self._thread.wait(5000)
        try:
            with open(self._last_cfg_path(), "w", encoding="utf-8") as f:
                json.dump(self._collect().to_dict(), f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        e.accept()

    # ------------------------------------------------------------ 输出
    def _choose_out(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出文件夹", self.edit_out.text())
        if d:
            self.edit_out.setText(d)

    def _open_out(self):
        d = self.edit_out.text().strip()
        if d and os.path.isdir(d) and hasattr(os, "startfile"):
            os.startfile(d)  # noqa  (Windows)

    def _force_clicked(self, on):
        # 只在用户亲手勾选时提醒一次（读取配置时不弹窗）
        if on:
            QMessageBox.warning(self, "强制直接拼接",
                                "打开后程序不再转换任何素材。\n\n"
                                "规格（分辨率、帧率、编码、音频）和多数不一样的素材会照样拼进去，"
                                "用到它们的成品可能花屏、卡顿或音画不同步。\n\n"
                                "日志里会列出这些素材，混完请重点检查用到它们的成品。")

    def _update_auto(self):
        manual = not self.chk_auto.isChecked()
        self.spin_prep.setEnabled(manual)
        self.spin_workers.setEnabled(manual)

    def _update_mode(self):
        slow = self.rb_slow.isChecked()
        self.cmb_trans.setEnabled(slow)
        self.spin_tdur.setEnabled(slow)
        self.lbl_slow_warn.setVisible(slow)

    # ------------------------------------------------------------ 运行
    def _running(self):
        return self._thread is not None and self._thread.isRunning()

    def _set_editing(self, enabled):
        for w in (self.table, self.btn_add, self.btn_parent, self.btn_up, self.btn_down,
                  self.btn_del, self.btn_clear, self.btn_clear_hist, self.btn_cache_clean,
                  self.btn_load_cfg, self.btn_reset, self.btn_out, self.edit_out, self.spin_count,
                  self.edit_prefix, self.cmb_res, self.cmb_fps, self.cmb_enc, self.spin_quality, self.cmb_speed,
                  self.spin_prep, self.spin_workers, self.rb_fast, self.rb_slow,
                  self.chk_history, self.chk_direct, self.chk_lowprio, self.chk_auto, self.chk_force, self.chk_autoclean, self.cmb_trans, self.spin_tdur):
            w.setEnabled(enabled)
        if enabled:
            self._update_mode()
            self._update_auto()
        self.btn_start.setEnabled(enabled)
        self.btn_stop.setEnabled(not enabled)

    def _start(self):
        if self._running():
            return
        cfg = self._collect()
        if not cfg.folders:
            QMessageBox.warning(self, "提示", "请先添加分镜文件夹")
            return
        if not cfg.output_dir:
            QMessageBox.warning(self, "提示", "请先选择输出路径")
            return
        empty = [f for f, m in zip(self.folders, self._materials) if not m]
        if empty:
            QMessageBox.warning(self, "提示", "以下文件夹里没有视频：\n" + "\n".join(empty))
            return
        if cfg.mode == "slow":
            r = QMessageBox.question(self, "慢速模式", "慢速模式每条都要重新编码，速度会明显变慢。继续吗？")
            if r != QMessageBox.Yes:
                return
        self.pb_prep.setRange(0, 1)
        self.pb_prep.setValue(0)
        self.pb_gen.setRange(0, max(1, cfg.count))
        self.pb_gen.setValue(0)
        self.lbl_stats.setText("平均每条：-")
        self._stage_text = {}
        self._eta = ""
        self._log("=" * 40)
        self._log("顺序已锁定：" + " → ".join(os.path.basename(os.path.normpath(f)) for f in cfg.folders))
        self._set_editing(False)

        self._thread = QThread(self)
        self.worker = Worker(cfg)
        self.worker.moveToThread(self._thread)
        self._thread.started.connect(self.worker.run)
        self.worker.log.connect(self._log)
        self.worker.progress.connect(self._on_progress)
        self.worker.stats.connect(self._on_stats)
        self.worker.ask.connect(self._on_ask)
        self.worker.finished.connect(self._on_finished)
        self.worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self.worker.deleteLater)
        self._thread.start()

    def _stop(self):
        if self.worker:
            self._log("正在停止…")
            self.btn_stop.setEnabled(False)
            self.worker.stop()

    @Slot(str, int, int)
    def _on_progress(self, stage, done, total):
        bar = {"prep": self.pb_prep, "gen": self.pb_gen}.get(stage)
        if bar is None:
            return
        bar.setRange(0, max(1, total))
        bar.setValue(done)
        self._stage_text[stage] = (done, total)
        self._update_status_text()

    @Slot(dict)
    def _on_stats(self, d):
        remain = max(0, d["total"] - d["done"]) * d["avg"]
        self._eta = self._fmt_time(remain)
        self.lbl_stats.setText(f"平均每条 {d['avg']:.2f}s · 剩余约 {self._eta}")
        self._update_status_text()

    @Slot(int, int)
    def _on_ask(self, requested, remaining):
        box = QMessageBox(self)
        box.setWindowTitle("组合数量不够")
        box.setIcon(QMessageBox.Question)
        box.setText(f"你要生成 {requested:,} 条，但剩余不重复的组合只有 {remaining:,} 个。")
        b_rem = box.addButton(f"只生成剩余 {remaining:,} 条", QMessageBox.AcceptRole)
        b_rep = box.addButton("允许重复，生成全部", QMessageBox.DestructiveRole)
        b_can = box.addButton("取消", QMessageBox.RejectRole)
        if remaining <= 0:
            b_rem.setEnabled(False)
        box.exec()
        clicked = box.clickedButton()
        ans = "remaining" if clicked is b_rem else "repeat" if clicked is b_rep else "cancel"
        _ = b_can
        if self.worker:
            self.worker.answer(ans)

    @staticmethod
    def _fmt_time(sec):
        sec = int(round(sec))
        if sec < 60:
            return f"{sec} 秒"
        if sec < 3600:
            return f"{sec // 60} 分 {sec % 60} 秒"
        return f"{sec // 3600} 小时 {sec % 3600 // 60} 分"

    def _update_status_text(self):
        """窗口标题和游戏窗口里同步显示进度"""
        gd, gt = self._stage_text.get("gen", (0, 0))
        pd, pt = self._stage_text.get("prep", (0, 0))
        if not self._running():
            return
        if gd > 0 and gt > 0:
            text = f"正在生成 {gd}/{gt}（{gd * 100 // gt}%）"
            if self._eta:
                text += f"，剩余约 {self._eta}"
            title = f"{gd * 100 // gt}% - 批量混剪"
        elif pt > 1:
            text = f"正在预处理素材 {pd}/{pt}"
            title = f"预处理 {pd}/{pt} - 批量混剪"
        else:
            text = "正在准备…"
            title = "处理中 - 批量混剪"
        self.setWindowTitle(title)
        if self._game is not None:
            self._game.set_progress("混剪进度：" + text)

    def _open_game(self):
        try:
            from .game import GameDialog
            if self._game is None:
                self._game = GameDialog(self)
            self._game.show()
            self._game.raise_()
            self._game.activateWindow()
            self._update_status_text()
        except Exception as e:  # noqa  游戏出任何问题都不能影响混剪
            self._game = None
            QMessageBox.information(self, "小游戏", f"小游戏打不开：{e}")

    @Slot(dict)
    def _on_finished(self, summary):
        self._set_editing(True)
        self.setWindowTitle(self._base_title)
        if summary.get("error"):
            done_text = "出错了，请看主窗口"
        elif summary.get("stopped"):
            done_text = f"已停止，完成 {summary['done']} 条"
        else:
            done_text = f"✅ 全部完成：成功 {summary['done']} 条，失败 {summary['failed']} 条"
        if self._game is not None:
            self._game.set_progress("混剪进度：" + done_text)
        try:
            QApplication.alert(self, 0)   # 任务栏闪烁提醒
            QApplication.beep()
        except Exception:  # noqa
            pass
        self._refresh_combo()
        self._refresh_cache_size()
        if summary.get("error"):
            QMessageBox.warning(self, "出错了", summary["error"])
        elif summary.get("stopped"):
            self._log(f"已停止，完成 {summary['done']} 条")
        else:
            self._log(f"✅ 全部完成：成功 {summary['done']} 条，失败 {summary['failed']} 条")
        self.worker = None

    @Slot(str)
    def _log(self, msg):
        self.log_view.appendPlainText(msg)


def run_app():
    import sys
    app = QApplication(sys.argv)
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)
    apply_theme(app)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
