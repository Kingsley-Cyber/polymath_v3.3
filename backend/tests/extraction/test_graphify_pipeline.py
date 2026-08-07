from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from models.graphify_contracts import PipelineStage
from services.extraction.gliner2_cpu_provider import EntityPrediction
from services.extraction.graphify_pipeline import run_graphify_pipeline


def _matches(row: dict, query: dict) -> bool:
    return all(row.get(key) == value for key, value in query.items())


class _Collection:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def find_one(self, query, projection=None):
        del projection
        return next((deepcopy(row) for row in self.rows if _matches(row, query)), None)

    async def update_one(self, query, update, upsert=False):
        row = next((item for item in self.rows if _matches(item, query)), None)
        if row is None and upsert:
            row = dict(query)
            self.rows.append(row)
            for key, value in (update.get("$setOnInsert") or {}).items():
                row[key] = deepcopy(value)
        if row is not None:
            for key, value in (update.get("$set") or {}).items():
                row[key] = deepcopy(value)
        return SimpleNamespace(modified_count=1 if row is not None else 0)

    async def insert_one(self, row):
        self.rows.append(deepcopy(row))
        return SimpleNamespace(inserted_id=len(self.rows))

    async def count_documents(self, query):
        return sum(_matches(row, query) for row in self.rows)


class _Db(dict):
    def __missing__(self, key):
        value = _Collection()
        self[key] = value
        return value


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    def predict_entities(self, texts, *, batch_size, threshold):
        del batch_size, threshold
        self.calls += 1
        output = []
        for text in texts:
            rows = []
            for surface, entity_type in (("Graphify", "software"), ("MongoDB", "software")):
                start = text.find(surface)
                if start >= 0:
                    rows.append(EntityPrediction(
                        text=surface, entity_type=entity_type,
                        start=start, end=start + len(surface), confidence=0.99,
                    ))
            output.append(rows)
        return output


class _NamedProvider:
    def __init__(self, specs) -> None:
        self.specs = tuple(specs)
        self.calls = 0

    def predict_entities(self, texts, *, batch_size, threshold):
        del batch_size, threshold
        self.calls += 1
        output = []
        for text in texts:
            rows = []
            for surface, entity_type in self.specs:
                start = text.find(surface)
                if start >= 0:
                    rows.append(EntityPrediction(
                        text=surface, entity_type=entity_type,
                        start=start, end=start + len(surface), confidence=0.99,
                    ))
            output.append(rows)
        return output


def _children():
    return [SimpleNamespace(chunk_id="chunk-1", text="Graphify uses MongoDB.")]


@pytest.mark.asyncio
async def test_pipeline_resumes_without_second_model_call_and_prevents_duplicates() -> None:
    db = _Db()
    provider = _Provider()
    first = await run_graphify_pipeline(
        db=db, corpus_id="corpus", doc_id="doc",
        text="Graphify uses MongoDB.", children=_children(), provider=provider,
    )
    second = await run_graphify_pipeline(
        db=db, corpus_id="corpus", doc_id="doc",
        text="Graphify uses MongoDB.", children=_children(), provider=provider,
    )
    assert provider.calls == 1
    assert first.identity_digest == second.identity_digest
    assert second.resumed_stages == (
        PipelineStage.DISCOVERED.value,
        PipelineStage.NORMALIZED.value,
        PipelineStage.SURVEY_COMPLETE.value,
        PipelineStage.ENTITY_CENSUS_COMPLETE.value,
        PipelineStage.ENTITY_REDUCTION_COMPLETE.value,
        PipelineStage.MENTION_COMPLETION_COMPLETE.value,
        PipelineStage.RELATION_ELIGIBILITY_COMPLETE.value,
        PipelineStage.OPENIE_EXTRACTION_COMPLETE.value,
        PipelineStage.OPENIE_ARGUMENT_ADAPTATION_COMPLETE.value,
        PipelineStage.OPENIE_PROPOSITION_REDUCTION_COMPLETE.value,
        PipelineStage.OPENIE_PREDICATE_COMPILATION_COMPLETE.value,
        PipelineStage.OPENIE_ASSERTION_ASSEMBLY_COMPLETE.value,
        PipelineStage.RELATION_COMPILATION_COMPLETE.value,
        PipelineStage.ASSERTION_VALIDATION_COMPLETE.value,
    )
    assert len(db["graphify_stage_artifacts"].rows) == 14
    assert len(db["graphify_stage_receipts"].rows) == 14
    relation = first.report.results[0].relations[0]
    assert (relation.subject, relation.predicate, relation.object) == (
        "Graphify", "uses", "MongoDB",
    )


@pytest.mark.asyncio
async def test_pipeline_failure_is_closed_and_resume_continues_after_checkpoint() -> None:
    db = _Db()
    provider = _Provider()
    with pytest.raises(RuntimeError, match="injected Graphify failure"):
        await run_graphify_pipeline(
            db=db, corpus_id="corpus", doc_id="doc",
            text="Graphify uses MongoDB.", children=_children(), provider=provider,
            fail_after_stage=PipelineStage.ENTITY_CENSUS_COMPLETE,
        )
    assert provider.calls == 1
    assert not any(
        row.get("stage") == PipelineStage.RELATION_COMPILATION_COMPLETE.value
        for row in db["graphify_stage_receipts"].rows
    )
    resumed = await run_graphify_pipeline(
        db=db, corpus_id="corpus", doc_id="doc",
        text="Graphify uses MongoDB.", children=_children(), provider=provider,
    )
    assert provider.calls == 1
    assert resumed.resumed_stages == (
        PipelineStage.DISCOVERED.value,
        PipelineStage.NORMALIZED.value,
        PipelineStage.SURVEY_COMPLETE.value,
        PipelineStage.ENTITY_CENSUS_COMPLETE.value,
    )
    assert resumed.report.results[0].relations


