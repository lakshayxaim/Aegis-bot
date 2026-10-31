"""The native shell: main window, quick-ask overlay, tray icon, global hotkeys. All one process."""
from __future__ import annotations

import ctypes
import json
import logging
import os
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", " ".join([
    "--disable-background-networking", "--disable-component-update", "--disable-sync", "--no-pings", "--disable-domain-reliability",
    "--disable-features=OptimizationHints,MediaRouter,Translate,AutofillServerCommunication", "--no-first-run", "--disable-breakpad",
]))

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray, QObject, QSettings, QSize, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QGuiApplication, QIcon, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings, QWebEngineUrlRequestInterceptor
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon, QWidget

from aegis import config

logger = logging.getLogger("aegis.shell")
INSTANCE_NAME = "aegis-v2-single-instance"
APP_ID = "Aegis.Desktop"
ICON_PATH = Path(__file__).with_name("assets") / "aegis.png"
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
VK_SPACE = 0x20
HOTKEY_SPOTLIGHT, HOTKEY_VOICE = 1, 2
# Another app may already own a shortcut, so each has fallbacks. The UI shows whichever was free.
_HOTKEY_CHOICES = {
    HOTKEY_SPOTLIGHT: [("Alt+Shift+Space", MOD_ALT | MOD_SHIFT, VK_SPACE), ("Ctrl+Alt+Space", MOD_CONTROL | MOD_ALT, VK_SPACE), ("Ctrl+Shift+Space", MOD_CONTROL | MOD_SHIFT, VK_SPACE)],
    HOTKEY_VOICE: [("Alt+Shift+V", MOD_ALT | MOD_SHIFT, ord("V")), ("Ctrl+Alt+V", MOD_CONTROL | MOD_ALT, ord("V")), ("Ctrl+Shift+Alt+V", MOD_CONTROL | MOD_SHIFT | MOD_ALT, ord("V"))],
}
SPOTLIGHT_WIDTH, SPOTLIGHT_MIN_HEIGHT, SPOTLIGHT_MAX_HEIGHT = 600, 30, 470
GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x11



def _set_app_identity() -> None:
    """Gives Aegis its own taskbar icon and its own name on notifications, rather than Python's."""
    try:
        import winreg

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}") as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Aegis")
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(ICON_PATH))
    except OSError as exc:
        logger.info("could not register the app identity: %s", exc)


def already_running() -> bool:
    """If Aegis is already open, ask it to show itself."""
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_NAME)
    if socket.waitForConnected(300):
        socket.write(b"show")
        socket.flush()
        socket.waitForBytesWritten(300)
        socket.disconnectFromServer()
        return True
    return False


class _LocalOnly(QWebEngineUrlRequestInterceptor):
    """The window only ever loads Aegis's own local pages."""

    def interceptRequest(self, info: Any) -> None:
        url = info.requestUrl()
        if url.scheme() in {"http", "https", "ws", "wss"} and url.host() not in {"127.0.0.1", "localhost"}:
            logger.warning("blocked web request to %s", url.host())
            info.block(True)


class _ExternalLinks(QWebEnginePage):
    """Links in replies open in the user's own browser, never inside Aegis."""

    def acceptNavigationRequest(self, url: QUrl, kind: Any, is_main: bool) -> bool:
        if is_main and url.host() not in {"127.0.0.1", "localhost"}:
            if url.scheme() in {"http", "https"}:
                os.startfile(url.toString())  # noqa: S606 - http(s) only
            return False
        return True


class _Hotkeys(QAbstractNativeEventFilter):
    def __init__(self, on_hotkey: Any) -> None:
        super().__init__()
        self.on_hotkey = on_hotkey

    def nativeEventFilter(self, event_type: Any, message: Any) -> tuple[bool, int]:
        if bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                self.on_hotkey(int(msg.wParam))
                return True, 0
        return False, 0


