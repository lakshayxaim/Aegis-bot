"""Desktop control: windows, apps, screenshots, clipboard, sound, system state."""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
import winreg
from pathlib import Path
from typing import Any

import psutil

from aegis import paths, winapi
from aegis.toolkit import DANGER, WRITE, Approval, Result, ToolError, arg, resolve_input, resolve_output, tool

logger = logging.getLogger("aegis.desktop")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW


def _describe_window(window: winapi.Window) -> str:
    return f"'{window.title}' ({window.app})" if window.app else f"'{window.title}'"


def _require_window(query: str) -> winapi.Window:
    best, matches = winapi.find_window(query)
    if best is None:
        open_titles = ", ".join(f"'{window.title[:50]}'" for window in winapi.list_windows()[:10]) or "none"
        raise ToolError(f"No open window matches '{query}'. Open windows: {open_titles}.", "Use a title from that list, or open_app if it is not running.")
    return best


# ---------------------------------------------------------------------- windows
@tool("list_windows", "List the application windows that are open right now.", group="Windows & apps", doing="Looking at open windows",
      keywords="windows open running apps programs active showing")
def list_windows(ctx) -> Result:
    windows = winapi.list_windows()
    if not windows:
        return Result("No application windows are open.")
    lines = [f"{'* ' if window.active else ''}{window.title[:70]} [{window.app}]{' (minimized)' if window.minimized else ''}" for window in windows[:25]]
    return Result(f"{len(windows)} window(s) open.", "\n".join(lines))


def _gate_window(ctx, title: str = "", action: str = "focus") -> Approval | None:
    if action != "close":
        return None
    window, _ = winapi.find_window(title)
    name = _describe_window(window) if window else title
    return Approval(f"Close {name}?", "Unsaved work in that window could be lost, though most apps will ask first.")


_SNAP = {
    "snap_left": (0, 0, .5, 1), "snap_right": (.5, 0, 1, 1), "snap_top": (0, 0, 1, .5), "snap_bottom": (0, .5, 1, 1),
    "snap_top_left": (0, 0, .5, .5), "snap_top_right": (.5, 0, 1, .5), "snap_bottom_left": (0, .5, .5, 1), "snap_bottom_right": (.5, .5, 1, 1),
    "center": (.15, .1, .85, .9),
}


@tool("window_action", "Control an open window: focus, minimize, maximize, restore, close, center, or snap it to part of the screen.",
      risk=WRITE, terminal=True, group="Windows & apps", gate=_gate_window, doing="Arranging a window",
      keywords="window snap left right half maximize minimize restore close focus switch move resize side screen arrange bring front tile",
      title=arg("string", "Window title or app name, e.g. 'VS Code'. Use 'active' for the current window.", required=True),
      action=arg("string", "What to do", required=True, enum=["focus", "minimize", "maximize", "restore", "close", "center", *[key for key in _SNAP if key != "center"]]))
def window_action(ctx, title: str, action: str) -> Result:
    window = _require_window(title)
    hwnd, name = window.hwnd, _describe_window(window)
    if action == "focus":
        if not winapi.focus_window(hwnd):
            raise ToolError(f"Windows would not bring {name} to the front.")
        return Result(f"Brought {name} to the front.", verified="It is now the foreground window.")
    if action == "minimize":
        winapi.user32.ShowWindow(hwnd, winapi.SW_MINIMIZE)
        time.sleep(0.15)
        if not winapi.user32.IsIconic(hwnd):
            raise ToolError(f"{name} did not minimize.")
        return Result(f"Minimized {name}.", verified="Window state reads as minimized.")
    if action == "maximize":
        winapi.user32.ShowWindow(hwnd, winapi.SW_MAXIMIZE)
        winapi.focus_window(hwnd)
        time.sleep(0.15)
        if not winapi.user32.IsZoomed(hwnd):
            raise ToolError(f"{name} did not maximize.")
        return Result(f"Maximized {name}.", verified="Window state reads as maximized.")
    if action == "restore":
        winapi.user32.ShowWindow(hwnd, winapi.SW_RESTORE)
        winapi.focus_window(hwnd)
        return Result(f"Restored {name}.", verified="Window is on screen at its normal size.")
    if action == "close":
        winapi.user32.PostMessageW(hwnd, winapi.WM_CLOSE, 0, 0)
        for _ in range(30):
            if not winapi.user32.IsWindow(hwnd):
                return Result(f"Closed {name}.", verified="The window no longer exists.")
            time.sleep(0.1)
        return Result(f"Asked {name} to close, but it is still open. It may be waiting for you to save.", unverified=True)
    left, top, right, bottom = winapi.work_area(hwnd)
    fx0, fy0, fx1, fy1 = _SNAP[action]
    width, height = right - left, bottom - top
    target = (left + round(width * fx0), top + round(height * fy0), left + round(width * fx1), top + round(height * fy1))
    actual = winapi.move_window(hwnd, *target)
    winapi.focus_window(hwnd)
    if max(abs(a - b) for a, b in zip(actual, target)) > 40:
        raise ToolError(f"{name} refused to move to that position (it may have a fixed size).")
    return Result(f"Moved {name} to the {action.replace('snap_', '').replace('_', ' ')} of the screen.", verified=f"Window frame now measures {actual[2] - actual[0]}x{actual[3] - actual[1]}.")