@pytest.mark.asyncio
async def test_provider_failure_records_failure_and_no_downstream_artifact() -> None:
    db = _Db()

    class BrokenProvider(_Provider):
        def predict_entities(self, texts, *, batch_size, threshold):
            del texts, batch_size, threshold
            raise RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await run_graphify_pipeline(
            db=db, corpus_id="corpus", doc_id="doc",
            text="Graphify uses MongoDB.", children=_children(), provider=BrokenProvider(),
        )
    census_receipt = next(
        row for row in db["graphify_stage_receipts"].rows
        if row["stage"] == PipelineStage.ENTITY_CENSUS_COMPLETE.value
    )
    assert census_receipt["status"] == "failed"
    assert not any(
        row.get("stage") == PipelineStage.ENTITY_REDUCTION_COMPLETE.value
        for row in db["graphify_stage_artifacts"].rows
    )
    recovered = await run_graphify_pipeline(
        db=db, corpus_id="corpus", doc_id="doc",
        text="Graphify uses MongoDB.", children=_children(), provider=_Provider(),
    )
    census_receipt = next(
        row for row in db["graphify_stage_receipts"].rows
        if row["stage"] == PipelineStage.ENTITY_CENSUS_COMPLETE.value
    )
    assert census_receipt["attempt_no"] == 2
    assert census_receipt["receipt"]["retry_count"] == 1
    assert recovered.resumed_stages == (
        PipelineStage.DISCOVERED.value,
        PipelineStage.NORMALIZED.value,
        PipelineStage.SURVEY_COMPLETE.value,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,specs,predicate",
    [
        (
            "Maya did not say Harbor uses Qdrant.",
            (("Maya", "person"), ("Harbor", "software"), ("Qdrant", "software")),
            "uses",
        ),
        (
            # Subject must carry real name evidence: the endpoint policy
            # refuses to mint bare common nouns ("the possibility"), which
            # would make this containment check vacuous.
            "The Orion Module is not part of the current production architecture.",
            (("Orion Module", "software"), ("current production architecture", "concept")),
            "part_of",
        ),
    ],
)
async def test_pipeline_never_promotes_negated_or_negated_attributed_fact(
    text, specs, predicate,
) -> None:
    db = _Db()
    await run_graphify_pipeline(
        db=db, corpus_id="corpus", doc_id="doc", text=text,
        children=[SimpleNamespace(chunk_id="chunk-1", text=text)],
        provider=_NamedProvider(specs),
    )
    relation_payload = next(
        row["payload"] for row in db["graphify_stage_artifacts"].rows
        if row["stage"] == PipelineStage.RELATION_COMPILATION_COMPLETE.value
    )
    matching = [
        row for row in relation_payload["mapped_relations"]
        if row.get("canonical_candidate") == predicate
    ]
    assert matching
    assert all(row["terminal_state"] != "accepted" for row in matching)
    assert any(row["terminal_state"] == "qualified" for row in matching)


@pytest.mark.asyncio
async def test_pipeline_never_creates_wildcard_relation_endpoint() -> None:
    text = "Graphify uses *."
    db = _Db()
    await run_graphify_pipeline(
        db=db, corpus_id="corpus", doc_id="doc", text=text,
        children=[SimpleNamespace(chunk_id="chunk-1", text=text)],
        provider=_NamedProvider((("Graphify", "software"),)),
    )
    relation_payload = next(
        row["payload"] for row in db["graphify_stage_artifacts"].rows
        if row["stage"] == PipelineStage.RELATION_COMPILATION_COMPLETE.value
    )
    assert not relation_payload["endpoint_entities"]
    assert not relation_payload["endpoint_mentions"]
    assert all(
        row["terminal_state"] == "rejected"
        for row in relation_payload["mapped_relations"]
        if row.get("canonical_candidate")
    )


@pytest.mark.asyncio
async def test_relation_local_endpoint_requires_an_accepted_assertion_to_reach_entity_output() -> None:
    text = "Graphify does not use Temporary Coordination."
    output = await run_graphify_pipeline(
        db=_Db(), corpus_id="corpus", doc_id="doc", text=text,
        children=[SimpleNamespace(chunk_id="chunk-1", text=text)],
        provider=_NamedProvider((("Graphify", "software"),)),
    )
    assert "Temporary Coordination" not in {
        entity.canonical_name for entity in output.report.results[0].entities
    }


@pytest.mark.asyncio
async def test_relation_local_endpoint_is_retained_when_its_assertion_is_accepted() -> None:
    text = "Graphify uses Atlas API."
    output = await run_graphify_pipeline(
        db=_Db(), corpus_id="corpus", doc_id="doc", text=text,
        children=[SimpleNamespace(chunk_id="chunk-1", text=text)],
        provider=_NamedProvider((("Graphify", "software"),)),
    )
    result = output.report.results[0]
    assert "Atlas API" in {entity.canonical_name for entity in result.entities}
    assert any(
        (relation.subject, relation.predicate, relation.object)
        == ("Graphify", "uses", "Atlas API")
        for relation in result.relations
    )


@pytest.mark.asyncio
async def test_reducer_supported_entity_survives_without_an_accepted_relation() -> None:
    text = "Graphify does not use Temporary Coordination."
    output = await run_graphify_pipeline(
        db=_Db(), corpus_id="corpus", doc_id="doc", text=text,
        children=[SimpleNamespace(chunk_id="chunk-1", text=text)],
        provider=_NamedProvider((
            ("Graphify", "software"),
            ("Temporary Coordination", "method"),
        )),
    )
    assert "Temporary Coordination" in {
        entity.canonical_name for entity in output.report.results[0].entities
    }
