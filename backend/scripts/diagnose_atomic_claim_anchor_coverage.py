#!/usr/bin/env python3
"""Offline replay of claim-anchor eligibility over persisted selected sources.

This script makes no API/model calls and performs no writes. It locates the
assistant source packets persisted by one completed held-out Graph artifact,
then replays only the deterministic claim-anchor attachment and provenance
checks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from models.schemas import SourceChunk
from models.semantic_digest_claim_input import (
    COMPILATION_COLLECTION,
    parse_materialized_row_document,
)
from services.conversation import conversation_service
from services.ingestion.semantic_digest_claim_inputs import document_source_version_id
from services.retriever.atomic_claim_anchors import attach_atomic_claim_anchors
from services.storage.mongo_contracts import restore_bson_utc_awareness

DEFAULT_IDS = tuple(f"q{number:03d}" for number in range(21, 30))


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _effective_tier(message: dict[str, Any]) -> str:
    for trace in message.get("trace_events") or []:
        if not isinstance(trace, dict):
            continue
        metadata = trace.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        direct = str(metadata.get("effective_tier") or "")
        if direct:
            return direct
        retrieval = metadata.get("retrieval_diagnostics") or {}
        if isinstance(retrieval, dict) and retrieval.get("effective_tier"):
            return str(retrieval["effective_tier"])
    return ""


async def _persisted_sources(
    db: Any,
    *,
    question: str,
    not_before: datetime,
    not_after: datetime,
) -> tuple[list[SourceChunk], dict[str, str]]:
    user_rows = await db["messages"].find(
        {
            "role": "user",
            "content": question,
            "created_at": {"$gte": not_before, "$lte": not_after},
        },
        {"conversation_id": 1, "created_at": 1},
    ).sort("created_at", -1).to_list(length=None)
    for user_row in user_rows:
        created_at = _utc(user_row["created_at"])
        assistants = await db["messages"].find(
            {
                "role": "assistant",
                "conversation_id": user_row["conversation_id"],
                "created_at": {
                    "$gte": created_at,
                    "$lte": not_after + timedelta(seconds=10),
                },
            },
            {
                "created_at": 1,
                "sources": 1,
                "trace_events": 1,
            },
        ).sort("created_at", 1).to_list(length=None)
        for assistant in assistants:
            tier = _effective_tier(assistant)
            if tier not in {"qdrant_mongo_graph", "RetrievalTier.qdrant_mongo_graph"}:
                continue
            sources: list[SourceChunk] = []
            for raw in assistant.get("sources") or []:
                try:
                    sources.append(SourceChunk.model_validate(raw))
                except Exception:
                    continue
            if sources:
                return sources, {
                    "conversation_id": str(user_row["conversation_id"]),
                    "user_created_at": created_at.isoformat(),
                    "assistant_created_at": _utc(assistant["created_at"]).isoformat(),
                    "effective_tier": tier,
                }
    raise RuntimeError(f"persisted Graph sources not found for question: {question}")


async def _validate_anchor(
    db: Any,
    *,
    source: SourceChunk,
    anchor: dict[str, Any],
) -> dict[str, bool]:
    key = {
        "corpus_id": str(source.corpus_id or ""),
        "document_id": str(source.doc_id or ""),
        "child_id": str(anchor.get("child_id") or ""),
    }
    row_raw = await db[COMPILATION_COLLECTION].find_one(key)
    child = await db["chunks"].find_one(
        {
            "corpus_id": key["corpus_id"],
            "doc_id": key["document_id"],
            "chunk_id": key["child_id"],
        }
    )
    document = await db["documents"].find_one(
        {"corpus_id": key["corpus_id"], "doc_id": key["document_id"]}
    )
    if not row_raw or not child or not document:
        return {
            "selected_source_ownership": False,
            "exact_span": False,
            "provenance_closure": False,
            "valid": False,
        }

    row = parse_materialized_row_document(
        restore_bson_utc_awareness(row_raw)
    )
    evidence = {
        item.evidence_ref_id: item for item in row.evidence_refs
    }.get(str(anchor.get("evidence_ref_id") or ""))
    claim = {
        item.claim_id: item for item in row.envelope.body.claims
    }.get(str(anchor.get("claim_id") or ""))
    start = int(anchor.get("start") or 0)
    end = int(anchor.get("end") or 0)
    exact_sentence = str(anchor.get("exact_sentence") or "")
    selected_source_ownership = bool(
        str(source.chunk_id or "") == key["child_id"]
        and row.corpus_id == key["corpus_id"]
        and row.document_id == key["document_id"]
        and row.child_id == key["child_id"]
    )
    exact_span = bool(
        evidence is not None
        and str(child.get("text") or "")[start:end] == exact_sentence
        and evidence.quote == exact_sentence
        and evidence.start == start
        and evidence.end == end
    )
    provenance_closure = bool(
        claim is not None
        and claim.canonical_proposition == str(anchor.get("claim_text") or "")
        and row.source_version_id == str(anchor.get("source_version_id") or "")
        and row.envelope.artifact_revision_id
        == str(anchor.get("compilation_revision_id") or "")
        and row.source_version_id == document_source_version_id(document)
    )
    return {
        "selected_source_ownership": selected_source_ownership,
        "exact_span": exact_span,
        "provenance_closure": provenance_closure,
        "valid": (
            selected_source_ownership and exact_span and provenance_closure
        ),
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    baseline = json.loads(Path(args.baseline).read_text())
    if baseline.get("summary", {}).get("tier") != "qdrant_mongo_graph":
        raise RuntimeError("baseline is not the completed Graph arm")
    captured_at = datetime.fromisoformat(
        str(baseline["captured_at"]).replace("Z", "+00:00")
    )
    not_before = captured_at - timedelta(minutes=20)
    requested = set(args.ids)
    question_rows = [
        json.loads(line)
        for line in Path(args.questions).read_text().splitlines()
        if line.strip()
    ]
    question_rows = [row for row in question_rows if row.get("id") in requested]
    if {row["id"] for row in question_rows} != requested:
        raise RuntimeError("one or more requested question IDs are absent")

    await conversation_service.connect()
    db = conversation_service._db
    results: list[dict[str, Any]] = []
    try:
        for question_row in question_rows:
            sources, persisted = await _persisted_sources(
                db,
                question=question_row["question"],
                not_before=not_before,
                not_after=captured_at,
            )
            enriched, diagnostics = await attach_atomic_claim_anchors(
                db,
                sources,
                query=question_row["question"],
                per_source=2,
                total=8,
            )
            checks: list[dict[str, bool]] = []
            per_source: list[dict[str, Any]] = []
            for source in enriched:
                anchors = list(
                    (source.metadata or {}).get("atomic_claim_anchors") or []
                )
                source_valid = 0
                for anchor in anchors:
                    check = await _validate_anchor(
                        db,
                        source=source,
                        anchor=anchor,
                    )
                    checks.append(check)
                    source_valid += int(check["valid"])
                per_source.append(
                    {
                        "corpus_id": str(source.corpus_id or ""),
                        "doc_id": str(source.doc_id or ""),
                        "chunk_id": str(source.chunk_id or ""),
                        "eligible_anchor_count": len(anchors),
                        "valid_anchor_count": source_valid,
                    }
                )
            anchor_count = len(checks)
            valid_count = sum(int(check["valid"]) for check in checks)
            results.append(
                {
                    "question_id": question_row["id"],
                    "persisted_source_receipt": persisted,
                    "selected_source_count": len(sources),
                    "eligible_anchor_count": anchor_count,
                    "valid_anchor_count": valid_count,
                    "structural_sentence_anchor_precision": (
                        valid_count / anchor_count if anchor_count else None
                    ),
                    "all_selected_source_ownership_valid": all(
                        check["selected_source_ownership"] for check in checks
                    ) if checks else None,
                    "all_exact_spans_valid": all(
                        check["exact_span"] for check in checks
                    ) if checks else None,
                    "all_provenance_closed": all(
                        check["provenance_closure"] for check in checks
                    ) if checks else None,
                    "diagnostics": diagnostics,
                    "sources": per_source,
                }
            )
    finally:
        await conversation_service.disconnect()

    total_anchors = sum(row["eligible_anchor_count"] for row in results)
    total_valid = sum(row["valid_anchor_count"] for row in results)
    positive_questions = [
        row["question_id"] for row in results if row["eligible_anchor_count"] > 0
    ]
    return {
        "schema_version": "atomic_claim_anchor_offline_replay.v1",
        "mode": "read_only_no_api_no_model",
        "baseline": {
            "path": str(args.baseline),
            "captured_at": baseline["captured_at"],
            "out_suffix": baseline.get("run", {}).get("out_suffix"),
        },
        "summary": {
            "question_count": len(results),
            "questions_with_eligible_anchors": len(positive_questions),
            "positive_question_ids": positive_questions,
            "eligible_anchor_count": total_anchors,
            "valid_anchor_count": total_valid,
            "structural_sentence_anchor_precision": (
                total_valid / total_anchors if total_anchors else None
            ),
            "q021_isolated_zero": bool(
                results
                and next(
                    row for row in results if row["question_id"] == "q021"
                )["eligible_anchor_count"] == 0
                and any(
                    row["eligible_anchor_count"] > 0
                    for row in results
                    if row["question_id"] != "q021"
                )
            ),
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--ids", nargs="+", default=list(DEFAULT_IDS))
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    output = Path(args.out)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result["summary"], sort_keys=True))
    print(f"WROTE {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