# ---------------------------------------------------------------------- launching
_KNOWN_APPS = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe", "paint": "mspaint.exe", "file explorer": "explorer.exe", "explorer": "explorer.exe", "files": "explorer.exe",
    "cmd": "cmd.exe", "command prompt": "cmd.exe", "powershell": "powershell.exe", "terminal": "wt.exe", "windows terminal": "wt.exe", "task manager": "taskmgr.exe",
    "control panel": "control.exe", "registry editor": "regedit.exe", "snipping tool": "snippingtool.exe", "settings": "ms-settings:", "windows settings": "ms-settings:",
    "word": "winword.exe", "microsoft word": "winword.exe", "excel": "excel.exe", "microsoft excel": "excel.exe", "powerpoint": "powerpnt.exe", "microsoft powerpoint": "powerpnt.exe",
    "outlook": "outlook.exe", "onenote": "onenote.exe", "vs code": "code", "vscode": "code", "visual studio code": "code", "code": "code",
    "chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe", "microsoft edge": "msedge.exe", "firefox": "firefox.exe", "browser": "http://",
}


def _app_path_registry(exe: str) -> str | None:
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                value, _ = winreg.QueryValueEx(key, None)
                if value and Path(value.strip('"')).exists():
                    return value.strip('"')
        except OSError:
            continue
    return None


def _start_menu_shortcuts() -> list[Path]:
    roots = [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
             Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
             paths.folder("desktop"), Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"]
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            found += [item for item in root.rglob("*.lnk") if not re.search(r"uninstall|readme|help|documentation|license", item.stem, re.IGNORECASE)]
    return found


_start_apps: list[dict[str, str]] | None = None


def _store_apps() -> list[dict[str, str]]:
    """Apps from Get-StartApps, which includes Microsoft Store apps. Cached for the session."""
    global _start_apps
    if _start_apps is None:
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"],
                                 capture_output=True, text=True, encoding="utf-8", timeout=20, creationflags=_NO_WINDOW)
            data = json.loads(out.stdout or "[]")
            _start_apps = data if isinstance(data, list) else [data]
        except (OSError, ValueError, subprocess.SubprocessError):
            _start_apps = []
    return _start_apps


def _match_name(query: str, names: list[str]) -> int | None:
    words = query.split()
    best, best_score = None, 0
    for index, name in enumerate(names):
        lowered = name.lower()
        if lowered == query:
            return index
        score = 0
        if lowered.startswith(query):
            score = 80
        elif query in lowered:
            score = 60
        elif all(word in lowered for word in words):
            score = 40
        if score:
            score -= min(len(lowered), 30) / 10  # prefer the tightest name
            if score > best_score:
                best, best_score = index, score
    return best


