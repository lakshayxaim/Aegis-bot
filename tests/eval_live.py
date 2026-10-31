"""Live end-to-end evaluation: real requests, real model, real tools, and checks against the real result.

    python tests/eval_live.py [--model NAME] [--only substring]

It starts its own Aegis server with a throwaway data folder, works inside a scratch folder, and judges
each request by what actually happened on disk or in the tool log, not by what the model said.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib import request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BOX = Path(tempfile.gettempdir()) / "aegis_eval_box"


class Client:
    def __init__(self, port: int, token: str) -> None:
        self.base, self.token = f"http://127.0.0.1:{port}", token

    def call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json", "X-Aegis-Token": self.token})
        with request.urlopen(req, timeout=30) as response:
            return json.load(response)

    def ask(self, text: str, thread_id: str | None = None, approve: str = "once", timeout: int = 240) -> dict[str, Any]:
        thread = thread_id or self.call("POST", "/api/threads", {})["id"]
        started = time.time()
        run = self.call("POST", f"/api/threads/{thread}/messages", {"text": text})["run_id"]
        deadline = started + timeout
        while time.time() < deadline:
            view = self.call("GET", f"/api/threads/{thread}")
            for item in view["items"]:
                if item["role"] == "tool" and item["data"].get("status") == "awaiting_approval" and item["data"].get("run_id") == run:
                    self.call("POST", f"/api/approvals/{item['id']}", {"decision": approve})
            if not view["running"]:
                break
            time.sleep(0.25)
        else:
            self.call("POST", f"/api/runs/{run}/cancel", {})
            return {"thread": thread, "timeout": True, "reply": "(timed out)", "tools": [], "seconds": timeout, "status": "timeout"}
        view = self.call("GET", f"/api/threads/{thread}")
        items = [item for item in view["items"] if item["data"].get("run_id") == run]
        tools = [item for item in items if item["role"] == "tool"]
        replies = [item for item in items if item["role"] == "assistant"]
        return {
            "thread": thread, "seconds": time.time() - started, "reply": replies[-1]["text"] if replies else "", "status": replies[-1]["data"].get("status") if replies else "none",
            "tools": [{"name": item["data"].get("name"), "args": item["data"].get("args"), "status": item["data"].get("status"), "text": item["text"], "files": item["data"].get("files", [])} for item in tools],
        }


def used(result: dict[str, Any], name: str, status: tuple[str, ...] = ("ok", "unverified")) -> bool:
    return any(tool["name"] == name and tool["status"] in status for tool in result["tools"])


def has(result: dict[str, Any], *words: str) -> bool:
    text = result["reply"].lower().replace(",", "")
    return all(word.lower() in text for word in words)


def _foreground_title() -> str:
    sys.path.insert(0, str(ROOT))
    from aegis import winapi

    window = winapi.current_window()
    return window.title if window else ""


def build_cases() -> list[tuple[str, str, Callable[[dict[str, Any]], bool]]]:
    box = BOX.as_posix()
    return [
        # The twelve requests the first version was measured on.
        ("math", "what is 17 percent of 2350?", lambda r: has(r, "399.5")),
        ("screenshot", f"take a screenshot and save it as {box}/shot.png", lambda r: used(r, "take_screenshot") and (BOX / "shot.png").stat().st_size > 1000),
        ("system", "how much free disk space do I have and what is my battery level?", lambda r: used(r, "system_info") and ("gb" in r["reply"].lower() or "%" in r["reply"])),
        ("list", f"list the files in {box}", lambda r: (used(r, "list_dir") or used(r, "find_files")) and has(r, "notes.txt")),
        ("read", f"read {box}/notes.txt and tell me the budget", lambda r: used(r, "read_file") and has(r, "42000")),
        ("create", f"create a file called hello.txt in {box} containing the word banana", lambda r: (BOX / "hello.txt").read_text(encoding="utf-8").strip().lower() == "banana"),
        ("csv", f"which month had the highest sales in {box}/sales.csv?", lambda r: has(r, "apr")),
        ("chart", f"make a bar chart from {box}/sales.csv and save it as {box}/chart.png", lambda r: used(r, "make_chart") and (BOX / "chart.png").stat().st_size > 1000),
        ("open", "open notepad", lambda r: used(r, "open_app") and bool(__import__("aegis.winapi", fromlist=["find_window"]).find_window("notepad")[0])),
        ("windows", "what windows do I have open right now?", lambda r: used(r, "list_windows")),
        ("reminder", "remind me in 10 minutes to drink water", lambda r: used(r, "set_reminder") and "water" in r["tools"][0]["args"].get("message", "").lower()),
        ("memory", "remember that my favourite editor is VS Code", lambda r: used(r, "remember")),
        # Harder: multi-step, follow-ups, recovery, knowledge.
        ("general", "what is the capital of Japan?", lambda r: has(r, "tokyo") and not r["tools"]),
        ("recall", "what's my favourite editor?", lambda r: has(r, "code")),
        ("multi", f"create {box}/todo.md with the text 'buy milk' and then read it back to me", lambda r: (BOX / "todo.md").exists() and used(r, "read_file") and has(r, "milk")),
        ("convert", f"convert {box}/report.docx to pdf", lambda r: used(r, "convert_document") and (BOX / "report.pdf").exists()),
        ("summarize", f"summarise {box}/report.docx", lambda r: has(r, "falcon") or has(r, "priya") or has(r, "budget")),
        ("clipboard", "copy the words purple elephant to my clipboard", lambda r: used(r, "clipboard_write") and "purple elephant" in _clipboard().lower()),
        ("find", "find the file named aegis_eval_needle", lambda r: used(r, "find_files") and has(r, "aegis_eval_needle")),
        ("rename", f"rename {box}/oldname.txt to newname.txt", lambda r: (BOX / "newname.txt").exists() and not (BOX / "oldname.txt").exists()),
        ("todo", "add call the dentist to my to-do list", lambda r: used(r, "add_todo")),
        ("snap", "move the notepad window to the left half of the screen", lambda r: any(t["name"] == "window_action" and t["status"] == "ok" and t["args"].get("action") == "snap_left" for t in r["tools"])),
        ("processes", "what's using the most memory on my pc?", lambda r: used(r, "list_processes")),
        ("volume", "set the volume to 22 percent", lambda r: used(r, "set_volume")),
        ("doc", f"write a short thank-you letter to Sam and save it as {box}/letter.docx", lambda r: used(r, "create_document") and (BOX / "letter.docx").exists()),
        ("close", "close notepad", lambda r: any(t["name"] == "window_action" and t["args"].get("action") == "close" and t["status"] == "ok" for t in r["tools"])),
        ("python", f"use python to count how many .txt files are in {box} and tell me the number", lambda r: used(r, "run_python") and has(r, str(len(list(BOX.glob("*.txt")))))),
    ]


def _clipboard() -> str:
    sys.path.insert(0, str(ROOT))
    from aegis.tools.desktop import read_clipboard_text

    return read_clipboard_text() or ""


def prepare_box() -> None:
    shutil.rmtree(BOX, ignore_errors=True)
    BOX.mkdir(parents=True)
    (BOX / "notes.txt").write_text("Project Falcon kickoff notes. Budget is 42000 dollars. Owner is Priya. Launch date is 14 March.", encoding="utf-8")
    (BOX / "sales.csv").write_text("month,sales\nJan,120\nFeb,150\nMar,90\nApr,210\n", encoding="utf-8")
    (BOX / "oldname.txt").write_text("rename me", encoding="utf-8")
    needle = Path.home() / "Documents" / "aegis_eval_needle.txt"
    needle.write_text("needle", encoding="utf-8")
    import docx

    document = docx.Document()
    document.add_heading("Project Falcon status", 1)
    document.add_paragraph("Project Falcon is on track. The owner is Priya. The budget is 42000 dollars and launch is on 14 March.")
    document.save(str(BOX / "report.docx"))


def cleanup_box() -> None:
    (Path.home() / "Documents" / "aegis_eval_needle.txt").unlink(missing_ok=True)


def _snapshot_machine() -> dict[str, Any]:
    from aegis.tools.desktop import _endpoint

    return {"volume": _endpoint().GetMasterVolumeLevelScalar(), "clipboard": _clipboard()}


def _restore_machine(state: dict[str, Any]) -> None:
    from aegis.tools.desktop import _endpoint, write_clipboard_text

    _endpoint().SetMasterVolumeLevelScalar(state["volume"], None)
    write_clipboard_text(state["clipboard"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="")
    parser.add_argument("--only", default="")
    parser.add_argument("--port", type=int, default=8499)
    args = parser.parse_args()
    prepare_box()
    machine = _snapshot_machine()
    home = Path(tempfile.mkdtemp(prefix="aegis_eval_home_"))
    env = {**os.environ, "AEGIS_HOME": str(home), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    if args.model:
        (home / "settings.json").write_text(json.dumps({"model": args.model}), encoding="utf-8")
    server = subprocess.Popen([sys.executable, "-m", "aegis", "--serve", "--port", str(args.port)], cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    client = None
    try:
        deadline = time.time() + 60
        while time.time() < deadline and client is None:
            runtime = home / "runtime.json"
            if runtime.exists():
                info = json.loads(runtime.read_text(encoding="utf-8"))
                client = Client(info["port"], info["token"])
                break
            if server.poll() is not None:
                print(server.stdout.read())
                return 2
            time.sleep(0.2)
        for _ in range(240):  # let the model load
            state = client.call("GET", "/api/state")
            if state["model"]["model"] and state["model"]["loaded"]:
                break
            time.sleep(0.5)
        print(f"model={state['model']['model']} gpu={state['model']['gpu_percent']}% server={state['model']['server']}\n", flush=True)
        passed, total, times = 0, 0, []
        for name, text, check in build_cases():
            if args.only and args.only not in name:
                continue
            total += 1
            result = client.ask(text)
            try:
                ok = bool(check(result))
            except Exception as exc:
                ok = False
                result["reply"] = f"{result['reply']}  [check error: {exc}]"
            passed += ok
            times.append(result["seconds"])
            tools = ",".join(f"{tool['name']}:{tool['status']}" for tool in result["tools"]) or "-"
            print(f"{'PASS' if ok else 'FAIL'} {result['seconds']:5.1f}s {name:<10} [{result['status']}] {tools}\n      -> {result['reply'][:170]!r}", flush=True)
        times.sort()
        if times:
            print(f"\n{passed}/{total} passed. Median {times[len(times) // 2]:.1f}s, max {times[-1]:.1f}s.", flush=True)
        return 0 if passed == total else 1
    finally:
        _restore_machine(machine)
        cleanup_box()
        server.terminate()
        try:
            server.wait(timeout=8)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
