"""Evidence ledger helpers for research jobs."""

from __future__ import annotations

import hashlib
from typing import Any


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        if name in value:
            return value[name]
        metadata = value.get("metadata")
        if isinstance(metadata, dict) and name in metadata:
            return metadata[name]
        return default
    if hasattr(value, name):
        return getattr(value, name)
    metadata = getattr(value, "metadata", None)
    if isinstance(metadata, dict) and name in metadata:
        return metadata[name]
    return default


def _compact(text: Any, limit: int = 1400) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."


def evidence_id_for(corpus_id: str, doc_id: str, chunk_id: str, quote: str) -> str:
    raw = "|".join([corpus_id, doc_id, chunk_id, quote[:200]])
    return "ev_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def chunk_to_evidence(chunk: Any, *, subquestion_id: str, rank: int) -> dict[str, Any] | None:
    corpus_id = str(_field(chunk, "corpus_id") or "")
    doc_id = str(_field(chunk, "doc_id") or "")
    chunk_id = str(_field(chunk, "chunk_id") or _field(chunk, "id") or "")
    text = _compact(_field(chunk, "text") or _field(chunk, "content") or "")
    if not corpus_id or not chunk_id or not text:
        return None
    quote = text[:700]
    score = _field(chunk, "score", 0.0)
    try:
        score = float(score)
    except Exception:
        score = 0.0
    return {
        "evidence_id": evidence_id_for(corpus_id, doc_id, chunk_id, quote),
        "citation_id": "",
        "source": "retrieval",
        "subquestion_id": subquestion_id,
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "parent_id": _field(chunk, "parent_id"),
        "title": _field(chunk, "doc_name") or _field(chunk, "filename") or doc_id or chunk_id,
        "quote": quote,
        "summary": _compact(_field(chunk, "summary") or quote, 300),
        "score": score,
        "rank": rank,
    }


def dedupe_and_number_evidence(
    records: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda r: (-float(r.get("score") or 0.0), int(r.get("rank") or 9999))):
        key = (
            str(record.get("corpus_id") or ""),
            str(record.get("doc_id") or ""),
            str(record.get("chunk_id") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        numbered = dict(record)
        numbered["citation_id"] = f"C{len(out) + 1}"
        out.append(numbered)
        if len(out) >= max(1, int(limit)):
            break
    return out
