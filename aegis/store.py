"""All durable state, in one SQLite file."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads(
    id TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, source TEXT DEFAULT 'chat');
CREATE TABLE IF NOT EXISTS items(
    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, role TEXT NOT NULL, text TEXT DEFAULT '', data TEXT DEFAULT '{}', ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS items_thread ON items(thread_id, seq);
CREATE TABLE IF NOT EXISTS artifacts(
    id TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER,
    thread_id TEXT, item_id TEXT, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS artifacts_path ON artifacts(path);
CREATE TABLE IF NOT EXISTS audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, tool TEXT NOT NULL, args TEXT, status TEXT,
    summary TEXT, thread_id TEXT, prev_hash TEXT, hash TEXT);
CREATE TABLE IF NOT EXISTS undo(
    id TEXT PRIMARY KEY, item_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, label TEXT, done INTEGER DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reminders(
    id TEXT PRIMARY KEY, message TEXT NOT NULL, due REAL NOT NULL, repeat_seconds INTEGER, kind TEXT NOT NULL DEFAULT 'notify',
    status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL, fired_at REAL);
CREATE TABLE IF NOT EXISTS todos(
    id TEXT PRIMARY KEY, text TEXT NOT NULL, done INTEGER DEFAULT 0, created REAL NOT NULL, completed REAL);
CREATE TABLE IF NOT EXISTS documents(
    path TEXT PRIMARY KEY, folder TEXT, mtime REAL, size INTEGER, chunks INTEGER);
CREATE VIRTUAL TABLE IF NOT EXISTS memory USING fts5(fact, created UNINDEXED, tokenize='porter unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks USING fts5(path UNINDEXED, chunk UNINDEXED, text, tokenize='porter unicode61');
"""


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR query."""
    words = [word for word in re.findall(r"[\w']+", text.lower()) if len(word) > 1 and word not in _STOP]
    return " OR ".join(f'"{word}"' for word in dict.fromkeys(words))


_STOP = {"the", "a", "an", "of", "to", "in", "on", "is", "are", "am", "was", "were", "my", "me", "i", "you", "and", "or", "for", "what", "whats", "which", "who", "do", "does", "did", "it", "that", "this", "with", "about", "from", "at", "by", "be", "have", "has", "had", "can", "could", "please", "tell", "show", "find", "any"}


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, tuple(args)).fetchall()]

    def _one(self, sql: str, args: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(sql, tuple(args)).fetchone()
            return dict(row) if row else None

    def _run(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._lock:
            return self._db.execute(sql, tuple(args)).rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ threads
    def create_thread(self, title: str = "New chat", source: str = "chat") -> dict[str, Any]:
        now = time.time()
        thread = {"id": new_id("t_"), "title": title, "created": now, "updated": now, "source": source}
        self._run("INSERT INTO threads(id,title,created,updated,source) VALUES(?,?,?,?,?)", (thread["id"], title, now, now, source))
        return thread

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM threads WHERE id=?", (thread_id,))

    def list_threads(self, limit: int = 200, query: str = "") -> list[dict[str, Any]]:
        if query.strip():
            like = f"%{query.strip()}%"
            return self._all(
                "SELECT DISTINCT t.* FROM threads t LEFT JOIN items i ON i.thread_id=t.id "
                "WHERE t.title LIKE ? OR i.text LIKE ? ORDER BY t.updated DESC LIMIT ?", (like, like, limit))
        return self._all("SELECT * FROM threads ORDER BY updated DESC LIMIT ?", (limit,))

    def rename_thread(self, thread_id: str, title: str) -> None:
        self._run("UPDATE threads SET title=? WHERE id=?", (title, thread_id))

    def delete_thread(self, thread_id: str) -> None:
        self._run("DELETE FROM threads WHERE id=?", (thread_id,))

    def touch_thread(self, thread_id: str) -> None:
        self._run("UPDATE threads SET updated=? WHERE id=?", (time.time(), thread_id))

    # ------------------------------------------------------------------ timeline
    def add_item(self, thread_id: str, role: str, text: str = "", data: dict[str, Any] | None = None, item_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT COALESCE(MAX(seq),0)+1 AS next FROM items WHERE thread_id=?", (thread_id,)).fetchone()
            item = {"id": item_id or new_id("i_"), "thread_id": thread_id, "seq": row["next"], "role": role, "text": text, "data": data or {}, "ts": time.time()}
            self._db.execute("INSERT INTO items(id,thread_id,seq,role,text,data,ts) VALUES(?,?,?,?,?,?,?)",
                             (item["id"], thread_id, item["seq"], role, text, json.dumps(item["data"], ensure_ascii=False, default=str), item["ts"]))
            self._db.execute("UPDATE threads SET updated=? WHERE id=?", (item["ts"], thread_id))
            return item

    def update_item(self, item_id: str, text: str | None = None, data: dict[str, Any] | None = None) -> None:
        with self._lock:
            if text is not None:
                self._db.execute("UPDATE items SET text=? WHERE id=?", (text, item_id))
            if data is not None:
                self._db.execute("UPDATE items SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False, default=str), item_id))

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM items WHERE id=?", (item_id,))
        if row:
            row["data"] = json.loads(row["data"] or "{}")
        return row

    def items(self, thread_id: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM items WHERE thread_id=? ORDER BY seq", (thread_id,))
        for row in rows:
            row["data"] = json.loads(row["data"] or "{}")
        return rows

    def recent_tools(self, thread_id: str, limit: int = 6) -> list[str]:
        rows = self._all("SELECT data FROM items WHERE thread_id=? AND role='tool' ORDER BY seq DESC LIMIT ?", (thread_id, limit))
        return [json.loads(row["data"]).get("name", "") for row in rows]

    # ------------------------------------------------------------------ artifacts
    def add_artifact(self, path: str, kind: str, size: int, thread_id: str | None, item_id: str | None) -> dict[str, Any]:
        artifact = {"id": new_id("a_"), "path": path, "name": Path(path).name, "kind": kind, "size": size, "thread_id": thread_id, "item_id": item_id, "created": time.time()}
        self._run("INSERT INTO artifacts(id,path,name,kind,size,thread_id,item_id,created) VALUES(?,?,?,?,?,?,?,?)",
                  (artifact["id"], path, artifact["name"], kind, size, thread_id, item_id, artifact["created"]))
        return artifact

    def list_artifacts(self, limit: int = 300) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM artifacts ORDER BY created DESC LIMIT ?", (limit,))

    def artifact_by_id(self, artifact_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))

    def artifact_known(self, path: str) -> bool:
        return self._one("SELECT id FROM artifacts WHERE path=? LIMIT 1", (path,)) is not None

    def remove_artifact(self, artifact_id: str) -> None:
        self._run("DELETE FROM artifacts WHERE id=?", (artifact_id,))

    # ------------------------------------------------------------------ audit chain
    def audit(self, tool: str, args: dict[str, Any], status: str, summary: str, thread_id: str | None = None) -> None:
        with self._lock:
            last = self._db.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else ""
            ts = time.time()
            args_json = json.dumps(args, ensure_ascii=False, default=str, sort_keys=True)[:4000]
            digest = hashlib.sha256(f"{prev}|{ts}|{tool}|{args_json}|{status}|{summary}".encode()).hexdigest()
            self._db.execute("INSERT INTO audit(ts,tool,args,status,summary,thread_id,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?)",
                             (ts, tool, args_json, status, summary, thread_id, prev, digest))

    def audit_entries(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    def audit_valid(self) -> bool:
        prev = ""
        for row in self._all("SELECT * FROM audit ORDER BY id"):
            digest = hashlib.sha256(f"{prev}|{row['ts']}|{row['tool']}|{row['args']}|{row['status']}|{row['summary']}".encode()).hexdigest()
            if row["prev_hash"] != prev or row["hash"] != digest:
                return False
            prev = row["hash"]
        return True

    # ------------------------------------------------------------------ undo
    def add_undo(self, item_id: str | None, kind: str, payload: dict[str, Any], label: str) -> str:
        undo_id = new_id("u_")
        self._run("INSERT INTO undo(id,item_id,kind,payload,label,created) VALUES(?,?,?,?,?,?)",
                  (undo_id, item_id, kind, json.dumps(payload, ensure_ascii=False), label, time.time()))
        return undo_id

    def get_undo(self, undo_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM undo WHERE id=?", (undo_id,))
        if row:
            row["payload"] = json.loads(row["payload"])
        return row

    def last_undo(self) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM undo WHERE done=0 ORDER BY created DESC LIMIT 1")
        if row:
            row["payload"] = json.loads(row["payload"])
        return row

    def mark_undone(self, undo_id: str) -> None:
        self._run("UPDATE undo SET done=1 WHERE id=?", (undo_id,))

    # ------------------------------------------------------------------ memory
    def remember(self, fact: str) -> int:
        with self._lock:
            existing = self._db.execute("SELECT rowid FROM memory WHERE lower(fact)=lower(?)", (fact,)).fetchone()
            if existing:
                return existing["rowid"]
            return self._db.execute("INSERT INTO memory(fact,created) VALUES(?,?)", (fact, time.time())).lastrowid

    def forget(self, memory_id: int) -> int:
        return self._run("DELETE FROM memory WHERE rowid=?", (memory_id,))

    def memories(self, limit: int = 300) -> list[dict[str, Any]]:
        return self._all("SELECT rowid AS id, fact, created FROM memory ORDER BY created DESC LIMIT ?", (limit,))

    def recall(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        fts = _fts_query(query)
        if not fts:
            return []
        return self._all("SELECT rowid AS id, fact, created FROM memory WHERE memory MATCH ? ORDER BY rank LIMIT ?", (fts, limit))

    # ------------------------------------------------------------------ reminders
    def add_reminder(self, message: str, due: float, repeat_seconds: int | None = None, kind: str = "notify") -> dict[str, Any]:
        reminder = {"id": new_id("r_"), "message": message, "due": due, "repeat_seconds": repeat_seconds, "kind": kind, "status": "pending", "created": time.time()}
        self._run("INSERT INTO reminders(id,message,due,repeat_seconds,kind,status,created) VALUES(?,?,?,?,?,?,?)",
                  (reminder["id"], message, due, repeat_seconds, kind, "pending", reminder["created"]))
        return reminder

    def reminders(self, include_done: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        if include_done:
            return self._all("SELECT * FROM reminders ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, due DESC LIMIT ?", (limit,))
        return self._all("SELECT * FROM reminders WHERE status='pending' ORDER BY due LIMIT ?", (limit,))

    def due_reminders(self, now: float) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM reminders WHERE status='pending' AND due<=? ORDER BY due", (now,))

    def set_reminder(self, reminder_id: str, **fields: Any) -> int:
        keys = ", ".join(f"{key}=?" for key in fields)
        return self._run(f"UPDATE reminders SET {keys} WHERE id=?", (*fields.values(), reminder_id))

    # ------------------------------------------------------------------ todos
    def add_todo(self, text: str) -> dict[str, Any]:
        todo = {"id": new_id("d_"), "text": text, "done": 0, "created": time.time(), "completed": None}
        self._run("INSERT INTO todos(id,text,created) VALUES(?,?,?)", (todo["id"], text, todo["created"]))
        return todo

    def todos(self, include_done: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM todos " + ("" if include_done else "WHERE done=0 ") + "ORDER BY done, created DESC"
        return self._all(sql)

    def set_todo_done(self, todo_id: str, done: bool = True) -> int:
        return self._run("UPDATE todos SET done=?, completed=? WHERE id=?", (1 if done else 0, time.time() if done else None, todo_id))

    def delete_todo(self, todo_id: str) -> int:
        return self._run("DELETE FROM todos WHERE id=?", (todo_id,))

    def find_todo(self, text: str) -> dict[str, Any] | None:
        like = f"%{text.strip()}%"
        return self._one("SELECT * FROM todos WHERE text LIKE ? AND done=0 ORDER BY created DESC LIMIT 1", (like,))

    # ------------------------------------------------------------------ document index
    def doc_state(self, path: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM documents WHERE path=?", (path,))

    def index_document(self, path: str, folder: str, mtime: float, size: int, chunks: list[str]) -> None:
        with self._lock:
            self._db.execute("DELETE FROM doc_chunks WHERE path=?", (path,))
            self._db.executemany("INSERT INTO doc_chunks(path,chunk,text) VALUES(?,?,?)", [(path, index, chunk) for index, chunk in enumerate(chunks)])
            self._db.execute("INSERT OR REPLACE INTO documents(path,folder,mtime,size,chunks) VALUES(?,?,?,?,?)", (path, folder, mtime, size, len(chunks)))

    def remove_document(self, path: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM doc_chunks WHERE path=?", (path,))
            self._db.execute("DELETE FROM documents WHERE path=?", (path,))

    def indexed_paths(self, folder: str) -> list[str]:
        return [row["path"] for row in self._all("SELECT path FROM documents WHERE folder=?", (folder,))]

    def index_stats(self) -> list[dict[str, Any]]:
        return self._all("SELECT folder, COUNT(*) AS files, SUM(chunks) AS chunks, SUM(size) AS bytes FROM documents GROUP BY folder")

    def search_documents(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        fts = _fts_query(query)
        if not fts:
            return []
        return self._all(
            "SELECT path, chunk, snippet(doc_chunks, 2, '[', ']', ' … ', 40) AS snippet, text, bm25(doc_chunks) AS score "
            "FROM doc_chunks WHERE doc_chunks MATCH ? ORDER BY rank LIMIT ?", (fts, limit))