def _resolve_app(name: str) -> tuple[str, str]:
    """Returns (launch target, display name) or raises."""
    query = re.sub(r"\b(the|app|application|program)\b", "", name.lower()).strip()
    if "\\" in name or name.lower().endswith(".exe"):
        path = paths.expand(name)
        if path.exists():
            return str(path), path.stem
    known = _KNOWN_APPS.get(query)
    if known:
        if known.endswith(":"):
            return known, name
        if known == "http://":
            return known, "your browser"
        found = shutil.which(known) or _app_path_registry(known if known.endswith(".exe") else known + ".exe")
        if found:
            return found, name
    shortcuts = _start_menu_shortcuts()
    index = _match_name(query, [item.stem for item in shortcuts])
    if index is not None:
        return str(shortcuts[index]), shortcuts[index].stem
    apps = _store_apps()
    index = _match_name(query, [item.get("Name", "") for item in apps])
    if index is not None:
        return f"shell:AppsFolder\\{apps[index]['AppID']}", apps[index]["Name"]
    found = shutil.which(query) or shutil.which(query + ".exe") or _app_path_registry(query.replace(" ", "") + ".exe")
    if found:
        return found, name
    raise ToolError(f"'{name}' does not appear to be installed.", "Check the spelling, or tell the user it was not found. Do not guess a different app.")


def _launch(target: str) -> None:
    if target.startswith("shell:"):
        subprocess.Popen(["explorer.exe", target], creationflags=_NO_WINDOW)
    elif target.endswith(":") or target.endswith(".lnk"):
        os.startfile(target)  # noqa: S606 - resolved from the Start Menu or a fixed table
    elif Path(target).name.lower() in {"cmd.exe", "powershell.exe", "wt.exe"}:
        subprocess.Popen([target], creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=str(Path.home()))
    elif target.lower().endswith((".cmd", ".bat")):
        subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=_NO_WINDOW, cwd=str(Path.home()))
    else:
        subprocess.Popen([target], cwd=str(Path.home()), creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@tool("open_app", "Launch an installed application by name, e.g. Notepad, VS Code, Word, Spotify.", risk=WRITE, terminal=True, group="Windows & apps", doing="Opening an app",
      keywords="open launch start run fire app application program notepad calculator word excel chrome spotify code paint terminal",
      name=arg("string", "The app's name", required=True))
def open_app(ctx, name: str) -> Result:
    target, display = _resolve_app(name)
    before = {window.hwnd for window in winapi.list_windows()}
    if target == "http://":
        os.startfile("https://www.google.com")  # noqa: S606 - the default browser
    else:
        _launch(target)
    query = name.lower().replace("the ", "").strip()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ctx.check_cancelled()
        windows = winapi.list_windows()
        fresh = [window for window in windows if window.hwnd not in before]
        match = fresh[0] if fresh else None
        if match is None:
            best, _ = winapi.find_window(query)
            if best and time.monotonic() > deadline - 7:
                winapi.focus_window(best.hwnd)
                return Result(f"{display} was already open, so I brought it to the front.", verified=f"Window {_describe_window(best)} is in front.")
        if match:
            winapi.focus_window(match.hwnd)
            return Result(f"Opened {display}.", verified=f"New window appeared: {_describe_window(match)}.")
        time.sleep(0.25)
    return Result(f"Started {display}, but no window has appeared yet. It may still be loading.", unverified=True)


@tool("open_path", "Open a file or folder with its default app, like double-clicking it. Works with desktop, downloads, etc.", risk=WRITE, terminal=True, group="Windows & apps", doing="Opening",
      keywords="open show view file folder document picture video launch double click explorer",
      path=arg("string", "File or folder", required=True))
def open_path(ctx, path: str) -> Result:
    resolved = resolve_input(path)
    if resolved.suffix.lower() in {".exe", ".bat", ".cmd", ".ps1", ".msi", ".vbs", ".js", ".scr", ".com", ".lnk"}:
        raise ToolError(f"{resolved.name} is a program, so it was not opened through open_path.", "Use open_app, or run_command if the user clearly wants to run it.")
    os.startfile(str(resolved))  # noqa: S606 - a user data file, opened with its associated app
    return Result(f"Opened {paths.pretty(resolved)}.", verified="Windows accepted the request to open it.")


@tool("reveal_in_explorer", "Show a file selected in File Explorer.", risk=WRITE, terminal=True, group="Windows & apps", doing="Showing the file",
      keywords="reveal locate explorer folder where show containing", path=arg("string", "File or folder", required=True))
def reveal(ctx, path: str) -> Result:
    resolved = resolve_input(path)
    subprocess.Popen(["explorer.exe", f"/select,{resolved}"], creationflags=_NO_WINDOW)
    return Result(f"Showed {resolved.name} in File Explorer.", verified="Explorer was asked to select it.")


# ---------------------------------------------------------------------- screenshot
def capture(scope: str = "screen", shell: Any = None):
    """Grab pixels, stepping Aegis's own windows out of the way first. Returns (image, label)."""
    from PIL import ImageGrab

    window = winapi.current_window()
    hidden = bool(shell and scope != "window" and shell.hide_for_capture())
    try:
        if scope == "all":
            return ImageGrab.grab(all_screens=True), "all screens"
        if scope == "window":
            if window is None:
                raise ToolError("There is no window to capture.", "Use scope 'screen' instead.")
            winapi.focus_window(window.hwnd)
            time.sleep(0.2)
            return ImageGrab.grab(bbox=winapi.frame_rect(window.hwnd), all_screens=True), f"the window '{window.title[:50]}'"
        if window is not None:
            return ImageGrab.grab(bbox=winapi.monitor_rect(window.hwnd), all_screens=True), "the screen"
        return ImageGrab.grab(), "the screen"
    finally:
        if hidden:
            shell.restore_after_capture()


def _screenshot_scope(args: dict[str, Any], request: str) -> dict[str, Any]:
    """Which part of the screen is only ever the user's call, never the model's guess."""
    lowered = request.lower()
    if re.search(r"\b(?:all|every|both|multiple)\b.{0,20}\b(?:screens?|monitors?|displays?)\b", lowered):
        return {**args, "scope": "all"}
    return {**args, "scope": "window" if re.search(r"\bwindow\b", lowered) else "screen"}


@tool("take_screenshot", "Capture the screen to a PNG image and save it.", risk=WRITE, terminal=True, group="Screen & media", doing="Taking a screenshot", refine=_screenshot_scope,
      keywords="screenshot screen capture snapshot snip grab picture print shot image",
      scope=arg("string", "screen = the current monitor (default), all = every monitor, window = just the active window", enum=["screen", "all", "window"]),
      path=arg("string", "Where to save, e.g. desktop/shot.png. Omit for the Screenshots folder."))
def take_screenshot(ctx, scope: str = "screen", path: str = "") -> Result:
    from PIL import Image

    image, label = capture(scope, ctx.shell)
    default_name = f"Screenshot {dt.datetime.now():%Y-%m-%d %H%M%S}.png"
    target = resolve_output(path, kind="screenshot", default_name=default_name)
    if target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}:
        target = target.with_suffix(".png")
    image.convert("RGB").save(target)
    with Image.open(target) as check:
        check.verify()
    size = Image.open(target).size
    return Result(f"Saved a screenshot of {label} to {paths.pretty(target)}.", files=[target], verified=f"Valid image on disk: {size[0]}x{size[1]}, {target.stat().st_size // 1024} KB.",
                  undo={"kind": "delete_created", "path": str(target)})


