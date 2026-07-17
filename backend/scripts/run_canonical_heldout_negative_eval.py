#!/usr/bin/env python3
"""Run the immutable 28-probe held-out refusal measurement.

This harness deliberately separates transport health from refusal measurement.
It exits non-zero only when the run is technically incomplete or the durable
journal is incomplete.  The observed refusal rate is never an exit gate.

Prompt-template hash method (``polymath.chat_system_prompt_render.v1``):
the runner imports the production ``_build_polymath_system_prompt`` function,
renders it once with an explicit timezone-aware wall-clock value immediately
before the batch, and hashes the exact UTF-8 result.  The production builder's
only dynamic fields are local date and timezone name.  The runner verifies
those fields did not change before sealing, so the recorded hash is the exact
prompt produced by the same baked code for every request date in this batch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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


BACKEND = Path(__file__).resolve().parents[1]
SPEC_PATH = BACKEND / "evals/e2e_heldout_negative_v2_20260717.json"
SELECTION_PATH = BACKEND / "evals/runpod_e2e_15doc_selection_v1.json"
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
CLASSIFIER_VERSION = "canonical_refusal_three_state.v1"
PROMPT_HASH_METHOD = "polymath.chat_system_prompt_render.v1"

MINIMAX_INPUT_USD_PER_MILLION = 0.30
MINIMAX_OUTPUT_USD_PER_MILLION = 1.20
MODEL_COMPLETION_TOKEN_BOUND = 16_384
MEASURED_SYSTEM_PROMPT_TOKENS = 2_338
MEASURED_MAX_EVIDENCE_CHARS = 13_293
PROMPT_WRAPPER_TOKEN_ALLOWANCE = 4_096

REFUSAL_CUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "speaker_cannot_answer",
        re.compile(
            r"\b(?:i|we)\s+(?:can(?:not|'t)|could(?:\s+not|n't)|am\s+unable"
            r"|are\s+unable)\s+(?:reliably\s+|fully\s+)?"
            r"(?:answer|provide|confirm|determine|identify|say|verify|infer)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "speaker_did_not_find",
        re.compile(
            r"\b(?:i|we)\s+(?:did\s+not|didn't|could\s+not|couldn't)\s+"
            r"(?:find|locate|verify|identify)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "corpus_absence",
        re.compile(
            r"\b(?:the\s+)?(?:selected|provided|retrieved|available)?\s*"
            r"(?:corpus|context|sources?|documents?|evidence|material|passages?)\s+"
            r"(?:do(?:es)?\s+not|doesn't|don't|cannot|can't|fail(?:s)?\s+to)\s+"
            r"(?:directly\s+)?(?:address|answer|contain|cover|describe|detail|"
            r"establish|include|mention|name|provide|recommend|specify|state|"
            r"support|verify)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "no_evidence",
        re.compile(
            r"\b(?:there\s+is\s+)?(?:no|not\s+enough|insufficient|inadequate)\s+"
            r"(?:source[- ]backed\s+)?(?:evidence|information|material|support|"
            r"context|detail|mention)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "speaker_lacks_information",
        re.compile(
            r"\b(?:i|we)\s+(?:do\s+not|don't)\s+have\s+(?:enough\s+)?"
            r"(?:source[- ]backed\s+)?(?:evidence|information|context|support)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "no_source_mentions",
        re.compile(
            r"\bno\s+(?:selected\s+|provided\s+|retrieved\s+|available\s+)?"
            r"(?:source|document|passage)\s+(?:addresses|answers|contains|covers|"
            r"describes|establishes|includes|mentions|names|provides|specifies|"
            r"states|supports)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "corpus_provides_no_information",
        re.compile(
            r"\b(?:the\s+)?(?:selected|provided|retrieved|available)?\s*"
            r"(?:corpus|context|sources?|documents?|evidence|material|passages?)\s+"
            r"(?:provides?|contains?|has)\s+no\s+(?:evidence|information|detail|"
            r"mention|support)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "not_in_scope",
        re.compile(
            r"\b(?:this|that|the\s+(?:answer|information|detail|claim|topic))\s+"
            r"(?:is|was)\s+not\s+(?:available|covered|established|included|"
            r"mentioned|present|provided|specified|stated|supported)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "outside_sources",
        re.compile(
            r"\b(?:outside|beyond)\s+(?:the\s+)?(?:selected|provided|retrieved)?\s*"
            r"(?:corpus|context|sources?|documents?|evidence|material)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "absent_from_corpus",
        re.compile(
            r"\b(?:(?:this|that|the)\s+"
            r"(?:answer|information|detail|claim|topic))\s+"
            r"(?:is|was)\s+not\s+in\s+(?:the\s+)?"
            r"(?:selected|provided|retrieved|available)?\s*"
            r"(?:corpus|context|sources?|documents?|evidence|material)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "sources_silent",
        re.compile(
            r"\b(?:the\s+)?(?:selected|provided|retrieved|available)?\s*"
            r"(?:corpus|sources?|documents?|evidence|material)\s+"
            r"(?:is|are|remain(?:s)?)\s+silent\s+(?:about|on|regarding)\b",
            re.IGNORECASE,
        ),
    ),
)

NON_SUBSTANTIVE_CLAUSES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:please\s+)?(?:provide|share|add|select|upload)\b.*"
        r"(?:source|document|context|material|evidence).*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:if|once|when)\b.*(?:provide|share|add|select|upload)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:then\s+)?(?:i|we)\s+(?:can|could|would)\s+"
        r"(?:answer|help|review|summarize|check|analyze)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:answering|confirming|determining)\b.*\b(?:would\s+)?require(?:s)?\b"
        r".*(?:outside|additional|other|new)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:without|based\s+only\s+on)\b.*(?:source|corpus|context|evidence).*$",
        re.IGNORECASE,
    ),
)

CLAUSE_SPLIT_RE = re.compile(
    r"(?:[.!?]\s+|\n+|;\s*|,\s*(?:but|however|although|yet|while)\s+|"
    r"\s+(?:but|however|although|yet|nevertheless)\s+|,\s+and\s+|\s+and\s+)",
    re.IGNORECASE,
)

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
    "transport.errors",
    "technical.status",
    "technical.ok",
    "answerability.raw_answerable",
    "answerability.telemetry",
    "answerability.guard.eligible",
    "answerability.guard.coverage",
    "model_skipped",
    "model_route.model",
    "system_prompt_template.method_version",
    "system_prompt_template.sha256",
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


def _clean_clause(value: str) -> str:
    value = re.sub(r"`{1,3}", "", value)
    value = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", value)
    value = re.sub(r"^[\s>*#\-–—\d.)]+", "", value)
    return re.sub(r"\s+", " ", value).strip(" \t\r\n,.:")


def _refusal_cue_ids(value: str) -> list[str]:
    return [cue_id for cue_id, pattern in REFUSAL_CUES if pattern.search(value)]


def _is_non_substantive_clause(value: str) -> bool:
    if not value:
        return True
    return any(pattern.search(value) for pattern in NON_SUBSTANTIVE_CLAUSES)


def classify_refusal(answer: str, *, model_skipped: bool) -> dict[str, Any]:
    """Classify one answer under the frozen deterministic three-state rule."""

    normalized = re.sub(r"\s+", " ", str(answer or "")).strip()
    cue_ids = _refusal_cue_ids(normalized)
    if model_skipped:
        state = "gate_blocked"
        substantive: list[str] = []
    else:
        clauses = [
            cleaned
            for cleaned in (
                _clean_clause(part) for part in CLAUSE_SPLIT_RE.split(answer)
            )
            if cleaned
        ]
        substantive = [
            clause
            for clause in clauses
            if not _refusal_cue_ids(clause)
            and not _is_non_substantive_clause(clause)
            and len(re.findall(r"[A-Za-z0-9]+", clause)) >= 2
        ]
        state = (
            "model_voiced_refusal"
            if normalized and cue_ids and not substantive
            else "answered"
        )
    return {
        "version": CLASSIFIER_VERSION,
        "state": state,
        "refused": state in {"gate_blocked", "model_voiced_refusal"},
        "refusal_cue_ids": cue_ids,
        "substantive_clause_count": len(substantive),
        "substantive_clause_excerpts": [value[:160] for value in substantive[:4]],
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
    if not str(((row.get("model_route") or {}).get("model")) or ""):
        errors.append("empty:model_route.model")
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


def _extract_model_route(traces: list[dict[str, Any]]) -> dict[str, Any]:
    for trace in traces:
        if trace.get("title") == "Chat model route":
            metadata = trace.get("metadata") or {}
            return {
                "model": metadata.get("model"),
                "web_planner_split": metadata.get("web_planner_split"),
            }
    return {}


def _model_skipped(traces: list[dict[str, Any]]) -> bool:
    return any(
        trace.get("title") == "Assistant final answer"
        and (trace.get("metadata") or {}).get("model_skipped") is True
        for trace in traces
    )


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
    done: dict[str, Any] = {}
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
                    done = event
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
        "done": done,
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


def _prompt_template_receipt() -> tuple[dict[str, Any], datetime]:
    from services.chat_orchestrator import _build_polymath_system_prompt

    rendered_at = datetime.now().astimezone()
    rendered = _build_polymath_system_prompt(rendered_at)
    return (
        {
            "method_version": PROMPT_HASH_METHOD,
            "sha256": _sha256_bytes(rendered.encode("utf-8")),
            "rendered_for_local_date": rendered_at.strftime("%Y-%m-%d"),
            "rendered_for_timezone_name": rendered_at.tzname() or "local time",
            "utf8_bytes": len(rendered.encode("utf-8")),
            "builder": "services.chat_orchestrator._build_polymath_system_prompt",
        },
        rendered_at,
    )


def _runtime_flags(expected_temporal: bool) -> dict[str, bool]:
    settings = get_settings()
    observed = {
        "relationship_evidence_allocation_enabled": bool(
            settings.RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED
        ),
        "answerability_corpus_scope_v2_enabled": bool(
            settings.ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED
        ),
        "temporal_query_routing_enabled": bool(settings.TEMPORAL_QUERY_ROUTING_ENABLED),
        "two_lane_anchoring_enabled": bool(settings.TWO_LANE_ANCHORING_ENABLED),
        "four_lane_tier0_router_enabled": bool(settings.FOUR_LANE_TIER0_ROUTER_ENABLED),
        "four_lane_subquery_decomposition_enabled": bool(
            settings.FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED
        ),
        "atomic_claim_anchors_enabled": bool(settings.ATOMIC_CLAIM_ANCHORS_ENABLED),
    }
    expected = {
        "relationship_evidence_allocation_enabled": True,
        "answerability_corpus_scope_v2_enabled": True,
        "temporal_query_routing_enabled": expected_temporal,
        "two_lane_anchoring_enabled": False,
        "four_lane_tier0_router_enabled": False,
        "four_lane_subquery_decomposition_enabled": False,
        "atomic_claim_anchors_enabled": False,
    }
    if observed != expected:
        raise RuntimeError(
            "runtime flags do not match canonical harness contract: "
            + json.dumps({"expected": expected, "observed": observed}, sort_keys=True)
        )
    return observed


@contextmanager
def _eval_lock(owner: str, wait_seconds: int) -> Iterator[None]:
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        try:
            descriptor = os.open(
                LOCK_PATH,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
            )
        except FileExistsError:
            if time.monotonic() >= deadline:
                holder = LOCK_PATH.read_text(encoding="utf-8", errors="replace").strip()
                raise RuntimeError(f"eval lock held by {holder or 'unknown'}")
            time.sleep(60)
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(owner + "\n")
        break
    try:
        yield
    finally:
        try:
            if LOCK_PATH.read_text(encoding="utf-8", errors="replace").strip() == owner:
                LOCK_PATH.unlink()
        except FileNotFoundError:
            pass


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
    model_route = _extract_model_route(raw["traces"])
    model_skipped = _model_skipped(raw["traces"])
    classification = classify_refusal(answer, model_skipped=model_skipped)
    sources = _source_receipt(
        raw["sources"],
        document_names,
        selected_filenames,
    )
    effective_tier = _effective_tier(raw["traces"])
    technical_errors = list(raw["errors"])
    if not raw["done"]:
        technical_errors.append("missing SSE done event")
    if effective_tier != TIER:
        technical_errors.append(f"effective tier mismatch: {effective_tier!r}")
    if not answerability:
        technical_errors.append("missing Answerability gate trace")
    if not model_route.get("model"):
        technical_errors.append("missing Chat model route trace/model")
    if not model_skipped and not answer.strip():
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
            "done_received": bool(raw["done"]),
            "errors": list(raw["errors"]),
            "event_counts": raw["event_counts"],
            "elapsed_seconds": raw["elapsed_seconds"],
            "effective_tier": effective_tier,
        },
        "answerability": answerability,
        "model_skipped": model_skipped,
        "model_route": model_route,
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
    parser.add_argument("--token", default=os.getenv("POLYMATH_EVAL_TOKEN"))
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument(
        "--lock-owner",
        default="codex/canonical-refusal-harness-20260717",
    )
    parser.add_argument("--lock-wait-seconds", type=int, default=0)
    return parser


def run(args: argparse.Namespace) -> int:
    if not args.token:
        raise RuntimeError("--token or POLYMATH_EVAL_TOKEN is required")
    if args.output.exists():
        raise RuntimeError("output already exists; choose a fresh journal path")

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

    with _eval_lock(args.lock_owner, args.lock_wait_seconds):
        preflight = _embedder_preflight(args.api)
        document_names, corpus_receipt = _verify_corpus(
            args.api,
            args.token,
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
                token=args.token,
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

        current_prompt_time = datetime.now().astimezone()
        prompt_context_stable = current_prompt_time.strftime(
            "%Y-%m-%d"
        ) == prompt_rendered_at.strftime("%Y-%m-%d") and (
            current_prompt_time.tzname() or "local time"
        ) == (
            prompt_rendered_at.tzname() or "local time"
        )
        if not prompt_context_stable:
            for row in state["executions"]:
                row["technical"]["ok"] = False
                row["technical"]["status"] = "failed"
                row["technical"]["errors"].append(
                    "system-prompt date/timezone changed during batch"
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
