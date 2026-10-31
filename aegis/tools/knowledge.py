"""Long-term memory, reminders, to-dos, and full-text search over the user's own documents."""
from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from aegis import paths
from aegis.toolkit import WRITE, Result, ToolError, arg, resolve_input, tool
from aegis.tools.docs import extract_text

logger = logging.getLogger("aegis.knowledge")


# ---------------------------------------------------------------------- memory
@tool("remember", "Save a lasting fact or preference about the user for future conversations.", risk=WRITE, terminal=True, group="Memory", doing="Remembering",
      keywords="remember memorize note favourite favorite prefer preference my name always keep in mind don't forget",
      fact=arg("string", "The fact, as a short sentence", required=True))
def remember(ctx, fact: str) -> Result:
    fact = fact.strip().rstrip(".") + "."
    memory_id = ctx.store.remember(fact)
    stored = {row["id"]: row["fact"] for row in ctx.store.memories()}
    if stored.get(memory_id) != fact and not any(value.lower() == fact.lower() for value in stored.values()):
        raise ToolError("The memory did not save.")
    return Result(f"I'll remember: {fact}", verified="Stored in the local memory database.")


@tool("recall", "Look up facts the user asked you to remember.", group="Memory", doing="Checking memory", keywords="recall remember memory what did i tell you know about me favourite favorite my",
      query=arg("string", "What to look for; empty lists everything"))
def recall(ctx, query: str = "") -> Result:
    rows = ctx.store.recall(query) if query else ctx.store.memories(30)
    if not rows:
        rows = ctx.store.memories(30) if query else []
    if not rows:
        return Result("Nothing has been saved to memory yet.")
    return Result(f"{len(rows)} saved fact(s):", "\n".join(f"[{row['id']}] {row['fact']}" for row in rows))


@tool("forget", "Delete a saved memory.", risk=WRITE, terminal=True, group="Memory", doing="Forgetting", keywords="forget delete remove memory stop remembering",
      query=arg("string", "Words from the memory to delete", required=True))
def forget(ctx, query: str) -> Result:
    rows = ctx.store.recall(query, limit=3)
    if not rows:
        raise ToolError(f"No saved memory matches '{query}'.")
    if len(rows) > 1:
        lowered = query.lower()
        exact = [row for row in rows if lowered in row["fact"].lower()]
        rows = exact if len(exact) == 1 else rows
    if len(rows) > 1:
        listing = "\n".join(f"- {row['fact']}" for row in rows)
        raise ToolError(f"Several memories match:\n{listing}", "Ask the user which one.")
    ctx.store.forget(rows[0]["id"])
    return Result(f"Forgot: {rows[0]['fact']}", verified="Removed from the local memory database.")


# ---------------------------------------------------------------------- reminders
def _parse_when(at: str) -> float:
    text = at.strip().lower()
    now = dt.datetime.now()
    # Models often put "in 10 minutes" in the clock-time field.
    relative = re.fullmatch(r"(?:in\s+)?(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)(?:\s+from now)?", text)
    if relative:
        unit = {"s": 1, "m": 60, "h": 3600}[relative.group(2)[0]]
        return now.timestamp() + float(relative.group(1)) * unit
    try:
        parsed = dt.datetime.fromisoformat(at.strip())
        return parsed.timestamp()
    except ValueError:
        pass
    match = re.fullmatch(r"(?:(today|tomorrow)\s*(?:at\s*)?)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*(today|tomorrow))?", text)
    if match:
        day, hour, minute, meridiem, later_day = match.group(1), int(match.group(2)), int(match.group(3) or 0), match.group(4), match.group(5)
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            raise ToolError(f"'{at}' is not a valid time.")
        moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        chosen = day or later_day
        if chosen == "tomorrow":
            moment += dt.timedelta(days=1)
        elif moment <= now and chosen != "today":
            moment += dt.timedelta(days=1)
        return moment.timestamp()
    raise ToolError(f"Could not understand the time '{at}'.", "Use in_minutes, or an ISO time like 2026-11-02T09:00.")


