from __future__ import annotations

import pytest
from pydantic import ValidationError

from models.extraction_artifact import CandidateExtractionArtifact
from services.ghost_b import EntityItem, ExtractionResult, RelationItem
from services.ingestion.extraction_artifacts import (
    adapt_extraction_failure,
    adapt_extraction_result,
)
from services.ingestion.extraction_burst import (
    CorpusDisposition,
    LaneBurstMetrics,
    build_extraction_burst_manifest,
    build_extraction_burst_metrics,
    durable_retry_safety_decision,
)
from services.ingestion.extraction_jobs import (
    terminal_extraction_artifact_matches_job,
)
from services.ingestion.extraction_parity import compare_engine_artifacts


def _result(chunk_id: str, text: str, *, relation: bool = True) -> ExtractionResult:
    relations = []
    if relation:
        relations = [
            RelationItem(
                subject="alpha",
                predicate="uses",
                object="beta",
                object_kind="entity",
                confidence=0.8,
                evidence_phrase=text,
                relation_cue="uses" if "uses" in text else "",
                validation_status="accepted",
            )
        ]
    return ExtractionResult(
        schema_version="polymath.extract.v1",
        chunk_id=chunk_id,
        doc_id="doc-1",
        corpus_id="corpus-1",
        text=text,
        entities=[
            EntityItem(
                canonical_name="alpha",
                surface_form="alpha",
                entity_type="concept",
                confidence=0.9,
            ),
            EntityItem(
                canonical_name="beta",
                surface_form="beta",
                entity_type="concept",
                confidence=0.85,
            ),
        ],
        relations=relations,
        model="model",
        provider="provider",
        attempts=1,
    )


def _artifact(
    engine: str,
    chunk_id: str,
    text: str,
    *,
    relation: bool = True,
    fallback_from: list[str] | None = None,
):
    return adapt_extraction_result(
        _result(chunk_id, text, relation=relation),
        engine=engine,
        engine_runtime_version=f"{engine}-runtime.1",
        source_wire_contract_version=f"{engine}-wire.1",
        source_contract_hash="sha256:contract",
        model_id=f"{engine}-model",
        fallback_from=fallback_from,
    )


def _failure(engine: str, chunk_id: str, text: str):
    return adapt_extraction_failure(
        {
            "chunk_id": chunk_id,
            "doc_id": "doc-1",
            "corpus_id": "corpus-1",
            "model": f"{engine}-model",
            "lane": 1,
            "attempts": 1,
            "error_type": "TimeoutError",
            "error_message": "timeout",
        },
        engine=engine,
        engine_runtime_version=f"{engine}-runtime.1",
        source_wire_contract_version=f"{engine}-wire.1",
        source_contract_hash="sha256:contract",
        source_text=text,
    )


def test_parity_reports_like_for_like_measures_without_adjudication() -> None:
    artifacts = [
        _artifact("legacy_local", "c1", "alpha uses beta."),
        _artifact("runpod_flash", "c1", "alpha uses beta.", fallback_from=["a"]),
        _failure("legacy_local", "c2", "alpha differs from beta."),
        _artifact("runpod_flash", "c2", "alpha differs from beta.", relation=False),
    ]

    report = compare_engine_artifacts(
        artifacts,
        expected_chunk_ids=["c1", "c2"],
        engines=["legacy_local", "runpod_flash"],
    )

    assert report.authority == "measurement_only_no_adjudication"
    assert report.production_readiness == "not_evaluated"
    assert report.expected_chunks == 2
    by_engine = {row.engine: row for row in report.engines}
    assert by_engine["legacy_local"].failure_rate == 0.5
    assert by_engine["runpod_flash"].fallback_rate == 0.5
    assert by_engine["runpod_flash"].fallback_events == 1
    pair = report.pairwise[0]
    assert pair.paired_candidate_chunks == 1
    assert pair.unpaired_or_noncandidate_chunks == 1
    assert pair.entity_jaccard_mean == 1.0
    dumped = report.model_dump()
    assert "winner" not in dumped
    assert "verdict" not in dumped
    assert report.report_id.startswith("engine-parity:")


def test_parity_fails_closed_on_nonidentical_input_or_duplicate_rows() -> None:
    left = _artifact("legacy_local", "c1", "alpha uses beta.")
    right = _artifact("runpod_flash", "c1", "alpha uses beta.")
    mismatched = CandidateExtractionArtifact.model_validate(
        {**right.model_dump(), "source_text_sha256": "sha256:different"}
    )
    with pytest.raises(ValueError, match="identical source text hashes"):
        compare_engine_artifacts(
            [left, mismatched],
            expected_chunk_ids=["c1"],
            engines=["legacy_local", "runpod_flash"],
        )
    with pytest.raises(ValueError, match="duplicate engine artifact"):
        compare_engine_artifacts(
            [left, left, right],
            expected_chunk_ids=["c1"],
            engines=["legacy_local", "runpod_flash"],
        )


def _disposition(**overrides) -> CorpusDisposition:
    values = {
        "matrix_version": "disposition_matrix.v1",
        "corpus_id": "corpus-1",
        "mode": "re_extract_only",
        "owner_status": "not_required",
        "rationale": "existing chunks are sound",
    }
    values.update(overrides)
    return CorpusDisposition(**values)


