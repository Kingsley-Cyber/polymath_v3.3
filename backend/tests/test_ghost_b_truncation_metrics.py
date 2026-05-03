"""Focused tests for Ghost B health-metric aliases.

Validates the six targeted health fields surfaced in the extraction batch
report and rolled into per-document Mongo writes:

  - ghost_b_total_chunks
  - ghost_b_skipped_chunks
  - ghost_b_truncated_count
  - ghost_b_recovered_count
  - ghost_b_failed_count
  - ghost_b_truncated_rate

Truncation detection rides on `completion_tokens >= safe_max_tokens * 0.98`
in the per-call metric dicts, which is independently asserted.
"""

import pytest

from services import ghost_b
from services.ghost_b import (
    ExtractionTask,
    SchemaContext,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
    extract_entities,
    summarize_extraction_batch,
)


def _ok_content(chunk_id: str = "c1") -> str:
    return ghost_b.json.dumps(
        {
            "schema_version": "polymath.extract.v1",
            "chunk_id": chunk_id,
            "doc_id": "d1",
            "corpus_id": "corp1",
            "entities": [
                {
                    "canonical_name": "app",
                    "surface_form": "app",
                    "entity_type": "Product",
                    "confidence": 0.95,
                },
                {
                    "canonical_name": "ml kit",
                    "surface_form": "ML Kit",
                    "entity_type": "Product",
                    "confidence": 0.95,
                },
            ],
            "relations": [],
        }
    )


class _Response:
    def __init__(self, content: str, *, completion_tokens: int = 50):
        self._content = content
        self._completion = completion_tokens

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"content": self._content}}],
            "usage": {
                "total_tokens": 800 + self._completion,
                "prompt_tokens": 800,
                "completion_tokens": self._completion,
            },
        }


class _FakeAsyncClient:
    calls: list = []
    responses: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, json, headers):
        self.__class__.calls.append(json)
        return self.__class__.responses.pop(0)


def _schema() -> SchemaContext:
    return SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )


def test_summarize_extraction_batch_emits_health_aliases():
    """All six health aliases must appear with arithmetically consistent values."""
    call_metrics = [
        {
            "chunk_id": "c1",
            "completion_tokens": 100,
            "max_tokens": 4096,
            "recovery_mode": False,
            "truncated": False,
            "duration_seconds": 1.0,
            "total_tokens": 900,
            "prompt_tokens": 800,
        },
        {
            "chunk_id": "c2",
            "completion_tokens": 4096,
            "max_tokens": 4096,
            "recovery_mode": False,
            "truncated": True,
            "duration_seconds": 1.0,
            "total_tokens": 4900,
            "prompt_tokens": 800,
        },
        {
            "chunk_id": "c2",
            "completion_tokens": 1500,
            "max_tokens": 2048,
            "recovery_mode": True,
            "truncated": False,
            "duration_seconds": 1.0,
            "total_tokens": 2300,
            "prompt_tokens": 800,
        },
    ]
    metrics = summarize_extraction_batch(
        total_chunks=2,
        results=[],
        failures=[],
        call_metrics=call_metrics,
        models=["m"],
        metrics_context={"skipped_low_value_chunks": 7},
    )
    # Aliases present with the documented semantics
    assert metrics["ghost_b_total_chunks"] == 2 + 7
    assert metrics["ghost_b_skipped_chunks"] == 7
    assert metrics["ghost_b_truncated_count"] == 1
    assert metrics["ghost_b_recovered_count"] == 1
    assert metrics["ghost_b_failed_count"] == 0
    # Rate is a fraction of attempts, not chunks
    assert metrics["ghost_b_truncated_rate"] == round(1 / 3, 4)
    # Existing keys must not regress
    assert metrics["json_recovery_count"] == 1
    assert metrics["requested_chunks"] == 2


def test_summarize_extraction_batch_failed_count_reflects_failures():
    from services.ghost_b import ExtractionFailureItem

    failures = [
        ExtractionFailureItem(
            chunk_id="c1",
            doc_id="d1",
            corpus_id="corp1",
            model="m",
            lane=0,
            attempts=2,
            error_type="parse_error",
            error_message="bad json",
            retryable=True,
        ),
    ]
    metrics = summarize_extraction_batch(
        total_chunks=3,
        results=[],
        failures=failures,
        call_metrics=[],
        models=["m"],
    )
    assert metrics["ghost_b_failed_count"] == 1
    assert metrics["ghost_b_total_chunks"] == 3  # no skipped passed in
    assert metrics["ghost_b_skipped_chunks"] == 0
    assert metrics["ghost_b_truncated_count"] == 0
    assert metrics["ghost_b_recovered_count"] == 0


@pytest.mark.asyncio
async def test_extract_entities_records_truncation_when_completion_hits_cap(monkeypatch):
    """Live call path: when the provider returns completion_tokens at the cap,
    the per-call metric must carry truncated=True and the batch report must
    surface ghost_b_truncated_count >= 1."""

    # Two responses for two tasks. First saturates output cap. Second is normal.
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responses = [
        _Response(_ok_content("c1"), completion_tokens=4096),
        _Response(_ok_content("c2"), completion_tokens=80),
    ]
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", _FakeAsyncClient)

    report = await extract_entities(
        [
            ExtractionTask("c1", "d1", "corp1", "app uses ML Kit"),
            ExtractionTask("c2", "d1", "corp1", "app uses ML Kit again"),
        ],
        schema=_schema(),
        pool=[{"model": "m", "max_concurrent": 1, "extra_params": {}}],
        return_report=True,
        per_chunk_max_attempts=1,
    )

    assert len(report.results) == 2
    assert report.metrics["ghost_b_total_chunks"] == 2
    assert report.metrics["ghost_b_failed_count"] == 0
    # Exactly one call hit the output cap
    assert report.metrics["ghost_b_truncated_count"] == 1
    # Rate = 1 truncated / 2 attempts
    assert report.metrics["ghost_b_truncated_rate"] == round(1 / 2, 4)
