"""T-HOOK-1 — temporal CAPTURE fields in the extraction wire contract (v3).

Offline contract tests: the Runpod Flash adapter must accept BOTH v2
responses (no ``time_expressions`` field — never required, no error) and v3
responses (field mapped through verbatim), and the shared persistence seam
must write ``temporal_captures`` + ``temporal_capture_version`` onto
``ghost_b_extractions`` rows when present. Capture-only: nothing here
normalizes or resolves a time expression.
"""

from __future__ import annotations

import pytest

from models.schemas import RunpodFlashExtractionSettings
from services.ghost_b import ExtractionResult, ExtractionTask, SchemaContext
from services import runpod_flash_extraction as runpod_flash
from services.ingestion.extraction_jobs import _persist_extraction_rows


def _config(**overrides) -> RunpodFlashExtractionSettings:
    values = {
        "enabled": True,
        "endpoint_id": "endpoint-test",
        "request_batch_size": 2,
        "request_concurrency": 2,
        "poll_interval_seconds": 0.25,
    }
    values.update(overrides)
    return RunpodFlashExtractionSettings(**values)


def _task(chunk_id: str, text: str) -> ExtractionTask:
    return ExtractionTask(
        chunk_id=chunk_id,
        doc_id="doc-1",
        corpus_id="corpus-1",
        text=text,
    )


def _schema() -> SchemaContext:
    return SchemaContext(
        entity_schema=["Concept"],
        relation_schema=["related_to"],
        strict="soft",
    )


def test_accepted_contract_versions_is_the_known_compatible_set() -> None:
    assert runpod_flash.RUNPOD_CONTRACT_VERSION == "polymath.runpod_gliner_relex.v2"
    assert runpod_flash.RUNPOD_CONTRACT_VERSION_V3 == "polymath.runpod_gliner_relex.v3"
    assert runpod_flash.ACCEPTED_CONTRACT_VERSIONS == frozenset(
        {
            "polymath.runpod_gliner_relex.v2",
            "polymath.runpod_gliner_relex.v3",
        }
    )
    assert "polymath.runpod_gliner_relex.v1" not in runpod_flash.ACCEPTED_CONTRACT_VERSIONS


@pytest.mark.asyncio
async def test_v2_response_without_time_expressions_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_submit(*_args, request, **_kwargs):
        # The request keeps the unchanged v2 stamp (wire request shape frozen).
        assert request["contract_version"] == runpod_flash.RUNPOD_CONTRACT_VERSION
        return {
            "contract_version": runpod_flash.RUNPOD_CONTRACT_VERSION,
            "results": [
                {
                    "chunk_id": request["tasks"][0]["chunk_id"],
                    "entities": [],
                    "relations": [],
                }
            ],
            "metrics": {"entities_emitted": 0, "relations_emitted": 0},
            "_runpod_job": {},
        }

    monkeypatch.setattr(runpod_flash, "_submit_and_wait", fake_submit)
    report = await runpod_flash.extract_entities(
        [_task("chunk-v2", "A plain source-backed sentence.")],
        schema=_schema(),
        runpod_config=_config(),
        runpod_api_key="test-secret",
        return_report=True,
    )

    assert not report.failures
    assert len(report.results) == 1
    result = report.results[0]
    # Absent on the wire -> empty defaults, never an error.
    assert result.temporal_captures == []
    assert result.temporal_capture_version is None


@pytest.mark.asyncio
async def test_v3_response_maps_time_expressions_through_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "The framework was published in March 2024."
    capture = {
        "text": "March 2024",
        "char_start": text.index("March 2024"),
        "char_end": text.index("March 2024") + len("March 2024"),
        "detector": "regex",
        "role_candidates": ["publication"],
    }

    async def fake_submit(*_args, request, **_kwargs):
        return {
            "contract_version": runpod_flash.RUNPOD_CONTRACT_VERSION_V3,
            "results": [
                {
                    "chunk_id": request["tasks"][0]["chunk_id"],
                    "entities": [],
                    "relations": [],
                    "time_expressions": [capture],
                    "time_expressions_truncated": False,
                }
            ],
            "metrics": {
                "entities_emitted": 0,
                "relations_emitted": 0,
                "time_expressions_emitted": 1,
            },
            "_runpod_job": {},
        }

    monkeypatch.setattr(runpod_flash, "_submit_and_wait", fake_submit)
    report = await runpod_flash.extract_entities(
        [_task("chunk-v3", text)],
        schema=_schema(),
        runpod_config=_config(),
        runpod_api_key="test-secret",
        return_report=True,
    )

    assert not report.failures
    result = report.results[0]
    assert result.temporal_captures == [capture]
    assert result.temporal_capture_version == runpod_flash.RUNPOD_CONTRACT_VERSION_V3


