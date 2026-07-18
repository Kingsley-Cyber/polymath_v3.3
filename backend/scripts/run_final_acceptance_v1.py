#!/usr/bin/env python3
"""Run the single preregistered COMPLETE-pipeline final acceptance window.

The caller owns the host eval lock and deploy transaction. This runner executes
inside the canonical backend container against its loopback API, discovers the
E2E corpus by its durable name, freezes the exact 23-query selection, journals
every execution, and performs the synthesis-free #1-#5 determinism repeat.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from bson import ObjectId
from config import get_settings
from evals.canonical_refusal_contract import (
    classify_refusal,
    validate_chat_trace_contract,
)
from pymongo import MongoClient
from services.auth import auth_service
from services.chat_cost_meter import (
    CHAT_COST_TRACE_TITLE,
    _route_for,
    _safe_route_base,
    aggregate_chat_cost_ledgers,
)
from services.provider_presets import normalize_model_for_litellm

from scripts.run_canonical_heldout_negative_eval import (
    _atomic_write,
    _canonical_bytes,
    _embedder_preflight,
    _prompt_template_receipt,
    _seal_journal,
    _sha256_bytes,
    _utc_now,
    _validate_local_api,
    _validate_same_container_runtime,
)


BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
MANIFEST_PATH = BACKEND / "evals/final_acceptance_set_v1_20260718.json"
FINAL_SPEC_PATH = REPO / "docs/FINAL_ACCEPTANCE_SET_V1_2026-07-18.md"
DEPTH_SPEC_PATH = REPO / "docs/DEEP_RETRIEVAL_DEPTH_PROBE_SPEC_2026-07-17.md"
RETRIEVAL_SPEC_PATH = BACKEND / "evals/runpod_e2e_retrieval_preregister_v1.json"
NEGATIVE_SPEC_PATH = BACKEND / "evals/e2e_heldout_negative_v2_20260717.json"
CORPUS_SELECTION_PATH = BACKEND / "evals/runpod_e2e_15doc_selection_v1.json"

MANIFEST_SHA256 = "abdc68bb937c2c47c88eeafe918e19cbb462cf44ca9c2ec56e5ae351a6d8eac5"
FINAL_SPEC_SHA256 = "3ffec2b1b4de8cd2432ff3a52d3baa42935fa828d0f91564d2f6476d91a3d737"
DEPTH_SPEC_SHA256 = "2c147676a4fa5dded07f813b64376997e640945e8b06990e5dc892a972cecf7e"
RETRIEVAL_SPEC_SHA256 = (
    "8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110"
)
NEGATIVE_SPEC_SHA256 = (
    "3b35c14c165f6be89202b809ea01a1cd6ad0f5c0217e4167b86e4b5dc0b09960"
)
CORPUS_SELECTION_SHA256 = (
    "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00"
)

CORPUS_NAME = "runpod_e2e_15doc_20260715"
CONCURRENCY = 3
TOP_K = 8
TEMPERATURE = 0.0
EXPECTED_EXECUTIONS = 23
REFINEMENT_DEPTH_ORDINALS = (1, 2, 5, 6)
REFINEMENT_SIMPLE_ORDINALS = (13, 14, 15, 16)
DETERMINISM_IDS = (
    "d1a_anticipation_editing_tension",
    "d1b_guiding_eye_drawing_cinematography",
    "d2a_facs_character_animation",
    "d2b_laban_stage_combat",
    "d6a_story_directing_cinematography_vfx",
)
JOURNAL_SCHEMA = "polymath.complete_pipeline_final_acceptance.v1"
EXECUTION_SCHEMA = "polymath.complete_pipeline_final_execution.v1"


def _expected_state_receipt(
    case: dict[str, Any],
    classification: dict[str, Any],
) -> tuple[str, bool]:
    """Match the frozen refusal intent to the canonical three-state contract."""

    is_refusal = str(case.get("class") or "").startswith("refusal_")
    if is_refusal:
        return "refused", classification.get("refused") is True
    return "answered", classification.get("state") == "answered"


def _repeat_librarian_controls(settings: Any, *, user_id: str) -> dict[str, Any]:
    """Keep the repeat on the exact Librarian/refinement feature path."""

    enabled = bool(getattr(settings, "LIBRARIAN_LLM_DECOMPOSER_ENABLED", False))
    return {
        "llm_decomposer_enabled": enabled,
        "librarian_refinement_enabled": enabled,
        "librarian_refinement_user_id": user_id,
    }


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _load_hashed_json(path: Path, expected: str, label: str) -> dict[str, Any]:
    payload = path.read_bytes()
    actual = _sha256_bytes(payload)
    require(actual == expected, f"{label} hash drifted: {actual} != {expected}")
    value = json.loads(payload)
    require(isinstance(value, dict), f"{label} must be a JSON object")
    return value


def _assert_file_hash(path: Path, expected: str, label: str) -> None:
    actual = _sha256_bytes(path.read_bytes())
    require(actual == expected, f"{label} hash drifted: {actual} != {expected}")


def _selection_surface(manifest: dict[str, Any]) -> dict[str, Any]:
    queries = list(manifest.get("queries") or [])
    require(len(queries) == EXPECTED_EXECUTIONS, "final selection must contain 23 rows")
    require(
        [int(row.get("ordinal") or 0) for row in queries]
        == list(range(1, EXPECTED_EXECUTIONS + 1)),
        "final selection ordinals drifted",
    )
    ids = [str(row.get("id") or "") for row in queries]
    require(all(ids) and len(ids) == len(set(ids)), "final selection IDs drifted")
    require(
        tuple(ids[:5]) == DETERMINISM_IDS,
        "final determinism surface must be query ordinals 1-5",
    )
    require(
        all(
            str(row.get("retrieval_tier") or "")
            in {"qdrant_only", "qdrant_mongo_graph"}
            for row in queries
        ),
        "final selection contains an unsupported retrieval tier",
    )
    exact = [
        {
            "ordinal": int(row["ordinal"]),
            "id": str(row["id"]),
            "question": str(row["question"]),
            "retrieval_tier": str(row["retrieval_tier"]),
            "latency_tier": str(row["latency_tier"]),
        }
        for row in queries
    ]
    return {
        "name": str(manifest.get("name") or ""),
        "query_count": len(queries),
        "query_ids": ids,
        "query_id_sha256": _sha256_bytes(_canonical_bytes(ids)),
        "exact_query_surface_sha256": _sha256_bytes(_canonical_bytes(exact)),
        "queries": queries,
    }


def _discover_runtime(
    expected_synthesis_entry_id: str,
) -> tuple[MongoClient, Any, dict[str, Any], str, str, dict[str, Any]]:
    settings = get_settings()
    client = MongoClient(settings.MONGODB_URI)
    database = client[settings.MONGODB_DATABASE]
    corpora = list(
        database.corpora.find(
            {"name": CORPUS_NAME, "status": {"$ne": "deleted"}},
            {"_id": 0, "corpus_id": 1, "name": 1, "user_id": 1, "status": 1},
        )
    )
    require(len(corpora) == 1, f"expected one active {CORPUS_NAME!r} corpus")
    corpus = corpora[0]
    corpus_id = str(corpus.get("corpus_id") or "")
    user_id = str(corpus.get("user_id") or "")
    require(corpus_id, "discovered corpus has no corpus_id")
    require(ObjectId.is_valid(user_id), "discovered corpus owner is invalid")
    user = database.users.find_one(
        {"_id": ObjectId(user_id)},
        {"_id": 1, "username": 1},
    )
    require(bool(user and user.get("username")), "discovered corpus owner is absent")
    token = auth_service.create_access_token(
        user_id=str(user["_id"]),
        username=str(user["username"]),
    )

    stored = (
        database.settings.find_one(
            {"user_id": user_id},
            {
                "_id": 0,
                "models.synthesis": 1,
                "models.query_model_pool": 1,
            },
        )
        or {}
    )
    models = dict(stored.get("models") or {})
    role_entry_id = str((models.get("synthesis") or {}).get("pool_entry_id") or "")
    require(
        role_entry_id == expected_synthesis_entry_id,
        "configured synthesis role does not match the preregistered candidate",
    )
    pool = [
        row for row in (models.get("query_model_pool") or []) if isinstance(row, dict)
    ]
    matches = [row for row in pool if str(row.get("entry_id") or "") == role_entry_id]
    require(len(matches) == 1, "synthesis candidate pool entry is absent or duplicated")
    entry = matches[0]
    require(entry.get("enabled", True) is True, "synthesis candidate is disabled")
    provider = str(entry.get("provider") or "")
    model_name = str(entry.get("model_name") or "")
    expected_model = normalize_model_for_litellm(provider, model_name)
    require(expected_model, "synthesis candidate has no model identity")
    safe_base_url = _safe_route_base(entry.get("base_url"))
    price_route, price_registry_sha256 = _route_for(
        expected_model,
        entry.get("base_url"),
    )
    require(
        price_route is not None,
        "synthesis candidate has no registered cost route; refusing UNKNOWN cost",
    )
    credential_present = bool(entry.get("api_key_ciphertext"))
    credential_reference = entry.get("credential_ref")
    if isinstance(credential_reference, dict):
        reference_provider = str(credential_reference.get("provider") or "").strip()
        reference_user_id = str(
            credential_reference.get("settings_user_id") or ""
        ).strip()
        if (
            credential_reference.get("kind") == "settings_api_key.v1"
            and credential_reference.get("scope") == "system"
            and reference_provider == provider
            and reference_user_id
        ):
            credential_present = bool(
                database.settings.find_one(
                    {
                        "user_id": reference_user_id,
                        f"api_keys.{provider}": {"$exists": True, "$ne": ""},
                    },
                    {"_id": 1},
                )
            )
    if not credential_present:
        credential_present = bool(
            (
                database.settings.find_one(
                    {"user_id": user_id},
                    {f"api_keys.{provider}": 1, "_id": 0},
                )
                or {}
            )
            .get("api_keys", {})
            .get(provider)
        )
    require(
        credential_present,
        "synthesis candidate credential reference is absent or dangling",
    )
    safe_entry = {
        "entry_id": role_entry_id,
        "provider": provider,
        "model_name": model_name,
        "route_model": expected_model,
        "base_url": safe_base_url,
        "enabled": entry.get("enabled", True),
        "credential_present": credential_present,
        "price_route_id": price_route.get("route_id"),
        "price_registry_sha256": price_registry_sha256,
    }
    return client, database, corpus, token, expected_model, safe_entry


def _runtime_flags(*, expected_two_lane: bool) -> dict[str, bool]:
    settings = get_settings()
    expected = {
        "QUERY_PLAN_V2": True,
        "NEO4J_ENABLED": True,
        "RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED": True,
        "ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED": True,
        "ANSWERABILITY_CORPUS_SCOPE_V3_ENABLED": True,
        "TEMPORAL_QUERY_ROUTING_ENABLED": True,
        "ATOMIC_CLAIM_ANCHORS_ENABLED": True,
        "LIBRARIAN_PLANNER_ENABLED": True,
        "LIBRARIAN_PLANNER_SHADOW": False,
        "LIBRARIAN_LLM_DECOMPOSER_ENABLED": True,
        "SYNTHESIS_ROUTE_OVERRIDE_ENABLED": True,
        "TWO_LANE_ANCHORING_ENABLED": expected_two_lane,
        "FOUR_LANE_TIER0_ROUTER_ENABLED": False,
        "FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED": False,
        "WATERFALL_ASSEMBLY": False,
        "RERANK_EVIDENCE_SUPPORT": False,
        "PARENT_EXCERPT_ENABLED": False,
        "HYDE_ENABLED": False,
        "SHELF_RESERVE_ENABLED": False,
        "GROUNDED_QUERY_PLANNER_ENABLED": False,
        "AGENTIC_MODE_ENABLED": False,
        "CHAT_COST_TELEMETRY_ENABLED": True,
    }
    observed = {name: bool(getattr(settings, name)) for name in expected}
    require(
        observed == expected,
        "runtime flags do not match final acceptance contract: "
        + json.dumps({"expected": expected, "observed": observed}, sort_keys=True),
    )
    return observed


def _http_json(url: str, token: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def _verify_corpus(
    api: str,
    token: str,
    corpus_id: str,
    expected_filenames: set[str],
) -> tuple[dict[str, str], dict[str, Any]]:
    corpora = _http_json(f"{api.rstrip('/')}/api/corpora", token)
    matched = [
        row
        for row in corpora
        if isinstance(row, dict) and str(row.get("corpus_id") or "") == corpus_id
    ]
    require(
        len(matched) == 1 and str(matched[0].get("name") or "") == CORPUS_NAME,
        "API corpus identity does not match discovered durable state",
    )
    documents = _http_json(
        f"{api.rstrip('/')}/api/corpora/{urllib.parse.quote(corpus_id)}/documents",
        token,
    )
    require(isinstance(documents, list), "documents endpoint did not return a list")
    names = {
        str(row.get("doc_id") or ""): str(
            row.get("original_filename") or row.get("filename") or ""
        )
        for row in documents
        if isinstance(row, dict)
    }
    require(
        len(names) == 15 and all(names),
        f"final corpus must expose 15 durable documents, got {len(names)}",
    )
    require(
        set(names.values()) == expected_filenames,
        "live corpus filenames drifted from the frozen 15-document selection",
    )
    return names, {
        "corpus_id": corpus_id,
        "corpus_name": CORPUS_NAME,
        "document_count": len(names),
        "document_identity_sha256": _sha256_bytes(
            _canonical_bytes(sorted(names.items()))
        ),
    }


def _request_payload(case: dict[str, Any], corpus_id: str) -> dict[str, Any]:
    return {
        "message": str(case["question"]),
        "corpus_ids": [corpus_id],
        "retrieval_tier": str(case["retrieval_tier"]),
        "overrides": {
            "retrieval_k": TOP_K,
            "final_top_k": TOP_K,
            "temperature": TEMPERATURE,
            "hyde_enabled": False,
            "agentic_mode": False,
        },
    }


def _run_sse(
    *,
    api: str,
    token: str,
    case: dict[str, Any],
    corpus_id: str,
    timeout: float,
) -> dict[str, Any]:
    payload = _request_payload(case, corpus_id)
    request = urllib.request.Request(
        f"{api.rstrip('/')}/api/chat",
        data=_canonical_bytes(payload),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    started = time.monotonic()
    first_token_s: float | None = None
    answer_parts: list[str] = []
    sources: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    errors: list[str] = []
    done_events: list[dict[str, Any]] = []
    event_counts: Counter[str] = Counter()
    conversation_ids: set[str] = set()
    current_event = ""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            require(response.status == 200, f"chat HTTP status {response.status}")
            require(
                "text/event-stream" in str(response.headers.get("Content-Type") or ""),
                "chat response was not SSE",
            )
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    current_event = line.split(":", 1)[1].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                encoded = line[5:].strip()
                if not encoded or encoded == "[DONE]":
                    continue
                try:
                    event = json.loads(encoded)
                except json.JSONDecodeError as exc:
                    errors.append(f"invalid SSE JSON: {exc}")
                    continue
                event_type = str(event.get("type") or current_event or "unknown")
                event_counts[event_type] += 1
                conversation_id = str(event.get("conversation_id") or "")
                if conversation_id:
                    conversation_ids.add(conversation_id)
                if event_type == "token":
                    if first_token_s is None:
                        first_token_s = round(time.monotonic() - started, 3)
                    answer_parts.append(
                        str(event.get("content") or event.get("token") or "")
                    )
                elif event_type == "sources":
                    value = event.get("sources") or event.get("data") or []
                    sources = value if isinstance(value, list) else []
                elif event_type == "trace_event" or event.get("trace_event"):
                    traces.append(dict(event.get("trace_event") or event))
                elif event_type == "error":
                    errors.append(
                        str(event.get("content") or event.get("error") or "")[:500]
                    )
                elif event_type == "done":
                    done_events.append(event)
    except urllib.error.HTTPError as exc:
        errors.append(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}")
    except Exception as exc:  # noqa: BLE001 - durable technical receipt
        errors.append(f"{type(exc).__name__}: {exc}"[:500])
    return {
        "payload": payload,
        "answer": "".join(answer_parts),
        "sources": sources,
        "traces": traces,
        "errors": errors,
        "done_events": done_events,
        "event_counts": dict(sorted(event_counts.items())),
        "conversation_ids": sorted(conversation_ids),
        "first_token_seconds": first_token_s,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _trace(
    traces: Sequence[dict[str, Any]],
    title: str,
    *,
    status: str | None = None,
) -> dict[str, Any]:
    for row in reversed(traces):
        if row.get("title") != title:
            continue
        if status is not None and row.get("status") != status:
            continue
        metadata = row.get("metadata")
        return dict(metadata) if isinstance(metadata, dict) else {}
    return {}


def _safe_trace_receipt(traces: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "lane": row.get("lane"),
            "title": row.get("title"),
            "status": row.get("status"),
            "content": str(row.get("content") or "")[:600],
            "metadata": row.get("metadata")
            if isinstance(row.get("metadata"), dict)
            else {},
        }
        for row in traces
    ]


def _source_receipt(
    sources: Sequence[dict[str, Any]],
    *,
    corpus_id: str,
    document_names: dict[str, str],
    selected_filenames: set[str],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for source in sources:
        doc_id = str(source.get("doc_id") or "")
        filename = str(
            document_names.get(doc_id)
            or source.get("doc_name")
            or source.get("filename")
            or ""
        )
        metadata = (
            dict(source.get("metadata") or {})
            if isinstance(source.get("metadata"), dict)
            else {}
        )
        items.append(
            {
                "corpus_id": source.get("corpus_id"),
                "doc_id": doc_id,
                "doc_name": filename,
                "chunk_id": source.get("chunk_id"),
                "parent_id": source.get("parent_id"),
                "score": source.get("score"),
                "source_tier": source.get("source_tier"),
                "chunk_kind": source.get("chunk_kind") or metadata.get("chunk_kind"),
                "hydration_level": metadata.get("hydration_level"),
                "heading_path": source.get("heading_path"),
                "provenance": source.get("provenance") or [],
                "atomic_claim_anchors": metadata.get("atomic_claim_anchors") or [],
                "selected_corpus_member": (
                    str(source.get("corpus_id") or "") == corpus_id
                    and doc_id in document_names
                    and filename in selected_filenames
                ),
            }
        )
    return {
        "count": len(items),
        "membership_count": sum(row["selected_corpus_member"] is True for row in items),
        "all_in_selected_corpus": all(
            row["selected_corpus_member"] is True for row in items
        ),
        "items": items,
    }


def _normalize_filename(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _expected_group_score(
    case: dict[str, Any],
    sources: dict[str, Any],
) -> dict[str, Any]:
    returned = {
        _normalize_filename(row.get("doc_name"))
        for row in sources.get("items") or []
        if row.get("doc_name")
    }
    groups = [
        {_normalize_filename(value) for value in group}
        for group in (case.get("expected_groups") or [])
    ]
    group_hits = [sorted(group & returned) for group in groups]
    return {
        "expected_groups": [sorted(group) for group in groups],
        "returned_documents": sorted(returned),
        "group_hits": group_hits,
        "groups_hit": sum(bool(hits) for hits in group_hits),
        "groups_required": len(groups),
        "all_groups_hit": all(bool(hits) for hits in group_hits),
    }


def _recursive_values(value: Any, key: str) -> Iterable[Any]:
    if isinstance(value, dict):
        for current_key, current_value in value.items():
            if current_key == key:
                yield current_value
            yield from _recursive_values(current_value, key)
    elif isinstance(value, list):
        for current_value in value:
            yield from _recursive_values(current_value, key)


def _seat_surface(retrieval_diagnostics: dict[str, Any]) -> dict[str, Any]:
    reservations = dict(retrieval_diagnostics.get("reservations") or {})
    return {
        "lane_quota_reservation_refs": reservations.get("lane_quota_reservation_refs")
        or {},
        "protected_lane_reservation_refs": reservations.get(
            "protected_lane_reservation_refs"
        )
        or {},
        "routed_document_refs_selected": reservations.get(
            "routed_document_refs_selected"
        )
        or [],
        "selected_candidates": reservations.get("selected_candidates"),
    }


def _schema_proofs(
    case: dict[str, Any],
    *,
    traces: Sequence[dict[str, Any]],
    sources: dict[str, Any],
    expected: dict[str, Any],
) -> dict[str, Any]:
    query_plan = _trace(traces, "Query plan", status="done")
    retrieval = _trace(traces, "Local RAG retrieval", status="done")
    retrieval_diagnostics = dict(retrieval.get("retrieval_diagnostics") or {})
    librarian_trace = dict(query_plan.get("librarian_query_plan") or {})
    librarian_plan = dict(librarian_trace.get("plan") or {})
    librarian_diagnostics = dict(librarian_trace.get("diagnostics") or {})
    shortlist_diagnostics = dict(librarian_diagnostics.get("shortlist") or {})
    librarian_execution = dict(retrieval_diagnostics.get("librarian_execution") or {})
    librarian_refinement = dict(librarian_execution.get("refinement") or {})
    librarian_refinement_second_pass = dict(
        librarian_refinement.get("second_pass") or {}
    )
    temporal = dict(retrieval_diagnostics.get("temporal_routing") or {})
    claim = _trace(traces, "Atomic claim anchors")
    if not claim:
        claim = dict(retrieval.get("atomic_claim_anchors") or {})
    answerability = _trace(traces, "Answerability gate")
    guard = dict(answerability.get("corpus_scope_v3_guard") or {})
    source_tiers = [
        str(row.get("source_tier") or "").casefold()
        for row in sources.get("items") or []
    ]
    chunk_kinds = [
        str(row.get("chunk_kind") or "").casefold()
        for row in sources.get("items") or []
    ]
    graph_contributed = any(
        "graph" in value or "neo4j" in value for value in source_tiers
    ) or any(
        bool(row.get("provenance")) and "graph" in str(row.get("source_tier") or "")
        for row in sources.get("items") or []
    )
    temporal_matches = [
        value
        for value in _recursive_values(temporal, "matched_candidates")
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    exact_surfaces = [
        str(item)
        for value in _recursive_values(temporal, "exact_surfaces")
        if isinstance(value, list)
        for item in value
    ]
    lane_refs = (retrieval_diagnostics.get("reservations") or {}).get(
        "lane_quota_reservation_refs"
    ) or {}
    filled_lanes = sum(bool(values) for values in lane_refs.values())
    shortlist_doc_ids = {
        str(row.get("doc_id") or "")
        for row in librarian_plan.get("shortlist") or []
        if isinstance(row, dict)
    }
    returned_doc_ids = {
        str(row.get("doc_id") or "")
        for row in sources.get("items") or []
        if row.get("doc_id")
    }
    target_doc_ids = {
        str(doc_id)
        for row in librarian_plan.get("subqueries") or []
        if isinstance(row, dict)
        for doc_id in row.get("target_doc_ids") or []
    }
    shortlist_profile_doc_ids = {
        str(row.get("doc_id") or "")
        for row in librarian_plan.get("shortlist") or []
        if isinstance(row, dict)
        and str(row.get("doc_id") or "")
        and str(row.get("summary") or "").strip()
    }
    consumed_shortlist_profile_doc_ids = (
        shortlist_profile_doc_ids & target_doc_ids & returned_doc_ids
    )
    associative_hits = [
        value
        for value in _recursive_values(
            retrieval_diagnostics.get("document_routing") or {},
            "associative",
        )
        if isinstance(value, dict) and value
    ]
    t91_profile_ids = [
        str(item)
        for value in _recursive_values(
            retrieval_diagnostics.get("document_routing") or {},
            "t91_profile_ids",
        )
        if isinstance(value, list)
        for item in value
        if str(item)
    ]
    proof = str(case.get("schema_proof") or "")
    proof_pass = expected["all_groups_hit"]
    if proof in {"per_side_seats", "entity_bridge_seats", "minimum_distinct_two"}:
        proof_pass = proof_pass and filled_lanes >= 2
    elif proof == "three_expected_groups_seated":
        proof_pass = proof_pass and expected["groups_hit"] >= 3
    elif proof == "rendered_claim_anchor":
        proof_pass = proof_pass and int(claim.get("prompt_render_count") or 0) >= 1
    elif proof == "list_or_table_hydration":
        proof_pass = proof_pass and any(
            value in {"list", "table"} for value in chunk_kinds
        )
    elif proof in {"temporal_preferred_evidence", "time_expression_match"}:
        proof_pass = (
            proof_pass
            and temporal.get("active") is True
            and any(value > 0 for value in temporal_matches)
        )
        if proof == "time_expression_match":
            proof_pass = proof_pass and any(
                "2006" in value.casefold() for value in exact_surfaces
            )
    elif proof == "graph_contributed_source":
        proof_pass = proof_pass and graph_contributed
    elif proof == "author_title_anchor":
        proof_pass = (
            proof_pass
            and bool(shortlist_doc_ids & returned_doc_ids)
            and bool(target_doc_ids & returned_doc_ids)
            and not bool(guard.get("applied"))
        )
    elif proof == "v3_named_source_positive":
        named = dict(guard.get("named_source") or {})
        proof_pass = (
            proof_pass
            and not bool(guard.get("applied"))
            and bool(named.get("eligible"))
            and not bool(named.get("missing"))
        )
    elif proof == "named_v3_guard":
        proof_pass = str(case.get("expected_guard") or "") in set(
            guard.get("blocking_reason_codes") or []
        )
    elif proof == "bait_stripped_refusal":
        proof_pass = str(case.get("expected_guard") or "") in set(
            guard.get("blocking_reason_codes") or []
        ) and bool((guard.get("bait") or {}).get("stripped"))

    return {
        "proof_name": proof,
        "proof_pass": bool(proof_pass),
        "librarian": {
            "mode": librarian_trace.get("mode"),
            "behavior_applied": librarian_trace.get("behavior_applied"),
            "plan_hash": librarian_plan.get("plan_hash"),
            "shape": librarian_plan.get("shape"),
            "shortlist_doc_ids": sorted(shortlist_doc_ids),
            "target_doc_ids": sorted(target_doc_ids),
            "execution_active": librarian_execution.get("active"),
            "filled_lane_count": filled_lanes,
            "seat_surface": _seat_surface(retrieval_diagnostics),
        },
        "refinement": {
            "enabled": librarian_refinement.get("enabled"),
            "fired": librarian_refinement.get("fired"),
            "status": librarian_refinement.get("status"),
            "reason": librarian_refinement.get("reason"),
            "round": librarian_refinement.get("round"),
            "gap_count": len(librarian_refinement.get("gaps") or []),
            "second_pass_attempted": librarian_refinement_second_pass.get("attempted"),
            "improved_seating": librarian_refinement_second_pass.get(
                "improved_seating"
            ),
            "remaining_gap_count": len(
                librarian_refinement_second_pass.get("remaining_gaps") or []
            ),
            "planner_refinement_unavailable": librarian_refinement.get(
                "planner_refinement_unavailable"
            ),
            "silent_fallback_count": int(
                librarian_refinement.get("silent_fallback_count") or 0
            ),
        },
        "associative_profile": {
            "associative_hit_count": len(associative_hits),
            "t91_profile_ids": sorted(set(t91_profile_ids)),
            "shortlist_mode": shortlist_diagnostics.get("mode"),
            "shortlist_lanes": shortlist_diagnostics.get("lanes") or [],
            "consumed_shortlist_profile_doc_ids": sorted(
                consumed_shortlist_profile_doc_ids
            ),
            "consumed": bool(
                associative_hits
                or t91_profile_ids
                or consumed_shortlist_profile_doc_ids
            ),
        },
        "graph": {
            "source_tiers": source_tiers,
            "graph_contributed_source": graph_contributed,
            "facts_used": int(
                ((retrieval_diagnostics.get("graph_evidence") or {}).get("facts_used"))
                or 0
            ),
        },
        "claims": {
            "anchors_attached": int(claim.get("anchors_attached") or 0),
            "prompt_render_count": int(claim.get("prompt_render_count") or 0),
            "additivity_verified": claim.get("additivity_verified"),
        },
        "chunk_kind": {
            "selected_kinds": chunk_kinds,
            "list_or_table_selected": any(
                value in {"list", "table"} for value in chunk_kinds
            ),
        },
        "temporal": {
            "active": temporal.get("active"),
            "matched_candidate_counts": temporal_matches,
            "exact_surfaces": exact_surfaces,
        },
        "answerability_guard": {
            "policy_version": answerability.get("answerability_policy_version"),
            "applied": guard.get("applied"),
            "decision": guard.get("decision"),
            "reason_codes": guard.get("reason_codes") or [],
            "blocking_reason_codes": guard.get("blocking_reason_codes") or [],
            "bait": guard.get("bait") or {},
            "named_source": guard.get("named_source") or {},
        },
    }


def _build_execution(
    *,
    case: dict[str, Any],
    raw: dict[str, Any],
    ordinal: int,
    process_run_id: str,
    prompt_receipt: dict[str, Any],
    expected_model: str,
    expected_entry_id: str,
    corpus_id: str,
    document_names: dict[str, str],
    selected_filenames: set[str],
) -> dict[str, Any]:
    answer = str(raw["answer"])
    sources = _source_receipt(
        raw["sources"],
        corpus_id=corpus_id,
        document_names=document_names,
        selected_filenames=selected_filenames,
    )
    trace_contract = validate_chat_trace_contract(
        raw["traces"],
        raw["done_events"],
        expected_model=expected_model,
    )
    model_route = _trace(raw["traces"], "Chat model route", status="done")
    synthesis_route = dict(model_route.get("synthesis_route") or {})
    answerability = _trace(raw["traces"], "Answerability gate")
    retrieval = _trace(raw["traces"], "Local RAG retrieval", status="done")
    effective_tier = str(retrieval.get("effective_tier") or "")
    cost_ledger_meta = _trace(raw["traces"], CHAT_COST_TRACE_TITLE)
    cost_ledger = dict(cost_ledger_meta.get("chat_cost_ledger") or {})
    classification = classify_refusal(
        answer,
        model_skipped=trace_contract.get("model_skipped") is True,
    )
    expected = _expected_group_score(case, sources)
    proofs = _schema_proofs(
        case,
        traces=raw["traces"],
        sources=sources,
        expected=expected,
    )
    technical_errors = list(raw["errors"])
    technical_errors.extend(trace_contract.get("errors") or [])
    if len(raw["done_events"]) != 1:
        technical_errors.append(
            f"SSE done event count must be 1, observed {len(raw['done_events'])}"
        )
    if effective_tier != str(case["retrieval_tier"]):
        technical_errors.append(
            f"effective tier mismatch: {effective_tier!r} "
            f"!= {case['retrieval_tier']!r}"
        )
    if not answerability:
        technical_errors.append("missing Answerability gate trace")
    if not retrieval:
        technical_errors.append("missing completed retrieval trace")
    if synthesis_route.get("applied") is not True:
        technical_errors.append("synthesis candidate route was not applied")
    if synthesis_route.get("candidate_entry_id") != expected_entry_id:
        technical_errors.append("synthesis candidate entry identity drifted")
    if synthesis_route.get("candidate_model") != expected_model:
        technical_errors.append("synthesis candidate model identity drifted")
    if cost_ledger.get("accounting_state") != "CLOSED":
        technical_errors.append("chat cost ledger is absent or OPEN")
    if not sources["all_in_selected_corpus"]:
        technical_errors.append("source escaped preregistered corpus selection")
    if trace_contract.get("model_skipped") is not True and not answer.strip():
        technical_errors.append("model-called response has empty answer")

    expected_state, state_ok = _expected_state_receipt(case, classification)
    row = {
        "schema_version": EXECUTION_SCHEMA,
        "execution_id": f"{case['id']}::{case['retrieval_tier']}",
        "query_id": str(case["id"]),
        "ordinal": ordinal,
        "class": case.get("class"),
        "latency_tier": case.get("latency_tier"),
        "question": str(case["question"]),
        "question_sha256": _sha256_bytes(str(case["question"]).encode()),
        "request": {
            "payload_sha256": _sha256_bytes(_canonical_bytes(raw["payload"])),
            "corpus_id": corpus_id,
            "tier": case["retrieval_tier"],
            "temperature": TEMPERATURE,
            "top_k": TOP_K,
            "conversation_id_sent": "conversation_id" in raw["payload"],
        },
        "prior_call_state": {
            "process_run_id": process_run_id,
            "request_ordinal": ordinal,
            "prior_call_count": ordinal - 1,
            "history_turn_count_sent": 0,
            "session_mode": "fresh_conversation_per_probe_concurrent_process",
            "concurrency": CONCURRENCY,
            "returned_conversation_ids": raw["conversation_ids"],
        },
        "transport": {
            "done_received": bool(raw["done_events"]),
            "done_event_count": len(raw["done_events"]),
            "errors": list(raw["errors"]),
            "event_counts": raw["event_counts"],
            "first_token_seconds": raw["first_token_seconds"],
            "elapsed_seconds": raw["elapsed_seconds"],
            "effective_tier": effective_tier,
        },
        "answerability": answerability,
        "classification": classification,
        "expected_state": expected_state,
        "state_ok": state_ok,
        "model_skipped": trace_contract.get("model_skipped"),
        "model_route": model_route,
        "trace_contract": trace_contract,
        "system_prompt_template": dict(prompt_receipt),
        "chat_cost_ledger": cost_ledger,
        "answer": {
            "chars": len(answer),
            "sha256": _sha256_bytes(answer.encode()),
            "text": answer,
        },
        "sources": sources,
        "expected_evidence": expected,
        "schema_proofs": proofs,
        "trace_events": _safe_trace_receipt(raw["traces"]),
        "technical": {
            "ok": not technical_errors,
            "errors": technical_errors,
        },
    }
    required = (
        row["technical"]["ok"],
        bool(row["question_sha256"]),
        bool(row["request"]["payload_sha256"]),
        len(row["trace_events"]) > 0,
    )
    row["journal_complete"] = all(required)
    return row


async def _retrieval_only_repeat(
    *,
    cases: Sequence[dict[str, Any]],
    executions: Sequence[dict[str, Any]],
    corpus_id: str,
    user_id: str,
) -> list[dict[str, Any]]:
    from models.schemas import RetrievalTier
    from motor.motor_asyncio import AsyncIOMotorClient
    from neo4j import AsyncGraphDatabase
    from qdrant_client import AsyncQdrantClient
    from services.chat_orchestrator import _build_librarian_plan_trace
    from services.conversation import conversation_service
    from services.ingestion_service import ingestion_service
    from services.retriever import retriever_orchestrator
    from services.retriever.query_plan import build_query_plan_v2

    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    database = mongo[settings.MONGODB_DATABASE]
    qdrant = AsyncQdrantClient(
        url=settings.QDRANT_URL,
        timeout=settings.QDRANT_TIMEOUT_SECONDS,
    )
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
    by_id = {row["query_id"]: row for row in executions}
    semaphore = asyncio.Semaphore(CONCURRENCY)
    librarian_controls = _repeat_librarian_controls(settings, user_id=user_id)

    async def repeat_one(case: dict[str, Any]) -> dict[str, Any]:
        full = by_id[str(case["id"])]
        query_plan_trace = next(
            (
                row.get("metadata") or {}
                for row in full.get("trace_events") or []
                if row.get("title") == "Query plan" and row.get("status") == "done"
            ),
            {},
        )
        budget = dict(query_plan_trace.get("budget") or {})
        expected_plan = dict(
            (query_plan_trace.get("librarian_query_plan") or {}).get("plan") or {}
        )
        expected_retrieval = next(
            (
                (row.get("metadata") or {}).get("retrieval_diagnostics") or {}
                for row in full.get("trace_events") or []
                if row.get("title") == "Local RAG retrieval"
                and row.get("status") == "done"
            ),
            {},
        )
        started = time.monotonic()
        error = None
        plan_trace: dict[str, Any] | None = None
        result = None
        try:
            async with semaphore:
                plan_trace = await _build_librarian_plan_trace(
                    query=str(case["question"]),
                    corpus_ids=[corpus_id],
                    requested_tier=RetrievalTier(str(case["retrieval_tier"])),
                    enabled=True,
                    shadow=False,
                    db=database,
                    user_id=user_id,
                    llm_decomposer_enabled=librarian_controls["llm_decomposer_enabled"],
                )
                base_plan = build_query_plan_v2(
                    str(case["question"]),
                    corpus_ids=[corpus_id],
                    standalone_query=str(case["question"]),
                )
                result = await retriever_orchestrator.retrieve_planned(
                    plan=base_plan,
                    corpus_ids=[corpus_id],
                    retrieval_tier=RetrievalTier(str(case["retrieval_tier"])),
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
                    fact_seed_limit=budget.get("fact_seed_limit"),
                    search_mode=str(query_plan_trace.get("search_mode") or "local"),
                    librarian_plan=(
                        plan_trace.get("plan")
                        if isinstance(plan_trace, dict)
                        and plan_trace.get("behavior_applied") is True
                        else None
                    ),
                    librarian_refinement_enabled=librarian_controls[
                        "librarian_refinement_enabled"
                    ],
                    librarian_refinement_user_id=librarian_controls[
                        "librarian_refinement_user_id"
                    ],
                )
        except Exception as exc:  # noqa: BLE001 - durable receipt
            error = f"{type(exc).__name__}: {exc}"[:500]
        repeat_plan = dict((plan_trace or {}).get("plan") or {})
        diagnostics = dict(getattr(result, "diagnostics", None) or {}) if result else {}
        plan_provider_calls = int(
            ((plan_trace or {}).get("diagnostics") or {}).get("provider_calls") or 0
        )
        refinement_provider_calls = int(
            (
                (
                    (diagnostics.get("librarian_execution") or {}).get("refinement")
                    or {}
                ).get("provider_calls")
                or 0
            )
        )
        provider_calls = plan_provider_calls + refinement_provider_calls
        expected_seats = _seat_surface(dict(expected_retrieval))
        repeat_seats = _seat_surface(diagnostics)
        return {
            "query_id": str(case["id"]),
            "provider_calls": provider_calls,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "error": error,
            "expected_plan_hash": expected_plan.get("plan_hash"),
            "repeat_plan_hash": repeat_plan.get("plan_hash"),
            "plan_hash_identical": (
                bool(expected_plan.get("plan_hash"))
                and expected_plan.get("plan_hash") == repeat_plan.get("plan_hash")
            ),
            "expected_seats": expected_seats,
            "repeat_seats": repeat_seats,
            "seats_identical": _canonical_bytes(expected_seats)
            == _canonical_bytes(repeat_seats),
            "technical_ok": bool(
                result is not None and not error and provider_calls == 0
            ),
        }

    try:
        tasks = [asyncio.create_task(repeat_one(case)) for case in cases]
        return list(await asyncio.gather(*tasks))
    finally:
        ingestion_service._db = old_db
        ingestion_service._qdrant = old_qdrant
        ingestion_service._neo4j = old_neo4j
        conversation_service._db = old_conversation_db
        await qdrant.close()
        await neo4j.close()
        mongo.close()


def _median(values: Sequence[float]) -> float | None:
    return round(float(statistics.median(values)), 3) if values else None


def _refinement_acceptance_surface(
    by_ordinal: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    def row(ordinal: int) -> dict[str, Any]:
        refinement = dict(by_ordinal[ordinal]["schema_proofs"].get("refinement") or {})
        return {
            "ordinal": ordinal,
            "query_id": by_ordinal[ordinal]["query_id"],
            "enabled": refinement.get("enabled"),
            "fired": refinement.get("fired"),
            "status": refinement.get("status"),
            "second_pass_attempted": refinement.get("second_pass_attempted"),
            "improved_seating": refinement.get("improved_seating"),
        }

    depth = [row(ordinal) for ordinal in REFINEMENT_DEPTH_ORDINALS]
    simple = [row(ordinal) for ordinal in REFINEMENT_SIMPLE_ORDINALS]
    return {
        "depth_probes": depth,
        "simple_probes": simple,
        "gap_firing_improved": any(
            item["enabled"] is True
            and item["fired"] is True
            and item["second_pass_attempted"] is True
            and item["improved_seating"] is True
            for item in depth
        ),
        "simple_zero_firings": all(
            item["enabled"] is True and item["fired"] is False for item in simple
        ),
    }


def summarize(
    executions: Sequence[dict[str, Any]],
    repeats: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    by_ordinal = {int(row["ordinal"]): row for row in executions}
    fast = [
        float(row["transport"]["elapsed_seconds"])
        for row in executions
        if row["latency_tier"] == "fast"
    ]
    deep = [
        float(row["transport"]["elapsed_seconds"])
        for row in executions
        if row["latency_tier"] == "deep"
    ]
    proof = lambda ordinal: bool(by_ordinal[ordinal]["schema_proofs"]["proof_pass"])
    state = lambda ordinal: bool(by_ordinal[ordinal]["state_ok"])
    membership = all(
        row["sources"]["all_in_selected_corpus"] is True for row in executions
    )
    associative_consumed = any(
        row["schema_proofs"]["associative_profile"]["consumed"] is True
        for row in executions
    )
    cost = aggregate_chat_cost_ledgers(
        [
            row["chat_cost_ledger"]
            for row in executions
            if isinstance(row.get("chat_cost_ledger"), dict)
        ]
    )
    fast_p50 = _median(fast)
    deep_p50 = _median(deep)
    refinement = _refinement_acceptance_surface(by_ordinal)
    gates = {
        "technical_23_of_23": (
            len(executions) == EXPECTED_EXECUTIONS
            and all(row["technical"]["ok"] is True for row in executions)
            and all(row["journal_complete"] is True for row in executions)
        ),
        "depth_bridge_1_of_2": sum(proof(i) and state(i) for i in (1, 2)) >= 1,
        "graph_hop_1_of_2": sum(proof(i) and state(i) for i in (3, 4)) >= 1,
        "multi_hop_d6a": proof(5) and state(5),
        "schema_consumption_7_to_10": all(proof(i) and state(i) for i in range(7, 11)),
        "floors_11_to_16": all(proof(i) and state(i) for i in range(11, 17)),
        "named_positive_17_to_18": all(proof(i) and state(i) for i in range(17, 19)),
        "refusals_19_to_23": all(
            proof(i) and state(i) and by_ordinal[i]["classification"]["refused"] is True
            for i in range(19, 24)
        ),
        "associative_profile_consumed": associative_consumed,
        "refinement_gap_improves_d1_d6": refinement["gap_firing_improved"],
        "refinement_not_fired_clean_13_to_16": refinement["simple_zero_firings"],
        "corpus_citation_membership": membership,
        "fast_p50_le_5s": fast_p50 is not None and fast_p50 <= 5.0,
        "deep_p50_le_15s": deep_p50 is not None and deep_p50 <= 15.0,
        "plan_hash_determinism_1_to_5": (
            len(repeats) == 5
            and all(row["plan_hash_identical"] is True for row in repeats)
        ),
        "seat_determinism_1_to_5": (
            len(repeats) == 5 and all(row["seats_identical"] is True for row in repeats)
        ),
        "cost_ledger_closed": cost.get("accounting_state") == "CLOSED",
    }
    return {
        "execution_count": len(executions),
        "classification_counts": dict(
            Counter(row["classification"]["state"] for row in executions)
        ),
        "fast_count": len(fast),
        "fast_p50_seconds": fast_p50,
        "fast_p95_seconds": (
            round(sorted(fast)[max(0, int(len(fast) * 0.95) - 1)], 3) if fast else None
        ),
        "deep_count": len(deep),
        "deep_p50_seconds": deep_p50,
        "deep_p95_seconds": (
            round(sorted(deep)[max(0, int(len(deep) * 0.95) - 1)], 3) if deep else None
        ),
        "cost_ledger": cost,
        "refinement": refinement,
        "gates": gates,
        "all_green": all(gates.values()),
        "owner_quality_eyeball_pending": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument("--lock-owner", required=True)
    parser.add_argument("--expected-synthesis-entry-id", required=True)
    parser.add_argument("--expected-two-lane", choices=("off", "on"), required=True)
    return parser


def run(args: argparse.Namespace) -> int:
    require(not args.output.exists(), "output already exists; use a fresh journal")
    lock_path = Path("/tmp/polymath-eval.lock")
    require(lock_path.is_file(), "host/container eval lock is not mounted")
    require(
        lock_path.read_text(encoding="utf-8") == args.lock_owner,
        "eval lock owner drifted",
    )
    endpoint = _validate_same_container_runtime(_validate_local_api(args.api))
    manifest = _load_hashed_json(
        MANIFEST_PATH,
        MANIFEST_SHA256,
        "final acceptance selection",
    )
    _assert_file_hash(FINAL_SPEC_PATH, FINAL_SPEC_SHA256, "final acceptance spec")
    _assert_file_hash(DEPTH_SPEC_PATH, DEPTH_SPEC_SHA256, "depth probe spec")
    _assert_file_hash(RETRIEVAL_SPEC_PATH, RETRIEVAL_SPEC_SHA256, "retrieval spec")
    _assert_file_hash(NEGATIVE_SPEC_PATH, NEGATIVE_SPEC_SHA256, "negative spec")
    selection = _load_hashed_json(
        CORPUS_SELECTION_PATH,
        CORPUS_SELECTION_SHA256,
        "15-document selection",
    )
    selected_filenames = {
        str(row.get("filename") or "") for row in selection.get("selected") or []
    }
    require(
        len(selected_filenames) == 15 and all(selected_filenames),
        "15-document selection drifted",
    )
    surface = _selection_surface(manifest)
    client, database, corpus, token, expected_model, safe_candidate = _discover_runtime(
        args.expected_synthesis_entry_id
    )
    try:
        corpus_id = str(corpus["corpus_id"])
        flags = _runtime_flags(expected_two_lane=args.expected_two_lane == "on")
        preflight = _embedder_preflight(args.api)
        document_names, corpus_receipt = _verify_corpus(
            args.api,
            token,
            corpus_id,
            selected_filenames,
        )
        prompt_receipt, prompt_rendered_at = _prompt_template_receipt()
        process_run_id = str(uuid.uuid4())
        cases = list(surface["queries"])
        state: dict[str, Any] = {
            "schema_version": JOURNAL_SCHEMA,
            "started_at_utc": _utc_now(),
            "completed_at_utc": None,
            "sealed": False,
            "manifest": {
                **{key: value for key, value in surface.items() if key != "queries"},
                "file_sha256": MANIFEST_SHA256,
                "parent_acceptance_spec_sha256": FINAL_SPEC_SHA256,
            },
            "corpus": corpus_receipt,
            "runtime_flags": flags,
            "synthesis_candidate": safe_candidate,
            "expected_model": expected_model,
            "concurrency": CONCURRENCY,
            "temperature": TEMPERATURE,
            "endpoint_binding": endpoint,
            "embedder_preflight": preflight,
            "system_prompt_template": prompt_receipt,
            "process_run_id": process_run_id,
            "executions": [],
            "retrieval_only_repeats": [],
            "summary": None,
            "seal": None,
        }
        _atomic_write(args.output, state)
        print(
            "FINAL_ACCEPTANCE_START "
            + json.dumps(
                {
                    "queries": len(cases),
                    "concurrency": CONCURRENCY,
                    "temperature": TEMPERATURE,
                    "model": expected_model,
                    "selection_sha256": surface["exact_query_surface_sha256"],
                },
                sort_keys=True,
            ),
            flush=True,
        )

        def execute_one(case: dict[str, Any]) -> dict[str, Any]:
            ordinal = int(case["ordinal"])
            print(
                f"FINAL_EXECUTION_START {ordinal}/{EXPECTED_EXECUTIONS} {case['id']}",
                flush=True,
            )
            raw = _run_sse(
                api=args.api,
                token=token,
                case=case,
                corpus_id=corpus_id,
                timeout=args.request_timeout,
            )
            return _build_execution(
                case=case,
                raw=raw,
                ordinal=ordinal,
                process_run_id=process_run_id,
                prompt_receipt=prompt_receipt,
                expected_model=expected_model,
                expected_entry_id=args.expected_synthesis_entry_id,
                corpus_id=corpus_id,
                document_names=document_names,
                selected_filenames=selected_filenames,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = [pool.submit(execute_one, case) for case in cases]
            for future in concurrent.futures.as_completed(futures):
                row = future.result()
                state["executions"].append(row)
                state["executions"].sort(key=lambda value: int(value["ordinal"]))
                _atomic_write(args.output, state)
                print(
                    "FINAL_EXECUTION_DONE "
                    + json.dumps(
                        {
                            "ordinal": row["ordinal"],
                            "query_id": row["query_id"],
                            "state": row["classification"]["state"],
                            "proof": row["schema_proofs"]["proof_pass"],
                            "elapsed_seconds": row["transport"]["elapsed_seconds"],
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
                row["technical"]["errors"].append(
                    "system prompt rendered/source hash changed during batch"
                )

        repeat_cases = [case for case in cases if str(case["id"]) in DETERMINISM_IDS]
        repeats = asyncio.run(
            _retrieval_only_repeat(
                cases=repeat_cases,
                executions=state["executions"],
                corpus_id=corpus_id,
                user_id=str(corpus["user_id"]),
            )
        )
        state["retrieval_only_repeats"] = sorted(
            repeats,
            key=lambda value: DETERMINISM_IDS.index(value["query_id"]),
        )
        for row in state["retrieval_only_repeats"]:
            print(
                "FINAL_RETRIEVAL_REPEAT_DONE "
                + json.dumps(
                    {
                        "query_id": row["query_id"],
                        "plan_hash_identical": row["plan_hash_identical"],
                        "seats_identical": row["seats_identical"],
                        "technical_ok": row["technical_ok"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

        state["completed_at_utc"] = _utc_now()
        state["prompt_render_context_stable"] = prompt_stable
        state["summary"] = summarize(state["executions"], repeats)
        technically_sealable = (
            len(state["executions"]) == EXPECTED_EXECUTIONS
            and len(repeats) == 5
            and all(row["journal_complete"] is True for row in state["executions"])
            and all(row["technical_ok"] is True for row in repeats)
            and prompt_stable
        )
        state["sealed"] = technically_sealable
        state["seal"] = _seal_journal(state) if technically_sealable else None
        _atomic_write(args.output, state)
        print(
            "FINAL_ACCEPTANCE_SUMMARY " + json.dumps(state["summary"], sort_keys=True),
            flush=True,
        )
        if not technically_sealable:
            return 2
        return 0 if state["summary"]["all_green"] else 1
    finally:
        client.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return run(args)
    except (RuntimeError, OSError, ValueError, urllib.error.URLError) as exc:
        print(
            f"FINAL_ACCEPTANCE_ABORT={type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
