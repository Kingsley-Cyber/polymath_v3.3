import pytest

from models.schemas import IngestionConfig
from services import local_graph_extractor as lge
from services.ghost_b import (
    EntityItem,
    ExtractionBatchReport,
    ExtractionResult,
    ExtractionTask,
    RelationItem,
    SchemaContext,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
)


def _schema() -> SchemaContext:
    return SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )


def _tasks(n: int) -> list[ExtractionTask]:
    return [
        ExtractionTask(
            chunk_id=f"c{i}",
            doc_id="d1",
            corpus_id="corp1",
            text="The app uses ML Kit for object detection.",
        )
        for i in range(n)
    ]


class _FakeAdapter:
    def __init__(self, _model_name: str, device: str):
        self.device = device

    def infer_batch(self, texts, *, entity_labels, relation_labels):
        return [
            {
                "entities": [
                    {"text": "app", "label": "Product", "score": 0.9},
                    {"text": "ML Kit", "label": "Product", "score": 0.9},
                ],
                "relations": [
                    {
                        "subject": "app",
                        "predicate": "uses",
                        "object": "ML Kit",
                        "score": 0.9,
                        "evidence": text,
                    }
                ],
            }
            for text in texts
        ]


@pytest.fixture(autouse=True)
def _clear_local_adapter(monkeypatch):
    lge._ADAPTER_CACHE.clear()
    monkeypatch.setattr(lge, "_ADAPTER_FACTORY", _FakeAdapter)
    monkeypatch.setattr(lge, "_cuda_device_count", lambda: None)


@pytest.mark.asyncio
async def test_local_extractor_maps_to_ghost_b_result_shape():
    cfg = IngestionConfig(
        graph_extraction_engine="local_gliner",
        llm_fallback_enabled=False,
        local_workers=[{"device": "cuda:0", "name": "rtx_3090", "batch_size": 2, "weight": 1}],
    )

    report = await lge.extract_entities_local_first(
        _tasks(1),
        config=cfg,
        schema=_schema(),
        llm_kwargs={"return_report": True},
    )

    assert isinstance(report, ExtractionBatchReport)
    assert len(report.results) == 1
    assert report.results[0].entities
    assert report.results[0].candidate_facts
    assert report.results[0].relations[0].predicate == "uses"
    assert report.metrics["graph_extraction_engine_used"] == "local_gliner"
    assert report.metrics["local_graph_chunks_processed"] == 1


@pytest.mark.asyncio
async def test_scheduler_assigns_chunks_by_worker_weight():
    cfg = IngestionConfig(
        graph_extraction_engine="local_gliner",
        llm_fallback_enabled=False,
        local_workers=[
            {"device": "cuda:0", "name": "rtx_3090", "batch_size": 8, "weight": 2},
            {"device": "cuda:1", "name": "rtx_4070", "batch_size": 8, "weight": 1},
        ],
    )

    report = await lge.extract_entities_local_first(
        _tasks(6),
        config=cfg,
        schema=_schema(),
        llm_kwargs={"return_report": True},
    )

    assert isinstance(report, ExtractionBatchReport)
    assert report.metrics["per_gpu_chunks_processed"]["rtx_3090"] == 4
    assert report.metrics["per_gpu_chunks_processed"]["rtx_4070"] == 2


@pytest.mark.asyncio
async def test_oom_halves_batch_and_retries_once(monkeypatch):
    class OomThenOkAdapter(_FakeAdapter):
        raised = False

        def infer_batch(self, texts, *, entity_labels, relation_labels):
            if len(texts) > 1 and not self.__class__.raised:
                self.__class__.raised = True
                raise RuntimeError("CUDA out of memory")
            return super().infer_batch(
                texts,
                entity_labels=entity_labels,
                relation_labels=relation_labels,
            )

    monkeypatch.setattr(lge, "_ADAPTER_FACTORY", OomThenOkAdapter)
    cfg = IngestionConfig(
        graph_extraction_engine="local_gliner",
        llm_fallback_enabled=False,
        local_workers=[{"device": "cuda:0", "name": "rtx_3090", "batch_size": 4, "weight": 1}],
    )

    report = await lge.extract_entities_local_first(
        _tasks(4),
        config=cfg,
        schema=_schema(),
        llm_kwargs={"return_report": True},
    )

    assert isinstance(report, ExtractionBatchReport)
    assert len(report.results) == 4
    assert report.metrics["per_gpu_oom_count"]["rtx_3090"] == 1
    assert report.metrics["adaptive_batch_size_current"]["rtx_3090"] == 2


