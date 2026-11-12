"""联机小游戏「坦克混战」（最多 4 人）的规则和网络部分（不依赖界面，方便测试）。

房主的电脑负责计算（移动、子弹、命中），每秒约 30 次把画面状态发给对方；
对方只把自己的按键发给房主。数据是一行一个 JSON，走局域网 TCP。
"""
import json
import queue
import random
import socket
import threading

W, H = 640, 400            # 场地大小（4 人版，比 2 人版大）
TANK = 20                  # 坦克边长
SPEED = 130.0              # 坦克速度（每秒）
BULLET_SPEED = 330.0
BULLET_R = 3
COOLDOWN = 0.45            # 开火间隔（秒）
MAX_BULLETS = 3            # 每人同时在场的子弹数
MAX_HP = 3
MAX_PLAYERS = 4
DEFAULT_PORT = 47800

# 地图：中间一个碉堡，四周对称的掩体，四个角是出生点
WALLS = [(296, 176, 48, 48),
         (150, 70, 20, 90), (150, 240, 20, 90), (470, 70, 20, 90), (470, 240, 20, 90),
         (250, 90, 140, 16), (250, 294, 140, 16),
         # 四条边中间各有一堵墙：出生点之间互相看不见，开局不会被直接打到
         (0, 190, 70, 20), (570, 190, 70, 20), (310, 0, 20, 60), (310, 340, 20, 60)]
SPAWNS = [(30, 30, 1, 0), (W - 30 - TANK, H - 30 - TANK, -1, 0),
          (W - 30 - TANK, 30, -1, 0), (30, H - 30 - TANK, 1, 0)]


def _hit(ax, ay, aw, ah, bx, by, bw, bh):
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


