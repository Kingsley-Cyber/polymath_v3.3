#!/usr/bin/env python3
"""Diagnose Agent-T determinism without reaching a provider call.

The runner executes inside the canonical backend image with Agent T enabled
only in this process. It performs:

* d1: three serial retrieval-only passes over the compact ten-query set;
* d2: a real chat-orchestrator pass stopped at the retrieval-done trace,
  compared with a direct retrieval-only pass for three queries;
* d3: a stage-input comparison for one divergence in the sealed RED packet.

The chat generator is closed before synthesis, and both LLM entry points are
replaced with fail-closed sentinels. Any attempted provider call fails the run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import types
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Sequence

from bson import ObjectId
from config import get_settings
from models.schemas import ChatRequest, ModelConfig, ModelOverrides, RetrievalTier
from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient
from services.chat_orchestrator import chat_orchestrator
from services.conversation import conversation_service
from services.ingestion_service import ingestion_service
from services.llm import llm_service
from services.retriever import retriever_orchestrator
from services.retriever.query_plan import build_query_plan_v2, query_plan_to_dict
from services.settings import settings_service

from scripts.run_two_lane_canonical_window import (
    QUERY_IDS,
    _selection_surface,
)


SCHEMA_VERSION = "polymath.two_lane_zero_provider_diagnosis.v1"
BASELINE_SHA256 = "9ad78c13ed4233e97f23bd4f2ae302ddcf3b4fb9c81b8b88c0813e1b7c60b501"
D2_QUERY_IDS = (
    "direct_anatomy_masses",
    "direct_elemental_novel",
    "relationship_shoot_edit_emotion",
)
D3_QUERY_ID = "direct_anatomy_masses"
EXPECTED_FLAGS = {
    "TWO_LANE_ANCHORING_ENABLED": True,
    "TEMPORAL_QUERY_ROUTING_ENABLED": False,
    "ATOMIC_CLAIM_ANCHORS_ENABLED": False,
    "GROUNDED_QUERY_PLANNER_ENABLED": False,
    "FOUR_LANE_TIER0_ROUTER_ENABLED": False,
    "FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED": False,
    "WATERFALL_ASSEMBLY": False,
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha(value: Any) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _load_baseline(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    _require(
        sha256(raw).hexdigest() == BASELINE_SHA256,
        "sealed T RED baseline SHA drifted",
    )
    packet = json.loads(raw)
    _require(packet.get("sealed") is True, "T RED baseline is not sealed")
    executions = packet.get("executions") or []
    repeats = packet.get("retrieval_only_repeats") or []
    _require(
        [str(row.get("query_id") or "") for row in executions] == list(QUERY_IDS),
        "T RED baseline execution order drifted",
    )
    _require(len(repeats) == len(QUERY_IDS), "T RED repeat count drifted")
    return packet


def _stage_inputs(surface: dict[str, Any]) -> dict[str, Any]:
    diagnostics = surface.get("diagnostics") or {}
    groups = [row for row in diagnostics.get("groups") or [] if isinstance(row, dict)]
    selected = [
        row for row in diagnostics.get("selected") or [] if isinstance(row, dict)
    ]
    return {
        "anchor_candidate_pool_order": [
            {
                "side": str(group.get("side") or ""),
                "candidate_ids": [
                    str(value) for value in group.get("anchor_candidate_ids") or []
                ],
            }
            for group in groups
        ],
        "lane_classification": [
            {
                "candidate_id": str(row.get("candidate_id") or ""),
                "lane": str(row.get("lane") or ""),
                "side": str(row.get("side") or ""),
                "matched_fields": sorted(
                    str(value) for value in row.get("matched_fields") or []
                ),
            }
            for row in selected
        ],
        "quota_math": {
            "budget": diagnostics.get("budget"),
            "anchor_seats": diagnostics.get("anchor_seats"),
            "expansion_seats": diagnostics.get("expansion_seats"),
            "groups": [
                {
                    "side": str(group.get("side") or ""),
                    "budget": group.get("budget"),
                    "anchor_quota": group.get("anchor_quota"),
                    "expansion_quota": group.get("expansion_quota"),
                    "anchor_primary_filled": group.get("anchor_primary_filled"),
                    "expansion_primary_filled": group.get("expansion_primary_filled"),
                }
                for group in groups
            ],
        },
        "selected_identity": surface.get("selected_identity") or [],
    }


def _compare_stages(
    left_surface: dict[str, Any],
    right_surface: dict[str, Any],
) -> dict[str, Any]:
    left = _stage_inputs(left_surface)
    right = _stage_inputs(right_surface)
    comparisons = {
        key: left[key] == right[key]
        for key in (
            "anchor_candidate_pool_order",
            "lane_classification",
            "quota_math",
            "selected_identity",
        )
    }
    first_divergent = next(
        (key for key, identical in comparisons.items() if not identical),
        None,
    )
    return {
        "identical": all(comparisons.values()),
        "comparisons": comparisons,
        "first_divergent_stage": first_divergent,
        "left": left,
        "right": right,
    }


def _execution_map(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["query_id"]): row for row in packet.get("executions") or []}


def _repeat_map(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row["query_id"]): row for row in packet.get("retrieval_only_repeats") or []
    }


def _budget_for(execution: dict[str, Any]) -> dict[str, Any]:
    budget = dict(execution["evaluation"]["query_plan"].get("budget") or {})
    return {
        "retrieval_k": int(budget.get("retrieval_k") or 8),
        "rerank_enabled": bool(budget.get("rerank_enabled")),
        "top_k_summary": budget.get("top_k_summary"),
        "rerank_top_n": (
            int(budget["rerank_top_n"])
            if budget.get("rerank_top_n") is not None
            else None
        ),
        "final_top_k": int(budget.get("final_top_k") or 8),
        "fact_seed_limit": 40,
        "search_mode": str(
            execution["evaluation"]["query_plan"].get("search_mode") or "local"
        ),
    }


class Runtime:
    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings
        self.mongo = AsyncIOMotorClient(settings.MONGODB_URI)
        self.database = self.mongo[settings.MONGODB_DATABASE]
        self.qdrant = AsyncQdrantClient(
            url=settings.QDRANT_URL,
            timeout=settings.QDRANT_TIMEOUT_SECONDS,
        )
        self.neo4j = (
            AsyncGraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
            )
            if settings.NEO4J_ENABLED
            else None
        )
        self.old_bindings = {
            "ingestion_db": getattr(ingestion_service, "_db", None),
            "ingestion_qdrant": getattr(ingestion_service, "_qdrant", None),
            "ingestion_neo4j": getattr(ingestion_service, "_neo4j", None),
            "conversation_db": getattr(conversation_service, "_db", None),
            "settings_db": getattr(settings_service, "_db", None),
        }
        ingestion_service._db = self.database
        ingestion_service._qdrant = self.qdrant
        ingestion_service._neo4j = self.neo4j
        conversation_service._db = self.database
        settings_service._db = self.database

    async def close(self) -> None:
        ingestion_service._db = self.old_bindings["ingestion_db"]
        ingestion_service._qdrant = self.old_bindings["ingestion_qdrant"]
        ingestion_service._neo4j = self.old_bindings["ingestion_neo4j"]
        conversation_service._db = self.old_bindings["conversation_db"]
        settings_service._db = self.old_bindings["settings_db"]
        await self.qdrant.close()
        if self.neo4j is not None:
            await self.neo4j.close()
        self.mongo.close()


async def _direct_retrieval(
    execution: dict[str, Any],
) -> dict[str, Any]:
    trace_plan = execution["evaluation"]["query_plan"]
    question = str(trace_plan.get("query") or "")
    retrieval_query = str(trace_plan.get("retrieval_query") or question)
    corpus_ids = list((trace_plan.get("query_plan_v2") or {}).get("corpus_ids") or [])
    _require(bool(corpus_ids), "diagnostic execution lacks corpus_ids")
    budget = _budget_for(execution)
    plan = build_query_plan_v2(
        question,
        corpus_ids=corpus_ids,
        standalone_query=retrieval_query,
    )
    result = await retriever_orchestrator.retrieve_planned(
        plan=plan,
        corpus_ids=corpus_ids,
        retrieval_tier=RetrievalTier("qdrant_mongo_graph"),
        collections=None,
        retrieval_k=budget["retrieval_k"],
        rerank_enabled=budget["rerank_enabled"],
        top_k_summary=budget["top_k_summary"],
        rerank_top_n=budget["rerank_top_n"],
        final_top_k=budget["final_top_k"],
        fact_seed_limit=budget["fact_seed_limit"],
        search_mode=budget["search_mode"],
    )
    diagnostics = dict(getattr(result, "diagnostics", None) or {})
    selection = diagnostics.get("selection") or {}
    surface = _selection_surface(selection.get("two_lane_anchoring"))
    _require(
        surface["present"], f"missing two-lane diagnostics for {execution['query_id']}"
    )
    plan_dict = query_plan_to_dict(plan)
    return {
        "query_id": execution["query_id"],
        "plan": plan_dict,
        "plan_sha256": _sha(plan_dict),
        "budget": budget,
        "surface": surface,
        "fingerprint_sha256": surface["allocation_fingerprint_sha256"],
    }


def _decode_sse(raw: str) -> dict[str, Any] | None:
    for line in str(raw).splitlines():
        if not line.startswith("data:"):
            continue
        encoded = line[5:].strip()
        if encoded and encoded != "[DONE]":
            return json.loads(encoded)
    return None


async def _full_path_until_retrieval(
    execution: dict[str, Any],
    provider_attempts: dict[str, int],
) -> dict[str, Any]:
    trace_plan = execution["evaluation"]["query_plan"]
    question = str(trace_plan.get("query") or "")
    corpus_ids = list((trace_plan.get("query_plan_v2") or {}).get("corpus_ids") or [])
    budget = _budget_for(execution)
    request = ChatRequest(
        message=question,
        corpus_ids=corpus_ids,
        retrieval_tier=RetrievalTier("qdrant_mongo_graph"),
        overrides=ModelOverrides(
            model="openai/provider-call-forbidden",
            retrieval_k=budget["retrieval_k"],
            final_top_k=budget["final_top_k"],
            rerank_enabled=budget["rerank_enabled"],
            rerank_top_n=budget["rerank_top_n"],
            hyde_enabled=False,
            agentic_mode=False,
            temperature=0.0,
            search_mode=budget["search_mode"],
            fact_seed_limit=budget["fact_seed_limit"],
        ),
    )

    async def fake_load(_request: ChatRequest):
        return ObjectId(), ModelConfig(model="openai/provider-call-forbidden"), []

    async def forbidden_complete(*_args, **_kwargs):
        provider_attempts["complete_sync"] += 1
        raise RuntimeError("provider call forbidden in T diagnosis")

    async def forbidden_stream(*_args, **_kwargs):
        provider_attempts["stream_chat"] += 1
        raise RuntimeError("provider call forbidden in T diagnosis")
        yield ""  # pragma: no cover

    old_load = chat_orchestrator._load_or_create_conversation
    old_complete = llm_service.complete_sync
    old_stream = llm_service.stream_chat
    chat_orchestrator._load_or_create_conversation = fake_load
    llm_service.complete_sync = forbidden_complete
    llm_service.stream_chat = forbidden_stream
    query_plan_trace = None
    retrieval_diagnostics = None
    stream = chat_orchestrator.process_chat_request(request, user_id=None)
    try:
        async for raw in stream:
            event = _decode_sse(raw)
            trace = (event or {}).get("trace_event") or {}
            if trace.get("title") == "Query plan" and trace.get("status") == "done":
                query_plan_trace = dict(trace.get("metadata") or {})
            if (
                trace.get("title") == "Local RAG retrieval"
                and trace.get("status") == "done"
            ):
                retrieval_diagnostics = dict(
                    (trace.get("metadata") or {}).get("retrieval_diagnostics") or {}
                )
                break
    finally:
        await stream.aclose()
        chat_orchestrator._load_or_create_conversation = old_load
        llm_service.complete_sync = old_complete
        llm_service.stream_chat = old_stream

    _require(query_plan_trace is not None, "full path emitted no Query plan trace")
    _require(
        retrieval_diagnostics is not None,
        "full path emitted no completed retrieval trace",
    )
    selection = retrieval_diagnostics.get("selection") or {}
    surface = _selection_surface(selection.get("two_lane_anchoring"))
    _require(surface["present"], "full path omitted two-lane diagnostics")
    comparable_plan = dict(query_plan_trace.get("query_plan_v2") or {})
    return {
        "query_id": execution["query_id"],
        "query_plan_trace_sha256": _sha(query_plan_trace),
        "comparable_plan": comparable_plan,
        "comparable_plan_sha256": _sha(comparable_plan),
        "surface": surface,
        "fingerprint_sha256": surface["allocation_fingerprint_sha256"],
    }


async def diagnose(packet: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    observed_flags = {name: bool(getattr(settings, name)) for name in EXPECTED_FLAGS}
    _require(
        observed_flags == EXPECTED_FLAGS,
        "diagnostic runtime flag contract mismatch: "
        + json.dumps(
            {"expected": EXPECTED_FLAGS, "observed": observed_flags},
            sort_keys=True,
        ),
    )
    execution_by_id = _execution_map(packet)
    repeat_by_id = _repeat_map(packet)
    runtime = Runtime()
    provider_attempts = {"complete_sync": 0, "stream_chat": 0}
    try:
        d1_rows = []
        for query_id in QUERY_IDS:
            passes = []
            for pass_number in range(1, 4):
                result = await _direct_retrieval(execution_by_id[query_id])
                result["pass"] = pass_number
                passes.append(result)
            fingerprints = [row["fingerprint_sha256"] for row in passes]
            identities = [row["surface"]["selected_identity"] for row in passes]
            plans = [row["plan_sha256"] for row in passes]
            d1_rows.append(
                {
                    "query_id": query_id,
                    "passes": passes,
                    "fingerprint_stable": len(set(fingerprints)) == 1,
                    "selected_identity_stable": all(
                        value == identities[0] for value in identities[1:]
                    ),
                    "plan_stable": len(set(plans)) == 1,
                }
            )

        d2_rows = []
        for query_id in D2_QUERY_IDS:
            full = await _full_path_until_retrieval(
                execution_by_id[query_id],
                provider_attempts,
            )
            direct = await _direct_retrieval(execution_by_id[query_id])
            comparison = _compare_stages(full["surface"], direct["surface"])
            d2_rows.append(
                {
                    "query_id": query_id,
                    "full_path": full,
                    "retrieval_only": direct,
                    "comparable_plan_identical": (
                        full["comparable_plan"] == direct["plan"]
                    ),
                    "fingerprint_identical": (
                        full["fingerprint_sha256"] == direct["fingerprint_sha256"]
                    ),
                    "stage_comparison": comparison,
                }
            )

        prior_full = execution_by_id[D3_QUERY_ID]["evaluation"]["two_lane"]
        prior_repeat = repeat_by_id[D3_QUERY_ID]["two_lane"]
        d3_comparison = _compare_stages(prior_full, prior_repeat)
        prior_full_trace = execution_by_id[D3_QUERY_ID]["evaluation"]["query_plan"]
        d3 = {
            "query_id": D3_QUERY_ID,
            "sealed_red_stage_comparison": d3_comparison,
            "stored_full_plan_hash_domain": "chat_query_plan_trace_wrapper",
            "stored_repeat_plan_hash_domain": "query_plan_v2",
            "stored_hashes_comparable": False,
            "full_trace_wrapper_sha256_recomputed": _sha(prior_full_trace),
            "stored_full_trace_sha256": execution_by_id[D3_QUERY_ID]["evaluation"][
                "query_plan_sha256"
            ],
            "stored_repeat_plan_sha256": repeat_by_id[D3_QUERY_ID]["query_plan_sha256"],
        }

        d1_stable = all(
            row["fingerprint_stable"]
            and row["selected_identity_stable"]
            and row["plan_stable"]
            for row in d1_rows
        )
        d2_equivalent = all(
            row["comparable_plan_identical"]
            and row["fingerprint_identical"]
            and row["stage_comparison"]["identical"]
            for row in d2_rows
        )
        if d1_stable and d2_equivalent:
            verdict = "H1_CONCURRENCY_CONFIRMED"
        elif not d2_equivalent:
            verdict = "H2_PATH_INEQUIVALENCE_CONFIRMED"
        else:
            verdict = "H2_LEAD_SERIAL_INSTABILITY"
        _require(sum(provider_attempts.values()) == 0, "provider sentinel was reached")
        return {
            "schema_version": SCHEMA_VERSION,
            "started_from_baseline_sha256": BASELINE_SHA256,
            "captured_at_utc": _utc_now(),
            "runtime_flags": observed_flags,
            "provider_calls": 0,
            "corpus_writes": 0,
            "d1": {
                "mode": "serial_retrieval_only_x3",
                "query_count": len(d1_rows),
                "all_stable": d1_stable,
                "rows": d1_rows,
            },
            "d2": {
                "mode": "serial_chat_generator_stop_after_retrieval_vs_direct",
                "query_ids": list(D2_QUERY_IDS),
                "all_equivalent": d2_equivalent,
                "rows": d2_rows,
            },
            "d3": d3,
            "provider_sentinels": provider_attempts,
            "verdict": verdict,
        }
    finally:
        await runtime.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def run(args: argparse.Namespace) -> int:
    _require(not args.output.exists(), "diagnosis output already exists")
    packet = _load_baseline(args.baseline)
    result = asyncio.run(diagnose(packet))
    result["seal_sha256"] = _sha(result)
    args.output.write_bytes(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        + b"\n"
    )
    print(
        "T_DIAGNOSIS "
        + json.dumps(
            {
                "verdict": result["verdict"],
                "d1_all_stable": result["d1"]["all_stable"],
                "d2_all_equivalent": result["d2"]["all_equivalent"],
                "provider_calls": result["provider_calls"],
                "seal_sha256": result["seal_sha256"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(_parser().parse_args(argv))
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"T_DIAGNOSIS_ABORT={type(exc).__name__}: {exc}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