@pytest.mark.asyncio
async def test_v3_empty_capture_list_is_mapped_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_submit(*_args, request, **_kwargs):
        return {
            "contract_version": runpod_flash.RUNPOD_CONTRACT_VERSION_V3,
            "results": [
                {
                    "chunk_id": request["tasks"][0]["chunk_id"],
                    "entities": [],
                    "relations": [],
                    "time_expressions": [],
                    "time_expressions_truncated": False,
                }
            ],
            "metrics": {},
            "_runpod_job": {},
        }

    monkeypatch.setattr(runpod_flash, "_submit_and_wait", fake_submit)
    report = await runpod_flash.extract_entities(
        [_task("chunk-v3-empty", "No temporal surface forms here at all.")],
        schema=_schema(),
        runpod_config=_config(),
        runpod_api_key="test-secret",
        return_report=True,
    )

    assert not report.failures
    result = report.results[0]
    assert result.temporal_captures == []
    # An explicit (empty) v3 field still records which contract captured it.
    assert result.temporal_capture_version == runpod_flash.RUNPOD_CONTRACT_VERSION_V3


# --------------------------------------------------------------------------
# Persistence seam — fake-collection idiom from tests/test_storage_lifecycle.py


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    async def to_list(self, length=None):
        return list(self._rows)


class _FakeCollection:
    def __init__(self):
        self.bulk_ops = []

    async def find_one(self, query, projection=None):
        return None

    def find(self, query, projection=None):
        return _FakeCursor([])

    async def bulk_write(self, ops, ordered=False):
        self.bulk_ops.extend(ops)
        self.ordered = ordered
        return None


class _FakeDb:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, _FakeCollection())


@pytest.mark.asyncio
async def test_persistence_seam_writes_temporal_capture_fields() -> None:
    db = _FakeDb()
    capture = {
        "text": "Q1 2025",
        "char_start": 10,
        "char_end": 17,
        "detector": "regex",
        "role_candidates": [],
    }
    with_captures = ExtractionResult(
        schema_version="ghost_b_extraction.v1",
        chunk_id="chunk-with",
        doc_id="doc-1",
        corpus_id="corpus-1",
        temporal_captures=[capture],
        temporal_capture_version="polymath.runpod_gliner_relex.v3",
    )
    without_captures = ExtractionResult(
        schema_version="ghost_b_extraction.v1",
        chunk_id="chunk-without",
        doc_id="doc-1",
        corpus_id="corpus-1",
    )

    await _persist_extraction_rows(
        db,
        doc_id="doc-1",
        corpus_id="corpus-1",
        results=[with_captures, without_captures],
        failures=[],
    )

    ops = db["ghost_b_extractions"].bulk_ops
    rows = {op._doc["chunk_id"]: op._doc for op in ops}
    assert set(rows) == {"chunk-with", "chunk-without"}
    assert rows["chunk-with"]["temporal_captures"] == [capture]
    assert (
        rows["chunk-with"]["temporal_capture_version"]
        == "polymath.runpod_gliner_relex.v3"
    )
    # Pre-v3 providers persist the additive defaults, never an error.
    assert rows["chunk-without"]["temporal_captures"] == []
    assert rows["chunk-without"]["temporal_capture_version"] is None
    assert rows["chunk-with"]["status"] == "ok"


def test_staging_rehydration_round_trips_temporal_capture_fields() -> None:
    """Backfill/resume paths re-stash rehydrated staging rows via ReplaceOne;
    if rehydration dropped the additive fields, a later backfill pass would
    silently erase persisted temporal captures."""

    from services.ingestion import graph_backfill, worker

    capture = {
        "text": "2024-03-05",
        "char_start": 0,
        "char_end": 10,
        "detector": "regex",
        "role_candidates": ["publication"],
    }
    row = {
        "schema_version": "ghost_b_extraction.v1",
        "chunk_id": "chunk-r",
        "doc_id": "doc-1",
        "corpus_id": "corpus-1",
        "text": "2024-03-05 marks the publication.",
        "entities": [],
        "relations": [],
        "facts": [],
        "temporal_captures": [capture],
        "temporal_capture_version": "polymath.runpod_gliner_relex.v3",
    }

    for rehydrate in (
        graph_backfill._rehydrate_ghost_b_staging,
        worker._rehydrate_ghost_b_staging,
    ):
        results = rehydrate([dict(row)])
        assert len(results) == 1
        assert results[0].temporal_captures == [capture]
        assert (
            results[0].temporal_capture_version
            == "polymath.runpod_gliner_relex.v3"
        )
