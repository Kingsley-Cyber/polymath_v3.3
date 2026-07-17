#!/usr/bin/env python3
"""Run the preregistered deterministic hydration-pressure diagnostic.

The runner is read-only and synthesis-free. It executes the immutable six
bridge questions through retrieval with the waterfall OFF, then twice with it
ON. The OFF/ON evidence selection must be identical; only context hydration is
allowed to change.

The senior-preregistered budget is exactly 1,500 tokens/query. A single
750-token fallback is legal only when a saved 1,500-token artifact proves that
every ranked-parent decision was ``full``. No other budget is accepted.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


BRIDGE_PREREG_SHA256 = (
    "6c348cbf852a26e483ee810f6d3776ce1425955acc53ec4aede880f76dedc4b8"
)
SELECTION_SHA256 = "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00"
CORPUS_NAME = "runpod_e2e_15doc_20260715"
PRIMARY_BUDGET_TOKENS = 1500
FALLBACK_BUDGET_TOKENS = 750
DIAGNOSTIC_TIER = "qdrant_mongo_graph"
DIAGNOSTIC_TOP_K = 10
VALID_LEVELS = frozenset({"full", "summary", "skip"})


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
    )
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_hashed_json(path: Path, expected_hash: str, label: str) -> dict[str, Any]:
    payload = path.read_bytes()
    require(
        hashlib.sha256(payload).hexdigest() == expected_hash,
        f"{label} hash drifted",
    )
    value = json.loads(payload)
    require(isinstance(value, dict), f"{label} must be a JSON object")
    return value


def _validate_fallback_artifact(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    value = json.loads(path.read_text())
    require(
        value.get("schema_version") == "polymath.waterfall_pressure_results.v1",
        "fallback source schema drifted",
    )
    require(
        int((value.get("runtime") or {}).get("budget_tokens") or 0)
        == PRIMARY_BUDGET_TOKENS,
        "fallback source was not the 1,500-token primary run",
    )
    require(
        value.get("preregistration_sha256") == BRIDGE_PREREG_SHA256,
        "fallback source bridge binding drifted",
    )
    require(
        value.get("selection_sha256") == SELECTION_SHA256,
        "fallback source corpus selection drifted",
    )
    summary = value.get("summary") or {}
    require(
        bool(summary.get("fallback_authorized")),
        "fallback source did not authorize the one preregistered halving",
    )
    require(
        bool(summary.get("all_ranked_parent_decisions_full")),
        "fallback source was not all-full",
    )
    require(
        int(summary.get("summary_decisions") or 0) == 0
        and int(summary.get("skip_decisions") or 0) == 0,
        "fallback source already exercised a lower hydration tier",
    )
    results = list(value.get("results") or [])
    require(len(results) == 6, "fallback source did not close all six bridge queries")
    decisions = [
        decision
        for result in results
        for decision in result.get("hydration_decisions") or []
    ]
    require(decisions, "fallback source contains no ranked-parent decisions")
    require(
        all(
            str(decision.get("hydration_level") or "") == "full"
            for decision in decisions
        ),
        "fallback source per-parent decisions are not all full",
    )
    require(
        int(summary.get("full_decisions") or 0) == len(decisions),
        "fallback source decision totals are internally inconsistent",
    )
    return value


def validate_budget(
    budget_tokens: int,
    fallback_from: Path | None,
) -> dict[str, Any] | None:
    """Engrave the one-time 1,500 -> 750 preregistration."""

    require(
        budget_tokens in {PRIMARY_BUDGET_TOKENS, FALLBACK_BUDGET_TOKENS},
        "budget must be exactly 1,500 or the one authorized 750 fallback",
    )
    if budget_tokens == PRIMARY_BUDGET_TOKENS:
        require(fallback_from is None, "primary run cannot name a fallback source")
        return None
    require(
        fallback_from is not None,
        "750-token fallback requires the saved 1,500-token authorization artifact",
    )
    return _validate_fallback_artifact(fallback_from)


def _chunk_signature(chunks: Sequence[Any]) -> list[dict[str, str]]:
    """Evidence-selection signature; ignores packet-only hydration metadata."""

    return [
        {
            "corpus_id": str(getattr(chunk, "corpus_id", "") or ""),
            "doc_id": str(getattr(chunk, "doc_id", "") or ""),
            "parent_id": str(getattr(chunk, "parent_id", "") or ""),
            "chunk_id": str(getattr(chunk, "chunk_id", "") or ""),
            "text_sha256": hashlib.sha256(
                str(getattr(chunk, "text", "") or "").encode()
            ).hexdigest(),
        }
        for chunk in chunks
    ]


def _signature_sha256(signature: Sequence[dict[str, str]]) -> str:
    return hashlib.sha256(
        json.dumps(
            list(signature),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _top_distinct_titles(
    chunks: Sequence[Any],
    document_names: dict[str, str],
    *,
    limit: int = 3,
) -> list[str]:
    titles: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        doc_id = str(getattr(chunk, "doc_id", "") or "")
        title = str(
            document_names.get(doc_id) or getattr(chunk, "doc_name", "") or doc_id
        )
        if not title or title in seen:
            continue
        seen.add(title)
        titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def score_bridge_titles(case: dict[str, Any], titles: Sequence[str]) -> dict[str, Any]:
    expected = set(str(value) for value in case.get("expected_title_any") or [])
    forbidden = set(
        str(value)
        for value in (
            case.get("forbidden_rank1")
            or ["Blain Brown - Cinematography - Theory and Practice (2016).md"]
        )
    )
    expected_hit = bool(expected & set(titles))
    forbidden_rank1 = bool(titles and titles[0] in forbidden)
    return {
        "top_three_titles": list(titles),
        "expected_hit_top_three": expected_hit,
        "forbidden_rank1": forbidden_rank1,
        "passed": expected_hit and not forbidden_rank1,
    }


def summarize_gate(
    *,
    budget_tokens: int,
    results: Sequence[dict[str, Any]],
    hydration_counts: dict[str, int],
) -> dict[str, Any]:
    total_decisions = sum(int(hydration_counts.get(level, 0)) for level in VALID_LEVELS)
    all_full = (
        total_decisions > 0 and int(hydration_counts.get("full", 0)) == total_decisions
    )
    quality_preserved = all(bool(row.get("quality_preserved")) for row in results)
    hashes_stable = all(bool(row.get("packet_hash_stable")) for row in results)
    levels_recorded = all(bool(row.get("hydration_levels_recorded")) for row in results)
    summary_seen = int(hydration_counts.get("summary", 0)) > 0
    skip_seen = int(hydration_counts.get("skip", 0)) > 0
    integrity_green = quality_preserved and hashes_stable and levels_recorded
    accepted = integrity_green and summary_seen and skip_seen
    fallback_authorized = (
        budget_tokens == PRIMARY_BUDGET_TOKENS
        and integrity_green
        and all_full
        and not accepted
    )
    stage_valid = accepted or fallback_authorized
    return {
        "execution_count": len(results),
        "quality_preserved": quality_preserved,
        "packet_hashes_stable": hashes_stable,
        "hydration_levels_recorded": levels_recorded,
        "full_decisions": int(hydration_counts.get("full", 0)),
        "summary_decisions": int(hydration_counts.get("summary", 0)),
        "skip_decisions": int(hydration_counts.get("skip", 0)),
        "all_ranked_parent_decisions_full": all_full,
        "fallback_authorized": fallback_authorized,
        "acceptance_passed": accepted,
        "stage_valid": stage_valid,
    }


def _decision_counts(decisions: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {level: 0 for level in VALID_LEVELS}
    for decision in decisions:
        level = str(decision.get("hydration_level") or "")
        require(level in VALID_LEVELS, f"invalid hydration level: {level!r}")
        counts[level] += 1
    return counts


async def _retrieve_once(
    *,
    orchestrator: Any,
    query: str,
    corpus_id: str,
    retrieval_tier: Any,
) -> Any:
    # Positional query bypasses the retriever's kwargs cache. That is required
    # because the cache key intentionally does not include this dark assembly
    # flag, and the diagnostic must exercise each arm rather than reuse OFF.
    return await orchestrator.retrieve(
        query,
        corpus_ids=[corpus_id],
        retrieval_tier=retrieval_tier,
        collections=None,
        final_top_k=DIAGNOSTIC_TOP_K,
    )


async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    validate_budget(args.budget_tokens, args.fallback_from)
    prereg = _load_hashed_json(
        args.prereg,
        BRIDGE_PREREG_SHA256,
        "bridge preregistration",
    )
    selection = _load_hashed_json(
        args.selection,
        SELECTION_SHA256,
        "selection manifest",
    )
    cases = list(prereg.get("queries") or [])
    require(len(cases) == 6, "bridge query count drifted")
    selected_titles = {str(row["filename"]) for row in selection["selected"]}
    require(len(selected_titles) == 15, "selection did not close at 15 titles")

    from config import get_settings
    from models.schemas import RetrievalTier
    from motor.motor_asyncio import AsyncIOMotorClient
    from neo4j import AsyncGraphDatabase
    from qdrant_client import AsyncQdrantClient
    from services.conversation import conversation_service
    from services.ingestion_service import ingestion_service
    from services.retriever import retriever_orchestrator

    settings = get_settings()
    require(
        not bool(getattr(settings, "FOUR_LANE_TIER0_ROUTER_ENABLED", False)),
        "router activation is forbidden in the isolated waterfall diagnostic",
    )
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    database = mongo[settings.MONGODB_DATABASE]
    qdrant = AsyncQdrantClient(
        url=settings.QDRANT_URL,
        timeout=settings.QDRANT_TIMEOUT_SECONDS,
    )
    neo4j = None
    if settings.NEO4J_ENABLED:
        neo4j = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )

    # Read-only process-local service binding. Deliberately do not call
    # IngestionService.connect(): startup readiness repair can mutate indexes.
    old_db = getattr(ingestion_service, "_db", None)
    old_qdrant = getattr(ingestion_service, "_qdrant", None)
    old_neo4j = getattr(ingestion_service, "_neo4j", None)
    old_conversation_db = getattr(conversation_service, "_db", None)
    old_flag = bool(getattr(settings, "WATERFALL_ASSEMBLY", False))
    old_budget = int(getattr(settings, "WATERFALL_BUDGET_TOKENS", 4000))
    ingestion_service._db = database
    ingestion_service._qdrant = qdrant
    ingestion_service._neo4j = neo4j
    conversation_service._db = database

    try:
        corpora = (
            await database["corpora"]
            .find(
                {"name": CORPUS_NAME, "status": {"$ne": "deleted"}},
                {"_id": 0, "corpus_id": 1, "name": 1},
            )
            .to_list(length=None)
        )
        require(len(corpora) == 1, "fresh E2E corpus discovery was not unique")
        corpus_id = str(corpora[0]["corpus_id"])
        documents = (
            await database["documents"]
            .find(
                {"corpus_id": corpus_id, "status": {"$ne": "deleted"}},
                {
                    "_id": 0,
                    "doc_id": 1,
                    "original_filename": 1,
                    "filename": 1,
                },
            )
            .to_list(length=None)
        )
        require(len(documents) == 15, "E2E document count drifted")
        document_names = {
            str(row.get("doc_id") or ""): str(
                row.get("original_filename") or row.get("filename") or ""
            )
            for row in documents
        }
        require(
            set(document_names.values()) == selected_titles,
            "E2E document selection drifted",
        )

        settings.WATERFALL_BUDGET_TOKENS = args.budget_tokens
        tier = RetrievalTier(DIAGNOSTIC_TIER)
        results: list[dict[str, Any]] = []
        aggregate_counts = {level: 0 for level in VALID_LEVELS}

        for case in cases:
            query_id = str(case["id"])
            print(f"WATERFALL_PRESSURE_START {query_id}", flush=True)

            settings.WATERFALL_ASSEMBLY = False
            off = await _retrieve_once(
                orchestrator=retriever_orchestrator,
                query=str(case["question"]),
                corpus_id=corpus_id,
                retrieval_tier=tier,
            )
            require(
                not getattr(off, "packet", None), "OFF arm unexpectedly built packet"
            )

            settings.WATERFALL_ASSEMBLY = True
            on_first = await _retrieve_once(
                orchestrator=retriever_orchestrator,
                query=str(case["question"]),
                corpus_id=corpus_id,
                retrieval_tier=tier,
            )
            on_second = await _retrieve_once(
                orchestrator=retriever_orchestrator,
                query=str(case["question"]),
                corpus_id=corpus_id,
                retrieval_tier=tier,
            )
            first_packet = getattr(on_first, "packet", None) or {}
            second_packet = getattr(on_second, "packet", None) or {}
            require(first_packet, f"{query_id}: first ON run produced no packet")
            require(second_packet, f"{query_id}: repeated ON run produced no packet")
            require(
                int(first_packet.get("budget_tokens") or 0) == args.budget_tokens,
                f"{query_id}: packet budget drifted",
            )
            require(
                int(second_packet.get("budget_tokens") or 0) == args.budget_tokens,
                f"{query_id}: repeated packet budget drifted",
            )
            require(
                int(first_packet.get("used_tokens") or 0) <= args.budget_tokens
                and int(second_packet.get("used_tokens") or 0) <= args.budget_tokens,
                f"{query_id}: packet exceeded the fixed budget",
            )

            off_signature = _chunk_signature(off.chunks)
            first_signature = _chunk_signature(on_first.chunks)
            second_signature = _chunk_signature(on_second.chunks)
            selection_identical = off_signature == first_signature == second_signature
            first_hash = str(first_packet.get("packet_hash") or "")
            second_hash = str(second_packet.get("packet_hash") or "")
            hash_stable = bool(first_hash and first_hash == second_hash)

            decisions = list(first_packet.get("hydration_decisions") or [])
            repeat_decisions = list(second_packet.get("hydration_decisions") or [])
            require(decisions, f"{query_id}: no ranked-parent hydration decisions")
            levels_recorded = (
                all(
                    str(decision.get("hydration_level") or "") in VALID_LEVELS
                    for decision in decisions
                )
                and decisions == repeat_decisions
                and all(
                    str(item.get("hydration_level") or "")
                    for item in first_packet.get("items") or []
                )
            )
            counts = _decision_counts(decisions)
            for level in VALID_LEVELS:
                aggregate_counts[level] += counts[level]

            off_titles = _top_distinct_titles(off.chunks, document_names)
            on_titles = _top_distinct_titles(on_first.chunks, document_names)
            off_score = score_bridge_titles(case, off_titles)
            on_score = score_bridge_titles(case, on_titles)
            quality_preserved = (
                selection_identical
                and on_score["passed"] == off_score["passed"]
                and (not bool(off_score["passed"]) or bool(on_score["passed"]))
            )
            row = {
                "query_id": query_id,
                "question": case["question"],
                "off": off_score,
                "on": on_score,
                "evidence_selection_identical": selection_identical,
                "evidence_signature_sha256": {
                    "off": _signature_sha256(off_signature),
                    "on_first": _signature_sha256(first_signature),
                    "on_repeat": _signature_sha256(second_signature),
                },
                "selected_source_ids": [
                    {
                        key: value
                        for key, value in source.items()
                        if key != "text_sha256"
                    }
                    for source in off_signature
                ],
                "packet_hash": first_hash,
                "repeat_packet_hash": second_hash,
                "packet_hash_stable": hash_stable,
                "budget_tokens": int(first_packet.get("budget_tokens") or 0),
                "used_tokens": int(first_packet.get("used_tokens") or 0),
                "hydration_counts": counts,
                "hydration_levels_recorded": levels_recorded,
                "hydration_decisions": decisions,
                "quality_preserved": quality_preserved,
            }
            results.append(row)
            print(
                "WATERFALL_PRESSURE_DONE "
                + json.dumps(
                    {
                        "query_id": query_id,
                        "hash_stable": hash_stable,
                        "quality_preserved": quality_preserved,
                        "hydration_counts": counts,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

        summary = summarize_gate(
            budget_tokens=args.budget_tokens,
            results=results,
            hydration_counts=aggregate_counts,
        )
        artifact = {
            "schema_version": "polymath.waterfall_pressure_results.v1",
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "preregistration_sha256": BRIDGE_PREREG_SHA256,
            "selection_sha256": SELECTION_SHA256,
            "corpus_id": corpus_id,
            "corpus_name": CORPUS_NAME,
            "runtime": {
                "waterfall_assembly": True,
                "budget_tokens": args.budget_tokens,
                "fallback_from": (
                    str(args.fallback_from) if args.fallback_from is not None else None
                ),
                "retrieval_tier": DIAGNOSTIC_TIER,
                "final_top_k": DIAGNOSTIC_TOP_K,
                "four_lane_tier0_router_enabled": False,
                "synthesis_calls": 0,
                "corpus_writes": 0,
            },
            "results": results,
            "summary": summary,
        }
        return artifact, 0 if summary["stage_valid"] else 1
    finally:
        settings.WATERFALL_ASSEMBLY = old_flag
        settings.WATERFALL_BUDGET_TOKENS = old_budget
        ingestion_service._db = old_db
        ingestion_service._qdrant = old_qdrant
        ingestion_service._neo4j = old_neo4j
        conversation_service._db = old_conversation_db
        await qdrant.close()
        if neo4j is not None:
            await neo4j.close()
        mongo.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--budget-tokens",
        type=int,
        choices=(PRIMARY_BUDGET_TOKENS, FALLBACK_BUDGET_TOKENS),
        default=PRIMARY_BUDGET_TOKENS,
    )
    parser.add_argument("--fallback-from", type=Path)
    args = parser.parse_args()
    artifact, exit_code = asyncio.run(_run(args))
    _atomic_write(args.output, artifact)
    print(json.dumps(artifact["summary"], sort_keys=True), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
