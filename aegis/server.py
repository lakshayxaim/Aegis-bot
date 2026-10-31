"""The local API and UI host. Loopback only, and protected against other websites calling it."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from aegis import __version__, browser, config, netguard, paths, speech
from aegis.agent import Agent
from aegis.config import API_TOKEN, Settings
from aegis.events import Bus
from aegis.llm import LLM
from aegis.scheduler import Scheduler
from aegis.store import Store
from aegis.toolkit import REGISTRY, ToolError, available_tools
from aegis.tools import files as file_tools
from aegis.tools import knowledge, media
from aegis.voice import Voice

logger = logging.getLogger("aegis.server")


class Guard:
    """Rejects anything that is not this app: DNS rebinding, and pages in the user's browser posting to localhost."""

    def __init__(self, app: Any, port_getter: Any) -> None:
        self.app, self.port_getter = app, port_getter

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", [])}
        host = headers.get("host", "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return await JSONResponse({"detail": "Forbidden host"}, status_code=403)(scope, receive, send)
        path = scope.get("path", "")
        if path.startswith("/api/"):
            query = scope.get("query_string", b"").decode()
            token = headers.get("x-aegis-token") or next((part[6:] for part in query.split("&") if part.startswith("token=")), "")
            if token != API_TOKEN:
                return await JSONResponse({"detail": "Unauthorized"}, status_code=401)(scope, receive, send)
            origin = headers.get("origin")
            if origin and not re.fullmatch(r"https?://(?:127\.0\.0\.1|localhost)(?::\d+)?", origin):
                return await JSONResponse({"detail": "Forbidden origin"}, status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)


class SendMessage(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class Decision(BaseModel):
    decision: str


class Rename(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class FactBody(BaseModel):
    fact: str = Field(min_length=1, max_length=1000)


class TodoBody(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class PathBody(BaseModel):
    path: str


class ModelBody(BaseModel):
    model: str


class VoiceStart(BaseModel):
    owner: str = Field(default="main", pattern="^(main|overlay)$")


class SpeechChoice(BaseModel):
    id: str = Field(max_length=40)


class SpotlightBody(BaseModel):
    height: int | None = None
    width: int | None = None
    action: str = "resize"


class Runtime:
    def __init__(self, shell: Any = None) -> None:
        config.ensure_dirs()
        self.settings = Settings()
        netguard.install(self.settings["offline"])
        self.store = Store(config.DB_PATH)
        self.bus = Bus()
        self.llm = LLM(self.settings)
        self.shell = shell
        self.voice = Voice(self.bus, self.settings)
        self.agent = Agent(self.settings, self.store, self.llm, self.bus, shell=shell, voice=self.voice)
        self.scheduler = Scheduler(self.store, self.bus, self.agent, shell=shell)
        self.port = config.DEFAULT_PORT
        self.pulling: dict[str, dict[str, Any]] = {}
        self.indexing = False
        self.started = time.time()

    async def startup(self) -> None:
        self.bus.bind(asyncio.get_running_loop())
        self.scheduler.start()
        threading.Thread(target=self._warm, daemon=True, name="aegis-warm").start()

    def wants_listening(self) -> bool:
        return bool(self.settings["listen_on_open"]) and speech.resolve_engine(self.settings["speech_engine"]) is not None

    def _warm(self) -> None:
        self.voice.prepare()
        self.voice.preload()
        if not self.llm.ensure_server():
            self.bus.publish({"type": "model_status"})
            return
        if self.llm.pick_model():
            self.bus.publish({"type": "model_status"})
            from aegis.agent import system_prompt
            from aegis.toolkit import select_tools

            schemas = [tool.schema() for tool in select_tools("", offline=self.settings["offline"])]
            self.llm.warm([{"role": "system", "content": system_prompt(self.settings["user_name"], self.settings["offline"])}, {"role": "user", "content": "ok"}], schemas)
        self.bus.publish({"type": "model_status"})

    async def shutdown(self) -> None:
        await self.scheduler.stop()
        media.stop_active_recording()
        self.llm.shutdown()
        speech.shutdown()
        browser.session.shutdown()

    def doctor(self) -> list[dict[str, Any]]:
        """What works on this machine, and exactly what to do about what does not."""
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str, fix: str = "", optional: bool = False) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix, "optional": optional})

        status = self.llm.status()
        add("Local model server", status["reachable"], status["server"] if status["reachable"] else "Not running", "Install Ollama from ollama.com, then restart Aegis." if not status["ollama_installed"] else "Restart Aegis.")
        add("Language model", bool(status["model"]), status["model"] or "None installed", f"Download {status['recommended']} from the Model section." if not status["model"] else "")
        vision = self.llm.vision_model() if status["reachable"] else None
        add("Seeing images and the screen", bool(vision), vision or "No vision model installed; only OCR is available", "Choose a model with vision in the Model section, such as gemma4:e2b.", optional=True)
        add("Microphone", self.voice.available().get("microphone", False), self.voice.available().get("device") or "No input device found", "Plug in or enable a microphone.", optional=True)
        engine = speech.resolve_engine(self.settings["speech_engine"])
        add("Speech recognition", engine is not None, speech.ENGINES[engine].label if engine else "No speech model yet.", "Download one in the Voice section above.", optional=True)
        driver = browser.find_browser()
        add("Browser control", driver is not None, f"Uses {driver[0].capitalize()}, with a sign-in profile of its own" if driver else "No supported browser found", "Install Chrome, Edge or Brave.", optional=True)
        add("FFmpeg", bool(shutil.which("ffmpeg")), shutil.which("ffmpeg") or "Not found", "Install FFmpeg for recording and media editing.", optional=True)
        word = Path(r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE").exists() or Path(r"C:\Program Files (x86)\Microsoft Office\root\Office16\WINWORD.EXE").exists()
        soffice = bool(shutil.which("soffice")) or Path(r"C:\Program Files\LibreOffice\program\soffice.exe").exists()
        add("Office to PDF", word or soffice, "Microsoft Office" if word else "LibreOffice" if soffice else "Neither Office nor LibreOffice was found", "Install either one to convert Word and PowerPoint files to PDF.", optional=True)
        return checks


def create_app(runtime: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.startup()
        try:
            yield
        finally:
            await runtime.shutdown()

    app = FastAPI(title="Aegis", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runtime = runtime
    store, bus, agent, llm, settings = runtime.store, runtime.bus, runtime.agent, runtime.llm, runtime.settings

    # ------------------------------------------------------------------ event stream
    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        queue = bus.subscribe()

        async def stream():
            try:
                yield "retry: 1500\n\n"
                yield f"data: {json.dumps({'type': 'hello', 'active_runs': agent.active_runs()})}\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                    if await request.is_disconnected():
                        return
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ overview
    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return {"version": __version__, "settings": settings.as_dict(), "model": llm.status(), "net": netguard.status(), "active_runs": agent.active_runs(), "recording": media.recording_status(), "folders": {key: str(value) for key, value in paths.known_folders().items()}, "hotkeys": runtime.shell.hotkeys if runtime.shell else None, "speech": speech.status(settings["speech_engine"])}

    @app.get("/api/vitals")
    def vitals() -> dict[str, Any]:
        memory = psutil.virtual_memory()
        battery = psutil.sensors_battery()
        return {"cpu": psutil.cpu_percent(interval=None), "ram_used": memory.used, "ram_total": memory.total, "battery": round(battery.percent) if battery else None, "plugged": battery.power_plugged if battery else None, "model": llm.status(), "net": netguard.status(), "recording": media.recording_status()}

    @app.get("/api/doctor")
    def doctor() -> dict[str, Any]:
        return {"checks": runtime.doctor()}

    # ------------------------------------------------------------------ threads
    @app.get("/api/threads")
    def threads(q: str = "") -> dict[str, Any]:
        return {"threads": store.list_threads(query=q)}

    @app.post("/api/threads")
    def new_thread() -> dict[str, Any]:
        thread = store.create_thread()
        return thread

    @app.get("/api/threads/{thread_id}")
    def get_thread(thread_id: str) -> dict[str, Any]:
        thread = store.get_thread(thread_id)
        if thread is None:
            raise HTTPException(404, "No such conversation.")
        return {"thread": thread, "items": store.items(thread_id), "running": [run for run in agent.active_runs() if run["thread_id"] == thread_id]}

    @app.patch("/api/threads/{thread_id}")
    def rename(thread_id: str, body: Rename) -> dict[str, Any]:
        store.rename_thread(thread_id, body.title)
        bus.publish({"type": "thread_updated", "thread_id": thread_id, "title": body.title})
        return {"ok": True}

    @app.delete("/api/threads/{thread_id}")
    def delete_thread(thread_id: str) -> dict[str, Any]:
        if any(run["thread_id"] == thread_id for run in agent.active_runs()):
            raise HTTPException(409, "Stop the running request first.")
        store.delete_thread(thread_id)
        bus.publish({"type": "thread_deleted", "thread_id": thread_id})
        return {"ok": True}

    @app.post("/api/threads/{thread_id}/messages")
    async def send(thread_id: str, body: SendMessage) -> dict[str, Any]:
        if store.get_thread(thread_id) is None:
            raise HTTPException(404, "No such conversation.")
        return {"run_id": agent.submit(thread_id, body.text.strip())}

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str) -> dict[str, Any]:
        return {"ok": agent.cancel(run_id)}

    @app.post("/api/approvals/{item_id}")
    def decide(item_id: str, body: Decision) -> dict[str, Any]:
        if body.decision not in {"once", "task", "always", "deny"}:
            raise HTTPException(400, "decision must be once, task, always or deny.")
        if not agent.decide(item_id, body.decision):
            raise HTTPException(409, "That approval is no longer waiting.")
        return {"ok": True}

    @app.post("/api/undo/{undo_id}")
    def undo(undo_id: str) -> dict[str, Any]:
        entry = store.get_undo(undo_id)
        if entry is None:
            raise HTTPException(404, "Nothing to undo.")
        if entry["done"]:
            raise HTTPException(409, "That was already undone.")
        try:
            message = file_tools.apply_undo(entry["kind"], entry["payload"], store)
        except ToolError as exc:
            raise HTTPException(409, str(exc)) from exc
        store.mark_undone(undo_id)
        if entry.get("item_id"):
            item = store.get_item(entry["item_id"])
            if item:
                data = {**item["data"], "undone": True}
                store.update_item(item["id"], data=data)
                bus.publish({"type": "tool_finished", "thread_id": item["thread_id"], "item": {**item, "data": data}})
        store.audit("undo", entry["payload"], "ok", message)
        return {"ok": True, "message": message}

    # ------------------------------------------------------------------ files and library
    @app.get("/api/library")
    def library() -> dict[str, Any]:
        rows = []
        for artifact in store.list_artifacts():
            exists = Path(artifact["path"]).exists()
            rows.append({**artifact, "exists": exists})
        return {"artifacts": rows}

    @app.delete("/api/library/{artifact_id}")
    def forget_artifact(artifact_id: str) -> dict[str, Any]:
        store.remove_artifact(artifact_id)
        return {"ok": True}

    @app.get("/api/files/raw")
    def raw_file(path: str) -> FileResponse:
        real = str(Path(path))
        if not store.artifact_known(real):
            raise HTTPException(403, "Aegis only serves files it created.")
        if not Path(real).is_file():
            raise HTTPException(404, "That file no longer exists.")
        return FileResponse(real, headers={"Cache-Control": "no-store"})

    @app.post("/api/files/open")
    def open_file(body: PathBody) -> dict[str, Any]:
        real = Path(body.path)
        if not store.artifact_known(str(real)):
            raise HTTPException(403, "Aegis only opens files it created from here.")
        if not real.exists():
            raise HTTPException(404, "That file no longer exists.")
        os.startfile(str(real))  # noqa: S606 - a file Aegis produced and registered
        return {"ok": True}

    @app.post("/api/files/reveal")
    def reveal_file(body: PathBody) -> dict[str, Any]:
        real = Path(body.path)
        if not store.artifact_known(str(real)):
            raise HTTPException(403, "Aegis only reveals files it created from here.")
        if not real.exists():
            raise HTTPException(404, "That file no longer exists.")
        subprocess.Popen(["explorer.exe", f"/select,{real}"], creationflags=subprocess.CREATE_NO_WINDOW)
        return {"ok": True}

    # ------------------------------------------------------------------ memory, reminders, to-dos
    @app.get("/api/memory")
    def memory() -> dict[str, Any]:
        return {"facts": store.memories()}

    @app.post("/api/memory")
    def add_memory(body: FactBody) -> dict[str, Any]:
        return {"id": store.remember(body.fact.strip())}

    @app.delete("/api/memory/{memory_id}")
    def delete_memory(memory_id: int) -> dict[str, Any]:
        return {"ok": store.forget(memory_id) > 0}

    @app.get("/api/reminders")
    def reminders() -> dict[str, Any]:
        return {"reminders": store.reminders(include_done=True)}

    @app.delete("/api/reminders/{reminder_id}")
    def cancel_reminder(reminder_id: str) -> dict[str, Any]:
        store.set_reminder(reminder_id, status="cancelled")
        bus.publish({"type": "reminders_changed"})
        return {"ok": True}

    @app.get("/api/todos")
    def todos() -> dict[str, Any]:
        return {"todos": store.todos(include_done=True)}

    @app.post("/api/todos")
    def add_todo(body: TodoBody) -> dict[str, Any]:
        todo = store.add_todo(body.text.strip())
        bus.publish({"type": "todos_changed"})
        return todo

    @app.post("/api/todos/{todo_id}/toggle")
    def toggle_todo(todo_id: str) -> dict[str, Any]:
        rows = {row["id"]: row for row in store.todos(include_done=True)}
        if todo_id not in rows:
            raise HTTPException(404, "No such item.")
        store.set_todo_done(todo_id, not rows[todo_id]["done"])
        bus.publish({"type": "todos_changed"})
        return {"ok": True}

    @app.delete("/api/todos/{todo_id}")
    def delete_todo(todo_id: str) -> dict[str, Any]:
        store.delete_todo(todo_id)
        bus.publish({"type": "todos_changed"})
        return {"ok": True}

    # ------------------------------------------------------------------ document search
    @app.get("/api/documents")
    def documents() -> dict[str, Any]:
        stats = {row["folder"]: row for row in store.index_stats()}
        folders = [{"folder": folder, **{key: stats.get(folder, {}).get(key, 0) for key in ("files", "chunks", "bytes")}} for folder in settings["doc_folders"]]
        return {"folders": folders, "indexing": runtime.indexing}

    def _index_in_background(folder: Path) -> None:
        def work() -> None:
            runtime.indexing = True
            bus.publish({"type": "index_progress", "text": f"Indexing {folder.name}…", "active": True})
            try:
                stats = knowledge.index_folder_now(store, folder, progress=lambda text: bus.publish({"type": "index_progress", "text": text, "active": True}))
                bus.publish({"type": "index_progress", "text": f"Indexed {stats['indexed']} files in {folder.name}.", "active": False})
            except Exception as exc:
                logger.exception("indexing failed")
                bus.publish({"type": "index_progress", "text": f"Indexing failed: {str(exc)[:160]}", "active": False})
            finally:
                runtime.indexing = False

        threading.Thread(target=work, daemon=True, name="aegis-index").start()

    @app.post("/api/documents")
    def add_folder(body: PathBody) -> dict[str, Any]:
        folder = paths.expand(body.path)
        if not folder.is_dir():
            raise HTTPException(400, f"{body.path} is not a folder on this PC.")
        folder = folder.resolve()
        settings.update({"doc_folders": sorted({*settings["doc_folders"], str(folder)})})
        _index_in_background(folder)
        return {"ok": True}

    @app.delete("/api/documents")
    def remove_folder(path: str) -> dict[str, Any]:
        settings.update({"doc_folders": [item for item in settings["doc_folders"] if item != path]})
        for document in store.indexed_paths(path):
            store.remove_document(document)
        return {"ok": True}

    # ------------------------------------------------------------------ settings and capabilities
    @app.patch("/api/settings")
    def patch_settings(patch: dict[str, Any]) -> dict[str, Any]:
        try:
            updated = settings.update(patch)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if "offline" in patch:
            netguard.set_offline(bool(patch["offline"]))
        if "use_gpu" in patch:
            llm.force_cpu = False
            threading.Thread(target=_restart_model, daemon=True).start()
        bus.publish({"type": "settings_changed", "settings": updated})
        return updated

    def _restart_model() -> None:
        llm.restart_server()
        llm.pick_model()
        bus.publish({"type": "model_status"})

    @app.get("/api/capabilities")
    def capabilities() -> dict[str, Any]:
        offline = bool(settings["offline"])
        return {"tools": [{"name": tool.name, "description": tool.description, "group": tool.group, "risk": tool.risk, "web": tool.web, "enabled": tool in available_tools(offline), "always_allowed": tool.name in settings["always_allow"]} for tool in REGISTRY.values()]}

    @app.get("/api/activity")
    def activity(limit: int = 200) -> dict[str, Any]:
        return {"entries": store.audit_entries(min(limit, 1000)), "chain_valid": store.audit_valid()}

    # ------------------------------------------------------------------ models
    @app.post("/api/models/select")
    def select_model(body: ModelBody) -> dict[str, Any]:
        if body.model not in [item["name"] for item in llm.installed()]:
            raise HTTPException(400, f"{body.model} is not installed.")
        settings.update({"model": body.model})
        llm.pick_model()
        threading.Thread(target=lambda: (llm.warm(), bus.publish({"type": "model_status"})), daemon=True).start()
        bus.publish({"type": "model_status"})
        return {"ok": True, "model": body.model}

    @app.post("/api/models/pull")
    def pull_model(body: ModelBody) -> dict[str, Any]:
        name = body.model.strip()
        if not re.fullmatch(r"[\w.:/-]{1,80}", name):
            raise HTTPException(400, "That is not a valid model name.")
        if name in runtime.pulling:
            return {"ok": True, "already": True}
        runtime.pulling[name] = {"status": "starting"}

        def work() -> None:
            def progress(event: dict[str, Any]) -> None:
                bus.publish({"type": "model_pull", "model": name, "status": event.get("status"), "completed": event.get("completed"), "total": event.get("total")})

            try:
                llm.pull(name, progress)
                llm.pick_model()
                bus.publish({"type": "model_pull", "model": name, "status": "success", "done": True})
                bus.publish({"type": "model_status"})
            except Exception as exc:
                bus.publish({"type": "model_pull", "model": name, "status": f"failed: {str(exc)[:200]}", "done": True, "error": True})
            finally:
                runtime.pulling.pop(name, None)

        threading.Thread(target=work, daemon=True, name="aegis-pull").start()
        return {"ok": True}

    # ------------------------------------------------------------------ voice
    @app.post("/api/voice/start")
    def voice_start(body: VoiceStart) -> dict[str, Any]:
        try:
            runtime.voice.start(body.owner)
        except Exception as exc:
            raise HTTPException(409, f"Could not open the microphone: {str(exc)[:200]}") from exc
        return {"ok": True}

    @app.post("/api/voice/stop")
    def voice_stop() -> dict[str, Any]:
        runtime.voice.stop()
        return {"ok": True}

    @app.post("/api/voice/cancel")
    def voice_cancel() -> dict[str, Any]:
        runtime.voice.cancel()
        return {"ok": True}

    @app.get("/api/voice/devices")
    def voice_devices() -> dict[str, Any]:
        try:
            return runtime.voice.devices()
        except Exception as exc:
            logger.info("could not list microphones: %s", exc)
            return {"default": None, "inputs": []}

    @app.get("/api/speech")
    def speech_status() -> dict[str, Any]:
        return speech.status(settings["speech_engine"])

    @app.post("/api/speech/select")
    def speech_select(body: SpeechChoice) -> dict[str, Any]:
        if not speech.is_installed(body.id):
            raise HTTPException(409, "That speech model has not been downloaded.")
        bus.publish({"type": "settings_changed", "settings": settings.update({"speech_engine": body.id})})
        bus.publish({"type": "speech_changed"})
        return {"ok": True}

    @app.post("/api/speech/download")
    def speech_download(body: SpeechChoice) -> dict[str, Any]:
        try:
            speech.start_download(body.id, bus.publish)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    # ------------------------------------------------------------------ native shell bridge
    @app.post("/api/shell/spotlight")
    def spotlight(body: SpotlightBody) -> dict[str, Any]:
        if runtime.shell:
            runtime.shell.spotlight_command(body.action, body.height, body.width)
        return {"ok": True}

    @app.post("/api/shell/theme")
    def shell_theme(body: dict[str, Any]) -> dict[str, Any]:
        if runtime.shell:
            runtime.shell.set_titlebar(str(body.get("mode", "dark")))
        return {"ok": True}

    # ------------------------------------------------------------------ the UI
    dist = config.WEB_DIST
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def ui(path: str) -> Response:
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        index = dist / "index.html"
        if not index.is_file():
            return HTMLResponse("<h1>The Aegis UI has not been built yet.</h1><p>Run <code>npm install</code> and <code>npm run build</code> in the web folder.</p>", status_code=503)
        html = index.read_text(encoding="utf-8").replace("</head>", f'<meta name="aegis-token" content="{API_TOKEN}"></head>', 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    return Guard(app, lambda: runtime.port)
