#!/usr/bin/env python3
"""Deterministic live acceptance harness for the host Metal GPU arbiter.

This runner is intentionally split into two sealed phases:

* ``capture-off`` installs no software and mutates no flags. It requires the
  caller to have deployed the review commit with ``ARBITER_ENABLED=false`` and
  captures the immutable model outputs plus one frozen retrieval spot.
* ``run-on`` requires that sealed OFF artifact and a caller-deployed
  ``ARBITER_ENABLED=true`` runtime. It executes Q1-Q5 and exits non-zero when
  any registered gate is red.

The CLI always runs the Q4 mixed soak for 600 seconds. The orchestration
function accepts a duration only so unit tests can exercise it quickly; there
is no production CLI duration override.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.request

SCHEMA_VERSION = "polymath.gpu_arbiter_live_gates.v3"
FIXTURE_VERSION = "gpu-arbiter-q1-q4-fixture.v1"
REFUSAL_CLASSIFIER_VERSION = "gpu-arbiter-q5-refusal.v2"
PERCENTILE_METHOD_VERSION = "nearest_rank.v1"
FROZEN_PREREG_SHA256 = (
    "8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110"
)
FROZEN_QUERY_ID = "direct_facs"
FROZEN_TIER = "qdrant_only"
PRODUCTION_SOAK_SECONDS = 600.0
PRODUCTION_KILL_AT_SECONDS = 300.0
LOCAL_ONLY_POLICY = "owner_compact_window_2026-07-18T02:34Z"
EMBED_SAMPLE_COUNT = 100
EMBED_DIMENSION = 1024
RERANK_SAMPLE_COUNT = 24
SOLO_RERANK_CALLS = 20
Q2_EMBED_CALLS = 100
Q2_EMBED_CONCURRENCY = 3
Q2_MIN_MIXED_RERANK_CALLS = 20
Q2_MIN_OVERLAPPED_RERANK_CALLS = 5
Q1_TOLERANCE = 1e-12
Q2_EMBED_P95_SECONDS = 2.0
Q3_RERANK_RATIO_MAX = 2.0
Q3_RERANK_HOLD_P95_MS_MAX = 500.0
FAIL_OPEN_ALERT = "gpu_arbiter_unavailable"
FAIL_OPEN_ALERTS = {
    "embed": f"{FAIL_OPEN_ALERT} workload=embed operation=acquire",
    "rerank": f"{FAIL_OPEN_ALERT} workload=rerank operation=acquire",
}
EXPECTED_ARBITER_ARGV_SUFFIX = (
    "-m",
    "uvicorn",
    "gpu_arbiter.main:app",
    "--host",
    "127.0.0.1",
    "--port",
    "8085",
    "--log-level",
    "info",
)
FROZEN_CORPUS_ID = "2c894530-8d57-4432-a6d4-bc14505a698b"
FROZEN_CORPUS_NAME = "runpod_e2e_15doc_20260715"
FROZEN_SELECTION_SHA256 = (
    "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00"
)
CANONICAL_SUITE_TIMEOUT_SECONDS = 600.0
CANONICAL_SUITE_TESTS = (
    "tests/test_gpu_priority_arbiter.py",
    "tests/test_embedder_priority_gate.py",
    "tests/test_embedder_warmup.py",
    (
        "tests/test_answerability_gate_loosening.py::"
        "test_rerank_evidence_support_defaults_off_and_flips_on"
    ),
    "tests/test_gpu_arbiter_live_harness.py",
    "tests/test_gpu_arbiter_promotion_safety.py",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PREREG = REPO_ROOT / "backend/evals/runpod_e2e_retrieval_preregister_v1.json"
DEFAULT_SELECTION = REPO_ROOT / "backend/evals/runpod_e2e_15doc_selection_v1.json"
DEFAULT_PID_FILE = Path.home() / "PolymathRuntime/logs/gpu-arbiter.pid"
DEFAULT_ERROR_LOG = Path.home() / "PolymathRuntime/logs/apple_ml_services.err.log"

REFUSAL_PATTERNS = (
    re.compile(r"\bcannot answer\b", re.IGNORECASE),
    re.compile(r"\bcan(?:not|'t) find\b", re.IGNORECASE),
    re.compile(r"\bdid not find\b", re.IGNORECASE),
    re.compile(r"\bnot supported by (?:the )?(?:selected )?corpus\b", re.IGNORECASE),
    re.compile(r"\bnot (?:in|within) (?:the )?(?:selected )?corpus\b", re.IGNORECASE),
    re.compile(r"\binsufficient (?:source )?evidence\b", re.IGNORECASE),
    re.compile(r"\bno relevant (?:source|evidence)\b", re.IGNORECASE),
)


class HarnessError(RuntimeError):
    """A deterministic precondition or live-contract failure."""


@dataclass(frozen=True)
class HarnessConfig:
    embedder_url: str
    reranker_url: str
    arbiter_url: str
    backend_url: str
    corpus_id: str
    auth_token: str = field(repr=False)
    prereg_path: Path
    selection_path: Path
    pid_file: Path
    error_log: Path
    http_timeout_seconds: float = 120.0
    recovery_timeout_seconds: float = 180.0
    alert_timeout_seconds: float = 15.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _seal(payload: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(payload)
    sealed.pop("seal_sha256", None)
    sealed["seal_sha256"] = _sha256(_canonical_bytes(sealed))
    return sealed


def _verify_seal(payload: dict[str, Any]) -> None:
    expected = str(payload.get("seal_sha256") or "")
    unsealed = dict(payload)
    unsealed.pop("seal_sha256", None)
    actual = _sha256(_canonical_bytes(unsealed))
    if not expected or actual != expected:
        raise HarnessError(
            f"artifact seal mismatch: expected={expected!r} actual={actual!r}"
        )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
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


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile used consistently across Q2-Q4."""
    if not values:
        raise HarnessError("percentile requires at least one value")
    if not 0.0 < fraction <= 1.0:
        raise HarnessError(f"invalid percentile fraction: {fraction}")
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def build_fixture() -> dict[str, Any]:
    """Build the immutable, corpus-independent Q1-Q4 request fixture."""
    embed_texts = [
        (
            "Polymath Metal scheduling identity probe "
            f"{index:03d}; deterministic token arbiter-vector-{index:03d}."
        )
        for index in range(EMBED_SAMPLE_COUNT)
    ]
    rerank_query = (
        "How should evidence routing preserve relevance while interactive "
        "embedding and cross-encoder reranking share one accelerator?"
    )
    rerank_documents = [
        (
            f"Candidate {index:02d}. "
            "A deterministic retrieval system keeps evidence provenance, "
            "model identity, and scheduling policy independently auditable. "
            f"Stable fixture token rerank-candidate-{index:02d}."
        )
        for index in range(RERANK_SAMPLE_COUNT)
    ]
    fixture = {
        "fixture_version": FIXTURE_VERSION,
        "embed_texts": embed_texts,
        "rerank_query": rerank_query,
        "rerank_documents": rerank_documents,
    }
    fixture["fixture_sha256"] = _sha256(_canonical_bytes(fixture))
    return fixture


def verify_fixture(fixture: dict[str, Any]) -> None:
    expected = str(fixture.get("fixture_sha256") or "")
    unhashed = dict(fixture)
    unhashed.pop("fixture_sha256", None)
    actual = _sha256(_canonical_bytes(unhashed))
    if actual != expected:
        raise HarnessError(
            f"fixture hash mismatch: expected={expected!r} actual={actual!r}"
        )
    canonical = build_fixture()
    if fixture != canonical:
        raise HarnessError("fixture differs from the registered deterministic fixture")


