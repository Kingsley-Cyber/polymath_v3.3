#!/usr/bin/env python3
"""Run the owner-bounded canonical Agent-T verification window.

The first pass drives the real chat SSE path for ten preregistered positive
queries.  The second pass repeats retrieval only, so determinism is measured
on the allocation fingerprint and selected-seat identity rather than answer
bytes.  The runner must execute in the canonical backend container while the
host and container eval locks are already held by the caller.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import sys
import urllib.error
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from config import get_settings
from scripts.run_canonical_heldout_negative_eval import (
    CORPUS_ID,
    CORPUS_NAME,
    SELECTION_PATH,
    SELECTION_SHA256,
    TEMPERATURE,
    TIER,
    TOP_K,
    _atomic_write,
    _build_execution,
    _canonical_bytes,
    _cost_envelope,
    _embedder_preflight,
    _eval_lock,
    _load_hashed_json,
    _prompt_template_receipt,
    _run_sse,
    _seal_journal,
    _sha256_bytes,
    _utc_now,
    _validate_local_api,
    _validate_same_container_runtime,
    _verify_corpus,
)


BACKEND = Path(__file__).resolve().parents[1]
PREREG_PATH = BACKEND / "evals/runpod_e2e_retrieval_preregister_v1.json"
PREREG_SHA256 = "8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110"
BASELINE_PATH = (
    BACKEND.parent / "docs/baselines/BUILD_FIRST_CANONICAL_BASELINE_10_2026-07-18.json"
)

SELECTION_NAME = "two-lane-canonical-10.v1"
ANCHOR_SURFACE_QUERY_IDS = (
    "direct_rule_of_six",
    "direct_laban_machine",
    "direct_edit_grammar",
    "direct_elemental_novel",
    "lay_manga_attention",
    "relationship_movement_machine_figure",
)
RELATIONSHIP_SPOT_QUERY_IDS = (
    "relationship_shoot_edit_emotion",
    "relationship_fight_camera_direction",
)
DIRECT_SPOT_QUERY_IDS = (
    "direct_facs",
    "direct_anatomy_masses",
)
QUERY_IDS = (
    *ANCHOR_SURFACE_QUERY_IDS,
    *RELATIONSHIP_SPOT_QUERY_IDS,
    *DIRECT_SPOT_QUERY_IDS,
)

JOURNAL_SCHEMA = "polymath.two_lane_canonical_window.v1"
RETRIEVAL_REPEAT_SCHEMA = "polymath.two_lane_retrieval_repeat.v1"
# Full-path repeats replace the retrieval-only shortcut: the 2026-07-18
# zero-provider diagnosis proved the in-process reconstruction was not
# path-identical (candidate order and quota diverged after pool
# construction), so determinism is now measured by re-running the exact
# chat execution and comparing the same trace surface.
FULL_PATH_REPEAT_SCHEMA = "polymath.two_lane_full_path_repeat.v1"
CONCURRENCY = 3


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _normalized_doc_name(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def select_cases(prereg: dict[str, Any]) -> list[dict[str, Any]]:
    """Select the exact compact surface without editing the frozen spec."""

    queries = list(prereg.get("queries") or [])
    by_id = {str(row.get("id") or ""): row for row in queries}
    missing = [query_id for query_id in QUERY_IDS if query_id not in by_id]
    require(not missing, f"T compact query ids missing from frozen spec: {missing}")
    selected = [dict(by_id[query_id]) for query_id in QUERY_IDS]
    require(len(selected) == 10, "T compact selection must contain exactly 10 queries")
    require(len({str(row["id"]) for row in selected}) == 10, "duplicate T query id")
    return selected


def _trace_metadata(
    traces: Sequence[dict[str, Any]],
    title: str,
) -> dict[str, Any]:
    for trace in reversed(traces):
        if trace.get("title") != title or trace.get("status") != "done":
            continue
        metadata = trace.get("metadata")
        if isinstance(metadata, dict):
            return json.loads(json.dumps(metadata, default=str))
    return {}


def _selection_surface(selection: dict[str, Any] | None) -> dict[str, Any]:
    value = selection if isinstance(selection, dict) else {}
    groups = [row for row in value.get("groups") or [] if isinstance(row, dict)]
    selected = [row for row in value.get("selected") or [] if isinstance(row, dict)]
    identity = [
        {
            "seat": int(row.get("seat") or position),
            "candidate_id": str(row.get("candidate_id") or ""),
            "side": str(row.get("side") or ""),
            "lane": str(row.get("lane") or ""),
        }
        for position, row in enumerate(selected, start=1)
    ]
    pool_anchor_ids = sorted(
        {
            str(candidate_id)
            for group in groups
            for candidate_id in group.get("anchor_candidate_ids") or []
            if str(candidate_id)
        }
    )
    pool_has_anchor = any(int(row.get("anchors_available") or 0) > 0 for row in groups)
    selected_has_anchor = any(row["lane"] == "anchor" for row in identity)
    return {
        "present": bool(value),
        "pool_has_anchor": pool_has_anchor,
        "pool_anchor_candidate_ids": pool_anchor_ids,
        "selected_has_anchor": selected_has_anchor,
        "anchor_seats": int(value.get("anchor_seats") or 0),
        "expansion_seats": int(value.get("expansion_seats") or 0),
        "selected_identity": identity,
        "allocation_fingerprint": [row["candidate_id"] for row in identity],
        "allocation_fingerprint_sha256": _sha256_bytes(_canonical_bytes(identity)),
        "diagnostics": value,
    }


def _score_sources(
    case: dict[str, Any],
    source_receipt: dict[str, Any],
) -> dict[str, Any]:
    expected = {_normalized_doc_name(value) for value in case.get("expected_any") or []}
    returned = {
        _normalized_doc_name(row.get("doc_name"))
        for row in source_receipt.get("items") or []
        if row.get("doc_name")
    }
    hits = sorted(expected & returned)
    minimum = int(case.get("expected_min_distinct") or 1)
    return {
        "expected_doc_names": sorted(expected),
        "returned_doc_names": sorted(returned),
        "expected_hits": hits,
        "doc_hit": bool(hits),
        "minimum_distinct_required": minimum,
        "minimum_distinct_ok": len(hits) >= minimum,
    }


def _runtime_flags() -> dict[str, bool]:
    settings = get_settings()
    expected = {
        "RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED": True,
        "ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED": True,
        "TEMPORAL_QUERY_ROUTING_ENABLED": False,
        "RERANK_EVIDENCE_SUPPORT": False,
        "ATOMIC_CLAIM_ANCHORS_ENABLED": False,
        "PARENT_EXCERPT_ENABLED": False,
        "WATERFALL_ASSEMBLY": False,
        # Compatibility-only legacy setting; the production selector reads the
        # explicitly named ENABLED flag and Compose passes only that contract.
        "TWO_LANE_ANCHORING": False,
        "TWO_LANE_ANCHORING_ENABLED": True,
        "HYDE_ENABLED": False,
        "SHELF_RESERVE_ENABLED": False,
        "GROUNDED_QUERY_PLANNER_ENABLED": False,
        "FOUR_LANE_TIER0_ROUTER_ENABLED": False,
        "FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED": False,
        "AGENTIC_MODE_ENABLED": False,
    }
    observed = {name: bool(getattr(settings, name)) for name in expected}
    require(
        observed == expected,
        "runtime flags do not match T canonical contract: "
        + json.dumps({"expected": expected, "observed": observed}, sort_keys=True),
    )
    return observed


def _augment_execution(
    *,
    row: dict[str, Any],
    case: dict[str, Any],
    raw: dict[str, Any],
) -> dict[str, Any]:
    retrieval_meta = _trace_metadata(raw["traces"], "Local RAG retrieval")
    query_plan_meta = _trace_metadata(raw["traces"], "Query plan")
    retrieval_diagnostics = retrieval_meta.get("retrieval_diagnostics")
    if not isinstance(retrieval_diagnostics, dict):
        retrieval_diagnostics = {}
    selection = retrieval_diagnostics.get("selection")
    if not isinstance(selection, dict):
        selection = {}
    t_diagnostics = selection.get("two_lane_anchoring")
    surface = _selection_surface(
        t_diagnostics if isinstance(t_diagnostics, dict) else None
    )
    row["evaluation"] = {
        "shape": str(case.get("shape") or ""),
        "selection_role": (
            "anchor_surface"
            if case["id"] in ANCHOR_SURFACE_QUERY_IDS
            else "relationship_spot"
            if case["id"] in RELATIONSHIP_SPOT_QUERY_IDS
            else "direct_spot"
        ),
        "source_score": _score_sources(case, row["sources"]),
        "two_lane": surface,
        "query_plan": query_plan_meta,
        "query_plan_sha256": _sha256_bytes(_canonical_bytes(query_plan_meta)),
    }
    if not query_plan_meta:
        row["technical"]["errors"].append("missing completed Query plan trace")
    if not surface["present"]:
        row["technical"]["errors"].append(
            "missing selection.two_lane_anchoring diagnostics"
        )
    if row["technical"]["errors"]:
        row["technical"]["ok"] = False
        row["technical"]["status"] = "failed"
    return row


def _nested_int(value: dict[str, Any] | None, *keys: str) -> int | None:
    current: Any = value or {}
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if current is None:
        return None
    return int(current)


def _full_path_repeat(
    *,
    api: str,
    token: str,
    executions: Sequence[dict[str, Any]],
    request_timeout: float,
) -> list[dict[str, Any]]:
    """Serial full-path determinism repeats.

    Each repeat re-runs the identical chat execution and extracts the
    two-lane surface from the identical trace location as pass one, so the
    fingerprint comparison has one domain and one code path by construction.
    """

    rows: list[dict[str, Any]] = []
    for execution in executions:
        evaluation = execution["evaluation"]
        trace_plan = evaluation.get("query_plan") or {}
        question = str(trace_plan.get("query") or "")
        expected_surface = evaluation["two_lane"]
        started = datetime.now(timezone.utc)
        error = None
        surface = _selection_surface(None)
        if not question:
            error = "pass-one execution carries no question text"
        else:
            try:
                raw = _run_sse(
                    api=api,
                    token=token,
                    question=question,
                    timeout=request_timeout,
                )
                retrieval_meta = _trace_metadata(
                    raw["traces"], "Local RAG retrieval"
                )
                retrieval_diagnostics = retrieval_meta.get(
                    "retrieval_diagnostics"
                )
                if not isinstance(retrieval_diagnostics, dict):
                    retrieval_diagnostics = {}
                selection = retrieval_diagnostics.get("selection")
                if not isinstance(selection, dict):
                    selection = {}
                t_diagnostics = selection.get("two_lane_anchoring")
                surface = _selection_surface(
                    t_diagnostics if isinstance(t_diagnostics, dict) else None
                )
            except Exception as exc:  # noqa: BLE001 - durable technical receipt
                error = f"{type(exc).__name__}: {exc}"[:500]
        rows.append(
            {
                "schema_version": FULL_PATH_REPEAT_SCHEMA,
                "query_id": execution["query_id"],
                "started_at_utc": started.isoformat(),
                "completed_at_utc": _utc_now(),
                "synthesis_calls": 0 if error else 1,
                "error": error,
                "two_lane": surface,
                "fingerprint_identical": (
                    surface["allocation_fingerprint"]
                    == expected_surface["allocation_fingerprint"]
                ),
                "selected_identity_identical": (
                    surface["selected_identity"]
                    == expected_surface["selected_identity"]
                ),
                "technical_ok": bool(not error and surface["present"]),
            }
        )
    return rows


async def _retrieval_only_repeat(
    *,
    token: str,
    executions: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Repeat the exact planned retrieval surface with no synthesis call."""

    from models.schemas import RetrievalTier
    from motor.motor_asyncio import AsyncIOMotorClient
    from neo4j import AsyncGraphDatabase
    from qdrant_client import AsyncQdrantClient
    from services.auth import auth_service
    from services.conversation import conversation_service
    from services.ingestion_service import ingestion_service
    from services.retriever import retriever_orchestrator
    from services.retriever.query_plan import build_query_plan_v2, query_plan_to_dict

    settings = get_settings()
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

    old_db = getattr(ingestion_service, "_db", None)
    old_qdrant = getattr(ingestion_service, "_qdrant", None)
    old_neo4j = getattr(ingestion_service, "_neo4j", None)
    old_conversation_db = getattr(conversation_service, "_db", None)
    ingestion_service._db = database
    ingestion_service._qdrant = qdrant
    ingestion_service._neo4j = neo4j
    conversation_service._db = database

    try:
        token_data = auth_service.verify_token(token)
        require(token_data is not None, "eval token could not be decoded for repeat")
        saved = await database["settings"].find_one(
            {"user_id": token_data.user_id},
            {"_id": 0, "retrieval.graph_fact_seeds": 1},
        )
        fact_seed_limit = _nested_int(saved, "retrieval", "graph_fact_seeds")
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def repeat_one(execution: dict[str, Any]) -> dict[str, Any]:
            evaluation = execution["evaluation"]
            trace_plan = evaluation["query_plan"]
            budget = (
                trace_plan.get("budget")
                if isinstance(trace_plan.get("budget"), dict)
                else {}
            )
            question = str(trace_plan.get("query") or "")
            retrieval_query = str(trace_plan.get("retrieval_query") or question)
            plan = build_query_plan_v2(
                question,
                corpus_ids=[CORPUS_ID],
                standalone_query=retrieval_query,
            )
            started = datetime.now(timezone.utc)
            error = None
            result = None
            try:
                async with semaphore:
                    result = await retriever_orchestrator.retrieve_planned(
                        plan=plan,
                        corpus_ids=[CORPUS_ID],
                        retrieval_tier=RetrievalTier(TIER),
                        collections=None,
                        retrieval_k=int(budget.get("retrieval_k") or TOP_K),
                        rerank_enabled=bool(budget.get("rerank_enabled")),
                        top_k_summary=(
                            int(budget["top_k_summary"])
                            if budget.get("top_k_summary") is not None
                            else None
                        ),
                        rerank_top_n=(
                            int(budget["rerank_top_n"])
                            if budget.get("rerank_top_n") is not None
                            else None
                        ),
                        final_top_k=int(budget.get("final_top_k") or TOP_K),
                        fact_seed_limit=fact_seed_limit,
                        search_mode=str(trace_plan.get("search_mode") or "local"),
                    )
            except Exception as exc:  # noqa: BLE001 - durable technical receipt
                error = f"{type(exc).__name__}: {exc}"[:500]

            diagnostics = (
                dict(getattr(result, "diagnostics", None) or {}) if result else {}
            )
            selection = diagnostics.get("selection")
            if not isinstance(selection, dict):
                selection = {}
            surface = _selection_surface(
                selection.get("two_lane_anchoring")
                if isinstance(selection.get("two_lane_anchoring"), dict)
                else None
            )
            expected_surface = evaluation["two_lane"]
            return {
                "schema_version": RETRIEVAL_REPEAT_SCHEMA,
                "query_id": execution["query_id"],
                "started_at_utc": started.isoformat(),
                "completed_at_utc": _utc_now(),
                "synthesis_calls": 0,
                "error": error,
                "query_plan_sha256": _sha256_bytes(
                    _canonical_bytes(query_plan_to_dict(plan))
                ),
                "runtime_parameters": {
                    "retrieval_k": int(budget.get("retrieval_k") or TOP_K),
                    "rerank_enabled": bool(budget.get("rerank_enabled")),
                    "top_k_summary": budget.get("top_k_summary"),
                    "rerank_top_n": budget.get("rerank_top_n"),
                    "final_top_k": int(budget.get("final_top_k") or TOP_K),
                    "fact_seed_limit": fact_seed_limit,
                    "search_mode": str(trace_plan.get("search_mode") or "local"),
                },
                "two_lane": surface,
                "fingerprint_identical": (
                    surface["allocation_fingerprint"]
                    == expected_surface["allocation_fingerprint"]
                ),
                "selected_identity_identical": (
                    surface["selected_identity"]
                    == expected_surface["selected_identity"]
                ),
                "technical_ok": bool(
                    result is not None and not error and surface["present"]
                ),
            }

        tasks = [asyncio.create_task(repeat_one(row)) for row in executions]
        return list(await asyncio.gather(*tasks))
    finally:
        ingestion_service._db = old_db
        ingestion_service._qdrant = old_qdrant
        ingestion_service._neo4j = old_neo4j
        conversation_service._db = old_conversation_db
        await qdrant.close()
        if neo4j is not None:
            await neo4j.close()
        mongo.close()


