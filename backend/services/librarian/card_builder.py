"""Deterministic ``librarian_card.v0`` builder (Phase 1 / P2.1).

Cards are built from EXISTING projections only — zero LLM calls at build
time.  Per document the builder reads:

``corpus_lexicon``              corpus-scoped vocabulary entries tied to the
                                document via ``source_document_ids`` /
                                ``source_document_support``
``corpus_lexicon_sources``      the document-scoped lexicon contribution rows
                                (doc-tied definitions, application contexts,
                                contextual usages, chunk/parent provenance)
``ghost_b_extractions``         the document's per-chunk entity extractions
``parent_chunks``               Ghost A summary semantics (``mechanisms``,
                                ``main_mechanism``, ``semantic_chunk_type``,
                                and — when a future ingest stores them —
                                ``latent_concepts``)
``documents.doc_profile``       L4 tree profile (``concepts``,
                                ``section_ids``, ``summary_id``)

Card schema (``librarian_card.v0``) — the authoritative Mongo document in
the ``librarian_cards`` collection, upserted on ``(corpus_id, doc_id)``:

.. code-block:: python

    {
        "schema_version": "librarian_card.v0",
        "corpus_id": str,
        "doc_id": str,
        "built_at": datetime,           # UTC
        "builder_version": str,
        "rejected_value_count": int,    # values dropped for missing source_ids

        # Every entry in the seven seed fields below carries the same
        # provenance contract:
        #   {
        #     "value": str,          # display value
        #     "value_key": str,      # normalize_identity(value) — dedup key
        #     "method": str,         # derivation method(s), "+"-joined when
        #                            # several deterministic seeds agree
        #     "source_ids": [str],   # non-empty; parent/chunk/tree/lexicon ids
        #     "confidence": float,   # carried from the source artifact when it
        #                            # stores one; 1.0 marks a verbatim
        #                            # deterministic copy of a stored field
        #     "support": int,        # frequency used for deterministic ordering
        #   }
        # Entries lacking source_ids are REJECTED at build time (counted in
        # rejected_value_count) — empty-not-fabricated.
        # Lists are ordered by support desc, then value_key asc.

        "central_subjects": [entry],          # doc-scoped lexicon canonical
                                              # terms + doc_profile.concepts +
                                              # Ghost B entity canonical_names
        "mechanisms_taught": [entry],         # parent mechanisms[] +
                                              # main_mechanism (validated
                                              # parents only)
        "candidate_latent_subjects": [entry], # SEPARATE field: parents'
                                              # latent_concepts[].concept with
                                              # evidence_basis == "direct".
                                              # Kept apart from
                                              # central_subjects because the
                                              # artifact is LLM-generated at
                                              # ingest time.
        "capabilities_developed": [entry],    # doc-scoped lexicon
                                              # application_contexts +
                                              # utility-gloss fragments
                                              # (functional contextual usages)
        "problems_addressed": [entry],        # ONLY doc-tied lexicon
                                              # definitional evidence; entries
                                              # carry an extra "subject" key
        "transferable_principles": [entry],   # doc-scoped lexicon entries with
                                              # corpus support_count >= 3 AND
                                              # >= 2 distinct docs in
                                              # source_document_support; extra
                                              # keys corpus_support_count /
                                              # distinct_document_count record
                                              # the numbers
        "risks_or_likely_misuse": [entry],    # ONLY parents with
                                              # semantic_chunk_type=="warning":
                                              # a flag entry carrying the
                                              # parent_ids — no prose
        "counterbalancing_concepts": [],      # empty in v0

        # Aggregated provenance (id lists, not value entries):
        "evidence_spans": {
            "source_parent_ids": [str],   # from contributing lexicon rows
            "source_chunk_ids": [str],    # from contributing lexicon rows
            "section_ids": [str],         # doc_profile.section_ids
        },
    }

Cards are query-neutral universal facts — no shelf labels, no query-relative
roles.  A document with zero seeds yields ``None`` (degrade, never
fabricate).  The slim routing projection (:func:`slim_card_payload`) is
RETURNED only — this module performs no Qdrant writes.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from services.ingestion.corpus_lexicon import (
    _natural_language_label,
    _useful_target,
    normalize_identity,
)
from services.storage.record_status import with_active_records

logger = logging.getLogger(__name__)

CARD_SCHEMA_VERSION = "librarian_card.v0"
CARD_COLLECTION = "librarian_cards"
BUILDER_VERSION = "librarian_card_builder.v0.1"

# Deterministic per-field caps (house style: every lexicon projection is
# bounded). Generous — routing slims to 8/6/6 anyway.
_MAX_SUBJECTS = 64
_MAX_MECHANISMS = 48
_MAX_LATENT_SUBJECTS = 24
_MAX_CAPABILITIES = 32
_MAX_PROBLEMS = 24
_MAX_PRINCIPLES = 24
_MAX_ENTRY_SOURCE_IDS = 12
_MAX_SPAN_IDS = 256

# Slim routing caps (returned dict only; never written to Qdrant here).
_SLIM_SUBJECTS = 8
_SLIM_MECHANISMS = 6
_SLIM_CAPABILITIES = 6

# Corpus-level thresholds for transferable_principles (spec: support_count
# >= 3 AND >= 2 distinct documents in source_document_support).
_PRINCIPLE_MIN_SUPPORT = 3
_PRINCIPLE_MIN_DOCS = 2

# Functional contextual-usage methods — the exact set _build_utility_gloss
# treats as utility evidence ("Useful for ..." fragments).
_UTILITY_USAGE_METHODS = frozenset({"parent_main_mechanism", "parent_retrieval_use"})

_CARD_ENTRY_FIELDS = (
    "central_subjects",
    "mechanisms_taught",
    "candidate_latent_subjects",
    "capabilities_developed",
    "problems_addressed",
    "transferable_principles",
    "risks_or_likely_misuse",
    "counterbalancing_concepts",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_ids(values: Any) -> list[str]:
    return sorted({str(v) for v in (values or []) if str(v or "").strip()})


class _EntryAccumulator:
    """Merge seed observations per normalized value with rejection counting."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self.rejected = 0

    def add(
        self,
        *,
        value: Any,
        method: str,
        source_ids: Any,
        confidence: float | None,
        support: int = 1,
        extra: dict[str, Any] | None = None,
    ) -> None:
        display = str(value or "").strip()
        key = normalize_identity(display)
        ids = _clean_ids(source_ids)
        if not display or not key:
            return
        if not ids:
            # Build-time rejection: a value without source ids is never
            # allowed onto a card (empty-not-fabricated).
            self.rejected += 1
            return
        row = self._rows.setdefault(
            key,
            {
                "value": display,
                "value_key": key,
                "methods": set(),
                "source_ids": set(),
                "confidences": [],
                "support": 0,
                "extra": {},
            },
        )
        row["methods"].add(str(method))
        row["source_ids"].update(ids)
        if confidence is not None:
            row["confidences"].append(max(0.0, min(1.0, float(confidence))))
        row["support"] += max(1, int(support))
        if extra:
            row["extra"].update(extra)

    def entries(self, *, limit: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in self._rows.values():
            confidences = row["confidences"]
            entry = {
                "value": row["value"],
                "value_key": row["value_key"],
                "method": "+".join(sorted(row["methods"])),
                "source_ids": sorted(row["source_ids"])[:_MAX_ENTRY_SOURCE_IDS],
                "confidence": (
                    round(sum(confidences) / len(confidences), 4)
                    if confidences
                    else 1.0
                ),
                "support": int(row["support"]),
            }
            entry.update(row["extra"])
            out.append(entry)
        out.sort(key=lambda item: (-item["support"], item["value_key"]))
        return out[:limit]


def _doc_tied_source_ids(source_rows: list[dict[str, Any]]) -> list[str]:
    parent_ids = _clean_ids(
        pid for row in source_rows for pid in (row.get("source_parent_ids") or [])
    )
    if parent_ids:
        return parent_ids
    return _clean_ids(
        cid for row in source_rows for cid in (row.get("source_chunk_ids") or [])
    )


def _lexicon_entry_keys(entry: dict[str, Any]) -> set[str]:
    keys = {str(entry.get("canonical_key") or "")}
    keys.update(str(k) for k in (entry.get("member_keys") or []))
    return {k for k in keys if k}


def _doc_support(entry: dict[str, Any], doc_id: str) -> int:
    for item in entry.get("source_document_support") or []:
        if str(item.get("doc_id") or "") == doc_id:
            return max(1, int(item.get("support_count") or 1))
    return 1


async def build_librarian_card(
    db, *, corpus_id: str, doc_id: str
) -> dict | None:
    """Build one deterministic ``librarian_card.v0`` for ``(corpus_id, doc_id)``.

    Reads existing projections only (no LLM calls, no writes). Returns the
    card dict, or ``None`` when the document has zero seeds — missing cards
    degrade to the existing Tier-0 document path, never to fabricated prose.
    """

    doc = await db["documents"].find_one(
        with_active_records({"doc_id": doc_id, "corpus_id": corpus_id}),
        {"_id": 0, "doc_id": 1, "doc_profile": 1},
    )
    if not doc:
        return None
    profile = doc.get("doc_profile") or {}
    profile_source_ids = _clean_ids(
        [profile.get("summary_id"), *(profile.get("section_ids") or [])]
    )

    source_rows = (
        await db["corpus_lexicon_sources"]
        .find(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {
                "_id": 0,
                "canonical_key": 1,
                "canonical_keys": 1,
                "canonical_names": 1,
                "definitions": 1,
                "application_contexts": 1,
                "contextual_usages": 1,
                "source_chunk_ids": 1,
                "source_parent_ids": 1,
                "support_count": 1,
                "mean_confidence": 1,
            },
        )
        .to_list(length=None)
    )
    # Deterministic ordering independent of Mongo natural order.
    source_rows.sort(key=lambda row: str(row.get("canonical_key") or ""))
    source_rows_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source_rows:
        for key in {
            str(row.get("canonical_key") or ""),
            *(str(k) for k in (row.get("canonical_keys") or [])),
        }:
            if key:
                source_rows_by_key[key].append(row)

    lexicon_entries = (
        await db["corpus_lexicon"]
        .find(
            {"corpus_id": corpus_id, "source_document_ids": doc_id},
            {
                "_id": 0,
                "lexicon_id": 1,
                "canonical_name": 1,
                "canonical_key": 1,
                "member_keys": 1,
                "support_count": 1,
                "mean_confidence": 1,
                "source_document_ids": 1,
                "source_document_support": 1,
                "retrieval_eligible": 1,
            },
        )
        .to_list(length=None)
    )
    # Trustworthy-projection gate: entries the lexicon itself flagged as
    # not retrieval eligible (junk identities) never seed a card. Missing
    # flag (legacy rows) counts as eligible.
    lexicon_entries = sorted(
        (
            entry
            for entry in lexicon_entries
            if entry.get("retrieval_eligible") is not False
        ),
        key=lambda entry: str(entry.get("canonical_key") or ""),
    )

    ghost_rows = (
        await db["ghost_b_extractions"]
        .find(
            {"corpus_id": corpus_id, "doc_id": doc_id, "status": "ok"},
            {"_id": 0, "chunk_id": 1, "entities": 1},
        )
        .to_list(length=None)
    )

    parent_rows = (
        await db["parent_chunks"]
        .find(
            with_active_records({"corpus_id": corpus_id, "doc_id": doc_id}),
            {
                "_id": 0,
                "parent_id": 1,
                "mechanisms": 1,
                "main_mechanism": 1,
                "semantic_chunk_type": 1,
                "latent_concepts": 1,
                "quality_score": 1,
                "validation_status": 1,
            },
        )
        .to_list(length=None)
    )

    # ── central_subjects ────────────────────────────────────────────────
    subjects = _EntryAccumulator()
    for entry in lexicon_entries:
        rows = [
            row
            for key in _lexicon_entry_keys(entry)
            for row in source_rows_by_key.get(key, [])
        ]
        source_ids = _doc_tied_source_ids(rows)
        if not source_ids and entry.get("lexicon_id"):
            source_ids = [f"lexicon:{entry['lexicon_id']}"]
        subjects.add(
            value=entry.get("canonical_name"),
            method="lexicon_canonical_term",
            source_ids=source_ids,
            confidence=entry.get("mean_confidence"),
            support=_doc_support(entry, doc_id),
        )
    for concept in profile.get("concepts") or []:
        subjects.add(
            value=concept,
            method="doc_profile_concept",
            source_ids=profile_source_ids,
            confidence=None,
        )
    ghost_names: dict[str, dict[str, Any]] = {}
    ghost_rows.sort(key=lambda row: str(row.get("chunk_id") or ""))
    for row in ghost_rows:
        chunk_id = str(row.get("chunk_id") or "")
        for entity in row.get("entities") or []:
            if not isinstance(entity, dict):
                continue
            name = str(
                entity.get("canonical_name") or entity.get("surface_form") or ""
            ).strip()
            key = normalize_identity(name)
            if not name or not key:
                continue
            acc = ghost_names.setdefault(
                key, {"name": name, "chunk_ids": set(), "confidences": [], "count": 0}
            )
            acc["count"] += 1
            if chunk_id:
                acc["chunk_ids"].add(chunk_id)
            if entity.get("confidence") is not None:
                acc["confidences"].append(float(entity.get("confidence") or 0.0))
    for key in sorted(ghost_names):
        acc = ghost_names[key]
        confidences = acc["confidences"]
        subjects.add(
            value=acc["name"],
            method="ghost_b_entity",
            source_ids=sorted(acc["chunk_ids"]),
            confidence=(
                round(sum(confidences) / len(confidences), 4) if confidences else None
            ),
            support=int(acc["count"]),
        )

    # ── mechanisms_taught / candidate_latent_subjects / risks ───────────
    mechanisms = _EntryAccumulator()
    latent = _EntryAccumulator()
    warning_parent_ids: list[str] = []
    for row in sorted(parent_rows, key=lambda r: str(r.get("parent_id") or "")):
        parent_id = str(row.get("parent_id") or "")
        valid = str(row.get("validation_status") or "valid") == "valid"
        quality = row.get("quality_score")
        confidence = (
            max(0.0, min(1.0, float(quality))) if quality is not None else None
        )
        if valid:
            for mech in row.get("mechanisms") or []:
                mechanisms.add(
                    value=mech,
                    method="parent_mechanisms",
                    source_ids=[parent_id],
                    confidence=confidence,
                )
            if str(row.get("main_mechanism") or "").strip():
                mechanisms.add(
                    value=str(row.get("main_mechanism")).strip(),
                    method="parent_main_mechanism",
                    source_ids=[parent_id],
                    confidence=confidence,
                )
        for item in row.get("latent_concepts") or []:
            # LLM-generated at ingest — kept in the SEPARATE candidate field
            # and only when the artifact itself claims direct evidence.
            if not isinstance(item, dict):
                continue
            if str(item.get("evidence_basis") or "") != "direct":
                continue
            latent.add(
                value=item.get("concept"),
                method="parent_latent_concept_direct",
                source_ids=[parent_id],
                confidence=item.get("confidence"),
            )
        if str(row.get("semantic_chunk_type") or "") == "warning" and parent_id:
            warning_parent_ids.append(parent_id)

    risks: list[dict[str, Any]] = []
    if warning_parent_ids:
        risks.append(
            {
                "value": "warning_chunks_present",
                "value_key": "warning chunks present",
                "method": "parent_semantic_chunk_type_warning",
                "source_ids": sorted(set(warning_parent_ids)),
                "confidence": 1.0,
                "support": len(set(warning_parent_ids)),
            }
        )

    # ── capabilities_developed ──────────────────────────────────────────
    capabilities = _EntryAccumulator()
    for row in source_rows:
        for item in row.get("application_contexts") or []:
            target = _useful_target(item.get("target"))
            predicate = _natural_language_label(item.get("predicate"))
            if not target or not predicate:
                continue
            capabilities.add(
                value=f"{predicate} {target}",
                method="lexicon_application_context",
                source_ids=[item.get("chunk_id"), item.get("parent_id")],
                confidence=item.get("confidence"),
            )
        for item in row.get("contextual_usages") or []:
            if str(item.get("method") or "") not in _UTILITY_USAGE_METHODS:
                continue
            text = _natural_language_label(item.get("text"))
            if not text:
                continue
            capabilities.add(
                value=text,
                method="lexicon_utility_fragment",
                source_ids=[item.get("chunk_id"), item.get("parent_id")],
                confidence=item.get("confidence"),
            )

    # ── problems_addressed — ONLY definitional evidence ─────────────────
    problems = _EntryAccumulator()
    for row in source_rows:
        names = row.get("canonical_names") or []
        subject = str((names[0].get("value") if names else "") or "").strip()
        for item in row.get("definitions") or []:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            problems.add(
                value=text,
                method=(
                    "lexicon_definition:"
                    + str(item.get("method") or "unknown")
                ),
                source_ids=[item.get("chunk_id"), item.get("parent_id")],
                confidence=item.get("confidence"),
                extra={"subject": subject} if subject else None,
            )

    # ── transferable_principles ─────────────────────────────────────────
    principles = _EntryAccumulator()
    for entry in lexicon_entries:
        support_count = int(entry.get("support_count") or 0)
        distinct_docs = len(
            {
                str(item.get("doc_id") or "")
                for item in (entry.get("source_document_support") or [])
                if str(item.get("doc_id") or "")
            }
        )
        if support_count < _PRINCIPLE_MIN_SUPPORT or distinct_docs < _PRINCIPLE_MIN_DOCS:
            continue
        rows = [
            row
            for key in _lexicon_entry_keys(entry)
            for row in source_rows_by_key.get(key, [])
        ]
        source_ids = _doc_tied_source_ids(rows)
        if not source_ids and entry.get("lexicon_id"):
            source_ids = [f"lexicon:{entry['lexicon_id']}"]
        principles.add(
            value=entry.get("canonical_name"),
            method="lexicon_multi_doc_support",
            source_ids=source_ids,
            confidence=entry.get("mean_confidence"),
            support=support_count,
            extra={
                "corpus_support_count": support_count,
                "distinct_document_count": distinct_docs,
            },
        )

    # ── evidence_spans ──────────────────────────────────────────────────
    evidence_spans = {
        "source_parent_ids": _clean_ids(
            pid for row in source_rows for pid in (row.get("source_parent_ids") or [])
        )[:_MAX_SPAN_IDS],
        "source_chunk_ids": _clean_ids(
            cid for row in source_rows for cid in (row.get("source_chunk_ids") or [])
        )[:_MAX_SPAN_IDS],
        "section_ids": _clean_ids(profile.get("section_ids"))[:_MAX_SPAN_IDS],
    }

    card = {
        "schema_version": CARD_SCHEMA_VERSION,
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "central_subjects": subjects.entries(limit=_MAX_SUBJECTS),
        "mechanisms_taught": mechanisms.entries(limit=_MAX_MECHANISMS),
        "candidate_latent_subjects": latent.entries(limit=_MAX_LATENT_SUBJECTS),
        "capabilities_developed": capabilities.entries(limit=_MAX_CAPABILITIES),
        "problems_addressed": problems.entries(limit=_MAX_PROBLEMS),
        "transferable_principles": principles.entries(limit=_MAX_PRINCIPLES),
        "risks_or_likely_misuse": risks,
        "counterbalancing_concepts": [],
        "evidence_spans": evidence_spans,
        "built_at": _utcnow(),
        "builder_version": BUILDER_VERSION,
        "rejected_value_count": subjects.rejected
        + mechanisms.rejected
        + latent.rejected
        + capabilities.rejected
        + problems.rejected
        + principles.rejected,
    }
    if not any(card[field] for field in _CARD_ENTRY_FIELDS):
        # Zero seeds — degrade to the existing document path, never fabricate.
        return None
    return card


def slim_card_payload(card: dict) -> dict:
    """Return the slim routing projection for a card (flat value lists).

    Returned only — this function never writes to Qdrant.
    """

    def values(field: str, limit: int) -> list[str]:
        return [str(entry.get("value")) for entry in (card.get(field) or [])][:limit]

    return {
        "schema_version": str(card.get("schema_version") or CARD_SCHEMA_VERSION),
        "corpus_id": str(card.get("corpus_id") or ""),
        "doc_id": str(card.get("doc_id") or ""),
        "subjects": values("central_subjects", _SLIM_SUBJECTS),
        "mechanisms": values("mechanisms_taught", _SLIM_MECHANISMS),
        "capabilities": values("capabilities_developed", _SLIM_CAPABILITIES),
    }


def _field_coverage(cards: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    coverage = {
        field: {"documents_with_values": 0, "total_values": 0}
        for field in _CARD_ENTRY_FIELDS
    }
    for card in cards:
        for field in _CARD_ENTRY_FIELDS:
            entries = card.get(field) or []
            if entries:
                coverage[field]["documents_with_values"] += 1
                coverage[field]["total_values"] += len(entries)
    return coverage


async def build_corpus_cards(
    db, *, corpus_id: str, limit: int | None = None
) -> dict:
    """Build + upsert cards for every active document of a corpus.

    Upserts each card into ``librarian_cards`` on ``(corpus_id, doc_id)``
    and returns counts plus per-field coverage. Documents with zero seeds
    are skipped (no card written) and counted.
    """

    doc_rows = (
        await db["documents"]
        .find(
            with_active_records({"corpus_id": corpus_id}),
            {"_id": 0, "doc_id": 1},
        )
        .to_list(length=None)
    )
    doc_ids = sorted({str(row.get("doc_id") or "") for row in doc_rows if row.get("doc_id")})
    if limit is not None:
        doc_ids = doc_ids[: max(0, int(limit))]

    cards: list[dict[str, Any]] = []
    skipped = 0
    rejected_values = 0
    for doc_id in doc_ids:
        card = await build_librarian_card(db, corpus_id=corpus_id, doc_id=doc_id)
        if card is None:
            skipped += 1
            continue
        rejected_values += int(card.get("rejected_value_count") or 0)
        await db[CARD_COLLECTION].update_one(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {"$set": card, "$setOnInsert": {"created_at": _utcnow()}},
            upsert=True,
        )
        cards.append(card)

    return {
        "corpus_id": corpus_id,
        "documents_scanned": len(doc_ids),
        "cards_built": len(cards),
        "cards_skipped_zero_seed": skipped,
        "values_rejected_missing_source_ids": rejected_values,
        "field_coverage": _field_coverage(cards),
    }