@pytest.mark.asyncio
async def test_llm_fallback_cap_is_respected(monkeypatch):
    class WeakAdapter(_FakeAdapter):
        def infer_batch(self, texts, *, entity_labels, relation_labels):
            rows = super().infer_batch(
                texts,
                entity_labels=entity_labels,
                relation_labels=relation_labels,
            )
            for row in rows:
                row["relations"][0]["predicate"] = "related_to"
                row["relations"][0]["predicate_confidence"] = 0.4
            return rows

    async def fake_llm(tasks, **_kwargs):
        results = [
            ExtractionResult(
                schema_version="polymath.extract.test",
                chunk_id=task.chunk_id,
                doc_id=task.doc_id,
                corpus_id=task.corpus_id,
                entities=[
                    EntityItem("app", "app", "Product", 0.95),
                    EntityItem("ml kit", "ML Kit", "Product", 0.95),
                ],
                relations=[
                    RelationItem(
                        "app",
                        "uses",
                        "ml kit",
                        "entity",
                        0.95,
                        predicate_confidence=0.95,
                        extraction_confidence=0.95,
                    )
                ],
            )
            for task in tasks
        ]
        return ExtractionBatchReport(
            results=results,
            failures=[],
            metrics={"total_tokens": 12, "prompt_tokens": 10, "completion_tokens": 2},
        )

    monkeypatch.setattr(lge, "_ADAPTER_FACTORY", WeakAdapter)
    cfg = IngestionConfig(
        graph_extraction_engine="hybrid_local_first",
        llm_fallback_enabled=True,
        llm_fallback_max_percent=0.25,
        local_workers=[{"device": "cuda:0", "name": "rtx_3090", "batch_size": 4, "weight": 1}],
    )

    report = await lge.extract_entities_local_first(
        _tasks(4),
        config=cfg,
        schema=_schema(),
        llm_extract_func=fake_llm,
        llm_kwargs={"return_report": True},
    )

    assert isinstance(report, ExtractionBatchReport)
    assert report.metrics["llm_fallback_chunks"] == 1
    assert sum(1 for result in report.results for rel in result.relations if rel.predicate == "uses") == 1


@pytest.mark.asyncio
async def test_hybrid_falls_back_to_llm_when_local_dependency_missing(monkeypatch):
    def missing_factory(_model_name: str, _device: str):
        raise lge.LocalGraphDependencyError("missing gliner")

    async def fake_llm(tasks, **_kwargs):
        return ExtractionBatchReport(
            results=[
                ExtractionResult(
                    schema_version="polymath.extract.test",
                    chunk_id=task.chunk_id,
                    doc_id=task.doc_id,
                    corpus_id=task.corpus_id,
                )
                for task in tasks
            ],
            failures=[],
            metrics={"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0},
        )

    monkeypatch.setattr(lge, "_ADAPTER_FACTORY", missing_factory)
    cfg = IngestionConfig(graph_extraction_engine="hybrid_local_first")

    report = await lge.extract_entities_local_first(
        _tasks(3),
        config=cfg,
        schema=_schema(),
        llm_extract_func=fake_llm,
        llm_kwargs={"return_report": True},
    )

    assert isinstance(report, ExtractionBatchReport)
    assert len(report.results) == 3
    assert report.metrics["graph_extraction_engine_used"] == "llm_fallback_local_unavailable"
