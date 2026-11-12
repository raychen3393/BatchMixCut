"""等待时玩的小游戏：贪吃蛇。和混剪互不影响（混剪在后台线程里跑）。"""
import json
import os
import random
import time

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QTabWidget, QVBoxLayout, QWidget)

from . import lan_game as lg

from .utils import data_root

COLS, ROWS, CELL = 20, 20, 22


def _score_path():
    return os.path.join(data_root(), "game.json")


def _load_all():
    try:
        with open(_score_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa
        return {}


def load_best(key="best"):
    try:
        return int(_load_all().get(key, 0))
    except Exception:  # noqa
        return 0


def save_best(v, key="best"):
    try:
        d = _load_all()
        d[key] = int(v)
        with open(_score_path(), "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:  # noqa
        pass


def load_text(key, default=""):
    v = _load_all().get(key, default)
    return v if isinstance(v, str) else default


def save_text(key, value):
    try:
        d = _load_all()
        d[key] = str(value)
        with open(_score_path(), "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:  # noqa
        pass


def _key_int(k):
    """不同版本的 PySide6 里按键可能是 int 或枚举，统一成 int 再比较"""
    try:
        return int(k)
    except (TypeError, ValueError):
        return int(getattr(k, "value", -1))


class SnakeBoard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(COLS * CELL, ROWS * CELL)
        self.setFocusPolicy(Qt.StrongFocus)
        self.best = load_best()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.wrap = False         # 穿墙模式：从一边出去，从另一边回来
        self.state = "ready"      # ready / playing / paused / over
        self._reset()

    def _reset(self):
        cx, cy = COLS // 2, ROWS // 2
        self.snake = [(cx, cy), (cx - 1, cy), (cx - 2, cy)]
        self.direction = (1, 0)
        self.pending = []          # 按键队列，避免一帧内连按两次导致掉头撞自己
        self.score = 0
        self._place_food()

    def _place_food(self):
        free = [(x, y) for x in range(COLS) for y in range(ROWS) if (x, y) not in self.snake]
        self.food = random.choice(free) if free else None

    def _interval(self):
        return max(60, 140 - self.score * 3)   # 吃得越多越快

    def start(self):
        self._reset()
        self.state = "playing"
        self.timer.start(self._interval())
        self.update()

    def toggle_pause(self):
        if self.state == "playing":
            self.state = "paused"
            self.timer.stop()
        elif self.state == "paused":
            self.state = "playing"
            self.timer.start(self._interval())
        self.update()

    def stop(self):
        self.timer.stop()

    def _tick(self):
        if self.state != "playing":
            return
        if self.pending:
            d = self.pending.pop(0)
            if (d[0] + self.direction[0], d[1] + self.direction[1]) != (0, 0):
                self.direction = d
        hx, hy = self.snake[0]
        nx, ny = hx + self.direction[0], hy + self.direction[1]
        if self.wrap:
            nx, ny = nx % COLS, ny % ROWS
        hit_wall = not (0 <= nx < COLS and 0 <= ny < ROWS)
        if hit_wall or (nx, ny) in self.snake[:-1]:
            self.state = "over"
            self.timer.stop()
            if self.score > self.best:
                self.best = self.score
                save_best(self.best)
            self.update()
            return
        self.snake.insert(0, (nx, ny))
        if self.food is not None and (nx, ny) == self.food:
            self.score += 1
            self._place_food()
            self.timer.start(self._interval())
            if self.food is None:      # 铺满全屏，通关
                self.state = "over"
                self.timer.stop()
        else:
            self.snake.pop()
        self.update()

    def keyPressEvent(self, e):
        k = _key_int(e.key())
        dirs = {}
        for names, d in ((("Key_Left", "Key_A"), (-1, 0)), (("Key_Right", "Key_D"), (1, 0)),
                         (("Key_Up", "Key_W"), (0, -1)), (("Key_Down", "Key_S"), (0, 1))):
            for n in names:
                dirs[_key_int(getattr(Qt, n))] = d
        if k in dirs:
            if self.state == "ready" or self.state == "over":
                self.start()
            if self.state == "playing" and len(self.pending) < 2:
                self.pending.append(dirs[k])
        elif k == _key_int(Qt.Key_Space):
            if self.state in ("ready", "over"):
                self.start()
            else:
                self.toggle_pause()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        try:
            p.fillRect(0, 0, self.width(), self.height(), QColor("#0f172a"))
            # 棋盘淡格
            for x in range(COLS):
                for y in range(ROWS):
                    if (x + y) % 2 == 0:
                        p.fillRect(x * CELL, y * CELL, CELL, CELL, QColor("#131d33"))
            if self.food is not None:
                fx, fy = self.food
                p.fillRect(fx * CELL + 4, fy * CELL + 4, CELL - 8, CELL - 8, QColor("#f59e0b"))
            for i, (x, y) in enumerate(self.snake):
                color = QColor("#4ade80") if i == 0 else QColor("#22c55e")
                p.fillRect(x * CELL + 1, y * CELL + 1, CELL - 2, CELL - 2, color)
            msg = ""
            if self.state == "ready":
                msg = "按 空格 或 方向键 开始"
            elif self.state == "paused":
                msg = "已暂停（空格继续）"
            elif self.state == "over":
                msg = f"游戏结束  得分 {self.score}\n按 空格 再来一局"
            if msg:
                p.fillRect(0, 0, self.width(), self.height(), QColor(15, 23, 42, 170))
                p.setPen(QColor("#ffffff"))
                f = QFont(self.font())
                f.setPointSize(13)
                f.setBold(True)
                p.setFont(f)
                p.drawText(QRect(0, 0, self.width(), self.height()), Qt.AlignCenter, msg)
        finally:
            p.end()


# ============================ 坦克混战（单人打电脑 / 局域网最多 4 人） ============================

P_COLORS = ["#3b82f6", "#ef4444", "#22c55e", "#eab308"]
P_TEXT = ["#93c5fd", "#fca5a5", "#86efac", "#fde047"]
P_NAMES = ["蓝", "红", "绿", "黄"]


class DuelBoard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(lg.W, lg.H)
        self.setFocusPolicy(Qt.StrongFocus)
        self.view = None          # 房主发来的画面状态
        self.me = 0
        self.hint = "选择上面的一种玩法开始"
        self.held = []            # 按住的方向键，最后按下的优先
        self.fire = False
        self.on_input = None      # 回调：(mx, my, fire)
        self.on_action = None     # 回调：空格（开始 / 再来一局）

    def set_view(self, view, me):
        self.view, self.me = view, me
        self.update()

    def reset_keys(self):
        self.held, self.fire = [], False

    def _dir_of(self, e):
        k = _key_int(e.key())
        for names, d in ((("Key_Left", "Key_A"), (-1, 0)), (("Key_Right", "Key_D"), (1, 0)),
                         (("Key_Up", "Key_W"), (0, -1)), (("Key_Down", "Key_S"), (0, 1))):
            for n in names:
                if k == _key_int(getattr(Qt, n)):
                    return d
        return None

    def _emit(self):
        mx, my = self.held[-1] if self.held else (0, 0)
        if self.on_input:
            self.on_input(mx, my, self.fire)

    def keyPressEvent(self, e):
        try:
            if e.isAutoRepeat():
                return
        except Exception:  # noqa
            pass
        d = self._dir_of(e)
        if d is not None:
            if d in self.held:
                self.held.remove(d)
            self.held.append(d)
            self._emit()
        elif _key_int(e.key()) == _key_int(Qt.Key_Space):
            self.fire = True
            self._emit()
            if self.on_action:
                self.on_action()
        else:
            super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        try:
            if e.isAutoRepeat():
                return
        except Exception:  # noqa
            pass
        d = self._dir_of(e)
        if d is not None:
            if d in self.held:
                self.held.remove(d)
            self._emit()
        elif _key_int(e.key()) == _key_int(Qt.Key_Space):
            self.fire = False
            self._emit()
        else:
            super().keyReleaseEvent(e)

    def focusOutEvent(self, e):
        # 切到别的窗口时松开所有键，免得坦克一直走
        if self.held or self.fire:
            self.reset_keys()
            self._emit()
        super().focusOutEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        try:
            w, h = lg.W, lg.H
            p.fillRect(0, 0, w, h, QColor("#0f172a"))
            for x, y, ww, hh in lg.WALLS:
                p.fillRect(x, y, ww, hh, QColor("#475569"))
            v = self.view
            msg = self.hint
            note = ""
            f = QFont(self.font())
            f.setBold(True)
            if v:
                T = lg.TANK
                ps = v.get("p", [])
                wins = v.get("wins", [])
                here = [i for i, q in enumerate(ps) if len(q) > 5 and q[5]]
                for i in here:
                    x, y, fx, fy, hp = ps[i][:5]
                    if hp <= 0:
                        continue
                    p.fillRect(x, y, T, T, QColor(P_COLORS[i]))
                    cx, cy = x + T // 2, y + T // 2      # 炮管
                    if fx:
                        p.fillRect(cx if fx > 0 else cx - 14, cy - 2, 14, 4, QColor("#e2e8f0"))
                    else:
                        p.fillRect(cx - 2, cy if fy > 0 else cy - 14, 4, 14, QColor("#e2e8f0"))
                    if i == self.me:                      # 自己的坦克加一个白框
                        p.setPen(QColor("#ffffff"))
                        p.drawRect(x - 2, y - 2, T + 3, T + 3)
                for bx, by, _owner in v.get("b", []):
                    p.fillRect(bx - 3, by - 3, 6, 6, QColor("#fde047"))
                # 顶部状态：每人的血量和胜场
                f.setPointSize(9)
                p.setFont(f)
                slot_w = w // max(1, len(here))
                for k, i in enumerate(here):
                    hp = max(0, ps[i][4])
                    text = f"{P_NAMES[i]}{'（你）' if i == self.me else ''} {'♥' * hp if hp else '出局'}"
                    if i < len(wins) and wins[i]:
                        text += f"  胜{wins[i]}"
                    p.setPen(QColor(P_TEXT[i]))
                    p.drawText(QRect(k * slot_w, 2, slot_w, 18), Qt.AlignHCenter, text)
                st = v.get("st")
                n = len(here)
                if st == "playing":
                    msg = ""
                    if self.me < len(ps) and ps[self.me][4] <= 0:
                        note = "你已出局，观战中…"
                elif st == "countdown":
                    msg = f"准备… {int(v.get('cd', 0)) + 1}"
                elif st == "waiting":
                    if self.me == 0:
                        msg = (f"房间里有 {n} 人\n按 空格 开始" if n >= 2
                               else "等待其他人加入…（至少 2 人才能开始）")
                    else:
                        msg = f"房间里有 {n} 人，等房主按空格开始…"
                elif st == "over":
                    win = v.get("w", -1)
                    if win < 0:
                        msg = "平局！"
                    elif win == self.me:
                        msg = "你赢了！"
                    else:
                        msg = f"{P_NAMES[win]}方获胜"
                    msg += "\n按 空格 再来一局"
            if msg:
                p.fillRect(0, 0, w, h, QColor(15, 23, 42, 150))
                p.setPen(QColor("#ffffff"))
                f.setPointSize(13)
                p.setFont(f)
                p.drawText(QRect(0, 0, w, h), Qt.AlignCenter, msg)
            elif note:
                p.setPen(QColor("#e2e8f0"))
                f.setPointSize(10)
                p.setFont(f)
                p.drawText(QRect(0, h - 24, w, 20), Qt.AlignHCenter, note)
        finally:
            p.end()


class LanPage(QWidget):
    """坦克混战页面：单人练习 / 创建房间 / 加入房间"""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.btn_solo = QPushButton("单人练习（打 3 个电脑）")
        self.btn_host = QPushButton("创建房间")
        self.edit_ip = QLineEdit(load_text("last_ip"))
        self.edit_ip.setPlaceholderText("房主的 IP，例如 192.168.1.5")
        self.btn_join = QPushButton("加入房间")
        self.btn_leave = QPushButton("退出")
        for b in (self.btn_solo, self.btn_host, self.btn_join, self.btn_leave):
            b.setFocusPolicy(Qt.NoFocus)       # 空格留给开火，不触发按钮
        row.addWidget(self.btn_solo)
        row.addWidget(self.btn_host)
        row.addWidget(self.edit_ip, 1)
        row.addWidget(self.btn_join)
        row.addWidget(self.btn_leave)
        lay.addLayout(row)
        self.lbl = QLabel("联机最多 4 人：一人点“创建房间”，其他人输入房主显示的 IP 后点“加入房间”，人齐了房主按空格开始。")
        self.lbl.setWordWrap(True)
        lay.addWidget(self.lbl)
        self.board = DuelBoard(self)
        self.board.on_input = self._on_input
        self.board.on_action = self._on_action
        lay.addWidget(self.board, 0, Qt.AlignCenter)
        tip = QLabel("方向键 / WASD 移动，空格 开火。每人 3 滴血，墙可以挡子弹，最后活下来的获胜。")
        tip.setObjectName("hint")
        lay.addWidget(tip)

        self.mode = None           # None / solo / host / client
        self.me = 0
        self.duel = None
        self.host = None
        self.client = None
        self._addr = ""
        self._last = time.monotonic()
        self.btn_solo.clicked.connect(self.start_solo)
        self.btn_host.clicked.connect(self.start_host)
        self.btn_join.clicked.connect(self.start_join)
        self.btn_leave.clicked.connect(lambda: self.leave("已退出。"))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

    # ---------- 开始 / 退出 ----------
    def leave(self, text=None):
        for link in (self.host, self.client):
            try:
                if link is not None:
                    link.close()
            except Exception:  # noqa
                pass
        self.host = self.client = self.duel = None
        self.mode = None
        self.me = 0
        self.board.reset_keys()
        self.board.hint = "选择上面的一种玩法开始"
        self.board.set_view(None, 0)
        if text:
            self.lbl.setText(text)

    def start_solo(self):
        self.leave()
        self.mode = "solo"
        self.duel = lg.Duel(bots=(1, 2, 3))
        self.duel.add_player(0)
        self.duel.start_round()
        self._last = time.monotonic()
        self.lbl.setText("单人练习：你是蓝色（带白框），另外 3 辆是电脑，它们之间也会互相打。")
        self.board.setFocus()

    def _host_text(self, extra=""):
        n = self.duel.count() if self.duel else 1
        return (f"房间已创建（{n}/4 人），你是蓝色。让其他人在“加入房间”左边输入：{self._addr}\n"
                + (extra or "如果 Windows 防火墙弹窗，请点“允许访问”，否则别人连不上。"))

    def start_host(self):
        self.leave()
        try:
            self.host = lg.Host()
        except Exception as e:  # noqa
            self.lbl.setText(f"创建房间失败：{e}")
            return
        self.mode = "host"
        self.duel = lg.Duel()
        self.duel.add_player(0)
        self._last = time.monotonic()
        self._addr = lg.local_ip()
        if self.host.port != lg.DEFAULT_PORT:
            self._addr += f":{self.host.port}"
        self.lbl.setText(self._host_text())
        self.board.setFocus()

    def start_join(self):
        self.leave()
        try:
            ip, port = lg.parse_address(self.edit_ip.text())
            self.client = lg.Client(ip, port, timeout=3.0)
        except Exception as e:  # noqa
            self.client = None
            self.lbl.setText(f"加入失败：{e}\n请确认 IP 没输错、对方已经点了“创建房间”、两台电脑在同一个局域网，"
                             "并且对方的防火墙允许了访问。")
            return
        save_text("last_ip", self.edit_ip.text().strip())
        self.mode = "client"
        self.me = -1               # 等房主分配颜色
        self.lbl.setText("已连接房间…")
        self.board.hint = "已连接，等待房主…"
        self.board.setFocus()

    # ---------- 按键 ----------
    def _on_input(self, mx, my, fire):
        if self.mode in ("solo", "host") and self.duel is not None:
            self.duel.set_input(0, mx, my, fire)
        elif self.mode == "client" and self.client is not None:
            self.client.send({"t": "in", "mx": mx, "my": my, "fire": bool(fire)})

    def _on_action(self):
        if self.mode in ("solo", "host") and self.duel is not None:
            if self.duel.status in ("over", "waiting"):
                self.duel.start_round()
        elif self.mode == "client" and self.client is not None:
            self.client.send({"t": "act"})

    # ---------- 每帧 ----------
    def _tick(self):
        try:
            self._tick_inner()
        except Exception as e:  # noqa  小游戏出错不能影响混剪
            self.leave(f"小游戏出错了：{e}")

    def _tick_inner(self):
        now = time.monotonic()
        dt, self._last = now - self._last, now
        if self.mode == "solo":
            self.duel.step(dt)
            self.board.set_view(self.duel.snapshot(), 0)
        elif self.mode == "host":
            for m in self.host.poll():
                t, slot = m.get("t"), m.get("slot", -1)
                if not isinstance(slot, int) or not (1 <= slot < lg.MAX_PLAYERS):
                    continue
                if t == "joined":
                    self.duel.add_player(slot)       # 对局进行中加入的人，下一局上场
                    self.lbl.setText(self._host_text(f"{P_NAMES[slot]}方（{m.get('addr', '')}）加入了。"
                                                     "人齐后按 空格 开始。"))
                elif t == "in":
                    self.duel.set_input(slot, m.get("mx", 0), m.get("my", 0), m.get("fire", False))
                elif t == "act":
                    if self.duel.status == "over":   # 结束后谁都可以按空格再来一局；第一局由房主开始
                        self.duel.start_round()
                elif t == "bye":
                    self.duel.remove_player(slot)
                    self.duel._check_over()
                    if self.duel.count() < 2 and self.duel.status != "waiting":
                        self.duel.status = "waiting"
                    self.lbl.setText(self._host_text(f"{P_NAMES[slot]}方离开了。"))
            self.duel.step(dt)
            snap = self.duel.snapshot()
            self.host.send(snap)
            self.board.set_view(snap, 0)
        elif self.mode == "client":
            latest = None
            for m in self.client.poll():
                t = m.get("t")
                if t == "state":
                    latest = m
                elif t == "hello":
                    you = m.get("you")
                    if isinstance(you, int) and 0 < you < lg.MAX_PLAYERS:
                        self.me = you
                        self.lbl.setText(f"已加入房间，你是{P_NAMES[you]}色（带白框的那辆）。")
                elif t == "bye":
                    self.leave("连接断开了（房间已满、房主退出或网络中断）。")
                    return
            if latest is not None and self.me > 0:
                self.board.set_view(latest, self.me)


class GameDialog(QDialog):
    """非模态窗口：一边玩，一边在顶部看混剪进度。两个游戏用标签页切换。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("小游戏 - 等待时玩一会儿")
        lay = QVBoxLayout(self)
        self.lbl_progress = QLabel("混剪进度：还没开始")
        self.lbl_progress.setObjectName("stats")
        lay.addWidget(self.lbl_progress)

        self.tabs = QTabWidget()
        try:
            self.tabs.tabBar().setFocusPolicy(Qt.NoFocus)   # 方向键留给游戏，不切换标签
        except Exception:  # noqa
            pass
        page1 = QWidget()
        l1 = QVBoxLayout(page1)
        self.board = SnakeBoard(page1)
        l1.addWidget(self.board, 0, Qt.AlignCenter)
        self.chk_wrap = QCheckBox("穿墙模式（撞墙不死，从另一边出来）")
        self.chk_wrap.setFocusPolicy(Qt.NoFocus)
        self.chk_wrap.toggled.connect(self._set_wrap)
        l1.addWidget(self.chk_wrap)
        self.lbl_score = QLabel("")
        l1.addWidget(self.lbl_score)
        t1 = QLabel("方向键 / WASD 控制，空格 开始或暂停，越吃越快。")
        t1.setObjectName("hint")
        l1.addWidget(t1)
        self.tabs.addTab(page1, "贪吃蛇")
        self.lan = LanPage()
        self.tabs.addTab(self.lan, "坦克混战（可 4 人联机）")
        self.tabs.currentChanged.connect(self._tab_changed)
        lay.addWidget(self.tabs)

        tip = QLabel("关掉这个窗口不影响混剪。")
        tip.setObjectName("hint")
        lay.addWidget(tip)
        self._score_timer = QTimer(self)
        self._score_timer.timeout.connect(self._refresh_score)
        self._score_timer.start(200)
        self._refresh_score()

    def _set_wrap(self, on):
        self.board.wrap = bool(on)
        self.board.setFocus()

    def _on_lan_tab(self):
        try:
            return int(self.tabs.currentIndex()) == 1
        except Exception:  # noqa
            return False

    def _tab_changed(self, _i):
        if self.board.state == "playing":
            self.board.toggle_pause()
        (self.lan.board if self._on_lan_tab() else self.board).setFocus()

    def _refresh_score(self):
        b = self.board
        self.lbl_score.setText(f"得分：{b.score}    最高分：{max(b.best, b.score)}")

    def set_progress(self, text):
        self.lbl_progress.setText(text)

    def showEvent(self, e):
        super().showEvent(e)
        (self.lan.board if self._on_lan_tab() else self.board).setFocus()

    def closeEvent(self, e):
        if self.board.state == "playing":
            self.board.toggle_pause()
        self.lan.leave("已退出。")        # 关窗口时关闭房间 / 断开连接
        super().closeEvent(e)