class _MainWindow(QMainWindow):
    def __init__(self, shell: "Shell") -> None:
        super().__init__()
        self.shell = shell
        self.setWindowTitle("Aegis")
        self.setMinimumSize(QSize(860, 560))

    def changeEvent(self, event: Any) -> None:
        super().changeEvent(event)
        if event.type() == event.Type.ActivationChange:
            self.shell.main_activation(self.isActiveWindow())

    def closeEvent(self, event: Any) -> None:
        if self.shell.quitting:
            self.shell.settings.setValue("geometry", self.saveGeometry())
            event.accept()
            return
        event.ignore()
        self.hide()
        self.shell.settings.setValue("geometry", self.saveGeometry())
        if not self.shell.settings.value("told_about_tray", False, type=bool):
            self.shell.settings.setValue("told_about_tray", True)
            self.shell.notify("Aegis is still running", "It stays in the tray so reminders and the quick-ask hotkey keep working. Right-click the icon to quit.")


class _Spotlight(QWidget):
    def __init__(self, shell: "Shell") -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.shell = shell
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.resize(SPOTLIGHT_WIDTH, SPOTLIGHT_MIN_HEIGHT)
        self.pinned = False

    def changeEvent(self, event: Any) -> None:
        super().changeEvent(event)
        if event.type() == event.Type.ActivationChange and not self.isActiveWindow() and self.isVisible():
            if self.pinned:
                self.shell.page_call("{blur:true}")
            else:
                self.hide()

    def moveEvent(self, event: Any) -> None:
        super().moveEvent(event)
        # Only a move the user made changes where it lives from then on.
        if self.shell.dragging:
            self.shell.remember_place()

    def hideEvent(self, event: Any) -> None:
        super().hideEvent(event)
        # The microphone never outlives the window that shows it is on.
        if self.shell.runtime is not None:
            self.shell.runtime.voice.cancel_for("overlay")


