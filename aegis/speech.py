"""Speech recognition on this PC: which models exist, where they live, and one way to run any of them."""
from __future__ import annotations

import json
import difflib
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from aegis import config

logger = logging.getLogger("aegis.speech")
SAMPLE_RATE = 16_000
IDLE_UNLOAD_SECONDS = 15 * 60
COMPLETE_MARKER = ".complete"


@dataclass(frozen=True)
class Engine:
    id: str
    label: str
    detail: str
    size_mb: int
    kind: str  # "parakeet" or "whisper"
    repo: str
    files: tuple[str, ...] = ()


# Best-first. Measured on real recordings of people on their own microphones: see README.
ENGINES: dict[str, Engine] = {
    engine.id: engine
    for engine in (
        Engine("parakeet-v2", "Parakeet TDT 0.6 v2", "Most accurate, and fast enough to show your words as you talk. English.", 670, "parakeet", "istupakov/parakeet-tdt-0.6b-v2-onnx",
               ("config.json", "encoder-model.int8.onnx", "decoder_joint-model.int8.onnx", "vocab.txt")),
        Engine("whisper-small", "Whisper small", "Lighter on memory, but slower and less accurate. English.", 490, "whisper", "Systran/faster-whisper-small.en"),
    )
}
DEFAULT_ENGINE = next(iter(ENGINES))


def model_dir(engine_id: str) -> Path:
    return config.MODELS_DIR / "speech" / engine_id


def is_installed(engine_id: str) -> bool:
    return (model_dir(engine_id) / COMPLETE_MARKER).is_file()


def installed_engines() -> list[str]:
    return [engine_id for engine_id in ENGINES if is_installed(engine_id)]


def resolve_engine(chosen: str) -> str | None:
    """The chosen model if it is here, otherwise the best one that is."""
    if chosen in ENGINES and is_installed(chosen):
        return chosen
    available = installed_engines()
    return available[0] if available else None


# Recognizers are trained on general speech. These are the words people say to a desktop assistant.
_APPS = ("Chrome", "Edge", "Firefox", "Brave", "Notepad", "WordPad", "Word", "Excel", "PowerPoint", "Outlook", "Teams", "OneNote", "Spotify", "VLC", "Discord", "Slack", "Zoom", "Steam", "Telegram", "WhatsApp",
         "File Explorer", "Task Manager", "Control Panel", "Settings", "Calculator", "Paint", "Photos", "Camera", "Terminal", "PowerShell", "Command Prompt", "VS Code", "Visual Studio", "Snipping Tool", "Clock", "Calendar", "Mail")
_APP_BY_LOWER = {name.lower(): name for name in _APPS}
_EXTENSIONS = "pdf|docx|doc|txt|csv|xlsx|xls|pptx|ppt|png|jpg|jpeg|gif|mp3|mp4|mkv|wav|zip|py|md|html|json|exe"
_APP_COMMAND = re.compile(r"^((?:please\s+)?(?:open|close|launch|quit|exit|kill|focus|switch to|minimi[sz]e|maximi[sz]e)\s+(?:the\s+|my\s+)?)(.+?)([.?!]?)$", re.IGNORECASE)


