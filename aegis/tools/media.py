"""Screen and audio recording, FFmpeg editing, speech-to-text. Results are checked with ffprobe."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from aegis import paths
from aegis.toolkit import WRITE, Approval, Result, ToolError, arg, resolve_input, resolve_output, tool

logger = logging.getLogger("aegis.media")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW


def ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise ToolError("FFmpeg is not installed. Install it and add it to PATH to use media tools.") from exc


def probe(path: Path) -> dict[str, Any]:
    tool_path = shutil.which("ffprobe")
    if not tool_path:
        return {}
    done = subprocess.run([tool_path, "-v", "error", "-show_entries", "format=duration,size,bit_rate:stream=codec_type,codec_name,width,height,r_frame_rate,channels,sample_rate", "-of", "json", str(path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=_NO_WINDOW)
    if done.returncode:
        return {}
    try:
        return json.loads(done.stdout)
    except ValueError:
        return {}


def duration_of(path: Path) -> float:
    try:
        return float(probe(path).get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def _describe_media(path: Path) -> str:
    info = probe(path)
    if not info:
        return f"{path.stat().st_size // 1024} KB"
    seconds = duration_of(path)
    parts = [f"{seconds:.1f} s"]
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video" and stream.get("width"):
            parts.append(f"{stream['width']}x{stream['height']} {stream.get('codec_name', '')}")
        elif stream.get("codec_type") == "audio":
            parts.append(f"{stream.get('codec_name', 'audio')} audio")
    return ", ".join(parts)


def _run_ffmpeg(ctx, argv: list[str], timeout: int = 1800) -> None:
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
    if ctx:
        ctx.procs.append(process)
    started = time.monotonic()
    tail: list[str] = []
    reader = threading.Thread(target=lambda: tail.extend(process.stderr.readlines()[-6:]), daemon=True)
    reader.start()
    while process.poll() is None:
        if ctx and ctx.cancelled.is_set():
            process.kill()
            raise ToolError("Cancelled.")
        if time.monotonic() - started > timeout:
            process.kill()
            raise ToolError("FFmpeg took too long and was stopped.")
        time.sleep(0.1)
    reader.join(timeout=2)
    if process.returncode:
        message = next((line.strip() for line in reversed(tail) if line.strip()), f"exit code {process.returncode}")
        raise ToolError(f"FFmpeg failed: {message[:240]}")


def _finish(source_label: str, target: Path, verb: str = "Saved") -> Result:
    if not target.exists() or target.stat().st_size == 0:
        raise ToolError(f"FFmpeg produced no output for {source_label}.")
    info = _describe_media(target)
    if not probe(target) and shutil.which("ffprobe"):
        raise ToolError(f"{target.name} was written, but it is not a readable media file.")
    return Result(f"{verb} {paths.pretty(target)} ({info}).", files=[target], verified=f"ffprobe reads it: {info}.", undo={"kind": "delete_created", "path": str(target)})


# ---------------------------------------------------------------------- recording
class _Recording:
    def __init__(self, kind: str, target: Path, process: subprocess.Popen, seconds: int | None) -> None:
        self.kind, self.target, self.process, self.seconds = kind, target, process, seconds
        self.started = time.time()


_recording: _Recording | None = None
_rec_lock = threading.Lock()


def recording_status() -> dict[str, Any] | None:
    current = _recording
    if current is None or current.process.poll() is not None:
        return None
    return {"kind": current.kind, "path": str(current.target), "elapsed": time.time() - current.started, "seconds": current.seconds}


def stop_active_recording() -> None:
    current = _recording
    if current and current.process.poll() is None:
        _stop_process(current.process)


def _stop_process(process: subprocess.Popen) -> None:
    try:
        process.stdin.write(b"q")
        process.stdin.flush()
    except (OSError, ValueError):
        pass
    try:
        process.wait(timeout=12)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    _close_pipes(process)


def _close_pipes(process: subprocess.Popen) -> None:
    for pipe in (process.stdin, process.stderr):
        try:
            if pipe:
                pipe.close()
        except OSError:
            pass


def audio_devices() -> list[str]:
    done = subprocess.run([ffmpeg(), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, creationflags=_NO_WINDOW)
    return re.findall(r'"([^"]+)"\s+\(audio\)', done.stderr)


def _gate_recording(ctx, kind: str = "screen", **_: Any) -> Approval | None:
    """Recording is private enough that it must come from the user's words, not a model's guess."""
    if re.search(r"\b(?:record|recording|screencast|film|capture)\b", ctx.request or "", re.IGNORECASE):
        return None
    what = "your microphone" if kind == "audio" else "your screen"
    return Approval(f"Start recording {what}?", "You did not mention recording, so Aegis is checking first.")


def _drain(process: subprocess.Popen) -> None:
    try:
        process.stderr.read()
    except (OSError, ValueError):
        pass


@tool("start_recording", "Record the screen or the microphone. With seconds it stops by itself; otherwise it runs until stop_recording.", risk=WRITE, terminal=True, group="Screen & media", doing="Starting a recording", gate=_gate_recording,
      keywords="record recording capture screen video audio microphone mic voice start begin",
      kind=arg("string", "screen or audio", enum=["screen", "audio"]), seconds=arg("integer", "Stop after this long"),
      with_mic=arg("boolean", "Also capture the microphone in a screen recording"), path=arg("string", "Where to save"))
def start_recording(ctx, kind: str = "screen", seconds: int = 0, with_mic: bool = False, path: str = "") -> Result:
    global _recording
    with _rec_lock:
        if recording_status():
            raise ToolError(f"A {_recording.kind} recording is already running.", "Use stop_recording first.")
        stamp = f"{dt.datetime.now():%Y-%m-%d %H%M%S}"
        exe = ffmpeg()
        limit = ["-t", str(int(seconds))] if seconds else []
        if kind == "audio":
            devices = audio_devices()
            if not devices:
                raise ToolError("No microphone was found.")
            target = resolve_output(path, kind="audio", default_name=f"Audio {stamp}.m4a")
            argv = [exe, "-y", "-hide_banner", "-f", "dshow", "-i", f"audio={devices[0]}", *limit, "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(target)]
        else:
            target = resolve_output(path, kind="recording", default_name=f"Screen {stamp}.mp4")
            inputs = ["-f", "gdigrab", "-framerate", "24", "-i", "desktop"]
            audio: list[str] = []
            if with_mic:
                devices = audio_devices()
                if not devices:
                    raise ToolError("No microphone was found, so the screen recording was not started.", "Try again without the microphone.")
                inputs += ["-f", "dshow", "-i", f"audio={devices[0]}"]
                audio = ["-c:a", "aac", "-b:a", "128k"]
            argv = [exe, "-y", "-hide_banner", *inputs, *limit, "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p", *audio, str(target)]
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
        time.sleep(1.2)
        if process.poll() is not None:
            error = process.stderr.read().decode(errors="replace").strip().splitlines()
            raise ToolError(f"The recording did not start: {error[-1][:200] if error else 'FFmpeg exited'}")
        threading.Thread(target=_drain, args=(process,), daemon=True).start()
        _recording = _Recording(kind, target, process, seconds or None)
    if ctx.bus:
        ctx.bus.publish({"type": "recording", "active": True, "kind": kind, "path": str(target), "seconds": seconds or None})
    # Short timed recordings wait for the finished file, so the result can be proven.
    if seconds and seconds <= 90:
        deadline = time.monotonic() + seconds + 25
        while process.poll() is None and time.monotonic() < deadline:
            if ctx.cancelled.is_set():
                _stop_process(process)
                break
            time.sleep(0.2)
        if process.poll() is None:
            _stop_process(process)
        _close_pipes(process)
        if ctx.bus:
            ctx.bus.publish({"type": "recording", "active": False})
        return _finish("the recording", target, "Recorded")
    if seconds:
        return Result(f"Recording the {kind} for {seconds} s into {paths.pretty(target)}. It will stop by itself.", files=[], verified="FFmpeg is running.")
    return Result(f"Recording the {kind} into {paths.pretty(target)}. Say 'stop recording' when you are done.", verified="FFmpeg is running.")


@tool("stop_recording", "Stop the recording that is in progress.", risk=WRITE, terminal=True, group="Screen & media", doing="Stopping the recording",
      keywords="stop end finish recording record halt done")
def stop_recording(ctx) -> Result:
    global _recording
    with _rec_lock:
        current = _recording
        if current is None:
            raise ToolError("No recording is running.")
        if current.process.poll() is None:
            _stop_process(current.process)
        _recording = None
    if ctx.bus:
        ctx.bus.publish({"type": "recording", "active": False})
    return _finish("the recording", current.target, "Finished recording:")


# ---------------------------------------------------------------------- editing
@tool("media_info", "Get an audio or video file's length, resolution and format.", group="Screen & media", doing="Inspecting media", keywords="video audio media length duration resolution how long codec info format",
      path=arg("string", "Media file", required=True))
def media_info(ctx, path: str) -> Result:
    source = resolve_input(path)
    return Result(f"{source.name}: {_describe_media(source)}. {source.stat().st_size // (1024 * 1024)} MB.")


@tool("convert_media", "Convert audio or video to another format, e.g. mov to mp4, wav to mp3, video to gif.", risk=WRITE, terminal=True, group="Screen & media", doing="Converting media",
      keywords="convert video audio media mp4 mp3 wav mov mkv gif webm format change turn",
      path=arg("string", "Source file", required=True), to=arg("string", "Target format, e.g. mp4, mp3, gif", required=True), destination=arg("string", "Where to save"))
def convert_media(ctx, path: str, to: str, destination: str = "") -> Result:
    source = resolve_input(path)
    fmt = to.strip().lower().lstrip(".")
    target = resolve_output(destination or str(source.with_suffix(f".{fmt}")), default_name=f"{source.stem}.{fmt}", beside=source)
    extra: list[str] = []
    if fmt == "gif":
        extra = ["-vf", "fps=12,scale=720:-1:flags=lanczos"]
    elif fmt == "mp4":
        extra = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
    ctx.progress("Encoding")
    _run_ffmpeg(ctx, [ffmpeg(), "-y", "-hide_banner", "-i", str(source), *extra, str(target)])
    return _finish(source.name, target, "Converted to")


@tool("trim_media", "Cut a section out of an audio or video file and save it as a new file.", risk=WRITE, terminal=True, group="Screen & media", doing="Trimming",
      keywords="trim cut clip shorten crop section segment video audio start end first last seconds",
      path=arg("string", "Source file", required=True), start=arg("string", "Start time, e.g. 0:30 or 90"), end=arg("string", "End time, e.g. 2:15"), destination=arg("string", "Where to save"))
def trim_media(ctx, path: str, start: str = "0", end: str = "", destination: str = "") -> Result:
    source = resolve_input(path)
    target = resolve_output(destination or str(source.with_name(f"{source.stem} (trimmed){source.suffix}")), default_name=source.name, beside=source)
    argv = [ffmpeg(), "-y", "-hide_banner", "-ss", _time(start), "-i", str(source)]
    if end:
        length = _seconds(end) - _seconds(start)
        if length <= 0:
            raise ToolError("The end time must be after the start time.")
        argv += ["-t", str(length)]
    ctx.progress("Trimming")
    _run_ffmpeg(ctx, argv + ["-c", "copy", "-avoid_negative_ts", "make_zero", str(target)])
    return _finish(source.name, target, "Trimmed to")


def _seconds(value: str) -> float:
    parts = str(value).strip().split(":")
    try:
        total = 0.0
        for part in parts:
            total = total * 60 + float(part)
        return total
    except ValueError as exc:
        raise ToolError(f"'{value}' is not a time. Use seconds or mm:ss.") from exc


def _time(value: str) -> str:
    return str(_seconds(value))


@tool("merge_media", "Join two or more video or audio files, one after another, into one file.", risk=WRITE, terminal=True, group="Screen & media", doing="Merging",
      keywords="merge join combine concatenate stitch videos clips audio together",
      inputs=arg("array", "Files in order", required=True), destination=arg("string", "Where to save"))
def merge_media(ctx, inputs: list[str], destination: str = "") -> Result:
    sources = [resolve_input(item) for item in inputs]
    if len(sources) < 2:
        raise ToolError("Merging needs at least two files.", "Use find_files to locate them.")
    infos = [probe(source) for source in sources]
    has_video = all(any(stream.get("codec_type") == "video" for stream in info.get("streams", [])) for info in infos)
    has_audio = all(any(stream.get("codec_type") == "audio" for stream in info.get("streams", [])) for info in infos)
    suffix = ".mp4" if has_video else ".m4a"
    target = resolve_output(destination or str(sources[0].with_name(f"{sources[0].stem} (merged){suffix}")), default_name=f"Merged{suffix}")
    argv = [ffmpeg(), "-y", "-hide_banner"]
    for source in sources:
        argv += ["-i", str(source)]
    filters: list[str] = []
    labels = ""
    if has_video:
        first = next(stream for stream in infos[0]["streams"] if stream.get("codec_type") == "video")
        width, height = int(first.get("width", 1280)), int(first.get("height", 720))
        width, height = width + width % 2, height + height % 2
        for index in range(len(sources)):
            filters.append(f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{index}]")
            labels += f"[v{index}]"
            if has_audio:
                filters.append(f"[{index}:a]aresample=48000[a{index}]")
                labels += f"[a{index}]"
    else:
        for index in range(len(sources)):
            filters.append(f"[{index}:a]aresample=48000[a{index}]")
            labels += f"[a{index}]"
    count = f"n={len(sources)}:v={1 if has_video else 0}:a={1 if has_audio else 0}"
    outputs = ("[v]" if has_video else "") + ("[a]" if has_audio else "")
    filters.append(f"{labels}concat={count}{outputs}")
    argv += ["-filter_complex", ";".join(filters)]
    if has_video:
        argv += ["-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
    if has_audio:
        argv += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    ctx.progress("Encoding")
    _run_ffmpeg(ctx, argv + [str(target)])
    result = _finish("the merge", target, "Merged into")
    expected = sum(duration_of(source) for source in sources)
    actual = duration_of(target)
    if expected and abs(actual - expected) > max(2.0, expected * 0.05):
        raise ToolError(f"The merged file is {actual:.1f} s but the inputs add up to {expected:.1f} s.")
    return result


@tool("extract_audio", "Save the audio track from a video file.", risk=WRITE, terminal=True, group="Screen & media", doing="Extracting audio",
      keywords="extract audio sound from video mp3 rip track",
      path=arg("string", "Video file", required=True), to=arg("string", "Format: mp3, wav, m4a"), destination=arg("string", "Where to save"))
def extract_audio(ctx, path: str, to: str = "mp3", destination: str = "") -> Result:
    source = resolve_input(path)
    fmt = to.strip().lower().lstrip(".")
    target = resolve_output(destination or str(source.with_suffix(f".{fmt}")), default_name=f"{source.stem}.{fmt}", beside=source)
    ctx.progress("Extracting")
    _run_ffmpeg(ctx, [ffmpeg(), "-y", "-hide_banner", "-i", str(source), "-vn", str(target)])
    return _finish(source.name, target, "Extracted audio to")


@tool("remove_silence", "Remove the silent parts from an audio or video recording, like a lecture.", risk=WRITE, terminal=True, group="Screen & media", doing="Removing silence",
      keywords="silence silent quiet pauses remove trim lecture recording dead air",
      path=arg("string", "Audio or video file", required=True), destination=arg("string", "Where to save"))
def remove_silence(ctx, path: str, destination: str = "") -> Result:
    source = resolve_input(path)
    info = probe(source)
    has_video = any(stream.get("codec_type") == "video" for stream in info.get("streams", []))
    target = resolve_output(destination or str(source.with_name(f"{source.stem} (no silence){source.suffix}")), default_name=source.name, beside=source)
    ctx.progress("Finding silent parts")
    detect = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(source), "-af", "silencedetect=noise=-35dB:d=0.6", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800, creationflags=_NO_WINDOW)
    starts = [float(value) for value in re.findall(r"silence_start: ([\d.]+)", detect.stderr)]
    ends = [float(value) for value in re.findall(r"silence_end: ([\d.]+)", detect.stderr)]
    total = duration_of(source)
    if not starts:
        raise ToolError(f"{source.name} has no silent parts longer than 0.6 s.", "Tell the user nothing needed removing.")
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in zip(starts, ends + [total]):
        if start > cursor + 0.05:
            keep.append((cursor, start))
        cursor = end
    if total > cursor + 0.05:
        keep.append((cursor, total))
    if not keep:
        raise ToolError(f"{source.name} is entirely silent.")
    expression = "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in keep)
    ctx.progress(f"Keeping {len(keep)} sections")
    argv = [ffmpeg(), "-y", "-hide_banner", "-i", str(source), "-af", f"aselect='{expression}',asetpts=N/SR/TB"]
    if has_video:
        argv += ["-vf", f"select='{expression}',setpts=N/FRAME_RATE/TB", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac"]
    _run_ffmpeg(ctx, argv + [str(target)])
    result = _finish(source.name, target, "Removed silence:")
    shorter = duration_of(target)
    result.summary += f" It went from {total:.0f} s to {shorter:.0f} s."
    return result


# ---------------------------------------------------------------------- speech to text
@tool("transcribe_audio", "Turn speech in an audio or video file into text, and save it as a .txt next to the file.", risk=WRITE, group="Screen & media", doing="Transcribing",
      keywords="transcribe transcript speech text dictation lecture meeting recording subtitles words said audio video",
      path=arg("string", "Audio or video file", required=True))
def transcribe_audio(ctx, path: str) -> Result:
    source = resolve_input(path)
    from faster_whisper.audio import decode_audio

    from aegis import speech

    engine = speech.resolve_engine(ctx.settings["speech_engine"] if ctx.settings else "")
    if engine is None:
        raise ToolError("There is no speech model on this PC yet.", "Tell the user to download one in Settings, under Voice.")
    ctx.progress("Reading the audio")
    audio = decode_audio(str(source), sampling_rate=speech.SAMPLE_RATE)
    duration = len(audio) / speech.SAMPLE_RATE
    ctx.progress("Loading the speech model")
    text = speech.transcribe_long(engine, audio, progress=lambda done: ctx.progress(f"Transcribed {done * 100:.0f}%"), cancelled=ctx.check_cancelled)
    if not text:
        raise ToolError(f"No speech was detected in {source.name}.")
    target = resolve_output(str(source.with_name(f"{source.stem} (transcript).txt")), default_name="transcript.txt")
    target.write_text(text, encoding="utf-8")
    return Result(f"Transcribed {source.name} ({duration:.0f} s) and saved {paths.pretty(target)}.", text[:6000], files=[target], verified=f"{len(text.split())} words written. Automatic transcription can contain mistakes.", undo={"kind": "delete_created", "path": str(target)})