_WORD_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "half an": 0.5, "half a": 0.5}


def _ground_delay(args: dict[str, Any], request: str) -> dict[str, Any]:
    """A delay comes from the user's own words: a small model can turn "10 seconds" into 10 minutes."""
    match = re.search(r"\b(?:in|after)\s+(\d+(?:\.\d+)?|half an?|an?|one|two|three|four|five|six|ten|fifteen|twenty|thirty)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", request.lower())
    if not match:
        return args
    amount = _WORD_NUMBERS.get(match.group(1))
    if amount is None:
        amount = float(match.group(1))
    per_unit = {"s": 1 / 60, "m": 1, "h": 60}[match.group(2)[0]]
    grounded = {**args, "in_minutes": amount * per_unit}
    grounded.pop("at", None)
    return grounded


def describe_time(timestamp: float) -> str:
    left = timestamp - time.time()
    if 0 < left < 120:
        return f"in {round(left)} seconds"
    moment = dt.datetime.fromtimestamp(timestamp)
    today = dt.date.today()
    day = "today" if moment.date() == today else "tomorrow" if moment.date() == today + dt.timedelta(days=1) else moment.strftime("%a %d %b")
    return f"{day} at {moment:%H:%M}"


def _due(in_minutes: float | None, at: str) -> float:
    if in_minutes is not None:
        if in_minutes <= 0:
            raise ToolError("in_minutes must be positive.")
        return time.time() + float(in_minutes) * 60
    if at:
        due = _parse_when(at)
        if due <= time.time():
            raise ToolError(f"{at} is in the past.")
        return due
    raise ToolError("Say when: in_minutes or at.")


@tool("set_reminder", "Remind the user of something later with a notification. Use in_minutes for 'in 10 minutes', or at for a clock time.", risk=WRITE, terminal=True, group="Reminders", doing="Setting a reminder", refine=_ground_delay,
      keywords="remind reminder alert notify notification timer alarm later minutes hours tomorrow don't forget schedule pomodoro focus",
      message=arg("string", "What to remind them of", required=True), in_minutes=arg("number", "Minutes from now"), at=arg("string", "A clock time, e.g. 17:30 or tomorrow 9am"), repeat_minutes=arg("integer", "Repeat every N minutes"))
def set_reminder(ctx, message: str, in_minutes: float | None = None, at: str = "", repeat_minutes: int = 0) -> Result:
    due = _due(in_minutes, at)
    reminder = ctx.store.add_reminder(message.strip(), due, repeat_minutes * 60 if repeat_minutes else None)
    if ctx.bus:
        ctx.bus.publish({"type": "reminders_changed"})
    repeat = f", then every {repeat_minutes} minutes" if repeat_minutes else ""
    return Result(f"I'll remind you to \"{message.strip()}\" {describe_time(due)}{repeat}.", verified="Saved to the reminder list; it survives restarts.", undo={"kind": "cancel_reminder", "id": reminder["id"]})


@tool("schedule_task", "Have Aegis carry out a request automatically at a later time, once or repeatedly. Actions that need approval are skipped.", risk=WRITE, terminal=True, group="Reminders", doing="Scheduling a task", refine=_ground_delay,
      keywords="schedule every daily later automatically recurring routine task run at",
      request=arg("string", "What Aegis should do, in plain words", required=True), in_minutes=arg("number", "Minutes from now"), at=arg("string", "A clock time"), repeat_minutes=arg("integer", "Repeat every N minutes"))
def schedule_task(ctx, request: str, in_minutes: float | None = None, at: str = "", repeat_minutes: int = 0) -> Result:
    due = _due(in_minutes, at)
    ctx.store.add_reminder(request.strip(), due, repeat_minutes * 60 if repeat_minutes else None, kind="run")
    if ctx.bus:
        ctx.bus.publish({"type": "reminders_changed"})
    return Result(f"Scheduled \"{request.strip()}\" for {describe_time(due)}.", verified="Saved to the schedule; it survives restarts.")


