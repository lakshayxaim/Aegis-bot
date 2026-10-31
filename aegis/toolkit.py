"""The tool framework: declaring tools, repairing what a small model sends, picking relevant tools."""
from __future__ import annotations

import difflib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from aegis import paths

READ, WRITE, DANGER = "read", "write", "danger"


class ToolError(Exception):
    """A failure worth telling the model and the user about. ``hint`` says what to try next, and ``retry`` marks a passing fault worth trying again."""

    def __init__(self, message: str, hint: str = "", retry: bool = False) -> None:
        super().__init__(message)
        self.hint = hint
        self.retry = retry


@dataclass
class Result:
    summary: str
    data: Any = None
    files: list[Path | str] = field(default_factory=list)
    verified: str = ""  # the evidence that it really happened
    unverified: bool = False  # sent, but there is no way to prove it landed
    undo: dict[str, Any] | None = None
    needs_user: str = ""  # only the user can do the next part, such as signing in; the request pauses here
    options: list[str] = field(default_factory=list)  # ready-made answers to needs_user
    declined: bool = False  # the user said no partway through, so the request stops here

    def for_model(self, limit: int = 6000) -> str:
        text = self.summary
        if self.data not in (None, "", [], {}):
            detail = self.data if isinstance(self.data, str) else json.dumps(self.data, ensure_ascii=False, default=str)
            if len(detail) > limit:
                detail = detail[:limit] + f"\n[truncated: {len(detail) - limit} more characters]"
            text += "\n" + detail
        return text


@dataclass
class Approval:
    title: str
    detail: str = ""
    code: str = ""
    language: str = ""
    kind: str = "change"  # change: alters something here; send: reaches other people or websites; run: executes code
    undoable: bool = False


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, dict[str, Any]]
    required: list[str]
    fn: Callable[..., Result]
    risk: str = READ
    terminal: bool = False  # on success its summary can stand as the final reply
    group: str = "General"
    keywords: tuple[str, ...] = ()
    doing: str = ""
    gate: Callable[..., Approval | None] | None = None
    web: bool = False  # only offered when Web access is on
    direct: bool = False  # its text output is already the answer to the user
    refine: Callable[[dict[str, Any], str], dict[str, Any]] | None = None  # corrects arguments using the user's own words

    def schema(self, compact: bool = False) -> dict[str, Any]:
        properties = {}
        for name, spec in self.params.items():
            entry: dict[str, Any] = {"type": spec["type"]}
            if spec.get("description") and not compact:
                entry["description"] = spec["description"]
            if spec.get("enum"):
                entry["enum"] = spec["enum"]
            if spec["type"] == "array":
                entry["items"] = {"type": spec.get("items", "string")}
            properties[name] = entry
        description = self.description.split(". ")[0] if compact else self.description
        return {"type": "function", "function": {"name": self.name, "description": description, "parameters": {"type": "object", "properties": properties, "required": self.required}}}


REGISTRY: dict[str, Tool] = {}


def arg(type: str = "string", description: str = "", required: bool = False, enum: list[str] | None = None, items: str = "string") -> dict[str, Any]:
    return {"type": type, "description": description, "required": required, "enum": enum, "items": items}


def tool(tool_name: str, description: str, /, *, risk: str = READ, terminal: bool = False, group: str = "General", keywords: str = "", doing: str = "", gate: Callable[..., Approval | None] | None = None, web: bool = False, direct: bool = False, refine: Callable[[dict[str, Any], str], dict[str, Any]] | None = None, **params: dict[str, Any]):
    def register(fn: Callable[..., Result]) -> Callable[..., Result]:
        REGISTRY[tool_name] = Tool(
            name=tool_name, description=description, params=params,
            required=[key for key, spec in params.items() if spec.get("required")],
            fn=fn, risk=risk, terminal=terminal, group=group,
            keywords=tuple(keywords.split()), doing=doing or tool_name.replace("_", " ").capitalize(), gate=gate, web=web, direct=direct, refine=refine,
        )
        return fn
    return register


