"""Reading and operating any Windows app through UI Automation, the interface screen readers use."""
from __future__ import annotations

import ctypes
import logging
import time
from dataclasses import dataclass
from typing import Any

from aegis import winapi
from aegis.browser import ThreadWorker

logger = logging.getLogger("aegis.uia")

# UIA_ControlTypeIds
_BUTTON, _CHECK, _COMBO, _EDIT, _LINK, _LIST_ITEM, _MENU_ITEM, _RADIO, _SLIDER, _TAB_ITEM, _TEXT, _TREE_ITEM, _DOCUMENT, _SPLIT = 50000, 50002, 50003, 50004, 50005, 50007, 50011, 50013, 50015, 50019, 50020, 50024, 50030, 50031
_ROLES = {_BUTTON: "button", _CHECK: "checkbox", _COMBO: "dropdown", _EDIT: "textbox", _LINK: "link", _LIST_ITEM: "item", 50029: "item", _MENU_ITEM: "menu", _RADIO: "radio", _SLIDER: "slider", _TAB_ITEM: "tab", _TREE_ITEM: "tree item", _DOCUMENT: "document", _SPLIT: "button"}
# UIA property and pattern ids
_NAME, _TYPE, _ENABLED, _OFFSCREEN, _RECT, _PASSWORD, _VALUE, _AUTOMATION_ID = 30005, 30003, 30010, 30022, 30001, 30019, 30045, 30011
_INVOKE, _SELECTION_ITEM, _VALUE_PATTERN, _TOGGLE, _EXPAND, _TEXT_PATTERN = 10000, 10010, 10002, 10015, 10005, 10014
_DESCENDANTS = 4
MAX_ELEMENTS = 60


@dataclass
class Control:
    ref: int
    role: str
    name: str
    value: str
    element: Any
    password: bool = False