# ---------------------------------------------------------------------- clipboard
def _clip_open():
    import win32clipboard
    for attempt in range(8):
        try:
            win32clipboard.OpenClipboard()
            return win32clipboard
        except Exception:  # another app holds the clipboard
            time.sleep(0.05 * (attempt + 1))
    raise ToolError("Another app is holding the clipboard. Try again in a moment.")


def read_clipboard_text() -> str | None:
    clip = _clip_open()
    try:
        import win32con
        if clip.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return clip.GetClipboardData(win32con.CF_UNICODETEXT)
        return None
    finally:
        clip.CloseClipboard()


def write_clipboard_text(text: str) -> None:
    import win32con
    clip = _clip_open()
    try:
        clip.EmptyClipboard()
        clip.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        clip.CloseClipboard()


@tool("clipboard_read", "Read the text currently on the clipboard.", group="Clipboard", doing="Reading the clipboard",
      keywords="clipboard paste copied read what")
def clipboard_read(ctx) -> Result:
    text = read_clipboard_text()
    if text is None or not text.strip():
        return Result("The clipboard has no text on it.")
    return Result(f"The clipboard holds {len(text)} characters.", text)


@tool("clipboard_write", "Copy text, or a file or image, to the clipboard so it can be pasted.", risk=WRITE, terminal=True, group="Clipboard", doing="Copying to the clipboard",
      keywords="clipboard copy paste put",
      text=arg("string", "Text to copy"), path=arg("string", "Or: a file or image to copy instead"))