def summarize(
    executions: Sequence[dict[str, Any]],
    repeats: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    direct = [
        row
        for row in executions
        if str(row["evaluation"]["shape"]).startswith("direct_")
    ]
    lay = [row for row in executions if row["evaluation"]["shape"] == "lay_language"]
    relationship = [
        row
        for row in executions
        if row["evaluation"]["shape"] == "relationship_multi_document"
    ]
    anchor_eligible = [
        row for row in executions if row["evaluation"]["two_lane"]["pool_has_anchor"]
    ]

    def rate(values: Sequence[bool]) -> float:
        return (
            round(sum(value is True for value in values) / len(values), 6)
            if values
            else 0.0
        )

    direct_rate = rate([row["evaluation"]["source_score"]["doc_hit"] for row in direct])
    lay_rate = rate([row["evaluation"]["source_score"]["doc_hit"] for row in lay])
    relationship_rate = rate(
        [
            row["evaluation"]["source_score"]["minimum_distinct_ok"]
            for row in relationship
        ]
    )
    anchor_coverage = rate(
        [
            row["evaluation"]["two_lane"]["selected_has_anchor"]
            for row in anchor_eligible
        ]
    )
    states = Counter(row["classification"]["state"] for row in executions)
    gates = {
        "technical": all(row["technical"]["ok"] is True for row in executions)
        and all(row["technical_ok"] is True for row in repeats),
        "journal_complete": all(row["journal_complete"] is True for row in executions),
        "positive_answerability": all(
            row["classification"]["state"] == "answered" for row in executions
        ),
        "direct_floor": direct_rate >= 0.85,
        "lay_floor": lay_rate >= 0.75,
        "relationship_floor": relationship_rate >= 0.75,
        "relationship_cited_side": all(
            row["evaluation"]["source_score"]["minimum_distinct_ok"]
            for row in relationship
        ),
        "corpus_citation_membership": all(
            row["sources"]["all_in_selected_corpus"] is True for row in executions
        ),
        "anchor_surface_eligible": all(
            next(row for row in executions if row["query_id"] == query_id)[
                "evaluation"
            ]["two_lane"]["pool_has_anchor"]
            for query_id in ANCHOR_SURFACE_QUERY_IDS
        ),
        "anchor_coverage": len(anchor_eligible) >= 6 and anchor_coverage >= 0.90,
        "fingerprint_determinism": all(
            row["fingerprint_identical"] is True for row in repeats
        ),
        "selected_identity_determinism": all(
            row["selected_identity_identical"] is True for row in repeats
        ),
    }
    return {
        "execution_count": len(executions),
        "retrieval_only_repeat_count": len(repeats),
        "classification_counts": dict(sorted(states.items())),
        "direct_count": len(direct),
        "direct_doc_hit_rate": direct_rate,
        "lay_count": len(lay),
        "lay_doc_hit_rate": lay_rate,
        "relationship_count": len(relationship),
        "relationship_minimum_distinct_rate": relationship_rate,
        "anchor_eligible_count": len(anchor_eligible),
        "anchor_coverage_rate": anchor_coverage,
        "fingerprint_determinism_rate": rate(
            [row["fingerprint_identical"] for row in repeats]
        ),
        "selected_identity_determinism_rate": rate(
            [row["selected_identity_identical"] for row in repeats]
        ),
        "gates": gates,
        "all_green": all(gates.values()),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument(
        "--lock-owner",
        default="codex/build-first-queue-20260718",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    token = os.getenv("POLYMATH_EVAL_TOKEN")
    require(bool(token), "POLYMATH_EVAL_TOKEN is required")
    require(not args.output.exists(), "output already exists; use a fresh journal")
    endpoint = _validate_same_container_runtime(_validate_local_api(args.api))
    prereg = _load_hashed_json(PREREG_PATH, PREREG_SHA256, "frozen retrieval spec")
    cases = select_cases(prereg)
    selection = _load_hashed_json(
        SELECTION_PATH,
        SELECTION_SHA256,
        "15-document selection",
    )
    selected_filenames = {
        str(row["filename"]) for row in selection.get("selected") or []
    }
    require(len(selected_filenames) == 15, "15-document selection drifted")
    flags = _runtime_flags()
    cost_envelope = _cost_envelope(len(cases))
    process_run_id = str(uuid.uuid4())

    with _eval_lock(args.lock_owner, 0, mode="assert-held"):
        preflight = _embedder_preflight(args.api)
        document_names, corpus_receipt = _verify_corpus(
            args.api,
            str(token),
            selected_filenames,
        )
        prompt_receipt, prompt_rendered_at = _prompt_template_receipt()
        state: dict[str, Any] = {
            "schema_version": JOURNAL_SCHEMA,
            "started_at_utc": _utc_now(),
            "completed_at_utc": None,
            "sealed": False,
            "selection": {
                "name": SELECTION_NAME,
                "query_ids": list(QUERY_IDS),
                "anchor_surface_query_ids": list(ANCHOR_SURFACE_QUERY_IDS),
                "relationship_spot_query_ids": list(RELATIONSHIP_SPOT_QUERY_IDS),
                "direct_spot_query_ids": list(DIRECT_SPOT_QUERY_IDS),
                "query_id_sha256": _sha256_bytes(_canonical_bytes(list(QUERY_IDS))),
            },
            "frozen_spec_sha256": PREREG_SHA256,
            "corpus_selection_sha256": SELECTION_SHA256,
            "corpus": corpus_receipt,
            "tier": TIER,
            "top_k": TOP_K,
            "temperature": TEMPERATURE,
            "concurrency": CONCURRENCY,
            "runtime_flags": flags,
            "endpoint_binding": endpoint,
            "authentication": {"token_source": "POLYMATH_EVAL_TOKEN"},
            "embedder_preflight": preflight,
            "system_prompt_template": prompt_receipt,
            "cost_envelope": cost_envelope,
            "process_run_id": process_run_id,
            "comparison_baseline": {
                "path": str(BASELINE_PATH.relative_to(BACKEND.parent)),
                "file_sha256": (
                    _sha256_bytes(BASELINE_PATH.read_bytes())
                    if BASELINE_PATH.is_file()
                    else None
                ),
                "prior_off_anchor_coverage_rate": 0.5556,
                "prior_off_value_source": (
                    "senior ruling 2026-07-18T01:47Z over sealed pre-hardening OFF arm"
                ),
            },
            "executions": [],
            "retrieval_only_repeats": [],
            "summary": None,
            "seal": None,
        }
        _atomic_write(args.output, state)
        print(
            "T_CANONICAL_START "
            + json.dumps(
                {
                    "queries": len(cases),
                    "concurrency": CONCURRENCY,
                    "temperature": TEMPERATURE,
                    "cost_envelope_usd": cost_envelope["total_usd"],
                },
                sort_keys=True,
            ),
            flush=True,
        )

        def execute_one(
            ordinal: int,
            case: dict[str, Any],
        ) -> tuple[int, dict[str, Any]]:
            print(f"T_SYNTHESIS_START {ordinal}/10 {case['id']}", flush=True)
            raw = _run_sse(
                api=args.api,
                token=str(token),
                question=str(case["question"]),
                timeout=args.request_timeout,
            )
            canonical_case = {**case, "family": str(case.get("shape") or "")}
            row = _build_execution(
                case=canonical_case,
                ordinal=ordinal,
                process_run_id=process_run_id,
                raw=raw,
                prompt_receipt=prompt_receipt,
                document_names=document_names,
                selected_filenames=selected_filenames,
                concurrency=CONCURRENCY,
            )
            return ordinal, _augment_execution(row=row, case=case, raw=raw)

        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = [
                pool.submit(execute_one, ordinal, case)
                for ordinal, case in enumerate(cases, start=1)
            ]
            for future in concurrent.futures.as_completed(futures):
                ordinal, row = future.result()
                state["executions"].append(row)
                state["executions"].sort(
                    key=lambda item: item["prior_call_state"]["request_ordinal"]
                )
                _atomic_write(args.output, state)
                print(
                    "T_SYNTHESIS_DONE "
                    + json.dumps(
                        {
                            "ordinal": ordinal,
                            "query_id": row["query_id"],
                            "state": row["classification"]["state"],
                            "doc_hit": row["evaluation"]["source_score"]["doc_hit"],
                            "minimum_distinct": row["evaluation"]["source_score"][
                                "minimum_distinct_ok"
                            ],
                            "pool_anchor": row["evaluation"]["two_lane"][
                                "pool_has_anchor"
                            ],
                            "selected_anchor": row["evaluation"]["two_lane"][
                                "selected_has_anchor"
                            ],
                            "technical_ok": row["technical"]["ok"],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )

        final_prompt_receipt, _ = _prompt_template_receipt(prompt_rendered_at)
        prompt_stable = final_prompt_receipt == prompt_receipt
        if not prompt_stable:
            for row in state["executions"]:
                row["technical"]["ok"] = False
                row["technical"]["status"] = "failed"
                row["technical"]["errors"].append(
                    "system-prompt rendered hash or source SHA changed during batch"
                )

        repeats = _full_path_repeat(
            api=args.api,
            token=str(token),
            executions=state["executions"],
            request_timeout=args.request_timeout,
        )
        for repeat in repeats:
            state["retrieval_only_repeats"].append(repeat)
            state["retrieval_only_repeats"].sort(key=lambda row: row["query_id"])
            _atomic_write(args.output, state)
            print(
                "T_RETRIEVAL_REPEAT_DONE "
                + json.dumps(
                    {
                        "query_id": repeat["query_id"],
                        "fingerprint_identical": repeat["fingerprint_identical"],
                        "selected_identity_identical": repeat[
                            "selected_identity_identical"
                        ],
                        "technical_ok": repeat["technical_ok"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

        state["completed_at_utc"] = _utc_now()
        state["prompt_render_context_stable"] = prompt_stable
        state["summary"] = summarize(state["executions"], repeats)
        technically_sealable = (
            len(state["executions"]) == len(cases)
            and len(repeats) == len(cases)
            and state["summary"]["gates"]["technical"]
            and state["summary"]["gates"]["journal_complete"]
            and prompt_stable
        )
        state["sealed"] = technically_sealable
        state["seal"] = _seal_journal(state) if technically_sealable else None
        _atomic_write(args.output, state)
        print("T_CANONICAL_SUMMARY " + json.dumps(state["summary"], sort_keys=True))
        if not technically_sealable:
            return 2
        return 0 if state["summary"]["all_green"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return run(args)
    except (RuntimeError, OSError, ValueError, urllib.error.URLError) as exc:
        print(f"T_CANONICAL_ABORT={type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
