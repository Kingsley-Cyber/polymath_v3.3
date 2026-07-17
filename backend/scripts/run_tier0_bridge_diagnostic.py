#!/usr/bin/env python3
"""Run the immutable six-query Tier-0 bridge diagnostic.

The scorer observes routed-document diagnostics from the real ``/api/chat``
path. It never scores the generated answer and never writes corpus data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from config import get_settings
from pymongo import MongoClient
from services.auth import auth_service
from services.secrets import decrypt


BRIDGE_PREREG_SHA256 = (
    "6c348cbf852a26e483ee810f6d3776ce1425955acc53ec4aede880f76dedc4b8"
)
SELECTION_SHA256 = "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00"
CORPUS_NAME = "runpod_e2e_15doc_20260715"
EXPECTED_QUERY_MODEL = "anthropic/minimax-m2.7"
EXPECTED_QUERY_PROVIDER = "opencode-go-anthropic"
EXPECTED_QUERY_BASE = "https://opencode.ai/zen/go"
EXPECTED_QUERY_POOL_SAFE_SHA256 = (
    "91bf6ceb54940ac467163624c3a92e2284f28cdbfda3863ccf4acc3671edadfe"
)
REQUIRED_ATTRIBUTION = (
    "lexical",
    "semantic",
    "child_rollup",
    "associative",
)
DIAGNOSTIC_TIER = "qdrant_mongo_graph"
DIAGNOSTIC_TOP_K = 10


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


def _load_hashed_json(path: Path, expected: str, label: str) -> dict[str, Any]:
    payload = path.read_bytes()
    require(hashlib.sha256(payload).hexdigest() == expected, f"{label} hash drifted")
    return json.loads(payload)


def _mint_token(database: Any, corpus: dict[str, Any]) -> str:
    user_id = str(corpus.get("user_id") or "")
    require(ObjectId.is_valid(user_id), "E2E corpus owner identity is invalid")
    user = database["users"].find_one(
        {"_id": ObjectId(user_id)},
        {"_id": 1, "username": 1},
    )
    require(bool(user and user.get("username")), "E2E corpus owner is absent")
    return auth_service.create_access_token(
        user_id=str(user["_id"]),
        username=str(user["username"]),
    )


def _query_route_preflight(database: Any, corpus: dict[str, Any]) -> dict[str, Any]:
    user_id = str(corpus.get("user_id") or "")
    row = (
        database["settings"].find_one(
            {"user_id": user_id},
            {"_id": 0, "models.query_model_pool": 1},
        )
        or {}
    )
    pool = list(((row.get("models") or {}).get("query_model_pool") or []))
    safe_pool = [
        {key: value for key, value in entry.items() if key != "api_key_ciphertext"}
        for entry in pool
        if isinstance(entry, dict)
    ]
    safe_hash = hashlib.sha256(
        json.dumps(
            safe_pool,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    enabled = [entry for entry in pool if entry.get("enabled", True)]
    require(safe_hash == EXPECTED_QUERY_POOL_SAFE_SHA256, "query pool hash drifted")
    require(bool(enabled), "query pool has no enabled entry")
    primary = enabled[0]
    require(
        str(primary.get("provider") or "") == EXPECTED_QUERY_PROVIDER,
        "query provider drifted",
    )
    require(
        str(primary.get("model_name") or "") == "minimax-m2.7",
        "query model drifted",
    )
    require(
        str(primary.get("base_url") or "") == EXPECTED_QUERY_BASE,
        "query base URL drifted",
    )
    ciphertext = primary.get("api_key_ciphertext")
    require(
        bool(ciphertext and decrypt(ciphertext)),
        "query credential is absent or undecryptable",
    )
    return {
        "pool_safe_sha256": safe_hash,
        "provider": EXPECTED_QUERY_PROVIDER,
        "model": EXPECTED_QUERY_MODEL,
        "base_url": EXPECTED_QUERY_BASE,
        "credential_ciphertext_present": True,
    }


def _run_sse(
    *,
    base: str,
    token: str,
    corpus_id: str,
    question: str,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/chat",
        data=json.dumps(
            {
                "message": question,
                "corpus_ids": [corpus_id],
                "retrieval_tier": DIAGNOSTIC_TIER,
                "overrides": {"final_top_k": DIAGNOSTIC_TOP_K, "temperature": 0},
            },
            separators=(",", ":"),
        ).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    started = time.perf_counter()
    current_event = ""
    traces: list[dict[str, Any]] = []
    errors: list[str] = []
    done: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            require(response.status == 200, f"chat HTTP status {response.status}")
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
                event_type = str(obj.get("type") or current_event)
                if event_type == "trace_event" or obj.get("trace_event"):
                    traces.append(dict(obj.get("trace_event") or obj))
                elif event_type == "error":
                    errors.append(
                        str(obj.get("content") or obj.get("error") or "")[:500]
                    )
                elif event_type == "done":
                    done = obj
    except urllib.error.HTTPError as exc:
        errors.append(
            f"HTTP {exc.code}: " + exc.read().decode("utf-8", errors="replace")[:500]
        )
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}"[:500])
    return {
        "traces": traces,
        "errors": errors,
        "done": done,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def _retrieval_diagnostics(traces: list[dict[str, Any]]) -> dict[str, Any]:
    for trace in reversed(traces):
        if trace.get("title") == "Local RAG retrieval":
            metadata = trace.get("metadata") or {}
            diagnostics = metadata.get("retrieval_diagnostics") or {}
            if diagnostics:
                return dict(diagnostics)
    return {}


def _resolved_models(traces: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str((trace.get("metadata") or {}).get("model") or "")
            for trace in traces
            if trace.get("title") == "Chat model route"
            and (trace.get("metadata") or {}).get("model")
        }
    )


def rank_routed_documents(
    routes_by_lane: dict[str, list[dict[str, Any]]],
    *,
    document_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Collapse per-query-plan lanes using max router score, then best rank."""

    by_document: dict[tuple[str, str], dict[str, Any]] = {}
    for lane_id in sorted(routes_by_lane):
        for index, route in enumerate(routes_by_lane[lane_id]):
            key = (
                str(route.get("corpus_id") or ""),
                str(route.get("doc_id") or ""),
            )
            if not all(key):
                continue
            score = float(route.get("score") or 0.0)
            current = by_document.setdefault(
                key,
                {
                    "corpus_id": key[0],
                    "doc_id": key[1],
                    "title": str(
                        (document_names or {}).get(key[1]) or route.get("title") or ""
                    ),
                    "max_score": score,
                    "best_lane_rank": index + 1,
                    "lane_ids": [],
                    "attribution": {},
                },
            )
            current["max_score"] = max(float(current["max_score"]), score)
            current["best_lane_rank"] = min(int(current["best_lane_rank"]), index + 1)
            current["lane_ids"].append(lane_id)
            trace = route.get("routing_trace") or {}
            if trace.get("router_version"):
                current["attribution"][lane_id] = {
                    "seat_owner": trace.get("seat_owner"),
                    "lane_scores": {
                        name: float((trace.get("lane_scores") or {}).get(name, 0.0))
                        for name in REQUIRED_ATTRIBUTION
                    },
                    "effective_lane_scores": {
                        name: float(
                            (trace.get("effective_lane_scores") or {}).get(name, 0.0)
                        )
                        for name in REQUIRED_ATTRIBUTION
                    },
                    "divergent_profile_demoted": bool(
                        trace.get("divergent_profile_demoted")
                    ),
                }
    return sorted(
        by_document.values(),
        key=lambda row: (
            -float(row["max_score"]),
            int(row["best_lane_rank"]),
            str(row["title"]),
            str(row["doc_id"]),
        ),
    )


