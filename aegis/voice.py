"""Local speech: microphone capture with voice detection, live transcription, Windows text-to-speech."""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from typing import Any

import numpy as np

from aegis import speech
from aegis.speech import SAMPLE_RATE, Transcript

logger = logging.getLogger("aegis.voice")
MAX_SECONDS = 45
SILENCE_TO_STOP = 1.5
SILENCE_MID_SENTENCE = 3.5
# A sentence ending on one of these isn't finished, so a pause is someone thinking.
_UNFINISHED = re.compile(r"\b(?:and|or|but|so|then|to|the|a|an|of|for|from|with|into|about|my|your|this|these|those|if|when|because|is|are|please|open|close|save|find|make|create|put|send|show|tell|remind|search|called|named)[.,?!…\s]*$", re.IGNORECASE)
NO_SPEECH_TIMEOUT = 8.0
DETECTOR_WINDOW = 1.0
PARTIAL_EVERY = 0.7
PARTIAL_MAX_SECONDS = 30


class Voice:
    def __init__(self, bus: Any, settings: Any) -> None:
        self.bus, self.settings = bus, settings
        self.state = "idle"
        self.owner: str | None = None
        self._stream = None
        self._frames: list[np.ndarray] = []
        self._rate = SAMPLE_RATE
        self._lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._session = 0
        self._noise_floor = 0.004
        self._speech_started = False
        self._last_speech = 0.0
        self._started_at = 0.0
        self._last_level = 0.0
        self._peak = 0.0
        self._cancelled = False
        self._detector: Any = None
        self._detector_tried = False
        self._partial_busy = False
        self._heard = ""
        self._speech_queue: queue.Queue[str | None] = queue.Queue()
        self._speaker: threading.Thread | None = None
        self._sapi = None

    def _engine(self) -> str | None:
        return speech.resolve_engine(self.settings["speech_engine"])

    def available(self) -> dict[str, Any]:
        try:
            with self._lock:
                return {"microphone": True, "device": self.devices()["default"]}
        except Exception as exc:
            return {"microphone": False, "error": str(exc)[:200]}

    def devices(self) -> dict[str, Any]:
        """Input devices by name. WASAPI gives each one's real name and native format."""
        import sounddevice as sd

        with self._lock:
            wasapi = self._wasapi(sd)
            if wasapi is None:
                default = sd.query_devices(kind="input")
                return {"default": default["name"], "inputs": []}
            listed = [sd.query_devices(index) for index in wasapi["devices"]]
            inputs = [device["name"] for device in listed if device["max_input_channels"] > 0]
            index = wasapi["default_input_device"]
            return {"default": sd.query_devices(index)["name"] if index >= 0 else None, "inputs": inputs}

    @staticmethod
    def _wasapi(sd: Any) -> dict[str, Any] | None:
        return next((api for api in sd.query_hostapis() if "WASAPI" in api["name"]), None)

    def prepare(self) -> None:
        """Loads the speech detector ahead of time, so opening the microphone is instant."""
        with self._lock:
            if self._detector is None and not self._detector_tried:
                self._detector_tried = True
                self._detector = self._load_detector()

    def preload(self) -> None:
        engine = self._engine()
        if engine is None:
            return
        try:
            speech.warm(engine)
        except Exception as exc:
            logger.warning("speech model did not load: %s", exc)

    def _set_state(self, state: str) -> None:
        self.state = state
        self.bus.publish({"type": "voice_state", "state": state, "owner": self.owner})

    def _capture_choices(self, sd: Any) -> list[tuple[Any, int, int]]:
        """(device, rate, channels) to try, best first: the device's own format, then Windows's converted one."""
        choices: list[tuple[Any, int, int]] = []
        wasapi = self._wasapi(sd)
        wanted = self.settings["voice_device"]
        if wasapi is not None:
            index = wasapi["default_input_device"]
            for candidate in wasapi["devices"]:
                device = sd.query_devices(candidate)
                if wanted and device["name"] == wanted and device["max_input_channels"] > 0:
                    index = candidate
            if index >= 0:
                device = sd.query_devices(index)
                choices.append((index, int(device["default_samplerate"]), min(2, device["max_input_channels"])))
        choices.append((None, SAMPLE_RATE, 1))
        return choices

    def _open_stream(self) -> None:
        import sounddevice as sd

        # PortAudio caches the device list, so a headset connected since launch needs a refresh.
        try:
            sd._terminate()
            sd._initialize()
        except Exception as exc:
            logger.info("could not refresh audio devices: %s", exc)
        failure: Exception | None = None
        for device, rate, channels in self._capture_choices(sd):
            try:
                stream = sd.InputStream(device=device, samplerate=rate, channels=channels, dtype="float32", blocksize=rate // 10, callback=self._on_audio)
                stream.start()
                self._stream, self._rate = stream, rate
                return
            except Exception as exc:
                logger.info("could not capture from %s at %s Hz: %s", device, rate, exc)
                failure = exc
        raise RuntimeError(str(failure))

    def start(self, owner: str = "main") -> None:
        with self._lock:
            if self.state != "idle":
                # A second window asking takes over the same dictation; both must not act on it.
                self.owner = owner
                self._set_state(self.state)
                return
            if self._engine() is None:
                raise RuntimeError("There is no speech model on this PC yet. Download one in Settings, under Voice.")
            self.stop_speaking()
            self.owner = owner
            self._frames = []
            self._stop_requested.clear()
            self._cancelled = False
            self._speech_started = False
            self._heard = ""
            self._peak = 0.0
            self._session += 1
            self.prepare()
            self._started_at = time.monotonic()
            self._last_speech = self._started_at
            self._open_stream()
            self._set_state("listening")
            session = self._session
        threading.Thread(target=self._watch, args=(session,), daemon=True, name="aegis-voice").start()
        threading.Thread(target=self.preload, daemon=True, name="aegis-voice-load").start()

    def stop(self) -> None:
        self._stop_requested.set()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            self._stop_requested.set()
            if self.state == "transcribing":
                self._session += 1  # the finished transcription now has nowhere to go
                self._set_state("idle")

    def cancel_for(self, owner: str) -> None:
        if self.owner == owner and self.state != "idle":
            self.cancel()

    def _on_audio(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        chunk = indata.copy()
        self._frames.append(chunk)
        level = float(np.sqrt(np.mean(chunk ** 2)))
        self._peak = max(self._peak, float(np.abs(chunk).max(initial=0.0)))
        now = time.monotonic()
        if self._detector is None:
            # No speech detector: fall back to loudness, adapting to the room while nobody talks.
            if not self._speech_started and now - self._started_at < 0.5:
                self._noise_floor = max(self._noise_floor * 0.9 + level * 0.1, 0.002)
            if level > max(self._noise_floor * 2.8, 0.012):
                if now - self._started_at > 0.15:
                    self._speech_started = True
                self._last_speech = now
        if now - self._last_level > 0.08:
            self._last_level = now
            self.bus.publish({"type": "voice_level", "level": min(1.0, level * 14)})

    def _load_detector(self) -> Any:
        """Silero recognises voices rather than noise, so music or a fan does not keep the mic open. Returns seconds of trailing silence, or None."""
        try:
            from faster_whisper.vad import VadOptions, get_speech_timestamps

            options = VadOptions(threshold=0.45, min_speech_duration_ms=120, min_silence_duration_ms=120, speech_pad_ms=0)
            get_speech_timestamps(np.zeros(SAMPLE_RATE, dtype=np.float32), options)

            def since_speech(audio: np.ndarray) -> float | None:
                spans = get_speech_timestamps(audio, options)
                return (len(audio) - spans[-1]["end"]) / SAMPLE_RATE if spans else None

            return since_speech
        except Exception as exc:
            logger.info("speech detector unavailable, using loudness: %s", exc)
            return None

    def _audio_16k(self, frames: list[np.ndarray], rate: int) -> np.ndarray:
        """Mono at 16 kHz, whatever the device delivered."""
        audio = _to_mono(np.concatenate(frames))
        if rate != SAMPLE_RATE:
            from math import gcd

            from scipy.signal import resample_poly

            divisor = gcd(SAMPLE_RATE, rate)
            audio = resample_poly(audio, SAMPLE_RATE // divisor, rate // divisor).astype(np.float32)
        return audio

    def _partial(self, session: int, frames: list[np.ndarray], rate: int) -> None:
        try:
            engine = self._engine()
            if engine is None:
                return
            audio = prepare_for_recognition(self._audio_16k(frames, rate))
            text = speech.transcribe(engine, audio, quick=True).text
            if text and session == self._session and self.state == "listening" and not self._stop_requested.is_set():
                self._heard = text
                self.bus.publish({"type": "voice_partial", "owner": self.owner, "text": text})
        except Exception as exc:
            logger.info("live transcription skipped: %s", exc)
        finally:
            self._partial_busy = False

    def _watch(self, session: int) -> None:
        last_check = 0.0
        last_partial = 0.0
        rate = self._rate
        blocks_per_second = 10
        while not self._stop_requested.is_set():
            now = time.monotonic()
            if self._detector is not None and now - last_check >= 0.2 and self._frames:
                last_check = now
                recent = self._frames[-int(DETECTOR_WINDOW * blocks_per_second):]
                silence = self._detector(self._audio_16k(recent, rate))
                if silence is not None:
                    self._speech_started = True
                    self._last_speech = now - silence
            if self._speech_started and now - last_partial >= PARTIAL_EVERY and not self._partial_busy:
                last_partial = now
                self._partial_busy = True
                frames = list(self._frames)[-PARTIAL_MAX_SECONDS * blocks_per_second:]
                threading.Thread(target=self._partial, args=(session, frames, rate), daemon=True, name="aegis-voice-partial").start()
            # Until the model has heard something, there is no telling whether the sentence was finished.
            undecided = not self._heard and self._partial_busy
            if self._speech_started and not undecided and now - self._last_speech > (SILENCE_MID_SENTENCE if _UNFINISHED.search(self._heard) else SILENCE_TO_STOP):
                break
            if not self._speech_started and now - self._started_at > NO_SPEECH_TIMEOUT:
                break
            if now - self._started_at > MAX_SECONDS:
                break
            time.sleep(0.05)
        with self._lock:
            stream, self._stream = self._stream, None
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    logger.exception("could not close the microphone")
            frames, self._frames = self._frames, []
            owner = self.owner
            if self._cancelled:
                if session == self._session:
                    self._set_state("idle")
                return
            if not frames or not self._speech_started:
                self._set_state("idle")
                silent = self._peak < 1e-5
                message = "The microphone is only sending silence. Check it isn't muted, and that Windows lets desktop apps use it (Settings > Privacy & security > Microphone)." if silent else "I didn't hear anything. Press Space to try again."
                self.bus.publish({"type": "voice_result", "owner": owner, "text": "", "message": message})
                return
            self._set_state("transcribing")
        try:
            result = self.transcribe(self._audio_16k(frames, rate))
            if session == self._session and not self._cancelled:
                self.bus.publish({"type": "voice_result", "owner": owner, "text": result.text, "confident": result.confident, "message": "" if result.text else "I couldn't make that out."})
        except Exception as exc:
            logger.exception("transcription failed")
            if session == self._session:
                self.bus.publish({"type": "voice_result", "owner": owner, "text": "", "message": f"Speech recognition failed: {str(exc)[:160]}"})
        finally:
            with self._lock:
                if session == self._session and self.state == "transcribing":
                    self._set_state("idle")

    def transcribe(self, audio: np.ndarray) -> Transcript:
        """Only a confident result is acted on without the user reading it first."""
        engine = self._engine()
        if engine is None:
            raise RuntimeError("There is no speech model on this PC yet.")
        return speech.transcribe(engine, prepare_for_recognition(audio))

    # ------------------------------------------------------------------ text to speech
    def speak(self, text: str) -> None:
        clean = re.sub(r"```.*?```", " code block ", text, flags=re.DOTALL)
        clean = re.sub(r"[*_`#>|]", "", clean)
        clean = re.sub(r"(?:[A-Za-z]:)?[\\/][^\s]+", "that location", clean)
        clean = re.sub(r"\s+", " ", clean).strip()[:900]
        if not clean:
            return
        if self._speaker is None or not self._speaker.is_alive():
            self._speaker = threading.Thread(target=self._speak_loop, daemon=True, name="aegis-tts")
            self._speaker.start()
        self._speech_queue.put(clean)

    def _speak_loop(self) -> None:
        try:
            import pythoncom
            from win32com import client

            pythoncom.CoInitialize()
            self._sapi = client.Dispatch("SAPI.SpVoice")
        except Exception as exc:
            logger.warning("text-to-speech unavailable: %s", exc)
            return
        while True:
            text = self._speech_queue.get()
            if text is None:
                return
            try:
                self._sapi.Speak(text, 1)  # asynchronous, so it can be interrupted
                while not self._sapi.WaitUntilDone(100):
                    if not self._speech_queue.empty():
                        self._sapi.Speak("", 3)  # purge
                        break
            except Exception as exc:
                logger.info("speech failed: %s", exc)

    def stop_speaking(self) -> None:
        try:
            while not self._speech_queue.empty():
                self._speech_queue.get_nowait()
        except queue.Empty:
            pass
        if self._speaker is not None and self._speaker.is_alive():
            self._speech_queue.put("")


def _to_mono(audio: np.ndarray) -> np.ndarray:
    """Averages stereo, unless the two channels cancel out, as some laptop microphone arrays do."""
    if audio.ndim == 1 or audio.shape[1] == 1:
        return audio.reshape(-1)
    mixed = audio.mean(axis=1)
    loudest = int(np.argmax(np.sqrt(np.mean(audio ** 2, axis=0))))
    channel = audio[:, loudest]
    return mixed if np.sqrt(np.mean(mixed ** 2)) >= 0.6 * np.sqrt(np.mean(channel ** 2)) else channel


def prepare_for_recognition(audio: np.ndarray) -> np.ndarray:
    """Removes rumble and brings quiet microphones up to a level recognizers are trained on."""
    from scipy.signal import butter, sosfilt

    if not len(audio):
        return audio
    filtered = sosfilt(butter(4, 70, "highpass", fs=SAMPLE_RATE, output="sos"), audio - np.mean(audio)).astype(np.float32)
    peak = float(np.abs(filtered).max())
    if peak < 1e-5:
        return filtered
    return np.clip(filtered * min(0.8 / peak, 30.0), -1.0, 1.0).astype(np.float32)