def tidy_command(text: str) -> str:
    """Repairs what speech recognition predictably gets wrong for this job, without touching anything else."""
    text = re.sub(r"\b([ap])\.\s?m\.?(?=\s|$)", lambda found: f"{found.group(1).lower()}m", text, flags=re.IGNORECASE)
    text = re.sub(rf"(\w)\s+dot\s+({_EXTENSIONS})\b", lambda found: f"{found.group(1)}.{found.group(2).lower()}", text, flags=re.IGNORECASE)
    text = re.sub(r"(\w)(?:\.|\s+dot\s+)docs\b", r"\1.docx", text, flags=re.IGNORECASE)
    # "Save it in MD" comes out as the hash, and "write this in it" as the direction.
    text = re.sub(r"\b(in|as|to)\s+(?:an?\s+)?md5\b", r"\1 md", text, flags=re.IGNORECASE)
    text = re.sub(r"^right,?\s+(?=.+\b(?:in it|into it|to it|in the file|in that file)\b)", "Write ", text, flags=re.IGNORECASE)
    command = _APP_COMMAND.match(text.strip())
    if command:
        lead, name, end = command.groups()
        lowered = name.lower()
        if lowered not in _APP_BY_LOWER:
            # Same first letter and close in spelling: "close crow" and "open fine explorer", but not "open the door".
            close = [option for option in difflib.get_close_matches(lowered, _APP_BY_LOWER, n=3, cutoff=0.6) if option[0] == lowered[0]]
            if close:
                return f"{lead}{_APP_BY_LOWER[close[0]]}{end}"
    return text


@dataclass
class Transcript:
    text: str
    confident: bool
    score: float = 0.0


class _Parakeet:
    # Below this average per-token log-probability, the recording is too unclear to act on unread.
    # Calibrated: 99% of real speech passes; speech from across a room and unknown languages never did.
    CONFIDENT_ABOVE = -0.25

    def __init__(self, engine: Engine) -> None:
        import onnx_asr
        import onnxruntime as rt

        options = rt.SessionOptions()
        options.intra_op_num_threads = _thread_count()
        # Waiting threads should sleep, or a short phrase keeps every core busy for no gain.
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self.model = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v2", path=str(model_dir(engine.id)), quantization="int8", sess_options=options, providers=["CPUExecutionProvider"]).with_timestamps()

    def transcribe(self, audio: np.ndarray, quick: bool = False) -> Transcript:
        result = self.model.recognize(audio, sample_rate=SAMPLE_RATE)
        text = tidy_command((result.text or "").strip())
        probabilities = result.logprobs or []
        score = float(np.mean(probabilities)) if probabilities else -9.0
        return Transcript(text, bool(text) and score > self.CONFIDENT_ABOVE, score)


class _Whisper:
    # Biases recognition toward what people say to a desktop assistant.
    HINT = "Aegis, open Notepad, VS Code, screenshot, Downloads, Desktop, remind me, volume, Excel, PDF."

    def __init__(self, engine: Engine) -> None:
        from faster_whisper import WhisperModel

        self.model = WhisperModel(str(model_dir(engine.id)), device="cpu", compute_type="int8", cpu_threads=_thread_count(), local_files_only=True)

    def transcribe(self, audio: np.ndarray, quick: bool = False) -> Transcript:
        segments, _ = self.model.transcribe(audio, language="en", beam_size=1 if quick else 5, initial_prompt=self.HINT, condition_on_previous_text=False)
        # Whisper invents fluent text on noise and on a nearby TV; these per-segment scores give that away.
        kept = [segment for segment in segments if segment.no_speech_prob < 0.85 and segment.avg_logprob > -1.25]
        text = tidy_command(" ".join(segment.text.strip() for segment in kept).strip())
        confident = bool(kept) and all(segment.avg_logprob > -0.7 and segment.no_speech_prob < 0.5 for segment in kept)
        score = min((segment.avg_logprob for segment in kept), default=-9.0)
        return Transcript(text, confident, score)


