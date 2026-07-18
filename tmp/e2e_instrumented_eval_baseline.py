#!/usr/bin/env python3
"""Read-only instrumented rerun of the frozen and temporal E2E suites."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_settings
from e2e_retrieval_eval import (
    PREREG_SHA as FROZEN_PREREG_SHA,
    SELECTION_SHA,
    TIERS,
    _atomic_write,
    _finalize,
    _mint_token,
    _score_execution,
)
from e2e_temporal_diagnostic_eval import (
    PREREG_SHA as TEMPORAL_PREREG_SHA,
    summarize as summarize_temporal,
)
from neo4j import GraphDatabase
from pymongo import MongoClient
from services.secrets import decrypt
from utils.tokens import count_tokens


CORPUS_ID = "2c894530-8d57-4432-a6d4-bc14505a698b"
CORPUS_NAME = "runpod_e2e_15doc_20260715"
EXPECTED_QUERY_MODEL = "anthropic/minimax-m2.7"
EXPECTED_QUERY_PROVIDER = "opencode-go-anthropic"
EXPECTED_QUERY_BASE = "https://opencode.ai/zen/go"
EXPECTED_QUERY_POOL_SAFE_SHA256 = (
    "91bf6ceb54940ac467163624c3a92e2284f28cdbfda3863ccf4acc3671edadfe"
)
MINIMAX_INPUT_USD_PER_MILLION = 0.30
MINIMAX_OUTPUT_USD_PER_MILLION = 1.20
MODEL_COMPLETION_TOKEN_BOUND = 16_384
MEASURED_SYSTEM_PROMPT_TOKENS = 2_338
MEASURED_MAX_EVIDENCE_CHARS = 13_293
PROMPT_WRAPPER_TOKEN_ALLOWANCE = 4_096
MAX_EXECUTIONS_PER_COST_TRANCHE = 35
MONGO_INDEX_COLLECTIONS = (
    "corpora",
    "documents",
    "chunks",
    "parent_chunks",
    "summary_tree",
    "ghost_b_extractions",
    "relation_support_records",
    "corpus_lexicon",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def wait_for_local_retrieval_sidecars(timeout_seconds: float = 240.0) -> None:
    """Drain orphaned local inference before the next serial eval execution.

    A chat response can close after its retrieval deadline while the host-side
    inference thread is still completing.  Waiting here changes no request,
    score, model, or eval contract; it prevents that abandoned work from
    contaminating the next independently scored execution.
    """
    endpoints = (
        ("embedder", "http://host.docker.internal:8082/health"),
        ("reranker", "http://host.docker.internal:8081/health"),
    )
    deadline = time.monotonic() + timeout_seconds
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        ready = True
        current: dict[str, Any] = {}
        for name, url in endpoints:
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                current[name] = {
                    "status": payload.get("status"),
                    "in_flight": bool(payload.get("in_flight")),
                    "last_error": payload.get("last_error"),
                }
                if payload.get("status") != "ok" or payload.get("in_flight"):
                    ready = False
            except Exception as exc:
                current[name] = {"error": f"{type(exc).__name__}: {exc}"}
                ready = False
        last_state = current
        if ready:
            print(
                "SIDECAR_DRAIN " + json.dumps(current, sort_keys=True),
                flush=True,
            )
            return
        time.sleep(2)
    raise RuntimeError(
        "local retrieval sidecars failed to drain before the next execution: "
        + json.dumps(last_state, sort_keys=True)
    )


def load_json_with_hash(path: Path, expected_sha256: str, label: str) -> dict[str, Any]:
    payload = path.read_bytes()
    require(
        hashlib.sha256(payload).hexdigest() == expected_sha256, f"{label} hash drifted"
    )
    return json.loads(payload)


def query_route_preflight(database: Any, corpus: dict[str, Any]) -> dict[str, Any]:
    user_id = str(corpus.get("user_id") or "")
    require(user_id, "E2E corpus owner is absent for query-route preflight")
    settings_row = (
        database["settings"].find_one({"user_id": user_id}, {"_id": 0, "models": 1})
        or {}
    )
    pool = list(((settings_row.get("models") or {}).get("query_model_pool") or []))
    safe_pool = [
        {key: value for key, value in entry.items() if key != "api_key_ciphertext"}
        for entry in pool
        if isinstance(entry, dict)
    ]
    safe_hash = hashlib.sha256(
        json.dumps(
            safe_pool, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()
    require(
        safe_hash == EXPECTED_QUERY_POOL_SAFE_SHA256,
        "query-model pool safe hash drifted",
    )
    enabled = [entry for entry in pool if entry.get("enabled", True)]
    require(enabled, "query-model pool has no enabled entry")
    primary = enabled[0]
    credential = primary.get("api_key_ciphertext")
    require(
        str(primary.get("provider") or "") == EXPECTED_QUERY_PROVIDER,
        "primary query provider drifted",
    )
    require(
        str(primary.get("model_name") or "") == "minimax-m2.7",
        "primary query model drifted",
    )
    require(
        str(primary.get("base_url") or "") == EXPECTED_QUERY_BASE,
        "primary query base URL drifted",
    )
    require(bool(credential and decrypt(credential)), "primary query credential absent")
    return {
        "user_id": user_id,
        "pool_entry_count": len(pool),
        "pool_safe_sha256": safe_hash,
        "primary_entry_id": str(primary.get("entry_id") or ""),
        "provider": EXPECTED_QUERY_PROVIDER,
        "model": EXPECTED_QUERY_MODEL,
        "api_base": EXPECTED_QUERY_BASE,
        "credential_present_and_decryptable": True,
    }


def cost_envelope(execution_count: int) -> dict[str, Any]:
    require(
        0 <= execution_count <= MAX_EXECUTIONS_PER_COST_TRANCHE,
        "cost tranche exceeds fail-closed execution limit",
    )
    # One character per token for retrieved evidence is deliberately much more
    # conservative than the normal tokenizer. The separate wrapper allowance
    # covers query/fact/citation formatting around the measured evidence.
    input_tokens_per_attempt = (
        MEASURED_SYSTEM_PROMPT_TOKENS
        + MEASURED_MAX_EVIDENCE_CHARS
        + PROMPT_WRAPPER_TOKEN_ALLOWANCE
    )
    attempts = execution_count * 2
    input_usd = (
        attempts * input_tokens_per_attempt * MINIMAX_INPUT_USD_PER_MILLION / 1_000_000
    )
    output_usd = (
        attempts
        * MODEL_COMPLETION_TOKEN_BOUND
        * MINIMAX_OUTPUT_USD_PER_MILLION
        / 1_000_000
    )
    total_usd = input_usd + output_usd
    require(total_usd <= 2.0, "two-attempt tranche envelope exceeds $2")
    return {
        "schema_version": "chat_eval_cost_envelope.v1",
        "execution_count": execution_count,
        "attempt_bound": attempts,
        "input_tokens_per_attempt_bound": input_tokens_per_attempt,
        "completion_tokens_per_attempt_bound": MODEL_COMPLETION_TOKEN_BOUND,
        "input_usd": round(input_usd, 9),
        "output_usd": round(output_usd, 9),
        "total_usd": round(total_usd, 9),
        "ceiling_usd": 2.0,
        "rate_card": {
            "model": "MiniMax M2.7",
            "uncached_input_usd_per_million": MINIMAX_INPUT_USD_PER_MILLION,
            "output_usd_per_million": MINIMAX_OUTPUT_USD_PER_MILLION,
            "source": "https://opencode.ai/docs/go/",
            "checked_at_utc": "2026-07-16",
        },
        "measurement_basis": {
            "prior_model_calls": 50,
            "prior_max_evidence_chars": MEASURED_MAX_EVIDENCE_CHARS,
            "prior_total_output_tokens": 16_058,
            "prior_max_output_tokens": 631,
            "system_prompt_tokens": MEASURED_SYSTEM_PROMPT_TOKENS,
            "evidence_conversion": "one evidence character equals one input token",
            "wrapper_allowance_tokens": PROMPT_WRAPPER_TOKEN_ALLOWANCE,
        },
    }


def run_sse_instrumented(
    *,
    base: str,
    token: str,
    corpus_id: str,
    tier: str,
    question: str,
    top_k: int,
) -> dict[str, Any]:
    """The frozen request contract, with observation-only SSE capture added."""
    payload = {
        "message": question,
        "corpus_ids": [corpus_id],
        "retrieval_tier": tier,
        "overrides": {"final_top_k": top_k, "temperature": 0},
    }
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/chat",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    started = time.perf_counter()
    current_event: str | None = None
    answer: list[str] = []
    sources: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    errors: list[str] = []
    budget_frames: list[dict[str, Any]] = []
    event_counts: Counter[str] = Counter()
    done: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            if response.status != 200:
                raise RuntimeError(f"chat HTTP status {response.status}")
            if "text/event-stream" not in str(
                response.headers.get("Content-Type") or ""
            ):
                raise RuntimeError("chat did not return SSE")
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event:"):
                    current_event = line.split(":", 1)[1].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                try:
                    obj = json.loads(line[5:].strip())
                except Exception:
                    continue
                event_type = str(obj.get("type") or current_event or "unknown")
                event_counts[event_type] += 1
                if event_type == "token":
                    answer.append(str(obj.get("content") or obj.get("token") or ""))
                elif event_type == "sources":
                    raw_sources = obj.get("sources") or obj.get("data") or []
                    sources = raw_sources if isinstance(raw_sources, list) else []
                elif event_type == "trace_event" or obj.get("trace_event"):
                    traces.append(dict(obj.get("trace_event") or obj))
                elif event_type == "budget":
                    budget_frames.append(
                        {
                            key: obj.get(key)
                            for key in (
                                "tokens_used",
                                "tokens_max",
                                "trimming_applied",
                            )
                        }
                    )
                elif event_type == "error":
                    errors.append(
                        str(obj.get("content") or obj.get("error") or "")[:500]
                    )
                elif event_type == "done":
                    done = obj
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        errors.append(f"HTTP {exc.code}: {detail}")
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return {
        "answer": "".join(answer),
        "sources": sources,
        "traces": traces,
        "errors": errors,
        "done": done,
        "budget_frames": budget_frames,
        "sse_event_counts": dict(sorted(event_counts.items())),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def mongo_index_snapshot(database: Any) -> dict[str, Any]:
    counters: dict[str, int] = {}
    errors: dict[str, str] = {}
    for collection_name in MONGO_INDEX_COLLECTIONS:
        try:
            rows = list(database[collection_name].aggregate([{"$indexStats": {}}]))
            for row in rows:
                name = str(row.get("name") or "unknown")
                counters[f"{collection_name}::{name}"] = int(
                    ((row.get("accesses") or {}).get("ops") or 0)
                )
        except Exception as exc:  # diagnostic must record unsupported surfaces
            errors[collection_name] = f"{type(exc).__name__}: {exc}"[:500]
    return {"counters": counters, "errors": errors}


def neo4j_index_snapshot(settings: Any) -> dict[str, Any]:
    driver = GraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        rows, _, _ = driver.execute_query(
            "SHOW INDEXES YIELD name, readCount, lastRead "
            "RETURN name, readCount, lastRead"
        )
        return {
            "counters": {
                str(row["name"]): int(row.get("readCount") or 0) for row in rows
            },
            "last_read": {
                str(row["name"]): str(row.get("lastRead") or "") for row in rows
            },
            "error": None,
        }
    except Exception as exc:  # Neo4j edition/version may not expose readCount
        return {
            "counters": {},
            "last_read": {},
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
    finally:
        driver.close()


def counter_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    keys = set(before) | set(after)
    return {
        key: int(after.get(key, 0)) - int(before.get(key, 0))
        for key in sorted(keys)
        if int(after.get(key, 0)) - int(before.get(key, 0)) != 0
    }


def local_rag_metadata(traces: list[dict[str, Any]]) -> dict[str, Any]:
    for trace in reversed(traces):
        if (
            trace.get("title") == "Local RAG retrieval"
            and trace.get("status") == "done"
        ):
            return dict(trace.get("metadata") or {})
    return {}


def resolved_trace_models(traces: list[dict[str, Any]]) -> list[str]:
    models = {
        str((trace.get("metadata") or {}).get("model") or "")
        for trace in traces
        if trace.get("title") == "Chat model route"
    }
    return sorted(model for model in models if model)


def source_payload_key_counts(sources: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for source in sources:
        for key, value in source.items():
            if value is not None and value != "" and value != [] and value != {}:
                counts[str(key)] += 1
    return dict(sorted(counts.items()))


def canonical_stage_timings(
    *,
    raw_timings: dict[str, Any],
    trace_duration_s: float | None,
    internal_total_s: float | None,
) -> dict[str, float | None]:
    def present_sum(*names: str) -> float | None:
        values = [float(raw_timings[name]) for name in names if name in raw_timings]
        return round(sum(values), 6) if values else None

    assemble = None
    if trace_duration_s is not None and internal_total_s is not None:
        assemble = round(max(0.0, trace_duration_s - internal_total_s), 6)
    return {
        "embed_s": present_sum("embed"),
        "planning_vocabulary_s": present_sum("vocabulary_resolution"),
        "vector_candidate_search_s": present_sum(
            "document_routing",
            "summary_tree_routing",
            "candidate_generation",
            "funnels",
        ),
        "hydrate_s": present_sum("hydrate", "hydrate_finalists", "rerank_text_hydrate"),
        "graph_s": present_sum("fact_seed", "graph", "graph_boost", "graph_decoration"),
        "identity_dedupe_s": present_sum("identity_dedupe"),
        "repair_s": present_sum("repair"),
        "rerank_s": present_sum("rerank"),
        "assemble_support_residual_s": assemble,
    }


def index_contract_for_tier(
    tier: str, corpus_id: str, diagnostics: dict[str, Any]
) -> list[str]:
    prefix = f"corpus_{corpus_id[:8]}"
    indexes = [f"qdrant::{prefix}_hrag", f"qdrant::{prefix}_naive"]
    if tier == "qdrant_mongo_graph":
        indexes.append(f"qdrant::{prefix}_graph")
    counts = diagnostics.get("counts") or {}
    timings = diagnostics.get("timings_s") or {}
    if (
        int(counts.get("document_routes") or 0) > 0
        or float(timings.get("document_routing") or 0.0) > 0
    ):
        indexes.append("qdrant::polymath_doc_summaries")
    return indexes


def instrument_raw(raw: dict[str, Any], *, tier: str, corpus_id: str) -> dict[str, Any]:
    metadata = local_rag_metadata(raw["traces"])
    diagnostics = dict(metadata.get("retrieval_diagnostics") or {})
    raw_timings = {
        str(key): float(value)
        for key, value in (diagnostics.get("timings_s") or {}).items()
        if isinstance(value, (int, float))
    }
    trace_duration = metadata.get("duration_s")
    trace_duration_s = (
        float(trace_duration) if isinstance(trace_duration, (int, float)) else None
    )
    internal_total = diagnostics.get("total_s")
    internal_total_s = (
        float(internal_total) if isinstance(internal_total, (int, float)) else None
    )
    canonical = canonical_stage_timings(
        raw_timings=raw_timings,
        trace_duration_s=trace_duration_s,
        internal_total_s=internal_total_s,
    )
    client_total_s = float(raw["elapsed_seconds"])
    budget_tokens = [
        int(frame["tokens_used"])
        for frame in (raw.get("budget_frames") or [])
        if isinstance(frame.get("tokens_used"), int)
    ]
    return {
        "client_total_s": client_total_s,
        "retrieval_trace_s": trace_duration_s,
        "retrieval_internal_total_s": internal_total_s,
        "post_retrieval_and_planning_residual_s": (
            round(max(0.0, client_total_s - trace_duration_s), 6)
            if trace_duration_s is not None
            else None
        ),
        "canonical_stage_s": canonical,
        "raw_stage_s": dict(sorted(raw_timings.items())),
        "retrieval_status": diagnostics.get("status"),
        "store_contract": diagnostics.get("store_contract") or {},
        "diagnostic_counts": diagnostics.get("counts") or {},
        "final_source_tiers": diagnostics.get("final_source_tiers") or {},
        "index_execution_contract": index_contract_for_tier(
            tier, corpus_id, diagnostics
        ),
        "source_payload_key_counts": source_payload_key_counts(raw["sources"]),
        "budget_frames": raw.get("budget_frames") or [],
        "prompt_tokens_local_estimate": max(budget_tokens) if budget_tokens else None,
        "answer_tokens_local_estimate": count_tokens(
            raw.get("answer") or "", EXPECTED_QUERY_MODEL
        ),
        "sse_event_counts": raw.get("sse_event_counts") or {},
        "resolved_trace_models": resolved_trace_models(raw["traces"]),
        "trace_titles": [
            f"{trace.get('title')}::{trace.get('status')}" for trace in raw["traces"]
        ],
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return round(float(ordered[index]), 6)


def distribution(values: list[float]) -> dict[str, Any]:
    return {
        "exposed_count": len(values),
        "p50_s": percentile(values, 0.50),
        "p95_s": percentile(values, 0.95),
        "max_s": round(max(values), 6) if values else None,
    }


def aggregate_instrumentation(
    results: list[dict[str, Any]],
    *,
    mongo_before: dict[str, Any],
    mongo_after: dict[str, Any],
    neo4j_before: dict[str, Any],
    neo4j_after: dict[str, Any],
) -> dict[str, Any]:
    per_tier: dict[str, Any] = {}
    raw_stage_per_tier: dict[str, dict[str, list[float]]] = {}
    store_contract_counts: Counter[str] = Counter()
    diagnostic_count_sums: Counter[str] = Counter()
    source_tier_distribution: Counter[str] = Counter()
    payload_key_presence: Counter[str] = Counter()
    index_contract_counts: Counter[str] = Counter()
    retrieval_status_counts: Counter[str] = Counter()
    model_path_counts: Counter[str] = Counter()

    for tier in TIERS:
        rows = [row for row in results if row["tier"] == tier]
        stage_values: dict[str, list[float]] = defaultdict(list)
        raw_values: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            instrumentation = row["instrumentation"]
            for key in (
                "client_total_s",
                "retrieval_trace_s",
                "retrieval_internal_total_s",
                "post_retrieval_and_planning_residual_s",
            ):
                value = instrumentation.get(key)
                if isinstance(value, (int, float)):
                    stage_values[key].append(float(value))
            for key, value in instrumentation["canonical_stage_s"].items():
                if isinstance(value, (int, float)):
                    stage_values[key].append(float(value))
            for key, value in instrumentation["raw_stage_s"].items():
                raw_values[key].append(float(value))
            for key, value in instrumentation["store_contract"].items():
                if value is True:
                    store_contract_counts[f"{tier}::{key}"] += 1
            for key, value in instrumentation["diagnostic_counts"].items():
                if isinstance(value, (int, float)):
                    diagnostic_count_sums[f"{tier}::{key}"] += int(value)
            for key, value in instrumentation["final_source_tiers"].items():
                source_tier_distribution[f"{tier}::{key}"] += int(value)
            for key, value in instrumentation["source_payload_key_counts"].items():
                payload_key_presence[key] += int(value)
            for index in instrumentation["index_execution_contract"]:
                index_contract_counts[f"{tier}::{index}"] += 1
            retrieval_status_counts[
                f"{tier}::{instrumentation.get('retrieval_status') or 'unknown'}"
            ] += 1
            model_path_counts[
                f"{tier}::{'skipped' if row.get('model_skipped') else 'called'}"
            ] += 1
        per_tier[tier] = {
            key: distribution(values) for key, values in sorted(stage_values.items())
        }
        raw_stage_per_tier[tier] = {
            key: distribution(values) for key, values in sorted(raw_values.items())
        }

    called_rows = [row for row in results if not row.get("model_skipped")]
    prompt_token_estimate = sum(
        int(row["instrumentation"].get("prompt_tokens_local_estimate") or 0)
        for row in called_rows
    )
    answer_token_estimate = sum(
        int(row["instrumentation"].get("answer_tokens_local_estimate") or 0)
        for row in called_rows
    )
    observed_cost_estimate = (
        prompt_token_estimate * MINIMAX_INPUT_USD_PER_MILLION
        + answer_token_estimate * MINIMAX_OUTPUT_USD_PER_MILLION
    ) / 1_000_000

    return {
        "schema_version": "e2e_instrumented_optimization_baseline.v1",
        "execution_count": len(results),
        "per_tier_latency": per_tier,
        "per_tier_raw_stage_latency": raw_stage_per_tier,
        "usage_counters": {
            "store_contract_execution_counts": dict(
                sorted(store_contract_counts.items())
            ),
            "diagnostic_count_sums": dict(sorted(diagnostic_count_sums.items())),
            "hit_source_distribution": dict(sorted(source_tier_distribution.items())),
            "source_payload_key_presence": dict(sorted(payload_key_presence.items())),
            "qdrant_index_execution_contract_counts": dict(
                sorted(index_contract_counts.items())
            ),
            "retrieval_status_counts": dict(sorted(retrieval_status_counts.items())),
            "model_path_counts": dict(sorted(model_path_counts.items())),
            "local_token_estimate": {
                "model_called_executions": len(called_rows),
                "prompt_tokens": prompt_token_estimate,
                "answer_tokens": answer_token_estimate,
                "uncached_rate_cost_estimate_usd": round(observed_cost_estimate, 9),
                "disclaimer": (
                    "Local tokenizer/budget estimates only; provider usage and retries "
                    "are absent from the chat SSE contract and require invoice reconciliation."
                ),
            },
            "mongo_index_access_delta": counter_delta(
                mongo_before.get("counters") or {}, mongo_after.get("counters") or {}
            ),
            "neo4j_index_access_delta": counter_delta(
                neo4j_before.get("counters") or {}, neo4j_after.get("counters") or {}
            ),
        },
        "counter_scope_notes": {
            "qdrant": (
                "Qdrant exposes no per-index read counter here; counts are trace- and "
                "tier-contract execution counts, not fabricated server read totals."
            ),
            "mongo": "Actual $indexStats access.ops delta across the timed window.",
            "neo4j": "Actual SHOW INDEXES readCount delta when supported by the server.",
            "assemble_support_residual_s": (
                "Local-RAG trace duration minus retriever internal total; includes support "
                "retrieval, dedupe, context gates, and source assembly after base retrieval."
            ),
        },
        "index_snapshot_errors": {
            "mongo_before": mongo_before.get("errors") or {},
            "mongo_after": mongo_after.get("errors") or {},
            "neo4j_before": neo4j_before.get("error"),
            "neo4j_after": neo4j_after.get("error"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--frozen-prereg", type=Path, required=True)
    parser.add_argument("--temporal-prereg", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--suite", choices=("frozen", "temporal"), required=True)
    parser.add_argument(
        "--max-executions",
        type=int,
        default=MAX_EXECUTIONS_PER_COST_TRANCHE,
    )
    args = parser.parse_args()
    require(args.corpus_id == CORPUS_ID, "instrumented corpus identity drifted")
    require(1 <= args.concurrency <= 6, "concurrency must be between 1 and 6")
    require(
        1 <= args.max_executions <= MAX_EXECUTIONS_PER_COST_TRANCHE,
        "max executions exceeds the cost-tranche limit",
    )

    frozen = load_json_with_hash(
        args.frozen_prereg, FROZEN_PREREG_SHA, "frozen preregistration"
    )
    temporal = load_json_with_hash(
        args.temporal_prereg, TEMPORAL_PREREG_SHA, "temporal preregistration"
    )
    selection = load_json_with_hash(args.selection, SELECTION_SHA, "selection")
    require(tuple(frozen.get("tiers") or ()) == TIERS, "frozen tiers drifted")
    require(tuple(temporal.get("tiers") or ()) == TIERS, "temporal tiers drifted")
    require(len(frozen.get("queries") or []) == 17, "frozen query count drifted")
    require(len(temporal.get("queries") or []) == 8, "temporal query count drifted")
    require(
        temporal.get("disposition") == "report_only_non_gate",
        "temporal disposition drifted",
    )
    selected_filenames = {str(row["filename"]) for row in selection["selected"]}
    require(len(selected_filenames) == 15, "selection did not close at 15 files")

    settings = get_settings()
    mongo = MongoClient(settings.MONGODB_URI)
    database = mongo[settings.MONGODB_DATABASE]
    try:
        corpus = database["corpora"].find_one(
            {"corpus_id": args.corpus_id},
            {"_id": 0, "name": 1, "user_id": 1},
        )
        require(
            bool(corpus) and corpus.get("name") == CORPUS_NAME, "E2E corpus drifted"
        )
        document_names = {
            str(row.get("doc_id") or ""): str(
                row.get("original_filename") or row.get("filename") or ""
            )
            for row in database["documents"].find(
                {"corpus_id": args.corpus_id, "status": {"$ne": "deleted"}},
                {"_id": 0, "doc_id": 1, "filename": 1, "original_filename": 1},
            )
        }
        require(len(document_names) == 15, "E2E corpus is not 15-document complete")
        route_preflight = query_route_preflight(database, corpus)
        token = _mint_token(database, args.corpus_id)

        if args.output.exists():
            state = json.loads(args.output.read_text(encoding="utf-8"))
            require(
                state.get("frozen_preregistration_sha256") == FROZEN_PREREG_SHA
                and state.get("temporal_preregistration_sha256") == TEMPORAL_PREREG_SHA
                and state.get("corpus_id") == args.corpus_id,
                "existing instrumented journal identity drifted",
            )
            require(
                state.get("query_route_preflight") == route_preflight,
                "query route drifted from instrumented journal",
            )
        else:
            state = {
                "schema_version": "e2e_instrumented_eval_results.v1",
                "started_at_utc": utc_now(),
                "completed_at_utc": None,
                "corpus_id": args.corpus_id,
                "frozen_preregistration_sha256": FROZEN_PREREG_SHA,
                "temporal_preregistration_sha256": TEMPORAL_PREREG_SHA,
                "selection_sha256": SELECTION_SHA,
                "concurrency": args.concurrency,
                "query_route_preflight": route_preflight,
                "cost_tranches": [],
                "mongo_index_before": mongo_index_snapshot(database),
                "neo4j_index_before": neo4j_index_snapshot(settings),
                "results": [],
                "frozen_summary": None,
                "temporal_summary": None,
                "optimization_baseline": None,
            }
            _atomic_write(args.output, state)

        completed = {str(row["execution_id"]) for row in state["results"]}
        suite_cases = [
            ("frozen", case, tier, int(frozen["top_k"]))
            for case in frozen["queries"]
            for tier in TIERS
        ] + [
            ("temporal", case, tier, int(temporal["top_k"]))
            for case in temporal["queries"]
            for tier in TIERS
        ]
        pending = [
            item
            for item in suite_cases
            if item[0] == args.suite
            and f"{item[0]}::{item[1]['id']}::{item[2]}" not in completed
        ]
        selected_pending = pending[: args.max_executions]
        envelope = cost_envelope(len(selected_pending))
        tranche: dict[str, Any] | None = None
        if selected_pending:
            tranche = {
                "tranche_id": f"{args.suite}-{len(state['cost_tranches']) + 1}",
                "suite": args.suite,
                "started_at_utc": utc_now(),
                "completed_at_utc": None,
                "execution_ids": [
                    f"{suite}::{case['id']}::{tier}"
                    for suite, case, tier, _ in selected_pending
                ],
                "envelope": envelope,
                "actual_model_called": None,
                "budget_tokens_used": [],
            }
            state["cost_tranches"].append(tranche)
            _atomic_write(args.output, state)
            print("COST_TRANCHE " + json.dumps(tranche, sort_keys=True), flush=True)

        def run_execution(
            suite: str, case: dict[str, Any], tier: str, top_k: int
        ) -> dict[str, Any]:
            execution_id = f"{suite}::{case['id']}::{tier}"
            print(f"INSTRUMENTED_START {execution_id}", flush=True)
            raw = run_sse_instrumented(
                base=args.base,
                token=token,
                corpus_id=args.corpus_id,
                tier=tier,
                question=case["question"],
                top_k=top_k,
            )
            wait_for_local_retrieval_sidecars()
            require(
                resolved_trace_models(raw["traces"]) == [EXPECTED_QUERY_MODEL],
                f"{execution_id} resolved query model drifted",
            )
            scored = _score_execution(
                case=case,
                tier=tier,
                raw=raw,
                corpus_id=args.corpus_id,
                document_names=document_names,
                selected_filenames=selected_filenames,
            )
            scored["suite"] = suite
            scored["execution_id"] = execution_id
            scored["instrumentation"] = instrument_raw(
                raw, tier=tier, corpus_id=args.corpus_id
            )
            return scored

        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = {
                executor.submit(run_execution, suite, case, tier, top_k): (
                    suite,
                    case,
                    tier,
                )
                for suite, case, tier, top_k in selected_pending
            }
            for future in as_completed(futures):
                suite, case, tier = futures[future]
                scored = future.result()
                state["results"].append(scored)
                _atomic_write(args.output, state)
                print(
                    "INSTRUMENTED_DONE "
                    + json.dumps(
                        {
                            "execution_id": scored["execution_id"],
                            "client_total_s": scored["instrumentation"][
                                "client_total_s"
                            ],
                            "retrieval_trace_s": scored["instrumentation"][
                                "retrieval_trace_s"
                            ],
                            "errors": scored["errors"],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )

        if tranche is not None:
            tranche_rows = [
                row
                for row in state["results"]
                if row["execution_id"] in set(tranche["execution_ids"])
            ]
            tranche["completed_at_utc"] = utc_now()
            tranche["actual_model_called"] = sum(
                1 for row in tranche_rows if not row.get("model_skipped")
            )
            tranche["budget_tokens_used"] = [
                int(frame["tokens_used"])
                for row in tranche_rows
                for frame in row["instrumentation"].get("budget_frames", [])
                if isinstance(frame.get("tokens_used"), int)
            ]
            _atomic_write(args.output, state)

        suite_order = {"frozen": 0, "temporal": 1}
        query_orders = {
            "frozen": {
                str(case["id"]): index for index, case in enumerate(frozen["queries"])
            },
            "temporal": {
                str(case["id"]): index for index, case in enumerate(temporal["queries"])
            },
        }
        tier_order = {tier: index for index, tier in enumerate(TIERS)}
        state["results"].sort(
            key=lambda row: (
                suite_order[row["suite"]],
                query_orders[row["suite"]][row["query_id"]],
                tier_order[row["tier"]],
            )
        )
        frozen_rows = [row for row in state["results"] if row["suite"] == "frozen"]
        temporal_rows = [row for row in state["results"] if row["suite"] == "temporal"]
        require(len(frozen_rows) <= 51, "frozen execution count overflow")
        require(len(temporal_rows) <= 24, "temporal execution count overflow")
        if len(frozen_rows) == 51:
            state["frozen_summary"] = _finalize(frozen, frozen_rows)
        if len(temporal_rows) == 24:
            state["temporal_summary"] = summarize_temporal(temporal, temporal_rows)
        _atomic_write(args.output, state)

        expected_for_suite = 51 if args.suite == "frozen" else 24
        completed_for_suite = (
            len(frozen_rows) if args.suite == "frozen" else len(temporal_rows)
        )
        if completed_for_suite < expected_for_suite:
            print(
                "INSTRUMENTED_PARTIAL "
                + json.dumps(
                    {
                        "suite": args.suite,
                        "completed": completed_for_suite,
                        "expected": expected_for_suite,
                        "remaining": expected_for_suite - completed_for_suite,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return 0

        if len(frozen_rows) != 51 or len(temporal_rows) != 24:
            print(
                "INSTRUMENTED_SUITE_DONE "
                + json.dumps(
                    {
                        "suite": args.suite,
                        "completed": completed_for_suite,
                        "summary": state[f"{args.suite}_summary"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            if args.suite == "frozen":
                return 0 if state["frozen_summary"]["passed"] else 1
            return 0

        mongo_after = mongo_index_snapshot(database)
        neo4j_after = neo4j_index_snapshot(settings)
        state["mongo_index_after"] = mongo_after
        state["neo4j_index_after"] = neo4j_after
        state["optimization_baseline"] = aggregate_instrumentation(
            state["results"],
            mongo_before=state["mongo_index_before"],
            mongo_after=mongo_after,
            neo4j_before=state["neo4j_index_before"],
            neo4j_after=neo4j_after,
        )
        state["completed_at_utc"] = utc_now()
        _atomic_write(args.output, state)
        print(
            json.dumps(
                {
                    "frozen_summary": state["frozen_summary"],
                    "temporal_summary": state["temporal_summary"],
                    "optimization_baseline": state["optimization_baseline"],
                },
                indent=2,
                sort_keys=True,
            ),
            flush=True,
        )
        return 0 if state["frozen_summary"]["passed"] else 1
    finally:
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
