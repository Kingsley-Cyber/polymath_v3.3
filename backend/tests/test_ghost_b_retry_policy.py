import httpx
import pytest

from services import ghost_b
from services.ghost_b import (
    ExtractionTask,
    SchemaContext,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
    extract_entities,
)


def _ok_content(chunk_id: str = "c1", *, relation: str | None = None, confidence=0.9) -> str:
    relations = []
    if relation:
        relations.append(
            {
                "subject": "app",
                "predicate": relation,
                "object": "ml kit",
                "object_kind": "entity",
                "confidence": confidence,
                "predicate_confidence": confidence,
                "extraction_confidence": confidence,
                "evidence_phrase": "app uses ML Kit",
            }
        )
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
            "relations": relations,
        }
    )


class _Response:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"content": self._content}}],
            "usage": {
                "total_tokens": 10,
                "prompt_tokens": 8,
                "completion_tokens": 2,
            },
        }


class _FakeAsyncClient:
    calls = []
    responses = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, json, headers):
        self.__class__.calls.append(json)
        item = self.__class__.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _http_error(status: int, text: str = "provider error") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://litellm.test/chat/completions")
    response = httpx.Response(status, request=request, text=text)
    return httpx.HTTPStatusError(text, request=request, response=response)


def _schema() -> SchemaContext:
    return SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )


@pytest.mark.asyncio
async def test_parse_error_triggers_exactly_one_compact_retry(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responses = [_Response("{"), _Response(_ok_content())]
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", _FakeAsyncClient)

    report = await extract_entities(
        [ExtractionTask("c1", "d1", "corp1", "text")],
        schema=_schema(),
        pool=[{"model": "m", "max_concurrent": 1, "extra_params": {}}],
        return_report=True,
        per_chunk_max_attempts=2,
    )

    assert len(report.results) == 1
    assert report.failures == []
    assert len(_FakeAsyncClient.calls) == 2
    assert _FakeAsyncClient.calls[1]["max_tokens"] <= 2048
    assert report.metrics["json_recovery_count"] == 1


@pytest.mark.asyncio
async def test_valid_related_to_does_not_retry(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responses = [_Response(_ok_content(relation="related_to"))]
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", _FakeAsyncClient)

    report = await extract_entities(
        [ExtractionTask("c1", "d1", "corp1", "app uses ML Kit")],
        schema=_schema(),
        pool=[{"model": "m", "max_concurrent": 1, "extra_params": {}}],
        return_report=True,
        per_chunk_max_attempts=2,
    )

    assert len(report.results) == 1
    assert len(_FakeAsyncClient.calls) == 1
    assert report.failures == []


@pytest.mark.asyncio
async def test_low_predicate_confidence_does_not_retry(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responses = [_Response(_ok_content(relation="uses", confidence=0.3))]
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", _FakeAsyncClient)

    report = await extract_entities(
        [ExtractionTask("c1", "d1", "corp1", "app uses ML Kit")],
        schema=_schema(),
        pool=[{"model": "m", "max_concurrent": 1, "extra_params": {}}],
        return_report=True,
        per_chunk_max_attempts=2,
    )

    assert len(report.results) == 1
    assert len(_FakeAsyncClient.calls) == 1
    assert report.failures == []


@pytest.mark.asyncio
async def test_rate_limit_obeys_per_chunk_retry_budget(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responses = [_http_error(429, "rate limited"), _http_error(429, "rate limited")]
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", _FakeAsyncClient)

    report = await extract_entities(
        [ExtractionTask("c1", "d1", "corp1", "text")],
        schema=_schema(),
        pool=[{"model": "m", "max_concurrent": 1, "extra_params": {}}],
        return_report=True,
        per_chunk_max_attempts=2,
        per_lane_cooldown_seconds=60,
    )

    assert len(report.results) == 0
    assert len(_FakeAsyncClient.calls) == 2
    assert report.failures[0].error_type == "rate_limited"
    assert report.failures[0].attempts == 2
    assert report.failures[0].retryable is True
    assert report.failures[0].retry_after is not None


@pytest.mark.asyncio
async def test_all_lanes_exhausted_marks_pending_chunks_retryable(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responses = [_http_error(401, "invalid api key")]
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", _FakeAsyncClient)

    tasks = [
        ExtractionTask("c1", "d1", "corp1", "text"),
        ExtractionTask("c2", "d1", "corp1", "text"),
        ExtractionTask("c3", "d1", "corp1", "text"),
    ]
    report = await extract_entities(
        tasks,
        schema=_schema(),
        pool=[{"model": "m", "max_concurrent": 1, "extra_params": {}}],
        return_report=True,
        per_chunk_max_attempts=2,
        per_lane_cooldown_seconds=60,
    )

    assert len(report.results) == 0
    assert len(report.failures) == 3
    assert {failure.error_type for failure in report.failures} == {"all_lanes_exhausted"}
    assert all(failure.retryable for failure in report.failures)
    assert all(failure.retry_after is not None for failure in report.failures)
    assert report.metrics["all_lanes_exhausted_count"] == 3