def _thread_count() -> int:
    # The low-power cores slow a shared workload down, so stay near the fast core count.
    return max(2, min(8, (os.cpu_count() or 4) // 2))


class _Worker:
    """Runs the model in its own process. Loading one freezes every Python thread for seconds, which in
    the Aegis process would drop microphone audio and hang the interface. Exiting also returns all its memory."""

    def __init__(self, engine_id: str) -> None:
        self.engine_id = engine_id
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (config.LOG_DIR / "speech.log").open("ab") as log:
            self.process = subprocess.Popen([_python(), "-m", "aegis.speech", "--serve", engine_id], cwd=config.ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        started = time.perf_counter()
        self._reply()  # waits until the model has loaded
        logger.info("loaded speech model %s in %.1f s", engine_id, time.perf_counter() - started)

    def _reply(self) -> dict[str, Any]:
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("The speech model stopped unexpectedly. See speech.log.")
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply

    def transcribe(self, audio: np.ndarray, quick: bool) -> Transcript:
        self.process.stdin.write(json.dumps({"samples": len(audio), "quick": quick}).encode() + b"\n")
        self.process.stdin.write(audio.astype(np.float32).tobytes())
        self.process.stdin.flush()
        return Transcript(**self._reply())

    def close(self) -> None:
        # With stdin closed the worker leaves by itself. That also covers Aegis crashing.
        try:
            self.process.stdin.close()
            self.process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()


_worker: _Worker | None = None
_last_used = 0.0
_lock = threading.Lock()
_watcher_started = False


def _unload_when_idle() -> None:
    global _worker
    while True:
        time.sleep(30)
        with _lock:
            if _worker is not None and time.monotonic() - _last_used > IDLE_UNLOAD_SECONDS:
                logger.info("unloading idle speech model %s", _worker.engine_id)
                _worker.close()
                _worker = None


def _ensure_worker(engine_id: str) -> _Worker:
    global _worker, _watcher_started, _last_used
    _last_used = time.monotonic()
    if _worker is not None and (_worker.engine_id != engine_id or _worker.process.poll() is not None):
        _worker.close()
        _worker = None
    if _worker is None:
        _worker = _Worker(engine_id)
    if not _watcher_started:
        _watcher_started = True
        threading.Thread(target=_unload_when_idle, daemon=True, name="aegis-speech-idle").start()
    return _worker


def warm(engine_id: str) -> None:
    """Has the model loaded by the time it is needed."""
    with _lock:
        _ensure_worker(engine_id)


def transcribe(engine_id: str, audio: np.ndarray, quick: bool = False) -> Transcript:
    """Runs a model that is already on this PC. Nothing here can download."""
    global _worker, _last_used
    with _lock:
        for attempt in (1, 2):
            try:
                result = _ensure_worker(engine_id).transcribe(audio, quick)
                _last_used = time.monotonic()
                return result
            except (OSError, RuntimeError, ValueError):
                if _worker is not None:
                    _worker.close()
                    _worker = None
                if attempt == 2:
                    raise
    raise AssertionError("unreachable")


def shutdown() -> None:
    global _worker
    with _lock:
        if _worker is not None:
            _worker.close()
            _worker = None


def _python() -> str:
    """The console build, whose standard streams always work, without showing a console."""
    console = Path(sys.executable).with_name("python.exe")
    return str(console if console.exists() else sys.executable)


def speech_spans(audio: np.ndarray, *, gap_ms: int = 700) -> list[tuple[int, int]]:
    """Where the speech is, as sample offsets, using the voice detector that ships with faster-whisper."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    spans = get_speech_timestamps(audio, VadOptions(threshold=0.4, min_silence_duration_ms=gap_ms, speech_pad_ms=200))
    return [(span["start"], span["end"]) for span in spans]


def transcribe_long(engine_id: str, audio: np.ndarray, progress: Callable[[float], None] | None = None, cancelled: Callable[[], None] | None = None) -> str:
    """Transcribes a whole recording piece by piece, so length costs time rather than memory."""
    pieces: list[str] = []
    limit = 25 * SAMPLE_RATE
    chunks: list[tuple[int, int]] = []
    for start, end in speech_spans(audio):
        while end - start > limit:
            chunks.append((start, start + limit))
            start += limit
        chunks.append((start, end))
    for start, end in chunks:
        if cancelled:
            cancelled()
        text = transcribe(engine_id, audio[start:end]).text
        if text:
            pieces.append(text)
        if progress:
            progress(end / max(len(audio), 1))
    return " ".join(pieces).strip()


# ---------------------------------------------------------------------- download
_download: dict[str, Any] | None = None


def download_state() -> dict[str, Any] | None:
    return _download


def status(chosen: str) -> dict[str, Any]:
    active = resolve_engine(chosen)
    return {
        "ready": active is not None,
        "active": active,
        "chosen": chosen,
        "engines": [{"id": engine.id, "label": engine.label, "detail": engine.detail, "size_mb": engine.size_mb, "installed": is_installed(engine.id)} for engine in ENGINES.values()],
        "download": _download,
    }


def _folder_size(folder: Path) -> int:
    return sum(file.stat().st_size for file in folder.rglob("*") if file.is_file()) if folder.exists() else 0


def start_download(engine_id: str, publish: Callable[[dict[str, Any]], None]) -> None:
    """Runs in a helper process, because the Aegis process itself cannot reach the internet while offline."""
    global _download
    if engine_id not in ENGINES:
        raise ValueError("Unknown speech model.")
    if _download is not None:
        return
    engine = ENGINES[engine_id]
    _download = {"id": engine_id, "percent": 0, "status": "Starting the download"}

    def work() -> None:
        global _download
        env = {key: value for key, value in os.environ.items() if key not in {"HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"}}
        helper = subprocess.Popen([_python(), "-m", "aegis.speech", engine_id], cwd=config.ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        output: list[str] = []
        threading.Thread(target=lambda: output.extend(helper.stdout), daemon=True).start()  # type: ignore[arg-type]
        while helper.poll() is None:
            percent = min(99, round(_folder_size(model_dir(engine_id)) / (engine.size_mb * 1e6) * 100))
            _download = {"id": engine_id, "percent": percent, "status": f"Downloading {engine.label}: {percent}%"}
            publish({"type": "speech_download", **_download})
            time.sleep(0.7)
        _download = None
        if helper.returncode == 0 and is_installed(engine_id):
            publish({"type": "speech_download", "id": engine_id, "status": "done", "done": True})
        else:
            logger.warning("speech download failed: %s", "".join(output)[-600:])
            publish({"type": "speech_download", "id": engine_id, "status": "Check your internet connection and try again.", "done": True, "error": True})

    threading.Thread(target=work, daemon=True, name="aegis-speech-download").start()


def download(engine_id: str) -> None:
    from huggingface_hub import snapshot_download

    engine = ENGINES[engine_id]
    target = model_dir(engine_id)
    target.mkdir(parents=True, exist_ok=True)
    snapshot_download(engine.repo, local_dir=target, allow_patterns=list(engine.files) or None)
    (target / COMPLETE_MARKER).write_text(json.dumps({"repo": engine.repo, "at": time.time()}), encoding="utf-8")


def _serve(engine_id: str) -> None:
    """The worker process: one request at a time over stdin and stdout, until Aegis closes stdin."""
    from aegis import netguard

    netguard.install(True)
    engine = ENGINES[engine_id]
    model = (_Parakeet if engine.kind == "parakeet" else _Whisper)(engine)
    incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer
    outgoing.write(b'{"ready": true}\n')
    outgoing.flush()
    while header := incoming.readline():
        request = json.loads(header)
        audio = np.frombuffer(incoming.read(request["samples"] * 4), dtype=np.float32)
        try:
            result = model.transcribe(audio, quick=request["quick"])
            reply = {"text": result.text, "confident": result.confident, "score": result.score}
        except Exception as exc:  # the next request may well work, so stay up
            logger.exception("recognition failed")
            reply = {"error": str(exc)[:300]}
        outgoing.write(json.dumps(reply).encode() + b"\n")
        outgoing.flush()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--serve"]:
        _serve(sys.argv[2])
    else:
        # The download helper, and the launcher's first-run setup.
        name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ENGINE
        if not is_installed(name):
            download(name)
