"""Runtime helpers for using ingestion facets during retrieval.

These helpers are deliberately metadata-only. They do not decide final
answers; they keep facet fields attached to chunks and expose a small query
matcher so chat/graph coverage can ask, "Which stored document facets did the
user explicitly name?"
"""

from __future__ import annotations

import re
from typing import Any

from .normalizer import FACET_SCHEMA_VERSION, normalize_facet_id

_STOPWORDS = {
    "about",
    "after",
    "also",
    "all",
    "and",
    "across",
    "based",
    "between",
    "book",
    "books",
    "corpus",
    "could",
    "document",
    "documents",
    "framework",
    "frameworks",
    "from",
    "full",
    "give",
    "have",
    "help",
    "helps",
    "into",
    "libraries",
    "library",
    "make",
    "method",
    "methods",
    "model",
    "models",
    "over",
    "source",
    "sources",
    "spectrum",
    "strategies",
    "strategy",
    "system",
    "systems",
    "tactic",
    "tactics",
    "that",
    "their",
    "them",
    "this",
    "through",
    "using",
    "what",
    "when",
    "where",
    "with",
    "would",
}


def _norm(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: Any) -> list[str]:
    out: list[str] = []
    for token in _norm(value).split():
        if len(token) < 3 or token in _STOPWORDS:
            continue
        out.append(token)
        if token.endswith("ies") and len(token) > 5:
            out.append(f"{token[:-3]}y")
        if token.endswith("s") and len(token) > 5:
            out.append(token[:-1])
    return out


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if str(v or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


def metadata_with_facets(
    metadata: dict | None,
    carrier: dict | None,
) -> dict:
    """Merge top-level facet payload fields into chunk metadata."""

    base = dict(metadata or {})
    carrier = carrier or {}
    facet_ids = _as_list(carrier.get("facet_ids"))
    doc_facet_ids = _as_list(carrier.get("doc_facet_ids"))
    content_facet_ids = _as_list(carrier.get("content_facet_ids"))
    facet_text = str(carrier.get("facet_text") or "").strip()
    content_facet_text = str(carrier.get("content_facet_text") or "").strip()
    content_facet_source = str(carrier.get("content_facet_source") or "").strip()
    content_facet_confidence = carrier.get("content_facet_confidence")
    schema_version = str(
        carrier.get("facet_schema_version")
        or carrier.get("schema_version")
        or FACET_SCHEMA_VERSION
    )
    if (
        not facet_ids
        and not doc_facet_ids
        and not content_facet_ids
        and not facet_text
        and not content_facet_text
    ):
        return base
    semantic = dict(base.get("semantic_facets") or {})
    if facet_ids:
        semantic["facet_ids"] = facet_ids
    if doc_facet_ids:
        semantic["doc_facet_ids"] = doc_facet_ids
    if facet_text:
        semantic["facet_text"] = facet_text
    if content_facet_ids:
        semantic["content_facet_ids"] = content_facet_ids
    if content_facet_text:
        semantic["content_facet_text"] = content_facet_text
    if content_facet_source:
        semantic["content_facet_source"] = content_facet_source
    if content_facet_confidence not in (None, ""):
        try:
            semantic["content_facet_confidence"] = round(
                max(0.0, min(float(content_facet_confidence), 1.0)),
                3,
            )
        except (TypeError, ValueError):
            pass
    semantic["schema_version"] = schema_version
    semantic.setdefault("source", "ingestion")
    base["semantic_facets"] = semantic
    return base


def metadata_facet_terms(metadata: dict | None) -> list[str]:
    """Return normalized terms carried by a SourceChunk's facet metadata."""

    if not isinstance(metadata, dict):
        return []
    semantic = metadata.get("semantic_facets")
    if not isinstance(semantic, dict):
        return []
    values: list[str] = []
    values.extend(_as_list(semantic.get("facet_ids")))
    values.extend(_as_list(semantic.get("doc_facet_ids")))
    values.extend(_as_list(semantic.get("facet_text")))
    values.extend(_as_list(semantic.get("content_facet_ids")))
    values.extend(_as_list(semantic.get("content_facet_text")))
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        for candidate in (value, str(value).replace("_", " ")):
            term = _norm(candidate)
            if term and term not in seen:
                seen.add(term)
                out.append(term)
    return out


def _facet_values(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    values.append(str(row.get("display_name") or ""))
    values.append(str(row.get("facet_id") or "").replace("_", " "))
    values.extend(_as_list(row.get("aliases")))
    values.extend(_as_list(row.get("search_terms")))
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _norm(value)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _score_facet_match(query: str, facet: dict[str, Any]) -> tuple[float, list[str], int]:
    query_norm = _norm(query)
    query_tokens = set(_tokens(query_norm))
    best = 0.0
    matched: list[str] = []
    first_pos = 999999
    for value in _facet_values(facet):
        value_tokens = set(_tokens(value))
        if not value_tokens:
            continue
        score = 0.0
        if value in query_norm:
            score += 6.0 + min(len(value_tokens), 4)
            pos = query_norm.find(value)
            if pos >= 0:
                first_pos = min(first_pos, pos)
        overlap = query_tokens & value_tokens
        if overlap:
            coverage = len(overlap) / max(1, len(value_tokens))
            if len(overlap) >= 2 or any(len(token) >= 7 for token in overlap):
                score += (len(overlap) * 2.0) + (coverage * 2.0)
                positions = [
                    query_norm.find(token)
                    for token in overlap
                    if query_norm.find(token) >= 0
                ]
                if positions:
                    first_pos = min(first_pos, min(positions))
        if score > best:
            best = score
            matched = sorted(overlap) if overlap else [value]
    return best, matched[:5], first_pos


async def matching_ingest_facets(
    db: Any,
    query: str,
    corpus_ids: list[str] | None,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Find stored document facets explicitly named by the user's query."""

    if db is None or not query or not corpus_ids:
        return []
    docs = await db["documents"].find(
        {"corpus_id": {"$in": list(corpus_ids)}},
        {
            "_id": 0,
            "doc_id": 1,
            "corpus_id": 1,
            "filename": 1,
            "facet_profile": 1,
        },
    ).to_list(length=None)
    by_name: dict[str, dict[str, Any]] = {}
    for doc in docs:
        profile = doc.get("facet_profile") if isinstance(doc, dict) else None
        if not isinstance(profile, dict):
            continue
        for facet in profile.get("doc_facets") or []:
            if not isinstance(facet, dict):
                continue
            facet_id = str(facet.get("facet_id") or normalize_facet_id(facet.get("display_name")))
            if not facet_id:
                continue
            score, matched, first_pos = _score_facet_match(query, facet)
            if score < 4.0:
                continue
            existing = by_name.get(facet_id)
            support_terms = _facet_values(facet)[:10]
            doc_ref = {
                "doc_id": str(doc.get("doc_id") or ""),
                "corpus_id": str(doc.get("corpus_id") or ""),
                "filename": str(doc.get("filename") or ""),
            }
            if existing is None:
                by_name[facet_id] = {
                    "name": facet_id,
                    "label": str(facet.get("display_name") or facet_id.replace("_", " ")),
                    "matched": matched,
                    "query_matched": True,
                    "query_explicit": True,
                    "first_match_pos": first_pos,
                    "support_terms": support_terms,
                    "triggers": support_terms,
                    "source": "ingest_facet_profile",
                    "facet_doc_ids": [doc_ref["doc_id"]] if doc_ref["doc_id"] else [],
                    "facet_docs": [doc_ref],
                    "match_score": round(score, 3),
                }
            else:
                existing["match_score"] = max(float(existing.get("match_score") or 0), round(score, 3))
                if doc_ref["doc_id"] and doc_ref["doc_id"] not in existing["facet_doc_ids"]:
                    existing["facet_doc_ids"].append(doc_ref["doc_id"])
                    existing["facet_docs"].append(doc_ref)
                existing["first_match_pos"] = min(
                    int(existing.get("first_match_pos") or 999999),
                    first_pos,
                )
    rows = sorted(
        by_name.values(),
        key=lambda row: (
            int(row.get("first_match_pos") or 999999),
            -float(row.get("match_score") or 0.0),
            str(row.get("name") or ""),
        ),
    )
    return rows[: max(0, int(limit or 8))]


async def matching_vector_facets(
    db: Any,
    qdrant: Any,
    query: str,
    query_vector: list[float] | None,
    corpus_ids: list[str] | None,
    *,
    limit: int = 8,
    hit_limit: int = 96,
    score_floor: float = 0.42,
    relative_floor: float = 0.82,
) -> list[dict[str, Any]]:
    """Promote stored document facets from nearest Qdrant cosine hits.

    The ordinary retrieval path already uses Qdrant cosine similarity. This
    helper gives the facet-lane selector access to the same semantic signal:
    if a document appears near the query vector, its primary document facet can
    become an evidence lane even when the query words do not literally overlap
    with the facet name.
    """

    if db is None or qdrant is None or not query_vector or not corpus_ids:
        return []

    try:
        from qdrant_client import models
        from services.storage.qdrant_writer import (
            _col_for_corpus,
            _collection_layout,
            binary_quantization_search_params,
        )
    except Exception:
        return []

    docs = await db["documents"].find(
        {"corpus_id": {"$in": list(corpus_ids)}},
        {
            "_id": 0,
            "doc_id": 1,
            "corpus_id": 1,
            "filename": 1,
            "facet_profile": 1,
        },
    ).to_list(length=None)
    docs_by_id = {
        str(doc.get("doc_id") or ""): doc
        for doc in docs
        if str(doc.get("doc_id") or "")
    }
    if not docs_by_id:
        return []

    hits: list[Any] = []
    per_collection_limit = max(1, int(hit_limit or 96))
    for corpus_id in corpus_ids:
        try:
            collection_name = _col_for_corpus(str(corpus_id), "naive")
            kwargs: dict[str, Any] = {
                "collection_name": collection_name,
                "query": query_vector,
                "query_filter": models.Filter(
                    must=[
                        models.FieldCondition(
                            key="corpus_id",
                            match=models.MatchValue(value=str(corpus_id)),
                        )
                    ]
                ),
                "limit": per_collection_limit,
                "with_payload": True,
            }
            quantization_params = binary_quantization_search_params()
            if quantization_params is not None:
                kwargs["search_params"] = quantization_params
            try:
                has_named, _ = await _collection_layout(qdrant, collection_name)
                if has_named:
                    kwargs["using"] = "dense"
            except Exception:
                pass
            resp = await qdrant.query_points(**kwargs)
            hits.extend(list(getattr(resp, "points", []) or []))
        except Exception:
            continue

    if not hits:
        return []

    scored_hits: list[tuple[float, dict[str, Any]]] = []
    for hit in hits:
        payload = getattr(hit, "payload", None) or {}
        try:
            score = float(getattr(hit, "score", 0.0) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        doc_id = str(payload.get("doc_id") or "")
        if not doc_id or doc_id not in docs_by_id:
            continue
        scored_hits.append((score, payload))

    if not scored_hits:
        return []

    top_score = max(score for score, _ in scored_hits)
    active_floor = max(float(score_floor), top_score * float(relative_floor))

    by_name: dict[str, dict[str, Any]] = {}
    query_norm = _norm(query)
    for score, payload in scored_hits:
        if score < active_floor:
            continue
        doc_id = str(payload.get("doc_id") or "")
        doc = docs_by_id.get(doc_id) or {}
        profile = doc.get("facet_profile") if isinstance(doc, dict) else None
        if not isinstance(profile, dict):
            continue
        primary = str(profile.get("primary_facet_id") or "")
        doc_facets = [
            facet
            for facet in (profile.get("doc_facets") or [])
            if isinstance(facet, dict)
        ]
        preferred = [
            facet
            for facet in doc_facets
            if str(facet.get("facet_id") or "") == primary
            or str(facet.get("source_level") or "") == "doc"
        ]
        if not preferred and doc_facets:
            preferred = doc_facets[:1]

        for facet in preferred[:2]:
            facet_id = str(
                facet.get("facet_id") or normalize_facet_id(facet.get("display_name"))
            )
            if not facet_id:
                continue
            lexical_score, matched, first_pos = _score_facet_match(query, facet)
            support_terms = _facet_values(facet)[:10]
            if not matched:
                # Keep coverage retrieval grounded in concrete facet terms while
                # making the activation reason visible in traces.
                matched = [str(facet.get("display_name") or facet_id.replace("_", " "))]
            if first_pos >= 999999:
                positions = [
                    query_norm.find(token)
                    for token in _tokens(" ".join(support_terms))
                    if query_norm.find(token) >= 0
                ]
                if positions:
                    first_pos = min(positions)
            vector_score = round(score * 10.0, 3)
            row_score = max(float(lexical_score or 0.0), vector_score)
            query_explicit = float(lexical_score or 0.0) >= 4.0
            doc_ref = {
                "doc_id": doc_id,
                "corpus_id": str(doc.get("corpus_id") or payload.get("corpus_id") or ""),
                "filename": str(doc.get("filename") or payload.get("doc_name") or ""),
            }
            existing = by_name.get(facet_id)
            if existing is None:
                by_name[facet_id] = {
                    "name": facet_id,
                    "label": str(facet.get("display_name") or facet_id.replace("_", " ")),
                    "matched": matched[:5],
                    "query_matched": True,
                    "query_explicit": query_explicit,
                    "semantic_matched": True,
                    "first_match_pos": first_pos,
                    "support_terms": support_terms,
                    "triggers": support_terms,
                    "source": "vector_facet_probe",
                    "facet_doc_ids": [doc_id],
                    "facet_docs": [doc_ref],
                    "match_score": row_score,
                    "vector_score": round(score, 4),
                    "vector_hits": 1,
                }
            else:
                existing["match_score"] = max(
                    float(existing.get("match_score") or 0.0), row_score
                )
                existing["vector_score"] = max(
                    float(existing.get("vector_score") or 0.0), round(score, 4)
                )
                existing["vector_hits"] = int(existing.get("vector_hits") or 0) + 1
                existing["query_explicit"] = bool(
                    existing.get("query_explicit") or query_explicit
                )
                if doc_id not in existing["facet_doc_ids"]:
                    existing["facet_doc_ids"].append(doc_id)
                    existing["facet_docs"].append(doc_ref)
                existing["matched"] = list(
                    dict.fromkeys([*(existing.get("matched") or []), *matched])
                )[:5]
                existing["first_match_pos"] = min(
                    int(existing.get("first_match_pos") or 999999),
                    first_pos,
                )

    rows = sorted(
        by_name.values(),
        key=lambda row: (
            int(row.get("first_match_pos") or 999999),
            -float(row.get("match_score") or 0.0),
            -float(row.get("vector_score") or 0.0),
            str(row.get("name") or ""),
        ),
    )
    return rows[: max(0, int(limit or 8))]