class Automation:
    """COM objects stay on the thread that made them, so everything runs on one worker."""

    def __init__(self) -> None:
        self.worker = ThreadWorker("aegis-uia")
        self._client: Any = None
        self._gen: Any = None
        self.controls: list[Control] = []
        self.hwnd = 0

    def _ensure(self) -> Any:
        if self._client is None:
            import comtypes
            import comtypes.client

            try:
                comtypes.CoInitialize()
            except OSError:  # importing comtypes on this thread may already have done it
                pass
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as gen

            self._gen = gen
            self._client = comtypes.client.CreateObject(gen.CUIAutomation, interface=gen.IUIAutomation)
        return self._client

    def _read(self, hwnd: int, find: str) -> dict[str, Any]:
        client, gen = self._ensure(), self._gen
        request = client.CreateCacheRequest()
        for prop in (_NAME, _TYPE, _ENABLED, _OFFSCREEN, _RECT, _PASSWORD, _VALUE, _AUTOMATION_ID):
            request.AddProperty(prop)
        for pattern in (_INVOKE, _SELECTION_ITEM, _VALUE_PATTERN, _TOGGLE, _EXPAND):
            request.AddPattern(pattern)
        root = client.ElementFromHandle(ctypes.c_void_p(hwnd))
        found: list[Any] = []
        # Web-based apps build their tree only once something asks for it, so an empty first answer is normal.
        for attempt in range(4):
            result = root.FindAllBuildCache(_DESCENDANTS, client.CreateTrueCondition(), request)
            found = [result.GetElement(index) for index in range(min(result.Length, 3000))]
            if len(found) > 8:
                break
            time.sleep(0.6 + attempt * 0.4)
        wanted = find.lower().strip()
        controls: list[Control] = []
        texts: list[str] = []
        seen: set[str] = set()
        for element in found:
            try:
                kind = element.CachedControlType
                name = (element.CachedName or "").strip()
                if element.CachedIsOffscreen:
                    continue
                if kind == _TEXT:
                    if name and name not in seen and len(texts) < 120:
                        seen.add(name)
                        texts.append(name)
                    continue
                role = _ROLES.get(kind)
                if role is None or not element.CachedIsEnabled:
                    continue
                if role == "document" and not name:
                    continue
                if not name and role not in {"textbox", "dropdown"}:
                    continue
                value = ""
                if role in {"textbox", "dropdown"}:
                    value = str(element.GetCachedPropertyValue(_VALUE) or "")[:70]
                if wanted and wanted not in name.lower():
                    continue
                rect = element.CachedBoundingRectangle
                identity = f"{role}|{name}|{rect.left},{rect.top},{rect.right},{rect.bottom}"
                if identity in seen:
                    continue  # embedded web views report the same control many times
                seen.add(identity)
                controls.append(Control(0, role, name[:90], value, element, bool(element.CachedIsPassword)))
            except Exception:  # elements vanish while being read
                continue
        # Text boxes first, because they are what a request usually needs and busy lists would crowd them out.
        controls.sort(key=lambda control: control.role not in {"textbox", "document"})
        total = len(controls)
        controls = controls[:MAX_ELEMENTS]
        for index, control in enumerate(controls, 1):
            control.ref = index
        self.controls, self.hwnd = controls, hwnd
        return {"elements": [{"ref": control.ref, "role": control.role, "name": control.name, "value": "" if control.password else control.value} for control in controls], "total": total, "text": "\n".join(texts)}

    def _control(self, ref: int) -> Control:
        control = next((item for item in self.controls if item.ref == ref), None)
        if control is None:
            raise LookupError(f"There is no element [{ref}]. Read the window again.")
        return control

    def _click(self, control: Control) -> str:
        element = control.element
        gen = self._gen
        for pattern_id, interface, action in ((_INVOKE, gen.IUIAutomationInvokePattern, "Invoke"), (_SELECTION_ITEM, gen.IUIAutomationSelectionItemPattern, "Select"), (_TOGGLE, gen.IUIAutomationTogglePattern, "Toggle")):
            try:
                raw = element.GetCachedPattern(pattern_id)
                if raw:
                    getattr(raw.QueryInterface(interface), action)()
                    return action
            except Exception as exc:
                logger.debug("pattern %s failed: %s", pattern_id, exc)
        rect = element.CachedBoundingRectangle
        winapi.focus_window(self.hwnd)
        time.sleep(0.1)
        winapi.click((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        return "Click"

    def _text_of(self, control: Control) -> str | None:
        """What a text control holds right now, read back from the app itself."""
        element = control.element
        try:
            raw = element.GetCurrentPattern(_TEXT_PATTERN)
            if raw:
                return raw.QueryInterface(self._gen.IUIAutomationTextPattern).DocumentRange.GetText(20000)
        except Exception as exc:
            logger.debug("text pattern failed: %s", exc)
        try:
            raw = element.GetCurrentPattern(_VALUE_PATTERN)
            if raw:
                return raw.QueryInterface(self._gen.IUIAutomationValuePattern).CurrentValue
        except Exception as exc:
            logger.debug("value read failed: %s", exc)
        return None

    def _type(self, control: Control, text: str, submit: bool) -> None:
        element = control.element
        winapi.focus_window(self.hwnd)
        time.sleep(0.1)
        current = winapi.current_window()
        if current is None or current.hwnd != self.hwnd:
            raise PermissionError("Another window took focus, so nothing was typed.")
        done = False
        try:
            raw = element.GetCachedPattern(_VALUE_PATTERN)
            if raw:
                pattern = raw.QueryInterface(self._gen.IUIAutomationValuePattern)
                if not pattern.CurrentIsReadOnly:
                    pattern.SetValue(text)
                    done = True
        except Exception as exc:
            logger.debug("value pattern failed: %s", exc)
        if not done:
            try:
                element.SetFocus()
            except Exception:
                rect = element.CachedBoundingRectangle
                winapi.click((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
            time.sleep(0.15)
            winapi.press("ctrl+a")
            time.sleep(0.05)
            winapi.type_unicode(text)
        if submit:
            time.sleep(0.15)
            winapi.press("enter")

    # ---- public calls, each hopping to the worker thread
    def read(self, hwnd: int, find: str = "") -> dict[str, Any]:
        return self.worker.call(lambda: self._read(hwnd, find), timeout=25)

    def describe(self, ref: int) -> dict[str, Any] | None:
        control = next((item for item in self.controls if item.ref == ref), None)
        return {"ref": control.ref, "role": control.role, "name": control.name, "password": control.password} if control else None

    def click(self, ref: int) -> str:
        return self.worker.call(lambda: self._click(self._control(ref)), timeout=15)

    def type(self, ref: int, text: str, submit: bool = False) -> None:
        self.worker.call(lambda: self._type(self._control(ref), text, submit), timeout=max(15, len(text) // 20))

    def text_of(self, ref: int) -> str | None:
        return self.worker.call(lambda: self._text_of(self._control(ref)), timeout=10)


session = Automation()