class Shell(QObject):
    """Everything here runs on the Qt thread; signals carry requests over from worker threads."""

    _notify = Signal(str, str)
    _attention = Signal(str)
    _spotlight_cmd = Signal(str, object, object)
    _run_state = Signal(str, bool)
    _titlebar = Signal(str)
    _hide_capture = Signal(object)
    _restore_capture = Signal()
    _show_main = Signal()

    def __init__(self) -> None:
        _set_app_identity()
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        super().__init__()
        self.app.setApplicationName("Aegis")
        self.app.setQuitOnLastWindowClosed(False)
        self.quitting = False
        self.dragging = False
        self.previous_window = 0
        self.active_threads: list[str] = []
        self.place: tuple[float, float] | None = None
        self.hotkeys: dict[str, str | None] = {"spotlight": None, "voice": None}
        self.settings = QSettings(str(config.HOME / "window.ini"), QSettings.Format.IniFormat)
        self.url = ""
        self.runtime: Any = None
        self.icon = QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon()
        self.app.setWindowIcon(self.icon)
        self._hidden_for_capture: list[QWidget] = []
        self._notify.connect(self._do_notify)
        self._attention.connect(self._do_attention)
        self._spotlight_cmd.connect(self._do_spotlight)
        self._run_state.connect(self._do_run_state)
        self._titlebar.connect(self._do_titlebar)
        self._hide_capture.connect(self._do_hide_capture)
        self._restore_capture.connect(self._do_restore_capture)
        self._show_main.connect(self.show_main)
        self._server = QLocalServer(self)
        QLocalServer.removeServer(INSTANCE_NAME)
        self._server.listen(INSTANCE_NAME)
        self._server.newConnection.connect(self._on_second_instance)

    # ---- called from worker threads
    def notify(self, title: str, message: str) -> None:
        self._notify.emit(title, message)

    def attention(self, message: str) -> None:
        self._attention.emit(message)

    def spotlight_command(self, action: str, height: int | None, width: int | None = None) -> None:
        self._spotlight_cmd.emit(action, height, width)

    def run_state(self, thread_id: str, active: bool) -> None:
        self._run_state.emit(thread_id, active)

    def set_titlebar(self, mode: str) -> None:
        self._titlebar.emit(mode)

    def hide_for_capture(self) -> bool:
        """Step Aegis's own windows out of the picture, returning once they are gone."""
        done = threading.Event()
        self._hide_capture.emit(done)
        done.wait(timeout=1.5)
        return bool(self._hidden_for_capture)

    def restore_after_capture(self) -> None:
        self._restore_capture.emit()

    # ---- startup
    def run(self, url: str, runtime: Any) -> int:
        self.url, self.runtime = url, runtime
        self.profile = QWebEngineProfile("aegis", self)
        self.profile.setPersistentStoragePath(str(config.HOME / "webengine"))
        self.profile.setCachePath(str(config.HOME / "webengine-cache"))
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self.profile.setUrlRequestInterceptor(_LocalOnly(self))
        self.window = _MainWindow(self)
        self.view = QWebEngineView(self.window)
        page = _ExternalLinks(self.profile, self.view)
        page.setBackgroundColor(QColor("#141312"))
        self.view.setPage(page)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.view.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        self.window.setCentralWidget(self.view)
        self.view.load(QUrl(url))
        geometry = self.settings.value("geometry")
        if isinstance(geometry, QByteArray) and not geometry.isEmpty():
            self.window.restoreGeometry(geometry)
        else:
            self.window.resize(1240, 820)
        self.window.show()
        self._apply_titlebar(self.window, "dark")
        self._filter = _Hotkeys(self._on_hotkey)
        self.app.installNativeEventFilter(self._filter)
        self._register_hotkeys()
        self._build_spotlight()
        self._build_tray()
        code = self.app.exec()
        ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_SPOTLIGHT)
        ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_VOICE)
        return code

    def _register_hotkeys(self) -> None:
        user32 = ctypes.windll.user32
        for hotkey, name in ((HOTKEY_SPOTLIGHT, "spotlight"), (HOTKEY_VOICE, "voice")):
            for label, modifiers, key in _HOTKEY_CHOICES[hotkey]:
                if user32.RegisterHotKey(None, hotkey, modifiers | MOD_NOREPEAT, key):
                    self.hotkeys[name] = label
                    logger.info("%s hotkey: %s", name, label)
                    break
            else:
                logger.warning("no free hotkey for %s", name)

    def _on_hotkey(self, hotkey: int) -> None:
        if hotkey == HOTKEY_SPOTLIGHT:
            self.toggle_spotlight()
        elif hotkey == HOTKEY_VOICE:
            self.show_spotlight(voice=True)

    # ---- main window
    def show_main(self) -> None:
        self.window.show()
        self.window.setWindowState(self.window.windowState() & ~Qt.WindowState.WindowMinimized)
        self.window.raise_()
        self.window.activateWindow()

    def _on_second_instance(self) -> None:
        connection = self._server.nextPendingConnection()
        if connection:
            connection.deleteLater()
        self.show_main()

    def _apply_titlebar(self, window: QWidget, mode: str) -> None:
        try:
            hwnd = wintypes.HWND(int(window.winId()))
            dark = ctypes.c_int(1 if mode == "dark" else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
            caption = ctypes.c_int(0x00121314 if mode == "dark" else 0x00EEF3F5)  # COLORREF, BGR
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
        except (OSError, ValueError):
            pass

    def _do_titlebar(self, mode: str) -> None:
        self._apply_titlebar(self.window, mode)

    # ---- quick ask overlay
    def _build_spotlight(self) -> None:
        self.spotlight = _Spotlight(self)
        self.spotlight_view = QWebEngineView(self.spotlight)
        page = _ExternalLinks(self.profile, self.spotlight_view)
        page.setBackgroundColor(Qt.GlobalColor.transparent)
        self.spotlight_view.setPage(page)
        self.spotlight_view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.spotlight_view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.spotlight_view.resize(self.spotlight.size())
        self.spotlight_view.load(QUrl(self.url + "/#/spotlight"))
        self.spotlight.winId()  # creates the native window now, so its style can be set before it is ever shown
        self._apply_glass()
        self._set_activating(False)

    def page_call(self, options: str) -> None:
        if hasattr(self, "spotlight_view"):
            self.spotlight_view.page().runJavaScript(f"window.__aegisSpotlight && window.__aegisSpotlight({options})")

    def _hwnd(self) -> wintypes.HWND:
        return wintypes.HWND(int(self.spotlight.winId()))

    def _apply_glass(self) -> None:
        """It is Aegis's own furniture, so it stays out of screenshots of the user's screen."""
        if not os.environ.get("AEGIS_CAPTURABLE"):
            ctypes.windll.user32.SetWindowDisplayAffinity(self._hwnd(), WDA_EXCLUDEFROMCAPTURE)

    def _set_activating(self, activating: bool) -> None:
        hwnd = self._hwnd()
        style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        style = style & ~WS_EX_NOACTIVATE if activating else style | WS_EX_NOACTIVATE
        ctypes.windll.user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)

    def _position(self, width: int | None = None) -> None:
        """The overlay hangs from a fixed point, so it grows down and stays centred on it as its size changes."""
        screen = QGuiApplication.screenAt(self.spotlight.geometry().center()) if self.spotlight.isVisible() else QGuiApplication.screenAt(QCursor.pos())
        area = (screen or QGuiApplication.primaryScreen()).availableGeometry()
        wide = width or self.spotlight.width()
        if self.place is None:
            saved = self.settings.value("overlay_place")
            self.place = (float(saved[0]), float(saved[1])) if isinstance(saved, (list, tuple)) and len(saved) == 2 else (0.5, 0.05)
        left = area.left() + int(area.width() * self.place[0]) - wide // 2
        top = area.top() + int(area.height() * self.place[1])
        self.spotlight.move(max(area.left(), min(left, area.right() - wide)), max(area.top(), min(top, area.bottom() - 60)))

    def remember_place(self) -> None:
        geometry = self.spotlight.geometry()
        area = (QGuiApplication.screenAt(geometry.center()) or QGuiApplication.primaryScreen()).availableGeometry()
        self.place = ((geometry.center().x() - area.left()) / max(1, area.width()), (geometry.top() - area.top()) / max(1, area.height()))
        self.settings.setValue("overlay_place", list(self.place))

    def toggle_spotlight(self) -> None:
        if self.spotlight.isVisible() and self.spotlight.isActiveWindow():
            if self.spotlight.pinned:
                self._set_activating(False)
                self._give_back_focus()
            else:
                self.spotlight.hide()
        elif self.spotlight.isVisible():
            self.show_spotlight(focus=True)
        else:
            self.show_spotlight()

    def _give_back_focus(self) -> None:
        if self.previous_window:
            ctypes.windll.user32.SetForegroundWindow(wintypes.HWND(self.previous_window))

    def show_spotlight(self, voice: bool = False, focus: bool = False) -> None:
        current = ctypes.windll.user32.GetForegroundWindow()
        if current and current != int(self.spotlight.winId()):
            self.previous_window = current
        if not self.spotlight.isVisible():
            self.spotlight.resize(SPOTLIGHT_WIDTH, self.spotlight.height())
            self._position(SPOTLIGHT_WIDTH)
        self._set_activating(True)
        self.spotlight.show()
        self.spotlight.raise_()
        self.spotlight.activateWindow()
        self.spotlight_view.setFocus()
        # Foreground rights are only reliable while handling the hotkey itself.
        ctypes.windll.user32.SetForegroundWindow(self._hwnd())
        listening = self.runtime.voice.state == "listening" and self.runtime.voice.owner == "overlay"
        finish = voice and listening  # the voice hotkey is a toggle: press to talk, press again when done
        listen = not finish and (voice or (self.runtime.wants_listening() and not focus))
        if listen:
            # Started here rather than from the page, so the first word isn't lost to the round trip.
            threading.Thread(target=self._start_listening, daemon=True, name="aegis-voice-start").start()
        self.page_call("{finish:%s,listen:%s,focus:%s}" % (str(finish).lower(), str(listen).lower(), str(focus).lower()))

    def _do_run_state(self, thread_id: str, active: bool) -> None:
        if active and thread_id not in self.active_threads:
            self.active_threads.append(thread_id)
        elif not active and thread_id in self.active_threads:
            self.active_threads.remove(thread_id)
        if active and hasattr(self, "spotlight"):
            self._watch_if_away(thread_id)

    def _watch_if_away(self, thread_id: str) -> None:
        """When the user is elsewhere, a small status appears instead of the work going on unseen."""
        if self.window.isActiveWindow() or self.spotlight.isVisible():
            return
        self.spotlight.resize(380, 38)
        self._position(380)
        self._set_activating(False)
        self.spotlight.show()
        self.page_call("{watch:%s}" % json.dumps(thread_id))

    def main_activation(self, active: bool) -> None:
        if not hasattr(self, "spotlight"):
            return
        if active:
            self.page_call("{mainActive:true}")
        elif self.active_threads:
            # Give focus a moment to land, because activation briefly passes through nothing.
            QTimer.singleShot(250, lambda: self.active_threads and self._watch_if_away(self.active_threads[-1]))

    def _start_listening(self) -> None:
        try:
            self.runtime.voice.start("overlay")
        except Exception as exc:
            # The page asks too, and shows this same error where the user is looking.
            logger.info("could not start listening: %s", exc)

    def _do_spotlight(self, action: str, height: Any, width: Any = None) -> None:
        if action == "resize" and height:
            target = max(SPOTLIGHT_MIN_HEIGHT, min(SPOTLIGHT_MAX_HEIGHT, int(height)))
            wide = int(width or self.spotlight.width())
            if wide != self.spotlight.width():
                self._position(wide)
            self.spotlight.resize(wide, target)
            self.spotlight_view.resize(wide, target)
        elif action == "hide":
            self.spotlight.pinned = False
            self.spotlight.hide()
        elif action == "release":
            # Back to whatever the user was doing, with the status left quietly in view.
            self.spotlight.pinned = True
            self._set_activating(False)
            if self.spotlight.isActiveWindow():
                self._give_back_focus()
        elif action == "hold":
            self._set_activating(True)
            self.spotlight.activateWindow()
            self.spotlight_view.setFocus()
        elif action == "drag":
            handle = self.spotlight.windowHandle()
            if handle is not None:
                self.dragging = True
                handle.startSystemMove()
                QTimer.singleShot(4000, self._end_drag)
        elif action == "pin":
            self.spotlight.pinned = True
        elif action == "unpin":
            self.spotlight.pinned = False
        elif action == "open_main":
            self.spotlight.hide()
            self.show_main()

    def _end_drag(self) -> None:
        self.dragging = False

    # ---- screenshots
    def _do_hide_capture(self, done: threading.Event) -> None:
        # The overlay is already invisible to screen capture, and hiding it would make it flicker during the work.
        self._hidden_for_capture = [widget for widget in (self.window,) if widget.isVisible() and not widget.isMinimized()]
        if os.environ.get("AEGIS_CAPTURABLE") and self.spotlight.isVisible():
            self._hidden_for_capture.append(self.spotlight)
        for widget in self._hidden_for_capture:
            widget.setWindowOpacity(0.0)
            widget.hide()
        # Give the compositor time to actually remove the windows from the screen.
        QTimer.singleShot(220 if self._hidden_for_capture else 0, done.set)

    def _do_restore_capture(self) -> None:
        for widget in self._hidden_for_capture:
            widget.setWindowOpacity(1.0)
            widget.show()
        self._hidden_for_capture = []

    # ---- tray
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.icon if not self.icon.isNull() else QIcon(QPixmap(16, 16)), self)
        self.tray.setToolTip("Aegis")
        menu = QMenu()
        quick = f"Quick ask   {self.hotkeys['spotlight']}" if self.hotkeys["spotlight"] else "Quick ask"
        speak = f"Speak to Aegis   {self.hotkeys['voice']}" if self.hotkeys["voice"] else "Speak to Aegis"
        for label, handler in (("Open Aegis", self.show_main), (quick, self.show_spotlight), (speak, lambda: self.show_spotlight(voice=True))):
            action = QAction(label, menu)
            action.triggered.connect(lambda _=False, callback=handler: callback())
            menu.addAction(action)
        menu.addSeparator()
        quit_action = QAction("Quit Aegis", menu)
        quit_action.triggered.connect(self.quit)
        menu.addAction(quit_action)
        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_main() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def _do_notify(self, title: str, message: str) -> None:
        if hasattr(self, "tray"):
            self.tray.showMessage(title, message, self.icon, 8000)

    def _do_attention(self, message: str) -> None:
        if hasattr(self, "window") and not self.window.isActiveWindow():
            if self.active_threads:
                self._watch_if_away(self.active_threads[-1])
            if not self.spotlight.isVisible():
                self.tray.showMessage("Aegis needs you", message, self.icon, 8000)
                self.app.alert(self.window, 0)

    def quit(self) -> None:
        self.quitting = True
        if self.runtime is not None:
            self.runtime.llm.shutdown()
            self.runtime.voice.cancel()
        self.window.close()
        self.spotlight.close()
        self.tray.hide()
        self.app.quit()
