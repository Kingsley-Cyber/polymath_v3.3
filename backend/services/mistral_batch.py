"""Mistral Batch API client — async, used by Ghost A / Ghost B for offline
high-throughput summary + extraction at 50% sync price.

Background:
  Mistral's batch API processes up to 1M requests in a single .jsonl
  upload. Per-request body matches the live `/v1/chat/completions`
  shape EXCEPT the `model` field is set at JOB level (one model per
  batch). Mistral's fleet parallelizes server-side; throughput is
  dramatically higher than what a single sync client can sustain.

  Pricing: 50% of sync rates.
  SLA: completion within 24h (typical: minutes to ~1 hour).
  Endpoints supported: /v1/chat/completions, /v1/embeddings, /v1/ocr,
  /v1/classifications, /v1/conversations, /v1/audio/transcriptions,
  /v1/fim/completions, /v1/moderations, /v1/chat/moderations.

  Polymath uses /v1/chat/completions for both Ghost A (summary) and
  Ghost B (extraction). Two separate batch jobs per corpus when both
  phases use batch mode.

Workflow:
  1. build_jsonl(tasks)                  — assemble per-line bodies
  2. await client.upload(jsonl_bytes)    — POST /v1/files (purpose=batch)
  3. await client.submit(file_id, model) — POST /v1/batch/jobs
  4. poll loop: await client.get_job(id) — GET /v1/batch/jobs/{id}
  5. on SUCCESS: await client.download(output_file_id)
  6. parse output .jsonl (one row per request) and integrate

Per-request errors land in `error_file` (separate from the batch-level
`errors[]` field). Treat them as soft failures and route to
graph_repair queue.

This module does NOT decide POLICY (when to use batch vs sync) — that
belongs to the worker. It just provides the HTTP primitives.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.mistral.ai/v1"

# Mistral batch SLA window. Beyond this the batch transitions to
# TIMEOUT_EXCEEDED. Worker should detect and resubmit.
BATCH_TIMEOUT_HOURS = 24

# Polling cadence — Mistral discourages tight polling. 30-60s is fine
# for batches that take minutes-to-hours; for very small dev batches
# you can poll faster.
DEFAULT_POLL_SECONDS = 30
FAST_POLL_SECONDS = 5

TERMINAL_STATUSES = {"SUCCESS", "FAILED", "TIMEOUT_EXCEEDED", "CANCELLED"}


@dataclass
class BatchSubmitResult:
    job_id: str
    status: str
    total_requests: int
    created_at: int
    metadata: dict[str, Any]


@dataclass
class BatchJobStatus:
    job_id: str
    status: str  # QUEUED | RUNNING | SUCCESS | FAILED | TIMEOUT_EXCEEDED | CANCELLATION_REQUESTED | CANCELLED
    total_requests: int
    completed_requests: int
    succeeded_requests: int
    failed_requests: int
    output_file: str | None
    error_file: str | None
    errors: list[dict[str, Any]]
    created_at: int
    started_at: int | None
    completed_at: int | None
    metadata: dict[str, Any]

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_done(self) -> bool:
        return self.status == "SUCCESS"


class MistralBatchClient:
    """Async client for Mistral Batch API.

    Single-instance per (api_key, base_url) tuple is fine — httpx
    AsyncClient handles connection pooling internally. Caller is
    responsible for closing via __aexit__ or manual `aclose()`.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("MistralBatchClient requires a non-empty api_key")
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = httpx.Timeout(timeout_seconds, connect=10.0)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "MistralBatchClient":
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            # Lazy init for callers that skip the context manager.
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    # ── /v1/files ──────────────────────────────────────────────────────

    async def upload(self, jsonl_bytes: bytes, *, filename: str = "batch.jsonl") -> str:
        """Upload a .jsonl input file. Returns file_id.

        purpose=batch tells Mistral this file will feed a batch job (not
        fine-tuning, not other purposes). The file_id round-trips through
        /v1/batch/jobs.
        """
        url = f"{self.base_url}/files"
        files = {"file": (filename, jsonl_bytes, "application/jsonl")}
        data = {"purpose": "batch"}
        resp = await self._http().post(
            url, headers=self._headers, files=files, data=data
        )
        resp.raise_for_status()
        body = resp.json()
        file_id = body.get("id")
        if not file_id:
            raise RuntimeError(f"Mistral upload returned no file id: {body}")
        logger.info(
            "mistral_batch: uploaded %s bytes as file_id=%s",
            f"{len(jsonl_bytes):,}", file_id,
        )
        return file_id

    # ── /v1/batch/jobs ─────────────────────────────────────────────────

    async def submit(
        self,
        *,
        input_file_ids: list[str],
        model: str,
        endpoint: str = "/v1/chat/completions",
        metadata: dict[str, Any] | None = None,
        timeout_hours: int = BATCH_TIMEOUT_HOURS,
    ) -> BatchSubmitResult:
        """Submit a batch job. Returns BatchSubmitResult with job_id."""
        url = f"{self.base_url}/batch/jobs"
        body: dict[str, Any] = {
            "input_files": input_file_ids,
            "model": model,
            "endpoint": endpoint,
            "timeout_hours": timeout_hours,
        }
        if metadata:
            body["metadata"] = metadata
        resp = await self._http().post(url, headers=self._headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        return BatchSubmitResult(
            job_id=data["id"],
            status=data.get("status") or "QUEUED",
            total_requests=int(data.get("total_requests") or 0),
            created_at=int(data.get("created_at") or time.time()),
            metadata=data.get("metadata") or {},
        )

    async def get_job(self, job_id: str) -> BatchJobStatus:
        url = f"{self.base_url}/batch/jobs/{job_id}"
        resp = await self._http().get(url, headers=self._headers)
        resp.raise_for_status()
        data = resp.json()
        return BatchJobStatus(
            job_id=data["id"],
            status=data["status"],
            total_requests=int(data.get("total_requests") or 0),
            completed_requests=int(data.get("completed_requests") or 0),
            succeeded_requests=int(data.get("succeeded_requests") or 0),
            failed_requests=int(data.get("failed_requests") or 0),
            output_file=data.get("output_file"),
            error_file=data.get("error_file"),
            errors=data.get("errors") or [],
            created_at=int(data.get("created_at") or 0),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            metadata=data.get("metadata") or {},
        )

    async def cancel(self, job_id: str) -> BatchJobStatus:
        url = f"{self.base_url}/batch/jobs/{job_id}/cancel"
        resp = await self._http().post(url, headers=self._headers)
        resp.raise_for_status()
        return await self.get_job(job_id)

    async def wait_until_done(
        self,
        job_id: str,
        *,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        deadline_seconds: float | None = None,
        on_tick: callable | None = None,
    ) -> BatchJobStatus:
        """Poll until terminal state. Caller can pass a deadline (wall clock)
        and/or an on_tick callback for live progress logging.
        """
        started = time.monotonic()
        while True:
            status = await self.get_job(job_id)
            if on_tick is not None:
                try:
                    on_tick(status)
                except Exception:
                    logger.exception("mistral_batch: on_tick raised (ignored)")
            if status.is_terminal:
                return status
            if deadline_seconds is not None and (time.monotonic() - started) > deadline_seconds:
                logger.warning(
                    "mistral_batch: deadline %ds exceeded for job=%s status=%s",
                    int(deadline_seconds), job_id, status.status,
                )
                return status
            await asyncio.sleep(poll_seconds)

    # ── /v1/files/{id}/content ─────────────────────────────────────────

    async def download(self, file_id: str) -> bytes:
        """Download a result/error file's raw bytes (.jsonl). Returns
        empty bytes for empty files (e.g., error_file when no errors).
        """
        url = f"{self.base_url}/files/{file_id}/content"
        resp = await self._http().get(url, headers=self._headers)
        resp.raise_for_status()
        return resp.content

    async def iter_output_lines(self, file_id: str) -> AsyncIterator[dict[str, Any]]:
        """Stream-decode the output .jsonl one parsed dict at a time.

        Each yielded dict has shape:
          {
            "id": "<request id>",
            "custom_id": "<your tag>",
            "response": {
              "status_code": 200,
              "request_id": "...",
              "body": {...full /v1/chat/completions body...}
            },
            "error": null  (or {"code": str, "message": str} on failure)
          }
        """
        raw = await self.download(file_id)
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "mistral_batch: skipping malformed line in output file_id=%s: %s",
                    file_id, exc,
                )


def build_chat_jsonl(
    tasks: list[dict[str, Any]],
) -> bytes:
    """Build a Mistral batch input .jsonl from a list of per-task dicts.

    Each task dict shape:
      {
        "custom_id": str   (required — round-trips through output)
        "messages":  list  (required — chat messages)
        "max_tokens":      (optional, defaults to model max)
        "temperature":     (optional)
        "response_format": (optional — JSON schema response_format)
        "extra":           (optional dict merged into body)
      }

    Returns UTF-8 .jsonl bytes ready for upload(). Note: the `model`
    field is intentionally NOT included per task — it's set at job
    creation time and inherited by every line in the file.
    """
    lines: list[bytes] = []
    for task in tasks:
        custom_id = task["custom_id"]
        body: dict[str, Any] = {"messages": task["messages"]}
        for key in ("max_tokens", "temperature", "response_format"):
            if key in task and task[key] is not None:
                body[key] = task[key]
        for k, v in (task.get("extra") or {}).items():
            if k not in body and k != "model":
                body[k] = v
        lines.append(json.dumps({"custom_id": custom_id, "body": body}).encode("utf-8"))
    return b"\n".join(lines)