def _manifest_inputs():
    return {
        "corpus_id": "corpus-1",
        "disposition": _disposition(),
        "source_batch_status": "not_applicable_existing_corpus",
        "active_ingest_items": 0,
        "documents": [
            {
                "doc_id": "doc-1",
                "metadata_extracted": True,
                "chunking_complete": True,
            }
        ],
        "valid_chunks": [
            {"chunk_id": "c1", "doc_id": "doc-1", "chunk_hash": "h1"},
            {"chunk_id": "c2", "doc_id": "doc-1", "chunk_hash": "h2"},
        ],
        "document_pipeline_jobs": [],
        "extraction_jobs": [
            {
                "job_id": "j1",
                "chunk_id": "c1",
                "chunk_hash": "h1",
                "extraction_contract_hash": "x1",
                "status": "queued",
            },
            {
                "job_id": "j2",
                "chunk_id": "c2",
                "chunk_hash": "h2",
                "extraction_contract_hash": "x1",
                "status": "succeeded",
            },
        ],
    }


def test_chunk_barrier_reuses_durable_jobs_and_builds_full_runnable_manifest() -> None:
    manifest = build_extraction_burst_manifest(**_manifest_inputs())

    assert manifest.completeness_source == "cp1_d2a_durable_jobs_and_artifacts"
    assert manifest.dispatch_allowed is True
    assert manifest.blockers == []
    assert manifest.valid_chunk_count == 2
    assert manifest.terminal_chunk_count == 1
    assert manifest.runnable_chunk_count == 1
    assert [row.chunk_id for row in manifest.chunks] == ["c1"]
    assert manifest.manifest_id.startswith("extraction-burst:")


@pytest.mark.parametrize(
    ("change", "blocker"),
    [
        ({"active_ingest_items": 1}, "active_ingest_items"),
        (
            {
                "document_pipeline_jobs": [
                    {"kind": "chunk_document", "status": "running"}
                ]
            },
            "durable_document_pipeline_jobs_incomplete",
        ),
        (
            {
                "documents": [
                    {
                        "doc_id": "doc-1",
                        "metadata_extracted": False,
                        "chunking_complete": True,
                    }
                ]
            },
            "document_metadata_incomplete",
        ),
        (
            {"disposition": _disposition(mode="projection_only")},
            "projection_only_disallows_extraction",
        ),
        (
            {"disposition": _disposition(owner_status="pending")},
            "disposition_owner_approval_pending",
        ),
    ],
)
def test_chunk_barrier_blocks_each_adjacent_incomplete_state(change, blocker) -> None:
    values = _manifest_inputs()
    values.update(change)

    manifest = build_extraction_burst_manifest(**values)

    assert manifest.dispatch_allowed is False
    assert blocker in manifest.blockers


def test_chunk_barrier_fails_closed_on_job_identity_drift() -> None:
    values = _manifest_inputs()
    values["extraction_jobs"][0]["chunk_hash"] = "wrong"

    manifest = build_extraction_burst_manifest(**values)

    assert manifest.dispatch_allowed is False
    assert "extraction_job_chunk_hash_mismatch" in manifest.blockers
    assert "valid_chunk_accounting_incomplete" in manifest.blockers


def test_retry_safety_reuses_exact_terminal_artifact_decision() -> None:
    job = {
        "chunk_id": "c1",
        "extraction_contract_hash": "sha256:contract",
    }
    exact = {
        "chunk_id": "c1",
        "extraction_contract_hash": "sha256:contract",
        "status": "ok",
    }
    drifted = {**exact, "extraction_contract_hash": "sha256:new"}

    assert terminal_extraction_artifact_matches_job(job, exact) is True
    assert terminal_extraction_artifact_matches_job(job, drifted) is False
    skip = durable_retry_safety_decision(job=job, extraction_row=exact)
    retry = durable_retry_safety_decision(job=job, extraction_row=drifted)
    assert skip.action == "skip_provider_call"
    assert retry.action == "run_bounded_retry"
    assert skip.protected_artifact_policy == "preserve_existing"
    assert "qdrant_vectors" in skip.protected_surfaces
    assert "neo4j_promotions" in retry.protected_surfaces


def test_burst_metrics_surface_per_lane_failure_fallback_time_and_cost() -> None:
    lanes = [
        LaneBurstMetrics(
            lane_id="account-a",
            engine="runpod_flash",
            engine_runtime_version="image@sha256:a",
            model_id="model-a",
            request_batches=4,
            batch_failures=1,
            fallback_events=1,
            unique_chunks_assigned=10,
            succeeded_chunks=8,
            failed_chunks=1,
            skipped_chunks=1,
            worker_seconds=20.0,
            billed_worker_seconds=24.0,
            compute_cost_usd=0.2,
        )
    ]

    metrics = build_extraction_burst_metrics(
        manifest_id="extraction-burst:abc",
        eligible_chunks=10,
        duration_seconds=5.0,
        lanes=lanes,
        estimated_cost_only=True,
    )

    assert metrics.authority == "measurement_only_no_production_stamp"
    assert metrics.chunks_per_second == 1.6
    assert metrics.failure_rate == 0.1
    assert metrics.fallback_rate == 0.25
    assert metrics.cost_per_1k_chunks_usd == 20.0
    assert metrics.worker_seconds == 20.0
    assert metrics.billed_worker_seconds == 24.0
    with pytest.raises(ValidationError, match="terminal burst outcome"):
        build_extraction_burst_metrics(
            manifest_id="extraction-burst:abc",
            eligible_chunks=11,
            duration_seconds=5.0,
            lanes=lanes,
            estimated_cost_only=True,
        )
