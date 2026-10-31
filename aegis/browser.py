"""A real browser Aegis can see and drive: the user's installed browser, on a profile of its own so sign-ins persist."""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
import urllib.parse
import winreg
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable

from aegis import config

logger = logging.getLogger("aegis.browser")

_BROWSERS = {
    "BraveHTML": "brave", "BraveSSHTM": "brave", "ChromeHTML": "chrome", "MSEdgeHTM": "edge", "MSEdgeHTML": "edge", "VivaldiHTM": "vivaldi",
}
_LOCATIONS = {
    "brave": [r"BraveSoftware\Brave-Browser\Application\brave.exe"],
    "chrome": [r"Google\Chrome\Application\chrome.exe"],
    "edge": [r"Microsoft\Edge\Application\msedge.exe"],
    "vivaldi": [r"Vivaldi\Application\vivaldi.exe"],
}
# Interactive pages can take a while, but a tool call must not hang the whole request.
_CALL_TIMEOUT = 60
_NAV_TIMEOUT_MS = 30000
MAX_ELEMENTS = 45

# Runs inside the page. Numbers every visible control, so the model can refer to one without writing selectors.
_SNAPSHOT_JS = r"""
(args) => {
  const start = args.start, limit = args.limit, find = (args.find || '').toLowerCase();
  const query = 'a[href],button,input:not([type=hidden]),textarea,select,summary,[role=button],[role=link],[role=textbox],[role=searchbox],[role=combobox],[role=menuitem],[role=tab],[role=checkbox],[role=radio],[role=option],[role=switch],[role=listitem][tabindex],[contenteditable=""],[contenteditable=true]';
  const clean = text => (text || '').replace(/\s+/g, ' ').trim();
  const nameOf = el => {
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled) {
      const text = labelled.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(node => node.innerText).join(' ');
      if (clean(text)) return clean(text);
    }
    const label = el.labels && el.labels[0] ? el.labels[0].innerText : '';
    return clean(el.getAttribute('aria-label') || label || el.innerText || el.value || el.placeholder || el.title || el.getAttribute('alt') || (el.querySelector && el.querySelector('img[alt]') ? el.querySelector('img[alt]').alt : ''));
  };
  const roleOf = el => {
    const role = el.getAttribute('role');
    if (role) return role;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'input') {
      const type = (el.type || 'text').toLowerCase();
      if (type === 'search') return 'searchbox';
      if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
      if (['checkbox', 'radio', 'password', 'file'].includes(type)) return type;
      return 'textbox';
    }
    if (tag === 'textarea' || el.isContentEditable) return 'textbox';
    if (tag === 'select') return 'dropdown';
    return tag === 'summary' ? 'button' : tag;
  };
  document.querySelectorAll('[data-aegis-ref]').forEach(el => el.removeAttribute('data-aegis-ref'));
  const height = window.innerHeight, seen = new Set(), rows = [];
  for (const el of document.querySelectorAll(query)) {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (box.width < 2 || box.height < 2 || style.visibility === 'hidden' || style.display === 'none' || el.disabled) continue;
    const name = nameOf(el), role = roleOf(el), key = role + '|' + name;
    if (!name && !['textbox', 'searchbox', 'dropdown', 'password', 'checkbox', 'radio'].includes(role)) continue;
    if (seen.has(key) && role === 'link') continue;
    seen.add(key);
    if (find && !name.toLowerCase().includes(find)) continue;
    rows.push({ el, role, name: name.slice(0, 90), value: 'value' in el && role !== 'password' && role !== 'button' ? String(el.value || '').slice(0, 60) : '', onScreen: box.top < height && box.bottom > 0 });
  }
  rows.sort((a, b) => (b.onScreen - a.onScreen));
  const chosen = rows.slice(0, limit).map((row, index) => {
    const ref = start + index;
    row.el.setAttribute('data-aegis-ref', String(ref));
    return { ref, role: row.role, name: row.name, value: row.value };
  });
  const root = document.querySelector('main, article, [role=main]') || document.body;
  let text = '';
  try { text = root.innerText || ''; } catch (e) {}
  if (text.length < 200 && document.body) text = document.body.innerText || text;
  const lowered = (document.body ? document.body.innerText : '').slice(0, 4000).toLowerCase();
  const framed = [...document.querySelectorAll('iframe')].map(frame => (frame.src || '') + ' ' + (frame.title || '')).join(' ').toLowerCase();
  return {
    elements: chosen, total: rows.length, text: text.slice(0, 60000),
    password: !!document.querySelector('input[type=password]'),
    challenge: /recaptcha|hcaptcha|turnstile|arkose/.test(framed) || /verify you are (a )?human|confirm you are (a )?human|are you a robot|not a robot|unusual traffic|complete the following challenge/.test(lowered),
    qr: /scan (the|this) qr|use whatsapp on your computer|link with phone|log in by scanning|scan to log in/.test(lowered),
    video: (() => { const video = document.querySelector('video'); return video ? { paused: video.paused, time: video.currentTime, muted: video.muted } : null; })(),
  };
}
"""


