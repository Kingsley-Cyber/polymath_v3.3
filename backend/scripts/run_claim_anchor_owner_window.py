#!/usr/bin/env python3
"""Run the owner-authorized fresh-baseline claim-anchor verification window.

This harness leaves the historical v1/v2 replay scripts unchanged. The
owner-specific OFF capture forces temperature zero and seals complete prompt,
model, answerability, runtime, and outer-lock telemetry. The ON arm is
provider-free: it attaches anchors to the exact selected evidence sealed by
that fresh OFF artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from config import get_settings
from evals.canonical_refusal_contract import (
    EXPECTED_CHAT_MODEL,
    build_system_prompt_receipt,
    extract_answerability_contract,
    validate_local_eval_api,
    validate_same_container_runtime,
    validate_chat_trace_contract,
)
from models.schemas import SourceChunk
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
from scripts.run_claim_anchor_additivity_replay import (
    SEALED_V1_OFF_SHA256,
    V2_SCHEMA,
    V2_SPEC,
    _anchor_rows,
    _prompt_anchor_count,
    _source_keys,
)
from scripts.run_claim_anchor_micro_ab import (
    _anchor_trace,
    _atomic_write,
    _fingerprint,
    _load_contract,
    _mint_token,
    _source_fingerprint,
    _source_without_claim_anchors,
    _validate_anchor,
)
from services.context_manager import context_manager
from services.retriever.atomic_claim_anchors import (
    attach_atomic_claim_anchors,
    source_additivity_receipt,
)
from services.retriever.claim_anchor_rendering import render_claim_proposition

OWNER_WINDOW_SCHEMA = "claim_anchor_owner_window.v1"
OWNER_OFF_ATTESTATION_SCHEMA = "claim_anchor_owner_window_off_attestation.v2"
V2_SPEC_SHA256 = "42eb718dfee0ffd47e1310d1605f02514308bfc9941e8115644ab7434be91783"
OWNER_CAPTURE_HARNESS = Path(__file__).resolve()
CHAT_ORCHESTRATOR_PATH = (
    Path(__file__).resolve().parents[1] / "services/chat_orchestrator.py"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOW_NONCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_UNREADABLE_MACHINE_TOKEN_RE = re.compile(
    r"\b(?:POSITIVE|NEGATIVE|ASSERTED|POSSIBLE|PROBABLE|NECESSARY|"
    r"RECOMMENDED|HYPOTHETICAL|UNTYPED)\b|UNTYPED\["
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_snapshot(settings: Any) -> dict[str, Any]:
    boolean_names = (
        "RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED",
        "ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED",
        "TEMPORAL_QUERY_ROUTING_ENABLED",
        "RERANK_EVIDENCE_SUPPORT",
        "ATOMIC_CLAIM_ANCHORS_ENABLED",
        "PARENT_EXCERPT_ENABLED",
        "WATERFALL_ASSEMBLY",
        "TWO_LANE_ANCHORING",
        "TWO_LANE_ANCHORING_ENABLED",
        "HYDE_ENABLED",
        "SHELF_RESERVE_ENABLED",
        "GROUNDED_QUERY_PLANNER_ENABLED",
        "FOUR_LANE_TIER0_ROUTER_ENABLED",
        "FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED",
        "AGENTIC_MODE_ENABLED",
    )
    return {
        **{name: bool(getattr(settings, name)) for name in boolean_names},
        "HYDRATION_MODE": str(settings.HYDRATION_MODE),
    }


def _require_runtime(
    runtime: dict[str, Any],
    *,
    claim_anchors_enabled: bool,
) -> None:
    required = {
        "RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED": True,
        "ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED": True,
        "TEMPORAL_QUERY_ROUTING_ENABLED": True,
        "RERANK_EVIDENCE_SUPPORT": False,
        "ATOMIC_CLAIM_ANCHORS_ENABLED": claim_anchors_enabled,
        "PARENT_EXCERPT_ENABLED": False,
        "WATERFALL_ASSEMBLY": False,
        "TWO_LANE_ANCHORING": False,
        "HYDE_ENABLED": False,
        "SHELF_RESERVE_ENABLED": False,
        "GROUNDED_QUERY_PLANNER_ENABLED": False,
        "FOUR_LANE_TIER0_ROUTER_ENABLED": False,
        "FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED": False,
        "AGENTIC_MODE_ENABLED": False,
    }
    missing = sorted(set(required) - set(runtime))
    mismatched = {
        key: {"observed": runtime.get(key), "expected": value}
        for key, value in required.items()
        if runtime.get(key) != value
    }
    if missing or mismatched:
        raise RuntimeError(
            "claim owner-window runtime mismatch: "
            f"missing={json.dumps(missing)} "
            f"mismatched={json.dumps(mismatched, sort_keys=True)}"
        )
    if not isinstance(runtime.get("TWO_LANE_ANCHORING_ENABLED"), bool):
        raise RuntimeError("claim owner-window two-lane runtime flag is absent")
    if not str(runtime.get("HYDRATION_MODE") or ""):
        raise RuntimeError("claim owner-window hydration mode is absent")


def _require_claim_only_transition(
    off_runtime: dict[str, Any],
    on_runtime: dict[str, Any],
) -> None:
    _require_runtime(off_runtime, claim_anchors_enabled=False)
    _require_runtime(on_runtime, claim_anchors_enabled=True)
    expected_on = dict(off_runtime)
    expected_on["ATOMIC_CLAIM_ANCHORS_ENABLED"] = True
    if on_runtime != expected_on:
        changed = {
            key: {"off": off_runtime.get(key), "on": on_runtime.get(key)}
            for key in sorted(set(off_runtime) | set(on_runtime))
            if off_runtime.get(key) != on_runtime.get(key)
        }
        raise RuntimeError(
            "claim owner-window runtime changed outside the claim flag: "
            + json.dumps(changed, sort_keys=True)
        )


def _parse_utc(value: str, *, label: str) -> datetime:
    normalized = str(value or "").strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RuntimeError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _window_identity(
    *,
    lock_owner: str,
    window_nonce: str,
    window_not_before_utc: str,
) -> dict[str, str]:
    owner = str(lock_owner or "").strip()
    nonce = str(window_nonce or "").strip()
    if not owner:
        raise RuntimeError("outer eval-lock owner must be non-empty")
    if not _WINDOW_NONCE_RE.fullmatch(nonce):
        raise RuntimeError(
            "outer eval-lock nonce must be 16-128 safe identifier characters"
        )
    not_before = _parse_utc(
        window_not_before_utc,
        label="outer eval-lock not-before",
    )
    return {
        "lock_owner": owner,
        "window_nonce": nonce,
        "window_not_before_utc": not_before.isoformat(),
    }


def _require_outer_lock_environment(
    *,
    lock_owner: str,
    window_nonce: str,
    window_not_before_utc: str,
) -> None:
    if os.environ.get("POLYMATH_EVAL_OUTER_LOCK_ATTESTED") != "1":
        raise RuntimeError("claims window requires the outer host-lock attestation")
    expected = {
        "POLYMATH_EVAL_LOCK_OWNER": lock_owner,
        "POLYMATH_EVAL_WINDOW_NONCE": window_nonce,
        "POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC": window_not_before_utc,
    }
    drift = {
        key: {"observed": os.environ.get(key), "expected": value}
        for key, value in expected.items()
        if os.environ.get(key) != value
    }
    if drift:
        raise RuntimeError(
            "claims outer host-lock environment drifted: "
            + json.dumps(drift, sort_keys=True)
        )


def _load_v2_contract(spec_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    actual_sha = _sha256_file(spec_path)
    if actual_sha != V2_SPEC_SHA256:
        raise RuntimeError(
            f"claim-anchor v2 spec hash drifted: {actual_sha} != {V2_SPEC_SHA256}"
        )
    spec, questions = _load_contract(spec_path)
    if spec.get("schema_version") != V2_SCHEMA:
        raise RuntimeError("owner-window harness requires the frozen v2 spec")
    if spec.get("model_contract") != EXPECTED_CHAT_MODEL:
        raise RuntimeError("owner-window exact chat model contract drifted")
    return spec, questions


def _validate_off_payload_base(
    *,
    off: dict[str, Any],
    spec: dict[str, Any],
) -> None:
    failures: list[str] = []
    if off.get("arm") != "off":
        failures.append("arm_not_off")
    if off.get("runtime_flag_enabled") is not False:
        failures.append("off_runtime_claim_flag_not_false")
    if off.get("passed") is not True:
        failures.append("off_arm_not_green")
    if off.get("spec") != spec:
        failures.append("off_spec_drift")
    if off.get("model_contract") != spec.get("model_contract"):
        failures.append("off_model_contract_drift")
    if off.get("request_temperature") != 0:
        failures.append("off_temperature_drift")
    endpoint_binding = off.get("endpoint_binding")
    if (
        not isinstance(endpoint_binding, dict)
        or endpoint_binding.get("same_container_prompt_binding") is not True
        or endpoint_binding.get("loopback_required") is not True
    ):
        failures.append("off_endpoint_binding_absent")
    prompt_receipt = off.get("system_prompt_template")
    if (
        not isinstance(prompt_receipt, dict)
        or not _SHA256_RE.fullmatch(str(prompt_receipt.get("sha256") or ""))
        or not _SHA256_RE.fullmatch(str(prompt_receipt.get("source_sha256") or ""))
    ):
        failures.append("off_prompt_template_receipt_absent")
    if not str(off.get("corpus_id") or ""):
        failures.append("off_corpus_id_absent")
    if off.get("corpus_fingerprint_equal") is not True:
        failures.append("off_corpus_fingerprint_not_equal")
    if off.get("corpus_fingerprint_before") != off.get("corpus_fingerprint_after"):
        failures.append("off_corpus_fingerprint_drift")
    compilation_fingerprint = (
        (off.get("corpus_fingerprint_after") or {})
        .get("collections", {})
        .get("semantic_digest_claim_compilations", {})
        .get("sha256")
    )
    if not _SHA256_RE.fullmatch(str(compilation_fingerprint or "")):
        failures.append("off_raw_claim_store_fingerprint_absent")

    rows = list(off.get("results") or [])
    if [row.get("query_id") for row in rows] != spec.get("query_ids"):
        failures.append("off_query_order_drift")
    for row in rows:
        query_id = str(row.get("query_id") or "unknown")
        if row.get("errors") or row.get("done_received") is not True:
            failures.append(f"{query_id}:technical")
        if row.get("model_used") != spec.get("model_contract"):
            failures.append(f"{query_id}:model_contract")
        if row.get("request_temperature") != 0:
            failures.append(f"{query_id}:temperature")
        if row.get("journal_complete") is not True:
            failures.append(f"{query_id}:journal_incomplete")
        if not isinstance(row.get("model_skipped"), bool):
            failures.append(f"{query_id}:model_skipped")
        route = row.get("model_route")
        if not isinstance(route, dict) or route.get("model") != spec.get(
            "model_contract"
        ):
            failures.append(f"{query_id}:model_route")
        answerability = row.get("answerability")
        guard = answerability.get("guard") if isinstance(answerability, dict) else None
        if (
            not isinstance(answerability, dict)
            or answerability.get("ok") is not True
            or not isinstance(answerability.get("raw_answerable"), bool)
            or not isinstance(guard, dict)
            or not isinstance(guard.get("eligible"), bool)
            or "coverage" not in guard
        ):
            failures.append(f"{query_id}:answerability_telemetry")
        if row.get("system_prompt_template") != prompt_receipt:
            failures.append(f"{query_id}:prompt_template")
        prior_state = row.get("prior_call_session_state")
        if (
            not isinstance(prior_state, dict)
            or not isinstance(prior_state.get("history_turn_count"), int)
            or not isinstance(prior_state.get("conversation_id_sent"), bool)
            or not isinstance(prior_state.get("history_receipts"), list)
        ):
            failures.append(f"{query_id}:prior_call_state")
        if int(row.get("anchor_count") or 0) != 0:
            failures.append(f"{query_id}:off_anchor_exposure")
        if int(row.get("prompt_render_count") or 0) != 0:
            failures.append(f"{query_id}:off_render_exposure")
        sources = list(row.get("selected_sources") or [])
        if not sources:
            failures.append(f"{query_id}:selected_sources_absent")
            continue
        if _source_keys(sources) != row.get("source_keys"):
            failures.append(f"{query_id}:source_keys_drift")
        if _source_fingerprint(sources) != row.get(
            "selected_evidence_sha256_without_anchors"
        ):
            failures.append(f"{query_id}:selected_evidence_hash_drift")
    if failures:
        raise RuntimeError("fresh OFF artifact invalid: " + ",".join(failures))


def _attest_off_payload(
    *,
    off: dict[str, Any],
    spec: dict[str, Any],
    runtime: dict[str, Any],
    window: dict[str, str],
    now: datetime | None = None,
) -> dict[str, Any]:
    _require_runtime(runtime, claim_anchors_enabled=False)
    _validate_off_payload_base(off=off, spec=spec)
    attested_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    not_before = _parse_utc(
        window["window_not_before_utc"],
        label="outer eval-lock not-before",
    )
    if attested_at < not_before:
        raise RuntimeError("fresh OFF capture predates its outer lock window")
    attested = copy.deepcopy(off)
    attested["owner_window_attestation"] = {
        "schema_version": OWNER_OFF_ATTESTATION_SCHEMA,
        "attested_at_utc": attested_at.isoformat(),
        "spec_sha256": V2_SPEC_SHA256,
        "source_harness_sha256": _sha256_file(OWNER_CAPTURE_HARNESS),
        "capture_runtime": runtime,
        "outer_host_lock": dict(window),
    }
    return attested


def _validate_fresh_off_artifact(
    *,
    off_path: Path,
    expected_sha256: str,
    spec: dict[str, Any],
    expected_window: dict[str, str],
    max_age_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized_sha = expected_sha256.strip().lower()
    if not _SHA256_RE.fullmatch(normalized_sha):
        raise RuntimeError("--off-artifact-sha256 must be 64 lowercase hex characters")
    actual_sha = _sha256_file(off_path)
    if actual_sha != normalized_sha:
        raise RuntimeError(
            f"fresh OFF artifact SHA drifted: {actual_sha} != {normalized_sha}"
        )
    if actual_sha == SEALED_V1_OFF_SHA256:
        raise RuntimeError("stale pinned v1 OFF packet is forbidden in owner window")

    off = json.loads(off_path.read_text(encoding="utf-8"))
    _validate_off_payload_base(off=off, spec=spec)
    attestation = off.get("owner_window_attestation")
    if not isinstance(attestation, dict):
        raise RuntimeError("fresh OFF artifact lacks owner-window attestation")
    if attestation.get("schema_version") != OWNER_OFF_ATTESTATION_SCHEMA:
        raise RuntimeError("fresh OFF artifact attestation schema drifted")
    if attestation.get("spec_sha256") != V2_SPEC_SHA256:
        raise RuntimeError("fresh OFF artifact v2 spec hash drifted")
    if attestation.get("source_harness_sha256") != _sha256_file(OWNER_CAPTURE_HARNESS):
        raise RuntimeError("fresh OFF capture harness hash drifted")
    outer_host_lock = attestation.get("outer_host_lock")
    if outer_host_lock != expected_window:
        raise RuntimeError(
            "fresh OFF outer lock identity drifted: "
            + json.dumps(
                {"observed": outer_host_lock, "expected": expected_window},
                sort_keys=True,
            )
        )
    attested_at = _parse_utc(
        str(attestation.get("attested_at_utc") or ""),
        label="fresh OFF attested-at",
    )
    not_before = _parse_utc(
        expected_window["window_not_before_utc"],
        label="outer eval-lock not-before",
    )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if attested_at < not_before:
        raise RuntimeError("fresh OFF artifact predates the outer lock window")
    if attested_at > current + timedelta(seconds=60):
        raise RuntimeError("fresh OFF artifact attestation is in the future")
    if max_age_seconds <= 0:
        raise RuntimeError("fresh OFF max age must be positive")
    if current - attested_at > timedelta(seconds=max_age_seconds):
        raise RuntimeError("fresh OFF artifact exceeded the owner-window max age")
    capture_runtime = attestation.get("capture_runtime")
    if not isinstance(capture_runtime, dict):
        raise RuntimeError("fresh OFF artifact capture runtime is absent")
    _require_runtime(capture_runtime, claim_anchors_enabled=False)
    return off


def _render_is_readable(value: str) -> bool:
    compact = " ".join(str(value or "").split())
    return bool(compact) and not _UNREADABLE_MACHINE_TOKEN_RE.search(compact)


def _prompt_claim_block_is_readable(prompt: str) -> bool:
    start = prompt.find("<atomic_claim_anchors>")
    end = prompt.find("</atomic_claim_anchors>", start + 1)
    if start < 0 or end < 0:
        return False
    return not _UNREADABLE_MACHINE_TOKEN_RE.search(prompt[start:end])


async def _replay_owner_window(
    *,
    settings: Any,
    sync_db: Any,
    questions: list[dict[str, Any]],
    off_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    motor = AsyncIOMotorClient(settings.MONGODB_URI)
    async_db = motor[settings.MONGODB_DATABASE]
    results: list[dict[str, Any]] = []
    try:
        for question, off_row in zip(questions, off_rows, strict=True):
            started = time.perf_counter()
            off_sources = copy.deepcopy(off_row.get("selected_sources") or [])
            selected = [SourceChunk.model_validate(source) for source in off_sources]
            before = source_additivity_receipt(selected)
            enriched, diagnostics = await attach_atomic_claim_anchors(
                async_db,
                selected,
                query=question["question"],
                per_source=settings.ATOMIC_CLAIM_ANCHORS_PER_SOURCE,
                total=settings.ATOMIC_CLAIM_ANCHORS_TOTAL,
            )
            after = source_additivity_receipt(enriched)
            enriched_payload = [source.model_dump(mode="json") for source in enriched]
            anchors = _anchor_rows(enriched_payload)
            checks = [
                _validate_anchor(sync_db, source=source, anchor=anchor)
                for source, anchor in anchors
            ]
            raw_claims_before_render = [
                str(anchor.get("claim_text") or "") for _, anchor in anchors
            ]
            rendered_claims = [
                render_claim_proposition(
                    raw,
                    exact_sentence=str(anchor.get("exact_sentence") or ""),
                )
                for raw, (_, anchor) in zip(
                    raw_claims_before_render,
                    anchors,
                    strict=True,
                )
            ]
            prompt = context_manager.build_augmented_prompt(
                question["question"],
                enriched,
            )
            after_render_payload = [
                source.model_dump(mode="json") for source in enriched
            ]
            raw_claims_after_render = [
                str(anchor.get("claim_text") or "")
                for _, anchor in _anchor_rows(after_render_payload)
            ]
            source_keys = _source_keys(enriched_payload)
            valid_count = sum(int(check["valid"]) for check in checks)
            readable_count = sum(
                int(_render_is_readable(rendered)) for rendered in rendered_claims
            )
            encoded_rendered = json.dumps(
                rendered_claims,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            source_ids_equal = source_keys == off_row["source_keys"]
            evidence_bytes_equal = (
                before["non_anchor_evidence_sha256"]
                == after["non_anchor_evidence_sha256"]
                and before["non_anchor_evidence_bytes"]
                == after["non_anchor_evidence_bytes"]
                and _source_fingerprint(enriched_payload)
                == off_row["selected_evidence_sha256_without_anchors"]
            )
            result = {
                "query_id": question["id"],
                "shape": question["shape"],
                "source_keys": source_keys,
                "source_count": len(source_keys),
                "off_source_ids_sha256": before["source_ids_sha256"],
                "on_source_ids_sha256": after["source_ids_sha256"],
                "off_non_anchor_evidence_sha256": before["non_anchor_evidence_sha256"],
                "on_non_anchor_evidence_sha256": after["non_anchor_evidence_sha256"],
                "source_ids_equal": source_ids_equal,
                "non_anchor_evidence_bytes_equal": evidence_bytes_equal,
                "service_additivity_verified": diagnostics.get("additivity_verified"),
                "anchor_count": len(anchors),
                "valid_anchor_count": valid_count,
                "all_citations_valid": (
                    all(check["valid"] for check in checks) if checks else None
                ),
                "prompt_render_count": _prompt_anchor_count(prompt),
                "readable_claim_count": readable_count,
                "prompt_claim_block_readable": _prompt_claim_block_is_readable(prompt),
                "rendered_claims_sha256": hashlib.sha256(encoded_rendered).hexdigest(),
                "render_cleaned_claim_count": sum(
                    int(rendered != raw)
                    for raw, rendered in zip(
                        raw_claims_before_render,
                        rendered_claims,
                        strict=True,
                    )
                ),
                "raw_claim_text_preserved": (
                    raw_claims_before_render == raw_claims_after_render
                ),
                "diagnostics": diagnostics,
                "model_contract": off_row.get("model_used"),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
            results.append(result)
            print(
                json.dumps(
                    {
                        "query_id": result["query_id"],
                        "sources_equal": source_ids_equal,
                        "evidence_equal": evidence_bytes_equal,
                        "anchors": len(anchors),
                        "valid": valid_count,
                        "rendered": result["prompt_render_count"],
                        "readable": readable_count,
                        "raw_preserved": result["raw_claim_text_preserved"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
    finally:
        motor.close()
    return results


def _replay_failures(
    *,
    spec: dict[str, Any],
    off: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    results: list[dict[str, Any]],
) -> list[str]:
    failures: list[str] = []
    for row in results:
        query_id = str(row["query_id"])
        if not row["source_ids_equal"]:
            failures.append(f"{query_id}:source_identity")
        if not row["non_anchor_evidence_bytes_equal"]:
            failures.append(f"{query_id}:non_anchor_evidence")
        if row["service_additivity_verified"] is not True:
            failures.append(f"{query_id}:service_additivity")
        if row["anchor_count"] and not row["all_citations_valid"]:
            failures.append(f"{query_id}:citation_invalid")
        if row["prompt_render_count"] != row["anchor_count"]:
            failures.append(f"{query_id}:not_all_anchors_rendered")
        if row["readable_claim_count"] != row["anchor_count"]:
            failures.append(f"{query_id}:not_all_claims_readable")
        if row["anchor_count"] and not row["prompt_claim_block_readable"]:
            failures.append(f"{query_id}:prompt_claim_block_unreadable")
        if not row["raw_claim_text_preserved"]:
            failures.append(f"{query_id}:raw_claim_mutated")
        if row["model_contract"] != spec["model_contract"]:
            failures.append(f"{query_id}:model_contract")

    total_anchors = sum(int(row["anchor_count"]) for row in results)
    total_valid = sum(int(row["valid_anchor_count"]) for row in results)
    if total_anchors < int(spec["minimum_structural_anchor_count_when_on"]):
        failures.append("structural_anchor_count_below_minimum")
    if total_valid != total_anchors:
        failures.append("not_all_emitted_anchors_valid")
    q021 = next((row for row in results if row["query_id"] == "q021"), None)
    if q021 is None or int(q021["prompt_render_count"]) < int(
        spec["q021_min_rendered_anchors_when_on"]
    ):
        failures.append("q021:rendered_anchor_floor")

    sealed_fingerprint = off.get("corpus_fingerprint_after")
    if before != sealed_fingerprint:
        failures.append("fresh_off_corpus_fingerprint_drifted_before_replay")
    if before != after:
        failures.append("corpus_fingerprint_changed_during_replay")
    raw_claim_store_before = (
        before.get("collections", {})
        .get("semantic_digest_claim_compilations", {})
        .get("sha256")
    )
    raw_claim_store_after = (
        after.get("collections", {})
        .get("semantic_digest_claim_compilations", {})
        .get("sha256")
    )
    if (
        not _SHA256_RE.fullmatch(str(raw_claim_store_before or ""))
        or raw_claim_store_before != raw_claim_store_after
    ):
        failures.append("raw_claim_store_changed")
    return failures


def _owner_prompt_receipt(
    rendered_at: datetime | None = None,
) -> tuple[dict[str, Any], datetime]:
    from services.chat_orchestrator import _build_polymath_system_prompt

    rendered_at = rendered_at or datetime.now().astimezone()
    return (
        build_system_prompt_receipt(
            _build_polymath_system_prompt,
            rendered_at,
            source_path=CHAT_ORCHESTRATOR_PATH,
        ),
        rendered_at,
    )


def _run_owner_sse(
    *,
    base: str,
    token: str,
    corpus_id: str,
    tier: str,
    question: str,
    conversation_id: str | None = None,
    timeout: float = 600.0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message": question,
        "corpus_ids": [corpus_id],
        "retrieval_tier": tier,
        "overrides": {"temperature": 0},
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
    encoded_payload = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/chat",
        data=encoded_payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    started = time.perf_counter()
    current_event = ""
    answer_parts: list[str] = []
    sources: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    errors: list[str] = []
    done_events: list[dict[str, Any]] = []
    conversation_ids: set[str] = set()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"chat HTTP status {response.status}")
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
                event = json.loads(encoded)
                event_type = str(event.get("type") or current_event)
                returned_conversation_id = str(event.get("conversation_id") or "")
                if returned_conversation_id:
                    conversation_ids.add(returned_conversation_id)
                if event_type == "token":
                    answer_parts.append(
                        str(event.get("content") or event.get("token") or "")
                    )
                elif event_type == "sources":
                    raw_sources = event.get("sources") or event.get("data") or []
                    sources = raw_sources if isinstance(raw_sources, list) else []
                elif event_type == "trace_event" or event.get("trace_event"):
                    traces.append(dict(event.get("trace_event") or event))
                elif event_type == "error":
                    errors.append(
                        str(event.get("content") or event.get("error") or "")[:500]
                    )
                elif event_type == "done":
                    done_events.append(event)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        errors.append(f"HTTP {exc.code}: {detail}")
    except Exception as exc:  # noqa: BLE001 - durable owner-window error
        errors.append(f"{type(exc).__name__}: {exc}"[:500])

    trace_contract = validate_chat_trace_contract(
        traces,
        done_events,
        expected_model=EXPECTED_CHAT_MODEL,
    )
    answerability = extract_answerability_contract(traces)
    prompt_hashes = sorted(
        {
            str(metadata[key])
            for trace in traces
            for metadata in [trace.get("metadata") or {}]
            if isinstance(metadata, dict)
            for key in ("prompt_template_hash", "prompt_hash", "template_hash")
            if metadata.get(key)
        }
    )
    return {
        "payload_sha256": hashlib.sha256(encoded_payload).hexdigest(),
        "request_temperature": 0,
        "conversation_id_sent": bool(conversation_id),
        "answer": "".join(answer_parts),
        "sources": sources,
        "traces": traces,
        "errors": errors,
        "done_events": done_events,
        "done_received": len(done_events) == 1,
        "conversation_ids": sorted(conversation_ids),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "trace_contract": trace_contract,
        "answerability": answerability,
        "model_used": trace_contract["model_route"]["model"],
        "model_skipped": trace_contract["model_skipped"],
        "prompt_template_hashes": prompt_hashes,
    }


def _capture_off_payload(
    *,
    settings: Any,
    spec: dict[str, Any],
    questions: list[dict[str, Any]],
    base: str,
    request_timeout: float,
) -> dict[str, Any]:
    mongo = MongoClient(settings.MONGODB_URI)
    db = mongo[settings.MONGODB_DATABASE]
    try:
        corpus = db.corpora.find_one(
            {"name": spec["corpus_name"], "status": "active"},
            {"_id": 0, "corpus_id": 1},
        )
        corpus_id = str((corpus or {}).get("corpus_id") or "")
        if not corpus_id:
            raise RuntimeError("owner-window claim corpus is absent")
        token = _mint_token(db, corpus_id)
        before = _fingerprint(db, corpus_id)
        prompt_receipt, prompt_rendered_at = _owner_prompt_receipt()
        results: list[dict[str, Any]] = []
        for question in questions:
            conversation_id: str | None = None
            history_receipts: list[dict[str, Any]] = []
            for history_ordinal, history_turn in enumerate(
                question.get("history") or [],
                start=1,
            ):
                prior = _run_owner_sse(
                    base=base,
                    token=token,
                    corpus_id=corpus_id,
                    tier=spec["tier"],
                    question=str(history_turn),
                    conversation_id=conversation_id,
                    timeout=request_timeout,
                )
                history_errors = [
                    *prior["errors"],
                    *prior["trace_contract"]["errors"],
                    *prior["answerability"]["errors"],
                ]
                if prior["prompt_template_hashes"] and prior[
                    "prompt_template_hashes"
                ] != [prompt_receipt["sha256"]]:
                    history_errors.append("history prompt template hash mismatch")
                history_receipts.append(
                    {
                        "ordinal": history_ordinal,
                        "payload_sha256": prior["payload_sha256"],
                        "request_temperature": prior["request_temperature"],
                        "done_received": prior["done_received"],
                        "model_used": prior["model_used"],
                        "model_skipped": prior["model_skipped"],
                        "model_route": prior["trace_contract"]["model_route"],
                        "answerability": prior["answerability"],
                        "system_prompt_template": prompt_receipt,
                        "journal_complete": not history_errors,
                        "journal_completeness_errors": history_errors,
                    }
                )
                if history_errors:
                    raise RuntimeError(
                        f"{question['id']} history telemetry incomplete: "
                        + ",".join(history_errors)
                    )
                conversation_id = str(
                    (prior["conversation_ids"] or [conversation_id or ""])[-1]
                )

            raw = _run_owner_sse(
                base=base,
                token=token,
                corpus_id=corpus_id,
                tier=spec["tier"],
                question=str(question["question"]),
                conversation_id=conversation_id,
                timeout=request_timeout,
            )
            trace = _anchor_trace(raw["traces"])
            selected_sources = [
                _source_without_claim_anchors(source) for source in raw["sources"]
            ]
            source_keys = _source_keys(selected_sources)
            errors = [
                *raw["errors"],
                *raw["trace_contract"]["errors"],
                *raw["answerability"]["errors"],
            ]
            if raw["prompt_template_hashes"] and raw["prompt_template_hashes"] != [
                prompt_receipt["sha256"]
            ]:
                errors.append("prompt template hash mismatch")
            if not selected_sources:
                errors.append("selected sources absent")
            row = {
                "query_id": question["id"],
                "shape": question["shape"],
                "source_keys": source_keys,
                "source_count": len(source_keys),
                "selected_sources": selected_sources,
                "selected_evidence_sha256_without_anchors": _source_fingerprint(
                    selected_sources
                ),
                "anchor_count": sum(
                    len(
                        (source.get("metadata") or {}).get("atomic_claim_anchors") or []
                    )
                    for source in raw["sources"]
                ),
                "prompt_render_count": int(trace.get("prompt_render_count") or 0),
                "model_used": raw["model_used"],
                "model_skipped": raw["model_skipped"],
                "model_route": raw["trace_contract"]["model_route"],
                "answerability": raw["answerability"],
                "request_temperature": raw["request_temperature"],
                "payload_sha256": raw["payload_sha256"],
                "system_prompt_template": prompt_receipt,
                "prior_call_session_state": {
                    "history_turn_count": len(history_receipts),
                    "conversation_id_sent": bool(conversation_id),
                    "history_receipts": history_receipts,
                },
                "elapsed_seconds": raw["elapsed_seconds"],
                "done_received": raw["done_received"],
                "errors": errors,
                "journal_complete": not errors,
            }
            results.append(row)
            print(
                json.dumps(
                    {
                        "query_id": row["query_id"],
                        "sources": row["source_count"],
                        "model": row["model_used"],
                        "temperature": row["request_temperature"],
                        "journal_complete": row["journal_complete"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

        final_prompt_receipt, _ = _owner_prompt_receipt(prompt_rendered_at)
        prompt_context_stable = final_prompt_receipt == prompt_receipt
        if not prompt_context_stable:
            for row in results:
                row["journal_complete"] = False
                row["errors"].append(
                    "system-prompt date/timezone changed during capture"
                )
        after = _fingerprint(db, corpus_id)
        failures: list[str] = []
        for row in results:
            if not row["journal_complete"] or row["done_received"] is not True:
                failures.append(f"{row['query_id']}:technical")
            if row["model_used"] != spec["model_contract"]:
                failures.append(f"{row['query_id']}:model_contract")
            if row["request_temperature"] != 0:
                failures.append(f"{row['query_id']}:temperature")
            if int(row["anchor_count"] or 0) or int(row["prompt_render_count"] or 0):
                failures.append(f"{row['query_id']}:off_exposure")
        if before != after:
            failures.append("corpus_fingerprint_changed")
        return {
            "schema_version": "claim_anchor_join_micro_ab_arm.v1",
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "arm": "off",
            "runtime_flag_enabled": False,
            "spec": spec,
            "corpus_id": corpus_id,
            "model_contract": spec["model_contract"],
            "request_temperature": 0,
            "system_prompt_template": prompt_receipt,
            "prompt_render_context_stable": prompt_context_stable,
            "corpus_fingerprint_before": before,
            "corpus_fingerprint_after": after,
            "corpus_fingerprint_equal": before == after,
            "results": results,
            "failures": failures,
            "passed": not failures,
        }
    finally:
        mongo.close()


def _capture_off(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise RuntimeError("output already exists; choose a fresh OFF artifact path")
    _require_outer_lock_environment(
        lock_owner=args.lock_owner,
        window_nonce=args.window_nonce,
        window_not_before_utc=args.window_not_before_utc,
    )
    spec, questions = _load_v2_contract(args.spec)
    settings = get_settings()
    runtime = _runtime_snapshot(settings)
    _require_runtime(runtime, claim_anchors_enabled=False)
    endpoint_binding = validate_same_container_runtime(
        validate_local_eval_api(args.base)
    )
    window = _window_identity(
        lock_owner=args.lock_owner,
        window_nonce=args.window_nonce,
        window_not_before_utc=args.window_not_before_utc,
    )
    off = _capture_off_payload(
        settings=settings,
        spec=spec,
        questions=questions,
        base=args.base,
        request_timeout=args.request_timeout,
    )
    off["endpoint_binding"] = endpoint_binding
    _atomic_write(args.output, off)
    if off.get("passed") is not True:
        return 1
    attested = _attest_off_payload(
        off=off,
        spec=spec,
        runtime=runtime,
        window=window,
    )
    _atomic_write(args.output, attested)
    print(
        "FRESH_OFF_ARTIFACT="
        + json.dumps(
            {
                "path": str(args.output),
                "sha256": _sha256_file(args.output),
                "runtime": runtime,
                "outer_host_lock": window,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


def _replay_on(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise RuntimeError("output already exists; choose a fresh ON artifact path")
    _require_outer_lock_environment(
        lock_owner=args.lock_owner,
        window_nonce=args.window_nonce,
        window_not_before_utc=args.window_not_before_utc,
    )
    spec, questions = _load_v2_contract(args.spec)
    settings = get_settings()
    runtime = _runtime_snapshot(settings)
    _require_runtime(runtime, claim_anchors_enabled=True)
    window = _window_identity(
        lock_owner=args.lock_owner,
        window_nonce=args.window_nonce,
        window_not_before_utc=args.window_not_before_utc,
    )
    off = _validate_fresh_off_artifact(
        off_path=args.off_artifact,
        expected_sha256=args.off_artifact_sha256,
        spec=spec,
        expected_window=window,
        max_age_seconds=args.max_off_age_seconds,
    )
    off_runtime = off["owner_window_attestation"]["capture_runtime"]
    _require_claim_only_transition(off_runtime, runtime)
    off_rows = list(off["results"])
    normalized_off_sha256 = _sha256_file(args.off_artifact)

    mongo = MongoClient(settings.MONGODB_URI)
    db = mongo[settings.MONGODB_DATABASE]
    try:
        before = _fingerprint(db, str(off["corpus_id"]))
        results = asyncio.run(
            _replay_owner_window(
                settings=settings,
                sync_db=db,
                questions=questions,
                off_rows=off_rows,
            )
        )
        after = _fingerprint(db, str(off["corpus_id"]))
        failures = _replay_failures(
            spec=spec,
            off=off,
            before=before,
            after=after,
            results=results,
        )
        total_anchors = sum(int(row["anchor_count"]) for row in results)
        total_valid = sum(int(row["valid_anchor_count"]) for row in results)
        total_rendered = sum(int(row["prompt_render_count"]) for row in results)
        total_readable = sum(int(row["readable_claim_count"]) for row in results)
        compilation_before = (
            before.get("collections", {})
            .get("semantic_digest_claim_compilations", {})
            .get("sha256")
        )
        compilation_after = (
            after.get("collections", {})
            .get("semantic_digest_claim_compilations", {})
            .get("sha256")
        )
        output = {
            "schema_version": OWNER_WINDOW_SCHEMA,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "arm": "on_post_fresh_final_selection_replay",
            "runtime": runtime,
            "spec": spec,
            "spec_sha256": V2_SPEC_SHA256,
            "corpus_id": off["corpus_id"],
            "off_artifact": str(args.off_artifact),
            "off_artifact_sha256": normalized_off_sha256,
            "outer_host_lock": window,
            "off_runtime": off_runtime,
            "off_system_prompt_template": off.get("system_prompt_template"),
            "off_endpoint_binding": off.get("endpoint_binding"),
            "model_contract": spec["model_contract"],
            "provider_calls": 0,
            "corpus_fingerprint_before": before,
            "corpus_fingerprint_after": after,
            "fresh_off_corpus_fingerprint_equal": (
                before == off.get("corpus_fingerprint_after")
            ),
            "corpus_fingerprint_equal": before == after,
            "raw_claim_store_sha256_before": compilation_before,
            "raw_claim_store_sha256_after": compilation_after,
            "raw_claim_store_byte_unchanged": (
                bool(compilation_before) and compilation_before == compilation_after
            ),
            "total_anchor_count": total_anchors,
            "total_valid_anchor_count": total_valid,
            "total_rendered_anchor_count": total_rendered,
            "total_readable_claim_count": total_readable,
            "structural_citation_precision": (
                total_valid / total_anchors if total_anchors else None
            ),
            "results": results,
            "failures": failures,
            "passed": not failures,
        }
        _atomic_write(args.output, output)
        print(
            json.dumps(
                {
                    "passed": not failures,
                    "failures": failures,
                    "anchors": total_anchors,
                    "valid": total_valid,
                    "rendered": total_rendered,
                    "readable": total_readable,
                    "corpus_fingerprint_equal": before == after,
                    "raw_claim_store_byte_unchanged": (
                        bool(compilation_before)
                        and compilation_before == compilation_after
                    ),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0 if not failures else 1
    finally:
        mongo.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser(
        "capture-off",
        help="capture and runtime-attest a fresh temporal-ON, claims-OFF packet",
    )
    capture.add_argument("--spec", type=Path, default=V2_SPEC)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--base", default="http://127.0.0.1:8000")
    capture.add_argument("--request-timeout", type=float, default=600.0)
    capture.add_argument(
        "--lock-owner",
        default=os.environ.get("POLYMATH_EVAL_LOCK_OWNER"),
        required=os.environ.get("POLYMATH_EVAL_LOCK_OWNER") is None,
    )
    capture.add_argument(
        "--window-nonce",
        default=os.environ.get("POLYMATH_EVAL_WINDOW_NONCE"),
        required=os.environ.get("POLYMATH_EVAL_WINDOW_NONCE") is None,
    )
    capture.add_argument(
        "--window-not-before-utc",
        default=os.environ.get("POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC"),
        required=os.environ.get("POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC") is None,
    )
    capture.set_defaults(handler=_capture_off)

    replay = subparsers.add_parser(
        "replay-on",
        help="provider-free replay over an explicitly SHA-bound fresh OFF packet",
    )
    replay.add_argument("--spec", type=Path, default=V2_SPEC)
    replay.add_argument("--off-artifact", type=Path, required=True)
    replay.add_argument("--off-artifact-sha256", required=True)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument(
        "--lock-owner",
        default=os.environ.get("POLYMATH_EVAL_LOCK_OWNER"),
        required=os.environ.get("POLYMATH_EVAL_LOCK_OWNER") is None,
    )
    replay.add_argument(
        "--window-nonce",
        default=os.environ.get("POLYMATH_EVAL_WINDOW_NONCE"),
        required=os.environ.get("POLYMATH_EVAL_WINDOW_NONCE") is None,
    )
    replay.add_argument(
        "--window-not-before-utc",
        default=os.environ.get("POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC"),
        required=os.environ.get("POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC") is None,
    )
    replay.add_argument("--max-off-age-seconds", type=int, default=1800)
    replay.set_defaults(handler=_replay_on)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
