"""
Clause — voice-first legal document assistant (MVP).

Run locally:
  export GEMINI_API_KEY=...          # cloud mode
  export ELEVEN_API_KEY=...          # optional ElevenLabs TTS for /api/speak
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

import os
import uuid
from typing import Literal

import httpx

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from llm import DISCLAIMER, GenerateResult, availability, generate_answer
from pdf_extract import extract_pdf_text
from rag import split_paragraphs, top_paragraphs

ROOT = _APP_DIR
STATIC = ROOT / "static"

_SESSIONS: dict[str, dict] = {}

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


ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
DEFAULT_ELEVEN_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"


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
    model_id = os.environ.get("ELEVEN_MODEL_ID", "eleven_multilingual_v2")
    url = f"{ELEVENLABS_TTS_URL}/{voice_id}"

    payload = {
        "text": body.text,
        "model_id": model_id,
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                url,
                headers={
                    "xi-api-key": api_key.strip(),
                    "Accept": "audio/mpeg",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"ElevenLabs request failed: {e}",
        ) from e

    if resp.status_code != 200:
        detail = resp.text[:800] if resp.text else resp.reason_phrase
        raise HTTPException(
            status_code=502,
            detail=f"ElevenLabs error ({resp.status_code}): {detail}",
        )

    return Response(content=resp.content, media_type="audio/mpeg")


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
