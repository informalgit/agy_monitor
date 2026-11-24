"""AgyMonitor — CodexMini 风格的 Antigravity (agy) 额度悬浮卡片。

数据源：本机官方 Antigravity CLI (`agy`)。仅通过
`agy --print /usage --output-format json` 查询各模型分组的实时额度与重置时间，
不读取 OAuth 凭据或直连后台私有接口；查询结果持久化到 last_snapshot.json，
重启时先展示本地缓存，同时在后台发起实时刷新。

实时请求默认沿用系统代理与当前环境变量（适合直连、TUN 模式或已开启系统代理的场景）；
如需自定义代理、绑定项目或调整外观/刷新间隔，直接编辑同目录 config.json（本地私有覆盖可放 config.local.json）。
本程序只用 python 标准库。用 pythonw 运行（无控制台窗口）。

拖动：按住卡片任意位置移动；右键：菜单（显示 / 隐藏 / 重置位置 / 强制刷新 / 退出）。
位置自动记忆到 position.json。
"""

import ctypes
import datetime
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
LOCAL_CONFIG_FILE = os.path.join(HERE, "config.local.json")
POS_FILE = os.path.join(HERE, "position.json")
LOG_FILE = os.path.join(HERE, "log.txt")
LAST_SNAPSHOT_FILE = os.path.join(HERE, "last_snapshot.json")


def _load_config():
    """按 config.json → config.local.json 顺序合并加载配置。"""
    cfg = {}
    for path in (CONFIG_FILE, LOCAL_CONFIG_FILE):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    return cfg


_CFG = _load_config()


def _cfg_str(key, env_key=None, default=""):
    if env_key and env_key in os.environ:
        return str(os.environ.get(env_key, "")).strip()
    val = _CFG.get(key, default)
    return str(default if val is None else val).strip()


def _cfg_num(key, default, cast, lo, hi):
    try:
        val = cast(_CFG.get(key, default))
        if math.isfinite(val):
            return min(hi, max(lo, val))
    except (TypeError, ValueError):
        pass
    return default


# ---------------- CONFIG ----------------
SCALE = _cfg_num("scale", 1.0, float, 0.5, 3.0)          # 整体缩放；想更小可改 0.85
OPACITY = _cfg_num("opacity", 0.95, float, 0.2, 1.0)    # 窗口透明度 0~1
REFRESH_SECONDS = _cfg_num("refresh_seconds", 60, int, 10, 3600)  # 轮询间隔
TOPMOST_WATCHDOG_SECONDS = 5  # 置顶兜底巡检间隔；不依赖 WinEvent 钩子存活
AGY_LIVE_TIMEOUT_SECONDS = 30 # agy --print /usage 单次查询超时
AGY_LIVE_TTL_SECONDS = _cfg_num("live_ttl_seconds", 300, int, 30, 86400)  # 实时快照缓存有效期
AGY_LIVE_RETRY_SECONDS = 60   # 实时查询失败后的重试冷却
AGY_LIVE_MAX_RETRY_SECONDS = 300  # 连续失败时的最大重试间隔
AGY_LIVE_RETRY_ATTEMPTS = 2       # 瞬时失败时最多尝试次数
AGY_LIVE_RETRY_DELAY_SECONDS = 1.0
AGY_LIVE_MAX_STALE_SECONDS = 1800 # 缓存过期上限（30 分钟）
AGY_LIVE_MAX_OUTPUT_BYTES = 1_048_576
AGY_PROXY = _cfg_str("proxy", "AGY_MONITOR_PROXY", "")
AGY_PROJECT = _cfg_str("project", "AGY_MONITOR_PROJECT", "")
MAX_LOG_BYTES = 128 * 1024
LOG_KEEP_BYTES = 64 * 1024
FAMILY = "Microsoft YaHei UI"
# ----------------------------------------
_log_lock = threading.Lock()

BG, CARD, DIVIDER = "#25262a", "#303137", "#383a42"
FG, MUT = "#f2f2f2", "#b6b8c0"
GREEN, AMBER, RED, TRACK = "#4ade80", "#f0b44d", "#f06a6a", "#44464e"

GROUP_NAME_MAP = {
    "Gemini Models": "Gemini",
    "Claude and GPT models": "Claude/GPT",
}

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


def log(msg):
    """写入本地轮转日志 log.txt（线程安全）。"""
    try:
        with _log_lock:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_BYTES:
                with open(LOG_FILE, "rb") as f:
                    f.seek(-LOG_KEEP_BYTES, os.SEEK_END)
                    tail = f.read()
                with open(LOG_FILE, "wb") as f:
                    f.write(b"[earlier log entries rotated]\n" + tail)
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {msg}\n")
    except Exception:
        pass


def _top_level_hwnd(tk_window):
    """Return Tk's native top-level HWND rather than its TkChild HWND."""
    child = int(tk_window.winfo_id())
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        root = user32.GetAncestor(child, 2)  # GA_ROOT
        return int(root or child)
    except Exception:
        return child


def _is_cloaked(hwnd):
    """DWM-cloaked windows report IsWindowVisible()==True yet render nothing."""
    try:
        value = wintypes.DWORD(0)
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(
                hwnd, 14, ctypes.byref(value),  # 14 == DWMWA_CLOAKED
                ctypes.sizeof(value)) == 0:
            return bool(value.value)
    except Exception:
        pass
    return False


