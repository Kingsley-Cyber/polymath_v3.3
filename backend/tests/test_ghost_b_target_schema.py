import pytest

from services import ghost_b
from services.ghost_b import (
    PRIMARY_EXTRACTION_MODEL,
    REPAIR_EXTRACTION_MODEL,
    ExtractionTask,
    SchemaContext,
    TARGET_SCHEMA_VERSION,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
    _parse,
    _parse_with_repair_items,
    extract_entities,
)


def _schema() -> SchemaContext:
    return SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )


def _target_payload(*, confidence=0.92, family="Operational", include_object=True):
    entities = [
        {
            "name": "app",
            "type": "Product",
            "aliases": [],
            "description": "The app described in the sentence.",
        }
    ]
    if include_object:
        entities.append(
            {
                "name": "ML Kit",
                "type": "Product",
                "aliases": [],
                "description": "The kit used by the app.",
            }
        )
    return ghost_b.json.dumps(
        {
            "schema_version": TARGET_SCHEMA_VERSION,
            "chunk_id": "c1",
            "doc_id": "d1",
            "corpus_id": "corp1",
            "entities": entities,
            "relations": [
                {
                    "subject": "app",
                    "predicate": "uses",
                    "predicate_family": family,
                    "object": "ML Kit",
                    "qualifier": "",
                    "confidence": confidence,
                    "source_sentence": "The app uses ML Kit.",
                }
            ],
            "objects": [],
        }
    )


def test_target_schema_parses_to_legacy_graph_structs():
    parsed = _parse(
        _target_payload(),
        ExtractionTask("c1", "d1", "corp1", "The app uses ML Kit."),
        threshold=0.5,
        schema=_schema(),
    )

    assert parsed is not None
    assert parsed.schema_version == TARGET_SCHEMA_VERSION
    assert [entity.canonical_name for entity in parsed.entities] == ["app", "ml kit"]
    assert parsed.entities[1].surface_form == "ML Kit"
    assert parsed.entities[1].description == "The kit used by the app."
    relation = parsed.relations[0]
    assert relation.subject == "app"
    assert relation.object == "ml kit"
    assert relation.predicate == "uses"
    assert relation.predicate_family == "Operational"
    assert relation.source_sentence == "The app uses ML Kit."


def test_target_schema_recovers_truncated_json_prefix():
    raw = _target_payload().rsplit("]", 1)[0] + "]   "

    parsed = _parse(
        raw,
        ExtractionTask("c1", "d1", "corp1", "The app uses ML Kit."),
        threshold=0.5,
        schema=_schema(),
    )

    assert parsed is not None
    assert parsed.schema_version == TARGET_SCHEMA_VERSION
    assert parsed.entities
    relation = parsed.relations[0]
    assert relation.evidence_phrase == "The app uses ML Kit."
    assert relation.extraction_model == PRIMARY_EXTRACTION_MODEL
    assert relation.repaired is False


