"""Thin, typed Win32 bindings used by the desktop tools."""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass

import psutil

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Without this, a scaled display reports virtualized coordinates and windows land in the wrong place.
user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2; refused harmlessly if already set

HWND = wintypes.HWND
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [HWND]
user32.IsWindow.argtypes = [HWND]
user32.IsIconic.argtypes = [HWND]
user32.IsZoomed.argtypes = [HWND]
user32.GetWindowTextLengthW.argtypes = [HWND]
user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetForegroundWindow.restype = HWND
user32.SetForegroundWindow.argtypes = [HWND]
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.BringWindowToTop.argtypes = [HWND]
user32.GetWindowLongPtrW.argtypes = [HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindow.argtypes = [HWND, wintypes.UINT]
user32.GetWindow.restype = HWND
user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.MonitorFromWindow.argtypes = [HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.PostMessageW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
dwmapi.DwmGetWindowAttribute.argtypes = [HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
GW_OWNER = 4
GW_HWNDNEXT = 2
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE, SW_SHOW = 6, 3, 9, 5
WM_CLOSE = 0x0010
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
MONITOR_DEFAULTTONEAREST = 2

# Windows Aegis itself owns are never treated as "the current window".
_OWN_PID = os.getpid()


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]


@dataclass
class Window:
    hwnd: int
    title: str
    app: str
    pid: int
    minimized: bool
    maximized: bool
    active: bool
    rect: tuple[int, int, int, int]  # left, top, right, bottom

    def brief(self) -> dict:
        state = "minimized" if self.minimized else "maximized" if self.maximized else "normal"
        return {"title": self.title, "app": self.app, "state": state, "active": self.active}


def _title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _cloaked(hwnd: int) -> bool:
    value = wintypes.DWORD(0)
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    return value.value != 0


def frame_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def _pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


_exe_cache: dict[int, str] = {}


def _exe(pid: int) -> str:
    name = _exe_cache.get(pid)
    if name is None:
        try:
            name = psutil.Process(pid).name()
        except (psutil.Error, OSError):
            name = ""
        if len(_exe_cache) > 2000:
            _exe_cache.clear()
        _exe_cache[pid] = name
    return name


_NOISE = {"Program Manager", "Windows Input Experience", "Windows Shell Experience Host", "Microsoft Text Input Application", "Settings", "Task Switching"}
_NOISE_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow"}


def list_windows(include_own: bool = False) -> list[Window]:
    """Real, switchable top-level windows, front-most first."""
    foreground = user32.GetForegroundWindow()
    found: list[Window] = []

    def callback(hwnd: int, _: int) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER) or _cloaked(hwnd):
                return True
            style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            if style & WS_EX_TOOLWINDOW or style & WS_EX_NOACTIVATE:
                return True
            title = _title(hwnd)
            if not title.strip() or title in _NOISE:
                return True
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, cls, 128)
            if cls.value in _NOISE_CLASSES:
                return True
            pid = _pid(hwnd)
            if pid == _OWN_PID and not include_own:
                return True
            found.append(Window(int(hwnd), title, _exe(pid), pid, bool(user32.IsIconic(hwnd)), bool(user32.IsZoomed(hwnd)), hwnd == foreground, frame_rect(hwnd)))
        except OSError:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def current_window() -> Window | None:
    """The window the user was last working in, ignoring Aegis itself."""
    windows = [window for window in list_windows() if not window.minimized]
    return windows[0] if windows else None


_APP_HINTS = {
    "vscode": "code.exe", "vs code": "code.exe", "visual studio code": "code.exe", "code": "code.exe",
    "chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe", "browser": "chrome.exe",
    "word": "winword.exe", "excel": "excel.exe", "powerpoint": "powerpnt.exe", "outlook": "outlook.exe", "teams": "ms-teams.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe", "files": "explorer.exe", "notepad": "notepad.exe",
    "terminal": "windowsterminal.exe", "windows terminal": "windowsterminal.exe", "spotify": "spotify.exe", "calculator": "calculatorapp.exe",
    "cmd": "cmd.exe", "powershell": "powershell.exe", "paint": "mspaint.exe",
}


def find_window(query: str) -> tuple[Window | None, list[Window]]:
    """Find a window by title or app name. Returns (best, all matches)."""
    query = (query or "").strip().lower()
    query = query.removesuffix(" window").removesuffix(" app").removeprefix("the ").strip()
    windows = list_windows()
    if query in {"", "active", "current", "this", "this window", "foreground", "focused"}:
        window = current_window()
        return window, [window] if window else []
    hint = _APP_HINTS.get(query)
    words = query.split()
    scored: list[tuple[int, Window]] = []
    for position, window in enumerate(windows):
        title, app = window.title.lower(), window.app.lower()
        score = 0
        if hint and app == hint:
            score = 100
        elif query in title:
            score = 80
        elif query in app:
            score = 70
        elif all(word in title or word in app for word in words):
            score = 50
        if score:
            scored.append((score * 100 - position, window))
    scored.sort(key=lambda item: item[0], reverse=True)
    matches = [window for _, window in scored]
    return (matches[0] if matches else None), matches


