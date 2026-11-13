"""通用工具：路径、自然排序、扫描素材、磁盘空间等"""
import os
import re
import sys
import shutil
import hashlib

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv"}


def app_dir() -> str:
    """程序所在目录（打包成 exe 后为 exe 所在目录）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_root() -> str:
    """数据目录（缓存 / 历史 / 配置）。程序目录可写就放在程序目录的 data 下（绿色便携），
    否则放到 %LOCALAPPDATA%\\BatchMixCut。可用环境变量 MIXCUT_DATA 覆盖。"""
    env = os.environ.get("MIXCUT_DATA")
    if env:
        os.makedirs(env, exist_ok=True)
        return env
    cand = os.path.join(app_dir(), "data")
    try:
        os.makedirs(cand, exist_ok=True)
        test = os.path.join(cand, ".write_test")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        return cand
    except OSError:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        p = os.path.join(base, "BatchMixCut")
        os.makedirs(p, exist_ok=True)
        return p


def natural_key(s: str):
    """自然排序键：'新建文件夹 (2)' < '新建文件夹 (10)'，数字按大小比较"""
    s = os.path.basename(os.path.normpath(s))
    parts = re.split(r"(\d+)", s)
    key = []
    for i, p in enumerate(parts):
        if i % 2:  # 奇数位一定是数字
            key.append((0, int(p), p))
        else:
            key.append((1, p.lower(), p))
    return key


def natural_sorted(items):
    return sorted(items, key=natural_key)


def list_videos(folder: str):
    """列出文件夹（不递归）中的视频文件，自然排序。
    用 scandir：列目录时系统已经返回“是不是文件”，不用逐个文件再查一次（网络盘上快很多）"""
    files = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                if os.path.splitext(e.name)[1].lower() in VIDEO_EXTS and e.is_file():
                    files.append(e.path)
                    try:    # Windows 上列目录时已经带回了大小和修改时间，这里不会再访问一次硬盘/网络
                        st = e.stat()
                        _sig_cache[e.path] = (st.st_size, st.st_mtime_ns)
                    except OSError:
                        _sig_cache.pop(e.path, None)
    except OSError:
        return []
    return natural_sorted(files)


# 文件“身份”（大小 + 修改时间）：扫描文件夹时记下，之后查缓存、算空间都直接用，
# 不用为同一个文件反复去问硬盘（素材在网络盘上时，每问一次都是一次网络来回）
_sig_cache = {}


def file_sig(path: str):
    """返回 (大小, 修改时间纳秒)。每次开始任务都会重新扫描文件夹，所以不会用到过期的数据。"""
    sig = _sig_cache.get(path)
    if sig is None:
        st = os.stat(path)
        sig = _sig_cache[path] = (st.st_size, st.st_mtime_ns)
    return sig


def list_subfolders(parent: str):
    try:
        with os.scandir(parent) as it:
            subs = [e.path for e in it if e.is_dir()]
    except OSError:
        return []
    return natural_sorted(subs)


def norm_path(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def short_hash(text: str, n: int = 16) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def free_space(path: str) -> int:
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return shutil.disk_usage(p).free


# ---------------- 硬盘类型检测（固态 / 机械），用于自动设置并行数 ----------------
_disk_cache = {}


def disk_kind(path: str) -> str:
    """返回 'ssd' / 'hdd' / 'unknown'。只在 Windows 本地盘上检测，任何失败都返回 unknown。"""
    if os.name != "nt" or not path:
        return "unknown"
    try:
        drive = os.path.splitdrive(os.path.abspath(path))[0].upper()
    except Exception:  # noqa
        return "unknown"
    if len(drive) != 2 or drive[1] != ":":
        return "unknown"       # 网络路径等
    if drive not in _disk_cache:
        kind = _seek_penalty(drive)
        if kind == "unknown":
            kind = _ps_media_type(drive)
        _disk_cache[drive] = kind
    return _disk_cache[drive]


def _seek_penalty(drive: str) -> str:
    """问 Windows 这个盘“有没有寻道延迟”：有 = 机械硬盘，没有 = 固态硬盘（不需要管理员权限）"""
    try:
        import ctypes
        from ctypes import wintypes

        class Query(ctypes.Structure):
            _fields_ = [("PropertyId", wintypes.DWORD), ("QueryType", wintypes.DWORD),
                        ("Extra", ctypes.c_ubyte * 4)]

        class Desc(ctypes.Structure):
            _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD),
                        ("Incurs", ctypes.c_ubyte), ("Pad", ctypes.c_ubyte * 3)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.DeviceIoControl.restype = wintypes.BOOL
        k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                        wintypes.LPVOID, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.CreateFileW("\\\\.\\" + drive, 0, 3, None, 3, 0, None)
        if h is None or h == ctypes.c_void_p(-1).value:
            return "unknown"
        try:
            q, d, n = Query(7, 0), Desc(), wintypes.DWORD(0)   # 7 = StorageDeviceSeekPenaltyProperty
            ok = k32.DeviceIoControl(h, 0x002D1400, ctypes.byref(q), ctypes.sizeof(q),
                                     ctypes.byref(d), ctypes.sizeof(d), ctypes.byref(n), None)
            if not ok:
                return "unknown"
            return "hdd" if d.Incurs else "ssd"
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa
        return "unknown"


def _ps_media_type(drive: str) -> str:
    try:
        import subprocess
        cmd = f"(Get-Partition -DriveLetter {drive[0]} | Get-Disk | Get-PhysicalDisk).MediaType"
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True,
                           timeout=10, creationflags=0x08000000)
        out = r.stdout.decode("utf-8", "replace").strip().upper()
        if "SSD" in out:
            return "ssd"
        if "HDD" in out:
            return "hdd"
    except Exception:  # noqa
        pass
    return "unknown"


def is_network_path(path: str) -> bool:
    """素材是否在网络共享盘上（\\\\服务器\\共享 形式，或映射成盘符的网络驱动器）"""
    try:
        test = os.environ.get("MIXCUT_NET_PREFIX")      # 仅供自测：把某个目录当作网络盘
        ap = os.path.abspath(path)
        if test:
            return ap.startswith(os.path.abspath(test))
        if os.name != "nt":
            return False
        if ap.startswith("\\\\") and not ap.startswith("\\\\?\\"):
            return True
        drive = os.path.splitdrive(ap)[0]
        if len(drive) == 2 and drive[1] == ":":
            import ctypes
            return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4   # DRIVE_REMOTE
    except Exception:  # noqa
        pass
    return False