def test_target_schema_validation_flags_repair_triggers_without_inference():
    parsed, repairs = _parse_with_repair_items(
        _target_payload(confidence=0.42, family="WeakAssociation", include_object=False),
        ExtractionTask("c1", "d1", "corp1", "The app uses ML Kit."),
        threshold=0.5,
        schema=_schema(),
    )

    assert parsed is not None
    assert [entity.canonical_name for entity in parsed.entities] == ["app"]
    assert "ml kit" not in {entity.canonical_name for entity in parsed.entities}
    assert len(repairs) == 1
    assert set(repairs[0].reasons) >= {
        "confidence_below_0.70",
        "predicate_family_WeakAssociation",
        "object_missing_from_entities",
    }
    assert "object_missing_from_entities" in (
        parsed.relations[0].validation_status or ""
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


@pytest.mark.asyncio
async def test_gemma_triple_repair_gets_failed_triple_not_full_chunk(monkeypatch):
    full_chunk = "SECRET FULL CHUNK SHOULD NOT APPEAR. The app uses ML Kit."
    repaired = ghost_b.json.dumps(
        {
            "relation": {
                "subject": "app",
                "predicate": "uses",
                "predicate_family": "Operational",
                "object": "ML Kit",
                "qualifier": "",
                "confidence": 0.93,
                "source_sentence": "The app uses ML Kit.",
            }
        }
    )
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responses = [
        _Response(_target_payload(confidence=0.41)),
        _Response(repaired),
    ]
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", _FakeAsyncClient)

    report = await extract_entities(
        [ExtractionTask("c1", "d1", "corp1", full_chunk)],
        schema=_schema(),
        pool=[{"model": "lfm2-extract", "max_concurrent": 1, "extra_params": {}}],
        repair_pool=[{"model": "gemma4-e4b", "max_concurrent": 1, "extra_params": {}}],
        return_report=True,
        per_chunk_max_attempts=1,
    )

    assert len(report.results) == 1
    relation = report.results[0].relations[0]
    assert relation.repaired is True
    assert relation.extraction_model == REPAIR_EXTRACTION_MODEL
    assert relation.confidence == 0.93

    assert len(_FakeAsyncClient.calls) == 2
    repair_call = _FakeAsyncClient.calls[1]
    repair_system = repair_call["messages"][0]["content"]
    repair_user = repair_call["messages"][1]["content"]
    assert "failed_triple" in repair_user
    assert "entity_names" in repair_user
    assert "The app uses ML Kit." in repair_user
    assert "SECRET FULL CHUNK SHOULD NOT APPEAR" not in repair_user
    assert "relation repair specialist" in repair_system
    assert "LFM2-1.2B-Extract" not in repair_system
    assert repair_system.startswith("<|think|>")
    assert "polymath.extract.v2" in repair_system
    assert repair_call["temperature"] == 1.0
    assert repair_call["top_p"] == 0.95
    assert repair_call["top_k"] == 64
    assert "extra_body" not in repair_call


def test_gemma_repair_parser_ignores_thought_channel():
    original = ghost_b.RelationItem(
        subject="app",
        predicate="uses",
        object="ML Kit",
        object_kind="entity",
        confidence=0.41,
        source_sentence="The app uses ML Kit.",
    )
    raw = (
        "<|channel>thought\nThe candidate is directly supported.<channel|>\n"
        + ghost_b.json.dumps(
            {
                "relation": {
                    "subject": "app",
                    "predicate": "uses",
                    "predicate_family": "Operational",
                    "object": "ML Kit",
                    "qualifier": "",
                    "confidence": 0.93,
                    "source_sentence": "The app uses ML Kit.",
                }
            }
        )
    )

    repaired = ghost_b._parse_repaired_relation(
        raw,
        original=original,
        entity_names={"app", "ml kit"},
        schema=_schema(),
    )

    assert repaired is not None
    assert repaired.repaired is True
    assert repaired.extraction_model == REPAIR_EXTRACTION_MODEL
    assert repaired.confidence == 0.93


@pytest.mark.asyncio
async def test_deferred_triple_repair_queues_candidate_without_gemma(monkeypatch):
    full_chunk = "SECRET FULL CHUNK SHOULD NOT APPEAR. The app uses ML Kit."
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responses = [_Response(_target_payload(confidence=0.41))]
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", _FakeAsyncClient)

    report = await extract_entities(
        [ExtractionTask("c1", "d1", "corp1", full_chunk)],
        schema=_schema(),
        pool=[{"model": "lfm2-extract", "max_concurrent": 1, "extra_params": {}}],
        repair_pool=[{"model": "gemma4-e4b", "max_concurrent": 1, "extra_params": {}}],
        return_report=True,
        per_chunk_max_attempts=1,
        defer_triple_repair=True,
    )

    assert len(_FakeAsyncClient.calls) == 1
    assert len(report.results) == 1
    assert report.results[0].relations == []
    assert len(report.relation_repairs) == 1
    candidate = report.relation_repairs[0]
    assert candidate.source_sentence == "The app uses ML Kit."
    assert "confidence_below_0.70" in candidate.reasons
    assert "app" in candidate.entity_names
    assert "ml kit" in candidate.entity_names
    assert report.metrics["relation_repair_queued_count"] == 1


@pytest.mark.asyncio
async def test_primary_extraction_uses_strict_json_schema_response_format(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responses = [_Response(_target_payload())]
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", _FakeAsyncClient)

    report = await extract_entities(
        [ExtractionTask("c1", "d1", "corp1", "The app uses ML Kit.")],
        schema=_schema(),
        pool=[{"model": "lfm2-extract", "max_concurrent": 1, "extra_params": {}}],
        return_report=True,
        per_chunk_max_attempts=1,
    )

    assert len(report.results) == 1
    call = _FakeAsyncClient.calls[0]
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True
    schema = call["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["schema_version"]["enum"] == [TARGET_SCHEMA_VERSION]
    assert schema["additionalProperties"] is False
    assert "Return data as a JSON object with this exact schema" not in call["messages"][0]["content"]


@pytest.mark.asyncio
async def test_parse_recovery_with_repair_pool_marks_gemma_relations(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responses = [_Response("{"), _Response(_target_payload())]
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", _FakeAsyncClient)

    report = await extract_entities(
        [ExtractionTask("c1", "d1", "corp1", "The app uses ML Kit.")],
        schema=_schema(),
        pool=[{"model": "lfm2-extract", "max_concurrent": 1, "extra_params": {}}],
        repair_pool=[{"model": "gemma4-e4b", "max_concurrent": 1, "extra_params": {}}],
        return_report=True,
        per_chunk_max_attempts=2,
    )

    assert len(report.results) == 1
    relation = report.results[0].relations[0]
    assert relation.repaired is True
    assert relation.extraction_model == REPAIR_EXTRACTION_MODEL
    assert report.metrics["json_recovery_count"] == 1