@tool("list_reminders", "List upcoming reminders and scheduled tasks.", group="Reminders", doing="Checking reminders", keywords="reminders scheduled upcoming list what show alarms timers")
def list_reminders(ctx) -> Result:
    rows = ctx.store.reminders()
    if not rows:
        return Result("There are no upcoming reminders.")
    return Result(f"{len(rows)} upcoming:", "\n".join(f"{describe_time(row['due'])}: {row['message']}" + (" (scheduled task)" if row["kind"] == "run" else "") for row in rows))


@tool("cancel_reminder", "Cancel an upcoming reminder or scheduled task.", risk=WRITE, terminal=True, group="Reminders", doing="Cancelling a reminder", keywords="cancel remove delete reminder stop scheduled alarm timer",
      query=arg("string", "Words from the reminder", required=True))
def cancel_reminder(ctx, query: str) -> Result:
    lowered = query.lower()
    rows = [row for row in ctx.store.reminders() if lowered in row["message"].lower()] or [row for row in ctx.store.reminders() if any(word in row["message"].lower() for word in lowered.split())]
    if not rows:
        raise ToolError(f"No upcoming reminder matches '{query}'.")
    if len(rows) > 1:
        raise ToolError("Several reminders match:\n" + "\n".join(f"- {row['message']}" for row in rows), "Ask the user which one.")
    ctx.store.set_reminder(rows[0]["id"], status="cancelled")
    if ctx.bus:
        ctx.bus.publish({"type": "reminders_changed"})
    return Result(f"Cancelled the reminder \"{rows[0]['message']}\".", verified="It is no longer on the list.")


# ---------------------------------------------------------------------- to-dos
@tool("add_todo", "Add an item to the to-do list.", risk=WRITE, terminal=True, group="To-dos", doing="Adding a to-do", keywords="todo to-do task list add need buy groceries shopping item",
      text=arg("string", "The item", required=True))
def add_todo(ctx, text: str) -> Result:
    todo = ctx.store.add_todo(text.strip())
    if ctx.bus:
        ctx.bus.publish({"type": "todos_changed"})
    return Result(f"Added \"{todo['text']}\" to your to-do list.", verified="Saved to the local database.")


@tool("list_todos", "Show the to-do list.", group="To-dos", doing="Checking to-dos", keywords="todo to-do tasks list show pending")
def list_todos(ctx) -> Result:
    rows = ctx.store.todos()
    if not rows:
        return Result("The to-do list is empty.")
    return Result(f"{len(rows)} to-do(s):", "\n".join(f"- {row['text']}" for row in rows))


@tool("complete_todo", "Mark a to-do item as done.", risk=WRITE, terminal=True, group="To-dos", doing="Completing a to-do", keywords="todo done finish complete check off tick task",
      text=arg("string", "Words from the item", required=True))
def complete_todo(ctx, text: str) -> Result:
    row = ctx.store.find_todo(text)
    if row is None:
        raise ToolError(f"No open to-do matches '{text}'.")
    ctx.store.set_todo_done(row["id"])
    if ctx.bus:
        ctx.bus.publish({"type": "todos_changed"})
    return Result(f"Checked off \"{row['text']}\".", verified="Updated in the local database.")


# ---------------------------------------------------------------------- document index
_INDEXABLE = {".txt", ".md", ".pdf", ".docx", ".pptx", ".csv", ".rtf", ".html", ".htm", ".py", ".js", ".ts", ".java", ".c", ".cpp", ".json", ".xlsx"}
_index_lock = threading.Lock()