def available_tools(offline: bool) -> list[Tool]:
    return [item for item in REGISTRY.values() if not (item.web and offline)]


class Ctx:
    """What a running tool may reach: services, plus cooperative cancellation."""

    def __init__(self, *, settings: Any = None, store: Any = None, llm: Any = None, bus: Any = None, shell: Any = None, thread_id: str | None = None, run_id: str | None = None, item_id: str | None = None) -> None:
        self.settings, self.store, self.llm, self.bus, self.shell = settings, store, llm, bus, shell
        self.thread_id, self.run_id, self.item_id = thread_id, run_id, item_id
        self.cancelled = threading.Event()
        self.procs: list[Any] = []
        self.request = ""  # the user's own words, for gates that need to check them
        self.approver: Callable[[Approval], bool] | None = None  # asks the user partway through a tool

    def approve(self, approval: Approval) -> bool:
        """Blocks the tool until the user answers."""
        return bool(self.approver and self.approver(approval))

    def progress(self, text: str) -> None:
        if self.bus and self.item_id:
            self.bus.publish({"type": "tool_progress", "thread_id": self.thread_id, "item_id": self.item_id, "text": text})

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise ToolError("Cancelled.")


# ---------------------------------------------------------------------- input repair
_ALIASES: dict[str, tuple[str, ...]] = {
    "path": ("file_path", "filepath", "file", "filename", "file_name", "input", "input_path", "source_path", "folder", "folder_path", "directory", "dir", "location", "name"),
    "source": ("src", "from", "from_path", "source_path", "path", "input", "file", "file_path"),
    "destination": ("dest", "dst", "to", "to_path", "target", "destination_path", "output", "new_path", "new_name"),
    "query": ("q", "search", "term", "keyword", "keywords", "name", "pattern", "text"),
    "title": ("window", "window_title", "name", "app", "application"),
    "name": ("app", "application", "app_name", "program", "title"),
    "text": ("content", "message", "value", "string"),
    "content": ("text", "body", "data", "contents"),
    "code": ("script", "python", "source", "source_code"),
    "command": ("cmd", "script", "powershell"),
    "expression": ("expr", "formula", "math", "calculation", "input"),
    "fact": ("memory", "text", "content", "note", "information"),
    "message": ("text", "reminder", "title", "note", "content"),
    "url": ("link", "website", "site", "address"),
    "level": ("volume", "value", "percent", "percentage"),
}
_PLACEHOLDER = re.compile(r"^\s*(?:<[^>]*>|\{[^}]*\}|\[[^\]]*\]|none|null|undefined|n/a)\s*$", re.IGNORECASE)


