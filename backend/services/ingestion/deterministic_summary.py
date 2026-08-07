"""Deterministic ingestion summaries — ``deterministic_summary.v1``.

The REQUIRED baseline summary product for book ingestion. Every artifact in
this module is a pure function of durable Mongo artifacts (parent text, child
boundaries, Relex extraction rows):

* no provider calls (no summary_provider_pool, no LiteLLM, no cloud model)
* no cost authority (summary_cost_control belongs to the optional product)
* no network, no randomness, no wall-clock identity

Optional LLM enrichment is a separate product (``llm_summary_enrichment.v1``)
that remains gated behind an explicit summary cost authority and must never
block ingestion, readiness, or ordinary retrieval.

Artifacts produced:

* deterministic child record   (chunk metadata)
* deterministic parent summary (parent_chunks.summary + identity stamps)
* section/document summaries   (summary_tree built with ``use_llm=False``)

Identity: summary ids are sha256 over canonical JSON of schema + algorithm +
config hash + target id + source hash, so identical inputs reproduce
byte-identical artifacts regardless of completion order.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from models.contracts import ParentSummaryRecord, ParentSummaryWrite
from services.ingestion.section_classifier import parent_summary_required_clause

DETERMINISTIC_SUMMARY_SCHEMA_VERSION = "deterministic_summary.v1"
DETERMINISTIC_SUMMARY_ALGORITHM_VERSION = "deterministic_parent.v1"
DETERMINISTIC_CHILD_ALGORITHM_VERSION = "deterministic_child.v1"
DETERMINISTIC_SUMMARY_MODEL_STAMP = "deterministic:v1"

# Fixed scoring/limits. Any change alters the config hash and therefore every
# summary id — regeneration is then observable and auditable.
DETERMINISTIC_SUMMARY_CONFIG: dict[str, Any] = {
    "max_key_points": 4,
    "max_key_entities": 8,
    "max_key_terms": 8,
    "max_sentence_chars": 400,
    "min_sentence_chars": 25,
    "max_summary_chars": 1200,
    "sentence_weights": {
        "first": 2.0,
        "last": 1.0,
        "definition": 1.5,
        "date_or_quantity": 0.75,
        "heading_overlap": 1.25,
        "short_penalty_below_chars": 25,
        "short_penalty": -1.0,
    },
    "definition_pattern": r"\b(?:is|are|was|were|means?|refers? to|defined as|consists? of)\b",
}

_DEFINITION_RE = re.compile(
    DETERMINISTIC_SUMMARY_CONFIG["definition_pattern"], re.IGNORECASE
)
_DATE_OR_QUANTITY_RE = re.compile(r"\b(?:\d{4}|\d+(?:\.\d+)?\s*(?:%|percent|million|billion|thousand|kg|km|mb|gb))\b")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have in is it its of on or "
    "that the this to was were will with we you they he she i not can may".split()
)


def canonical_summary_json(value: Any) -> str:
    """Canonical serialization used before every identity hash."""

    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def deterministic_summary_config_hash() -> str:
    return _sha256_hex(
        canonical_summary_json(
            {
                "schema_version": DETERMINISTIC_SUMMARY_SCHEMA_VERSION,
                "algorithm_version": DETERMINISTIC_SUMMARY_ALGORITHM_VERSION,
                "config": DETERMINISTIC_SUMMARY_CONFIG,
            }
        )
    )


def deterministic_summary_id(*, parent_id: str, source_hash: str) -> str:
    return "detsum_" + _sha256_hex(
        canonical_summary_json(
            {
                "schema_version": DETERMINISTIC_SUMMARY_SCHEMA_VERSION,
                "algorithm_version": DETERMINISTIC_SUMMARY_ALGORITHM_VERSION,
                "config_hash": deterministic_summary_config_hash(),
                "parent_id": parent_id,
                "source_hash": source_hash,
            }
        )
    )


def parent_source_hash(text: str) -> str:
    return "sha256:" + _sha256_hex(text)


def _heading_path(row: dict[str, Any]) -> list[str]:
    return [str(h) for h in (row.get("heading_path") or []) if str(h).strip()]


def _heading_tokens(row: dict[str, Any]) -> frozenset[str]:
    tokens: set[str] = set()
    for heading in _heading_path(row):
        tokens.update(
            token
            for token in re.findall(r"[a-z0-9_]{3,}", heading.lower())
            if token not in _STOPWORDS
        )
    return frozenset(tokens)


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def score_sentences(
    sentences: list[str],
    *,
    heading_tokens: frozenset[str],
    config: dict[str, Any] | None = None,
) -> list[tuple[float, int, str]]:
    """Fixed salience scoring. Returns (score, canonical_index, sentence)."""

    cfg = config or DETERMINISTIC_SUMMARY_CONFIG
    weights = cfg["sentence_weights"]
    scored: list[tuple[float, int, str]] = []
    last_index = len(sentences) - 1
    for index, sentence in enumerate(sentences):
        score = 0.0
        lowered = sentence.lower()
        if index == 0:
            score += float(weights["first"])
        if index == last_index and last_index > 0:
            score += float(weights["last"])
        if _DEFINITION_RE.search(lowered):
            score += float(weights["definition"])
        if _DATE_OR_QUANTITY_RE.search(sentence):
            score += float(weights["date_or_quantity"])
        sentence_tokens = set(re.findall(r"[a-z0-9_]{3,}", lowered))
        if heading_tokens & sentence_tokens:
            score += float(weights["heading_overlap"])
        if len(sentence) < int(cfg["min_sentence_chars"]):
            score += float(weights["short_penalty"])
        scored.append((score, index, sentence))
    return scored


def representative_sentences(
    text: str,
    *,
    heading_tokens: frozenset[str],
    limit: int | None = None,
) -> list[str]:
    """Top-salience sentences, re-ordered by source position (canonical)."""

    cfg = DETERMINISTIC_SUMMARY_CONFIG
    cap = int(limit or cfg["max_key_points"])
    scored = score_sentences(split_sentences(text), heading_tokens=heading_tokens)
    ranked = sorted(scored, key=lambda item: (-item[0], item[1]))[:cap]
    ordered = sorted(ranked, key=lambda item: item[1])
    return [sentence[: int(cfg["max_sentence_chars"])] for _, _, sentence in ordered]


def _entity_inventory(
    extraction_rows: list[dict[str, Any]],
    *,
    limit: int | None = None,
) -> list[str]:
    """Accepted entities from Relex rows, canonical (count desc, text asc)."""

    cap = int(limit or DETERMINISTIC_SUMMARY_CONFIG["max_key_entities"])
    counts: dict[str, int] = {}
    for row in extraction_rows:
        status = str(row.get("status") or row.get("assessment") or "")
        if status and status not in {"accepted", "corroborated", "promoted", "qualified"}:
            continue
        for field in ("subject", "object", "entity", "canonical_name"):
            name = str(row.get(field) or "").strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
        for mention in row.get("mentions") or []:
            name = str((mention or {}).get("text") or "").strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [name for name, _ in ranked[:cap]]


def content_inventory(child_rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for child in child_rows:
        kind = str(child.get("chunk_kind") or "body")
        counts[kind] = counts.get(kind, 0) + 1
    return {kind: counts[kind] for kind in sorted(counts)}


def render_parent_summary(
    *,
    topic: str,
    inventory: dict[str, int],
    key_points: list[str],
    entities: list[str],
    quality_flags: list[str],
) -> str:
    """Bounded template rendering — a deterministic function of its fields."""

    cfg = DETERMINISTIC_SUMMARY_CONFIG
    parts: list[str] = []
    inventory_text = ", ".join(f"{count} {kind}" for kind, count in inventory.items())
    parts.append(f"Topic: {topic}.")
    if inventory_text:
        parts.append(f"Content: {inventory_text}.")
    if key_points:
        parts.append("Key points: " + " ".join(key_points))
    if entities:
        parts.append("Key entities: " + ", ".join(entities) + ".")
    if quality_flags:
        parts.append("Quality flags: " + ", ".join(sorted(quality_flags)) + ".")
    return " ".join(parts)[: int(cfg["max_summary_chars"])]


def build_deterministic_parent_summary(
    *,
    parent_row: dict[str, Any],
    child_rows: list[dict[str, Any]] | None = None,
    extraction_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Structured deterministic parent summary + bounded rendering.

    Every key point is a verbatim sentence from the parent text; entities come
    from accepted Relex extraction rows. Nothing is invented.
    """

    text = str(parent_row.get("text") or "")
    parent_id = str(parent_row.get("parent_id") or "")
    heading = _heading_path(parent_row)
    topic = heading[-1] if heading else (text.split("\n", 1)[0][:80] or parent_id)
    heading_tokens = _heading_tokens(parent_row)
    key_points = representative_sentences(text, heading_tokens=heading_tokens)
    entities = _entity_inventory(extraction_rows or [])
    inventory = content_inventory(child_rows or [])
    source_hash = str(parent_row.get("source_hash") or "") or parent_source_hash(text)

    quality_flags = ["deterministic", "extractive_only"]
    if len(text.strip()) < 80:
        quality_flags.append("low_text_density")
    if str(parent_row.get("chunk_kind") or "") in {"table", "code", "output"}:
        quality_flags.append("structured_content")

    summary_text = render_parent_summary(
        topic=topic,
        inventory=inventory,
        key_points=key_points,
        entities=entities,
        quality_flags=quality_flags,
    )
    return {
        "summary_type": "deterministic_parent",
        "schema_version": DETERMINISTIC_SUMMARY_SCHEMA_VERSION,
        "algorithm_version": DETERMINISTIC_SUMMARY_ALGORITHM_VERSION,
        "config_hash": deterministic_summary_config_hash(),
        "summary_id": deterministic_summary_id(parent_id=parent_id, source_hash=source_hash),
        "source_hash": source_hash,
        "topic": topic,
        "content_inventory": inventory,
        "key_points": key_points,
        "key_entities": entities,
        "quality_flags": sorted(quality_flags),
        "evidence_child_ids": sorted(
            {
                str(child.get("chunk_id"))
                for child in (child_rows or [])
                if child.get("chunk_id")
            }
        ),
        "summary": summary_text,
    }


