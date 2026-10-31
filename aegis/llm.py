"""The local model: a private Ollama server, streaming chat, and tolerant parsing of tool calls."""
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
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import psutil

from aegis.config import MODEL_NEEDS_GB, RECOMMENDED_MODELS, Settings

logger = logging.getLogger("aegis.llm")
PRIVATE_PORT = 11435
SYSTEM_PORT = 11434
_NO_WINDOW = subprocess.CREATE_NO_WINDOW


class ModelUnavailable(RuntimeError):
    pass


def _ollama_exe() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(candidate) if candidate.exists() else None


class _JobObject:
    """Kills the private Ollama server with this process, even after a crash."""

    def __init__(self) -> None:
        self.handle = None
        try:
            import win32job

            self.handle = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation, info)
        except Exception as exc:  # orphan protection is best-effort
            logger.info("job object unavailable: %s", exc)

    def assign(self, process: subprocess.Popen) -> None:
        if self.handle is None:
            return
        try:
            import win32api
            import win32con
            import win32job

            handle = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, process.pid)
            win32job.AssignProcessToJobObject(self.handle, handle)
        except Exception as exc:
            logger.info("could not assign Ollama to job: %s", exc)


class LLM:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base = f"http://127.0.0.1:{PRIVATE_PORT}"
        self.model: str | None = None
        self.force_cpu = False
        self.note = ""
        self._process: subprocess.Popen | None = None
        self._job = _JobObject()
        self._capabilities: dict[str, list[str]] = {}
        self._no_think: set[str] = set()
        self._start_lock = threading.Lock()
        self.stats = {"calls": 0, "last_ms": None, "last_tps": None}
        self._client = httpx.Client(timeout=httpx.Timeout(600, connect=5), trust_env=False)

    # ------------------------------------------------------------------ server lifecycle
    def _alive(self, base: str) -> bool:
        try:
            return self._client.get(base + "/api/version", timeout=1.5).status_code == 200
        except httpx.HTTPError:
            return False

    def ensure_server(self) -> bool:
        """Use (or start) a private server configured for this machine, or fall back to the system one."""
        with self._start_lock:
            private = f"http://127.0.0.1:{PRIVATE_PORT}"
            if self._process is not None and self._process.poll() is None and self._alive(private):
                self.base = private
                return True
            if self._alive(private):
                self.base = private
                return True
            exe = _ollama_exe()
            if exe:
                env = {**os.environ, "OLLAMA_HOST": f"127.0.0.1:{PRIVATE_PORT}", "OLLAMA_KEEP_ALIVE": "10m", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "2", "OLLAMA_NO_CLOUD": "1", "OLLAMA_NOHISTORY": "1"}
                if self.settings["use_gpu"]:
                    # Intel integrated GPUs are skipped unless asked for; on an Arc iGPU this is 3-4x faster than the CPU.
                    env.update({"OLLAMA_VULKAN": "1", "OLLAMA_IGPU_ENABLE": "1"})
                else:
                    env["OLLAMA_VULKAN"] = "0"
                try:
                    self._process = subprocess.Popen([exe, "serve"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
                    self._job.assign(self._process)
                except OSError as exc:
                    logger.warning("could not start Ollama: %s", exc)
                    self._process = None
                deadline = time.monotonic() + 15
                while self._process is not None and time.monotonic() < deadline:
                    if self._alive(private):
                        self.base = private
                        return True
                    if self._process.poll() is not None:
                        break
                    time.sleep(0.2)
            system = f"http://127.0.0.1:{SYSTEM_PORT}"
            if self._alive(system):
                self.base = system
                self.note = "Using the system Ollama server, so GPU settings could not be applied."
                return True
            return False

    def restart_server(self) -> bool:
        self.shutdown()
        self._capabilities.clear()
        return self.ensure_server()

    def shutdown(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, creationflags=_NO_WINDOW)
        self._client_safe_close()

    def _client_safe_close(self) -> None:
        # The client is reused after restart_server, so it stays open.
        pass

    # ------------------------------------------------------------------ models
    def installed(self) -> list[dict[str, Any]]:
        try:
            return self._client.get(self.base + "/api/tags", timeout=5).json().get("models", [])
        except (httpx.HTTPError, ValueError):
            return []

    def capabilities(self, model: str) -> list[str]:
        cached = self._capabilities.get(model)
        if cached is None:
            try:
                cached = self._client.post(self.base + "/api/show", json={"model": model}, timeout=10).json().get("capabilities") or []
            except (httpx.HTTPError, ValueError):
                return []
            self._capabilities[model] = cached
        return cached

    def pick_model(self) -> str | None:
        installed = [item["name"] for item in self.installed()]
        chosen = self.settings["model"]
        if chosen in installed:
            self.model = chosen
            return chosen
        # A heavier model is only worth it if it fits alongside what the user has open right now.
        free = psutil.virtual_memory().available / 1e9
        running = {item.get("name") for item in self.loaded()}
        usable = [name for name in RECOMMENDED_MODELS if name in installed]
        for name in usable:
            if name in running or free >= MODEL_NEEDS_GB.get(name, 0):
                self.model = name
                return name
        if usable:
            self.model = usable[-1]
            return usable[-1]
        for name in installed:
            if "tools" in self.capabilities(name):
                self.model = name
                return name
        self.model = None
        return None

    def vision_model(self) -> str | None:
        current = self.model or self.pick_model()
        if current and "vision" in self.capabilities(current):
            return current
        for item in self.installed():
            if "vision" in self.capabilities(item["name"]):
                return item["name"]
        return None

    def loaded(self) -> list[dict[str, Any]]:
        try:
            return self._client.get(self.base + "/api/ps", timeout=3).json().get("models", [])
        except (httpx.HTTPError, ValueError):
            return []

    def is_loaded(self, model: str | None = None) -> bool:
        target = model or self.model
        return any(item.get("name") == target for item in self.loaded())

    def status(self) -> dict[str, Any]:
        reachable = self._alive(self.base)
        installed = self.installed() if reachable else []
        if reachable and not self.model:
            self.pick_model()
        running = next((item for item in self.loaded() if item.get("name") == self.model), None) if reachable else None
        gpu_share = None
        if running and running.get("size"):
            gpu_share = round(100 * (running.get("size_vram") or 0) / running["size"])
        return {
            "reachable": reachable,
            "server": self.base,
            "ollama_installed": _ollama_exe() is not None,
            "model": self.model,
            "loaded": running is not None,
            "gpu_percent": gpu_share,
            "force_cpu": self.force_cpu,
            "note": self.note,
            "installed": [{"name": item["name"], "size": item.get("size"), "capabilities": self.capabilities(item["name"])} for item in installed],
            "recommended": RECOMMENDED_MODELS[0],
            "stats": dict(self.stats),
        }

    def _options(self, **extra: Any) -> dict[str, Any]:
        options: dict[str, Any] = {"num_ctx": int(self.settings["num_ctx"]), "temperature": 0.1, **extra}
        if self.force_cpu:
            options["num_gpu"] = 0
        return options

    def _body(self, model: str, messages: list[dict[str, Any]], *, stream: bool, tools: list[dict[str, Any]] | None = None, options: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": stream, "keep_alive": "10m", "options": self._options(**(options or {}))}
        if tools:
            body["tools"] = tools
        if model not in self._no_think:
            body["think"] = False
        return body

    def _record(self, payload: dict[str, Any], started: float) -> dict[str, Any]:
        self.stats["calls"] += 1
        self.stats["last_ms"] = round((time.perf_counter() - started) * 1000)
        duration = payload.get("eval_duration") or 0
        if duration:
            self.stats["last_tps"] = round(payload.get("eval_count", 0) / (duration / 1e9), 1)
        return {"prompt_tokens": payload.get("prompt_eval_count"), "output_tokens": payload.get("eval_count"), "ms": self.stats["last_ms"]}

    def _should_fall_back(self, status: int, text: str) -> bool:
        return status >= 500 and not self.force_cpu and bool(re.search(r"vulkan|memory|alloc|runner|gpu|device", text, re.IGNORECASE))

    # ------------------------------------------------------------------ chat
    async def chat(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None, on_token: Callable[[str], Awaitable[None]] | None = None, on_clear: Callable[[], Awaitable[None]] | None = None) -> dict[str, Any]:
        """Stream one assistant turn. Returns {content, tool_calls, stats}."""
        model = self.model or self.pick_model()
        if not model:
            raise ModelUnavailable("No local model is installed.")
        for attempt in range(3):
            body = self._body(model, messages, stream=True, tools=tools)
            filt = _StreamFilter(on_token)
            calls: list[dict[str, Any]] = []
            final: dict[str, Any] = {}
            started = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=5), trust_env=False) as client:
                    async with client.stream("POST", self.base + "/api/chat", json=body) as response:
                        if response.status_code != 200:
                            detail = (await response.aread()).decode(errors="replace")[:400]
                            if response.status_code == 400 and "think" in detail.lower():
                                self._no_think.add(model)
                                continue
                            if self._should_fall_back(response.status_code, detail):
                                logger.warning("model failed on the GPU, retrying on the CPU: %s", detail)
                                self.force_cpu = True
                                self.note = "The GPU could not run this model, so Aegis is using the CPU."
                                continue
                            raise ModelUnavailable(_friendly_error(detail))
                        async for line in response.aiter_lines():
                            if not line.strip():
                                continue
                            payload = json.loads(line)
                            if payload.get("error"):
                                raise ModelUnavailable(_friendly_error(str(payload["error"])))
                            message = payload.get("message") or {}
                            if message.get("content"):
                                await filt.feed(message["content"])
                            if message.get("tool_calls"):
                                calls.extend(message["tool_calls"])
                            if payload.get("done"):
                                final = payload
            except httpx.TransportError as exc:
                if attempt == 0 and await asyncio.to_thread(self.ensure_server):
                    continue
                raise ModelUnavailable(f"The local model server stopped responding: {exc}") from exc
            text = await filt.finish()
            parsed = [{"name": call.get("function", {}).get("name", ""), "arguments": call.get("function", {}).get("arguments", {})} for call in calls]
            if not parsed and filt.held:
                parsed = parse_text_tool_calls(filt.held)
                if parsed:
                    text = ""
                else:
                    text = filt.held.strip()
                    if on_token:
                        await on_token(text)
            elif not parsed and text:
                parsed = parse_text_tool_calls(text)
                if parsed:
                    text = ""
                    if on_clear:
                        await on_clear()
            return {"content": text.strip(), "tool_calls": parsed, "stats": self._record(final, started)}
        raise ModelUnavailable("The local model could not complete the request.")

    def complete(self, prompt: str, *, system: str = "", images: list[str] | None = None, model: str | None = None, max_tokens: int = 500) -> str:
        """A single blocking, tool-free completion, for use inside tools."""
        model = model or self.model or self.pick_model()
        if not model:
            raise ModelUnavailable("No local model is installed.")
        message: dict[str, Any] = {"role": "user", "content": prompt}
        if images:
            message["images"] = images
        messages = ([{"role": "system", "content": system}] if system else []) + [message]
        for _ in range(2):
            started = time.perf_counter()
            body = self._body(model, messages, stream=False, options={"num_predict": max_tokens})
            response = self._client.post(self.base + "/api/chat", json=body)
            if response.status_code == 200:
                payload = response.json()
                self._record(payload, started)
                return _strip_think((payload.get("message") or {}).get("content", ""))
            if response.status_code == 400 and "think" in response.text.lower():
                self._no_think.add(model)
                continue
            if self._should_fall_back(response.status_code, response.text):
                self.force_cpu = True
                self.note = "The GPU could not run this model, so Aegis is using the CPU."
                continue
            raise ModelUnavailable(_friendly_error(response.text))
        raise ModelUnavailable("The local model could not complete the request.")

    def complete_json(self, prompt: str, schema: dict[str, Any], *, system: str = "") -> dict[str, Any]:
        """A blocking completion that must fit a JSON schema."""
        model = self.model or self.pick_model()
        if not model:
            raise ModelUnavailable("No local model is installed.")
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body = self._body(model, messages, stream=False, options={"num_predict": 120})
        body["format"] = schema
        response = self._client.post(self.base + "/api/chat", json=body)
        if response.status_code != 200:
            raise ModelUnavailable(_friendly_error(response.text))
        return json.loads((response.json().get("message") or {}).get("content") or "{}")

    def warm(self, messages: list[dict[str, Any]] | None = None, tools: list[dict[str, Any]] | None = None) -> None:
        """Load the model and cache the system prompt, so the first real request starts fast."""
        model = self.model or self.pick_model()
        if not model:
            return
        body = self._body(model, messages or [{"role": "user", "content": "ok"}], stream=False, tools=tools, options={"num_predict": 1})
        try:
            self._client.post(self.base + "/api/chat", json=body, timeout=300)
        except httpx.HTTPError as exc:
            logger.info("warm-up failed: %s", exc)

    def pull(self, model: str, on_progress: Callable[[dict[str, Any]], None]) -> None:
        """Download a model. Ollama's own process does the network access; Aegis only talks to loopback."""
        with self._client.stream("POST", self.base + "/api/pull", json={"model": model, "stream": True}, timeout=httpx.Timeout(None, connect=5)) as response:
            for line in response.iter_lines():
                if line.strip():
                    event = json.loads(line)
                    if event.get("error"):
                        raise ModelUnavailable(event["error"])
                    on_progress(event)


def _friendly_error(detail: str) -> str:
    text = detail.strip()
    try:
        text = json.loads(text).get("error", text)
    except (ValueError, AttributeError):
        pass
    if "not found" in text.lower():
        return f"The model is not installed ({text})."
    if "memory" in text.lower():
        return f"There is not enough free memory to load the model. Close some apps and try again. ({text[:160]})"
    return f"The local model returned an error: {text[:300]}"


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


class _StreamFilter:
    """Hides reasoning, and holds back anything that may be a tool call written as text."""

    def __init__(self, emit: Callable[[str], Awaitable[None]] | None) -> None:
        self.emit = emit
        self.buffer = ""
        self.text = ""
        self.held = ""
        self.mode = "start"  # start | think | text | hold

    async def _out(self, value: str) -> None:
        if value:
            self.text += value
            if self.emit:
                await self.emit(value)

    async def feed(self, chunk: str) -> None:
        self.buffer += chunk
        while self.buffer:
            if self.mode == "think":
                end = self.buffer.find("</think>")
                if end < 0:
                    self.buffer = self.buffer[-8:]
                    return
                self.buffer = self.buffer[end + 8:]
                self.mode = "start"
            elif self.mode == "start":
                self.buffer = self.buffer.lstrip()
                if not self.buffer:
                    return
                if self.buffer.startswith("<think>"):
                    self.buffer = self.buffer[7:]
                    self.mode = "think"
                elif "<think>".startswith(self.buffer) or "<tool_call>".startswith(self.buffer) or "```".startswith(self.buffer):
                    return  # might still become one of these
                elif self.buffer.startswith(("{", "[{", "<tool_call>", "```json", "```")):
                    self.mode = "hold"
                else:
                    self.mode = "text"
            elif self.mode == "hold":
                self.held += self.buffer
                self.buffer = ""
            else:  # text
                marker = self.buffer.find("<tool_call>")
                if marker >= 0:
                    await self._out(self.buffer[:marker])
                    self.held += self.buffer[marker:]
                    self.buffer = ""
                    self.mode = "hold"
                else:
                    await self._out(self.buffer)
                    self.buffer = ""

    async def finish(self) -> str:
        if self.mode == "start" and self.buffer:
            await self._out(self.buffer)
        self.buffer = ""
        return self.text


_TAGGED = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.DOTALL)
_FENCED = re.compile(r"^```(?:json|tool_call)?\s*(.*?)\s*```$", re.DOTALL)


def parse_text_tool_calls(text: str) -> list[dict[str, Any]]:
    """Some models write the call in their reply instead of using the tool-call channel."""
    source = text.strip()
    fenced = _FENCED.match(source)
    if fenced:
        source = fenced.group(1)
    tagged = _TAGGED.findall(source)
    candidates = tagged or [source]
    calls: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        for entry in value if isinstance(value, list) else [value]:
            if not isinstance(entry, dict):
                continue
            function = entry.get("function") if isinstance(entry.get("function"), dict) else entry
            name = function.get("name") or function.get("tool") or function.get("tool_name")
            arguments = function.get("arguments", function.get("parameters", function.get("args", {})))
            if isinstance(name, str) and name:
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = {}
                calls.append({"name": name, "arguments": arguments if isinstance(arguments, dict) else {}})
    return calls