def _agy_command():
    """定位官方 agy CLI 可执行文件。"""
    path = shutil.which("agy")
    if not path:
        fallback = os.path.join(
            os.path.expanduser("~"), "AppData", "Local", "agy", "bin", "agy.exe"
        )
        if os.path.exists(fallback):
            path = fallback
    if not path:
        return None
    if path.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", path]
    return [path]


def _agy_env():
    """构造 agy 子进程环境变量；仅在显式配置 AGY_PROXY 时覆盖代理。"""
    env = os.environ.copy()
    proxy = str(AGY_PROXY or "").strip()
    if proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"):
            env[key] = proxy
        env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
    # 额度查询不需要 agy 自更新：新版本会整包下载 ~230 MiB，白占代理流量。
    env.setdefault("AGY_CLI_DISABLE_AUTO_UPDATE", "1")
    return env


def _terminate_process(proc):
    """安全结束子进程，避免竞态报错。"""
    try:
        if proc.poll() is None:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=3)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=1)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass


def _parse_json_output(raw_text):
    """解析 agy stdout JSON，兼容偶发前置提示行。"""
    text = raw_text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    return None


def _request_agy_live_once(cmd):
    """调用官方 `agy --print /usage --output-format json` 获取一次实时额度。"""
    popen_kwargs = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt" else {}
    )
    args = list(cmd)
    if AGY_PROJECT:
        args.extend(["--project", AGY_PROJECT])
    args.extend(["--print", "/usage", "--output-format", "json"])
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_agy_env(),
            **popen_kwargs,
        )
    except OSError as e:
        raise RuntimeError("无法启动 agy CLI") from e

    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=AGY_LIVE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        _terminate_process(proc)
        try:
            proc.communicate(timeout=1)
        except Exception:
            pass
        raise RuntimeError("agy 查询超时") from e

    if len(stdout_bytes) > AGY_LIVE_MAX_OUTPUT_BYTES:
        raise RuntimeError("agy 输出过大")

    stderr_sample = " ".join(
        stderr_bytes.decode("utf-8", "replace").split()
    )[:240]
    raw_text = stdout_bytes.decode("utf-8", "replace")
    payload = _parse_json_output(raw_text)

    if proc.returncode != 0 and payload is None:
        if stderr_sample:
            log("agy stderr: " + stderr_sample)
        raise RuntimeError(f"agy 非零退出 ({proc.returncode})")

    if not payload:
        if stderr_sample:
            log("agy stderr: " + stderr_sample)
        raise RuntimeError("agy 未返回有效 JSON")

    data = (payload.get("command") or {}).get("data")
    if not (isinstance(data, dict) and isinstance(data.get("groups"), list)):
        status = payload.get("status")
        detail = payload.get("error") or payload.get("response") or status or "未知错误"
        if stderr_sample:
            log("agy stderr: " + stderr_sample)
        raise RuntimeError(f"agy 未返回额度分组：{str(detail)[:160]}")

    return data


def _request_agy_live():
    """查询实时额度，仅对瞬时失败做有限重试。"""
    cmd = _agy_command()
    if not cmd:
        raise RuntimeError("agy CLI not found")
    last_error = None
    for attempt in range(AGY_LIVE_RETRY_ATTEMPTS):
        try:
            return _request_agy_live_once(cmd)
        except RuntimeError as e:
            last_error = e
            transient = any(text in str(e) for text in (
                "超时", "无法启动", "非零退出"))
            if not transient or attempt + 1 >= AGY_LIVE_RETRY_ATTEMPTS:
                raise
            time.sleep(AGY_LIVE_RETRY_DELAY_SECONDS)
    raise last_error or RuntimeError("agy live query failed")


def _save_disk_snapshot(data, at):
    """将最近一次成功的 command.data 持久化到 last_snapshot.json。"""
    tmp_file = LAST_SNAPSHOT_FILE + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump({"at": at, "data": data}, f, ensure_ascii=False)
        os.replace(tmp_file, LAST_SNAPSHOT_FILE)
    except Exception as e:
        log("save snapshot: " + repr(e))
        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
        except OSError:
            pass


def _load_disk_snapshot():
    """从 last_snapshot.json 读取上次缓存的 (data, epoch_seconds)。"""
    try:
        with open(LAST_SNAPSHOT_FILE, encoding="utf-8") as f:
            obj = json.load(f)
        if (isinstance(obj, dict) and isinstance(obj.get("data"), dict)
                and isinstance(obj.get("at"), (int, float))):
            return obj["data"], float(obj["at"])
    except Exception:
        pass
    return None, 0.0


def _format_5h_remaining(minutes):
    if minutes >= 1440:
        return f"{minutes // 1440}天{minutes % 1440 // 60}小时"
    if minutes >= 60:
        return f"{minutes // 60}小时{minutes % 60:02d}分"
    return f"{minutes}分钟"


def _format_week_remaining(minutes):
    if minutes >= 1440:
        return f"{minutes // 1440}天{minutes % 1440 // 60}小时"
    if minutes >= 60:
        return f"{minutes // 60}小时{minutes % 60:02d}分"
    return f"{minutes}分钟"


