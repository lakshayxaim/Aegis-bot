"""Finding, reading, writing and organising files. Every change can be undone."""
from __future__ import annotations

import datetime as dt
import fnmatch
import logging
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

import send2trash

from aegis import checks, config, paths
from aegis.toolkit import DANGER, WRITE, Approval, Result, ToolError, arg, resolve_input, resolve_output, tool
from aegis.tools.desktop import _fmt_bytes

logger = logging.getLogger("aegis.files")

_TYPE_GROUPS = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".heic", ".svg", ".tiff"},
    "video": {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv"},
    "audio": {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma"},
    "document": {".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".odt", ".pptx", ".ppt", ".xlsx", ".xls", ".csv"},
    "pdf": {".pdf"},
    "spreadsheet": {".xlsx", ".xls", ".csv", ".tsv"},
    "presentation": {".pptx", ".ppt"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "code": {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".html", ".css", ".json", ".yaml", ".yml", ".sh", ".ps1"},
    "installer": {".exe", ".msi"},
}
_CATEGORY_FOLDERS = {"image": "Images", "video": "Videos", "audio": "Audio", "document": "Documents", "archive": "Archives", "code": "Code", "installer": "Installers"}


def _when(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp).strftime("%d %b %Y %H:%M")


@tool("list_dir", "List what is in a folder, optionally sorted by size or date. Folder names like desktop, downloads and documents work.", group="Files", doing="Looking in a folder",
      keywords="list folder directory contents files biggest largest newest recent latest show inside downloads desktop documents pictures videos",
      path=arg("string", "Folder", required=True),
      sort=arg("string", "Sort order", enum=["name", "size", "modified"]),
      limit=arg("integer", "Maximum entries"),
      pattern=arg("string", "Only names matching, e.g. *.pdf"))
def list_dir(ctx, path: str, sort: str = "name", limit: int = 40, pattern: str = "") -> Result:
    folder = resolve_input(path)
    if folder.is_file():
        folder = folder.parent
    entries: list[dict[str, Any]] = []
    with os.scandir(folder) as scan:
        for entry in scan:
            if pattern and not fnmatch.fnmatch(entry.name.lower(), pattern.lower()):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            is_dir = entry.is_dir()
            entries.append({"name": entry.name, "is_dir": is_dir, "size": 0 if is_dir else stat.st_size, "modified": stat.st_mtime})
    key = {"name": lambda e: (not e["is_dir"], e["name"].lower()), "size": lambda e: -e["size"], "modified": lambda e: -e["modified"]}[sort]
    entries.sort(key=key)
    shown = entries[:max(1, min(limit, 200))]
    lines = [f"{entry['name']}{'/' if entry['is_dir'] else ''}  {'' if entry['is_dir'] else _fmt_bytes(entry['size'])}  {_when(entry['modified'])}" for entry in shown]
    extra = f" (showing {len(shown)})" if len(shown) < len(entries) else ""
    return Result(f"{paths.pretty(folder)} holds {len(entries)} item(s){extra}. Full path: {folder}", "\n".join(lines))


@tool("find_files", "Find files by name or type across the user's folders. Use this first when you do not know where a file is.", group="Files", doing="Searching for files",
      keywords="find search locate where look file named resume document pdf photo latest recent",
      query=arg("string", "Words from the name, or a pattern like *.pdf. May be empty if type is given."),
      folder=arg("string", "Limit to this folder"),
      type=arg("string", "Only this kind", enum=sorted(_TYPE_GROUPS)),
      days=arg("integer", "Only files changed in the last N days"),
      limit=arg("integer", "Maximum results"))
def find_files(ctx, query: str = "", folder: str = "", type: str = "", days: int = 0, limit: int = 12) -> Result:
    if not query and not type and not days and not folder:
        raise ToolError("Give words from the file name, a type, or a number of days.")
    roots = [resolve_input(folder)] if folder else paths.search_roots()
    extensions = _TYPE_GROUPS.get(type, set())
    cutoff = time.time() - days * 86400 if days else 0
    pattern = query.strip().lower()
    wildcard = any(char in pattern for char in "*?")
    words = [word for word in re.split(r"[\s_.-]+", pattern) if word and word not in {"my", "the", "file", "a"}]
    results: list[tuple[float, float, str, int]] = []
    scanned = 0
    for entry in paths.walk_files(roots):
        scanned += 1
        name = entry.name.lower()
        if extensions and Path(name).suffix not in extensions:
            continue
        if wildcard:
            if not fnmatch.fnmatch(name, pattern):
                continue
            score = 1.0
        elif words:
            stem = Path(name).stem
            hits = sum(word in name for word in words)
            if hits < len(words):
                continue
            score = 3.0 if stem == pattern else 2.0 if stem.startswith(words[0]) else 1.0
        else:
            score = 1.0
        try:
            stat = entry.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            continue
        results.append((score, stat.st_mtime, entry.path, stat.st_size))
    results.sort(key=lambda item: (item[0], item[1]), reverse=True)
    top = results[:max(1, min(limit, 50))]
    if not top:
        return Result(f"No files matched after checking {scanned} files.", "Try fewer words, or ask the user where it is.")
    lines = [f"{path}  {_fmt_bytes(size)}  {_when(mtime)}" for _, mtime, path, size in top]
    return Result(f"Found {len(results)} match(es), best first:", "\n".join(lines))


@tool("file_info", "Get a file or folder's size and dates.", group="Files", doing="Checking a file", keywords="info size date modified details properties how big",
      path=arg("string", "File or folder", required=True))
def file_info(ctx, path: str) -> Result:
    target = resolve_input(path)
    stat = target.stat()
    if target.is_dir():
        total = count = 0
        for entry in paths.walk_files([target], max_seconds=6, max_depth=30):
            try:
                total += entry.stat().st_size
                count += 1
            except OSError:
                continue
        return Result(f"{target} is a folder with {count} files, {_fmt_bytes(total)} in total. Modified {_when(stat.st_mtime)}.")
    return Result(f"{target} is {_fmt_bytes(stat.st_size)}. Modified {_when(stat.st_mtime)}, created {_when(stat.st_ctime)}.")


@tool("folder_usage", "See what is taking up space inside a folder: its largest files and subfolders.", group="Files", doing="Measuring folder sizes",
      keywords="space usage taking biggest largest disk clean up storage heavy size",
      path=arg("string", "Folder", required=True), limit=arg("integer", "Results to show"))
def folder_usage(ctx, path: str, limit: int = 10) -> Result:
    folder = resolve_input(path)
    if not folder.is_dir():
        raise ToolError(f"{folder} is not a folder.")
    files: list[tuple[int, str]] = []
    subs: dict[str, int] = {}
    total = 0
    for entry in paths.walk_files([folder], max_seconds=12, max_depth=30, max_entries=400_000):
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        total += size
        files.append((size, entry.path))
        relative = Path(entry.path).relative_to(folder).parts
        if len(relative) > 1:
            subs[relative[0]] = subs.get(relative[0], 0) + size
    files.sort(reverse=True)
    biggest = "\n".join(f"{_fmt_bytes(size)}  {path}" for size, path in files[:limit])
    folders = "\n".join(f"{_fmt_bytes(size)}  {name}/" for name, size in sorted(subs.items(), key=lambda item: item[1], reverse=True)[:limit])
    return Result(f"{paths.pretty(folder)} uses {_fmt_bytes(total)} across {len(files)} files.", f"Largest files:\n{biggest}\n\nLargest subfolders:\n{folders or '(none)'}")


def _gate_write(ctx, path: str = "", content: str = "", append: bool = False) -> Approval | None:
    target = paths.expand(path)
    if not target.is_absolute():
        target = paths.default_output_dir() / target
    # Saying the file's name is already the permission, and Undo restores it either way.
    if target.exists() and not append and target.name.lower() not in (ctx.request or "").lower():
        return Approval(f"Replace {target.name}?", "Its contents will be replaced. Undo brings them back.", code=content[:1500], language="text", undoable=True)
    return None


_bad_saves: dict[tuple[str | None, str], int] = {}


def _backup(path: Path) -> str:
    config.UNDO_DIR.mkdir(parents=True, exist_ok=True)
    backup = config.UNDO_DIR / f"{int(time.time() * 1000)}_{path.name}"
    shutil.copy2(path, backup)
    return str(backup)


@tool("write_file", "Create a text file or replace one's contents. Use create_document for Word or PDF.", risk=WRITE, terminal=True, group="Files", gate=_gate_write, doing="Writing a file",
      keywords="create write save new file text note notes txt markdown append add log",
      path=arg("string", "Where, e.g. desktop/notes.txt", required=True), content=arg("string", "The full text", required=True), append=arg("boolean", "Add to the end instead of replacing"))
def write_file(ctx, path: str, content: str, append: bool = False) -> Result:
    target = resolve_output(path, default_name="note.txt", overwrite=True)
    if checks.is_code(target) and not append:
        content = checks.tidy_code(target, content)
        problem = checks.check_code(target, content)
        attempts = _bad_saves.get((ctx.run_id, str(target)), 0)
        if problem and attempts < 2:
            _bad_saves[(ctx.run_id, str(target))] = attempts + 1
            raise ToolError(f"{target.name} was not saved, because the code has errors:\n{problem}", "Fix those errors and call write_file again with the whole corrected file.")
    elif not append and checks.looks_like_filler(content, ctx.request):
        raise ToolError("That text describes a document instead of being one, so nothing was saved.", "Get the real content first (research reads the web), then save it.")
    existed = target.exists()
    undo: dict[str, Any]
    if existed:
        undo = {"kind": "restore_backup", "path": str(target), "backup": _backup(target)}
    else:
        undo = {"kind": "delete_created", "path": str(target)}
    if append and existed:
        with target.open("a", encoding="utf-8", newline="") as handle:
            handle.write(content)
    else:
        target.write_text(content, encoding="utf-8", newline="")
    stored = target.read_text(encoding="utf-8")
    if (append and not stored.endswith(content)) or (not append and stored != content):
        raise ToolError(f"{target.name} did not read back correctly after writing.")
    verb = "Added to" if append and existed else "Updated" if existed else "Created"
    return Result(f"{verb} {paths.pretty(target)}.", files=[target], verified=f"Read it back: {len(stored)} characters match.", undo=undo)


@tool("make_folder", "Create a new folder.", risk=WRITE, terminal=True, group="Files", doing="Creating a folder", keywords="create new make folder directory mkdir",
      path=arg("string", "Folder to create, e.g. desktop/Receipts", required=True))
def make_folder(ctx, path: str) -> Result:
    target = paths.expand(path)
    if not target.is_absolute():
        target = paths.folder("documents") / target
    if paths.is_protected(target):
        raise ToolError(f"{target} is inside a protected system folder.")
    if target.is_dir():
        return Result(f"{paths.pretty(target)} already exists.", verified="The folder is there.")
    target.mkdir(parents=True)
    return Result(f"Created the folder {paths.pretty(target)}.", verified="The folder exists on disk.", undo={"kind": "remove_empty_dir", "path": str(target)})


def _destination(source: Path, raw: str) -> Path:
    target = paths.expand(raw)
    if not target.is_absolute():
        # A bare new name is a rename in place; anything else is relative to the source's folder.
        target = source.parent / target
    if target.is_dir() or raw.rstrip().endswith(("/", "\\")):
        target = target / source.name
    elif not target.suffix and source.suffix and source.is_file():
        target = target.with_suffix(source.suffix)
    if paths.is_protected(target):
        raise ToolError(f"{target.parent} is a protected system folder.")
    return target


def _gate_transfer(ctx, source: str = "", destination: str = "") -> Approval | None:
    try:
        src = paths.expand(source)
        target = _destination(src, destination)
    except (ToolError, OSError):
        return None
    if target.exists() and src.exists() and target.resolve() != src.resolve():
        return Approval(f"Replace {target.name}?", f"{target} already exists and would be replaced by {src}.", undoable=True)
    return None


@tool("copy_path", "Copy a file or folder.", risk=WRITE, terminal=True, group="Files", gate=_gate_transfer, doing="Copying", keywords="copy duplicate backup file folder to",
      source=arg("string", "What to copy", required=True), destination=arg("string", "Where to put it", required=True))
def copy_path(ctx, source: str, destination: str) -> Result:
    src = resolve_input(source)
    target = _destination(src, destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    undo: dict[str, Any] = {"kind": "restore_backup", "path": str(target), "backup": _backup(target)} if target.is_file() else {"kind": "delete_created", "path": str(target)}
    if src.is_dir():
        shutil.copytree(src, target, dirs_exist_ok=True)
    else:
        shutil.copy2(src, target)
        if target.stat().st_size != src.stat().st_size:
            raise ToolError("The copy is a different size from the original.")
    return Result(f"Copied {src.name} to {paths.pretty(target)}.", files=[target] if target.is_file() else [], verified="The copy exists and its size matches.", undo=undo)


def _gate_rename(ctx, path: str = "", new_name: str = "") -> Approval | None:
    source = paths.expand(path)
    target = source.with_name(Path(new_name).name)
    if target.exists() and source.exists() and target != source:
        return Approval(f"Replace {target.name}?", f"{target} already exists and would be replaced.")
    return None


@tool("rename_path", "Rename a file or folder, keeping it in the same place.", risk=WRITE, terminal=True, group="Files", gate=_gate_rename, doing="Renaming", keywords="rename name call change title file folder",
      path=arg("string", "The file or folder", required=True), new_name=arg("string", "Just the new name, e.g. final.txt", required=True))
def rename_path(ctx, path: str, new_name: str) -> Result:
    source = resolve_input(path)
    name = Path(new_name.strip().strip("\"'")).name
    if not name:
        raise ToolError("The new name is empty.")
    if not Path(name).suffix and source.suffix and source.is_file():
        name += source.suffix
    target = source.with_name(name)
    if target == source:
        return Result(f"{source.name} already has that name.")
    backup = _backup(target) if target.is_file() else None
    source.replace(target)
    if not target.exists() or source.exists():
        raise ToolError("The rename did not complete.")
    return Result(f"Renamed {source.name} to {name}.", verified="The new name exists and the old one is gone.", files=[target] if target.is_file() else [],
                  undo={"kind": "move_back", "from": str(target), "to": str(source), "backup": backup, "backup_for": str(target) if backup else None})


@tool("move_path", "Move a file or folder to another folder.", risk=WRITE, terminal=True, group="Files", gate=_gate_transfer, doing="Moving", keywords="move relocate put transfer file folder to into",
      source=arg("string", "What to move", required=True), destination=arg("string", "New location or new name", required=True))
def move_path(ctx, source: str, destination: str) -> Result:
    src = resolve_input(source)
    target = _destination(src, destination)
    if src == target:
        return Result(f"{src.name} is already there.")
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup(target) if target.is_file() else None
    shutil.move(str(src), str(target))
    if not target.exists() or src.exists():
        raise ToolError(f"The move did not complete: {target} is missing or {src} is still there.")
    undo = {"kind": "move_back", "from": str(target), "to": str(src), "backup": backup, "backup_for": str(target) if backup else None}
    verb = "Renamed" if src.parent == target.parent else "Moved"
    return Result(f"{verb} {src.name} to {paths.pretty(target)}.", files=[target] if target.is_file() else [], verified="It is at the new location and gone from the old one.", undo=undo)


def _gate_delete(ctx, path: str = "") -> Approval | None:
    target = paths.expand(path)
    detail = "It goes to the Recycle Bin, so it can be restored."
    if target.is_dir():
        count = sum(1 for _ in paths.walk_files([target], max_seconds=1, max_depth=20))
        detail = f"This folder holds at least {count} files. " + detail
    return Approval(f"Move {target.name} to the Recycle Bin?", detail, undoable=True)


@tool("delete_path", "Move a file or folder to the Recycle Bin.", risk=DANGER, terminal=True, group="Files", gate=_gate_delete, doing="Moving to the Recycle Bin",
      keywords="delete remove trash recycle bin erase get rid", path=arg("string", "File or folder", required=True))
def delete_path(ctx, path: str) -> Result:
    target = resolve_input(path)
    if paths.is_protected(target) or target in paths.known_folders().values():
        raise ToolError(f"{target} is protected, so Aegis will not delete it.")
    send2trash.send2trash(str(target))
    if target.exists():
        raise ToolError(f"{target.name} is still there. It may be open in another app.")
    return Result(f"Moved {target.name} to the Recycle Bin.", verified="It is gone from its folder.", undo={"kind": "restore_recycled", "path": str(target)})


@tool("search_in_files", "Search inside text files in a folder for a word or phrase.", group="Files", doing="Searching inside files",
      keywords="search inside contents grep contain mention word phrase text in files code",
      query=arg("string", "What to look for", required=True), folder=arg("string", "Folder to search; omit for Desktop, Documents and Downloads"), limit=arg("integer", "Maximum matches"))
def search_in_files(ctx, query: str, folder: str = "", limit: int = 20) -> Result:
    if not folder:
        known = paths.known_folders()
        found = [search_in_files(ctx, query, str(known[name]), limit) for name in ("desktop", "documents", "downloads") if name in known and known[name].exists()]
        hits = [item for item in found if item.data]
        if not hits:
            return Result(f"Nothing on the Desktop or in Documents or Downloads contains '{query}'.", "Try another word, or find_files to search by file name.")
        return Result("Found matching lines:", "\n".join(str(item.data) for item in hits)[:6000])
    root = resolve_input(folder)
    needle = query.lower()
    rg = shutil.which("rg")
    lines: list[str] = []
    if rg:
        done = subprocess.run([rg, "--no-heading", "-n", "-i", "-F", "--max-columns", "220", "-m", "5", "--", query, str(root)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        lines = done.stdout.splitlines()[:limit]
    else:
        for entry in paths.walk_files([root], max_seconds=15, max_depth=30):
            if len(lines) >= limit:
                break
            try:
                if entry.stat().st_size > 2_000_000 or Path(entry.name).suffix.lower() in {".exe", ".dll", ".png", ".jpg", ".zip", ".pdf", ".mp4"}:
                    continue
                with open(entry.path, "r", encoding="utf-8", errors="ignore") as handle:
                    for number, line in enumerate(handle, 1):
                        if needle in line.lower():
                            lines.append(f"{entry.path}:{number}:{line.strip()[:200]}")
                            if len(lines) >= limit:
                                break
            except OSError:
                continue
    if not lines:
        return Result(f"Nothing under {paths.pretty(root)} contains '{query}'.")
    return Result(f"Found {len(lines)} matching line(s):", "\n".join(lines))


def _gate_organize(ctx, path: str = "", by: str = "type", preview: bool = False) -> Approval | None:
    if preview:
        return None
    return Approval(f"Sort everything in {paths.expand(path).name or path} into subfolders by {by}?", "Files are moved, not changed.", undoable=True)


@tool("organize_folder", "Tidy a folder by moving its loose files into subfolders by type or by month.", risk=WRITE, terminal=True, group="Files", gate=_gate_organize, doing="Organising a folder",
      keywords="organize organise tidy clean sort arrange group folder files downloads desktop mess",
      path=arg("string", "Folder", required=True), by=arg("string", "How to group", enum=["type", "month"]), preview=arg("boolean", "Report the plan without moving anything"))
def organize_folder(ctx, path: str, by: str = "type", preview: bool = False) -> Result:
    folder = resolve_input(path)
    if not folder.is_dir():
        raise ToolError(f"{folder} is not a folder.")
    plan: list[tuple[Path, Path]] = []
    for entry in folder.iterdir():
        if not entry.is_file() or entry.name.lower() in {"desktop.ini", "thumbs.db"} or entry.suffix.lower() == ".lnk":
            continue
        if by == "month":
            group = dt.datetime.fromtimestamp(entry.stat().st_mtime).strftime("%Y-%m")
        else:
            group = next((_CATEGORY_FOLDERS[name] for name, extensions in _TYPE_GROUPS.items() if name in _CATEGORY_FOLDERS and entry.suffix.lower() in extensions), "Other")
        plan.append((entry, paths.unique(folder / group / entry.name)))
    counts: dict[str, int] = {}
    for _, target in plan:
        counts[target.parent.name] = counts.get(target.parent.name, 0) + 1
    breakdown = ", ".join(f"{count} to {name}" for name, count in sorted(counts.items()))
    if preview or not plan:
        return Result(f"{'Plan for' if plan else 'Nothing to tidy in'} {paths.pretty(folder)}{': ' + breakdown if plan else ''}.")
    moved = []
    for source, target in plan:
        target.parent.mkdir(exist_ok=True)
        shutil.move(str(source), str(target))
        moved.append({"from": str(target), "to": str(source)})
    missing = [item for item in moved if not Path(item["from"]).exists()]
    if missing:
        raise ToolError(f"{len(missing)} file(s) did not arrive where expected.")
    return Result(f"Organised {len(moved)} files in {paths.pretty(folder)}: {breakdown}.", verified="Every file is at its new location.", undo={"kind": "move_many", "moves": moved})


@tool("compress", "Put files or a folder into a .zip archive.", risk=WRITE, terminal=True, group="Files", doing="Compressing", keywords="zip compress archive pack bundle",
      paths_to_zip=arg("array", "Files or folders to include", required=True), destination=arg("string", "Zip file to create"))
def compress(ctx, paths_to_zip: list[str], destination: str = "") -> Result:
    sources = [resolve_input(item) for item in paths_to_zip]
    name = (sources[0].stem if len(sources) == 1 else "Archive") + ".zip"
    target = resolve_output(destination or str(sources[0].parent / name), default_name=name)
    if target.suffix.lower() != ".zip":
        target = target.with_suffix(".zip")
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for source in sources:
            if source.is_dir():
                for item in source.rglob("*"):
                    if item.is_file():
                        archive.write(item, item.relative_to(source.parent))
                        count += 1
            else:
                archive.write(source, source.name)
                count += 1
    with zipfile.ZipFile(target) as check:
        if check.testzip() is not None:
            raise ToolError("The archive failed its integrity check.")
    return Result(f"Compressed {count} file(s) into {paths.pretty(target)} ({_fmt_bytes(target.stat().st_size)}).", files=[target], verified="Zip integrity check passed.", undo={"kind": "delete_created", "path": str(target)})


@tool("extract_archive", "Unpack a .zip archive.", risk=WRITE, terminal=True, group="Files", doing="Extracting", keywords="unzip extract unpack archive zip decompress",
      path=arg("string", "The .zip file", required=True), destination=arg("string", "Folder to extract into"))
def extract_archive(ctx, path: str, destination: str = "") -> Result:
    source = resolve_input(path)
    if not zipfile.is_zipfile(source):
        raise ToolError(f"{source.name} is not a zip archive.")
    target = paths.expand(destination) if destination else paths.unique(source.parent / source.stem)
    if not target.is_absolute():
        target = source.parent / target
    if paths.is_protected(target):
        raise ToolError(f"{target} is a protected system folder.")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        for member in archive.namelist():
            if not (target / member).resolve().is_relative_to(target.resolve()):
                raise ToolError("This archive tries to write outside its folder, so it was not extracted.")
        archive.extractall(target)
        count = len(archive.namelist())
    return Result(f"Extracted {count} item(s) to {paths.pretty(target)}.", verified=f"{target.name} now holds the contents.")


# ---------------------------------------------------------------------- undo
def apply_undo(kind: str, payload: dict[str, Any], store: Any = None) -> str:
    """Reverse a recorded change. Returns what was done, or raises ToolError."""
    if kind == "cancel_reminder":
        store.set_reminder(payload["id"], status="cancelled")
        return "Cancelled that reminder."
    if kind == "delete_created":
        target = Path(payload["path"])
        if not target.exists():
            return f"{target.name} was already gone."
        send2trash.send2trash(str(target))
        return f"Moved {target.name} to the Recycle Bin."
    if kind == "restore_backup":
        target, backup = Path(payload["path"]), Path(payload["backup"])
        if not backup.exists():
            raise ToolError("The backup for this change no longer exists.")
        shutil.copy2(backup, target)
        return f"Restored the previous version of {target.name}."
    if kind == "move_back":
        source, destination = Path(payload["from"]), Path(payload["to"])
        if destination.exists():
            raise ToolError(f"{destination} exists again, so the move cannot be reversed automatically.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        if payload.get("backup") and payload.get("backup_for"):
            shutil.copy2(payload["backup"], payload["backup_for"])
        return f"Moved {destination.name} back."
    if kind == "move_many":
        created: set[Path] = set()
        for move in payload["moves"]:
            source, destination = Path(move["from"]), Path(move["to"])
            if source.exists() and not destination.exists():
                shutil.move(str(source), str(destination))
                created.add(source.parent)
        for folder in created:
            try:
                folder.rmdir()
            except OSError:
                pass
        return f"Moved {len(payload['moves'])} files back."
    if kind == "remove_empty_dir":
        target = Path(payload["path"])
        try:
            target.rmdir()
        except OSError as exc:
            raise ToolError(f"{target.name} is no longer empty, so it was left in place.") from exc
        return f"Removed the folder {target.name}."
    if kind == "restore_recycled":
        return _restore_recycled(Path(payload["path"]))
    if kind == "volume":
        from aegis.tools.desktop import _endpoint
        endpoint = _endpoint()
        if "mute" in payload:
            endpoint.SetMute(1 if payload["mute"] else 0, None)
            return "Restored the mute setting."
        endpoint.SetMasterVolumeLevelScalar(int(payload["level"]) / 100, None)
        return f"Set the volume back to {payload['level']}%."
    raise ToolError(f"Aegis does not know how to reverse a '{kind}' change.")


def _restore_recycled(original: Path) -> str:
    # Explorer hides known extensions, so an item's Name can be the stem only.
    script = (
        "$target=$env:AEGIS_RESTORE;"
        "$folder=[IO.Path]::GetDirectoryName($target);"
        "$leaf=[IO.Path]::GetFileName($target);"
        "$stem=[IO.Path]::GetFileNameWithoutExtension($target);"
        "$match=$null;"
        "foreach($i in (New-Object -ComObject Shell.Application).NameSpace(10).Items()){"
        "$from=[string]$i.ExtendedProperty('System.Recycle.DeletedFrom');"
        "if(-not $from){ continue };"
        "if($from.TrimEnd('\\') -ine $folder.TrimEnd('\\')){ continue };"
        "if($i.Name -ieq $leaf){ $match=$i; break };"
        "if($stem -and $i.Name -ieq $stem){ $match=$i } };"
        "if($match){ $match.InvokeVerb('undelete'); Write-Output 'ok' }"
    )
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script], capture_output=True, text=True, timeout=30, env={**os.environ, "AEGIS_RESTORE": str(original)}, creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(50):
        if original.exists():
            return f"Restored {original.name} from the Recycle Bin."
        time.sleep(0.1)
    raise ToolError(f"Could not find {original.name} in the Recycle Bin. {done.stderr.strip()[:150]}")


@tool("undo_last", "Reverse the most recent change Aegis made, such as a move, rename, overwrite or delete.", risk=WRITE, terminal=True, group="Files", doing="Undoing the last change",
      keywords="undo revert reverse back mistake restore oops put it back")
def undo_last(ctx) -> Result:
    entry = ctx.store.last_undo()
    if entry is None:
        return Result("There is nothing to undo.")
    message = apply_undo(entry["kind"], entry["payload"], ctx.store)
    ctx.store.mark_undone(entry["id"])
    return Result(message, verified=f"Reversed: {entry.get('label') or entry['kind']}.")
