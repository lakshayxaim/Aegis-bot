"""Turn whatever a person (or a small model) calls a place into a real Windows path."""
from __future__ import annotations

import ctypes
import os
import re
import tempfile
import time
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path

_FOLDER_IDS = {
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
}
_ALIASES = {
    "desktop": "desktop", "documents": "documents", "docs": "documents", "document": "documents", "my documents": "documents",
    "downloads": "downloads", "download": "downloads", "pictures": "pictures", "photos": "pictures", "images": "pictures",
    "videos": "videos", "video": "videos", "movies": "videos", "music": "music", "home": "home", "user": "home",
}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "appdata", "$recycle.bin", "system volume information", ".cache", "dist", "build", ".next", "site-packages"}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _known_folder(guid: str) -> Path | None:
    try:
        parsed = _GUID()
        ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(guid), ctypes.byref(parsed))
        buffer = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(parsed), 0, None, ctypes.byref(buffer)) == 0:
            value = buffer.value
            ctypes.windll.ole32.CoTaskMemFree(buffer)
            return Path(value) if value else None
    except (OSError, AttributeError):
        return None
    return None


@lru_cache(maxsize=None)
def known_folders() -> dict[str, Path]:
    """The user's real folders, which may have been redirected into OneDrive."""
    folders: dict[str, Path] = {"home": Path.home()}
    for name, guid in _FOLDER_IDS.items():
        folders[name] = _known_folder(guid) or Path.home() / name.capitalize()
    return folders


def folder(name: str) -> Path:
    return known_folders()[name]


def default_output_dir(kind: str = "documents") -> Path:
    base = {
        "screenshot": folder("pictures") / "Screenshots",
        "recording": folder("videos") / "Aegis Recordings",
        "audio": folder("music") / "Aegis Recordings",
        "chart": folder("pictures") / "Aegis Charts",
        "documents": folder("documents") / "Aegis",
    }.get(kind, folder("documents") / "Aegis")
    base.mkdir(parents=True, exist_ok=True)
    return base


def search_roots() -> list[Path]:
    roots = [folder(name) for name in ("desktop", "documents", "downloads", "pictures", "videos", "music")]
    onedrive = os.environ.get("OneDrive")
    if onedrive and Path(onedrive).is_dir():
        roots.append(Path(onedrive))
    projects = Path.home() / "Projects"
    if projects.is_dir():
        roots.append(projects)
    seen: dict[str, Path] = {}
    for root in roots:
        seen.setdefault(str(root).lower(), root)
    return list(seen.values())


_HOME_PATTERN = re.compile(r"^(?:[a-zA-Z]:)?[\\/]+(?:users|home)[\\/]+([^\\/]+)(?:[\\/]+(.*))?$", re.IGNORECASE)


def expand(raw: str) -> Path:
    """Best-effort interpretation of a path, without touching the disk."""
    text = (raw or "").strip().strip("\"'`").strip()
    text = os.path.expandvars(text.replace("~", str(Path.home()), 1) if text.startswith("~") else text)
    text = re.sub(r"^(?:\./|\.\\)+", "", text)
    # "my desktop", "the Downloads folder/x.txt"
    cleaned = re.sub(r"^(?:my|the)\s+", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"^([\w ]+?)\s+(?:folder|directory)(?=$|[\\/])", r"\1", cleaned, flags=re.IGNORECASE)
    head, _, tail = cleaned.replace("\\", "/").partition("/")
    alias = _ALIASES.get(head.strip().lower())
    if alias:
        base = known_folders()[alias]
        return base / tail if tail else base
    # A model that guessed the home directory: C:/Users/user/Desktop, /home/me/Downloads...
    match = _HOME_PATTERN.match(text)
    if match and match.group(1).lower() != Path.home().name.lower():
        rest = (match.group(2) or "").replace("\\", "/")
        head, _, tail = rest.partition("/")
        alias = _ALIASES.get(head.strip().lower())
        if alias:
            base = known_folders()[alias]
            return base / tail if tail else base
        return Path.home() / rest if rest else Path.home()
    path = Path(text)
    return path if path.is_absolute() else Path(text)


def walk_files(roots: list[Path], *, max_entries: int = 120_000, max_seconds: float = 4.0, max_depth: int = 7):
    """Yield DirEntry objects for files under roots, skipping noise, within a time budget."""
    deadline = time.monotonic() + max_seconds
    seen = 0
    stack = [(root, 0) for root in roots]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen > max_entries or time.monotonic() > deadline:
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if depth < max_depth and entry.name.lower() not in SKIP_DIRS and not entry.name.startswith("."):
                                stack.append((Path(entry.path), depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            yield entry
                    except OSError:
                        continue
        except OSError:
            continue


def find_by_name(name: str, roots: list[Path] | None = None, limit: int = 8) -> list[Path]:
    """Files whose name matches, best first. Used to recover from a slightly wrong path."""
    target = Path(name).name.lower()
    stem = Path(target).stem
    exact: list[tuple[float, Path]] = []
    close: list[tuple[float, Path]] = []
    for entry in walk_files(roots or search_roots(), max_seconds=2.5):
        lowered = entry.name.lower()
        if lowered == target:
            exact.append((entry.stat().st_mtime, Path(entry.path)))
        elif stem and stem in lowered and (not Path(target).suffix or lowered.endswith(Path(target).suffix)):
            close.append((entry.stat().st_mtime, Path(entry.path)))
    chosen = exact or close
    chosen.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in chosen[:limit]]


def _protected_roots() -> list[Path]:
    roots = []
    for name in ("WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value))
    return roots


def is_protected(path: Path) -> bool:
    """System locations Aegis will not write into through its file tools."""
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return True
    return any(_under(resolved, root) for root in _protected_roots())


_SENSITIVE_PARTS = {".ssh", ".aws", ".gnupg", ".azure", ".kube"}
_SENSITIVE_NAMES = re.compile(r"^(?:\.env(?:\..*)?|id_rsa|id_ed25519|credentials(?:\.json)?|.*\.(?:pem|pfx|key|kdbx))$", re.IGNORECASE)
_SENSITIVE_DIRS = re.compile(r"(?:Google[\\/]Chrome[\\/]User Data|Microsoft[\\/]Edge[\\/]User Data|Mozilla[\\/]Firefox[\\/]Profiles|Microsoft[\\/](?:Credentials|Protect|Vault))", re.IGNORECASE)


def is_sensitive(path: Path) -> bool:
    """Likely credentials: reading these always needs the user's say-so."""
    text = str(path)
    return bool(set(part.lower() for part in path.parts) & _SENSITIVE_PARTS or _SENSITIVE_NAMES.match(path.name) or _SENSITIVE_DIRS.search(text))


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def pretty(path: Path | str) -> str:
    """A short form for messages: Desktop/shot.png rather than the full path."""
    path = Path(path)
    best = None
    for name, base in known_folders().items():
        if _under(path, base) and (best is None or len(str(base)) > len(str(best[1]))):
            best = (name, base)
    if best:
        relative = path.resolve(strict=False).relative_to(best[1].resolve(strict=False))
        label = "Home" if best[0] == "home" else best[0].capitalize()
        return label if str(relative) == "." else f"{label}/{relative.as_posix()}"
    return str(path)


def unique(path: Path) -> Path:
    """Avoid clobbering: report.pdf -> report (2).pdf."""
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}_{int(time.time())}{path.suffix}")


def temp_file(suffix: str) -> Path:
    handle, name = tempfile.mkstemp(suffix=suffix, prefix="aegis_")
    os.close(handle)
    return Path(name)
