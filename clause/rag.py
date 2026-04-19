"""Lightweight retrieval: split the document and score paragraphs by term overlap."""

from __future__ import annotations

import re
from typing import Iterable

# Common English stopwords — keep legal terms; this is only for coarse matching.
_STOP = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "but",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "as",
    "by",
    "with",
    "from",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "must",
    "shall",
    "can",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "they",
    "them",
    "their",
    "there",
    "here",
    "what",
    "which",
    "who",
    "when",
    "where",
    "why",
    "how",
    "about",
    "into",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "between",
    "under",
    "again",
    "further",
    "then",
    "once",
    "i",
    "you",
    "he",
    "she",
    "we",
    "my",
    "your",
    "our",
    "me",
    "him",
    "her",
    "us",
    "am",
    "if",
    "so",
    "no",
    "not",
    "only",
    "own",
    "same",
    "than",
    "too",
    "very",
    "just",
    "also",
    "any",
    "all",
    "each",
    "some",
    "such",
    "other",
    "more",
    "most",
    "both",
    "either",
    "neither",
}


def split_paragraphs(text: str) -> list[str]:
    raw = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = re.split(r"\n\s*\n+", raw)
    cleaned: list[str] = []
    for p in parts:
        p = re.sub(r"[ \t]+", " ", p).strip()
        if len(p) < 20:
            continue
        cleaned.append(p)
    if not cleaned and raw.strip():
        return [raw.strip()]
    return cleaned


def tokenize(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]{1,}", text.lower())
    return {w for w in words if w not in _STOP and len(w) > 1}


def score_paragraph(question: str, paragraph: str) -> float:
    q = tokenize(question)
    p = tokenize(paragraph)
    if not q or not p:
        return 0.0
    overlap = len(q & p)
    return overlap / (len(q) ** 0.5)


def top_paragraphs(
    question: str,
    paragraphs: Iterable[str],
    *,
    max_chars: int = 12000,
    k: int = 6,
) -> list[str]:
    ranked = sorted(
        ((score_paragraph(question, p), p) for p in paragraphs),
        key=lambda x: x[0],
        reverse=True,
    )
    chosen: list[str] = []
    total = 0
    for _score, p in ranked[: max(k * 3, k)]:
        if not p:
            continue
        if total + len(p) > max_chars and chosen:
            break
        chosen.append(p)
        total += len(p)
        if len(chosen) >= k:
            break
    return chosen if chosen else list(paragraphs)[:3]