def _chunk(text: str, size: int = 1400, overlap: int = 180) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text)
    chunks, start = [], 0
    while start < len(text):
        piece = text[start:start + size]
        if start + size < len(text):
            cut = max(piece.rfind("\n\n"), piece.rfind(". "), piece.rfind("\n"))
            if cut > size * 0.5:
                piece = piece[:cut + 1]
        chunks.append(piece.strip())
        start += max(1, len(piece) - overlap)
    return [chunk for chunk in chunks if chunk]


def index_folder_now(store: Any, folder: Path, progress=None, cancelled=None) -> dict[str, int]:
    """Bring the search index up to date with a folder. Only changed files are re-read."""
    with _index_lock:
        folder_key = str(folder)
        seen: set[str] = set()
        indexed = skipped = failed = 0
        for entry in paths.walk_files([folder], max_seconds=600, max_entries=500_000, max_depth=30):
            if cancelled is not None and cancelled.is_set():
                break
            if Path(entry.name).suffix.lower() not in _INDEXABLE:
                continue
            try:
                stat = entry.stat()
                if stat.st_size > 60_000_000 or stat.st_size == 0:
                    continue
                seen.add(entry.path)
                state = store.doc_state(entry.path)
                if state and abs(state["mtime"] - stat.st_mtime) < 1 and state["size"] == stat.st_size:
                    skipped += 1
                    continue
                text, _ = extract_text(Path(entry.path))
                chunks = _chunk(text)[:600]
                if chunks:
                    store.index_document(entry.path, folder_key, stat.st_mtime, stat.st_size, chunks)
                    indexed += 1
                if progress and indexed % 5 == 0:
                    progress(f"Indexed {indexed} files, now {entry.name[:40]}")
            except Exception as exc:  # one unreadable file must not stop the index
                logger.info("could not index %s: %s", entry.path, exc)
                failed += 1
        removed = 0
        for path in store.indexed_paths(folder_key):
            if path not in seen and not Path(path).exists():
                store.remove_document(path)
                removed += 1
        return {"indexed": indexed, "unchanged": skipped, "failed": failed, "removed": removed}


@tool("index_folder", "Add a folder to the private search index, so search_documents can find text inside its files.", risk=WRITE, terminal=True, group="Documents", doing="Indexing a folder",
      keywords="index folder add library learn scan knowledge documents search", path=arg("string", "Folder", required=True))
def index_folder(ctx, path: str) -> Result:
    folder = resolve_input(path)
    if not folder.is_dir():
        raise ToolError(f"{folder} is not a folder.")
    stats = index_folder_now(ctx.store, folder, ctx.progress, ctx.cancelled)
    if ctx.settings:
        folders = list(ctx.settings["doc_folders"])
        if str(folder) not in folders:
            ctx.settings.update({"doc_folders": folders + [str(folder)]})
    return Result(f"Indexed {paths.pretty(folder)}: {stats['indexed']} new or changed, {stats['unchanged']} unchanged, {stats['failed']} unreadable.", verified="The search index holds these files.")


@tool("search_documents", "Search the text of the user's indexed documents, and cite the file each answer came from. Use for questions about their own notes, papers or files.", group="Documents", doing="Searching your documents",
      keywords="search documents notes papers knowledge base library what did say according my files mention research",
      query=arg("string", "What to look for", required=True), limit=arg("integer", "Results to return"))
def search_documents(ctx, query: str, limit: int = 5) -> Result:
    if not ctx.store.index_stats():
        raise ToolError("No folders have been indexed yet, so there is nothing to search here.", "Use search_in_files to search inside the files in documents, desktop or downloads, or find_files to search by name.")
    rows = ctx.store.search_documents(query, limit=max(1, min(limit, 10)))
    if not rows:
        return Result(f"Nothing in the indexed documents matches '{query}'.")
    body = "\n\n".join(f"[{index}] {row['path']}\n{row['text'][:900]}" for index, row in enumerate(rows, 1))
    return Result(f"{len(rows)} passage(s) found. Cite the file when answering.", body)
