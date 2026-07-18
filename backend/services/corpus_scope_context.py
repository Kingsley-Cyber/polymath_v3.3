"""Read-only selected-corpus context for the corpus_scope.v3 arbiter.

This module performs bounded Mongo reads only. It never alters the retrieval
query, evidence packet, prompt, scoring, or corpus data. All absence decisions
fail open unless the relevant selected-corpus lookup completed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from models.schemas import SourceChunk
from services.cache_util import TTLCache
from services.ingestion.bibliographic import normalize_date_string
from services.retriever.anchor_detect import detect_anchor_doc_ids
from services.retriever.document_anchor import _doc_labels, _score_doc_match
from services.retriever.librarian_planner import named_source_phrases
from services.retriever.temporal import (
    detect_temporal_intent,
    temporal_match_details,
)
from services.storage.record_status import with_active_records

CORPUS_SCOPE_CONTEXT_VERSION = "corpus_scope_context.v1"
_CATALOG_CACHE = TTLCache(maxsize=32, ttl_seconds=900.0)
_YEAR_RE = re.compile(r"\b(?:18|19|20)\d{2}\b")
_LOCATOR_RE = re.compile(
    r"\b(?:in|from)\s+(?:the\s+)?(.+?)\s+" r"(?:book|document|source|paper)\b",
    re.IGNORECASE,
)
_ARTIFACT_NUMBERED_RE = re.compile(
    r"\b(figure|table)\s+(\d+(?:\.\d+){0,2})\b",
    re.IGNORECASE,
)
_ARTIFACT_CHECKLIST_RE = re.compile(
    r"\b(?:(\d+)[-\s]?step\s+)?checklist\b",
    re.IGNORECASE,
)
_ARTIFACT_LOCATOR_TABLE_RE = re.compile(
    r"\b(?:the|a|an)\s+([a-z0-9][a-z0-9$%&'’\-\s]{1,60}?)\s+table\b",
    re.IGNORECASE,
)
_ARTIFACT_INTERVIEW_RE = re.compile(
    r"\b((?:[a-z0-9]+[-\s]+){0,3}interview)\b",
    re.IGNORECASE,
)
_ARTIFACT_FIELDS = ("text", "summary", "retrieval_text", "heading_path")
_GENERIC_SOURCE_REFERENCE_TOKENS = frozenset(
    {
        "a",
        "an",
        "book",
        "books",
        "corpus",
        "document",
        "documents",
        "my",
        "paper",
        "source",
        "sources",
        "the",
        "these",
        "this",
    }
)
_NAMED_SOURCE_SHAPE_GENERIC_TOKENS = frozenset(
    {
        *_GENERIC_SOURCE_REFERENCE_TOKENS,
        "actor",
        "actors",
        "and",
        "animator",
        "animators",
        "artist",
        "artists",
        "both",
        "camera",
        "cinematographer",
        "cinematographers",
        "director",
        "directors",
        "drawing",
        "each",
        "editor",
        "editors",
        "expert",
        "experts",
        "film",
        "instructor",
        "instructors",
        "or",
        "researcher",
        "researchers",
        "scholar",
        "scholars",
        "teacher",
        "teachers",
        "visual",
        "writer",
        "writers",
    }
)
_NAMED_SOURCE_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_POSSESSIVE_AUTHOR_RE = re.compile(
    r"\b(?P<author>[A-Za-z][A-Za-z\-]*"
    r"(?:\s+[A-Za-z][A-Za-z\-]*){0,3})"
    r"(?:['’]s\b|['’](?=\s|$))"
)
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"))


def _stable_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _values(blob: object, *keys: str) -> list[str]:
    if not isinstance(blob, Mapping):
        return []
    output: list[str] = []
    for key in keys:
        value = blob.get(key)
        for item in value if isinstance(value, list) else [value]:
            text = str(item or "").strip()
            if text and text not in output:
                output.append(text)
    return output


def _document_identity(row: Mapping[str, Any]) -> dict[str, str]:
    source_identity = (
        row.get("source_identity")
        if isinstance(row.get("source_identity"), Mapping)
        else {}
    )
    return {
        "corpus_id": str(row.get("corpus_id") or ""),
        "doc_id": str(row.get("doc_id") or ""),
        "content_sha256": str(
            source_identity.get("content_sha256") or row.get("content_sha256") or ""
        ),
        "source_version_id": str(
            source_identity.get("source_version_id")
            or row.get("source_version_id")
            or ""
        ),
        "updated_at": str(row.get("updated_at") or ""),
    }


def _catalog_epoch(
    rows: Sequence[Mapping[str, Any]],
    corpus_ids: Sequence[str],
) -> str:
    payload = {
        "corpus_ids": sorted(set(corpus_ids)),
        "documents": sorted(
            (_document_identity(row) for row in rows),
            key=lambda item: (item["corpus_id"], item["doc_id"]),
        ),
    }
    return "sha256:" + hashlib.sha256(_stable_json(payload)).hexdigest()


def _normalized_anchor_docs(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        authors: list[str] = []
        authors.extend(_values(row, "author", "authors"))
        for blob_name in ("metadata", "document_metadata", "source_metadata"):
            authors.extend(
                _values(
                    row.get(blob_name),
                    "author",
                    "authors",
                    "creator",
                    "creators",
                )
            )
        title = str(row.get("title") or "").strip()
        if not title:
            for blob_name in ("metadata", "document_metadata", "source_metadata"):
                nested = _values(
                    row.get(blob_name),
                    "title",
                    "book_title",
                    "name",
                )
                if nested:
                    title = nested[0]
                    break
        title = title or str(row.get("filename") or "").strip()
        output.append(
            {
                **dict(row),
                "title": title,
                "author": " ".join(dict.fromkeys(authors)),
            }
        )
    return output


def _phrase_is_quoted(query: str, phrase: str) -> bool:
    normalized_query = " ".join(str(query or "").split()).casefold()
    normalized_phrase = " ".join(str(phrase or "").split()).casefold()
    return any(
        f"{left}{normalized_phrase}{right}" in normalized_query
        for left, right in _QUOTE_PAIRS
    )


def _phrase_has_possessive_author(phrase: str) -> bool:
    for match in _POSSESSIVE_AUTHOR_RE.finditer(phrase):
        author_tokens = [
            token.casefold()
            for token in _NAMED_SOURCE_WORD_RE.findall(match.group("author"))
        ]
        if (
            author_tokens
            and author_tokens[-1] not in _NAMED_SOURCE_SHAPE_GENERIC_TOKENS
        ):
            return True
    return False


def _title_shaped_named_source(query: str, phrase: str) -> bool:
    """Reject bare roles while preserving explicit title/author surfaces."""

    if _phrase_is_quoted(query, phrase) or _phrase_has_possessive_author(phrase):
        return True
    return any(
        token[0].isupper()
        and token.casefold() not in _NAMED_SOURCE_SHAPE_GENERIC_TOKENS
        for token in _NAMED_SOURCE_WORD_RE.findall(phrase)
    )


def _eligible_named_source_phrases(query: str) -> tuple[str, ...]:
    output: list[str] = []
    for phrase in named_source_phrases(query):
        distinctive_tokens = {
            token for token in re.findall(r"[a-z0-9]+", phrase.casefold()) if token
        } - _GENERIC_SOURCE_REFERENCE_TOKENS
        if distinctive_tokens and _title_shaped_named_source(query, phrase):
            output.append(phrase)
    return tuple(output)


def _matched_named_documents(
    query: str,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    phrases = _eligible_named_source_phrases(query)
    if not phrases:
        return (), ()
    normalized_rows = _normalized_anchor_docs(rows)
    matched: set[str] = set()
    for phrase in phrases:
        matched.update(detect_anchor_doc_ids(phrase, normalized_rows))
        for row in normalized_rows:
            doc_id = str(row.get("doc_id") or "")
            if not doc_id:
                continue
            if any(
                _score_doc_match(phrase, label) >= 0.72
                for label in _doc_labels(dict(row))
            ):
                matched.add(doc_id)
    return phrases, tuple(sorted(matched))


def _locator_doc_ids(
    query: str,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    match = _LOCATOR_RE.search(query)
    if not match:
        return ()
    locator = " ".join(match.group(1).split())
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", locator.casefold())
        if len(token) >= 5 and token not in {"these", "their"}
    }
    if not tokens:
        return ()
    matches: list[str] = []
    for row in _normalized_anchor_docs(rows):
        labels = _doc_labels(dict(row))
        label_tokens = {
            token
            for label in labels
            for token in re.findall(r"[a-z0-9]+", label.casefold())
        }
        if tokens <= label_tokens:
            doc_id = str(row.get("doc_id") or "")
            if doc_id:
                matches.append(doc_id)
    return tuple(sorted(set(matches)))


def _artifact_spec(query: str) -> dict[str, Any] | None:
    numbered = _ARTIFACT_NUMBERED_RE.search(query)
    if numbered:
        kind = numbered.group(1).casefold()
        identifier = numbered.group(2)
        escaped = re.escape(identifier).replace(r"\.", r"[\.\s_-]+")
        return {
            "kind": kind,
            "identifier": identifier,
            "patterns": [rf"\b{kind}\s*{escaped}\b"],
        }

    locator_table = _ARTIFACT_LOCATOR_TABLE_RE.search(query)
    if locator_table and re.search(r"\b(?:chapter|appendix)\b", query, re.I):
        qualifier = re.sub(
            r"\s+",
            " ",
            locator_table.group(1).strip(" ,:;-"),
        )
        qualifier_pattern = re.escape(qualifier).replace(r"\ ", r"\s+")
        return {
            "kind": "table",
            "identifier": qualifier,
            "patterns": [rf"\b{qualifier_pattern}\s+table\b"],
        }

    checklist = _ARTIFACT_CHECKLIST_RE.search(query)
    if checklist and (
        checklist.group(1)
        or re.search(r"\b(?:chapter|appendix|ends?|ending)\b", query, re.I)
    ):
        count = checklist.group(1)
        pattern = (
            rf"\b{re.escape(count)}[-\s]?step\s+checklist\b"
            if count
            else r"\bchecklist\b"
        )
        return {
            "kind": "checklist",
            "identifier": f"{count}-step" if count else "locator_qualified",
            "patterns": [pattern],
        }

    interview = _ARTIFACT_INTERVIEW_RE.search(query)
    if interview and re.search(r"\bappendix\b", query, re.I):
        qualifier = re.sub(
            r"[^a-z0-9]+",
            " ",
            interview.group(1).casefold().replace("interview", ""),
        ).strip()
        qualifier = re.sub(r"^(?:the|a|an)\s+", "", qualifier)
        patterns = [r"\binterview\b", r"\bappendix\b"]
        if qualifier:
            qualifier_pattern = re.escape(qualifier).replace(r"\ ", r"[\s-]+")
            patterns.append(rf"\b{qualifier_pattern}\b")
        return {
            "kind": "interview",
            "identifier": qualifier or "appendix_interview",
            "patterns": patterns,
        }
    return None


def _artifact_mongo_query(
    *,
    corpus_ids: Sequence[str],
    doc_ids: Sequence[str],
    patterns: Sequence[str],
) -> dict[str, Any]:
    clauses: list[dict[str, Any]] = [
        {"corpus_id": {"$in": list(corpus_ids)}},
    ]
    if doc_ids:
        clauses.append({"doc_id": {"$in": list(doc_ids)}})
    for pattern in patterns:
        clauses.append(
            {
                "$or": [
                    {field: {"$regex": pattern, "$options": "i"}}
                    for field in _ARTIFACT_FIELDS
                ]
            }
        )
    return with_active_records({"$and": clauses})


def _date_year(value: object) -> int | None:
    normalized, _precision = normalize_date_string(str(value or ""))
    if not normalized:
        return None
    try:
        return int(normalized[:4])
    except (TypeError, ValueError):
        return None


def _time_expression_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        expressions = row.get("time_expressions")
        if not isinstance(expressions, list):
            continue
        cleaned = [
            dict(item)
            for item in expressions
            if isinstance(item, Mapping) and str(item.get("text") or "").strip()
        ]
        if cleaned:
            output.append(
                {
                    "corpus_id": str(row.get("corpus_id") or ""),
                    "doc_id": str(row.get("doc_id") or ""),
                    "temporal_class": str(row.get("temporal_class") or "unknown"),
                    "time_expressions": cleaned,
                }
            )
    return output


def _temporal_catalog(
    documents: Sequence[Mapping[str, Any]],
    temporal_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    years: set[int] = set()
    document_years: set[int] = set()
    for row in documents:
        for field in ("document_date", "source_published_at"):
            year = _date_year(row.get(field))
            if year is not None:
                years.add(year)
                document_years.add(year)
    expression_rows = _time_expression_rows(temporal_rows)
    expression_years: set[int] = set()
    for row in expression_rows:
        for expression in row["time_expressions"]:
            year = _date_year(expression.get("text"))
            if year is not None:
                years.add(year)
                expression_years.add(year)
    return {
        "min_year": min(years) if years else None,
        "max_year": max(years) if years else None,
        "year_count": len(years),
        "document_years": sorted(document_years),
        "expression_years": sorted(expression_years),
        "expression_rows": expression_rows,
    }


async def _find_rows(
    db: Any,
    collection: str,
    query: dict[str, Any],
    projection: dict[str, int],
) -> list[dict[str, Any]]:
    cursor = db[collection].find(query, projection)
    return list(await cursor.to_list(length=None))


async def build_corpus_scope_v3_context(
    db: Any,
    *,
    query: str,
    corpus_ids: Sequence[str] | None,
) -> dict[str, Any]:
    """Build authoritative read-only context for one selected-corpus query."""

    scoped = tuple(
        sorted({str(value).strip() for value in corpus_ids or () if str(value).strip()})
    )
    base: dict[str, Any] = {
        "context_version": CORPUS_SCOPE_CONTEXT_VERSION,
        "corpus_ids": list(scoped),
        "corpus_epoch": None,
        "named_source": {"eligible": False, "complete": False, "missing": False},
        "temporal": {"eligible": False, "complete": False, "out_of_range": False},
        "artifact": {"eligible": False, "complete": False, "matched_count": 0},
    }
    if db is None or not scoped:
        base["fail_open_reasons"] = ["selected_corpus_context_unavailable"]
        return base

    document_projection = {
        "_id": 0,
        "corpus_id": 1,
        "doc_id": 1,
        "filename": 1,
        "title": 1,
        "author": 1,
        "authors": 1,
        "metadata": 1,
        "document_metadata": 1,
        "source_metadata": 1,
        "facet_profile.doc_facets": 1,
        "document_date": 1,
        "source_published_at": 1,
        "source_identity": 1,
        "content_sha256": 1,
        "source_version_id": 1,
        "updated_at": 1,
    }
    try:
        documents = await _find_rows(
            db,
            "documents",
            with_active_records({"corpus_id": {"$in": list(scoped)}}),
            document_projection,
        )
    except Exception as exc:  # fail open at the selected-corpus boundary
        base["fail_open_reasons"] = [
            f"document_catalog_unavailable:{type(exc).__name__}"
        ]
        return base

    epoch = _catalog_epoch(documents, scoped)
    base["corpus_epoch"] = epoch
    phrases, matched_doc_ids = _matched_named_documents(query, documents)
    base["named_source"] = {
        "eligible": bool(phrases),
        "complete": True,
        "phrases": list(phrases),
        "matched_doc_ids": list(matched_doc_ids),
        "missing": bool(phrases and not matched_doc_ids),
        "signal_source": "full_selected_corpus_author_title_catalog",
    }

    cache_key = ("catalog", scoped, epoch)
    catalog = _CATALOG_CACHE.get(cache_key)
    temporal_complete = True
    if catalog is None:
        temporal_query = with_active_records(
            {
                "corpus_id": {"$in": list(scoped)},
                "$or": [
                    {"time_expressions.0": {"$exists": True}},
                    {"temporal_class": {"$exists": True}},
                ],
            }
        )
        temporal_projection = {
            "_id": 0,
            "corpus_id": 1,
            "doc_id": 1,
            "temporal_class": 1,
            "time_expressions": 1,
        }
        results = await asyncio.gather(
            _find_rows(
                db,
                "parent_chunks",
                temporal_query,
                temporal_projection,
            ),
            _find_rows(
                db,
                "summary_tree",
                temporal_query,
                temporal_projection,
            ),
            return_exceptions=True,
        )
        temporal_complete = not any(isinstance(result, Exception) for result in results)
        temporal_rows = [
            row
            for result in results
            if not isinstance(result, Exception)
            for row in result
        ]
        catalog = _temporal_catalog(documents, temporal_rows)
        catalog["temporal_complete"] = temporal_complete
        _CATALOG_CACHE.set(cache_key, catalog)
    else:
        temporal_complete = bool(catalog.get("temporal_complete"))

    intent = detect_temporal_intent(query)
    query_years = sorted(
        {
            int(year)
            for item in intent.expressions
            for year in _YEAR_RE.findall(item.text)
        }
    )
    exact_surfaces: set[str] = set()
    for index, row in enumerate(catalog.get("expression_rows") or []):
        details = temporal_match_details(
            SourceChunk(
                chunk_id=f"corpus-scope-temporal-{index}",
                parent_id="",
                doc_id=str(row.get("doc_id") or ""),
                corpus_id=str(row.get("corpus_id") or ""),
                text="",
                score=0.0,
                source_tier="corpus_scope_context",
                metadata={
                    "temporal": {
                        "temporal_class": row.get("temporal_class") or "unknown",
                        "time_expressions": row.get("time_expressions") or [],
                    }
                },
            ),
            intent,
        )
        exact_surfaces.update(details.get("exact_surfaces") or [])
    catalog_exact_years = {
        int(year)
        for key in ("document_years", "expression_years")
        for year in (catalog.get(key) or [])
    }
    exact_surfaces.update(
        str(year) for year in query_years if year in catalog_exact_years
    )
    min_year = catalog.get("min_year")
    max_year = catalog.get("max_year")
    out_of_range = bool(query_years and temporal_complete and not exact_surfaces)
    base["temporal"] = {
        "eligible": bool(intent.active and query_years),
        "complete": temporal_complete,
        "pattern_version": intent.diagnostics().get("pattern_version"),
        "expressions": [
            {"text": item.text, "family": item.family} for item in intent.expressions
        ],
        "query_years": query_years,
        "corpus_min_year": min_year,
        "corpus_max_year": max_year,
        "exact_support": sorted(exact_surfaces),
        "support_basis": "exact_time_expressions_or_document_dates",
        "out_of_range": out_of_range,
        "detector_error": intent.detector_error,
    }

    artifact_spec = _artifact_spec(query)
    if artifact_spec is not None:
        locator_doc_ids = _locator_doc_ids(query, documents)
        artifact_query = _artifact_mongo_query(
            corpus_ids=scoped,
            doc_ids=locator_doc_ids,
            patterns=artifact_spec["patterns"],
        )
        counts = await asyncio.gather(
            *(
                db[collection].count_documents(artifact_query, limit=1)
                for collection in ("chunks", "parent_chunks", "summary_tree")
            ),
            return_exceptions=True,
        )
        artifact_complete = not any(isinstance(count, Exception) for count in counts)
        matched_count = sum(
            int(count) for count in counts if not isinstance(count, Exception)
        )
        base["artifact"] = {
            "eligible": True,
            "complete": artifact_complete,
            "kind": artifact_spec["kind"],
            "identifier": artifact_spec["identifier"],
            "matched_count": matched_count,
            "lookup_scope": "locator_documents"
            if locator_doc_ids
            else "selected_corpus",
            "locator_doc_ids": list(locator_doc_ids),
        }
    else:
        base["artifact"] = {
            "eligible": False,
            "complete": True,
            "matched_count": 0,
        }
    return base


def clear_corpus_scope_context_cache() -> None:
    """Test/ingest hook for immediate in-process catalog invalidation."""

    _CATALOG_CACHE.clear()