def repair_args(item: Tool, raw: Any) -> tuple[dict[str, Any], str | None]:
    """Coerce what the model sent into what the tool takes, or say precisely what is wrong."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    incoming = {key: value for key, value in raw.items() if value is not None and not (isinstance(value, str) and (not value.strip() or _PLACEHOLDER.match(value)))}
    args: dict[str, Any] = {}
    for name, spec in item.params.items():
        value = incoming.pop(name, None)
        if value is None and name in _ALIASES:
            for alias in _ALIASES[name]:
                if alias not in item.params and alias in incoming:
                    value = incoming.pop(alias)
                    break
        if value is None:
            continue
        coerced, problem = _coerce(value, spec)
        if problem:
            return {}, f"Argument '{name}': {problem}"
        args[name] = coerced
    # When a tool takes exactly one required string, a lone unknown value is that value.
    missing = [name for name in item.required if name not in args]
    if len(missing) == 1 and len(incoming) == 1 and item.params[missing[0]]["type"] == "string":
        value = next(iter(incoming.values()))
        if isinstance(value, (str, int, float)):
            args[missing[0]] = str(value)
            missing = []
    if missing:
        return {}, f"Missing required argument(s): {', '.join(missing)}. Call {item.name} again with them, or ask the user if you do not know."
    return args, None


def _coerce(value: Any, spec: dict[str, Any]) -> tuple[Any, str | None]:
    expected = spec["type"]
    try:
        if expected == "integer":
            if isinstance(value, bool):
                return int(value), None
            if isinstance(value, str):
                match = re.search(r"-?\d+(?:\.\d+)?", value)
                if not match:
                    return None, f"expected a whole number, got {value!r}"
                return int(float(match.group())), None
            return int(value), None
        if expected == "number":
            if isinstance(value, str):
                match = re.search(r"-?\d+(?:\.\d+)?", value)
                if not match:
                    return None, f"expected a number, got {value!r}"
                return float(match.group()), None
            return float(value), None
        if expected == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in {"true", "yes", "1", "on"}, None
            return bool(value), None
        if expected == "array":
            if isinstance(value, str):
                stripped = value.strip()
                if stripped.startswith("["):
                    try:
                        return json.loads(stripped), None
                    except ValueError:
                        pass
                return [part.strip() for part in stripped.split(",") if part.strip()], None
            if isinstance(value, (list, tuple)):
                return list(value), None
            return [value], None
        if expected == "string":
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            enum = spec.get("enum")
            if enum and text not in enum:
                lowered = {option.lower(): option for option in enum}
                if text.strip().lower() in lowered:
                    return lowered[text.strip().lower()], None
                close = difflib.get_close_matches(text.strip().lower(), list(lowered), n=1, cutoff=0.6)
                if close:
                    return lowered[close[0]], None
                return None, f"must be one of {', '.join(enum)}"
            return text, None
    except (TypeError, ValueError) as exc:
        return None, str(exc)
    return value, None


def resolve_tool_name(name: str, offline: bool) -> Tool | None:
    """Small models sometimes get a name slightly wrong."""
    cleaned = re.sub(r"^(?:functions?|tools?)[.:]", "", (name or "").strip()).replace("-", "_").replace(".", "_").lower()
    tools = {item.name: item for item in available_tools(offline)}
    if cleaned in tools:
        return tools[cleaned]
    close = difflib.get_close_matches(cleaned, list(tools), n=1, cutoff=0.75)
    return tools[close[0]] if close else None


# ---------------------------------------------------------------------- tool selection
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "to", "of", "in", "on", "my", "me", "i", "it", "is", "at", "and", "or", "for", "this", "that", "please", "can", "you", "with", "from", "what", "whats", "how", "much", "do", "does", "be", "are", "was", "were", "have", "has", "now", "up", "as", "so", "some", "any", "all", "by", "out", "get", "make", "put", "just", "like", "bit", "want", "need", "should", "would", "could", "will", "let", "us"}
# Always offered, in this order, so wording can never hide what most work needs and the prompt cache stays valid.
CORE_TOOLS = ("research", "web_search", "web_fetch", "find_files", "search_in_files", "read_file", "write_file", "create_document", "list_dir", "open_app", "open_path", "open_url", "browser_open", "browser_read", "browser_click", "browser_type", "app_read", "app_click", "app_type", "press_keys", "send_message", "play_media", "run_python", "run_command", "ask_user", "more_tools")


def _stem(word: str) -> str:
    for suffix in ("ing", "ies", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def _tokens(text: str) -> set[str]:
    return {_stem(word) for word in _WORD.findall(text.lower()) if word not in _STOP}


_index_cache: dict[str, tuple[set[str], set[str], set[str]]] = {}


def _tool_index(item: Tool) -> tuple[set[str], set[str], set[str]]:
    cached = _index_cache.get(item.name)
    if cached is None:
        cached = (_tokens(item.name.replace("_", " ")), _tokens(" ".join(item.keywords)), _tokens(item.description))
        _index_cache[item.name] = cached
    return cached


def select_tools(query: str, *, recent: list[str] | None = None, limit: int = 8, offline: bool = True) -> list[Tool]:
    """The core set always, then whatever else fits the request. more_tools reaches everything else."""
    tools = available_tools(offline)
    by_name = {item.name: item for item in tools}
    chosen: dict[str, Tool] = {name: by_name[name] for name in CORE_TOOLS if name in by_name}
    wanted = _tokens(query)
    scored: list[tuple[float, int, Tool]] = []
    for order, item in enumerate(tools):
        if item.name in chosen:
            continue
        name_words, keywords, description = _tool_index(item)
        score = 4 * len(wanted & name_words) + 3 * len(wanted & keywords) + len(wanted & description)
        scored.append((score, -order, item))
    scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    # Weak matches cost prompt time and distract the model when a strong match exists.
    floor = max(1, scored[0][0] * 0.34) if scored else 1
    added = 0
    for score, _, item in scored:
        if score < floor or added >= limit:
            break
        chosen[item.name] = item
        added += 1
    for name in recent or []:
        if name in by_name and added < limit + 3 and name not in chosen:
            chosen[name] = by_name[name]
            added += 1
    return list(chosen.values())


def search_tools(query: str, offline: bool, exclude: set[str] | None = None, limit: int = 6) -> list[Tool]:
    """Finds tools by what they do, for more_tools."""
    wanted = _tokens(query)
    scored = []
    for order, item in enumerate(available_tools(offline)):
        if item.name in (exclude or set()):
            continue
        name_words, keywords, description = _tool_index(item)
        score = 4 * len(wanted & name_words) + 3 * len(wanted & keywords) + len(wanted & description)
        if score:
            scored.append((score, -order, item))
    scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return [entry[2] for entry in scored[:limit]]


def resolve_input(raw: str, *, kinds: tuple[str, ...] = ()) -> Path:
    """An existing file or folder, recovering from a slightly wrong path when it is unambiguous."""
    if not (raw or "").strip():
        raise ToolError("No path was given.", "Use find_files to locate it, or ask the user.")
    path = paths.expand(raw)
    if path.exists():
        return path.resolve()
    if not path.is_absolute():
        for root in paths.search_roots():
            candidate = root / path
            if candidate.exists():
                return candidate.resolve()
    matches = paths.find_by_name(path.name)
    if kinds:
        matches = [match for match in matches if match.suffix.lower() in kinds] or matches
    if len(matches) == 1:
        return matches[0].resolve()
    if matches:
        listing = "\n".join(f"- {match}" for match in matches[:6])
        raise ToolError(f"'{raw}' was not found, but these look similar:\n{listing}", "Ask the user which one, or use the exact path.")
    raise ToolError(f"'{raw}' does not exist.", "Use find_files to search for it, or ask the user where it is.")


def resolve_output(raw: str, *, kind: str = "documents", default_name: str = "", overwrite: bool = False, beside: Path | None = None) -> Path:
    """Where to save. Bare names go next to the source or to a sensible folder, and nothing is clobbered unless asked."""
    if (raw or "").strip():
        path = paths.expand(raw)
        if not path.is_absolute():
            path = (beside.parent if beside is not None else paths.default_output_dir(kind)) / path
        if path.is_dir() or raw.rstrip().endswith(("/", "\\")) or (not path.suffix and default_name and not path.parent.is_file() and _looks_like_folder(raw)):
            path = path / default_name
        elif default_name and not path.suffix:
            path = path.with_suffix(Path(default_name).suffix)
    else:
        path = paths.default_output_dir(kind) / default_name
    if paths.is_protected(path):
        raise ToolError(f"{path.parent} is a protected system folder, so Aegis will not write there.", "Choose a folder in the user's profile.")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path if overwrite else paths.unique(path)


def _looks_like_folder(raw: str) -> bool:
    expanded = paths.expand(raw)
    return expanded in paths.known_folders().values()
