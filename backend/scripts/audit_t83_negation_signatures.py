#!/usr/bin/env python3
"""Read-only aggregate census for T8.3 negation and legacy signatures.

The audit compares the pre-T8.3 evidence validator with the current validator
on already-stored extraction relations, and measures existing domain/range
would-violations/remaps.  It prints aggregate JSON only: no chunk text,
evidence phrase, entity name, credential, or artifact identifier is emitted.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ghost_b import (
    DOMAIN_RANGE_MAP,
    _EVIDENCE_STOPWORDS,
    _EVIDENCE_TOKEN_RE,
    _entity_key,
    _normalize_evidence,
    _validate_evidence,
)


SCHEMA_VERSION = "t83_negation_signature_census.v1"
_LEGACY_STOPWORDS = _EVIDENCE_STOPWORDS | {"not", "no", "never"}


class AuditError(RuntimeError):
    pass


def _legacy_evidence_token_overlap(
    phrase: str, text: str, *, threshold: float = 0.6
) -> bool:
    phrase_tokens = {
        token
        for token in _EVIDENCE_TOKEN_RE.findall(phrase or "")
        if token not in _LEGACY_STOPWORDS
    }
    if not phrase_tokens:
        return False
    text_tokens = set(_EVIDENCE_TOKEN_RE.findall(text or ""))
    effective_threshold = max(threshold, 0.8) if len(phrase_tokens) <= 3 else threshold
    return (
        len(phrase_tokens & text_tokens) / len(phrase_tokens)
    ) >= effective_threshold


def _legacy_validate_evidence(evidence_phrase: str | None, chunk_text: str) -> bool:
    phrase = _normalize_evidence(evidence_phrase or "")
    if not phrase:
        return False
    text = _normalize_evidence(chunk_text)
    if phrase in text:
        return True
    return _legacy_evidence_token_overlap(phrase, text)


def _new_counts() -> dict[str, int]:
    return {
        "extraction_rows": 0,
        "entities": 0,
        "facts": 0,
        "relations": 0,
        "evidence_pairs": 0,
        "missing_evidence_phrase": 0,
        "missing_chunk_text": 0,
        "legacy_accept": 0,
        "current_accept": 0,
        "legacy_accept_current_reject": 0,
        "legacy_reject_current_accept": 0,
        "signature_assessable": 0,
        "signature_would_violate": 0,
        "stored_domain_range_mismatch_relations": 0,
        "stored_domain_range_warn_relations": 0,
        "row_counter_domain_range_remap": 0,
        "row_counter_domain_range_warn": 0,
    }


def _new_evidence_counts() -> dict[str, int]:
    return {
        "evidence_pairs": 0,
        "missing_evidence_phrase": 0,
        "missing_chunk_text": 0,
        "legacy_accept": 0,
        "current_accept": 0,
        "legacy_accept_current_reject": 0,
        "legacy_reject_current_accept": 0,
    }


def _run_key(row: dict[str, Any]) -> str:
    values = (
        row.get("provider"),
        row.get("model"),
        row.get("engine"),
        row.get("schema_version"),
        row.get("schema_mode"),
    )
    return "|".join(str(value or "unknown") for value in values)


def _increment_decisions(counts: dict[str, int], *, phrase: str, text: str) -> None:
    if not phrase.strip():
        counts["missing_evidence_phrase"] += 1
        return
    if not text.strip():
        counts["missing_chunk_text"] += 1
        return
    counts["evidence_pairs"] += 1
    legacy = _legacy_validate_evidence(phrase, text)
    current = _validate_evidence(phrase, text)
    counts["legacy_accept"] += int(legacy)
    counts["current_accept"] += int(current)
    counts["legacy_accept_current_reject"] += int(legacy and not current)
    counts["legacy_reject_current_accept"] += int(not legacy and current)


def _increment_signature(
    counts: dict[str, int], *, relation: dict[str, Any], entities: dict[str, str]
) -> None:
    predicate = str(relation.get("predicate") or "")
    status = str(relation.get("validation_status") or "")
    source_predicate = str(relation.get("source_predicate") or "")
    if "domain_range_mismatch" in status:
        counts["stored_domain_range_mismatch_relations"] += 1
        if source_predicate:
            predicate = source_predicate
    if "domain_range_warn" in status:
        counts["stored_domain_range_warn_relations"] += 1
    constraints = DOMAIN_RANGE_MAP.get(predicate)
    if constraints is None or str(relation.get("object_kind") or "") != "entity":
        return
    counts["signature_assessable"] += 1
    subject_type = entities.get(_entity_key(str(relation.get("subject") or "")))
    object_type = entities.get(_entity_key(str(relation.get("object") or "")))
    if subject_type not in constraints.get(
        "subject_types", []
    ) or object_type not in constraints.get("object_types", []):
        counts["signature_would_violate"] += 1


def _merge(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] += value


def _rates(counts: dict[str, int]) -> dict[str, Any]:
    pairs = counts["evidence_pairs"]
    assessable = counts["signature_assessable"]
    result: dict[str, Any] = dict(counts)
    result["evidence_flip_rate"] = (
        (
            counts["legacy_accept_current_reject"]
            + counts["legacy_reject_current_accept"]
        )
        / pairs
        if pairs
        else 0.0
    )
    result["signature_would_violate_rate"] = (
        counts["signature_would_violate"] / assessable if assessable else 0.0
    )
    return result


def _evidence_rates(counts: dict[str, int]) -> dict[str, Any]:
    pairs = counts["evidence_pairs"]
    result: dict[str, Any] = dict(counts)
    result["evidence_flip_rate"] = (
        (
            counts["legacy_accept_current_reject"]
            + counts["legacy_reject_current_accept"]
        )
        / pairs
        if pairs
        else 0.0
    )
    return result


async def _corpus_census(db: Any, *, corpus_name: str) -> dict[str, Any]:
    corpora = (
        await db["corpora"]
        .find(
            {"name": corpus_name, "status": {"$ne": "deleted"}},
            {"_id": 0, "corpus_id": 1, "name": 1},
        )
        .to_list(length=3)
    )
    if len(corpora) != 1:
        raise AuditError(
            f"expected one active corpus named {corpus_name!r}, found {len(corpora)}"
        )
    corpus_id = str(corpora[0].get("corpus_id") or "")
    rows = (
        await db["ghost_b_extractions"]
        .find(
            {"corpus_id": corpus_id, "status": "ok"},
            {
                "_id": 0,
                "chunk_id": 1,
                "entities": 1,
                "facts": 1,
                "relations": 1,
                "provider": 1,
                "model": 1,
                "engine": 1,
                "schema_version": 1,
                "schema_mode": 1,
                "domain_range_remap_count": 1,
                "domain_range_warn_count": 1,
            },
        )
        .to_list(length=None)
    )
    chunk_ids = [str(row.get("chunk_id") or "") for row in rows]
    texts = {
        str(row.get("chunk_id") or ""): str(row.get("text") or "")
        for row in await db["chunks"]
        .find(
            {"corpus_id": corpus_id, "chunk_id": {"$in": chunk_ids}},
            {"_id": 0, "chunk_id": 1, "text": 1},
        )
        .to_list(length=None)
    }

    total = _new_counts()
    by_run: dict[str, dict[str, int]] = defaultdict(_new_counts)
    gate_families = {
        "entity": _new_evidence_counts(),
        "fact": _new_evidence_counts(),
        "relation": _new_evidence_counts(),
    }
    by_run_families: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: {
            "entity": _new_evidence_counts(),
            "fact": _new_evidence_counts(),
            "relation": _new_evidence_counts(),
        }
    )
    for row in rows:
        run_key = _run_key(row)
        run_counts = by_run[run_key]
        run_gate_families = by_run_families[run_key]
        total["extraction_rows"] += 1
        run_counts["extraction_rows"] += 1
        remaps = int(row.get("domain_range_remap_count") or 0)
        warnings = int(row.get("domain_range_warn_count") or 0)
        total["row_counter_domain_range_remap"] += remaps
        total["row_counter_domain_range_warn"] += warnings
        run_counts["row_counter_domain_range_remap"] += remaps
        run_counts["row_counter_domain_range_warn"] += warnings
        entities = {
            _entity_key(str(entity.get("canonical_name") or "")): str(
                entity.get("entity_type") or ""
            )
            for entity in row.get("entities") or []
            if entity.get("canonical_name")
        }
        text = texts.get(str(row.get("chunk_id") or ""), "")
        for entity in row.get("entities") or []:
            total["entities"] += 1
            run_counts["entities"] += 1
            surface = str(
                entity.get("surface_form") or entity.get("canonical_name") or ""
            )
            for counts in (
                total,
                run_counts,
                gate_families["entity"],
                run_gate_families["entity"],
            ):
                _increment_decisions(counts, phrase=surface, text=text)
        for fact in row.get("facts") or []:
            total["facts"] += 1
            run_counts["facts"] += 1
            phrase = str(fact.get("evidence_phrase") or "")
            for counts in (
                total,
                run_counts,
                gate_families["fact"],
                run_gate_families["fact"],
            ):
                _increment_decisions(counts, phrase=phrase, text=text)
        for relation in row.get("relations") or []:
            total["relations"] += 1
            run_counts["relations"] += 1
            phrase = str(relation.get("evidence_phrase") or "")
            for counts in (
                total,
                run_counts,
                gate_families["relation"],
                run_gate_families["relation"],
            ):
                _increment_decisions(counts, phrase=phrase, text=text)
            _increment_signature(total, relation=relation, entities=entities)
            _increment_signature(run_counts, relation=relation, entities=entities)

    return {
        "corpus_name": corpus_name,
        "corpus_id": corpus_id,
        "aggregate": _rates(total),
        "gate_families": {
            key: _evidence_rates(gate_families[key]) for key in sorted(gate_families)
        },
        "by_run": [
            {
                "run_identity": key,
                **_rates(by_run[key]),
                "gate_families": {
                    family: _evidence_rates(by_run_families[key][family])
                    for family in sorted(by_run_families[key])
                },
            }
            for key in sorted(by_run)
        ],
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = client.get_default_database()
        except Exception:
            db = client[settings.MONGODB_DATABASE]
        corpora = [
            await _corpus_census(db, corpus_name=name) for name in args.corpus_name
        ]
    finally:
        client.close()

    total = _new_counts()
    gate_families = {
        "entity": _new_evidence_counts(),
        "fact": _new_evidence_counts(),
        "relation": _new_evidence_counts(),
    }
    for corpus in corpora:
        aggregate = corpus["aggregate"]
        _merge(total, {key: int(aggregate[key]) for key in _new_counts()})
        for family in gate_families:
            source = corpus["gate_families"][family]
            _merge(
                gate_families[family],
                {key: int(source[key]) for key in _new_evidence_counts()},
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "writes": 0,
        "provider_calls": 0,
        "raw_text_in_output": False,
        "artifact_ids_in_output": False,
        "legacy_evidence_policy": "whole_chunk_overlap_drops_not_no_never",
        "current_evidence_policy": ("whole_chunk_overlap_preserves_not_no_never"),
        "domain_range_source": "services.ghost_b.DOMAIN_RANGE_MAP",
        "corpora": corpora,
        "aggregate": _rates(total),
        "gate_families": {
            key: _evidence_rates(gate_families[key]) for key in sorted(gate_families)
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus-name", action="append", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except AuditError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
