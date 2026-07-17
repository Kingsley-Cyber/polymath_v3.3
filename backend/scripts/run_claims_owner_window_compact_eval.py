#!/usr/bin/env python3
"""Run the frozen direct/lay/original-negative floors on all standard tiers.

The 17-query preregistration and its scoring implementation remain read-only.
This runner hash-verifies the frozen inputs, selects only the preregistered
direct, lay-language, and original-negative rows, and delegates document-hit
scoring to the existing Agent-T frozen scorer. Refusal truth and trace
completeness come only from the shared canonical contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from evals.canonical_refusal_contract import (
    CLASSIFIER_VERSION,
    EXPECTED_CHAT_MODEL,
    build_system_prompt_receipt,
    classify_refusal,
    extract_answerability_contract,
    model_answer_content_errors,
    validate_chat_trace_contract,
    validate_claims_retrieval_runtime,
    validate_local_eval_api,
    validate_same_container_runtime,
)
from scripts.run_two_lane_anchoring_ab import (
    LOCK_PATH,
    PREREG,
    SELECTION,
    _eval_lock,
    _mean,
    _preflight,
    _score_frozen,
    _sha256,
)

COMPACT_SCHEMA = "claims_owner_window_compact_frozen_floor.v1"
OWNER_WINDOW_SCHEMA = "claim_anchor_owner_window.v1"
CHAT_ORCHESTRATOR_PATH = (
    Path(__file__).resolve().parents[1] / "services/chat_orchestrator.py"
)
FROZEN_SHA256 = {
    PREREG: "8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110",
    SELECTION: "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00",
}
INCLUDED_SHAPES = frozenset(
    {"direct_expert", "direct_fact", "lay_language", "negative_control"}
)
EXPECTED_SHAPE_COUNTS = {
    "direct_expert": 5,
    "direct_fact": 1,
    "lay_language": 4,
    "negative_control": 3,
}
STANDARD_TIERS = ("qdrant_only", "qdrant_mongo", "qdrant_mongo_graph")
LOCK_NONCE_PATH = Path("/tmp/polymath-eval.lock.nonce")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOW_NONCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        + b"\n"
    )
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _load_compact_queries() -> tuple[list[dict[str, Any]], dict[str, str]]:
    hashes: dict[str, str] = {}
    for path, expected in FROZEN_SHA256.items():
        actual = _sha256(path)
        hashes[str(path)] = actual
        if actual != expected:
            raise RuntimeError(
                f"frozen input hash mismatch: {path.name} {actual} != {expected}"
            )
    prereg = json.loads(PREREG.read_text(encoding="utf-8"))
    queries = [
        query
        for query in prereg.get("queries") or []
        if query.get("shape") in INCLUDED_SHAPES
    ]
    observed_counts = {
        shape: sum(1 for query in queries if query.get("shape") == shape)
        for shape in EXPECTED_SHAPE_COUNTS
    }
    if observed_counts != EXPECTED_SHAPE_COUNTS or len(queries) != 13:
        raise RuntimeError(
            "compact frozen subset drifted: "
            f"observed={observed_counts} expected={EXPECTED_SHAPE_COUNTS}"
        )
    return queries, hashes


def _normalized_utc(value: str, *, label: str) -> str:
    normalized = str(value or "").strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RuntimeError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _window_from_args(args: argparse.Namespace) -> dict[str, str]:
    owner = str(args.lock_owner or "").strip()
    if not owner:
        raise RuntimeError("eval lock owner must be non-empty")
    return {
        "lock_owner": owner,
        "window_nonce": _validate_window_nonce(args.window_nonce),
        "window_not_before_utc": _normalized_utc(
            args.window_not_before_utc,
            label="eval window not-before",
        ),
    }


def _require_claim_runtime(runtime: dict[str, Any], *, claims: bool) -> None:
    validate_claims_retrieval_runtime(
        runtime,
        claim_anchors_enabled=claims,
    )


def _validate_claims_on_artifact(
    args: argparse.Namespace,
) -> dict[str, Any]:
    expected_sha = str(args.claims_on_artifact_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(expected_sha):
        raise RuntimeError("claims ON artifact SHA must be 64 lowercase hex characters")
    actual_sha = hashlib.sha256(args.claims_on_artifact.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"claims ON artifact SHA drifted: {actual_sha} != {expected_sha}"
        )
    artifact = json.loads(args.claims_on_artifact.read_text(encoding="utf-8"))
    window = _window_from_args(args)
    failures: list[str] = []
    if artifact.get("schema_version") != OWNER_WINDOW_SCHEMA:
        failures.append("schema")
    if artifact.get("passed") is not True or artifact.get("failures"):
        failures.append("not_green")
    if artifact.get("provider_calls") != 0:
        failures.append("provider_calls")
    if artifact.get("outer_host_lock") != window:
        failures.append("outer_host_lock")
    captured_at = _normalized_utc(
        str(artifact.get("captured_at_utc") or ""),
        label="claims ON captured-at",
    )
    not_before = _normalized_utc(
        window["window_not_before_utc"],
        label="eval window not-before",
    )
    captured_dt = datetime.fromisoformat(captured_at)
    not_before_dt = datetime.fromisoformat(not_before)
    current = datetime.now(timezone.utc)
    if captured_dt < not_before_dt:
        failures.append("predates_window")
    if captured_dt > current + timedelta(seconds=60):
        failures.append("future_timestamp")
    if (
        args.max_window_artifact_age_seconds <= 0
        or (current - captured_dt).total_seconds()
        > args.max_window_artifact_age_seconds
    ):
        failures.append("stale_window_artifact")
    if str(artifact.get("corpus_id") or "") != args.corpus_id:
        failures.append("corpus_id")
    if artifact.get("corpus_fingerprint_equal") is not True:
        failures.append("corpus_fingerprint")
    if artifact.get("fresh_off_corpus_fingerprint_equal") is not True:
        failures.append("fresh_off_fingerprint")
    if artifact.get("raw_claim_store_byte_unchanged") is not True:
        failures.append("raw_claim_store")
    if artifact.get("model_contract") != args.expected_model:
        failures.append("model_contract")
    prompt_receipt = artifact.get("off_system_prompt_template")
    if (
        not isinstance(prompt_receipt, dict)
        or not _SHA256_RE.fullmatch(str(prompt_receipt.get("sha256") or ""))
        or not _SHA256_RE.fullmatch(str(prompt_receipt.get("source_sha256") or ""))
    ):
        failures.append("prompt_template")
    endpoint_binding = artifact.get("off_endpoint_binding")
    if (
        not isinstance(endpoint_binding, dict)
        or endpoint_binding.get("same_container_prompt_binding") is not True
        or endpoint_binding.get("loopback_required") is not True
    ):
        failures.append("endpoint_binding")
    runtime = artifact.get("runtime")
    off_runtime = artifact.get("off_runtime")
    if not isinstance(runtime, dict) or not isinstance(off_runtime, dict):
        failures.append("runtime_absent")
    else:
        _require_claim_runtime(off_runtime, claims=False)
        _require_claim_runtime(runtime, claims=True)
        expected_on = dict(off_runtime)
        expected_on["ATOMIC_CLAIM_ANCHORS_ENABLED"] = True
        if runtime != expected_on:
            failures.append("non_claim_runtime_delta")
    if failures:
        raise RuntimeError("claims ON artifact invalid: " + ",".join(failures))
    return {
        "path": str(args.claims_on_artifact),
        "sha256": actual_sha,
        "outer_host_lock": window,
        "runtime": runtime,
        "off_runtime": off_runtime,
    }


def _validate_window_nonce(value: str) -> str:
    nonce = str(value or "").strip()
    if not _WINDOW_NONCE_RE.fullmatch(nonce):
        raise RuntimeError(
            "eval window nonce must be 16-128 safe identifier characters"
        )
    return nonce


def _read_exact(path: Path, *, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"assert-held lock mode requires the {label}") from exc


@contextmanager
def _lock_context(args: argparse.Namespace) -> Iterator[None]:
    if args.lock_mode == "assert-held":
        if (
            Path("/.dockerenv").is_file()
            and os.environ.get("POLYMATH_EVAL_OUTER_LOCK_ATTESTED") == "1"
        ):
            expected_env = {
                "POLYMATH_EVAL_LOCK_OWNER": args.lock_owner,
                "POLYMATH_EVAL_WINDOW_NONCE": args.window_nonce,
                "POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC": args.window_not_before_utc,
            }
            drift = {
                key: {"observed": os.environ.get(key), "expected": value}
                for key, value in expected_env.items()
                if os.environ.get(key) != value
            }
            if drift:
                raise RuntimeError(
                    "outer host-lock environment drifted: "
                    + json.dumps(drift, sort_keys=True)
                )
            try:
                yield
            finally:
                final_drift = {
                    key: {
                        "observed": os.environ.get(key),
                        "expected": value,
                    }
                    for key, value in expected_env.items()
                    if os.environ.get(key) != value
                }
                if final_drift:
                    raise RuntimeError(
                        "outer host-lock environment changed: "
                        + json.dumps(final_drift, sort_keys=True)
                    )
            return
        observed_owner = _read_exact(LOCK_PATH, label="eval lock")
        if observed_owner != args.lock_owner:
            raise RuntimeError(
                f"eval lock owner mismatch: {observed_owner or 'unknown'} "
                f"!= {args.lock_owner}"
            )
        expected_nonce = _validate_window_nonce(args.window_nonce)
        observed_nonce = _read_exact(LOCK_NONCE_PATH, label="eval lock nonce")
        if observed_nonce != expected_nonce:
            raise RuntimeError(
                f"eval lock nonce mismatch: {observed_nonce or 'unknown'} "
                f"!= {expected_nonce}"
            )
        try:
            yield
        finally:
            if _read_exact(LOCK_PATH, label="eval lock") != args.lock_owner:
                raise RuntimeError("assert-held eval lock owner changed")
            if _read_exact(LOCK_NONCE_PATH, label="eval lock nonce") != expected_nonce:
                raise RuntimeError("assert-held eval lock nonce changed")
        return

    nonce = _validate_window_nonce(f"claims-{uuid.uuid4()}")
    args.window_nonce = nonce
    with _eval_lock(args.lock_owner, args.lock_wait_seconds):
        args.window_not_before_utc = datetime.now(timezone.utc).isoformat()
        descriptor = os.open(
            LOCK_NONCE_PATH,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(nonce + "\n")
            yield
        finally:
            if _read_exact(LOCK_PATH, label="eval lock") != args.lock_owner:
                raise RuntimeError("acquired eval lock owner changed")
            if _read_exact(LOCK_NONCE_PATH, label="eval lock nonce") != nonce:
                raise RuntimeError("acquired eval lock nonce changed")
            LOCK_NONCE_PATH.unlink()


def _prompt_template_receipt(
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


def _chat_temperature_zero(
    *,
    api: str,
    token: str,
    question: str,
    corpus_id: str,
    tier: str,
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{api.rstrip('/')}/api/chat",
        data=json.dumps(
            {
                "message": question,
                "corpus_ids": [corpus_id],
                "retrieval_tier": tier,
                "overrides": {"temperature": 0},
            },
            separators=(",", ":"),
        ).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
    )
    started = time.monotonic()
    current_event = ""
    answer_parts: list[str] = []
    sources: list[dict[str, Any]] = []
    selection: dict[str, Any] | None = None
    traces: list[dict[str, Any]] = []
    done_events: list[dict[str, Any]] = []
    error: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            buffer = b""
            while True:
                byte = response.read(1)
                if not byte:
                    break
                buffer += byte
                if not buffer.endswith(b"\n\n"):
                    continue
                block, buffer = buffer, b""
                for line in block.decode("utf-8", "replace").splitlines():
                    if line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    event = json.loads(raw)
                    event_type = str(event.get("type") or current_event)
                    if event_type == "token":
                        answer_parts.append(str(event.get("content") or ""))
                    elif event_type == "error":
                        error = str(event.get("content") or "unknown SSE error")
                    elif event_type == "done":
                        done_events.append(event)
                    elif event_type == "sources":
                        sources = [
                            {
                                "corpus_id": item.get("corpus_id"),
                                "doc_id": item.get("doc_id"),
                                "doc_name": item.get("doc_name"),
                                "chunk_id": item.get("chunk_id"),
                            }
                            for item in (event.get("sources") or [])
                        ]
                    if event_type == "trace_event" or event.get("trace_event"):
                        trace = dict(event.get("trace_event") or event)
                        traces.append(trace)
                        if trace.get("title") == "Local RAG retrieval":
                            diagnostics = (trace.get("metadata") or {}).get(
                                "retrieval_diagnostics"
                            )
                            if isinstance(diagnostics, dict):
                                candidate = (diagnostics.get("selection") or {}).get(
                                    "two_lane_anchoring"
                                )
                                selection = (
                                    candidate if isinstance(candidate, dict) else None
                                )
    except Exception as exc:  # noqa: BLE001 - durable eval error
        error = f"{type(exc).__name__}: {exc}"

    prompt_template_hashes: list[str] = []
    for trace in traces:
        metadata = trace.get("metadata") or {}
        for key in ("prompt_template_hash", "prompt_hash", "template_hash"):
            if metadata.get(key):
                prompt_template_hashes.append(str(metadata[key]))

    trace_contract = validate_chat_trace_contract(
        traces,
        done_events,
        expected_model=EXPECTED_CHAT_MODEL,
    )
    answerability = extract_answerability_contract(traces)
    return {
        "answer": "".join(answer_parts),
        "sources": sources,
        "two_lane_anchoring": selection,
        "error": error,
        "wall_s": round(time.monotonic() - started, 3),
        "done_received": len(done_events) == 1,
        "done_event_count": len(done_events),
        "model_used": trace_contract["model_route"]["model"],
        "model_skipped": trace_contract["model_skipped"],
        "model_route": trace_contract["model_route"],
        "trace_contract": trace_contract,
        "answerability": answerability,
        "prompt_template_hashes": sorted(set(prompt_template_hashes)),
        "request_temperature": 0,
    }


def _journal_contract_errors(
    result: dict[str, Any],
    *,
    prompt_receipt: dict[str, Any],
) -> list[str]:
    errors = [
        *result["trace_contract"]["errors"],
        *result["answerability"]["errors"],
        *model_answer_content_errors(
            result["answer"],
            model_skipped=result["model_skipped"],
        ),
    ]
    if result["prompt_template_hashes"] and result["prompt_template_hashes"] != [
        prompt_receipt["sha256"]
    ]:
        errors.append("trace/local prompt template hash mismatch")
    return errors


def _finalize(
    rows: list[dict[str, Any]],
    *,
    corpus_id: str,
    expected_model: str,
) -> dict[str, Any]:
    direct = [
        bool(row["doc_hit"])
        for row in rows
        if row["shape"] in {"direct_expert", "direct_fact"}
    ]
    lay = [bool(row["doc_hit"]) for row in rows if row["shape"] == "lay_language"]
    negatives = [
        bool(row["answerability_ok"])
        for row in rows
        if row["shape"] == "negative_control"
    ]
    membership = [
        all(
            str(source.get("corpus_id") or "") == corpus_id
            for source in row.get("sources") or []
        )
        for row in rows
    ]
    tier_metrics: dict[str, dict[str, Any]] = {}
    for tier in STANDARD_TIERS:
        tier_rows = [row for row in rows if row.get("tier") == tier]
        tier_direct = [
            bool(row["doc_hit"])
            for row in tier_rows
            if row["shape"] in {"direct_expert", "direct_fact"}
        ]
        tier_lay = [
            bool(row["doc_hit"]) for row in tier_rows if row["shape"] == "lay_language"
        ]
        tier_negatives = [
            bool(row["answerability_ok"])
            for row in tier_rows
            if row["shape"] == "negative_control"
        ]
        tier_metrics[tier] = {
            "execution_count": len(tier_rows),
            "direct_execution_count": len(tier_direct),
            "lay_execution_count": len(tier_lay),
            "original_negative_execution_count": len(tier_negatives),
            "direct_doc_hit_rate": _mean(tier_direct),
            "lay_language_doc_hit_rate": _mean(tier_lay),
            "original_negative_refusals": sum(tier_negatives),
        }
    metrics = {
        "execution_count": len(rows),
        "direct_execution_count": len(direct),
        "lay_execution_count": len(lay),
        "original_negative_execution_count": len(negatives),
        "direct_doc_hit_rate": _mean(direct),
        "lay_language_doc_hit_rate": _mean(lay),
        "original_negative_refusal_rate": _mean(negatives),
        "original_negative_refusals": sum(1 for value in negatives if value),
        "corpus_citation_membership_rate": _mean(membership),
        "technical_success_rate": _mean(
            [
                not row.get("error")
                and row.get("done_received") is True
                and row.get("model_used") == expected_model
                and row.get("request_temperature") == 0
                and row.get("journal_complete") is True
                for row in rows
            ]
        ),
        "tier_metrics": tier_metrics,
    }
    exact_tier_closure = all(
        value["execution_count"] == 13
        and value["direct_execution_count"] == 6
        and value["lay_execution_count"] == 4
        and value["original_negative_execution_count"] == 3
        for value in tier_metrics.values()
    )
    per_tier_floors = all(
        value["direct_doc_hit_rate"] >= 0.85
        and value["lay_language_doc_hit_rate"] >= 0.75
        and value["original_negative_refusals"] == 3
        for value in tier_metrics.values()
    )
    gates = {
        "execution_closure": (
            len(rows) == 39
            and len(direct) == 18
            and len(lay) == 12
            and len(negatives) == 9
            and {str(row.get("tier") or "") for row in rows} == set(STANDARD_TIERS)
            and exact_tier_closure
        ),
        "technical_success": metrics["technical_success_rate"] == 1.0,
        "direct": metrics["direct_doc_hit_rate"] >= 0.85,
        "lay": metrics["lay_language_doc_hit_rate"] >= 0.75,
        "original_negatives": metrics["original_negative_refusals"] == 9,
        "per_tier_floors": per_tier_floors,
        "corpus_citation_membership": (
            metrics["corpus_citation_membership_rate"] == 1.0
        ),
    }
    return {"metrics": metrics, "gates": gates, "passed": all(gates.values())}


def _run(args: argparse.Namespace) -> dict[str, Any]:
    queries, frozen_hashes = _load_compact_queries()
    claims_on_attestation = _validate_claims_on_artifact(args)
    endpoint_binding = validate_same_container_runtime(
        validate_local_eval_api(args.api)
    )
    preflight = _preflight(args.api)
    prompt_receipt, prompt_rendered_at = _prompt_template_receipt()
    rows: list[dict[str, Any]] = []
    journal_state: dict[str, Any] = {
        "schema_version": COMPACT_SCHEMA,
        "status": "running",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "api": args.api,
        "corpus_id": args.corpus_id,
        "tiers": list(STANDARD_TIERS),
        "expected_model": args.expected_model,
        "request_temperature": 0,
        "refusal_state_classifier": CLASSIFIER_VERSION,
        "system_prompt_template": prompt_receipt,
        "frozen_hashes": frozen_hashes,
        "claims_on_attestation": claims_on_attestation,
        "endpoint_binding": endpoint_binding,
        "embedder_preflight": preflight,
        "results": rows,
    }
    _atomic_write(args.output, journal_state)
    prior_call_index = 0
    for tier in STANDARD_TIERS:
        for query in queries:
            result = _chat_temperature_zero(
                api=args.api,
                token=args.token,
                question=str(query["question"]),
                corpus_id=args.corpus_id,
                tier=tier,
                timeout=args.request_timeout,
            )
            scored = _score_frozen(query, result)
            scored.pop("refused", None)
            scored.pop("answerability_ok", None)
            classification = classify_refusal(
                result["answer"],
                model_skipped=result["model_skipped"] is True,
            )
            journal_errors = _journal_contract_errors(
                result,
                prompt_receipt=prompt_receipt,
            )
            row = {
                "id": query["id"],
                "shape": query["shape"],
                "tier": tier,
                **scored,
                **result,
                "refused": classification["refused"],
                "answerability_ok": (
                    classification["refused"]
                    if query["shape"] == "negative_control"
                    else not classification["refused"]
                ),
                "refusal_state": classification["state"],
                "refusal_state_classifier": CLASSIFIER_VERSION,
                "classification": classification,
                "system_prompt_template": dict(prompt_receipt),
                "journal_complete": not journal_errors,
                "journal_completeness_errors": journal_errors,
                "prior_call_session_state": {
                    "preceding_calls_in_process": prior_call_index,
                    "history_turn_count": 0,
                    "conversation_id_sent": False,
                    "concurrency": 1,
                },
            }
            rows.append(row)
            journal_state["results"] = rows
            journal_state["last_completed"] = {
                "tier": tier,
                "query_id": row["id"],
                "execution_count": len(rows),
            }
            _atomic_write(args.output, journal_state)
            prior_call_index += 1
            print(
                f"{tier} {row['id']} shape={row['shape']} "
                f"hit={row['doc_hit']} answerability={row['answerability_ok']} "
                f"error={row['error']}",
                flush=True,
            )
    final_prompt_receipt, _ = _prompt_template_receipt(prompt_rendered_at)
    prompt_context_stable = final_prompt_receipt == prompt_receipt
    if not prompt_context_stable:
        for row in rows:
            row["journal_complete"] = False
            row["journal_completeness_errors"].append(
                "system-prompt date/timezone changed during batch"
            )
    final = _finalize(
        rows,
        corpus_id=args.corpus_id,
        expected_model=args.expected_model,
    )
    return {
        "schema_version": COMPACT_SCHEMA,
        "status": "complete",
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "api": args.api,
        "corpus_id": args.corpus_id,
        "tiers": list(STANDARD_TIERS),
        "expected_model": args.expected_model,
        "request_temperature": 0,
        "refusal_state_classifier": CLASSIFIER_VERSION,
        "system_prompt_template": prompt_receipt,
        "prompt_render_context_stable": prompt_context_stable,
        "frozen_hashes": frozen_hashes,
        "selection_rule": {
            "included_shapes": sorted(INCLUDED_SHAPES),
            "excluded_shape": "relationship_multi_document",
            "query_count_per_tier": 13,
            "execution_count": 39,
        },
        "claims_on_attestation": claims_on_attestation,
        "endpoint_binding": endpoint_binding,
        "embedder_preflight": preflight,
        **final,
        "results": rows,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--claims-on-artifact", type=Path, required=True)
    parser.add_argument("--claims-on-artifact-sha256", required=True)
    parser.add_argument(
        "--api",
        default=os.environ.get("POLYMATH_API", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument("--max-window-artifact-age-seconds", type=int, default=1800)
    parser.add_argument("--lock-wait-seconds", type=int, default=3600)
    parser.add_argument(
        "--lock-mode",
        choices=("acquire", "assert-held"),
        default="assert-held",
        help="acquire the eval lock or assert a surrounding atomic window owns it",
    )
    parser.add_argument(
        "--lock-owner",
        default=os.environ.get(
            "POLYMATH_EVAL_LOCK_OWNER",
            "codex/claims-owner-window-harness-20260717",
        ),
    )
    parser.add_argument(
        "--window-nonce",
        default=os.environ.get("POLYMATH_EVAL_WINDOW_NONCE"),
    )
    parser.add_argument(
        "--window-not-before-utc",
        default=os.environ.get("POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.expected_model = EXPECTED_CHAT_MODEL
    args.token = os.environ.get("POLYMATH_EVAL_TOKEN", "")
    if not args.token:
        raise RuntimeError("POLYMATH_EVAL_TOKEN environment variable is required")
    if args.lock_mode == "assert-held" and (
        not args.window_nonce or not args.window_not_before_utc
    ):
        raise RuntimeError(
            "assert-held mode requires window nonce and not-before environment"
        )
    if args.output.exists():
        raise RuntimeError("output already exists; choose a fresh journal path")
    with _lock_context(args):
        output = _run(args)
        _atomic_write(args.output, output)
    print(
        json.dumps(
            {
                "passed": output["passed"],
                "metrics": output["metrics"],
                "gates": output["gates"],
                "output": str(args.output),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if output["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
