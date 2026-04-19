"""Cloud (Gemini) and optional on-device (Cactus + Gemma) generation."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Mode = Literal["cloud", "private"]


DISCLAIMER = (
    "This is educational information only, not legal advice. Laws vary by jurisdiction; "
    "consult a qualified attorney for your situation."
)

# Default model id for generateContent (no "models/" prefix). From google.genai client.models.list()
# as of 2026-04: latest general Gemini 3 Flash preview; override with GEMINI_MODEL.
DEFAULT_GEMINI_MODEL = "gemini-3-flash-preview"

# Retired or regional preview IDs that still appear in old .env files — map to a working id.
_GEMINI_LEGACY_MODEL_IDS: dict[str, str] = {
    "gemini-2.5-flash-preview-04-17": DEFAULT_GEMINI_MODEL,
    "gemini-2.0-flash-exp": DEFAULT_GEMINI_MODEL,
}


def _normalize_gemini_model_id(raw: str) -> str:
    s = str(raw).strip()
    if s.startswith("models/"):
        s = s[len("models/") :]
    return _GEMINI_LEGACY_MODEL_IDS.get(s, s)


def _resolve_gemini_model(explicit: str | None = None) -> str:
    """Prefer explicit arg, then GEMINI_MODEL (trimmed); ignore empty strings."""
    for candidate in (explicit, os.environ.get("GEMINI_MODEL")):
        if candidate is None:
            continue
        trimmed = str(candidate).strip()
        if trimmed:
            return _normalize_gemini_model_id(trimmed)
    return DEFAULT_GEMINI_MODEL


@dataclass
class GenerateResult:
    answer: str
    model_label: str
    source: Literal["gemini", "cactus", "error"]


def _cloud_generate(
    system_prompt: str,
    user_prompt: str,
    *,
    model_id: str | None = None,
) -> GenerateResult:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Set GEMINI_API_KEY for cloud mode (or switch to Private / on-device)."
        )

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    mid = _resolve_gemini_model(model_id)

    def _call(mid_: str):
        return client.models.generate_content(
            model=mid_,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                temperature=0.35,
                system_instruction=system_prompt,
            ),
        )

    try:
        resp = _call(mid)
    except Exception as e:
        err = str(e)
        # Unknown or sunset model id in GEMINI_MODEL — retry once with default.
        if ("404" in err or "NOT_FOUND" in err) and mid != DEFAULT_GEMINI_MODEL:
            mid = DEFAULT_GEMINI_MODEL
            resp = _call(mid)
        else:
            raise RuntimeError(
                f"Gemini request failed ({mid}). Check GEMINI_MODEL matches an id from "
                "client.models.list() that supports generateContent, or remove GEMINI_MODEL to use "
                f"{DEFAULT_GEMINI_MODEL}. Original error: {err}"
            ) from e
    text = ""
    if resp.text:
        text = resp.text.strip()
    elif resp.candidates:
        for c in resp.candidates:
            if c.content and c.content.parts:
                for part in c.content.parts:
                    if part.text:
                        text += part.text
        text = text.strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response.")

    return GenerateResult(
        answer=text,
        model_label=mid,
        source="gemini",
    )


def _inject_cactus_path() -> None:
    """Prefer CACTUS_PYTHON_SRC; else common clone path ~/Documents/cactus/python/src."""
    extra = os.environ.get("CACTUS_PYTHON_SRC")
    if not extra:
        fallback = Path.home() / "Documents" / "cactus" / "python" / "src"
        if fallback.is_dir():
            extra = str(fallback)
    if extra and extra not in sys.path:
        sys.path.insert(0, extra)


def _private_generate(
    system_prompt: str,
    user_prompt: str,
) -> GenerateResult:
    """On-device via Cactus (Gemma weights on disk). See README in repo for setup."""
    model_path = os.environ.get("CACTUS_MODEL_PATH")
    if not model_path:
        raise RuntimeError(
            "Private mode runs Gemma on-device via Cactus and does not use Gemini. "
            "Install and build the Cactus SDK, download Gemma weights, then set environment variable "
            "CACTUS_MODEL_PATH to that weights directory (and optionally CACTUS_PYTHON_SRC). "
            "If you only want cloud answers, switch to Cloud mode and set GEMINI_API_KEY."
        )

    _inject_cactus_path()

    try:
        from cactus import cactus_complete, cactus_destroy, cactus_init
    except (ImportError, RuntimeError) as e:
        raise RuntimeError(
            "Could not load Cactus Python bindings. From the cactus repo run: "
            "`cactus build --python` (creates libcactus.dylib), set CACTUS_PYTHON_SRC to "
            "<cactus_repo>/python/src if not using the default ~/Documents/cactus path, "
            "and ensure CACTUS_MODEL_PATH points at a converted weights folder."
        ) from e

    options = json.dumps(
        {
            "max_tokens": int(os.environ.get("CACTUS_MAX_TOKENS", "512")),
            "temperature": float(os.environ.get("CACTUS_TEMPERATURE", "0.35")),
        }
    )
    messages = json.dumps(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )

    try:
        handle = cactus_init(model_path, None, False)
    except TypeError:
        handle = cactus_init(model_path)
    try:
        raw = cactus_complete(handle, messages, options, None, None)
        data = json.loads(raw)
    finally:
        cactus_destroy(handle)

    text = (data.get("response") or "").strip()
    if not text and not data.get("success", True):
        raise RuntimeError(data.get("error") or "On-device model failed.")

    label = os.path.basename(model_path.rstrip("/"))
    return GenerateResult(answer=text, model_label=f"Cactus · {label}", source="cactus")


def generate_answer(
    *,
    mode: Mode,
    system_prompt: str,
    user_prompt: str,
) -> GenerateResult:
    if mode == "cloud":
        return _cloud_generate(system_prompt, user_prompt)
    return _private_generate(system_prompt, user_prompt)


def availability() -> dict:
    """What the UI can show in settings / health."""
    cloud = bool(os.environ.get("GEMINI_API_KEY"))
    path_set = bool(os.environ.get("CACTUS_MODEL_PATH"))
    import_ok = False
    if path_set:
        _inject_cactus_path()
        try:
            import cactus  # noqa: F401

            import_ok = True
        except (ImportError, RuntimeError):
            import_ok = False
    return {
        "cloud": cloud,
        "private": path_set and import_ok,
        "cactus_weights_configured": path_set,
        "cactus_import_ok": import_ok,
    }