def to_parent_summary_write(
    built: dict[str, Any],
    *,
    parent_row: dict[str, Any],
    updated_at: datetime | None = None,
) -> ParentSummaryWrite:
    """Typed write command for the Mongo summary writer boundary."""

    key_points = [
        {"source_child_ids": built.get("evidence_child_ids") or [], "text": point}
        for point in built.get("key_points") or []
    ]
    record = ParentSummaryRecord(
        summary=built["summary"],
        key_terms=(built.get("key_entities") or [])[:8] or None,
        schema_version=DETERMINISTIC_SUMMARY_SCHEMA_VERSION,
        summary_type="deterministic_parent",
        key_points=key_points or None,
        entity_hints=built.get("key_entities") or None,
        abstraction_level="evidence_bound",
        temporal_class="unknown",
        time_expressions=[],
        source_child_ids=built.get("evidence_child_ids") or None,
        summary_id=built["summary_id"],
        source_hash=built["source_hash"],
        summary_model=DETERMINISTIC_SUMMARY_MODEL_STAMP,
        summary_created_at=(updated_at or datetime.now(timezone.utc)).isoformat(),
        validation_status="deterministic",
        quality_flags=built.get("quality_flags") or [],
        retrieval_text=built["summary"],
    )
    return ParentSummaryWrite(
        parent_id=str(parent_row["parent_id"]),
        doc_id=str(parent_row.get("doc_id") or ""),
        corpus_id=str(parent_row.get("corpus_id") or ""),
        record=record,
        summary_updated_at=updated_at or datetime.now(timezone.utc),
        source_text=str(parent_row.get("text") or "") or None,
    )