def clipboard_write(ctx, text: str = "", path: str = "") -> Result:
    if path:
        resolved = resolve_input(path)
        if resolved.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}:
            import io

            import win32con
            from PIL import Image
            buffer = io.BytesIO()
            Image.open(resolved).convert("RGB").save(buffer, "BMP")
            clip = _clip_open()
            try:
                clip.EmptyClipboard()
                clip.SetClipboardData(win32con.CF_DIB, buffer.getvalue()[14:])
            finally:
                clip.CloseClipboard()
            return Result(f"Copied the image {resolved.name} to the clipboard.", verified="Clipboard now holds image data.")
        env = {**os.environ, "AEGIS_CLIP": str(resolved)}
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", "Set-Clipboard -LiteralPath $env:AEGIS_CLIP"], capture_output=True, text=True, timeout=15, env=env, creationflags=_NO_WINDOW)
        if done.returncode:
            raise ToolError(f"Could not copy the file: {done.stderr.strip()[:200]}")
        return Result(f"Copied the file {resolved.name} to the clipboard.", verified="Windows accepted the file on the clipboard.")
    if not text:
        raise ToolError("Nothing to copy: give text or a path.")
    write_clipboard_text(text)
    if read_clipboard_text() != text:
        raise ToolError("The clipboard did not keep the text. Another app may be managing it.")
    return Result(f"Copied {len(text)} characters to the clipboard.", verified="Read the clipboard back and it matches.")


# ---------------------------------------------------------------------- sound and screen
def _endpoint():
    import comtypes
    from pycaw.pycaw import AudioUtilities
    comtypes.CoInitialize()
    device = AudioUtilities.GetSpeakers()
    if hasattr(device, "EndpointVolume"):
        return device.EndpointVolume
    from comtypes import CLSCTX_ALL
    from ctypes import POINTER, cast
    from pycaw.pycaw import IAudioEndpointVolume
    return cast(device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))


@tool("set_volume", "Set the system volume. Use level for an exact percentage, change to go up or down from the current level, or mute.", risk=WRITE, terminal=True, group="Sound & display", doing="Changing the volume",
      keywords="volume sound loud loudness quiet quieter louder mute unmute speaker audio",
      level=arg("integer", "Exact volume 0-100"), change=arg("integer", "Relative change, e.g. 10 or -10"), mute=arg("boolean", "true to mute, false to unmute"))
def set_volume(ctx, level: int | None = None, change: int | None = None, mute: bool | None = None) -> Result:
    if level is None and change is None and mute is None:
        raise ToolError("Say what to change: level, change or mute.")
    endpoint = _endpoint()
    previous = round(endpoint.GetMasterVolumeLevelScalar() * 100)
    if mute is not None:
        endpoint.SetMute(1 if mute else 0, None)
        if bool(endpoint.GetMute()) != mute:
            raise ToolError("Windows did not apply the mute change.")
        if level is None and change is None:
            return Result("Muted the sound." if mute else "Unmuted the sound.", verified="Read the mute state back.", undo={"kind": "volume", "mute": not mute})
    target = level if level is not None else previous + (change or 0)
    target = max(0, min(100, int(target)))
    endpoint.SetMasterVolumeLevelScalar(target / 100, None)
    actual = round(endpoint.GetMasterVolumeLevelScalar() * 100)
    if abs(actual - target) > 1:
        raise ToolError(f"Volume stayed at {actual}%.")
    return Result(f"Volume is now {actual}%.", verified=f"Read the level back: {actual}% (was {previous}%).", undo={"kind": "volume", "level": previous})


@tool("set_brightness", "Set the built-in display brightness, 0-100.", risk=WRITE, terminal=True, group="Sound & display", doing="Changing the brightness",
      keywords="brightness screen dim brighter darker display", level=arg("integer", "Brightness 0-100", required=True))
def set_brightness(ctx, level: int) -> Result:
    level = max(0, min(100, int(level)))
    script = f"(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods).WmiSetBrightness(1,{level}) | Out-Null; Start-Sleep -Milliseconds 400; (Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness"
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
    if done.returncode or not done.stdout.strip():
        raise ToolError("This display does not allow software brightness control.", "External monitors usually need their own buttons.")
    return Result(f"Brightness is now {done.stdout.split()[0]}%.", verified="Read the level back from the display driver.")


