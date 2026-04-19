"""Extract plain text from PDF uploads."""

from __future__ import annotations

import io

from pypdf import PdfReader


def extract_pdf_text(data: bytes) -> tuple[str, int]:
    reader = PdfReader(io.BytesIO(data))
    chunks: list[str] = []
    for page in reader.pages:
        chunks.append(page.extract_text() or "")
    text = "\n\n".join(c.strip() for c in chunks if c.strip())
    return text, len(reader.pages)