def _request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> Any:
    body = None if payload is None else _canonical_bytes(payload)
    request_headers = dict(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        if response.status < 200 or response.status >= 300:
            raise HarnessError(f"HTTP {response.status} from {url}")
    return json.loads(raw.decode("utf-8")) if raw else {}


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _model_skipped(traces: list[dict[str, Any]]) -> bool:
    return any(
        trace.get("title") == "Assistant final answer"
        and (trace.get("metadata") or {}).get("model_skipped") is True
        for trace in traces
    )


def _effective_tier(traces: list[dict[str, Any]]) -> str:
    for trace in reversed(traces):
        if trace.get("title") == "Local RAG retrieval":
            value = str((trace.get("metadata") or {}).get("effective_tier") or "")
            if value:
                return value
    return ""


def classify_answer(answer: str, traces: list[dict[str, Any]]) -> str:
    """Versioned three-state classifier for the positive frozen spot."""
    if _model_skipped(traces):
        return "gate_blocked"
    normalized = re.sub(r"\s+", " ", answer).strip()
    if not normalized:
        return "empty_answer"
    if any(pattern.search(normalized) for pattern in REFUSAL_PATTERNS):
        return "model_voiced_refusal"
    return "answered"


def validate_embedding_matrix(vectors: Any) -> list[list[float]]:
    if not isinstance(vectors, list) or len(vectors) != EMBED_SAMPLE_COUNT:
        raise HarnessError(
            f"expected {EMBED_SAMPLE_COUNT} embedding vectors, got "
            f"{len(vectors) if isinstance(vectors, list) else type(vectors).__name__}"
        )
    validated: list[list[float]] = []
    for index, vector in enumerate(vectors):
        if not isinstance(vector, list) or len(vector) != EMBED_DIMENSION:
            raise HarnessError(
                f"embedding {index} dimension mismatch: "
                f"{len(vector) if isinstance(vector, list) else type(vector).__name__}"
            )
        row = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in row):
            raise HarnessError(f"embedding {index} contains non-finite values")
        validated.append(row)
    return validated


def validate_rerank_scores(scores: Any) -> list[float]:
    if not isinstance(scores, list) or len(scores) != RERANK_SAMPLE_COUNT:
        raise HarnessError(
            f"expected {RERANK_SAMPLE_COUNT} rerank scores, got "
            f"{len(scores) if isinstance(scores, list) else type(scores).__name__}"
        )
    validated = [float(value) for value in scores]
    if not all(math.isfinite(value) for value in validated):
        raise HarnessError("rerank sample contains non-finite scores")
    return validated


def max_abs_matrix_difference(
    left: list[list[float]], right: list[list[float]]
) -> float:
    if len(left) != len(right):
        raise HarnessError("embedding row count drifted")
    maximum = 0.0
    for row_index, (left_row, right_row) in enumerate(zip(left, right)):
        if len(left_row) != len(right_row):
            raise HarnessError(f"embedding dimension drifted at row {row_index}")
        for left_value, right_value in zip(left_row, right_row):
            maximum = max(maximum, abs(float(left_value) - float(right_value)))
    return maximum


def max_abs_vector_difference(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise HarnessError("score count drifted")
    return max(
        (abs(float(left_value) - float(right_value)))
        for left_value, right_value in zip(left, right)
    )


class LiveClient:
    """Host endpoint client. Tests replace this class with a pure fake."""

    def __init__(self, config: HarnessConfig) -> None:
        self.config = config

    def json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        return _request_json(
            method,
            url,
            payload=payload,
            headers=headers,
            timeout=timeout or self.config.http_timeout_seconds,
        )

    def health_snapshot(self, *, expected_enabled: bool) -> dict[str, Any]:
        embed = self.json("GET", f"{self.config.embedder_url}/health")
        rerank = self.json("GET", f"{self.config.reranker_url}/health")
        embed_enabled = bool((embed.get("gpu_arbiter") or {}).get("enabled"))
        rerank_enabled = bool((rerank.get("gpu_arbiter") or {}).get("enabled"))
        if embed_enabled is not expected_enabled:
            raise HarnessError(
                f"embedder arbiter flag mismatch: {embed_enabled} "
                f"!= {expected_enabled}"
            )
        if rerank_enabled is not expected_enabled:
            raise HarnessError(
                f"reranker arbiter flag mismatch: {rerank_enabled} "
                f"!= {expected_enabled}"
            )
        if embed.get("inference_ready") is not True:
            raise HarnessError("embedder inference_ready is not true")
        if rerank.get("warmup_complete") is not True:
            raise HarnessError("reranker warmup_complete is not true")
        arbiter: dict[str, Any] | None = None
        arbiter_absent = False
        try:
            arbiter = self.json("GET", f"{self.config.arbiter_url}/health", timeout=2.0)
        except Exception:
            arbiter_absent = True
        if expected_enabled:
            if arbiter_absent or (arbiter or {}).get("status") != "ok":
                raise HarnessError("arbiter is not healthy while expected ON")
        elif not arbiter_absent:
            raise HarnessError("arbiter endpoint is reachable while expected OFF")
        return {
            "embedder": _public_health(embed),
            "reranker": _public_health(rerank),
            "arbiter": arbiter,
            "arbiter_absent": arbiter_absent,
        }

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self.json(
            "POST",
            f"{self.config.embedder_url}/embeddings",
            payload={
                "input": texts,
                "model": "Qwen3-Embedding-0.6B",
                "workload_class": "interactive_query",
            },
        )
        rows = response.get("data") or []
        indices = [int(row.get("index", -1)) for row in rows]
        expected_indices = list(range(len(texts)))
        if indices != expected_indices:
            raise HarnessError(
                f"embedding response indices drifted: {indices!r} "
                f"!= {expected_indices!r}"
            )
        ordered = rows
        return [
            [float(value) for value in row.get("embedding") or []] for row in ordered
        ]

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        response = self.json(
            "POST",
            f"{self.config.reranker_url}/rerank",
            payload={"query": query, "documents": documents},
        )
        return [float(value) for value in response.get("scores") or []]

    def list_documents(self) -> dict[str, str]:
        payload = self.json(
            "GET",
            (
                f"{self.config.backend_url}/api/corpora/"
                f"{self.config.corpus_id}/documents?limit=100&offset=0"
            ),
            headers=_auth_headers(self.config.auth_token),
        )
        rows = payload if isinstance(payload, list) else payload.get("documents") or []
        mapping: dict[str, str] = {}
        for row in rows:
            doc_id = str(row.get("doc_id") or row.get("id") or "")
            filename = str(
                row.get("original_filename")
                or row.get("filename")
                or row.get("name")
                or ""
            )
            if doc_id and filename:
                mapping[doc_id] = filename
        if not mapping:
            raise HarnessError("document listing returned no resolvable documents")
        return mapping

    def corpus_binding(self) -> dict[str, Any]:
        if self.config.corpus_id != FROZEN_CORPUS_ID:
            raise HarnessError(
                f"Q5 requires frozen corpus id {FROZEN_CORPUS_ID}, "
                f"got {self.config.corpus_id}"
            )
        selection_bytes = self.config.selection_path.read_bytes()
        selection_sha = _sha256(selection_bytes)
        if selection_sha != FROZEN_SELECTION_SHA256:
            raise HarnessError(
                f"frozen selection drifted: {selection_sha} "
                f"!= {FROZEN_SELECTION_SHA256}"
            )
        selection = json.loads(selection_bytes)
        expected_names = sorted(
            str(row["filename"]) for row in selection.get("selected") or []
        )
        if (
            len(expected_names) != 15
            or int(selection.get("selection_count") or 0) != 15
        ):
            raise HarnessError("frozen selection must contain exactly 15 documents")
        corpus = self.json(
            "GET",
            f"{self.config.backend_url}/api/corpora/{self.config.corpus_id}",
            headers=_auth_headers(self.config.auth_token),
        )
        actual_id = str(corpus.get("corpus_id") or corpus.get("id") or "")
        actual_name = str(corpus.get("name") or "")
        documents = self.list_documents()
        actual_names = sorted(documents.values())
        return {
            "corpus_id": actual_id,
            "corpus_name": actual_name,
            "selection_sha256": selection_sha,
            "selection_count": len(expected_names),
            "expected_filenames": expected_names,
            "actual_document_count": len(actual_names),
            "actual_filenames": actual_names,
            "matches_frozen_corpus": (
                actual_id == FROZEN_CORPUS_ID
                and actual_name == FROZEN_CORPUS_NAME
                and actual_names == expected_names
            ),
        }

    def frozen_spot(self) -> dict[str, Any]:
        corpus_binding = self.corpus_binding()
        if corpus_binding["matches_frozen_corpus"] is not True:
            raise HarnessError(
                "Q5 corpus binding differs from the frozen 15-doc corpus"
            )
        prereg_bytes = self.config.prereg_path.read_bytes()
        prereg_sha = _sha256(prereg_bytes)
        if prereg_sha != FROZEN_PREREG_SHA256:
            raise HarnessError(
                f"frozen preregistration drifted: {prereg_sha} "
                f"!= {FROZEN_PREREG_SHA256}"
            )
        prereg = json.loads(prereg_bytes)
        matches = [
            row
            for row in prereg.get("queries") or []
            if str(row.get("id") or "") == FROZEN_QUERY_ID
        ]
        if len(matches) != 1:
            raise HarnessError(f"expected one frozen query {FROZEN_QUERY_ID}")
        case = matches[0]
        expected = {str(value) for value in case.get("expected_any") or []}
        if not expected:
            raise HarnessError("frozen direct_facs expected document set is empty")
        documents = self.list_documents()
        raw = self._run_chat_sse(
            {
                "message": str(case["question"]),
                "corpus_ids": [self.config.corpus_id],
                "retrieval_tier": FROZEN_TIER,
                "overrides": {
                    "final_top_k": int(prereg["top_k"]),
                    "temperature": 0,
                },
            }
        )
        source_names: list[str] = []
        memberships: list[bool] = []
        for source in raw["sources"]:
            doc_id = str(source.get("doc_id") or "")
            name = str(
                documents.get(doc_id)
                or source.get("filename")
                or source.get("doc_name")
                or source.get("document_name")
                or source.get("title")
                or ""
            )
            if name:
                source_names.append(name)
            memberships.append(
                str(source.get("corpus_id") or "") == self.config.corpus_id
                and doc_id in documents
            )
        source_name_set = sorted(set(source_names))
        expected_hits = sorted(expected.intersection(source_name_set))
        effective_tier = _effective_tier(raw["traces"])
        verdict = classify_answer(raw["answer"], raw["traces"])
        technical_success = (
            not raw["errors"]
            and raw["done_received"]
            and effective_tier == FROZEN_TIER
            and bool(raw["answer"].strip())
        )
        return {
            "query_id": FROZEN_QUERY_ID,
            "tier": FROZEN_TIER,
            "preregistration_sha256": prereg_sha,
            "technical_success": technical_success,
            "effective_tier": effective_tier,
            "done_received": raw["done_received"],
            "errors": raw["errors"],
            "source_count": len(raw["sources"]),
            "source_filenames": source_name_set,
            "expected_filenames": sorted(expected),
            "matched_expected_filenames": expected_hits,
            "doc_hit": bool(expected_hits),
            "citation_membership_rate": (
                sum(1 for value in memberships if value) / len(memberships)
                if memberships
                else 0.0
            ),
            "all_citations_in_corpus": bool(memberships) and all(memberships),
            "verdict": verdict,
            "refusal_classifier_version": REFUSAL_CLASSIFIER_VERSION,
            "answer_sha256": _sha256(raw["answer"].encode("utf-8")),
            "corpus_binding": corpus_binding,
        }

    def _run_chat_sse(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.config.backend_url}/api/chat",
            data=_canonical_bytes(payload),
            headers={
                **_auth_headers(self.config.auth_token),
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        answer: list[str] = []
        sources: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        errors: list[str] = []
        done_received = False
        current_event = ""
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.http_timeout_seconds * 5
            ) as response:
                if response.status != 200:
                    raise HarnessError(f"chat HTTP status {response.status}")
                if "text/event-stream" not in str(
                    response.headers.get("Content-Type") or ""
                ):
                    raise HarnessError("chat response is not text/event-stream")
                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    event_type = str(event.get("type") or current_event)
                    if event_type == "token":
                        answer.append(
                            str(event.get("content") or event.get("token") or "")
                        )
                    elif event_type == "sources":
                        candidate = event.get("sources") or event.get("data") or []
                        sources = candidate if isinstance(candidate, list) else []
                    elif event_type == "trace_event" or event.get("trace_event"):
                        traces.append(dict(event.get("trace_event") or event))
                    elif event_type == "error":
                        errors.append(
                            str(event.get("content") or event.get("error") or "")[:500]
                        )
                    elif event_type == "done":
                        done_received = True
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            errors.append(f"HTTP {exc.code}: {detail}")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}"[:500])
        return {
            "answer": "".join(answer),
            "sources": sources,
            "traces": traces,
            "errors": errors,
            "done_received": done_received,
        }