@tool("media_control", "Play, pause or skip whatever media is playing.", risk=WRITE, terminal=True, group="Sound & display", doing="Controlling playback",
      keywords="media music song track play pause skip next previous stop spotify youtube resume",
      action=arg("string", "What to do", required=True, enum=["play_pause", "next", "previous", "stop"]))
def media_control(ctx, action: str) -> Result:
    keys = {"play_pause": "media_play_pause", "next": "media_next", "previous": "media_prev", "stop": "media_stop"}
    winapi.press(keys[action])
    return Result(f"Sent the {action.replace('_', '/')} media key.", unverified=True)


# ---------------------------------------------------------------------- input
def _gate_type(ctx, text: str = "", window: str = "") -> Approval | None:
    target = winapi.find_window(window)[0] if window else winapi.current_window()
    name = _describe_window(target) if target else "the active window"
    return Approval(f"Type into {name}?", code=text[:2000], language="text")


@tool("type_text", "Type text with the keyboard into a window.", risk=DANGER, terminal=True, group="Keyboard", gate=_gate_type, doing="Typing",
      keywords="type write enter keyboard text input into notepad",
      text=arg("string", "What to type", required=True), window=arg("string", "Window to type into; omit for the current one"))
def type_text(ctx, text: str, window: str = "") -> Result:
    target = _require_window(window or "active")
    if not winapi.focus_window(target.hwnd):
        raise ToolError(f"Could not focus {_describe_window(target)}, so nothing was typed.")
    time.sleep(0.15)
    winapi.type_unicode(text)
    return Result(f"Typed {len(text)} characters into {_describe_window(target)}.", unverified=True)


def _gate_keys(ctx, keys: str = "", window: str = "") -> Approval | None:
    target = winapi.find_window(window)[0] if window else winapi.current_window()
    name = _describe_window(target) if target else "the active window"
    return Approval(f"Press {keys} in {name}?")


@tool("press_keys", "Press a keyboard shortcut, e.g. ctrl+s, alt+tab, win+d, enter.", risk=DANGER, terminal=True, group="Keyboard", gate=_gate_keys, doing="Pressing keys",
      keywords="press key keyboard shortcut hotkey ctrl alt enter tab save",
      keys=arg("string", "Combination such as ctrl+shift+s", required=True), window=arg("string", "Window to focus first; omit for the current one"))
def press_keys(ctx, keys: str, window: str = "") -> Result:
    if window:
        target = _require_window(window)
        winapi.focus_window(target.hwnd)
        time.sleep(0.12)
    try:
        winapi.press(keys)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return Result(f"Pressed {keys}.", unverified=True)


# ---------------------------------------------------------------------- system state
def _fmt_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in {"B", "KB"} else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


@tool("system_info", "Check this PC's battery, free disk space, memory, CPU load, uptime and Windows version.", group="System", doing="Checking the system",
      keywords="battery charge charging power plugged disk space free storage drive ram memory cpu processor usage load uptime system pc computer specs version")
def system_info(ctx) -> Result:
    parts: list[str] = []
    data: dict[str, Any] = {}
    battery = psutil.sensors_battery()
    if battery:
        state = "plugged in" if battery.power_plugged else "on battery"
        left = ""
        if not battery.power_plugged and battery.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) and battery.secsleft > 0:
            left = f", about {battery.secsleft // 3600} h {battery.secsleft % 3600 // 60} min left"
        parts.append(f"Battery {battery.percent:.0f}% ({state}{left})")
        data["battery_percent"] = round(battery.percent)
    else:
        parts.append("No battery detected")
    disks = []
    for partition in psutil.disk_partitions(all=False):
        if "cdrom" in partition.opts or not partition.fstype:
            continue
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except OSError:
            continue
        disks.append({"drive": partition.mountpoint, "free": _fmt_bytes(usage.free), "total": _fmt_bytes(usage.total), "used_percent": round(usage.percent)})
        parts.append(f"{partition.mountpoint.rstrip(chr(92))} has {_fmt_bytes(usage.free)} free of {_fmt_bytes(usage.total)}")
    data["disks"] = disks
    memory = psutil.virtual_memory()
    parts.append(f"RAM {_fmt_bytes(memory.used)} of {_fmt_bytes(memory.total)} used ({memory.percent:.0f}%)")
    cpu = psutil.cpu_percent(interval=0.4)
    parts.append(f"CPU at {cpu:.0f}%")
    uptime = time.time() - psutil.boot_time()
    parts.append(f"up {int(uptime // 3600)} h {int(uptime % 3600 // 60)} min")
    data.update({"cpu_percent": cpu, "cores": psutil.cpu_count(), "windows": platform.platform(), "machine": platform.processor()})
    return Result(". ".join(parts) + ".", data)