def build_deterministic_child_record(
    chunk_row: dict[str, Any],
    extraction_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Formal deterministic child information record (§2.2 contract)."""

    metadata = chunk_row.get("metadata") or {}
    rows = extraction_rows or []
    accepted = [
        str(row.get("claim_text") or row.get("text") or "")
        for row in rows
        if str(row.get("status") or row.get("assessment") or "") in {"accepted", "corroborated"}
    ]
    qualified = [
        str(row.get("claim_text") or row.get("text") or "")
        for row in rows
        if str(row.get("status") or row.get("assessment") or "") == "qualified"
    ]
    text = str(chunk_row.get("text") or "")
    return {
        "algorithm_version": DETERMINISTIC_CHILD_ALGORITHM_VERSION,
        "schema_version": DETERMINISTIC_SUMMARY_SCHEMA_VERSION,
        "chunk_id": str(chunk_row.get("chunk_id") or ""),
        "parent_id": str(chunk_row.get("parent_id") or ""),
        "doc_id": str(chunk_row.get("doc_id") or ""),
        "heading_path": _heading_path(chunk_row),
        "chunk_kind": str(chunk_row.get("chunk_kind") or "body"),
        "language": str(chunk_row.get("language") or metadata.get("language") or ""),
        "page_start": chunk_row.get("page_start") or metadata.get("page_start"),
        "page_end": chunk_row.get("page_end") or metadata.get("page_end"),
        "source_offsets": metadata.get("source_offsets"),
        "primary_entities": _entity_inventory(rows),
        "accepted_claims": sorted(c for c in accepted if c),
        "qualified_claims": sorted(c for c in qualified if c),
        "definitions": sorted(
            {
                sentence
                for sentence in split_sentences(text)
                if _DEFINITION_RE.search(sentence.lower())
            }
        )[:8],
        "dates_and_time_expressions": sorted(
            set(_DATE_OR_QUANTITY_RE.findall(text))
        ),
        "code_symbols": sorted(set(metadata.get("symbols_defined") or [])),
        "table_shape": metadata.get("table_shape"),
        "caption_links": sorted(metadata.get("caption_links") or []),
        "output_links": sorted(metadata.get("output_links") or []),
        "quality_flags": sorted(set(metadata.get("quality_flags") or [])),
        "representative_sentences": representative_sentences(
            text, heading_tokens=_heading_tokens(chunk_row)
        ),
    }


MISSING_SUMMARY_CLAUSE: dict[str, Any] = {
    "$or": [
        {"summary": {"$exists": False}},
        {"summary": None},
        {"summary": ""},
    ]
}


async def run_deterministic_parent_summaries(
    db: Any,
    *,
    corpus_id: str,
    limit: int = 25,
    doc_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Materialize deterministic parent summaries for a bounded slice.

    Same parent population as the provider-backed lane
    (``parent_summary_required_clause`` + missing-summary clause), so the two
    products are interchangeable at the readiness boundary. Pure Mongo
    read/write: no provider, no network, no cost authority.
    """

    from services.storage.mongo_writer import write_parent_summaries

    limit = max(1, min(int(limit or 25), 500))
    query: dict[str, Any] = {
        "corpus_id": corpus_id,
        "$and": [parent_summary_required_clause(), MISSING_SUMMARY_CLAUSE],
    }
    if doc_ids is not None:
        query["doc_id"] = {"$in": sorted({str(d) for d in doc_ids if str(d)})}
    rows = (
        await db["parent_chunks"]
        .find(
            query,
            {
                "_id": 0,
                "parent_id": 1,
                "doc_id": 1,
                "corpus_id": 1,
                "chunk_kind": 1,
                "heading_path": 1,
                "text": 1,
                "source_hash": 1,
                "child_ids": 1,
                "source_child_ids": 1,
            },
        )
        .sort("parent_id", 1)
        .limit(limit)
        .to_list(length=limit)
    )
    rows = [row for row in rows if str(row.get("text") or "").strip()]
    if not rows:
        return {
            "status": "empty",
            "corpus_id": corpus_id,
            "product": DETERMINISTIC_SUMMARY_SCHEMA_VERSION,
            "generated": 0,
            "generation_errors": [],
        }

    # Child kinds + accepted extraction evidence, fetched once per batch.
    child_ids = sorted(
        {
            str(child_id)
            for row in rows
            for child_id in (row.get("source_child_ids") or row.get("child_ids") or [])
            if str(child_id)
        }
    )
    child_rows_by_id: dict[str, dict[str, Any]] = {}
    if child_ids:
        async for child in db["chunks"].find(
            {"corpus_id": corpus_id, "chunk_id": {"$in": child_ids}},
            {"_id": 0, "chunk_id": 1, "chunk_kind": 1},
        ):
            child_rows_by_id[str(child.get("chunk_id"))] = child
    extraction_rows: list[dict[str, Any]] = []
    parent_ids = sorted(str(row.get("parent_id")) for row in rows if row.get("parent_id"))
    if parent_ids:
        extraction_rows = (
            await db["ghost_b_extractions"]
            .find(
                {"corpus_id": corpus_id, "parent_id": {"$in": parent_ids}},
                {
                    "_id": 0,
                    "parent_id": 1,
                    "status": 1,
                    "assessment": 1,
                    "subject": 1,
                    "object": 1,
                    "entity": 1,
                    "canonical_name": 1,
                    "mentions": 1,
                },
            )
            .to_list(length=None)
        )
    extractions_by_parent: dict[str, list[dict[str, Any]]] = {}
    for row in extraction_rows:
        extractions_by_parent.setdefault(str(row.get("parent_id") or ""), []).append(row)

    writes: list[ParentSummaryWrite] = []
    for row in rows:
        children = [
            child_rows_by_id[str(child_id)]
            for child_id in (row.get("source_child_ids") or row.get("child_ids") or [])
            if str(child_id) in child_rows_by_id
        ]
        built = build_deterministic_parent_summary(
            parent_row=row,
            child_rows=children,
            extraction_rows=extractions_by_parent.get(str(row.get("parent_id") or ""), []),
        )
        writes.append(to_parent_summary_write(built, parent_row=row))
    await write_parent_summaries(db, writes)
    return {
        "status": "healthy",
        "corpus_id": corpus_id,
        "product": DETERMINISTIC_SUMMARY_SCHEMA_VERSION,
        "generated": len(writes),
        "generation_errors": [],
    }