def focus_window(hwnd: int) -> bool:
    """Bring a window to the front, working around Windows' foreground-lock rules."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    foreground = user32.GetForegroundWindow()
    if foreground == hwnd:
        return True
    this_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    other_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    attached: list[int] = []
    try:
        for thread in {target_thread, other_thread} - {0, this_thread}:
            if user32.AttachThreadInput(this_thread, thread, True):
                attached.append(thread)
        # A synthetic Alt press satisfies the foreground-lock heuristic.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for thread in attached:
            user32.AttachThreadInput(this_thread, thread, False)
    for _ in range(12):
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.05)
    return False


def work_area(hwnd: int) -> tuple[int, int, int, int]:
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    user32.GetMonitorInfoW(monitor, ctypes.byref(info))
    area = info.rcWork
    return area.left, area.top, area.right, area.bottom


def monitor_rect(hwnd: int) -> tuple[int, int, int, int]:
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    user32.GetMonitorInfoW(monitor, ctypes.byref(info))
    area = info.rcMonitor
    return area.left, area.top, area.right, area.bottom


def move_window(hwnd: int, left: int, top: int, right: int, bottom: int) -> tuple[int, int, int, int]:
    """Place the visible frame at a rectangle, compensating for invisible resize borders."""
    if user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.12)
    outer = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(outer))
    frame = frame_rect(hwnd)
    pad_left, pad_top = frame[0] - outer.left, frame[1] - outer.top
    pad_right, pad_bottom = outer.right - frame[2], outer.bottom - frame[3]
    user32.SetWindowPos(hwnd, None, left - pad_left, top - pad_top, (right - left) + pad_left + pad_right, (bottom - top) + pad_top + pad_bottom, SWP_NOZORDER)
    time.sleep(0.1)
    return frame_rect(hwnd)


# ---------------------------------------------------------------------- keyboard input
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_KEYUP, _UNICODE = 0x0002, 0x0004

VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "windows": 0x5B, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "insert": 0x2D, "printscreen": 0x2C,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF, "media_next": 0xB0, "media_prev": 0xB1, "media_stop": 0xB2, "media_play_pause": 0xB3,
    **{f"f{index}": 0x6F + index for index in range(1, 25)},
}


def _send(events: list[_INPUT]) -> None:
    array = (_INPUT * len(events))(*events)
    if user32.SendInput(len(events), array, ctypes.sizeof(_INPUT)) != len(events):
        raise OSError("Windows rejected the keyboard input. An elevated window may be in the foreground.")


def _key(vk: int, up: bool = False) -> _INPUT:
    return _INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(wVk=vk, wScan=0, dwFlags=_KEYUP if up else 0, time=0, dwExtraInfo=0)))


def press(combo: str) -> None:
    """Press a combination such as ctrl+shift+s."""
    keys = []
    for part in combo.lower().replace(" ", "").split("+"):
        if part in VK:
            keys.append(VK[part])
        elif len(part) == 1 and part.isascii():
            keys.append(ord(part.upper()))
        else:
            raise ValueError(f"Unknown key: {part}")
    _send([_key(vk) for vk in keys] + [_key(vk, up=True) for vk in reversed(keys)])


def click(x: int, y: int) -> None:
    """A left click at screen coordinates."""
    user32.SetCursorPos(int(x), int(y))
    _send([_INPUT(type=0, u=_INPUTUNION(mi=_MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flag, time=0, dwExtraInfo=0))) for flag in (0x0002, 0x0004)])


def type_unicode(text: str) -> None:
    """Type literal text into whatever has keyboard focus."""
    events: list[_INPUT] = []
    for char in text.replace("\r\n", "\n"):
        if char == "\n":
            events += [_key(0x0D), _key(0x0D, up=True)]
            continue
        units = char.encode("utf-16-le")
        for index in range(0, len(units), 2):
            scan = int.from_bytes(units[index:index + 2], "little")
            events.append(_INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(wVk=0, wScan=scan, dwFlags=_UNICODE, time=0, dwExtraInfo=0))))
            events.append(_INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(wVk=0, wScan=scan, dwFlags=_UNICODE | _KEYUP, time=0, dwExtraInfo=0))))
    for start in range(0, len(events), 200):
        _send(events[start:start + 200])
        time.sleep(0.01)
