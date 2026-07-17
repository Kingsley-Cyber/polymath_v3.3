#!/usr/bin/env python3
"""Run the immutable 28-probe held-out refusal measurement.

This harness deliberately separates transport health from refusal measurement.
It exits non-zero only when the run is technically incomplete or the durable
journal is incomplete.  The observed refusal rate is never an exit gate.

Prompt-template hash method (``polymath.chat_system_prompt_render.v1``):
the runner must execute inside the backend container against a loopback API.
It imports the production ``_build_polymath_system_prompt`` function, renders
it once with an explicit timezone-aware wall-clock value immediately before
the batch, and hashes the exact UTF-8 result plus the orchestrator source.
Before sealing it repeats both hashes at the original render time.  This binds
the receipt to the same baked code and local endpoint used by the requests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from config import get_settings
from evals.canonical_refusal_contract import (
    CLASSIFIER_VERSION,
    EXPECTED_CHAT_MODEL,
    classify_refusal,
    validate_chat_trace_contract,
)


BACKEND = Path(__file__).resolve().parents[1]
SPEC_PATH = BACKEND / "evals/e2e_heldout_negative_v2_20260717.json"
SELECTION_PATH = BACKEND / "evals/runpod_e2e_15doc_selection_v1.json"
CHAT_ORCHESTRATOR_PATH = BACKEND / "services/chat_orchestrator.py"
LOCK_PATH = Path("/tmp/polymath-eval.lock")

SPEC_SHA256 = "3b35c14c165f6be89202b809ea01a1cd6ad0f5c0217e4167b86e4b5dc0b09960"
SELECTION_SHA256 = "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00"
CORPUS_ID = "2c894530-8d57-4432-a6d4-bc14505a698b"
CORPUS_NAME = "runpod_e2e_15doc_20260715"
TIER = "qdrant_mongo_graph"
TOP_K = 8
TEMPERATURE = 0.0
EXECUTION_COUNT = 28
MAX_COST_USD = 1.50

JOURNAL_SCHEMA = "polymath.canonical_heldout_negative_eval.v1"
EXECUTION_SCHEMA = "polymath.canonical_heldout_negative_execution.v1"
PROMPT_HASH_METHOD = "polymath.chat_system_prompt_render.v1"

MINIMAX_INPUT_USD_PER_MILLION = 0.30
MINIMAX_OUTPUT_USD_PER_MILLION = 1.20
MODEL_COMPLETION_TOKEN_BOUND = 16_384
MEASURED_SYSTEM_PROMPT_TOKENS = 2_338
MEASURED_MAX_EVIDENCE_CHARS = 13_293
PROMPT_WRAPPER_TOKEN_ALLOWANCE = 4_096

REQUIRED_EXECUTION_PATHS = (
    "schema_version",
    "execution_id",
    "query_id",
    "family",
    "question_sha256",
    "request.payload_sha256",
    "request.temperature",
    "request.top_k",
    "request.conversation_id_sent",
    "prior_call_state.process_run_id",
    "prior_call_state.request_ordinal",
    "prior_call_state.prior_call_count",
    "prior_call_state.session_mode",
    "transport.done_received",
    "transport.done_event_count",
    "transport.errors",
    "technical.status",
    "technical.ok",
    "answerability.raw_answerable",
    "answerability.telemetry",
    "answerability.guard.eligible",
    "answerability.guard.coverage",
    "model_skipped",
    "trace_contract.ok",
    "trace_contract.assistant_final_trace_count",
    "trace_contract.model_route_trace_count",
    "trace_contract.expected_model",
    "model_route.model",
    "system_prompt_template.method_version",
    "system_prompt_template.sha256",
    "system_prompt_template.source_sha256",
    "classification.version",
    "classification.state",
    "classification.refused",
    "answer.sha256",
    "answer.excerpt",
    "sources.membership_count",
    "sources.all_in_selected_corpus",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        + b"\n"
    )
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _load_hashed_json(path: Path, expected_sha256: str, label: str) -> dict[str, Any]:
    payload = path.read_bytes()
    actual = _sha256_bytes(payload)
    if actual != expected_sha256:
        raise RuntimeError(f"{label} hash drifted: {actual} != {expected_sha256}")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _cost_envelope(execution_count: int = EXECUTION_COUNT) -> dict[str, Any]:
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
    if total_usd > MAX_COST_USD:
        raise RuntimeError(
            f"two-attempt cost envelope ${total_usd:.6f} exceeds ${MAX_COST_USD:.2f}"
        )
    return {
        "schema_version": "chat_eval_cost_envelope.v1",
        "execution_count": execution_count,
        "attempt_bound": attempts,
        "input_tokens_per_attempt_bound": input_tokens_per_attempt,
        "completion_tokens_per_attempt_bound": MODEL_COMPLETION_TOKEN_BOUND,
        "input_usd": round(input_usd, 9),
        "output_usd": round(output_usd, 9),
        "total_usd": round(total_usd, 9),
        "ceiling_usd": MAX_COST_USD,
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


def _value_at_path(value: dict[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = value
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def execution_completeness_errors(row: dict[str, Any]) -> list[str]:
    missing = [
        path for path in REQUIRED_EXECUTION_PATHS if not _value_at_path(row, path)[0]
    ]
    errors = [f"missing:{path}" for path in missing]
    state = (row.get("classification") or {}).get("state")
    if state not in {"gate_blocked", "model_voiced_refusal", "answered"}:
        errors.append("invalid:classification.state")
    if (row.get("request") or {}).get("temperature") != TEMPERATURE:
        errors.append("invalid:request.temperature")
    if (row.get("request") or {}).get("top_k") != TOP_K:
        errors.append("invalid:request.top_k")
    if (row.get("request") or {}).get("conversation_id_sent") is not False:
        errors.append("invalid:request.conversation_id_sent")
    if (row.get("model_route") or {}).get("model") != EXPECTED_CHAT_MODEL:
        errors.append("invalid:model_route.model")
    if not str(((row.get("answer") or {}).get("sha256")) or ""):
        errors.append("empty:answer.sha256")
    answerability = row.get("answerability") or {}
    guard = answerability.get("guard") or {}
    if not isinstance(answerability.get("telemetry"), dict) or not answerability.get(
        "telemetry"
    ):
        errors.append("invalid:answerability.telemetry")
    if not isinstance(answerability.get("raw_answerable"), bool):
        errors.append("invalid:answerability.raw_answerable")
    if not isinstance(guard.get("eligible"), bool):
        errors.append("invalid:answerability.guard.eligible")
    coverage = guard.get("coverage")
    if coverage is not None and (
        not isinstance(coverage, (int, float))
        or isinstance(coverage, bool)
        or not 0.0 <= float(coverage) <= 1.0
    ):
        errors.append("invalid:answerability.guard.coverage")
    if not isinstance(row.get("model_skipped"), bool):
        errors.append("invalid:model_skipped")
    trace_contract = row.get("trace_contract") or {}
    if trace_contract.get("ok") is not True:
        errors.append("invalid:trace_contract.ok")
    if trace_contract.get("assistant_final_trace_count") != 1:
        errors.append("invalid:trace_contract.assistant_final_trace_count")
    if trace_contract.get("model_route_trace_count") != 1:
        errors.append("invalid:trace_contract.model_route_trace_count")
    if trace_contract.get("expected_model") != EXPECTED_CHAT_MODEL:
        errors.append("invalid:trace_contract.expected_model")
    if (row.get("transport") or {}).get("done_event_count") != 1:
        errors.append("invalid:transport.done_event_count")
    classification = row.get("classification") or {}
    if (
        row.get("model_skipped") is True
        and classification.get("state") != "gate_blocked"
    ):
        errors.append("inconsistent:model_skipped/classification.state")
    return errors


def _extract_answerability(traces: list[dict[str, Any]]) -> dict[str, Any]:
    for trace in reversed(traces):
        if trace.get("title") != "Answerability gate":
            continue
        metadata = trace.get("metadata")
        if not isinstance(metadata, dict):
            break
        guard = metadata.get("corpus_scope_guard")
        if not isinstance(guard, dict):
            guard = {}
        return {
            "telemetry": json.loads(json.dumps(metadata, default=str)),
            "status": metadata.get("status"),
            "answerable": metadata.get("answerable"),
            "raw_answerable": metadata.get("raw_answerable"),
            "required_atoms": metadata.get("required_atoms"),
            "covered_required_atoms": metadata.get("covered_required_atoms"),
            "missing_atoms": metadata.get("missing_atoms"),
            "missing_critical_atoms": metadata.get("missing_critical_atoms"),
            "required_coverage": metadata.get("required_coverage"),
            "policy_version": metadata.get("answerability_policy_version"),
            "coverage_threshold": metadata.get("coverage_threshold"),
            "lane_coverage": metadata.get("lane_coverage"),
            "answer_shape": metadata.get("answer_shape"),
            "diagnostic_source": metadata.get("diagnostic_source"),
            "evidence_plan": metadata.get("evidence_plan"),
            "guard": {
                "enabled": guard.get("enabled"),
                "eligible": guard.get("eligible"),
                "terms": guard.get("terms"),
                "matched_terms": guard.get("matched_terms"),
                "missing_terms": guard.get("missing_terms"),
                "coverage": guard.get("coverage"),
                "min_terms": guard.get("min_terms"),
                "min_coverage": guard.get("min_coverage"),
                "supported": guard.get("supported"),
                "applied": guard.get("applied"),
                "reason": guard.get("reason"),
            },
        }
    return {}


def _effective_tier(traces: list[dict[str, Any]]) -> str:
    for trace in reversed(traces):
        if trace.get("title") not in {"Synthesis lens", "Local RAG retrieval"}:
            continue
        tier = str((trace.get("metadata") or {}).get("effective_tier") or "")
        if tier:
            return tier
    return ""


def _http_json(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 40.0,
) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = _canonical_bytes(payload)
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _embedder_preflight(api: str) -> dict[str, Any]:
    value = _http_json(
        f"{api.rstrip('/')}/api/health/embedder/batch-ready",
        method="POST",
        payload={},
    )
    if not isinstance(value, dict) or value.get("status") != "ready":
        raise RuntimeError(f"embedder preflight refused: {value!r}")
    return value


def _verify_corpus(
    api: str,
    token: str,
    selected_filenames: set[str],
) -> tuple[dict[str, str], dict[str, Any]]:
    corpora = _http_json(f"{api.rstrip('/')}/api/corpora", token=token)
    if not isinstance(corpora, list):
        raise RuntimeError("corpus list response is not an array")
    corpus = next(
        (
            row
            for row in corpora
            if isinstance(row, dict) and str(row.get("corpus_id") or "") == CORPUS_ID
        ),
        None,
    )
    if not corpus or str(corpus.get("name") or "") != CORPUS_NAME:
        raise RuntimeError("exact preregistered corpus id/name is not available")
    url = (
        f"{api.rstrip('/')}/api/corpora/{urllib.parse.quote(CORPUS_ID)}/documents"
        "?limit=100&offset=0"
    )
    documents = _http_json(url, token=token)
    if not isinstance(documents, list) or len(documents) != 15:
        raise RuntimeError(
            f"exact preregistered corpus must expose 15 documents, got "
            f"{len(documents) if isinstance(documents, list) else 'non-list'}"
        )
    names = {
        str(row.get("doc_id") or ""): str(
            row.get("original_filename") or row.get("filename") or ""
        )
        for row in documents
        if isinstance(row, dict)
    }
    if not all(names) or set(names.values()) != selected_filenames:
        raise RuntimeError("live corpus document identities drifted from selection")
    receipt = {
        "corpus_id": CORPUS_ID,
        "corpus_name": CORPUS_NAME,
        "document_count": len(names),
        "document_identity_sha256": _sha256_bytes(
            _canonical_bytes(sorted(names.items()))
        ),
    }
    return names, receipt


def _request_payload(question: str) -> dict[str, Any]:
    return {
        "message": question,
        "corpus_ids": [CORPUS_ID],
        "retrieval_tier": TIER,
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
    question: str,
    timeout: float,
) -> dict[str, Any]:
    payload = _request_payload(question)
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
            if response.status != 200:
                raise RuntimeError(f"chat HTTP status {response.status}")
            content_type = str(response.headers.get("Content-Type") or "")
            if "text/event-stream" not in content_type:
                raise RuntimeError(f"chat content type is not SSE: {content_type}")
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
    except Exception as exc:  # noqa: BLE001 - transport failure is journaled
        errors.append(f"{type(exc).__name__}: {exc}"[:500])
    return {
        "payload": payload,
        "answer": "".join(answer_parts),
        "sources": sources,
        "traces": traces,
        "errors": errors,
        "done": done_events[-1] if done_events else {},
        "done_events": done_events,
        "event_counts": dict(sorted(event_counts.items())),
        "conversation_ids": sorted(conversation_ids),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _source_receipt(
    sources: list[dict[str, Any]],
    document_names: dict[str, str],
    selected_filenames: set[str],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for source in sources:
        doc_id = str(source.get("doc_id") or "")
        filename = str(
            document_names.get(doc_id)
            or source.get("doc_name")
            or source.get("filename")
            or ""
        )
        member = (
            str(source.get("corpus_id") or "") == CORPUS_ID
            and doc_id in document_names
            and filename in selected_filenames
        )
        rows.append(
            {
                "corpus_id": source.get("corpus_id"),
                "doc_id": doc_id,
                "doc_name": filename,
                "chunk_id": source.get("chunk_id"),
                "selected_corpus_member": member,
            }
        )
    return {
        "count": len(rows),
        "membership_count": sum(row["selected_corpus_member"] is True for row in rows),
        "all_in_selected_corpus": all(
            row["selected_corpus_member"] is True for row in rows
        ),
        "items": rows,
    }


def _validate_local_api(api: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(api)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeError("--api must be a credential-free loopback HTTP(S) origin")
    return {
        "schema_version": "polymath.local_eval_endpoint_binding.v1",
        "api_origin": api.rstrip("/"),
        "hostname": hostname,
        "loopback_required": True,
    }


def _validate_same_container_runtime(
    endpoint_binding: dict[str, Any],
    *,
    container_marker: Path = Path("/.dockerenv"),
) -> dict[str, Any]:
    if not container_marker.is_file():
        raise RuntimeError(
            "canonical harness must run inside the backend container so the "
            "loopback API and imported prompt builder are the same runtime"
        )
    return {
        **endpoint_binding,
        "container_marker": str(container_marker),
        "same_container_prompt_binding": True,
        "binding_method": (
            "container marker + loopback API + imported production builder/source"
        ),
    }


def _prompt_template_receipt(
    rendered_at: datetime | None = None,
) -> tuple[dict[str, Any], datetime]:
    from services.chat_orchestrator import _build_polymath_system_prompt

    rendered_at = rendered_at or datetime.now().astimezone()
    rendered = _build_polymath_system_prompt(rendered_at)
    source_payload = CHAT_ORCHESTRATOR_PATH.read_bytes()
    return (
        {
            "method_version": PROMPT_HASH_METHOD,
            "sha256": _sha256_bytes(rendered.encode("utf-8")),
            "source_sha256": _sha256_bytes(source_payload),
            "source_path": "services/chat_orchestrator.py",
            "rendered_for_local_date": rendered_at.strftime("%Y-%m-%d"),
            "rendered_for_timezone_name": rendered_at.tzname() or "local time",
            "utf8_bytes": len(rendered.encode("utf-8")),
            "builder": "services.chat_orchestrator._build_polymath_system_prompt",
        },
        rendered_at,
    )


def _runtime_flags(expected_temporal: bool) -> dict[str, bool]:
    settings = get_settings()
    expected = {
        "RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED": True,
        "ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED": True,
        "TEMPORAL_QUERY_ROUTING_ENABLED": expected_temporal,
        "RERANK_EVIDENCE_SUPPORT": False,
        "ATOMIC_CLAIM_ANCHORS_ENABLED": False,
        "PARENT_EXCERPT_ENABLED": False,
        "WATERFALL_ASSEMBLY": False,
        "TWO_LANE_ANCHORING": False,
        "TWO_LANE_ANCHORING_ENABLED": False,
        "HYDE_ENABLED": False,
        "SHELF_RESERVE_ENABLED": False,
        "GROUNDED_QUERY_PLANNER_ENABLED": False,
        "FOUR_LANE_TIER0_ROUTER_ENABLED": False,
        "FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED": False,
        "AGENTIC_MODE_ENABLED": False,
    }
    observed = {name: bool(getattr(settings, name)) for name in expected}
    if observed != expected:
        raise RuntimeError(
            "runtime flags do not match canonical harness contract: "
            + json.dumps({"expected": expected, "observed": observed}, sort_keys=True)
        )
    return observed


@contextmanager
def _eval_lock(
    owner: str,
    wait_seconds: int,
    *,
    mode: str = "acquire",
    lock_path: Path = LOCK_PATH,
) -> Iterator[None]:
    expected_payload = f"{owner}\n"
    if mode == "assert-held":
        try:
            observed = lock_path.read_text(encoding="utf-8", errors="strict")
        except FileNotFoundError as exc:
            raise RuntimeError("eval lock is not held") from exc
        if observed != expected_payload:
            raise RuntimeError(
                f"eval lock owner mismatch: {observed!r} != {expected_payload!r}"
            )
        try:
            yield
        finally:
            try:
                final_owner = lock_path.read_text(encoding="utf-8", errors="strict")
            except FileNotFoundError as exc:
                raise RuntimeError("assert-held eval lock disappeared") from exc
            if final_owner != expected_payload:
                raise RuntimeError("assert-held eval lock owner changed")
        return
    if mode != "acquire":
        raise RuntimeError(f"unsupported eval lock mode: {mode!r}")

    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        try:
            descriptor = os.open(
                lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
            )
        except FileExistsError:
            if time.monotonic() >= deadline:
                holder = lock_path.read_text(encoding="utf-8", errors="replace")
                raise RuntimeError(f"eval lock held by {holder!r}")
            time.sleep(60)
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(expected_payload)
        break
    try:
        yield
    finally:
        try:
            observed = lock_path.read_text(encoding="utf-8", errors="strict")
        except FileNotFoundError:
            raise RuntimeError("acquired eval lock disappeared before release")
        if observed != expected_payload:
            raise RuntimeError("acquired eval lock owner changed before release")
        lock_path.unlink()


def _seal_journal(state: dict[str, Any]) -> dict[str, Any]:
    value = dict(state)
    value.pop("seal", None)
    return {
        "schema_version": "polymath.atomic_json_journal_seal.v1",
        "canonical_payload_sha256": _sha256_bytes(_canonical_bytes(value)),
        "sealed_at_utc": _utc_now(),
    }


def _build_execution(
    *,
    case: dict[str, Any],
    ordinal: int,
    process_run_id: str,
    raw: dict[str, Any],
    prompt_receipt: dict[str, Any],
    document_names: dict[str, str],
    selected_filenames: set[str],
) -> dict[str, Any]:
    answer = str(raw["answer"])
    answerability = _extract_answerability(raw["traces"])
    trace_contract = validate_chat_trace_contract(
        raw["traces"],
        raw["done_events"],
        expected_model=EXPECTED_CHAT_MODEL,
    )
    model_route = trace_contract["model_route"]
    model_skipped = trace_contract["model_skipped"]
    classification = classify_refusal(
        answer,
        model_skipped=model_skipped is True,
    )
    sources = _source_receipt(
        raw["sources"],
        document_names,
        selected_filenames,
    )
    effective_tier = _effective_tier(raw["traces"])
    technical_errors = list(raw["errors"])
    technical_errors.extend(trace_contract["errors"])
    if len(raw["done_events"]) != 1:
        technical_errors.append(
            f"SSE done event count must be 1, observed {len(raw['done_events'])}"
        )
    if effective_tier != TIER:
        technical_errors.append(f"effective tier mismatch: {effective_tier!r}")
    if not answerability:
        technical_errors.append("missing Answerability gate trace")
    if model_skipped is not True and not answer.strip():
        technical_errors.append("model-called response has empty answer")
    if not sources["all_in_selected_corpus"]:
        technical_errors.append("source escaped preregistered corpus selection")
    payload = raw["payload"]
    row = {
        "schema_version": EXECUTION_SCHEMA,
        "execution_id": f"{case['id']}::{TIER}",
        "query_id": str(case["id"]),
        "family": case.get("family"),
        "question_sha256": _sha256_bytes(str(case["question"]).encode("utf-8")),
        "request": {
            "payload_sha256": _sha256_bytes(_canonical_bytes(payload)),
            "token_excluded_from_hash": True,
            "corpus_id": CORPUS_ID,
            "tier": TIER,
            "temperature": TEMPERATURE,
            "top_k": TOP_K,
            "conversation_id_sent": "conversation_id" in payload,
        },
        "prior_call_state": {
            "process_run_id": process_run_id,
            "request_ordinal": ordinal,
            "prior_call_count": ordinal - 1,
            "history_turn_count_sent": 0,
            "session_mode": "fresh_conversation_per_probe_sequential_process",
            "concurrency": 1,
            "returned_conversation_ids": raw["conversation_ids"],
        },
        "transport": {
            "done_received": bool(raw["done_events"]),
            "done_event_count": len(raw["done_events"]),
            "errors": list(raw["errors"]),
            "event_counts": raw["event_counts"],
            "elapsed_seconds": raw["elapsed_seconds"],
            "effective_tier": effective_tier,
        },
        "answerability": answerability,
        "model_skipped": model_skipped,
        "model_route": model_route,
        "trace_contract": trace_contract,
        "system_prompt_template": dict(prompt_receipt),
        "classification": classification,
        "answer": {
            "chars": len(answer),
            "sha256": _sha256_bytes(answer.encode("utf-8")),
            "excerpt": answer[:800],
        },
        "sources": sources,
        "technical": {
            "status": "ok" if not technical_errors else "failed",
            "ok": not technical_errors,
            "errors": technical_errors,
        },
        "journal_complete": False,
        "journal_completeness_errors": [],
    }
    completeness = execution_completeness_errors(row)
    row["journal_completeness_errors"] = completeness
    row["journal_complete"] = not completeness
    return row


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the canonical immutable held-out refusal measurement."
    )
    parser.add_argument("--expected-temporal", choices=("off", "on"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument(
        "--lock-owner",
        default="codex/canonical-refusal-harness-20260717",
    )
    parser.add_argument("--lock-wait-seconds", type=int, default=0)
    parser.add_argument(
        "--lock-mode",
        choices=("acquire", "assert-held"),
        default="acquire",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    token = os.getenv("POLYMATH_EVAL_TOKEN")
    if not token:
        raise RuntimeError("POLYMATH_EVAL_TOKEN is required")
    if args.output.exists():
        raise RuntimeError("output already exists; choose a fresh journal path")
    endpoint_binding = _validate_same_container_runtime(_validate_local_api(args.api))

    spec = _load_hashed_json(SPEC_PATH, SPEC_SHA256, "held-out negative spec")
    selection = _load_hashed_json(
        SELECTION_PATH,
        SELECTION_SHA256,
        "15-document selection",
    )
    queries = list(spec.get("queries") or [])
    if (
        spec.get("schema_version") != "polymath.e2e_heldout_negative.v2"
        or spec.get("used_for_tuning") is not False
        or spec.get("corpus_id") != CORPUS_ID
        or spec.get("corpus_name") != CORPUS_NAME
        or spec.get("selection_sha256") != SELECTION_SHA256
        or len(queries) != EXECUTION_COUNT
        or any(case.get("must_refuse") is not True for case in queries)
    ):
        raise RuntimeError("held-out negative spec contract drifted")
    selected_filenames = {
        str(row["filename"]) for row in list(selection.get("selected") or [])
    }
    if len(selected_filenames) != 15:
        raise RuntimeError("15-document selection contract drifted")

    expected_temporal = args.expected_temporal == "on"
    runtime_flags = _runtime_flags(expected_temporal)
    cost_envelope = _cost_envelope()
    process_run_id = str(uuid.uuid4())

    with _eval_lock(
        args.lock_owner,
        args.lock_wait_seconds,
        mode=args.lock_mode,
    ):
        preflight = _embedder_preflight(args.api)
        document_names, corpus_receipt = _verify_corpus(
            args.api,
            token,
            selected_filenames,
        )
        prompt_receipt, prompt_rendered_at = _prompt_template_receipt()
        state: dict[str, Any] = {
            "schema_version": JOURNAL_SCHEMA,
            "started_at_utc": _utc_now(),
            "completed_at_utc": None,
            "sealed": False,
            "measurement_only": True,
            "used_for_tuning": False,
            "refusal_rate_is_exit_gate": False,
            "classifier_version": CLASSIFIER_VERSION,
            "spec_sha256": SPEC_SHA256,
            "selection_sha256": SELECTION_SHA256,
            "corpus": corpus_receipt,
            "tier": TIER,
            "top_k": TOP_K,
            "temperature": TEMPERATURE,
            "runtime_flags": runtime_flags,
            "endpoint_binding": endpoint_binding,
            "authentication": {"token_source": "POLYMATH_EVAL_TOKEN"},
            "embedder_preflight": preflight,
            "system_prompt_template": prompt_receipt,
            "cost_envelope": cost_envelope,
            "process_run_id": process_run_id,
            "session_contract": {
                "concurrency": 1,
                "fresh_conversation_per_probe": True,
                "conversation_id_sent": False,
                "ordered_as_frozen_spec": True,
            },
            "executions": [],
            "summary": None,
            "seal": None,
        }
        _atomic_write(args.output, state)
        print(
            "CANONICAL_REFUSAL_START "
            + json.dumps(
                {
                    "executions": EXECUTION_COUNT,
                    "expected_temporal": args.expected_temporal,
                    "cost_envelope_usd": cost_envelope["total_usd"],
                },
                sort_keys=True,
            ),
            flush=True,
        )

        for ordinal, case in enumerate(queries, start=1):
            query_id = str(case["id"])
            print(f"EXECUTION_START {ordinal}/{EXECUTION_COUNT} {query_id}", flush=True)
            raw = _run_sse(
                api=args.api,
                token=token,
                question=str(case["question"]),
                timeout=args.request_timeout,
            )
            row = _build_execution(
                case=case,
                ordinal=ordinal,
                process_run_id=process_run_id,
                raw=raw,
                prompt_receipt=prompt_receipt,
                document_names=document_names,
                selected_filenames=selected_filenames,
            )
            state["executions"].append(row)
            _atomic_write(args.output, state)
            print(
                "EXECUTION_DONE "
                + json.dumps(
                    {
                        "query_id": query_id,
                        "state": row["classification"]["state"],
                        "technical_ok": row["technical"]["ok"],
                        "journal_complete": row["journal_complete"],
                        "model": row["model_route"].get("model"),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

        final_prompt_receipt, _ = _prompt_template_receipt(prompt_rendered_at)
        prompt_context_stable = final_prompt_receipt == prompt_receipt
        if not prompt_context_stable:
            for row in state["executions"]:
                row["technical"]["ok"] = False
                row["technical"]["status"] = "failed"
                row["technical"]["errors"].append(
                    "system-prompt rendered hash or source SHA changed during batch"
                )
                row["journal_complete"] = False
                row["journal_completeness_errors"].append(
                    "invalid:system_prompt_template.stability"
                )

        counts = Counter(row["classification"]["state"] for row in state["executions"])
        refused_count = counts["gate_blocked"] + counts["model_voiced_refusal"]
        technical_ok = all(
            row["technical"]["ok"] is True for row in state["executions"]
        )
        journal_complete = all(
            row["journal_complete"] is True for row in state["executions"]
        )
        state["completed_at_utc"] = _utc_now()
        state["summary"] = {
            "execution_count": len(state["executions"]),
            "state_counts": dict(sorted(counts.items())),
            "refused_count": refused_count,
            "refusal_rate": round(refused_count / EXECUTION_COUNT, 6),
            "answered_count": counts["answered"],
            "technical_success_count": sum(
                row["technical"]["ok"] is True for row in state["executions"]
            ),
            "journal_complete_count": sum(
                row["journal_complete"] is True for row in state["executions"]
            ),
            "technical_success": technical_ok,
            "journal_complete": journal_complete,
            "prompt_render_context_stable": prompt_context_stable,
            "refusal_rate_is_measurement_only": True,
        }
        complete = (
            len(state["executions"]) == EXECUTION_COUNT
            and technical_ok
            and journal_complete
            and prompt_context_stable
        )
        state["sealed"] = complete
        state["seal"] = _seal_journal(state) if complete else None
        _atomic_write(args.output, state)
        print(
            "CANONICAL_REFUSAL_SUMMARY " + json.dumps(state["summary"], sort_keys=True),
            flush=True,
        )
        return 0 if complete else 2


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return run(args)
    except (RuntimeError, OSError, ValueError, urllib.error.URLError) as exc:
        print(f"CANONICAL_REFUSAL_ABORT={type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
