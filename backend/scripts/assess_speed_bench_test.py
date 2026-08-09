#!/usr/bin/env python3
"""Assess speed_bench_test_20260805: extraction speed + ontology + summary quality.

Runs inside backend container (PYTHONPATH=/app) or host with motor.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

CORPUS = os.environ.get("SPEED_BENCH_CORPUS", "0189427c-6ccf-49e2-82cd-aedea87f4045")
BATCH = os.environ.get("SPEED_BENCH_BATCH", "b0d12dba-11c5-4303-b3c4-ec2bad23b188")
OUT_DIR = Path(
    os.environ.get(
        "SPEED_BENCH_OUT",
        "/Users/king/polymath_v3.3/data_eval/speed_bench_test_20260805",
    )
)

# Soft ontology expected from corpus config
ENTITY_TYPES = {
    "Person",
    "Organization",
    "Location",
    "Event",
    "Concept",
    "Method",
    "Product",
    "Software",
    "Document",
    "Standard",
    "Rule",
    "Law",
    "Artifact",
    "TimeReference",
}
RELATION_PREDICATES = {
    "part_of",
    "member_of",
    "located_in",
    "works_for",
    "created_by",
    "owns",
    "affiliated_with",
    "synonym_of",
    "instance_of",
    "uses",
    "runs_on",
    "trained_on",
    "references",
    "implements",
    "depends_on",
    "produces",
    "stores",
    "detects",
    "supports",
    "defines",
    "represents",
    "maps_to",
    "preceded_by",
    "causes",
    "overlaps",
    "derived_from",
    "contradicts",
    "excepts",
    "overrides",
    "related_to",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ent_type(e: Any) -> str:
    if isinstance(e, dict):
        return str(e.get("type") or e.get("label") or e.get("entity_type") or "").strip()
    return ""


def _ent_text(e: Any) -> str:
    if isinstance(e, dict):
        return str(e.get("text") or e.get("name") or e.get("surface") or "").strip()
    return str(e or "").strip()


def _rel_pred(r: Any) -> str:
    if isinstance(r, dict):
        return str(r.get("predicate") or r.get("type") or r.get("relation") or "").strip()
    return ""


def score_summary(text: str, parent_text: str) -> dict[str, Any]:
    summary = (text or "").strip()
    parent = (parent_text or "").strip()
    flags: list[str] = []
    if not summary:
        return {"ok": False, "flags": ["empty"], "chars": 0, "overlap_ratio": 0.0}
    if len(summary) < 40:
        flags.append("too_short")
    if len(summary) > 2000:
        flags.append("too_long")
    # extractive overlap: share of summary tokens appearing in parent
    tok = re.findall(r"[a-z0-9]+", summary.lower())
    parent_toks = set(re.findall(r"[a-z0-9]+", parent.lower()))
    if tok and parent_toks:
        overlap = sum(1 for t in tok if t in parent_toks) / len(tok)
    else:
        overlap = 0.0
    if overlap < 0.55:
        flags.append("low_extractive_overlap")
    if "topic:" not in summary.lower() and "key points:" not in summary.lower():
        flags.append("missing_deterministic_scaffold")
    # hallucination heuristic: long rare tokens not in parent
    rare = [t for t in tok if len(t) > 8 and t not in parent_toks]
    if len(rare) >= 4:
        flags.append("possible_invented_terms")
    return {
        "ok": not flags or flags == ["missing_deterministic_scaffold"],
        "flags": flags,
        "chars": len(summary),
        "overlap_ratio": round(overlap, 3),
    }


async def main() -> None:
    db = AsyncIOMotorClient(os.environ["MONGODB_URI"]).get_default_database()
    batch = await db.ingest_batches.find_one({"batch_id": BATCH}) or {}
    items = await db.ingest_batch_items.find({"batch_id": BATCH}).to_list(50)
    docs = await db.documents.find({"corpus_id": CORPUS}).to_list(50)
    parents = await db.parent_chunks.find({"corpus_id": CORPUS}).to_list(10000)
    extracts = await db.ghost_b_extractions.find({"corpus_id": CORPUS}).to_list(20000)

    # Speed from item timestamps
    per_file: list[dict[str, Any]] = []
    for it in items:
        started = it.get("started_at")
        completed = it.get("completed_at")
        secs = None
        if started and completed:
            try:
                secs = (completed - started).total_seconds()
            except Exception:
                secs = None
        per_file.append(
            {
                "filename": it.get("filename"),
                "status": it.get("status"),
                "phase": it.get("phase"),
                "attempts": it.get("attempts"),
                "error": it.get("error"),
                "size_bytes": it.get("size_bytes"),
                "started_at": started.isoformat() if hasattr(started, "isoformat") else started,
                "completed_at": completed.isoformat()
                if hasattr(completed, "isoformat")
                else completed,
                "wall_seconds": secs,
            }
        )

    # Summary quality
    summary_rows = []
    model_counts: Counter[str] = Counter()
    schema_counts: Counter[str] = Counter()
    flag_counts: Counter[str] = Counter()
    by_doc_parent = {p.get("parent_id"): p for p in parents}
    for p in parents:
        summary = (p.get("summary") or "").strip()
        if not summary:
            continue
        model_counts[str(p.get("summary_model") or "<none>")] += 1
        schema_counts[str(p.get("schema_version") or "<none>")] += 1
        scored = score_summary(summary, p.get("text") or "")
        for f in scored["flags"]:
            flag_counts[f] += 1
        summary_rows.append(
            {
                "doc_id": p.get("doc_id"),
                "parent_id": p.get("parent_id"),
                "summary_model": p.get("summary_model"),
                "schema_version": p.get("schema_version"),
                "summary_type": p.get("summary_type"),
                "validation_status": p.get("validation_status"),
                **scored,
                "summary_prefix": summary[:180].replace("\n", " "),
            }
        )

    # Extraction / ontology
    entity_types: Counter[str] = Counter()
    predicates: Counter[str] = Counter()
    validation: Counter[str] = Counter()
    engines: Counter[str] = Counter()
    entity_total = 0
    relation_total = 0
    empty_entity_rows = 0
    oov_types: Counter[str] = Counter()
    oov_preds: Counter[str] = Counter()
    sample_triples: list[dict[str, Any]] = []
    for row in extracts:
        engines[str(row.get("engine") or row.get("extractor") or "<unset>")] += 1
        ents = row.get("entities") or []
        rels = row.get("relations") or []
        if not ents:
            empty_entity_rows += 1
        entity_total += len(ents)
        relation_total += len(rels)
        for e in ents:
            t = _ent_type(e)
            entity_types[t or "<missing_type>"] += 1
            if t and t not in ENTITY_TYPES and t.title() not in ENTITY_TYPES:
                # case-insensitive membership
                if t.lower() not in {x.lower() for x in ENTITY_TYPES}:
                    oov_types[t] += 1
        for r in rels:
            pred = _rel_pred(r)
            predicates[pred or "<missing_pred>"] += 1
            if pred and pred not in RELATION_PREDICATES:
                oov_preds[pred] += 1
            validation[str((r.get("validation_status") if isinstance(r, dict) else None) or "<none>")] += 1
            if len(sample_triples) < 12 and isinstance(r, dict):
                sample_triples.append(
                    {
                        "subject": r.get("subject"),
                        "predicate": r.get("predicate"),
                        "object": r.get("object"),
                        "confidence": r.get("confidence"),
                        "validation_status": r.get("validation_status"),
                        "evidence": (r.get("evidence_phrase") or "")[:120],
                    }
                )

    # Doc-level write state
    doc_states = []
    for d in docs:
        ws = d.get("write_state") or {}
        gb = d.get("ghost_b_metrics") or {}
        doc_states.append(
            {
                "filename": d.get("filename"),
                "summaries_indexed": ws.get("summaries_indexed"),
                "neo4j_written": ws.get("neo4j_written"),
                "qdrant_written": ws.get("qdrant_written"),
                "extracted_chunks": gb.get("extracted_chunks") if isinstance(gb, dict) else None,
                "engine": gb.get("engine") if isinstance(gb, dict) else None,
                "failed_chunks": gb.get("failed_chunks") if isinstance(gb, dict) else None,
            }
        )

    # Throughput
    finished = [x for x in per_file if x.get("wall_seconds") is not None]
    total_wall = None
    if finished:
        starts = [it.get("started_at") for it in items if it.get("started_at")]
        ends = [it.get("completed_at") for it in items if it.get("completed_at")]
        if starts and ends:
            total_wall = (max(ends) - min(starts)).total_seconds()

    # children for extract rate
    children = await db.chunks.count_documents({"corpus_id": CORPUS}) if False else None
    # collection may be child_chunks
    child_count = await db.child_chunks.count_documents({"corpus_id": CORPUS})
    if child_count == 0:
        child_count = await db.chunks.count_documents({"corpus_id": CORPUS})

    extract_rate = None
    if extracts and total_wall and total_wall > 0:
        extract_rate = round(len(extracts) / total_wall, 3)

    cloud_summaries = sum(
        1
        for m in model_counts
        if m not in {"deterministic:v1", "<none>"} and "deterministic" not in m
    )

    report = {
        "measured_at": _utcnow(),
        "corpus_id": CORPUS,
        "batch_id": BATCH,
        "batch_status": batch.get("status"),
        "batch_counts": batch.get("counts"),
        "ladder": (batch.get("progress") or {}).get("ladder"),
        "speed": {
            "per_file_wall_seconds": per_file,
            "batch_span_seconds": total_wall,
            "files_completed": len(finished),
            "child_chunks": child_count,
            "extraction_rows": len(extracts),
            "extraction_rows_per_sec_batch_span": extract_rate,
        },
        "summaries": {
            "nonempty_parents": len(summary_rows),
            "total_parents": len(parents),
            "models": dict(model_counts),
            "schemas": dict(schema_counts),
            "cloud_provider_models": cloud_summaries,
            "quality_flag_counts": dict(flag_counts),
            "samples": summary_rows[:8],
            "verdict": (
                "PASS_CONTROL"
                if cloud_summaries == 0 and model_counts.get("deterministic:v1", 0) > 0
                else "FAIL_CONTROL"
            ),
        },
        "extractions": {
            "rows": len(extracts),
            "entity_total": entity_total,
            "relation_total": relation_total,
            "empty_entity_rows": empty_entity_rows,
            "engines": dict(engines),
            "entity_type_top": entity_types.most_common(20),
            "predicate_top": predicates.most_common(20),
            "validation_status": dict(validation),
            "oov_entity_types": oov_types.most_common(15),
            "oov_predicates": oov_preds.most_common(15),
            "sample_triples": sample_triples,
            "docs": doc_states,
        },
        "ontology": {
            "schema_strict": "soft",
            "allowed_entity_types": sorted(ENTITY_TYPES),
            "allowed_predicates": sorted(RELATION_PREDICATES),
            "oov_entity_type_rate": round(
                (sum(oov_types.values()) / entity_total) if entity_total else 0.0, 4
            ),
            "oov_predicate_rate": round(
                (sum(oov_preds.values()) / relation_total) if relation_total else 0.0, 4
            ),
            "verdict": (
                "PASS_SOFT"
                if (sum(oov_preds.values()) / relation_total if relation_total else 0) < 0.15
                else "REVIEW"
            ),
        },
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "quality_speed_assessment.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"wrote": str(out_path), "summary": {
        "batch_status": report["batch_status"],
        "batch_span_seconds": total_wall,
        "summary_verdict": report["summaries"]["verdict"],
        "ontology_verdict": report["ontology"]["verdict"],
        "nonempty_summaries": len(summary_rows),
        "extraction_rows": len(extracts),
        "entity_total": entity_total,
        "relation_total": relation_total,
        "cloud_summaries": cloud_summaries,
    }}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