class Duel:
    """最多 4 人的混战：最后活下来的人获胜"""

    def __init__(self, bots=()):
        self.present = [False] * MAX_PLAYERS      # 这个位置有没有人
        self.bots = set(bots)                     # 由电脑控制的位置
        self.wins = [0] * MAX_PLAYERS
        self.status = "waiting"     # waiting 等人 / countdown 倒计时 / playing / over
        self.countdown = 0.0
        self.winner = -1
        self._bot = {i: {"t": 0.0, "fire": random.uniform(0.0, 0.6), "last": None}
                     for i in range(MAX_PLAYERS)}
        self.p = [self._new_player(i, False) for i in range(MAX_PLAYERS)]
        self.bullets = []
        for i in self.bots:
            self.present[i] = True

    @staticmethod
    def _new_player(i, alive):
        x, y, fx, fy = SPAWNS[i]
        return {"x": float(x), "y": float(y), "fx": fx, "fy": fy, "hp": MAX_HP if alive else 0,
                "cd": 0.0, "mx": 0, "my": 0, "fire": False}

    def add_player(self, i):
        if 0 <= i < MAX_PLAYERS:
            self.present[i] = True

    def remove_player(self, i):
        if 0 <= i < MAX_PLAYERS:
            self.present[i] = False
            self.p[i]["hp"] = 0            # 中途离开算出局
            self.wins[i] = 0

    def count(self):
        return sum(self.present)

    def alive(self):
        return [i for i in range(MAX_PLAYERS) if self.present[i] and self.p[i]["hp"] > 0]

    def reset_round(self):
        self.p = [self._new_player(i, self.present[i]) for i in range(MAX_PLAYERS)]
        self.bullets = []           # [x, y, dx, dy, owner]
        self.winner = -1

    def start_round(self):
        if self.count() < 2:
            return False
        self.reset_round()
        self.status = "countdown"
        self.countdown = 3.0
        return True

    def set_input(self, i, mx, my, fire):
        """mx / my 取 -1、0、1，只能沿一个方向走"""
        try:
            i, mx, my = int(i), int(mx), int(my)
        except (TypeError, ValueError):
            return
        if not (0 <= i < MAX_PLAYERS):
            return
        mx = max(-1, min(1, mx))
        my = max(-1, min(1, my))
        if mx:
            my = 0
        pl = self.p[i]
        pl["mx"], pl["my"], pl["fire"] = mx, my, bool(fire)

    # ---------------- 电脑对手 ----------------
    def _clear_line(self, x0, y0, x1, y1):
        steps = int(max(abs(x1 - x0), abs(y1 - y0)) // 8) + 1
        for k in range(steps + 1):
            x = x0 + (x1 - x0) * k / steps
            y = y0 + (y1 - y0) * k / steps
            for w in WALLS:
                if _hit(x - 2, y - 2, 4, 4, *w):
                    return False
        return True

    def _bot_think(self, i, dt):
        me, st = self.p[i], self._bot[i]
        st["t"] -= dt
        st["fire"] -= dt
        foes = [j for j in self.alive() if j != i]
        if not foes:
            self.set_input(i, 0, 0, False)
            return
        mcx, mcy = me["x"] + TANK / 2, me["y"] + TANK / 2
        foe = min(foes, key=lambda j: abs(self.p[j]["x"] - me["x"]) + abs(self.p[j]["y"] - me["y"]))
        fcx, fcy = self.p[foe]["x"] + TANK / 2, self.p[foe]["y"] + TANK / 2
        dx, dy = fcx - mcx, fcy - mcy
        aligned_x = abs(dy) < 9          # 在同一横线上
        aligned_y = abs(dx) < 9
        if (aligned_x or aligned_y) and self._clear_line(mcx, mcy, fcx, fcy):
            if aligned_x:
                mx, my = (1 if dx > 0 else -1), 0
            else:
                mx, my = 0, (1 if dy > 0 else -1)
            fire = st["fire"] <= 0
            if fire:
                st["fire"] = random.uniform(0.55, 1.15)   # 开火节奏有快有慢，不会总是同归于尽
            self.set_input(i, mx, my, fire)
            st["t"] = 0.15
            return
        if st["t"] > 0:
            me["fire"] = False
            return
        stuck = (st["last"] is not None and (me["mx"] or me["my"])
                 and abs(me["x"] - st["last"][0]) + abs(me["y"] - st["last"][1]) < 2)
        st["last"] = (me["x"], me["y"])
        st["t"] = random.uniform(0.25, 0.5)
        if stuck:                          # 被墙挡住：换成垂直方向绕过去
            if me["mx"]:
                mx, my = 0, random.choice([1, -1])
            else:
                mx, my = random.choice([1, -1]), 0
            st["t"] = random.uniform(0.35, 0.7)
        elif random.random() < 0.12:       # 偶尔乱走，走位不那么死板
            mx, my = random.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
        elif abs(dx) >= 9 and (abs(dx) >= abs(dy) or abs(dy) < 9):
            mx, my = (1 if dx > 0 else -1), 0      # 先沿差得多的方向靠近，走到同一条线上再开火
        else:
            mx, my = 0, (1 if dy > 0 else -1)
        self.set_input(i, mx, my, False)

    # ---------------- 每一帧 ----------------
    def step(self, dt):
        dt = max(0.0, min(0.1, dt))
        if self.status == "countdown":
            self.countdown -= dt
            if self.countdown <= 0:
                self.status = "playing"
            return
        if self.status != "playing":
            return
        living = self.alive()
        for i in living:
            if i in self.bots:
                self._bot_think(i, dt)
        for i in living:
            pl = self.p[i]
            pl["cd"] = max(0.0, pl["cd"] - dt)
            if pl["mx"] or pl["my"]:
                pl["fx"], pl["fy"] = pl["mx"], pl["my"]
                nx = pl["x"] + pl["mx"] * SPEED * dt
                ny = pl["y"] + pl["my"] * SPEED * dt
                nx = max(0.0, min(W - TANK, nx))
                ny = max(0.0, min(H - TANK, ny))
                blocked = any(_hit(nx, ny, TANK, TANK, *w) for w in WALLS) or any(
                    _hit(nx, ny, TANK, TANK, self.p[j]["x"], self.p[j]["y"], TANK, TANK)
                    for j in living if j != i and self.p[j]["hp"] > 0)
                if not blocked:
                    pl["x"], pl["y"] = nx, ny
            if pl["fire"] and pl["cd"] <= 0 and sum(1 for b in self.bullets if b[4] == i) < MAX_BULLETS:
                cx = pl["x"] + TANK / 2 + pl["fx"] * (TANK / 2 + 4)
                cy = pl["y"] + TANK / 2 + pl["fy"] * (TANK / 2 + 4)
                self.bullets.append([cx, cy, pl["fx"], pl["fy"], i])
                pl["cd"] = COOLDOWN
        kept = []
        for b in self.bullets:
            # 分小步移动，子弹不会穿过薄墙或坦克
            dist = BULLET_SPEED * dt
            gone = False
            while dist > 0 and not gone:
                s = min(6.0, dist)
                dist -= s
                b[0] += b[2] * s
                b[1] += b[3] * s
                if not (0 <= b[0] <= W and 0 <= b[1] <= H):
                    gone = True
                    break
                box = (b[0] - BULLET_R, b[1] - BULLET_R, 2 * BULLET_R, 2 * BULLET_R)
                if any(_hit(*box, *w) for w in WALLS):
                    gone = True
                    break
                for j in range(MAX_PLAYERS):
                    t = self.p[j]
                    if j != b[4] and self.present[j] and t["hp"] > 0 and _hit(
                            *box, t["x"], t["y"], TANK, TANK):
                        t["hp"] -= 1
                        gone = True
                        break
            if not gone:
                kept.append(b)
        self.bullets = kept
        self._check_over()

    def _check_over(self):
        if self.status != "playing":
            return
        living = self.alive()
        if len(living) <= 1:
            self.status = "over"
            self.winner = living[0] if living else -1
            if self.winner >= 0:
                self.wins[self.winner] += 1

    def snapshot(self):
        return {"t": "state", "st": self.status, "cd": round(max(0.0, self.countdown), 1),
                "p": [[int(pl["x"]), int(pl["y"]), pl["fx"], pl["fy"], pl["hp"], int(self.present[i])]
                      for i, pl in enumerate(self.p)],
                "b": [[int(b[0]), int(b[1]), b[4]] for b in self.bullets],
                "w": self.winner, "wins": list(self.wins)}


# ============================ 网络 ============================

def local_ip():
    """本机在局域网里的 IP（给对方输入用）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))      # 不会真的发数据，只是让系统选出网卡
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:  # noqa
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa
            return "127.0.0.1"


def _shut(sock):
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class _Out:
    """发送队列：用单独的线程发数据。对方卡住时只丢掉来不及发的画面，
    不会把游戏窗口（和混剪主界面）一起卡住；卡住超过约 5 秒就断开这个连接。"""

    def __init__(self, conn):
        self.conn = conn
        self.q = queue.Queue(maxsize=32)
        self.dead = False
        self._full = 0
        threading.Thread(target=self._run, daemon=True).start()

    def put(self, obj):
        if self.dead:
            return False
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self.q.put_nowait(data)
            self._full = 0
            return True
        except queue.Full:
            self._full += 1
            if self._full > 150:
                self.dead = True
                _shut(self.conn)
            return False

    def _run(self):
        while True:
            data = self.q.get()
            if data is None:
                return
            try:
                self.conn.sendall(data)
            except OSError:
                self.dead = True
                return

    def stop(self):
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass


class _Link:
    """一条连接：后台线程收消息放进队列，界面定时来取"""

    def __init__(self):
        self.inbox = queue.Queue()
        self._conn = None
        self._out = None
        self._lock = threading.Lock()
        self._closed = False

    def poll(self):
        out = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                return out

    def send(self, obj):
        out = self._out
        return out.put(obj) if out is not None else False

    def _read_loop(self, conn):
        buf = b""
        try:
            while not self._closed:
                data = conn.recv(65536)
                if not data:
                    break
                buf += data
                if len(buf) > 262144:      # 不正常的数据，断开
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(msg, dict):
                        self.inbox.put(msg)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @property
    def connected(self):
        return self._conn is not None


class Host:
    """房主：最多接受 3 个人加入（加上自己共 4 人）。位置编号 1~3。"""

    def __init__(self, port=DEFAULT_PORT):
        self.inbox = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._conns = {}            # 位置编号 → 连接
        self._outs = {}             # 位置编号 → 发送队列
        self._srv = None
        last = None
        for p in range(port, port + 10):
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                srv.bind(("", p))
                srv.listen(4)
                self._srv, self.port = srv, p
                break
            except OSError as e:
                last = e
                try:
                    srv.close()
                except OSError:
                    pass
        if self._srv is None:
            raise OSError(f"无法创建房间：{last}")
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def poll(self):
        out = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                return out

    def slots(self):
        with self._lock:
            return sorted(self._conns)

    def _accept_loop(self):
        while not self._closed:
            try:
                conn, addr = self._srv.accept()
            except OSError:
                return
            with self._lock:
                free = [i for i in range(1, MAX_PLAYERS) if i not in self._conns]
                slot = free[0] if free else None
                if slot is not None:
                    self._conns[slot] = conn
                    self._outs[slot] = _Out(conn)
            if slot is None:                  # 房间已满
                _shut(conn)
                continue
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            self.send_to(slot, {"t": "hello", "you": slot})
            self.inbox.put({"t": "joined", "slot": slot, "addr": addr[0]})
            threading.Thread(target=self._serve, args=(conn, slot), daemon=True).start()

    def _serve(self, conn, slot):
        buf = b""
        try:
            while not self._closed:
                data = conn.recv(65536)
                if not data:
                    break
                buf += data
                if len(buf) > 262144:
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(msg, dict):
                        msg["slot"] = slot          # 位置以房主记录的为准，不信任对方自报
                        self.inbox.put(msg)
        except OSError:
            pass
        finally:
            _shut(conn)
            with self._lock:
                if self._conns.get(slot) is conn:
                    del self._conns[slot]
                    out = self._outs.pop(slot, None)
                    if out is not None:
                        out.stop()
            if not self._closed:
                self.inbox.put({"t": "bye", "slot": slot})

    def send_to(self, slot, obj):
        with self._lock:
            out = self._outs.get(slot)
        return out.put(obj) if out is not None else False

    def send(self, obj):
        """发给所有加入的人"""
        with self._lock:
            outs = list(self._outs.values())
        for out in outs:
            out.put(obj)

    def close(self):
        self._closed = True
        with self._lock:
            conns = list(self._conns.values())
            outs = list(self._outs.values())
            self._conns = {}
            self._outs = {}
        for o in outs:
            o.stop()
        with self._lock:
            pass
        for c in conns:
            _shut(c)
        _shut(self._srv)


class Client(_Link):
    def __init__(self, host, port=DEFAULT_PORT, timeout=3.0):
        super().__init__()
        conn = socket.create_connection((host, port), timeout=timeout)
        conn.settimeout(None)
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._conn = conn
        self._out = _Out(conn)
        threading.Thread(target=self._run, args=(conn,), daemon=True).start()

    def _run(self, conn):
        self._read_loop(conn)
        with self._lock:
            self._conn = None
        if not self._closed:
            self.inbox.put({"t": "bye"})

    def close(self):
        self._closed = True
        if self._out is not None:
            self._out.stop()
        _shut(self._conn)
        self._conn = None
        self._out = None


def parse_address(text):
    """'192.168.1.5' 或 '192.168.1.5:47801' → (ip, 端口)"""
    text = (text or "").strip().replace("：", ":")
    if not text:
        raise ValueError("请输入房主的 IP 地址")
    if ":" in text:
        host, port = text.rsplit(":", 1)
        return host.strip(), int(port)
    return text, DEFAULT_PORT
