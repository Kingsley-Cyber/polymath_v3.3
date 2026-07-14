"""Pure P2.7b burst manifest, barrier, metrics, and retry-safety contracts.

This module does not dispatch, persist, or mutate live state.  Its barrier is
derived from the existing CP1-D2a durable-job and artifact truth; it does not
create a second batch-completeness state machine.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from models.hash_taxonomy import namespace_hash
from services.ingestion.extraction_jobs import (
    terminal_extraction_artifact_matches_job,
)


BURST_MANIFEST_VERSION = "extraction_burst_manifest.v1"
BURST_METRICS_VERSION = "extraction_burst_metrics.v1"

_PIPELINE_TERMINAL = frozenset({"succeeded", "skipped", "superseded"})
_EXTRACTION_RUNNABLE = frozenset(
    {"queued", "provider_failed", "validation_failed", "failed"}
)
_EXTRACTION_TERMINAL = frozenset({"succeeded", "promoted", "skipped"})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, protected_namespaces=())


class CorpusDisposition(StrictModel):
    matrix_version: str
    corpus_id: str
    mode: Literal["reingest", "re_extract_only", "projection_only"]
    owner_status: Literal["approved", "pending", "not_required"]
    rationale: str


class ManifestChunk(StrictModel):
    chunk_id: str
    doc_id: str
    chunk_hash: str
    extraction_job_id: str
    extraction_contract_hash: str
    extraction_job_status: str


class ExtractionBurstManifest(StrictModel):
    schema_version: Literal["extraction_burst_manifest.v1"]
    authority: Literal["executor_proposed_owner_ratifiable"]
    completeness_source: Literal["cp1_d2a_durable_jobs_and_artifacts"]
    corpus_id: str
    disposition: CorpusDisposition
    source_batch_status: str
    active_ingest_items: int
    document_count: int
    valid_chunk_count: int
    terminal_chunk_count: int
    runnable_chunk_count: int
    document_pipeline_job_counts: dict[str, int]
    extraction_job_counts: dict[str, int]
    dispatch_allowed: bool
    blockers: list[str]
    chunks: list[ManifestChunk]

    @model_validator(mode="after")
    def validate_manifest_totals(self) -> "ExtractionBurstManifest":
        if self.active_ingest_items < 0:
            raise ValueError("active ingest count cannot be negative")
        if self.runnable_chunk_count != len(self.chunks):
            raise ValueError("runnable count must equal manifest chunk entries")
        if (
            self.terminal_chunk_count + self.runnable_chunk_count
            > self.valid_chunk_count
        ):
            raise ValueError("terminal+runnable chunks cannot exceed valid chunks")
        if self.dispatch_allowed and self.blockers:
            raise ValueError("dispatch cannot be allowed while blockers exist")
        if self.dispatch_allowed and not self.chunks:
            raise ValueError("dispatch requires at least one runnable chunk")
        if self.disposition.corpus_id != self.corpus_id:
            raise ValueError("disposition corpus must match manifest corpus")
        return self

    @property
    def manifest_id(self) -> str:
        digest = namespace_hash("work", self.model_dump()).split(":", 1)[1]
        return f"extraction-burst:{digest}"


class RetrySafetyDecision(StrictModel):
    action: Literal["skip_provider_call", "run_bounded_retry"]
    reason: str
    protected_artifact_policy: Literal["preserve_existing"]
    protected_surfaces: list[str]


class LaneBurstMetrics(StrictModel):
    lane_id: str
    engine: str
    engine_runtime_version: str
    model_id: str
    request_batches: int
    batch_failures: int
    fallback_events: int
    unique_chunks_assigned: int
    succeeded_chunks: int
    failed_chunks: int
    skipped_chunks: int
    worker_seconds: float
    billed_worker_seconds: float
    compute_cost_usd: float

    @model_validator(mode="after")
    def validate_lane_counts(self) -> "LaneBurstMetrics":
        numeric = (
            self.request_batches,
            self.batch_failures,
            self.fallback_events,
            self.unique_chunks_assigned,
            self.succeeded_chunks,
            self.failed_chunks,
            self.skipped_chunks,
        )
        if min(numeric) < 0:
            raise ValueError("lane counts cannot be negative")
        if (
            min(self.worker_seconds, self.billed_worker_seconds, self.compute_cost_usd)
            < 0
        ):
            raise ValueError("lane time/cost values cannot be negative")
        if (
            self.succeeded_chunks + self.failed_chunks + self.skipped_chunks
            > self.unique_chunks_assigned
        ):
            raise ValueError("lane outcomes cannot exceed unique assigned chunks")
        return self


class ExtractionBurstMetrics(StrictModel):
    schema_version: Literal["extraction_burst_metrics.v1"]
    authority: Literal["measurement_only_no_production_stamp"]
    manifest_id: str
    eligible_chunks: int
    succeeded_chunks: int
    failed_chunks: int
    skipped_chunks: int
    duration_seconds: float
    chunks_per_second: float
    worker_seconds: float
    billed_worker_seconds: float
    compute_cost_usd: float
    estimated_cost_only: bool
    cost_per_1k_chunks_usd: float | None
    failure_rate: float
    fallback_rate: float
    request_batches: int
    batch_failures: int
    fallback_events: int
    lanes: list[LaneBurstMetrics]

    @model_validator(mode="after")
    def validate_burst_totals(self) -> "ExtractionBurstMetrics":
        if self.eligible_chunks < 0:
            raise ValueError("eligible chunks cannot be negative")
        if (
            self.succeeded_chunks + self.failed_chunks + self.skipped_chunks
            != self.eligible_chunks
        ):
            raise ValueError("every eligible chunk needs one terminal burst outcome")
        if not 0.0 <= self.failure_rate <= 1.0:
            raise ValueError("failure_rate must be between 0 and 1")
        if not 0.0 <= self.fallback_rate <= 1.0:
            raise ValueError("fallback_rate must be between 0 and 1")
        return self


def _dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return dict(value or {})


def build_extraction_burst_manifest(
    *,
    corpus_id: str,
    disposition: CorpusDisposition,
    source_batch_status: str,
    active_ingest_items: int,
    documents: list[dict[str, Any]],
    valid_chunks: list[dict[str, Any]],
    document_pipeline_jobs: list[dict[str, Any]],
    extraction_jobs: list[dict[str, Any]],
) -> ExtractionBurstManifest:
    """Derive dispatch eligibility solely from durable jobs and artifacts."""

    blockers: set[str] = set()
    doc_ids = [str(row.get("doc_id") or "") for row in documents]
    chunk_ids = [str(row.get("chunk_id") or "") for row in valid_chunks]
    if (
        not doc_ids
        or any(not value for value in doc_ids)
        or len(doc_ids) != len(set(doc_ids))
    ):
        blockers.add("document_identity_invalid")
    if (
        not chunk_ids
        or any(not value for value in chunk_ids)
        or len(chunk_ids) != len(set(chunk_ids))
    ):
        blockers.add("chunk_identity_invalid")

    doc_id_set = set(doc_ids)
    chunks_by_doc = Counter(str(row.get("doc_id") or "") for row in valid_chunks)
    if any(chunks_by_doc.get(doc_id, 0) == 0 for doc_id in doc_id_set):
        blockers.add("document_missing_valid_chunks")
    if any(str(row.get("doc_id") or "") not in doc_id_set for row in valid_chunks):
        blockers.add("chunk_document_outside_disposition")
    if any(row.get("metadata_extracted") is not True for row in documents):
        blockers.add("document_metadata_incomplete")
    if any(row.get("chunking_complete") is not True for row in documents):
        blockers.add("document_chunking_incomplete")

    if active_ingest_items:
        blockers.add("active_ingest_items")
    if disposition.mode == "projection_only":
        blockers.add("projection_only_disallows_extraction")
    if disposition.owner_status == "pending":
        blockers.add("disposition_owner_approval_pending")
    if disposition.mode == "reingest" and source_batch_status != "done":
        blockers.add("reingest_batch_not_done")
    if disposition.mode == "re_extract_only" and source_batch_status not in {
        "done",
        "not_applicable_existing_corpus",
    }:
        blockers.add("source_batch_state_not_terminal")

    pipeline_counts = Counter(
        str(row.get("status") or "unknown") for row in document_pipeline_jobs
    )
    for row in document_pipeline_jobs:
        if str(row.get("kind") or "") not in {"chunk_document", "persist_document"}:
            continue
        if str(row.get("status") or "") not in _PIPELINE_TERMINAL:
            blockers.add("durable_document_pipeline_jobs_incomplete")

    current_jobs_by_chunk: dict[str, list[dict[str, Any]]] = {}
    for raw in extraction_jobs:
        row = _dict(raw)
        if str(row.get("status") or "") == "superseded":
            continue
        current_jobs_by_chunk.setdefault(str(row.get("chunk_id") or ""), []).append(row)
    extraction_counts = Counter(
        str(row.get("status") or "unknown")
        for rows in current_jobs_by_chunk.values()
        for row in rows
    )

    terminal_count = 0
    entries: list[ManifestChunk] = []
    for chunk in sorted(valid_chunks, key=lambda row: str(row.get("chunk_id") or "")):
        chunk_id = str(chunk.get("chunk_id") or "")
        current = current_jobs_by_chunk.get(chunk_id) or []
        if len(current) != 1:
            blockers.add(
                "missing_current_extraction_job"
                if not current
                else "multiple_current_extraction_jobs"
            )
            continue
        job = current[0]
        chunk_hash = str(chunk.get("chunk_hash") or chunk.get("text_hash") or "")
        if not chunk_hash or str(job.get("chunk_hash") or "") != chunk_hash:
            blockers.add("extraction_job_chunk_hash_mismatch")
            continue
        contract_hash = str(job.get("extraction_contract_hash") or "")
        if not contract_hash:
            blockers.add("extraction_job_contract_hash_missing")
            continue
        status = str(job.get("status") or "")
        if status in _EXTRACTION_TERMINAL:
            terminal_count += 1
            continue
        if status not in _EXTRACTION_RUNNABLE:
            blockers.add("extraction_job_not_runnable_or_terminal")
            continue
        entries.append(
            ManifestChunk(
                chunk_id=chunk_id,
                doc_id=str(chunk.get("doc_id") or ""),
                chunk_hash=chunk_hash,
                extraction_job_id=str(job.get("job_id") or ""),
                extraction_contract_hash=contract_hash,
                extraction_job_status=status,
            )
        )

    accounted = terminal_count + len(entries)
    if accounted != len(valid_chunks):
        blockers.add("valid_chunk_accounting_incomplete")
    if not entries:
        blockers.add("no_runnable_chunks")

    sorted_blockers = sorted(blockers)
    return ExtractionBurstManifest(
        schema_version=BURST_MANIFEST_VERSION,
        authority="executor_proposed_owner_ratifiable",
        completeness_source="cp1_d2a_durable_jobs_and_artifacts",
        corpus_id=corpus_id,
        disposition=disposition,
        source_batch_status=source_batch_status,
        active_ingest_items=max(0, int(active_ingest_items or 0)),
        document_count=len(documents),
        valid_chunk_count=len(valid_chunks),
        terminal_chunk_count=terminal_count,
        runnable_chunk_count=len(entries),
        document_pipeline_job_counts=dict(sorted(pipeline_counts.items())),
        extraction_job_counts=dict(sorted(extraction_counts.items())),
        dispatch_allowed=not sorted_blockers,
        blockers=sorted_blockers,
        chunks=entries,
    )


def durable_retry_safety_decision(
    *,
    job: dict[str, Any],
    extraction_row: dict[str, Any] | None,
) -> RetrySafetyDecision:
    """Expose the existing durable-job retry guard as a receipt contract."""

    protected_surfaces = [
        "parent_summaries",
        "document_summaries",
        "qdrant_vectors",
        "neo4j_promotions",
    ]
    if extraction_row and terminal_extraction_artifact_matches_job(job, extraction_row):
        return RetrySafetyDecision(
            action="skip_provider_call",
            reason="same_contract_terminal_extraction_artifact_exists",
            protected_artifact_policy="preserve_existing",
            protected_surfaces=protected_surfaces,
        )
    return RetrySafetyDecision(
        action="run_bounded_retry",
        reason="no_same_contract_terminal_extraction_artifact",
        protected_artifact_policy="preserve_existing",
        protected_surfaces=protected_surfaces,
    )


def build_extraction_burst_metrics(
    *,
    manifest_id: str,
    eligible_chunks: int,
    duration_seconds: float,
    lanes: list[LaneBurstMetrics],
    estimated_cost_only: bool,
) -> ExtractionBurstMetrics:
    """Aggregate first-class per-lane failure/fallback/time/cost metrics."""

    succeeded = sum(row.succeeded_chunks for row in lanes)
    failed = sum(row.failed_chunks for row in lanes)
    skipped = sum(row.skipped_chunks for row in lanes)
    request_batches = sum(row.request_batches for row in lanes)
    batch_failures = sum(row.batch_failures for row in lanes)
    fallback_events = sum(row.fallback_events for row in lanes)
    worker_seconds = sum(row.worker_seconds for row in lanes)
    billed_seconds = sum(row.billed_worker_seconds for row in lanes)
    cost = sum(row.compute_cost_usd for row in lanes)
    duration = max(0.0, float(duration_seconds or 0.0))
    return ExtractionBurstMetrics(
        schema_version=BURST_METRICS_VERSION,
        authority="measurement_only_no_production_stamp",
        manifest_id=manifest_id,
        eligible_chunks=eligible_chunks,
        succeeded_chunks=succeeded,
        failed_chunks=failed,
        skipped_chunks=skipped,
        duration_seconds=round(duration, 6),
        chunks_per_second=(round(succeeded / duration, 6) if duration else 0.0),
        worker_seconds=round(worker_seconds, 6),
        billed_worker_seconds=round(billed_seconds, 6),
        compute_cost_usd=round(cost, 8),
        estimated_cost_only=estimated_cost_only,
        cost_per_1k_chunks_usd=(
            round((cost / eligible_chunks) * 1000.0, 8) if eligible_chunks else None
        ),
        failure_rate=(round(failed / eligible_chunks, 6) if eligible_chunks else 0.0),
        fallback_rate=(
            round(fallback_events / request_batches, 6) if request_batches else 0.0
        ),
        request_batches=request_batches,
        batch_failures=batch_failures,
        fallback_events=fallback_events,
        lanes=lanes,
    )