def _score_case(
    *,
    case: dict[str, Any],
    raw: dict[str, Any],
    expect_router_enabled: bool,
    document_names: dict[str, str],
) -> dict[str, Any]:
    diagnostics = _retrieval_diagnostics(raw["traces"])
    document_routing = diagnostics.get("document_routing") or {}
    ranked = rank_routed_documents(
        document_routing.get("routes") or {},
        document_names=document_names,
    )
    top_three = ranked[:3]
    top_titles = [str(row["title"]) for row in top_three]
    expected = set(case.get("expected_title_any") or [])
    forbidden = set(
        case.get("forbidden_rank1")
        or ["Blain Brown - Cinematography - Theory and Practice (2016).md"]
    )
    expected_hit = bool(expected & set(top_titles))
    forbidden_rank1 = bool(top_titles and top_titles[0] in forbidden)
    attribution_complete = bool(
        any(row["attribution"] for row in ranked)
        and all(
            set(entry["lane_scores"]) == set(REQUIRED_ATTRIBUTION)
            and set(entry["effective_lane_scores"]) == set(REQUIRED_ATTRIBUTION)
            for row in ranked
            for entry in row["attribution"].values()
        )
    )
    router_version = str(document_routing.get("version") or "")
    technical_success = bool(
        not raw["errors"]
        and raw["done"]
        and diagnostics
        and (
            router_version == "four_lane_tier0_router.v1"
            if expect_router_enabled
            else router_version != "four_lane_tier0_router.v1"
        )
        and (
            attribution_complete
            if expect_router_enabled
            else not any(row["attribution"] for row in ranked)
        )
        and _resolved_models(raw["traces"]) == [EXPECTED_QUERY_MODEL]
    )
    return {
        "query_id": case["id"],
        "question": case["question"],
        "elapsed_seconds": raw["elapsed_seconds"],
        "errors": raw["errors"],
        "done_received": bool(raw["done"]),
        "resolved_models": _resolved_models(raw["traces"]),
        "router_version": router_version,
        "ranked_documents": ranked,
        "top_three_titles": top_titles,
        "expected_title_any": sorted(expected),
        "expected_hit_top_three": expected_hit,
        "forbidden_rank1": forbidden_rank1,
        "attribution_complete": attribution_complete,
        "technical_success": technical_success,
        "passed": technical_success and expected_hit and not forbidden_rank1,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arm", choices=("off", "on"), required=True)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()

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
    require(len(prereg.get("queries") or []) == 6, "bridge query count drifted")
    require(
        tuple((prereg.get("scoring") or {}).get("attribution_required") or ())
        == REQUIRED_ATTRIBUTION,
        "attribution contract drifted",
    )
    selected_titles = {str(row["filename"]) for row in selection["selected"]}
    require(len(selected_titles) == 15, "selection did not close at 15 titles")

    settings = get_settings()
    expect_router_enabled = args.arm == "on"
    require(
        bool(settings.FOUR_LANE_TIER0_ROUTER_ENABLED) is expect_router_enabled,
        "runtime router flag does not match requested arm",
    )
    require(
        bool(settings.FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED)
        is expect_router_enabled,
        "runtime decomposition flag does not match requested arm",
    )
    mongo = MongoClient(settings.MONGODB_URI)
    database = mongo[settings.MONGODB_DATABASE]
    try:
        corpora = list(
            database["corpora"].find(
                {"name": CORPUS_NAME, "status": {"$ne": "deleted"}},
                {"_id": 0, "corpus_id": 1, "user_id": 1, "name": 1},
            )
        )
        require(len(corpora) == 1, "fresh E2E corpus discovery was not unique")
        corpus = corpora[0]
        corpus_id = str(corpus["corpus_id"])
        active_documents = list(
            database["documents"].find(
                {"corpus_id": corpus_id, "status": {"$ne": "deleted"}},
                {"_id": 0, "doc_id": 1, "original_filename": 1, "filename": 1},
            )
        )
        require(len(active_documents) == 15, "E2E document count drifted")
        document_names = {
            str(row.get("doc_id") or ""): str(
                row.get("original_filename") or row.get("filename") or ""
            )
            for row in active_documents
        }
        actual_titles = {title for title in document_names.values() if title}
        require(actual_titles == selected_titles, "E2E document selection drifted")
        route_preflight = _query_route_preflight(database, corpus)
        token = _mint_token(database, corpus)
        results = []
        for case in prereg["queries"]:
            print(f"BRIDGE_START {case['id']}::{args.arm}", flush=True)
            raw = _run_sse(
                base=args.base,
                token=token,
                corpus_id=corpus_id,
                question=case["question"],
            )
            scored = _score_case(
                case=case,
                raw=raw,
                expect_router_enabled=expect_router_enabled,
                document_names=document_names,
            )
            results.append(scored)
            print(
                "BRIDGE_DONE "
                + json.dumps(
                    {
                        "query_id": scored["query_id"],
                        "top_three_titles": scored["top_three_titles"],
                        "attribution_complete": scored["attribution_complete"],
                        "passed": scored["passed"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        passed = all(row["passed"] for row in results)
        artifact = {
            "schema_version": "polymath.tier0_bridge_diagnostic_results.v1",
            "started_and_completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "arm": args.arm,
            "corpus_id": corpus_id,
            "corpus_name": CORPUS_NAME,
            "preregistration_sha256": BRIDGE_PREREG_SHA256,
            "selection_sha256": SELECTION_SHA256,
            "query_route_preflight": route_preflight,
            "runtime": {
                "four_lane_tier0_router_enabled": bool(
                    settings.FOUR_LANE_TIER0_ROUTER_ENABLED
                ),
                "subquery_decomposition_enabled": bool(
                    settings.FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED
                ),
                "tier": DIAGNOSTIC_TIER,
                "top_k": DIAGNOSTIC_TOP_K,
            },
            "results": results,
            "summary": {
                "execution_count": len(results),
                "technical_success_count": sum(
                    1 for row in results if row["technical_success"]
                ),
                "passed_count": sum(1 for row in results if row["passed"]),
                "pass_rate": (
                    sum(1 for row in results if row["passed"]) / len(results)
                    if results
                    else 0.0
                ),
                "passed": passed,
            },
        }
        _atomic_write(args.output, artifact)
        print(json.dumps(artifact["summary"], sort_keys=True), flush=True)
        return 0 if passed else 1
    finally:
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