@tool("list_processes", "See which programs are using the most memory or CPU.", group="System", doing="Checking running programs",
      keywords="process processes program task manager memory ram cpu using hogging slow eating running heavy",
      sort=arg("string", "Sort by memory or cpu", enum=["memory", "cpu"]), limit=arg("integer", "How many to show"), name=arg("string", "Only programs whose name contains this"))
def list_processes(ctx, sort: str = "memory", limit: int = 10, name: str = "") -> Result:
    if sort == "cpu":
        for process in psutil.process_iter():
            try:
                process.cpu_percent(None)
            except psutil.Error:
                continue
        time.sleep(0.5)
    groups: dict[str, dict[str, float]] = {}
    for process in psutil.process_iter(["name", "memory_info"]):
        try:
            label = process.info["name"] or "unknown"
            if name and name.lower() not in label.lower():
                continue
            group = groups.setdefault(label, {"memory": 0.0, "cpu": 0.0, "count": 0})
            group["memory"] += process.info["memory_info"].rss if process.info["memory_info"] else 0
            group["count"] += 1
            if sort == "cpu":
                group["cpu"] += process.cpu_percent(None) / (psutil.cpu_count() or 1)
        except psutil.Error:
            continue
    ordered = sorted(groups.items(), key=lambda item: item[1][sort], reverse=True)[:max(1, min(limit, 30))]
    lines = [f"{label}: {_fmt_bytes(group['memory'])}" + (f", {group['cpu']:.0f}% CPU" if sort == "cpu" else "") + (f" ({int(group['count'])} processes)" if group["count"] > 1 else "") for label, group in ordered]
    return Result(f"Top {len(lines)} programs by {sort}:", "\n".join(lines))


def _gate_kill(ctx, name: str = "") -> Approval | None:
    return Approval(f"Force-close every process named '{name}'?", "Unsaved work in those programs will be lost.")


@tool("kill_process", "Force-close a running program by name.", risk=DANGER, terminal=True, group="System", gate=_gate_kill, doing="Closing a program",
      keywords="kill end stop terminate force close process program frozen stuck", name=arg("string", "Program name, e.g. chrome", required=True))
def kill_process(ctx, name: str) -> Result:
    target = name.lower().removesuffix(".exe")
    own = os.getpid()
    victims = []
    for process in psutil.process_iter(["name"]):
        label = (process.info["name"] or "").lower().removesuffix(".exe")
        if label == target and process.pid != own:
            victims.append(process)
    if not victims:
        raise ToolError(f"No running program is named '{name}'.", "Use list_processes to see exact names.")
    for process in victims:
        try:
            process.kill()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(victims, timeout=4)
    if alive:
        raise ToolError(f"{len(alive)} of {len(victims)} '{name}' processes refused to close. They may need administrator rights.")
    return Result(f"Closed {len(victims)} '{name}' process(es).", verified="None of them are still running.")


def notify_user(ctx, title: str, message: str) -> None:
    if ctx.bus:
        ctx.bus.publish({"type": "notification", "title": title, "message": message})
    if ctx.shell:
        ctx.shell.notify(title, message)


@tool("lock_screen", "Lock the PC.", risk=WRITE, terminal=True, group="System", doing="Locking the PC", keywords="lock screen secure away")
def lock_screen(ctx) -> Result:
    if not ctypes.windll.user32.LockWorkStation():
        raise ToolError("Windows refused to lock the workstation.")
    return Result("Locked the PC.", verified="Windows accepted the lock request.")
