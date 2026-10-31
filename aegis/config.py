"""Where Aegis keeps its data, and the user's settings."""
from __future__ import annotations

import getpass
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("AEGIS_HOME") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Aegis2")
DB_PATH = HOME / "aegis.db"
LOG_DIR = HOME / "logs"
UNDO_DIR = HOME / "undo"
WORKSPACE = HOME / "workspace"
SETTINGS_PATH = HOME / "settings.json"
RUNTIME_PATH = HOME / "runtime.json"
# Models are large and hold nothing personal, so test and development data folders share them.
MODELS_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Aegis2" / "models"
WEB_DIST = ROOT / "web" / "dist"

DEFAULT_PORT = 8421
# Best-first. Measured on a Core Ultra 5 / Arc iGPU with 16 GB: see README.
RECOMMENDED_MODELS = ["gemma4:e4b", "gemma4:e2b", "qwen3.5:2b", "qwen3.5:4b", "qwen2.5:3b"]
# Free memory, in GB, a model needs before it's chosen automatically. Below this it would push the user's apps into the page file.
MODEL_NEEDS_GB = {"gemma4:e4b": 8.4}

DEFAULTS: dict[str, Any] = {
    "user_name": getpass.getuser().split(".")[0].capitalize(),
    "model": "",  # empty: pick the best installed model
    "num_ctx": 8192,
    "use_gpu": True,
    "offline": False,  # the model is always local; this only stops tools reaching the web
    "theme": "system",
    "speak_replies": False,
    "always_allow": [],
    "speech_engine": "",  # empty: the best one installed
    "voice_device": "",  # empty: the Windows default
    "listen_on_open": True,
    "voice_autosend": True,
    "doc_folders": [],
    "overlay_opacity": 92,
}
# These can do harm that Undo cannot reach, so a blanket yes is never remembered for them.
NEVER_ALWAYS = {"run_python", "run_command", "send_message", "type_text", "press_keys", "kill_process", "browser_click", "browser_type", "app_click", "app_type"}


class Settings:
    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data = dict(DEFAULTS)
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            self._data.update({key: value for key, value in saved.items() if key in DEFAULTS})
            self._data["always_allow"] = [name for name in self._data["always_allow"] if name not in NEVER_ALWAYS]
        except (OSError, ValueError):
            pass

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for key, value in patch.items():
                if key not in DEFAULTS:
                    raise KeyError(f"Unknown setting: {key}")
                expected = type(DEFAULTS[key])
                if not isinstance(value, expected):
                    raise ValueError(f"{key} must be {expected.__name__}")
                self._data[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            return self.as_dict()


def ensure_dirs() -> None:
    for folder in (HOME, LOG_DIR, UNDO_DIR, WORKSPACE):
        folder.mkdir(parents=True, exist_ok=True)


API_TOKEN = secrets.token_urlsafe(24)
