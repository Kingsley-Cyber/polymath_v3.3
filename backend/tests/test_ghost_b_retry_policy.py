from __future__ import annotations

from collections import deque

import pytest

from services import ghost_b
from services.ghost_b import (
    ExtractionBatchReport,
    ExtractionTask,
    SchemaContext,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
    extract_entities,
)


class _FakeResponse:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "usage": {
                "total_tokens": 100,
                "prompt_tokens": 80,
                "completion_tokens": 20,
            },
            "choices": [{"message": {"content": self._content}}],
        }


class _FakeAsyncClient:
    responses = deque()
    payloads: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, _url, *, json, headers):
        self.__class__.payloads.append(json)
        item = self.__class__.responses.popleft()
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(str(item))


def _ctx() -> SchemaContext:
    return SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )


def _task() -> ExtractionTask:
    return ExtractionTask(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        text="The app uses ML Kit.",
    )


def _json_response(*, predicate: str = "uses", predicate_confidence=0.95) -> str:
    return f"""
    {{
      "schema_version": "polymath.extract.v1",
      "chunk_id": "c1",
      "doc_id": "d1",
      "corpus_id": "corp1",
      "entities": [
        {{"canonical_name": "app", "surface_form": "app", "entity_type": "Product", "confidence": 0.9}},
        {{"canonical_name": "ml kit", "surface_form": "ML Kit", "entity_type": "Product", "confidence": 0.9}}
      ],
      "candidate_facts": [],
      "relations": [
        {{
          "subject": "app",
          "predicate": "{predicate}",
          "object": "ml kit",
          "object_kind": "entity",
          "confidence": 0.9,
          "predicate_confidence": {predicate_confidence},
          "extraction_confidence": 0.9,
          "evidence_phrase": "app uses ML Kit"
        }}
      ]
    }}
    """


async def _run_with_responses(monkeypatch, responses) -> ExtractionBatchReport:
    _FakeAsyncClient.responses = deque(responses)
    _FakeAsyncClient.payloads = []
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", _FakeAsyncClient)
    report = await extract_entities(
        [_task()],
        schema=_ctx(),
        pool=[{"model": "fake/model", "max_concurrent": 1, "extra_params": {}}],
        return_report=True,
    )
    assert isinstance(report, ExtractionBatchReport)
    return report


@pytest.mark.asyncio
async def test_parse_error_triggers_one_compact_recovery_retry(monkeypatch):
    report = await _run_with_responses(
        monkeypatch,
        ["{ malformed json", _json_response()],
    )

    assert len(_FakeAsyncClient.payloads) == 2
    assert report.metrics["attempt_count"] == 2
    assert report.metrics["json_recovery_count"] == 1
    recovery_payload = _FakeAsyncClient.payloads[1]
    assert recovery_payload["max_tokens"] <= 2048
    assert "RECOVERY MODE" in recovery_payload["messages"][0]["content"]
    assert "HARD LIMIT: output at most 5 entities and at most 5 relations" in recovery_payload["messages"][1]["content"]


@pytest.mark.asyncio
async def test_valid_related_to_json_does_not_retry(monkeypatch):
    report = await _run_with_responses(
        monkeypatch,
        [_json_response(predicate="related_to")],
    )

    assert len(_FakeAsyncClient.payloads) == 1
    assert report.metrics["attempt_count"] == 1
    assert report.metrics["json_recovery_count"] == 0
    assert report.results[0].relations[0].predicate == "related_to"


@pytest.mark.asyncio
async def test_low_predicate_confidence_does_not_retry(monkeypatch):
    report = await _run_with_responses(
        monkeypatch,
        [_json_response(predicate="uses", predicate_confidence=0.42)],
    )

    assert len(_FakeAsyncClient.payloads) == 1
    assert report.metrics["attempt_count"] == 1
    assert report.metrics["json_recovery_count"] == 0
    relation = report.results[0].relations[0]
    assert relation.predicate == "related_to"
    assert "low_predicate_confidence" in (relation.validation_status or "")


@pytest.mark.asyncio
async def test_domain_range_warning_does_not_retry(monkeypatch):
    report = await _run_with_responses(
        monkeypatch,
        [_json_response(predicate="works_for")],
    )

    assert len(_FakeAsyncClient.payloads) == 1
    assert report.metrics["attempt_count"] == 1
    assert report.metrics["json_recovery_count"] == 0
    assert report.results[0].domain_range_warn_count == 1


@pytest.mark.asyncio
async def test_non_parse_provider_error_does_not_enter_recovery_or_retry(monkeypatch):
    report = await _run_with_responses(
        monkeypatch,
        [RuntimeError("provider timeout")],
    )

    assert len(_FakeAsyncClient.payloads) == 1
    assert report.metrics["attempt_count"] == 1
    assert report.metrics["json_recovery_count"] == 0
    assert len(report.results) == 0
    assert len(report.failures) == 1
    assert report.failures[0].attempts == 1
    assert report.failures[0].error_type == "RuntimeError"
    assert "RECOVERY MODE" not in _FakeAsyncClient.payloads[0]["messages"][0]["content"]