class ThreadWorker:
    """Some libraries must only ever be used from the thread that started them, so their calls queue up on one."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._queue: queue.Queue[tuple[Callable[[], Any], Future] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def call(self, fn: Callable[[], Any], timeout: float = _CALL_TIMEOUT) -> Any:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True, name=self.name)
                self._thread.start()
        future: Future = Future()
        self._queue.put((fn, future))
        return future.result(timeout=timeout)

    def _loop(self) -> None:
        while True:
            entry = self._queue.get()
            if entry is None:
                return
            fn, future = entry
            try:
                future.set_result(fn())
            except BaseException as exc:  # handed to the waiting caller
                future.set_exception(exc)

    def stop(self) -> None:
        self._queue.put(None)


def find_browser() -> tuple[str, str] | None:
    """(name, path) of the default browser if it can be driven, otherwise the first one installed that can."""
    order: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as key:
            prog = winreg.QueryValueEx(key, "ProgId")[0]
            if prog.split(".")[0] in _BROWSERS:
                order.append(_BROWSERS[prog.split(".")[0]])
    except OSError:
        pass
    order += [name for name in ("chrome", "edge", "brave", "vivaldi") if name not in order]
    roots = [os.environ.get(name, "") for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    for name in order:
        for relative in _LOCATIONS[name]:
            for root in roots:
                candidate = Path(root) / relative if root else None
                if candidate and candidate.exists():
                    return name, str(candidate)
    return None


class BrowserError(RuntimeError):
    pass


class Browser:
    """One visible window, driven from one thread. Reopens itself if the user closes it."""

    def __init__(self) -> None:
        self.worker = ThreadWorker("aegis-browser")
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self.headless = bool(os.environ.get("AEGIS_BROWSER_HEADLESS"))
        self.last: list[dict[str, Any]] = []
        self.last_url = ""

    # ---- all of this runs on the worker thread
    def _ensure(self) -> Any:
        if self._context is not None:
            try:
                self._context.pages  # noqa: B018 - raises if the browser has gone away
                if self._page is not None and not self._page.is_closed():
                    return self._page
                open_pages = self._context.pages
                if open_pages:
                    self._page = open_pages[-1]
                    return self._page
            except Exception:
                logger.info("the browser was closed")
            self._drop()
        found = find_browser()
        if found is None:
            raise BrowserError("No supported browser is installed. Install Chrome, Edge or Brave.")
        from playwright.sync_api import sync_playwright

        if self._playwright is None:
            self._playwright = sync_playwright().start()
        profile = config.HOME / "browser"
        profile.mkdir(parents=True, exist_ok=True)
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(profile), executable_path=found[1], headless=self.headless, no_viewport=not self.headless, accept_downloads=True,
                ignore_default_args=["--disable-extensions"],
                args=["--no-first-run", "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required", "--window-size=1200,820"],
            )
        except Exception as exc:
            raise BrowserError(f"Could not start {found[0]}: {str(exc).splitlines()[0][:200]}") from exc
        self._context.set_default_timeout(12000)
        self._context.on("page", self._adopt)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def _adopt(self, page: Any) -> None:
        self._page = page

    def _drop(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        self._context = self._page = None
        self.last = []

    def _settle(self, page: Any) -> None:
        try:
            page.wait_for_timeout(300)  # a click or Enter may not have started its navigation yet
            page.wait_for_load_state("domcontentloaded", timeout=8000)
            page.wait_for_load_state("networkidle", timeout=1800)
        except Exception:
            pass  # busy pages never go idle, and that is fine

    def _snapshot(self, page: Any, find: str = "") -> dict[str, Any]:
        elements: list[dict[str, Any]] = []
        merged: dict[str, Any] = {"text": "", "password": False, "challenge": False, "qr": False, "video": None, "total": 0}
        for index, frame in enumerate(page.frames[:6]):
            if index and (frame.is_detached() or not frame.url or frame.url.startswith("about:")):
                continue
            try:
                found = frame.evaluate(_SNAPSHOT_JS, {"start": len(elements) + 1, "limit": MAX_ELEMENTS - len(elements), "find": find})
            except Exception:
                continue
            for element in found["elements"]:
                element["frame"] = index
            elements.extend(found["elements"])
            merged["total"] += found["total"]
            if index == 0:
                merged["text"] = found["text"]
                merged["video"] = found["video"]
            merged["password"] = merged["password"] or found["password"]
            merged["challenge"] = merged["challenge"] or found["challenge"]
            merged["qr"] = merged["qr"] or found["qr"]
        self.last, self.last_url = elements, page.url
        return {**merged, "elements": elements, "title": _safe(page.title), "url": page.url}

    def _element(self, page: Any, ref: int) -> tuple[Any, dict[str, Any]]:
        entry = next((item for item in self.last if item["ref"] == ref), None)
        if entry is None:
            raise BrowserError(f"There is no element [{ref}] on the page any more.")
        frame = page.frames[entry["frame"]] if entry["frame"] < len(page.frames) else page.main_frame
        locator = frame.locator(f'[data-aegis-ref="{ref}"]').first
        if locator.count() == 0:
            raise BrowserError(f"Element [{ref}] has gone, because the page changed.")
        return locator, entry

    # ---- the public calls, each hopping to the worker thread
    def open(self, url: str, find: str = "") -> dict[str, Any]:
        def work() -> dict[str, Any]:
            page = self._ensure()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            except Exception as exc:
                raise BrowserError(f"Could not load {url}: {str(exc).splitlines()[0][:200]}") from exc
            self._settle(page)
            return self._snapshot(page, find)

        return self.worker.call(work)

    def read(self, find: str = "") -> dict[str, Any]:
        return self.worker.call(lambda: self._snapshot(self._ensure(), find))

    def describe(self, ref: int) -> dict[str, Any] | None:
        return next((item for item in self.last if item["ref"] == ref), None)

    def click(self, ref: int) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            page = self._ensure()
            locator, _ = self._element(page, ref)
            try:
                locator.scroll_into_view_if_needed(timeout=4000)
                locator.click(timeout=8000)
            except Exception as exc:
                raise BrowserError(f"Could not click [{ref}]: {str(exc).splitlines()[0][:160]}") from exc
            page = self._ensure()
            self._settle(page)
            return self._snapshot(page)

        return self.worker.call(work)

    def type(self, ref: int, text: str, submit: bool = False) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            page = self._ensure()
            locator, _ = self._element(page, ref)
            try:
                locator.scroll_into_view_if_needed(timeout=4000)
                editable = locator.evaluate("el => el.isContentEditable")
                if editable:
                    locator.click(timeout=6000)
                    page.keyboard.press("Control+A")
                    page.keyboard.insert_text(text)
                else:
                    locator.fill(text, timeout=8000)
                if submit:
                    page.keyboard.press("Enter")
            except Exception as exc:
                raise BrowserError(f"Could not type into [{ref}]: {str(exc).splitlines()[0][:160]}") from exc
            page = self._ensure()
            self._settle(page)
            return self._snapshot(page)

        return self.worker.call(work)

    def press(self, keys: str) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            page = self._ensure()
            combo = "+".join(part.strip().capitalize() if len(part.strip()) > 1 else part.strip() for part in keys.split("+"))
            try:
                page.keyboard.press(combo)
            except Exception as exc:
                raise BrowserError(f"Could not press {keys}: {str(exc).splitlines()[0][:160]}") from exc
            self._settle(page)
            return self._snapshot(page)

        return self.worker.call(work)

    def play_first_video(self, url: str) -> dict[str, Any]:
        """Opens a YouTube results page and starts the top result, and says whether it is really playing."""
        def work() -> dict[str, Any]:
            page = self._ensure()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                link = page.locator("a#video-title").first
                link.wait_for(state="visible", timeout=15000)
                title = link.get_attribute("title") or link.inner_text()
                link.click(timeout=8000)
                page.wait_for_url("**/watch**", timeout=15000)
            except Exception as exc:
                raise BrowserError(f"Could not start a video: {str(exc).splitlines()[0][:180]}") from exc
            playing = False
            for _ in range(14):
                page.wait_for_timeout(500)
                state = page.evaluate("() => { const v = document.querySelector('video'); return v ? { paused: v.paused, time: v.currentTime } : null }")
                if state and not state["paused"] and state["time"] > 0:
                    playing = True
                    break
                if state and state["paused"] and _ == 6:
                    page.keyboard.press("k")  # YouTube's own play key, for when autoplay was refused
            return {"title": title, "url": page.url, "playing": playing}

        return self.worker.call(work)

    def status(self) -> dict[str, Any]:
        found = find_browser()
        return {"name": found[0] if found else None, "open": self._context is not None}

    def shutdown(self) -> None:
        def work() -> None:
            self._drop()
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

        if self.worker._thread is not None and self.worker._thread.is_alive():
            try:
                self.worker.call(work, timeout=8)
            except Exception as exc:
                logger.info("browser shutdown: %s", exc)
            self.worker.stop()


def _safe(getter: Callable[[], str]) -> str:
    try:
        return getter()
    except Exception:
        return ""


def host_of(url: str) -> str:
    return re.sub(r"^www\.", "", urllib.parse.urlparse(url).hostname or "")


session = Browser()