def _public_health(payload: dict[str, Any]) -> dict[str, Any]:
    """Retain model/runtime telemetry but never arbitrary error content."""
    keys = (
        "status",
        "model",
        "backend",
        "device",
        "dimension",
        "batch_size",
        "inference_ready",
        "warmup_complete",
        "cross_encoder",
        "gpu_arbiter",
    )
    return {key: payload.get(key) for key in keys if key in payload}


def runtime_identity(health: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the immutable compute identity used by the OFF/ON comparison."""
    embedder = health.get("embedder") or {}
    reranker = health.get("reranker") or {}
    identity = {
        "embedder": {
            key: embedder.get(key)
            for key in ("model", "device", "dimension", "batch_size")
        },
        "reranker": {
            key: reranker.get(key)
            for key in ("model", "backend", "device", "cross_encoder")
        },
    }
    missing = [
        f"{service}.{key}"
        for service, values in identity.items()
        for key, value in values.items()
        if value in (None, "")
    ]
    if missing:
        raise HarnessError(f"runtime identity fields are missing: {missing}")
    return identity


def scheduler_snapshot(client: LiveClient) -> dict[str, Any]:
    payload = client.json("GET", f"{client.config.arbiter_url}/health")
    if payload.get("status") != "ok" or not isinstance(payload.get("scheduler"), dict):
        raise HarnessError("arbiter scheduler snapshot is unavailable")
    return payload


def scheduler_delta(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, dict[str, int]]:
    before_scheduler = before.get("scheduler") or {}
    after_scheduler = after.get("scheduler") or {}
    delta: dict[str, dict[str, int]] = {}
    for counter in ("grants", "releases", "wait_sample_count", "hold_sample_count"):
        before_values = before_scheduler.get(counter) or {}
        after_values = after_scheduler.get(counter) or {}
        delta[counter] = {}
        for workload in ("embed", "rerank"):
            value = int(after_values.get(workload, 0)) - int(
                before_values.get(workload, 0)
            )
            if value < 0:
                raise HarnessError(
                    f"arbiter counter regressed: {counter}.{workload}={value}"
                )
            delta[counter][workload] = value
    return delta


def timed_embed(client: LiveClient, text: str) -> tuple[float, list[list[float]]]:
    started = time.perf_counter()
    value = client.embed([text])
    return time.perf_counter() - started, value


def timed_rerank(
    client: LiveClient, query: str, documents: list[str]
) -> tuple[float, list[float]]:
    started = time.perf_counter()
    value = client.rerank(query, documents)
    return time.perf_counter() - started, value


def measure_solo_rerank(
    client: LiveClient, fixture: dict[str, Any], *, calls: int = SOLO_RERANK_CALLS
) -> dict[str, Any]:
    latencies: list[float] = []
    errors: list[str] = []
    for index in range(calls):
        try:
            latency, scores = timed_rerank(
                client, fixture["rerank_query"], fixture["rerank_documents"]
            )
            validate_rerank_scores(scores)
            latencies.append(latency)
        except Exception as exc:
            errors.append(f"call={index} {type(exc).__name__}: {exc}"[:500])
    return _latency_summary(latencies, errors, requested=calls)


def run_q2_mixed(
    client: LiveClient,
    fixture: dict[str, Any],
    *,
    embed_calls: int = Q2_EMBED_CALLS,
    embed_concurrency: int = Q2_EMBED_CONCURRENCY,
    min_rerank_calls: int = Q2_MIN_MIXED_RERANK_CALLS,
) -> dict[str, Any]:
    """Run a fixed rerank sample concurrently with interactive embeddings.

    Twenty reranks is preregistered because it is the smallest sample where a
    nearest-rank p95 represents the nineteenth ordered observation rather than
    degenerating into an arbitrary handful of calls. At least five calls must
    overlap an embedding interval, which prevents a serial false pass.
    """
    if min_rerank_calls < Q2_MIN_MIXED_RERANK_CALLS:
        raise HarnessError(
            f"mixed rerank sample may not be below {Q2_MIN_MIXED_RERANK_CALLS}"
        )
    launch_barrier = threading.Barrier(2)
    workloads_released = threading.Event()
    embeddings_done = threading.Event()
    rerank_ready = threading.Event()
    lock = threading.Lock()
    rerank_latencies: list[float] = []
    rerank_errors: list[str] = []
    rerank_intervals: list[dict[str, float]] = []
    embed_intervals: list[dict[str, float]] = []

    def rerank_worker() -> None:
        rerank_ready.set()
        try:
            launch_barrier.wait(timeout=5.0)
        except threading.BrokenBarrierError:
            with lock:
                rerank_errors.append("mixed workload launch barrier broke")
            return
        while True:
            with lock:
                completed = len(rerank_latencies) + len(rerank_errors)
            if embeddings_done.is_set() and completed >= min_rerank_calls:
                break
            started = time.perf_counter()
            try:
                latency, scores = timed_rerank(
                    client, fixture["rerank_query"], fixture["rerank_documents"]
                )
                validate_rerank_scores(scores)
                finished = time.perf_counter()
                with lock:
                    rerank_latencies.append(latency)
                    rerank_intervals.append({"start": started, "end": finished})
            except Exception as exc:
                with lock:
                    rerank_errors.append(f"{type(exc).__name__}: {exc}"[:500])

    rerank_thread = threading.Thread(
        target=rerank_worker, name="q2-continuous-rerank", daemon=True
    )
    rerank_thread.start()
    if not rerank_ready.wait(timeout=5.0):
        raise HarnessError("continuous rerank worker did not start")

    embed_latencies: list[float] = []
    embed_errors: list[str] = []

    def one_embed(index: int) -> None:
        if index == 0:
            try:
                launch_barrier.wait(timeout=5.0)
                workloads_released.set()
            except threading.BrokenBarrierError:
                with lock:
                    embed_errors.append("mixed workload launch barrier broke")
                return
        elif not workloads_released.wait(timeout=5.0):
            with lock:
                embed_errors.append(
                    f"call={index} mixed workload launch was not released"
                )
            return
        started = time.perf_counter()
        try:
            latency, vectors = timed_embed(
                client,
                (
                    f"Q2 interactive embed {index:03d}; "
                    f"deterministic token q2-{index:03d}."
                ),
            )
            if len(vectors) != 1 or len(vectors[0]) != EMBED_DIMENSION:
                raise HarnessError("Q2 embedding shape is invalid")
            if not all(math.isfinite(float(value)) for value in vectors[0]):
                raise HarnessError("Q2 embedding contains non-finite values")
            finished = time.perf_counter()
            with lock:
                embed_latencies.append(latency)
                embed_intervals.append({"start": started, "end": finished})
        except Exception as exc:
            with lock:
                embed_errors.append(f"call={index} {type(exc).__name__}: {exc}"[:500])

    try:
        with ThreadPoolExecutor(max_workers=embed_concurrency) as executor:
            list(executor.map(one_embed, range(embed_calls)))
    finally:
        embeddings_done.set()
        rerank_thread.join(timeout=client.config.http_timeout_seconds + 5.0)
    if rerank_thread.is_alive():
        rerank_errors.append("continuous rerank worker did not stop before deadline")
    overlapped_reranks = sum(
        1
        for rerank in rerank_intervals
        if any(
            rerank["start"] < embed["end"] and embed["start"] < rerank["end"]
            for embed in embed_intervals
        )
    )
    return {
        "embed": _latency_summary(embed_latencies, embed_errors, requested=embed_calls),
        "rerank": _latency_summary(
            rerank_latencies,
            rerank_errors,
            requested=None,
        ),
        "rerank_thread_stopped": not rerank_thread.is_alive(),
        "launch_barrier_passed": workloads_released.is_set(),
        "rerank_minimum_sample": min_rerank_calls,
        "overlapped_rerank_calls": overlapped_reranks,
        "required_overlapped_rerank_calls": Q2_MIN_OVERLAPPED_RERANK_CALLS,
        "embed_intervals": embed_intervals,
        "rerank_intervals": rerank_intervals,
    }


def run_q4_soak(
    client: LiveClient,
    fixture: dict[str, Any],
    *,
    soak_seconds: float = PRODUCTION_SOAK_SECONDS,
    kill_at_seconds: float | None = None,
    failure_probe: Callable[..., dict[str, Any]] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run the failure/recovery probe at a fixed point inside the mixed soak."""
    if soak_seconds <= 0:
        raise HarnessError("soak duration must be positive")
    if kill_at_seconds is None:
        kill_at_seconds = (
            PRODUCTION_KILL_AT_SECONDS
            if soak_seconds == PRODUCTION_SOAK_SECONDS
            else soak_seconds / 2.0
        )
    if not 0 < kill_at_seconds < soak_seconds:
        raise HarnessError("Q4 kill point must be strictly inside the soak")
    probe = failure_probe or run_fail_open_probe
    started_at = monotonic()
    deadline = started_at + soak_seconds
    kill_deadline = started_at + kill_at_seconds
    lock = threading.Lock()
    results: dict[str, list[Any]] = {
        "embed_latencies": [],
        "embed_errors": [],
        "rerank_latencies": [],
        "rerank_errors": [],
        "embed_success_times": [],
        "rerank_success_times": [],
    }

    def embed_worker(worker_index: int) -> None:
        call_index = 0
        while monotonic() < deadline:
            try:
                latency, vectors = timed_embed(
                    client,
                    (
                        f"Q4 soak embed worker={worker_index} call={call_index}; "
                        f"token q4-e-{worker_index}-{call_index}."
                    ),
                )
                if len(vectors) != 1 or len(vectors[0]) != EMBED_DIMENSION:
                    raise HarnessError("Q4 embedding shape is invalid")
                with lock:
                    results["embed_latencies"].append(latency)
                    results["embed_success_times"].append(monotonic())
            except Exception as exc:
                with lock:
                    results["embed_errors"].append(
                        f"worker={worker_index} call={call_index} "
                        f"{type(exc).__name__}: {exc}"[:500]
                    )
            call_index += 1

    def rerank_worker() -> None:
        call_index = 0
        while monotonic() < deadline:
            try:
                latency, scores = timed_rerank(
                    client, fixture["rerank_query"], fixture["rerank_documents"]
                )
                validate_rerank_scores(scores)
                with lock:
                    results["rerank_latencies"].append(latency)
                    results["rerank_success_times"].append(monotonic())
            except Exception as exc:
                with lock:
                    results["rerank_errors"].append(
                        f"call={call_index} {type(exc).__name__}: {exc}"[:500]
                    )
            call_index += 1

    threads = [threading.Thread(target=rerank_worker, name="q4-rerank", daemon=True)]
    threads.extend(
        threading.Thread(
            target=embed_worker,
            args=(index,),
            name=f"q4-embed-{index}",
            daemon=True,
        )
        for index in range(Q2_EMBED_CONCURRENCY)
    )
    for thread in threads:
        thread.start()
    while monotonic() < kill_deadline:
        sleep_fn(min(0.25, max(0.0, kill_deadline - monotonic())))
    workers_active_at_kill = [thread.name for thread in threads if thread.is_alive()]
    failure_started_at = monotonic()
    fail_open = probe(client, fixture)
    failure_completed_at = monotonic()
    join_timeout = soak_seconds + client.config.http_timeout_seconds + 10.0
    join_deadline = time.monotonic() + join_timeout
    for thread in threads:
        thread.join(timeout=max(0.0, join_deadline - time.monotonic()))
    alive = [thread.name for thread in threads if thread.is_alive()]
    embed_after_recovery = sum(
        value >= failure_completed_at for value in results["embed_success_times"]
    )
    rerank_after_recovery = sum(
        value >= failure_completed_at for value in results["rerank_success_times"]
    )
    return {
        "requested_soak_seconds": soak_seconds,
        "kill_at_seconds": kill_at_seconds,
        "failure_started_elapsed_seconds": failure_started_at - started_at,
        "failure_completed_elapsed_seconds": failure_completed_at - started_at,
        "workers_active_at_kill": workers_active_at_kill,
        "fail_open": fail_open,
        "embed": _latency_summary(
            results["embed_latencies"],
            results["embed_errors"],
            requested=None,
        ),
        "rerank": _latency_summary(
            results["rerank_latencies"],
            results["rerank_errors"],
            requested=None,
        ),
        "threads_alive_after_deadline": alive,
        "zero_deadlock": not alive,
        "embed_successes_after_recovery": embed_after_recovery,
        "rerank_successes_after_recovery": rerank_after_recovery,
    }


def _latency_summary(
    latencies: list[float],
    errors: list[str],
    *,
    requested: int | None,
) -> dict[str, Any]:
    return {
        "requested": requested,
        "successful": len(latencies),
        "failed": len(errors),
        "latency_seconds": {
            "min": min(latencies) if latencies else None,
            "p50": percentile(latencies, 0.50) if latencies else None,
            "p95": percentile(latencies, 0.95) if latencies else None,
            "max": max(latencies) if latencies else None,
        },
        "percentile_method": PERCENTILE_METHOD_VERSION,
        "percentile_sample_count": len(latencies),
        "errors": errors,
    }


def _read_new_log_bytes(path: Path, offset: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read().decode("utf-8", "replace")


def _count_fail_open_alerts(value: str) -> int:
    return value.count(FAIL_OPEN_ALERT)


def run_fail_open_probe(
    client: LiveClient,
    fixture: dict[str, Any],
    *,
    kill_fn: Callable[[int, int], None] = os.kill,
    process_identity: Callable[[int], dict[str, Any] | None] | None = None,
    arbiter_down: Callable[[], bool] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Kill only the registered arbiter PID and prove named fail-open + recovery."""
    pid_text = client.config.pid_file.read_text(encoding="utf-8").strip()
    if not pid_text.isdigit() or int(pid_text) <= 1:
        raise HarnessError(f"invalid arbiter pid file: {pid_text!r}")
    pid = int(pid_text)
    identity_reader = process_identity or _process_identity
    old_identity = identity_reader(pid)
    if not _is_exact_arbiter_identity(old_identity, pid):
        raise HarnessError(
            f"refusing to kill pid {pid}; process identity is not the exact "
            f"arbiter contract: {old_identity!r}"
        )
    log_offset = (
        client.config.error_log.stat().st_size
        if client.config.error_log.exists()
        else 0
    )
    kill_fn(pid, signal.SIGKILL)

    def default_arbiter_down() -> bool:
        try:
            client.json("GET", f"{client.config.arbiter_url}/health", timeout=0.25)
        except Exception:
            return True
        return False

    down_reader = arbiter_down or default_arbiter_down
    down_deadline = monotonic() + client.config.alert_timeout_seconds
    old_process_gone = False
    arbiter_endpoint_down = False
    while monotonic() < down_deadline:
        try:
            old_process_gone = identity_reader(pid) is None
        except Exception:
            old_process_gone = True
        arbiter_endpoint_down = bool(down_reader())
        if old_process_gone and arbiter_endpoint_down:
            break
        sleep_fn(0.1)
    if not old_process_gone or not arbiter_endpoint_down:
        raise HarnessError(
            "arbiter down-state was not proven before fail-open workload probes"
        )

    probe_errors: list[str] = []
    probe_shapes: dict[str, Any] = {}

    def embed_probe() -> None:
        try:
            vectors = client.embed(["Q4 fail-open embed probe."])
            probe_shapes["embed"] = [
                len(vectors),
                len(vectors[0]) if vectors else 0,
            ]
            if len(vectors) != 1 or len(vectors[0]) != EMBED_DIMENSION:
                raise HarnessError("fail-open embed shape is invalid")
        except Exception as exc:
            probe_errors.append(f"embed {type(exc).__name__}: {exc}"[:500])

    def rerank_probe() -> None:
        try:
            scores = client.rerank(fixture["rerank_query"], fixture["rerank_documents"])
            probe_shapes["rerank"] = len(scores)
            validate_rerank_scores(scores)
        except Exception as exc:
            probe_errors.append(f"rerank {type(exc).__name__}: {exc}"[:500])

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(embed_probe), executor.submit(rerank_probe)]
        for future in futures:
            future.result()

    alert_deadline = monotonic() + client.config.alert_timeout_seconds
    new_log = ""
    while monotonic() < alert_deadline:
        new_log = _read_new_log_bytes(client.config.error_log, log_offset)
        if all(value in new_log for value in FAIL_OPEN_ALERTS.values()):
            break
        sleep_fn(0.25)
    alerts_seen = {
        workload: expected in new_log for workload, expected in FAIL_OPEN_ALERTS.items()
    }

    recovery_deadline = monotonic() + client.config.recovery_timeout_seconds
    recovery_errors: list[str] = []
    recovered_health: dict[str, Any] | None = None
    replacement_identity: dict[str, Any] | None = None
    replacement_pid: int | None = None
    while monotonic() < recovery_deadline:
        try:
            candidate_health = client.health_snapshot(expected_enabled=True)
            replacement_text = client.config.pid_file.read_text(
                encoding="utf-8"
            ).strip()
            if not replacement_text.isdigit():
                raise HarnessError("replacement pid file is invalid")
            candidate_pid = int(replacement_text)
            candidate_identity = identity_reader(candidate_pid)
            if not _is_exact_arbiter_identity(candidate_identity, candidate_pid):
                raise HarnessError("replacement arbiter identity is invalid")
            if candidate_pid == pid:
                raise HarnessError("replacement arbiter reused the killed pid")
            if candidate_identity.get("start_identity") == old_identity.get(
                "start_identity"
            ):
                raise HarnessError("replacement arbiter start identity did not change")
            recovered_health = candidate_health
            replacement_pid = candidate_pid
            replacement_identity = candidate_identity
            break
        except Exception as exc:
            recovery_errors = [f"{type(exc).__name__}: {exc}"[:500]]
            sleep_fn(2.0)
    return {
        "pid": pid,
        "process_identity": old_identity,
        "old_process_gone_before_probes": old_process_gone,
        "arbiter_endpoint_down_before_probes": arbiter_endpoint_down,
        "log_offset": log_offset,
        "probe_shapes": probe_shapes,
        "probe_errors": probe_errors,
        "required_alerts": dict(FAIL_OPEN_ALERTS),
        "alerts_seen_in_new_log_bytes": alerts_seen,
        "new_log_sha256": _sha256(new_log.encode("utf-8")),
        "recovered": recovered_health is not None,
        "recovery_errors": recovery_errors,
        "recovered_health": recovered_health,
        "replacement_pid": replacement_pid,
        "replacement_identity": replacement_identity,
    }


def _is_exact_arbiter_identity(
    identity: dict[str, Any] | None, expected_pid: int
) -> bool:
    if not identity or int(identity.get("pid") or 0) != expected_pid:
        return False
    if not str(identity.get("start_identity") or "").strip():
        return False
    command = str(identity.get("command") or "")
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    suffix = list(EXPECTED_ARBITER_ARGV_SUFFIX)
    return len(argv) > len(suffix) and argv[-len(suffix) :] == suffix


def _process_identity(pid: int) -> dict[str, Any] | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "pid=", "-o", "lstart=", "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split()
    if len(parts) < 8:
        return None
    try:
        actual_pid = int(parts[0])
    except ValueError:
        return None
    return {
        "pid": actual_pid,
        "start_identity": " ".join(parts[1:6]),
        "command": " ".join(parts[6:]),
    }


def run_canonical_suite(
    *,
    repo_root: Path = REPO_ROOT,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Run the frozen relevant suite in an isolated canonical-image container."""
    command = [
        "docker",
        "run",
        "--rm",
        "-e",
        "LITELLM_MASTER_KEY=test",
        "-e",
        "AUTH_SECRET_KEY=test",
        "-e",
        "DEFAULT_ADMIN_PASSWORD=test",
        "-e",
        "SIDECAR_PATH=/repo/scripts/apple_ml_services",
        "-v",
        f"{repo_root}:/repo",
        "-w",
        "/repo/backend",
        "--entrypoint",
        "python",
        "polymath_v33-backend",
        "-m",
        "pytest",
        "-q",
        *CANONICAL_SUITE_TESTS,
    ]
    started = time.perf_counter()
    result = runner(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=CANONICAL_SUITE_TIMEOUT_SECONDS,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return {
        "command": command,
        "exit_code": int(result.returncode),
        "elapsed_seconds": time.perf_counter() - started,
        "output_tail": output[-8000:],
        "tests": list(CANONICAL_SUITE_TESTS),
        "passed": result.returncode == 0,
    }


def evaluate_off_baseline(
    *,
    health: dict[str, Any],
    vectors: list[list[float]],
    scores: list[float],
    solo: dict[str, Any],
    frozen_spot: dict[str, Any] | None,
) -> dict[str, Any]:
    checks = {
        "runtime_dark": bool(health.get("arbiter_absent")),
        "embedding_sample_valid": (
            len(vectors) == EMBED_SAMPLE_COUNT
            and all(len(row) == EMBED_DIMENSION for row in vectors)
        ),
        "rerank_sample_valid": len(scores) == RERANK_SAMPLE_COUNT,
        "solo_rerank_valid": (
            solo.get("successful") == SOLO_RERANK_CALLS and solo.get("failed") == 0
        ),
    }
    if frozen_spot is not None:
        checks.update(
            {
                "frozen_spot_technical": frozen_spot.get("technical_success") is True,
                "frozen_spot_doc_hit": frozen_spot.get("doc_hit") is True,
                "frozen_spot_citations": (
                    frozen_spot.get("citation_membership_rate") == 1.0
                    and frozen_spot.get("all_citations_in_corpus") is True
                ),
                "frozen_spot_tier": frozen_spot.get("effective_tier") == FROZEN_TIER,
                "frozen_spot_answered": frozen_spot.get("verdict") == "answered",
                "exact_frozen_corpus": (frozen_spot.get("corpus_binding") or {}).get(
                    "matches_frozen_corpus"
                )
                is True,
            }
        )
    return {"checks": checks, "passed": all(checks.values())}


def evaluate_q1(
    off: dict[str, Any],
    on_vectors: list[list[float]],
    on_scores: list[float],
    on_health: dict[str, Any],
) -> dict[str, Any]:
    embed_diff = max_abs_matrix_difference(off["identity"]["embed_vectors"], on_vectors)
    rerank_diff = max_abs_vector_difference(off["identity"]["rerank_scores"], on_scores)
    checks = {
        "embed_sample_shape": len(on_vectors) == EMBED_SAMPLE_COUNT,
        "embed_max_abs_diff_le_1e_12": embed_diff <= Q1_TOLERANCE,
        "rerank_sample_shape": len(on_scores) == RERANK_SAMPLE_COUNT,
        "rerank_max_abs_diff_le_1e_12": rerank_diff <= Q1_TOLERANCE,
        "runtime_identity_unchanged": runtime_identity(on_health)
        == off["runtime_identity"],
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "embed_max_abs_diff": embed_diff,
        "rerank_max_abs_diff": rerank_diff,
        "off_runtime_identity": off["runtime_identity"],
        "on_runtime_identity": runtime_identity(on_health),
    }


def evaluate_q2(
    mixed: dict[str, Any],
    telemetry: dict[str, Any],
) -> dict[str, Any]:
    embed_p95 = mixed["embed"]["latency_seconds"]["p95"]
    checks = {
        "embed_100_of_100": mixed["embed"]["successful"] == Q2_EMBED_CALLS,
        "embed_zero_failures": mixed["embed"]["failed"] == 0,
        "mixed_rerank_sample_at_least_20": mixed["rerank"]["successful"]
        >= Q2_MIN_MIXED_RERANK_CALLS,
        "continuous_rerank_zero_failures": mixed["rerank"]["failed"] == 0,
        "embed_p95_lt_2_seconds": embed_p95 is not None
        and embed_p95 < Q2_EMBED_P95_SECONDS,
        "rerank_worker_stopped": mixed["rerank_thread_stopped"] is True,
        "launch_barrier_passed": mixed["launch_barrier_passed"] is True,
        "at_least_5_reranks_overlapped_embeds": mixed["overlapped_rerank_calls"]
        >= Q2_MIN_OVERLAPPED_RERANK_CALLS,
        "current_run_embed_grants": telemetry["delta"]["grants"]["embed"]
        >= Q2_EMBED_CALLS,
        "current_run_embed_releases": telemetry["delta"]["releases"]["embed"]
        >= Q2_EMBED_CALLS,
        "current_run_embed_wait_samples": telemetry["delta"]["wait_sample_count"][
            "embed"
        ]
        >= Q2_EMBED_CALLS,
        "current_run_embed_hold_samples": telemetry["delta"]["hold_sample_count"][
            "embed"
        ]
        >= Q2_EMBED_CALLS,
        "zero_fail_open_alerts_before_q4": telemetry["pre_q4_fail_open_alert_count"]
        == 0,
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "embed_p95_seconds": embed_p95,
        "overlapped_rerank_calls": mixed["overlapped_rerank_calls"],
        "scheduler_delta": telemetry["delta"],
    }


def evaluate_q3(
    on_solo: dict[str, Any],
    mixed: dict[str, Any],
    arbiter_health: dict[str, Any],
    telemetry: dict[str, Any],
) -> dict[str, Any]:
    solo_p95 = on_solo["latency_seconds"]["p95"]
    mixed_rerank_p95 = mixed["rerank"]["latency_seconds"]["p95"]
    ratio = (
        mixed_rerank_p95 / solo_p95
        if solo_p95 is not None and solo_p95 > 0 and mixed_rerank_p95 is not None
        else math.inf
    )
    scheduler = arbiter_health.get("scheduler") or {}
    hold_p95 = (scheduler.get("hold_p95_ms") or {}).get("rerank")
    checks = {
        "solo_sample_complete": on_solo["successful"] == SOLO_RERANK_CALLS
        and on_solo["failed"] == 0,
        "mixed_rerank_sample_at_least_20": mixed["rerank"]["successful"]
        >= Q2_MIN_MIXED_RERANK_CALLS,
        "rerank_p95_ratio_le_2": ratio <= Q3_RERANK_RATIO_MAX,
        "rerank_hold_p95_present": hold_p95 is not None,
        "rerank_hold_p95_le_500_ms": hold_p95 is not None
        and float(hold_p95) <= Q3_RERANK_HOLD_P95_MS_MAX,
        "rerank_hold_sample_count_present": int(
            (scheduler.get("hold_sample_count") or {}).get("rerank", 0)
        )
        > 0,
        "current_run_rerank_grants": telemetry["delta"]["grants"]["rerank"]
        >= SOLO_RERANK_CALLS + Q2_MIN_MIXED_RERANK_CALLS,
        "current_run_rerank_releases": telemetry["delta"]["releases"]["rerank"]
        >= SOLO_RERANK_CALLS + Q2_MIN_MIXED_RERANK_CALLS,
        "current_run_rerank_wait_samples": telemetry["delta"]["wait_sample_count"][
            "rerank"
        ]
        >= SOLO_RERANK_CALLS + Q2_MIN_MIXED_RERANK_CALLS,
        "current_run_rerank_hold_samples": telemetry["delta"]["hold_sample_count"][
            "rerank"
        ]
        >= SOLO_RERANK_CALLS + Q2_MIN_MIXED_RERANK_CALLS,
        "zero_fail_open_alerts_before_q4": telemetry["pre_q4_fail_open_alert_count"]
        == 0,
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "solo_rerank_p95_seconds": solo_p95,
        "mixed_rerank_p95_seconds": mixed_rerank_p95,
        "mixed_to_solo_p95_ratio": ratio,
        "arbiter_rerank_hold_p95_ms": hold_p95,
        "arbiter_rerank_hold_sample_count": (
            scheduler.get("hold_sample_count") or {}
        ).get("rerank"),
        "scheduler_delta": telemetry["delta"],
    }


def evaluate_q4(
    soak: dict[str, Any],
) -> dict[str, Any]:
    fail_open = soak["fail_open"]
    checks = {
        "soak_duration_600_seconds": soak["requested_soak_seconds"]
        == PRODUCTION_SOAK_SECONDS,
        "kill_at_fixed_300_second_point": soak["kill_at_seconds"]
        == PRODUCTION_KILL_AT_SECONDS,
        "kill_started_during_soak": 0
        < soak["failure_started_elapsed_seconds"]
        < soak["requested_soak_seconds"],
        "all_workers_active_at_kill": len(soak["workers_active_at_kill"])
        == Q2_EMBED_CONCURRENCY + 1,
        "soak_embed_present": soak["embed"]["successful"] > 0,
        "soak_rerank_present": soak["rerank"]["successful"] > 0,
        "soak_zero_errors": soak["embed"]["failed"] == 0
        and soak["rerank"]["failed"] == 0,
        "soak_zero_deadlock": soak["zero_deadlock"] is True,
        "fail_open_embed_and_rerank_succeeded": not fail_open["probe_errors"],
        "old_process_gone_before_probes": fail_open["old_process_gone_before_probes"]
        is True,
        "arbiter_endpoint_down_before_probes": fail_open[
            "arbiter_endpoint_down_before_probes"
        ]
        is True,
        "both_workload_alerts_seen": all(
            fail_open["alerts_seen_in_new_log_bytes"].get(workload) is True
            for workload in ("embed", "rerank")
        ),
        "runtime_recovered": fail_open["recovered"] is True,
        "replacement_pid_changed": fail_open["replacement_pid"] != fail_open["pid"],
        "replacement_start_identity_changed": (
            (fail_open["replacement_identity"] or {}).get("start_identity")
            != (fail_open["process_identity"] or {}).get("start_identity")
        ),
        "embed_continued_after_recovery": soak["embed_successes_after_recovery"] > 0,
        "rerank_continued_after_recovery": soak["rerank_successes_after_recovery"] > 0,
    }
    return {"checks": checks, "passed": all(checks.values())}


def evaluate_q5(
    off: dict[str, Any],
    frozen_spot: dict[str, Any],
    canonical_suite: dict[str, Any],
) -> dict[str, Any]:
    checks = {
        "canonical_build_suite_exit_zero": canonical_suite.get("exit_code") == 0
        and canonical_suite.get("passed") is True,
        "spot_technical": frozen_spot.get("technical_success") is True,
        "spot_doc_hit": frozen_spot.get("doc_hit") is True,
        "spot_citations": frozen_spot.get("citation_membership_rate") == 1.0
        and frozen_spot.get("all_citations_in_corpus") is True,
        "spot_tier": frozen_spot.get("effective_tier") == FROZEN_TIER,
        "off_spot_answered": off["frozen_spot"].get("verdict") == "answered",
        "on_spot_answered": frozen_spot.get("verdict") == "answered",
        "spot_verdict_unchanged": frozen_spot.get("verdict")
        == off["frozen_spot"].get("verdict"),
        "off_exact_frozen_corpus": (off["frozen_spot"].get("corpus_binding") or {}).get(
            "matches_frozen_corpus"
        )
        is True,
        "on_exact_frozen_corpus": (frozen_spot.get("corpus_binding") or {}).get(
            "matches_frozen_corpus"
        )
        is True,
        "selection_sha_unchanged": (off["frozen_spot"].get("corpus_binding") or {}).get(
            "selection_sha256"
        )
        == FROZEN_SELECTION_SHA256
        == (frozen_spot.get("corpus_binding") or {}).get("selection_sha256"),
    }
    return {"checks": checks, "passed": all(checks.values())}


def evaluate_on_gates(
    *,
    off: dict[str, Any],
    on_vectors: list[list[float]],
    on_scores: list[float],
    on_health: dict[str, Any],
    on_solo: dict[str, Any],
    mixed: dict[str, Any],
    arbiter_health: dict[str, Any],
    telemetry: dict[str, Any],
    soak: dict[str, Any],
    frozen_spot: dict[str, Any],
    canonical_suite: dict[str, Any],
) -> dict[str, Any]:
    gates = {
        "q1": evaluate_q1(off, on_vectors, on_scores, on_health),
        "q2": evaluate_q2(mixed, telemetry),
        "q3": evaluate_q3(on_solo, mixed, arbiter_health, telemetry),
        "q4": evaluate_q4(soak),
        "q5": evaluate_q5(off, frozen_spot, canonical_suite),
    }
    gates["passed"] = all(
        gates[name]["passed"] for name in ("q1", "q2", "q3", "q4", "q5")
    )
    return gates


def capture_off(
    client: LiveClient,
    *,
    local_only: bool = False,
) -> dict[str, Any]:
    fixture = build_fixture()
    verify_fixture(fixture)
    health = client.health_snapshot(expected_enabled=False)
    vectors = validate_embedding_matrix(client.embed(fixture["embed_texts"]))
    scores = validate_rerank_scores(
        client.rerank(fixture["rerank_query"], fixture["rerank_documents"])
    )
    solo = measure_solo_rerank(client, fixture)
    frozen_spot = None if local_only else client.frozen_spot()
    baseline = evaluate_off_baseline(
        health=health,
        vectors=vectors,
        scores=scores,
        solo=solo,
        frozen_spot=frozen_spot,
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "phase": "off",
        "mode": "local_only" if local_only else "full_q1_q5",
        "policy": LOCAL_ONLY_POLICY if local_only else None,
        "provider_call_count": 0 if local_only else 1,
        "captured_at_utc": _utc_now(),
        "corpus_id": client.config.corpus_id,
        "fixture": fixture,
        "health": health,
        "runtime_identity": runtime_identity(health),
        "identity": {
            "embed_vectors": vectors,
            "rerank_scores": scores,
        },
        "solo_rerank": solo,
        "baseline_gate": baseline,
    }
    if frozen_spot is not None:
        payload["frozen_spot"] = frozen_spot
    return _seal(payload)


def run_on(
    client: LiveClient,
    off: dict[str, Any],
    *,
    soak_seconds: float = PRODUCTION_SOAK_SECONDS,
    run_soak: Callable[..., dict[str, Any]] = run_q4_soak,
    run_failure_probe: Callable[..., dict[str, Any]] = run_fail_open_probe,
    run_suite: Callable[..., dict[str, Any]] = run_canonical_suite,
    local_only: bool = False,
) -> dict[str, Any]:
    _verify_seal(off)
    if off.get("schema_version") != SCHEMA_VERSION or off.get("phase") != "off":
        raise HarnessError("OFF artifact identity is invalid")
    if off.get("corpus_id") != client.config.corpus_id:
        raise HarnessError("OFF artifact corpus_id differs from the ON request")
    if off.get("baseline_gate", {}).get("passed") is not True:
        raise HarnessError("OFF baseline was not green")
    expected_mode = "local_only" if local_only else "full_q1_q5"
    if off.get("mode", "full_q1_q5") != expected_mode:
        raise HarnessError("OFF artifact mode differs from the ON request")
    fixture = off["fixture"]
    verify_fixture(fixture)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "phase": "on",
        "mode": expected_mode,
        "policy": LOCAL_ONLY_POLICY if local_only else None,
        "provider_call_count": 0 if local_only else 1,
        "captured_at_utc": _utc_now(),
        "corpus_id": client.config.corpus_id,
        "off_artifact_seal_sha256": off["seal_sha256"],
        "fixture_sha256": fixture["fixture_sha256"],
    }
    gates: dict[str, Any] = {}
    required_gates = (
        ("canonical_suite", "q1", "q2", "q3", "q4")
        if local_only
        else ("q1", "q2", "q3", "q4", "q5")
    )

    def finish(*, stopped_after: str) -> dict[str, Any]:
        complete = all(name in gates for name in required_gates)
        gates["passed"] = complete and all(
            gates[name]["passed"] for name in required_gates
        )
        gates["stopped_after"] = stopped_after
        payload["gates"] = gates
        return _seal(payload)

    canonical_suite = run_suite(repo_root=REPO_ROOT)
    payload["canonical_suite_q5"] = canonical_suite
    if canonical_suite.get("passed") is not True:
        failed_gate = "canonical_suite" if local_only else "q5"
        gates[failed_gate] = {
            "checks": {"canonical_build_suite_exit_zero": False},
            "passed": False,
        }
        return finish(stopped_after=f"{failed_gate}_red")
    if local_only:
        gates["canonical_suite"] = {
            "checks": {"canonical_build_suite_exit_zero": True},
            "passed": True,
        }

    health = client.health_snapshot(expected_enabled=True)
    payload["health"] = health
    pre_q4_log_offset = (
        client.config.error_log.stat().st_size
        if client.config.error_log.exists()
        else 0
    )
    vectors = validate_embedding_matrix(client.embed(fixture["embed_texts"]))
    scores = validate_rerank_scores(
        client.rerank(fixture["rerank_query"], fixture["rerank_documents"])
    )
    payload["identity"] = {
        "embed_vectors": vectors,
        "rerank_scores": scores,
    }
    gates["q1"] = evaluate_q1(off, vectors, scores, health)
    if gates["q1"]["passed"] is not True:
        return finish(stopped_after="q1_red")

    telemetry_before = scheduler_snapshot(client)
    solo = measure_solo_rerank(client, fixture)
    mixed = run_q2_mixed(client, fixture)
    arbiter_health = scheduler_snapshot(client)
    pre_q4_new_log = _read_new_log_bytes(client.config.error_log, pre_q4_log_offset)
    telemetry = {
        "before": telemetry_before,
        "after": arbiter_health,
        "delta": scheduler_delta(telemetry_before, arbiter_health),
        "pre_q4_log_offset": pre_q4_log_offset,
        "pre_q4_new_log_sha256": _sha256(pre_q4_new_log.encode("utf-8")),
        "pre_q4_fail_open_alert_count": _count_fail_open_alerts(pre_q4_new_log),
    }
    payload["solo_rerank"] = solo
    payload["mixed_q2_q3"] = mixed
    payload["arbiter_health_after_mixed"] = arbiter_health
    payload["q2_q3_scheduler_telemetry"] = telemetry
    gates["q2"] = evaluate_q2(mixed, telemetry)
    gates["q3"] = evaluate_q3(solo, mixed, arbiter_health, telemetry)
    if gates["q2"]["passed"] is not True or gates["q3"]["passed"] is not True:
        return finish(stopped_after="q2_q3_red")

    soak = run_soak(
        client,
        fixture,
        soak_seconds=soak_seconds,
        kill_at_seconds=PRODUCTION_KILL_AT_SECONDS,
        failure_probe=run_failure_probe,
    )
    fail_open = soak["fail_open"]
    payload["soak_q4"] = soak
    payload["fail_open_q4"] = fail_open
    gates["q4"] = evaluate_q4(soak)
    if gates["q4"]["passed"] is not True:
        return finish(stopped_after="q4_red")

    if local_only:
        gates["q5"] = {
            "passed": None,
            "status": "not_run_owner_zero_provider_law",
            "policy": LOCAL_ONLY_POLICY,
            "provider_call_count": 0,
        }
        return finish(stopped_after="q4_local_only_complete")

    frozen_spot = client.frozen_spot()
    payload["frozen_spot"] = frozen_spot
    gates["q5"] = evaluate_q5(off, frozen_spot, canonical_suite)
    return finish(stopped_after="q5_complete")


def _load_auth_token(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise HarnessError("this platform cannot enforce no-follow token loading")
    flags |= nofollow
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HarnessError(
            f"unable to securely open auth token file: {type(exc).__name__}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if not stat.S_ISREG(metadata.st_mode):
            raise HarnessError("auth token path must be a regular file")
        if metadata.st_uid != os.getuid():
            raise HarnessError("auth token file must be owned by the current user")
        if mode & 0o077:
            raise HarnessError("auth token file must be mode 0600 or stricter")
        if not mode & stat.S_IRUSR:
            raise HarnessError("auth token file must be owner-readable")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            token = handle.read().strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not token:
        raise HarnessError("auth token file is empty")
    return token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="phase", required=True)

    def common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--output", type=Path, required=True)
        subparser.add_argument("--corpus-id", required=True)
        subparser.add_argument("--auth-token-file", type=Path, required=True)
        subparser.add_argument(
            "--prereg",
            type=Path,
            default=DEFAULT_PREREG,
        )
        subparser.add_argument(
            "--selection",
            type=Path,
            default=DEFAULT_SELECTION,
        )
        subparser.add_argument("--embedder-url", default="http://127.0.0.1:8082")
        subparser.add_argument("--reranker-url", default="http://127.0.0.1:8081")
        subparser.add_argument("--arbiter-url", default="http://127.0.0.1:8085")
        subparser.add_argument("--backend-url", default="http://127.0.0.1:8000")
        subparser.add_argument("--pid-file", type=Path, default=DEFAULT_PID_FILE)
        subparser.add_argument("--error-log", type=Path, default=DEFAULT_ERROR_LOG)
        subparser.add_argument(
            "--local-only",
            action="store_true",
            help=(
                "Run provider-free Q1-Q4 plus canonical local tests under "
                f"{LOCAL_ONLY_POLICY}; Q5 remains explicitly not run."
            ),
        )

    capture = subparsers.add_parser("capture-off")
    common(capture)
    run = subparsers.add_parser("run-on")
    common(run)
    run.add_argument("--off-artifact", type=Path, required=True)
    return parser


def _config(args: argparse.Namespace) -> HarnessConfig:
    return HarnessConfig(
        embedder_url=args.embedder_url.rstrip("/"),
        reranker_url=args.reranker_url.rstrip("/"),
        arbiter_url=args.arbiter_url.rstrip("/"),
        backend_url=args.backend_url.rstrip("/"),
        corpus_id=str(args.corpus_id),
        auth_token=_load_auth_token(args.auth_token_file),
        prereg_path=args.prereg,
        selection_path=args.selection,
        pid_file=args.pid_file,
        error_log=args.error_log,
    )


def main() -> int:
    args = _parser().parse_args()
    try:
        config = _config(args)
        client = LiveClient(config)
        if args.phase == "capture-off":
            artifact = capture_off(client, local_only=args.local_only)
            _atomic_write_json(args.output, artifact)
            print(
                json.dumps(
                    {
                        "phase": "off",
                        "output": str(args.output),
                        "seal_sha256": artifact["seal_sha256"],
                        "passed": artifact["baseline_gate"]["passed"],
                    },
                    sort_keys=True,
                )
            )
            return 0 if artifact["baseline_gate"]["passed"] else 1
        off = json.loads(args.off_artifact.read_text(encoding="utf-8"))
        artifact = run_on(
            client,
            off,
            soak_seconds=PRODUCTION_SOAK_SECONDS,
            local_only=args.local_only,
        )
        _atomic_write_json(args.output, artifact)
        print(
            json.dumps(
                {
                    "phase": "on",
                    "output": str(args.output),
                    "seal_sha256": artifact["seal_sha256"],
                    "passed": artifact["gates"]["passed"],
                    "gates": {
                        name: (
                            artifact["gates"].get(name, {}).get("passed")
                            if name in artifact["gates"]
                            else None
                        )
                        for name in ("q1", "q2", "q3", "q4", "q5")
                    },
                    "stopped_after": artifact["gates"].get("stopped_after"),
                },
                sort_keys=True,
            )
        )
        return 0 if artifact["gates"]["passed"] else 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "phase": getattr(args, "phase", "unknown"),
                    "passed": False,
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