def _parse_bucket(bucket, now_local):
    """解析单个额度 bucket，时间转换到本地时区（UTC+8）。"""
    if not isinstance(bucket, dict):
        return None
    try:
        frac = float(bucket.get("remaining_fraction"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(frac):
        return None
    remaining = round(min(1.0, max(0.0, frac)) * 100)
    reset_raw = bucket.get("reset_time")
    if not reset_raw:
        return None
    try:
        dt_local = datetime.datetime.fromisoformat(
            str(reset_raw).replace("Z", "+00:00")
        ).astimezone()
    except ValueError:
        return None
    mins = max(0, int((dt_local - now_local).total_seconds() // 60))
    return {
        "remaining": remaining,
        "mins": mins,
        "reset_hm": dt_local.strftime("%H:%M"),
        "reset_md_hm": dt_local.strftime("%m-%d %H:%M"),
    }


def _find_group_buckets(group):
    """从组内提取 5h 与 weekly 两个窗口 bucket。"""
    five_raw = None
    week_raw = None
    for b in group.get("buckets") or []:
        if not isinstance(b, dict):
            continue
        win = str(b.get("window") or "").lower()
        bid = str(b.get("id") or "").lower()
        if win == "5h" or bid.endswith("-5h"):
            five_raw = five_raw or b
        elif win == "weekly" or bid.endswith("-weekly"):
            week_raw = week_raw or b
    return five_raw, week_raw


def _data_to_result(data, source, stale_ms, refresh_error=None):
    """把 command.data 换算成 UI 数据结构；返回 (result, source, stale_ms, error)。"""
    try:
        now_local = datetime.datetime.now().astimezone()
        parsed_groups = []
        for group in data.get("groups") or []:
            if not isinstance(group, dict):
                continue
            raw_name = str(group.get("name") or "").strip()
            name = GROUP_NAME_MAP.get(raw_name, raw_name or "Unknown")
            five_raw, week_raw = _find_group_buckets(group)
            five = _parse_bucket(five_raw, now_local)
            week = _parse_bucket(week_raw, now_local)
            primary = five or week
            if not primary:
                continue

            primary_window = "5h" if five else "weekly"
            if five and week:
                header_text = (
                    f"{name}  5h {five['remaining']}%  周 {week['remaining']}%"
                )
            elif five:
                header_text = f"{name}  5h {five['remaining']}%"
            else:
                header_text = f"{name}  周 {week['remaining']}%"

            if primary_window == "5h":
                left = _format_5h_remaining(five["mins"])
                week_left = _format_week_remaining(week["mins"]) if week else None
                reset_text = (
                    f"重置 {five['reset_hm']}（剩 {left}）· 周剩 {week_left}"
                    if week_left else f"重置 {five['reset_hm']}（剩 {left}）"
                )
            else:
                left = _format_week_remaining(week["mins"])
                week_left = left
                reset_text = f"周重置 {week['reset_md_hm']}（剩 {left}）"

            parsed_groups.append({
                "name": name,
                "raw_name": raw_name,
                "primary_window": primary_window,
                "remaining": primary["remaining"],
                "five_remaining": five["remaining"] if five else None,
                "week_remaining": week["remaining"] if week else None,
                "reset_hm": primary["reset_hm"],
                "left": left,
                "week_left": week_left,
                "header_text": header_text,
                "reset_text": reset_text,
            })

        if not parsed_groups:
            return None, source, stale_ms, "未获取到有效的 AGY 额度窗口"

        return ({
            "groups": parsed_groups,
            "source": source or "live",
            "stale_ms": int(stale_ms) if stale_ms is not None else 0,
            "refresh_error": refresh_error,
        }, None, None, None)
    except Exception as e:
        log("parse data: " + repr(e))
        return None, source, stale_ms, repr(e)


_live_lock = threading.Lock()
_live_cache = {
    "snapshot": None,
    "at": 0.0,
    "error": None,
    "error_at": 0.0,
    "retry_seconds": AGY_LIVE_RETRY_SECONDS,
}


def fetch(force=False):
    """查询 AGY 额度；此函数只在后台线程中运行。"""
    now = time.time()
    with _live_lock:
        snapshot = _live_cache["snapshot"]
        cached_at = _live_cache["at"]
        error, error_at = _live_cache["error"], _live_cache["error_at"]
        retry_seconds = _live_cache.get("retry_seconds", AGY_LIVE_RETRY_SECONDS)
    age = now - cached_at

    def cached_result(refresh_error=None):
        return _data_to_result(
            snapshot, "cache", max(0.0, time.time() - cached_at) * 1000,
            refresh_error=refresh_error,
        )

    def disk_fallback(refresh_error=None):
        disk_data, disk_at = _load_disk_snapshot()
        if disk_data is not None:
            stale_sec = max(0.0, time.time() - disk_at)
            if stale_sec <= AGY_LIVE_MAX_STALE_SECONDS:
                return _data_to_result(
                    disk_data, "disk", stale_sec * 1000,
                    refresh_error=refresh_error,
                )
        return None, "disk", None, (error or refresh_error or "AGY 不可用")

    if snapshot and age < AGY_LIVE_TTL_SECONDS and not force:
        return cached_result("上次实时刷新失败" if error else None)

    if error and not force and now - error_at < retry_seconds:
        if snapshot and age < AGY_LIVE_MAX_STALE_SECONDS:
            return cached_result("上次实时刷新失败")
        return disk_fallback("上次实时刷新失败")

    try:
        data = _request_agy_live()
    except Exception as e:
        log("live query: " + repr(e))
        with _live_lock:
            _live_cache["error"] = repr(e)
            _live_cache["error_at"] = time.time()
            previous_retry = _live_cache.get(
                "retry_seconds", AGY_LIVE_RETRY_SECONDS
            )
            _live_cache["retry_seconds"] = min(
                AGY_LIVE_MAX_RETRY_SECONDS,
                max(AGY_LIVE_RETRY_SECONDS, previous_retry * 2),
            )
        if snapshot and time.time() - cached_at < AGY_LIVE_MAX_STALE_SECONDS:
            return cached_result("本次实时刷新失败")
        res = disk_fallback("本次实时刷新失败")
        if res[0] is not None:
            return res
        return None, "live", None, repr(e)

    fetched_at = time.time()
    with _live_lock:
        _live_cache["snapshot"] = data
        _live_cache["at"] = fetched_at
        _live_cache["error"] = None
        _live_cache["error_at"] = 0.0
        _live_cache["retry_seconds"] = AGY_LIVE_RETRY_SECONDS
    _save_disk_snapshot(data, fetched_at)
    return _data_to_result(data, "live", 0.0)


def color(remaining):
    return GREEN if remaining > 50 else (AMBER if remaining > 20 else RED)


class TrayIcon:
    """Small native Windows notification-area icon; no third-party dependency."""

    WM_APP = 0x8000
    WM_CLOSE = 0x0010
    WM_DESTROY = 0x0002
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_CONTEXTMENU = 0x007B
    EVENT_SYSTEM_FOREGROUND = 0x0003
    EVENT_SYSTEM_MOVESIZEEND = 0x000B
    EVENT_SYSTEM_SWITCHEND = 0x0015
    EVENT_SYSTEM_MINIMIZEEND = 0x0017
    EVENT_SYSTEM_DESKTOPSWITCH = 0x0020
    EVENT_OBJECT_REORDER = 0x8004
    WINEVENT_OUTOFCONTEXT = 0x0000
    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004

    MF_SEPARATOR = 0x00000800
    TPM_RIGHTBUTTON = 0x0002
    TPM_RETURNCMD = 0x0100
    CMD_SHOW = 1001
    CMD_HIDE = 1002
    CMD_REFRESH = 1003
    CMD_QUIT = 1004
    CMD_RESET_POSITION = 1005

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeout", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HANDLE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", wintypes.BOOL),
            ("xHotspot", wintypes.DWORD),
            ("yHotspot", wintypes.DWORD),
            ("hbmMask", wintypes.HBITMAP),
            ("hbmColor", wintypes.HBITMAP),
        ]

    def __init__(self, events, watched_hwnd, watched_child_hwnd=None):
        self.events = events
        self.watched_hwnd = watched_hwnd
        self.watched_child_hwnd = watched_child_hwnd
        self.hwnd = None
        self.stopping = False
        self.class_name = f"AgyMonitorTray_{os.getpid()}"
        self.hicon = None
        self.event_hooks = []
        self._topmost_pending = threading.Event()
        self._window_api_ready = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _ensure_window_api(self, user32):
        if self._window_api_ready:
            return
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        self._window_api_ready = True

    def _queue_topmost(self):
        if self._topmost_pending.is_set():
            return
        self._topmost_pending.set()
        self.events.put_nowait("topmost")

    def consume_topmost_event(self):
        self._topmost_pending.clear()

    @staticmethod
    def _make_icon(user32, gdi32, hinstance):
        """Create a purple circular tray badge (#7c3aed) with a white 'A' glyph."""
        width = height = 32
        color_buf_bytes = bytearray(width * height * 4)
        mask_stride = ((width + 15) // 16) * 2
        mask = bytearray(mask_stride * height)
        for y in range(height):
            for x in range(width):
                dx, dy = x - 15.5, y - 15.5
                inside = dx * dx + dy * dy <= 14.5 * 14.5
                if inside:
                    in_a = False
                    if 7 <= y <= 24:
                        span = (y - 7) * (6.5 / 17.0)
                        left_cx = 15.5 - span
                        right_cx = 15.5 + span
                        if (abs(x - left_cx) <= 1.7 or
                                abs(x - right_cx) <= 1.7 or
                                (17 <= y <= 19 and abs(x - 15.5) <= span)):
                            in_a = True
                    # BGRA byte order for Windows 32-bit DIB; #7c3aed -> (237, 58, 124)
                    b, g, r, a = (255, 255, 255, 255) if in_a else (237, 58, 124, 255)
                    pos = ((height - 1 - y) * width + x) * 4
                    color_buf_bytes[pos:pos + 4] = bytes((b, g, r, a))
                else:
                    mask[(height - 1 - y) * mask_stride + (x // 8)] |= 1 << (7 - (x % 8))
        color_buf = ctypes.create_string_buffer(bytes(color_buf_bytes))
        mask_buf = ctypes.create_string_buffer(bytes(mask))
        hbm_color = gdi32.CreateBitmap(width, height, 1, 32, color_buf)
        hbm_mask = gdi32.CreateBitmap(width, height, 1, 1, mask_buf)
        if not hbm_color or not hbm_mask:
            return user32.LoadIconW(None, ctypes.c_void_p(32512))
        info = TrayIcon.ICONINFO(True, 0, 0, hbm_mask, hbm_color)
        icon = user32.CreateIconIndirect(ctypes.byref(info))
        gdi32.DeleteObject(hbm_color)
        gdi32.DeleteObject(hbm_mask)
        return icon or user32.LoadIconW(None, ctypes.c_void_p(32512))

    def _run(self):
        try:
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            kernel32 = ctypes.windll.kernel32
            gdi32 = ctypes.windll.gdi32
            callback_message = self.WM_APP + 1
            self._ensure_window_api(user32)

            user32.CreatePopupMenu.restype = wintypes.HMENU
            user32.AppendMenuW.argtypes = [
                wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
            user32.AppendMenuW.restype = wintypes.BOOL
            user32.TrackPopupMenu.argtypes = [
                wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
            user32.TrackPopupMenu.restype = ctypes.c_int
            user32.DestroyMenu.argtypes = [wintypes.HMENU]
            user32.DestroyMenu.restype = wintypes.BOOL

            wnd_proc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                ctypes.c_size_t, ctypes.c_ssize_t,
            )

            @wnd_proc_type
            def wnd_proc(hwnd, msg, wparam, lparam):
                if msg == callback_message:
                    lparam = int(lparam)
                    if lparam in (self.WM_LBUTTONUP, self.WM_LBUTTONDBLCLK):
                        self.events.put("show")
                    elif lparam in (self.WM_RBUTTONUP, self.WM_CONTEXTMENU):
                        self._popup_menu(user32)
                    return 0
                if msg == self.WM_DESTROY:
                    self._delete_icon(shell32, hwnd)
                    user32.PostQuitMessage(0)
                    return 0
                user32.DefWindowProcW.argtypes = [
                    wintypes.HWND, wintypes.UINT,
                    ctypes.c_size_t, ctypes.c_ssize_t,
                ]
                user32.DefWindowProcW.restype = ctypes.c_ssize_t
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            win_event_type = ctypes.WINFUNCTYPE(
                None, wintypes.HANDLE, wintypes.DWORD,
                wintypes.HWND, wintypes.LONG, wintypes.LONG,
                wintypes.DWORD, wintypes.DWORD,
            )

            @win_event_type
            def win_event(_hook, event, _hwnd, _object_id, _child_id, _thread_id, _time):
                watched = self._is_watched_window(user32, _hwnd)
                if event in (
                        self.EVENT_SYSTEM_FOREGROUND,
                        self.EVENT_SYSTEM_MOVESIZEEND,
                        self.EVENT_SYSTEM_SWITCHEND,
                        self.EVENT_SYSTEM_MINIMIZEEND,
                        self.EVENT_SYSTEM_DESKTOPSWITCH):
                    self._queue_topmost()
                elif (event == self.EVENT_OBJECT_REORDER and
                      _object_id in (0, -4) and _child_id == 0 and
                      (watched or self._window_covers(user32, _hwnd))):
                    self._queue_topmost()

            self._wnd_proc = wnd_proc
            self._win_event = win_event
            hinstance = kernel32.GetModuleHandleW(None)
            wc = self.WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(wnd_proc, ctypes.c_void_p)
            wc.hInstance = hinstance
            wc.lpszClassName = self.class_name
            user32.RegisterClassW(ctypes.byref(wc))
            hwnd = user32.CreateWindowExW(
                0, self.class_name, "AgyMonitor Tray", 0,
                0, 0, 0, 0, None, None, hinstance, None,
            )
            if not hwnd:
                raise ctypes.WinError()
            self.hwnd = hwnd
            self.hicon = self._make_icon(user32, gdi32, hinstance)
            for event_id in (
                    self.EVENT_SYSTEM_FOREGROUND,
                    self.EVENT_SYSTEM_MOVESIZEEND,
                    self.EVENT_SYSTEM_SWITCHEND,
                    self.EVENT_SYSTEM_MINIMIZEEND,
                    self.EVENT_SYSTEM_DESKTOPSWITCH,
                    self.EVENT_OBJECT_REORDER):
                hook = user32.SetWinEventHook(
                    event_id, event_id, None, self._win_event,
                    0, 0, self.WINEVENT_OUTOFCONTEXT)
                if hook:
                    self.event_hooks.append(hook)
            if not self.event_hooks:
                raise ctypes.WinError()
            nid = self.NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
            nid.uCallbackMessage = callback_message
            nid.hIcon = self.hicon
            nid.szTip = "AGY 额度浮窗"
            if not shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid)):
                raise ctypes.WinError()

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log("tray: " + repr(e))
        finally:
            for hook in self.event_hooks:
                try:
                    ctypes.windll.user32.UnhookWinEvent(hook)
                except Exception:
                    pass
            if self.hicon:
                try:
                    ctypes.windll.user32.DestroyIcon(self.hicon)
                except Exception:
                    pass
            self.hwnd = None
            self.event_hooks = []
            self.hicon = None

    def _popup_menu(self, user32):
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        user32.AppendMenuW(hmenu, 0, self.CMD_SHOW, "显示浮窗")
        user32.AppendMenuW(hmenu, 0, self.CMD_HIDE, "隐藏浮窗")
        user32.AppendMenuW(hmenu, self.MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, 0, self.CMD_RESET_POSITION, "重置位置")
        user32.AppendMenuW(hmenu, self.MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, 0, self.CMD_REFRESH, "强制刷新")
        user32.AppendMenuW(hmenu, self.MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, 0, self.CMD_QUIT, "退出")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(
            hmenu, self.TPM_RIGHTBUTTON | self.TPM_RETURNCMD,
            pt.x, pt.y, 0, self.hwnd, None,
        )
        user32.DestroyMenu(hmenu)
        mapping = {
            self.CMD_SHOW: "show",
            self.CMD_HIDE: "hide",
            self.CMD_RESET_POSITION: "reset",
            self.CMD_REFRESH: "refresh",
            self.CMD_QUIT: "exit",
        }
        evt = mapping.get(int(cmd))
        if evt:
            self.events.put(evt)

    @staticmethod
    def _rects_overlap(a, b):
        return (a.left < b.right and a.right > b.left and
                a.top < b.bottom and a.bottom > b.top)

    def _is_watched_window(self, user32, hwnd):
        if not hwnd:
            return False
        return int(hwnd) in {
            int(self.watched_hwnd or 0),
            int(self.watched_child_hwnd or 0),
        }

    def _window_covers(self, user32, other):
        self._ensure_window_api(user32)
        if not other or not user32.IsWindowVisible(other):
            return False
        if _is_cloaked(other):
            return False
        rect_type = wintypes.RECT
        root_rect, other_rect = rect_type(), rect_type()
        if not (user32.GetWindowRect(self.watched_hwnd, ctypes.byref(root_rect)) and
                user32.GetWindowRect(other, ctypes.byref(other_rect))):
            return False
        if other_rect.left >= other_rect.right or other_rect.top >= other_rect.bottom:
            return False
        return self._rects_overlap(root_rect, other_rect)

    def _has_covering_window(self, user32):
        self._ensure_window_api(user32)
        root = self.watched_hwnd
        if not root:
            return False
        rect_type = wintypes.RECT
        root_rect = rect_type()
        if not user32.GetWindowRect(root, ctypes.byref(root_rect)):
            return False
        other = user32.GetWindow(root, 3)  # GW_HWNDPREV
        for _ in range(128):
            if not other:
                return False
            if self._window_covers(user32, other):
                return True
            other = user32.GetWindow(other, 3)
        return False

    def _delete_icon(self, shell32, hwnd):
        nid = self.NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = hwnd
        nid.uID = 1
        try:
            shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(nid))
        except Exception:
            pass

    def has_covering_window(self):
        try:
            return self._has_covering_window(ctypes.windll.user32)
        except Exception:
            return False

    def stop(self):
        self.stopping = True
        hwnd = self.hwnd
        if hwnd:
            try:
                ctypes.windll.user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)
            except Exception:
                pass


class MiniCard:
    def __init__(self):
        self.tk = tk.Tk()
        self.tk.overrideredirect(True)
        self.tk.attributes("-topmost", True)
        self.tk.attributes("-alpha", OPACITY)
        self.tk.configure(bg=BG)

        self.f1 = (FAMILY, max(8, int(11 * SCALE)), "bold")
        self.f2 = (FAMILY, max(7, int(9 * SCALE)))
        self.pad = int(8 * SCALE)
        self.barw = int(175 * SCALE)
        self.barh = int(5 * SCALE)

        self.groups_frame = tk.Frame(self.tk, bg=BG)
        self.groups_frame.pack(fill="x")
        self.group_blocks = []

        self.placeholder_title = tk.Label(
            self.groups_frame, text="AGY 额度 …", fg=FG, bg=BG, font=self.f1
        )
        self.placeholder_title.pack(
            anchor="w", padx=self.pad, pady=(int(4 * SCALE), 0)
        )
        self.placeholder_sub = tk.Label(
            self.groups_frame, text="正在查询 AGY 额度", fg=MUT, bg=BG, font=self.f2
        )
        self.placeholder_sub.pack(
            anchor="w", padx=self.pad, pady=(int(2 * SCALE), 0)
        )

        self.status = tk.Label(
            self.tk, text="AGY · 更新中…", fg=MUT, bg=BG, font=self.f2
        )
        self.status.pack(
            anchor="w", padx=self.pad, pady=(int(2 * SCALE), int(4 * SCALE))
        )

        self.last_data = None
        self.fetching = False
        self.pending_force = False
        self.results = queue.Queue()
        self.tray_events = queue.Queue()
        self.refresh_after = None
        self.topmost_repair_after = None
        self.topmost_watchdog_after = None
        self.closing = False
        self._dx = 0
        self._dy = 0

        self.tk.bind("<Button-1>", self._press)
        self.tk.bind("<B1-Motion>", self._drag)
        self.tk.bind("<ButtonRelease-1>", self._release)
        self.tk.bind("<Button-3>", self._menu)

        self.menu = tk.Menu(self.tk, tearoff=0)
        self.menu.add_command(label="显示浮窗", command=self._show)
        self.menu.add_command(label="隐藏浮窗", command=self._hide)
        self.menu.add_separator()
        self.menu.add_command(label="重置位置", command=self._reset_position)
        self.menu.add_separator()
        self.menu.add_command(label="强制刷新", command=lambda: self.tick(force=True))
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self._quit)

        self._load_initial_snapshot()
        x, y = self._load_pos()
        self.tk.geometry(f"+{x}+{y}")
        self.hwnd = _top_level_hwnd(self.tk)
        self.tray = TrayIcon(
            self.tray_events, self.hwnd, int(self.tk.winfo_id())
        )
        self.tick()
        self.tk.after(100, self._drain_results)
        self.tk.after(100, self._drain_tray_events)
        self._arm_topmost_watchdog()

    def _load_initial_snapshot(self):
        """启动时立即渲染上次持久化的本地缓存（若有），随后后台发起实时查询。"""
        disk_data, disk_at = _load_disk_snapshot()
        if disk_data is None:
            return
        stale_ms = max(0.0, (time.time() - disk_at) * 1000)
        result, _src, _stale, _err = _data_to_result(disk_data, "disk", stale_ms)
        if result is not None:
            self._render(result)

    def _ensure_group_blocks(self, count):
        """仅当分组数变化时重建组件，避免日常刷新闪烁。"""
        if len(self.group_blocks) == count:
            return
        for w in self.groups_frame.winfo_children():
            w.destroy()
        self.group_blocks = []
        for idx in range(count):
            if idx > 0:
                div = tk.Frame(self.groups_frame, bg=DIVIDER, height=1)
                div.pack(
                    fill="x",
                    padx=self.pad,
                    pady=(int(4 * SCALE), int(2 * SCALE)),
                )
            top_pad = int(4 * SCALE) if idx == 0 else 0
            header = tk.Label(self.groups_frame, text="", fg=FG, bg=BG, font=self.f1)
            header.pack(anchor="w", padx=self.pad, pady=(top_pad, 0))
            bar = tk.Canvas(
                self.groups_frame,
                width=self.barw,
                height=self.barh,
                bg=BG,
                highlightthickness=0,
            )
            bar.pack(anchor="w", padx=self.pad, pady=int(2 * SCALE))
            reset = tk.Label(self.groups_frame, text="", fg=MUT, bg=BG, font=self.f2)
            reset.pack(anchor="w", padx=self.pad, pady=(0, 0))
            self.group_blocks.append({
                "header": header,
                "bar": bar,
                "reset": reset,
            })

    def _load_pos(self):
        try:
            with open(POS_FILE, encoding="utf-8") as f:
                p = json.load(f)
        except Exception:
            p = {}
        self.tk.update_idletasks()
        width = max(210, self.tk.winfo_reqwidth())
        height = max(110, self.tk.winfo_reqheight())
        try:
            user32 = ctypes.windll.user32
            left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
            top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
            screen_w = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
            screen_h = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        except Exception:
            left, top = 0, 0
            screen_w = self.tk.winfo_screenwidth()
            screen_h = self.tk.winfo_screenheight()
        right = left + max(screen_w, width + 20)
        bottom = top + max(screen_h, height + 20)
        default_x = right - width - 20
        default_y = max(top + 10, bottom - height - 160)
        x = int(p.get("x", default_x))
        y = int(p.get("y", default_y))
        x = min(max(x, left + 10), max(left + 10, right - width - 10))
        y = min(max(y, top + 10), max(top + 10, bottom - height - 10))
        return x, y

    def _save_pos(self):
        try:
            with open(POS_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": self.tk.winfo_x(), "y": self.tk.winfo_y()}, f)
        except Exception:
            pass

    def _press(self, e):
        self._dx = e.x_root - self.tk.winfo_x()
        self._dy = e.y_root - self.tk.winfo_y()

    def _drag(self, e):
        self.tk.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _release(self, e):
        self._save_pos()

    def _menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _quit(self):
        self.closing = True
        for attr in ("topmost_repair_after", "topmost_watchdog_after", "refresh_after"):
            handle = getattr(self, attr, None)
            if handle is not None:
                try:
                    self.tk.after_cancel(handle)
                except Exception:
                    pass
                setattr(self, attr, None)
        try:
            self.tray.stop()
        except Exception:
            pass
        self._save_pos()
        self.tk.destroy()

    def _show(self):
        self.tk.deiconify()
        self.tk.attributes("-alpha", OPACITY)
        self._clamp_current_position()
        self._enforce_topmost()

    def _hide(self):
        self.tk.withdraw()

    def _drain_tray_events(self):
        try:
            while True:
                event = self.tray_events.get_nowait()
                if event == "show":
                    self._show()
                elif event == "hide":
                    self._hide()
                elif event == "reset":
                    self._reset_position()
                elif event == "refresh":
                    self.tick(force=True)
                elif event == "exit":
                    self._quit()
                elif event == "topmost":
                    self.tray.consume_topmost_event()
                    self._schedule_topmost_repair()
        except queue.Empty:
            pass
        if not self.closing and self.tk.winfo_exists():
            self.tk.after(100, self._drain_tray_events)

    def _schedule_topmost_repair(self):
        if self.closing or self.topmost_repair_after is not None:
            return
        self.topmost_repair_after = self.tk.after(100, self._repair_topmost)

    def _repair_topmost(self):
        self.topmost_repair_after = None
        self._enforce_topmost()

    def _arm_topmost_watchdog(self):
        if self.closing:
            return
        self.topmost_watchdog_after = self.tk.after(
            TOPMOST_WATCHDOG_SECONDS * 1000, self._topmost_watchdog
        )

    def _topmost_watchdog(self):
        self.topmost_watchdog_after = None
        self._schedule_topmost_repair()
        self._arm_topmost_watchdog()

    def _clamp_current_position(self):
        x, y = self._load_pos()
        self.tk.geometry(f"+{x}+{y}")

    def _reset_position(self):
        """将浮窗恢复到屏幕右下角（避开 CodexMini 默认区域）。"""
        try:
            user32 = ctypes.windll.user32
            rect = wintypes.RECT()
            if not user32.GetWindowRect(self.hwnd, ctypes.byref(rect)):
                return
            left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
            top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
            right = left + user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
            bottom = top + user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
            width = max(1, rect.right - rect.left)
            height = max(1, rect.bottom - rect.top)
            x = max(left + 10, right - width - 20)
            y = max(top + 10, bottom - height - 160)
            self.tk.deiconify()
            self.tk.geometry(f"+{x}+{y}")
            self.tk.attributes("-alpha", OPACITY)
            self._save_pos()
            self._enforce_topmost()
        except Exception as e:
            log("reset position: " + repr(e))

    def _enforce_topmost(self):
        if self.closing:
            return
        try:
            if self.tk.state() == "withdrawn":
                return
            hwnd = self.hwnd
            user32 = ctypes.windll.user32
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user32.SetWindowPos.restype = wintypes.BOOL
            ex_style = user32.GetWindowLongPtrW(hwnd, -20)
            covered = self.tray.has_covering_window()
            banded = bool(int(ex_style) & 0x00000008)  # WS_EX_TOPMOST
            if banded and not covered:
                return
            self.tk.attributes("-topmost", True)
            flags = 0x0001 | 0x0002 | 0x0010 | 0x0040  # NOSIZE|NOMOVE|NOACTIVATE|SHOWWINDOW
            if not user32.SetWindowPos(
                    hwnd, wintypes.HWND(-1), 0, 0, 0, 0, flags):
                raise ctypes.WinError()
            log("topmost: re-applied (" +
                ("style-lost" if not banded else "covered") + ")")
        except Exception as e:
            log("topmost: " + repr(e))

    @staticmethod
    def _age_text(stale_ms, source):
        if source == "disk":
            label = "本地缓存"
        elif source == "live":
            label = "已更新"
        else:
            label = "缓存"
        if not isinstance(stale_ms, (int, float)):
            return label
        mins = max(0, int(stale_ms // 60_000))
        if mins == 0:
            return f"{label} 刚刚"
        if mins < 60:
            return f"{label} {mins}分钟前"
        if mins < 1440:
            return f"{label} {mins // 60}小时前"
        return f"{label} {mins // 1440}天前"

    def _schedule_refresh(self):
        if self.refresh_after is None:
            self.refresh_after = self.tk.after(REFRESH_SECONDS * 1000, self._loop)

    def _start_fetch(self, force):
        if self.fetching:
            self.pending_force = self.pending_force or force
            return
        self.fetching = True
        threading.Thread(
            target=lambda: self.results.put(fetch(force)), daemon=True
        ).start()

    def tick(self, force=False):
        """启动一次后台读取；不会重复创建轮询链。"""
        self._start_fetch(force)
        self._schedule_refresh()
        if self.last_data:
            self.status.config(
                text="AGY · 更新中… " + self._age_text(
                    self.last_data["stale_ms"], self.last_data["source"]
                ),
                fg=MUT,
            )
        else:
            if not self.group_blocks:
                self.placeholder_title.config(text="AGY 额度 更新中…", fg=MUT)
                self.placeholder_sub.config(text="正在查询 AGY 额度", fg=MUT)
            self.status.config(text="AGY · 更新中…", fg=MUT)

    def _render(self, data):
        self.last_data = data
        groups = data["groups"]
        self._ensure_group_blocks(len(groups))
        for block, grp in zip(self.group_blocks, groups):
            rem = grp["remaining"]
            c = color(rem)
            block["header"].config(text=grp["header_text"], fg=c)
            block["reset"].config(text=grp["reset_text"], fg=MUT)
            bar = block["bar"]
            bar.delete("all")
            bar.create_rectangle(0, 0, self.barw, self.barh, fill=TRACK, width=0)
            bar.create_rectangle(
                0, 0, max(2, int(self.barw * rem / 100)), self.barh,
                fill=c, width=0,
            )

        refresh_error = data.get("refresh_error")
        age = self._age_text(data["stale_ms"], data["source"])
        if refresh_error:
            self.status.config(text=f"AGY · 更新失败 · {age}", fg=AMBER)
        else:
            self.status.config(text=f"AGY · {age}", fg=MUT)

    def _render_error(self, source, stale_ms, error):
        if self.last_data:
            age = self._age_text(
                self.last_data["stale_ms"], self.last_data["source"]
            )
            self.status.config(text=f"AGY · 更新失败 · {age}", fg=AMBER)
            return
        if error and "超时" in str(error):
            message = "AGY 查询超时"
        elif error and "not found" in str(error).lower():
            message = "未找到 agy CLI"
        else:
            message = "AGY 不可用"
        if not self.group_blocks:
            self.placeholder_title.config(text="AGY 额度 —", fg=MUT)
            self.placeholder_sub.config(text=message, fg=MUT)
        self.status.config(text=f"AGY · {message}", fg=AMBER)

    def _drain_results(self):
        try:
            while True:
                data, source, stale_ms, error = self.results.get_nowait()
                self.fetching = False
                if data is not None:
                    self._render(data)
                else:
                    self._render_error(source, stale_ms, error)
                if self.pending_force:
                    self.pending_force = False
                    self._start_fetch(True)
        except queue.Empty:
            pass
        if self.tk.winfo_exists():
            self.tk.after(100, self._drain_results)

    def _loop(self):
        self.refresh_after = None
        try:
            self.tick()
        except Exception as e:
            log("loop: " + repr(e))
            self._schedule_refresh()

    def run(self):
        self.tk.mainloop()


if __name__ == "__main__":
    try:
        MiniCard().run()
    except Exception as e:
        log("fatal: " + repr(e))
        sys.exit(1)
