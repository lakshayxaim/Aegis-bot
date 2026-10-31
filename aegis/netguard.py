"""Offline enforcement.

While offline mode is on, this process cannot open a connection to anything except
loopback (Ollama and its own UI). This is enforced by an interpreter audit hook, so it
covers every Python library rather than relying on each tool to behave. Scripts run by
``run_python`` get the same guard through ``CHILD_PRELUDE``.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import sys
import threading
from collections import deque
from typing import Any

logger = logging.getLogger("aegis.net")

_enabled = True
_installed = False
_lock = threading.Lock()
blocked_total = 0
blocked_recent: deque[dict[str, Any]] = deque(maxlen=20)


class OfflineBlocked(PermissionError):
    pass


def _is_local(host: Any) -> bool:
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode(errors="replace")
    text = str(host).strip().lower().strip("[]")
    if text in {"", "localhost"} or text.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(text.split("%")[0]).is_loopback
    except ValueError:
        return False


def _block(host: Any, port: Any) -> None:
    global blocked_total
    with _lock:
        blocked_total += 1
        blocked_recent.appendleft({"host": str(host), "port": port})
    logger.warning("blocked outbound connection to %s:%s", host, port)
    raise OfflineBlocked(f"Aegis is in offline mode, so the connection to {host} was blocked. Turn on Web access in Settings to allow it.")


def _hook(event: str, args: tuple) -> None:
    if not _enabled or not event.startswith("socket."):
        return
    if event == "socket.connect" or event == "socket.sendto":
        address = args[1]
        if isinstance(address, tuple) and address and not _is_local(address[0]):
            _block(address[0], address[1] if len(address) > 1 else None)
    elif event == "socket.getaddrinfo":
        if not _is_local(args[0]):
            _block(args[0], args[1])
    elif event in {"socket.gethostbyname", "socket.gethostbyaddr", "socket.gethostbyname_ex"}:
        if not _is_local(args[0]):
            _block(args[0], None)


def install(offline: bool = True) -> None:
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True
    set_offline(offline)


def set_offline(offline: bool) -> None:
    global _enabled
    _enabled = bool(offline)
    value = "1" if offline else "0"
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        os.environ[name] = value
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"


def is_offline() -> bool:
    return _enabled


def status() -> dict[str, Any]:
    return {"offline": _enabled, "blocked_total": blocked_total, "blocked_recent": list(blocked_recent)}


CHILD_PRELUDE = '''\
import sys as _s, ipaddress as _i
def _aegis_local(h, _ip=_i.ip_address):
    if h is None: return True
    if isinstance(h, (bytes, bytearray)): h = bytes(h).decode(errors="replace")
    h = str(h).strip().lower().strip("[]")
    if h in ("", "localhost"): return True
    try: return _ip(h.split("%")[0]).is_loopback
    except ValueError: return False
def _aegis_hook(e, a):
    if not e.startswith("socket."): return
    h = None
    if e in ("socket.connect", "socket.sendto") and isinstance(a[1], tuple) and a[1]: h = a[1][0]
    elif e in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.gethostbyname_ex"): h = a[0]
    else: return
    if not _aegis_local(h): raise PermissionError("Blocked: Aegis offline mode does not allow network access (" + str(h) + ")")
_s.addaudithook(_aegis_hook)
del _s, _i
'''
