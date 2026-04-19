"""On-device speech-to-text via Cactus Parakeet (libcactus transcribe)."""

from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from llm import _inject_cactus_path

_logger = logging.getLogger("clause.asr")

_parakeet_handle = None
_parakeet_lock = threading.Lock()
# Serialize ASR calls; libcactus may not be safe for concurrent transcribe on one handle.
_transcribe_lock = threading.Lock()


def parakeet_configured() -> bool:
    raw = os.environ.get("PARAKEET_MODEL_PATH", "").strip()
    if not raw:
        return False
    return Path(os.path.expanduser(raw)).is_dir()


def _destroy_parakeet() -> None:
    global _parakeet_handle
    if _parakeet_handle is None:
        return
    _inject_cactus_path()
    try:
        from cactus import cactus_destroy

        cactus_destroy(_parakeet_handle)
    except Exception as exc:
        _logger.warning("cactus_destroy (parakeet): %s", exc)
    finally:
        _parakeet_handle = None


atexit.register(_destroy_parakeet)


def _get_parakeet_handle():
    global _parakeet_handle
    if not parakeet_configured():
        raise RuntimeError(
            "PARAKEET_MODEL_PATH is not set or does not point to Parakeet weights (directory with config.txt)."
        )
    _inject_cactus_path()
    from cactus import cactus_init

    with _parakeet_lock:
        if _parakeet_handle is None:
            path = os.path.expanduser(os.environ["PARAKEET_MODEL_PATH"].strip())
            try:
                _parakeet_handle = cactus_init(path, None, False)
            except TypeError:
                _parakeet_handle = cactus_init(path)
            if not _parakeet_handle:
                raise RuntimeError("cactus_init returned null for Parakeet.")
    return _parakeet_handle


def _prompt_for_parakeet() -> str:
    """Parakeet TDT uses an empty prompt in Cactus ASR; override if needed."""
    raw = os.environ.get("PARAKEET_TRANSCRIBE_PROMPT")
    if raw is not None:
        return raw
    return ""


def _options_json() -> str | None:
    return json.dumps({"telemetry_enabled": False})


def transcript_from_response(raw_json: str) -> str:
    raw_json = raw_json.strip()
    if not raw_json:
        raise RuntimeError("Empty transcription response.")

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid transcription JSON: {exc}") from exc

    if data.get("success") is False:
        raise RuntimeError(data.get("error") or "Transcription unsuccessful.")

    text = (data.get("response") or "").strip()
    if text:
        return text

    segments = data.get("segments")
    if isinstance(segments, list):
        parts: list[str] = []
        for seg in segments:
            if isinstance(seg, dict):
                chunk = (seg.get("text") or seg.get("transcript") or "").strip()
                if chunk:
                    parts.append(chunk)
            elif isinstance(seg, str) and seg.strip():
                parts.append(seg.strip())
        joined = " ".join(parts).strip()
        if joined:
            return joined

    raise RuntimeError("No text in transcription result.")


def transcribe_audio_file(path: Path) -> str:
    from cactus import cactus_transcribe

    with _transcribe_lock:
        handle = _get_parakeet_handle()
        prompt = _prompt_for_parakeet()
        opts = _options_json()
        raw = cactus_transcribe(handle, str(path), prompt, opts, None, None)
    return transcript_from_response(raw)


def transcribe_uploaded_bytes(data: bytes, filename: str) -> str:
    """Write browser upload to a temp file and run Parakeet; optional ffmpeg → WAV retry."""
    suffix = Path(filename).suffix.lower() or ".webm"
    if not suffix.startswith("."):
        suffix = f".{suffix}"

    fd, tmppath = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    tmp = Path(tmppath)
    wav_path: Path | None = None
    try:
        tmp.write_bytes(data)
        try:
            return transcribe_audio_file(tmp)
        except Exception as first:
            if suffix in (".wav", ".wave"):
                raise
            _logger.warning("Parakeet pass failed (%s); retrying after ffmpeg → WAV.", first)
            wav_path = tmp.with_suffix(".wav")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(tmp), str(wav_path)],
                    check=True,
                    capture_output=True,
                    timeout=180,
                )
            except FileNotFoundError as exc:
                raise first from exc
            except subprocess.CalledProcessError as exc:
                _logger.warning("ffmpeg failed: %s", exc.stderr.decode(errors="ignore")[:300])
                raise first from exc
            return transcribe_audio_file(wav_path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        if wav_path is not None:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass
