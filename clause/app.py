"""
Clause — voice-first legal document assistant (MVP).

Run locally:
  export GEMINI_API_KEY=...          # cloud mode
  export ELEVEN_API_KEY=...          # optional ElevenLabs TTS for /api/speak (official SDK first)
  export CACTUS_MODEL_PATH=...       # optional; private / on-device mode (Cactus + Gemma)

  uvicorn app:app --reload --host 127.0.0.1 --port 8765

Open http://127.0.0.1:8765
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# Load `.env` from the project directory (same folder as this file) before any code reads os.environ.
# `override=True` so project `.env` wins over stale shell exports (e.g. old GEMINI_MODEL).
_APP_DIR = Path(__file__).resolve().parent
load_dotenv(_APP_DIR / ".env", override=True)

import json
import logging
import os
import uuid
from typing import Literal

import httpx

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from llm import DISCLAIMER, GenerateResult, availability, generate_answer
from pdf_extract import extract_pdf_text
from rag import split_paragraphs, top_paragraphs

ROOT = _APP_DIR
STATIC = ROOT / "static"

_SESSIONS: dict[str, dict] = {}

_logger = logging.getLogger("clause.tts")

app = FastAPI(title="Clause", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CLAUSE_CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskBody(BaseModel):
    session_id: str = Field(..., min_length=8)
    question: str = Field(..., min_length=3, max_length=4000)
    mode: Literal["cloud", "private"]


class SpeakBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=500_000)


DEFAULT_ELEVEN_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
# Turbo v2.5 typically sounds more natural than multilingual_v2 for English conversational TTS.
DEFAULT_ELEVEN_MODEL_ID = "eleven_turbo_v2_5"

# Browser-like UA helps avoid shared-network / bot-detection blocks on some ElevenLabs edges.
DEFAULT_ELEVEN_HTTP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15 Clause/1.0"
)


def _elevenlabs_base_urls() -> list[str]:
    raw = os.environ.get("ELEVEN_API_BASE", "").strip()
    bases = []
    if raw:
        bases.append(raw.rstrip("/"))
    bases.extend(
        [
            "https://api.elevenlabs.io",
            "https://api.us.elevenlabs.io",
        ]
    )
    seen: set[str] = set()
    out: list[str] = []
    for b in bases:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def _elevenlabs_headers(*, user_agent: str, api_key_header: str | None) -> dict[str, str]:
    h = {
        "User-Agent": user_agent,
        "Accept": "audio/mpeg, audio/*;q=0.9, */*;q=0.8",
        "Content-Type": "application/json",
    }
    if api_key_header:
        h["xi-api-key"] = api_key_header.strip()
    return h


async def _elevenlabs_fetch_audio(
    *,
    api_key: str,
    voice_id: str,
    payload: dict,
) -> tuple[bytes, str]:
    """
    Try several ElevenLabs request shapes (streaming vs non-stream, header vs query auth,
    alternate regional hosts). Returns (mp3_bytes, attempt_label).
    """
    ua = os.environ.get("ELEVEN_HTTP_USER_AGENT", DEFAULT_ELEVEN_HTTP_UA).strip() or DEFAULT_ELEVEN_HTTP_UA
    output_fmt = os.environ.get("ELEVEN_OUTPUT_FORMAT", "mp3_44100_128").strip() or "mp3_44100_128"

    last_status: int | None = None
    last_snippet = ""

    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        for base in _elevenlabs_base_urls():
            for use_stream in (True, False):
                path = f"{base}/v1/text-to-speech/{voice_id}"
                if use_stream:
                    path += "/stream"

                params_header: dict[str, str] = {}
                if use_stream:
                    params_header["output_format"] = output_fmt

                # (1) Streaming/non-stream + xi-api-key header + realistic User-Agent
                label_a = f"{base} {'stream' if use_stream else 'generate'} header-auth"
                try:
                    r = await client.post(
                        path,
                        headers=_elevenlabs_headers(user_agent=ua, api_key_header=api_key),
                        params=params_header or None,
                        json=payload,
                    )
                except httpx.RequestError as e:
                    last_snippet = str(e)[:400]
                    continue

                last_status = r.status_code
                if r.status_code == 200 and r.content:
                    return r.content, label_a

                if r.text:
                    last_snippet = r.text[:500]

                # (2) Same URL but pass xi-api-key as query param (some networks strip headers)
                params_query = {**params_header, "xi-api-key": api_key.strip()}
                label_b = f"{base} {'stream' if use_stream else 'generate'} query-auth"
                try:
                    r2 = await client.post(
                        path,
                        headers=_elevenlabs_headers(user_agent=ua, api_key_header=None),
                        params=params_query,
                        json=payload,
                    )
                except httpx.RequestError as e:
                    last_snippet = str(e)[:400]
                    continue

                last_status = r2.status_code
                if r2.status_code == 200 and r2.content:
                    return r2.content, label_b

                if r2.text:
                    last_snippet = r2.text[:500]

    detail = last_snippet or "unknown error"
    raise HTTPException(
        status_code=502,
        detail=f"ElevenLabs error after retries (last HTTP {last_status}): {detail}",
    )


def _elevenlabs_tts_payload(text: str) -> dict:
    """Body for ElevenLabs text-to-speech — tuned for conversational, less robotic delivery."""
    model_id = os.environ.get("ELEVEN_MODEL_ID", DEFAULT_ELEVEN_MODEL_ID).strip() or DEFAULT_ELEVEN_MODEL_ID
    raw_settings = os.environ.get("ELEVEN_VOICE_SETTINGS_JSON", "").strip()
    if raw_settings:
        try:
            voice_settings = json.loads(raw_settings)
        except json.JSONDecodeError:
            voice_settings = None
    else:
        voice_settings = None

    if voice_settings is None:
        # Lower stability → more expressive; speaker boost + similarity → less "flat robot".
        voice_settings = {
            "stability": float(os.environ.get("ELEVEN_STABILITY", "0.42")),
            "similarity_boost": float(os.environ.get("ELEVEN_SIMILARITY", "0.85")),
            "style": float(os.environ.get("ELEVEN_STYLE", "0.22")),
            "use_speaker_boost": os.environ.get("ELEVEN_SPEAKER_BOOST", "true").lower()
            in ("1", "true", "yes"),
            "speed": float(os.environ.get("ELEVEN_SPEED", "0.94")),
        }

    return {
        "text": text,
        "model_id": model_id,
        "voice_settings": voice_settings,
    }


def _elevenlabs_sdk_convert_bytes(
    api_key: str,
    voice_id: str,
    payload: dict,
    output_format: str,
) -> bytes:
    """
    Official `elevenlabs` Python SDK — equivalent to JS:
      `elevenlabs.textToSpeech.convert(voiceId, { text, modelId, outputFormat, ... })`
    Uses ELEVEN_API_KEY from env (passed in); never expose key to the browser.
    """
    from elevenlabs import ElevenLabs, VoiceSettings

    vs_raw = payload.get("voice_settings") or {}
    voice_settings = VoiceSettings(
        stability=float(vs_raw["stability"]),
        similarity_boost=float(vs_raw["similarity_boost"]),
        style=float(vs_raw["style"]),
        use_speaker_boost=bool(vs_raw["use_speaker_boost"]),
        speed=float(vs_raw["speed"]),
    )

    client = ElevenLabs(api_key=api_key.strip())
    chunks = client.text_to_speech.convert(
        voice_id=voice_id,
        text=payload["text"],
        model_id=payload["model_id"],
        output_format=output_format,  # type: ignore[arg-type]
        voice_settings=voice_settings,
    )
    return b"".join(chunks)


def _system_prompt() -> str:
    return (
        "You are Clause — a warm, clear legal-document companion (voice chat). "
        "You sound like a smart friend who read their lease carefully—supportive, never preachy. "
        "Use ONLY the DOCUMENT EXCERPTS below, plus the conversation so far for context on follow-ups. "
        "If excerpts don't cover something, say what's missing and what to ask for—never invent clauses. "
        "Prefer short numbered steps when they need to handle a tricky conversation (factual, civil, documented). "
        "Plain English; define jargon in a few words if you must use it. "
        f"Always mention: {DISCLAIMER}"
    )


def _format_chat_history(prior_messages: list[dict]) -> str:
    if not prior_messages:
        return "(First message in this chat.)"
    lines: list[str] = []
    for m in prior_messages:
        who = "User" if m["role"] == "user" else "Clause"
        lines.append(f"{who}: {m['content']}")
    return "\n".join(lines)


def _retrieval_query(prior_messages: list[dict], question: str) -> str:
    """Blend latest question with recent turns so follow-ups still retrieve relevant clauses."""
    if not prior_messages:
        return question
    parts = [question]
    for m in prior_messages[-4:]:
        parts.append(m["content"][:420])
    return "\n".join(parts)


def _user_prompt(question: str, excerpts: list[str], prior_messages: list[dict]) -> str:
    joined = "\n\n---\n\n".join(excerpts)
    hist = _format_chat_history(prior_messages)
    return (
        f"CONVERSATION SO FAR:\n{hist}\n\n"
        f"DOCUMENT EXCERPTS (most relevant paragraphs):\n\n{joined}\n\n"
        f'WHAT THEY JUST SAID (voice):\n"{question}"\n\n'
        "Reply conversationally for spoken playback—about 120–260 words unless they need steps. "
        "Acknowledge follow-ups naturally; stay consistent with earlier answers."
    )


@app.get("/api/health")
def health():
    caps = availability()
    return {
        "ok": True,
        "modes": caps,
        "elevenlabs": bool(os.environ.get("ELEVEN_API_KEY")),
        "disclaimer": DISCLAIMER,
    }


@app.post("/api/speak")
async def speak(body: SpeakBody):
    """Text-to-speech via ElevenLabs (Rachel by default). Returns MP3 audio."""
    api_key = os.environ.get("ELEVEN_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="ELEVEN_API_KEY is not set on the server.",
        )

    voice_id = os.environ.get("ELEVEN_VOICE_ID", DEFAULT_ELEVEN_VOICE_ID)

    payload = _elevenlabs_tts_payload(body.text)
    output_fmt = os.environ.get("ELEVEN_OUTPUT_FORMAT", "mp3_44100_128").strip() or "mp3_44100_128"

    use_sdk = os.environ.get("ELEVEN_USE_OFFICIAL_SDK", "true").lower() in ("1", "true", "yes")
    if use_sdk:
        try:
            audio = await run_in_threadpool(
                _elevenlabs_sdk_convert_bytes,
                api_key,
                voice_id,
                payload,
                output_fmt,
            )
            return Response(content=audio, media_type="audio/mpeg")
        except Exception as exc:
            _logger.warning(
                "ElevenLabs Python SDK convert failed (%s); falling back to REST retries.",
                exc,
            )

    audio, _attempt = await _elevenlabs_fetch_audio(api_key=api_key, voice_id=voice_id, payload=payload)
    return Response(content=audio, media_type="audio/mpeg")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF document.")

    data = await file.read()
    if len(data) > int(os.environ.get("CLAUSE_MAX_UPLOAD_MB", "25")) * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large.")

    try:
        text, pages = extract_pdf_text(data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read PDF: {e}") from e

    if len(text.strip()) < 80:
        raise HTTPException(
            status_code=400,
            detail="Very little text could be extracted. Try a text-based PDF or OCR export.",
        )

    sid = uuid.uuid4().hex
    paras = split_paragraphs(text)
    _SESSIONS[sid] = {
        "filename": file.filename,
        "pages": pages,
        "chars": len(text),
        "full_text": text,
        "paragraphs": paras,
        "messages": [],
    }
    preview = text[:900] + ("…" if len(text) > 900 else "")
    return {
        "session_id": sid,
        "filename": file.filename,
        "pages": pages,
        "chars": len(text),
        "paragraph_count": len(paras),
        "preview": preview,
    }


@app.post("/api/ask")
def ask(body: AskBody):
    sess = _SESSIONS.get(body.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found. Upload your PDF again.")

    sess.setdefault("messages", [])
    prior = list(sess["messages"])

    rq = _retrieval_query(prior, body.question)
    excerpts = top_paragraphs(rq, sess["paragraphs"])
    if not excerpts:
        raise HTTPException(status_code=400, detail="No retrievable text in session.")

    sess["messages"].append({"role": "user", "content": body.question})

    mode = body.mode
    try:
        result: GenerateResult = generate_answer(
            mode=mode,
            system_prompt=_system_prompt(),
            user_prompt=_user_prompt(body.question, excerpts, prior),
        )
    except RuntimeError as e:
        sess["messages"].pop()
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        sess["messages"].pop()
        raise HTTPException(status_code=500, detail=f"Model error: {e}") from e

    sess["messages"].append({"role": "assistant", "content": result.answer})

    return {
        "answer": result.answer,
        "model": result.model_label,
        "source": result.source,
        "excerpts_used": len(excerpts),
        "disclaimer": DISCLAIMER,
    }


@app.get("/")
def index():
    index_path = STATIC / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=404, detail="Frontend not found.")
    return FileResponse(index_path)


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="assets")
