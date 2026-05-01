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
            document_title="Android ML Notes.md",
            heading_path=["TensorFlow Lite", "Object Detection"],
            chunk_kind="body",
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


def test_ingestion_config_defaults_to_local_only_graph_extraction():
    cfg = IngestionConfig()

    assert cfg.graph_extraction_engine == "local_gliner"
    assert cfg.local_graph_extraction_enabled is True
    assert cfg.llm_fallback_enabled is False
    assert cfg.llm_fallback_max_percent == 0


def test_markdown_context_wrapper_includes_doc_heading_and_strips_noise():
    task = ExtractionTask(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        document_title="Research Notes.md",
        heading_path=["Chapter 2", "Android ML Deployment"],
        chunk_kind="body",
        text=(
            "```python\nprint('not an entity')\n```\n"
            "| a | b | c | d |\n"
            "|---|---|---|---|\n"
            "TensorFlow Lite runs on Android."
        ),
    )

    formatted = lge.format_task_text_for_local_model(task, max_tokens=120)

    assert "Document: Research Notes.md" in formatted
    assert "Section: Chapter 2 > Android ML Deployment" in formatted
    assert "Chunk kind: body" in formatted
    assert "TensorFlow Lite runs on Android." in formatted
    assert "print('not an entity')" not in formatted
    assert "|---|" not in formatted
    assert "| a | b | c | d |" not in formatted


def test_worker_specs_tune_to_detected_gpu_names(monkeypatch):
    monkeypatch.setattr(lge, "_cuda_device_count", lambda: 2)
    monkeypatch.setattr(
        lge,
        "_cuda_device_names",
        lambda: ["NVIDIA GeForce RTX 4070", "NVIDIA GeForce RTX 3090"],
    )
    specs = lge._available_worker_specs(
        [
            lge.LocalWorkerSpec("cuda:0", "rtx_3090", 16, 2),
            lge.LocalWorkerSpec("cuda:1", "rtx_4070", 8, 1),
        ]
    )

    assert specs[0].name == "rtx_4070"
    assert specs[0].batch_size == 8
    assert specs[0].weight == 1
    assert specs[1].name == "rtx_3090"
    assert specs[1].batch_size == 16
    assert specs[1].weight == 2


def test_metadata_only_entities_are_filtered_from_local_results():
    task = ExtractionTask(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        document_title="Local Graph Smoke",
        heading_path=["Model Runtime"],
        chunk_kind="body",
        text="The mobile app stores prediction results in SQLite.",
    )
    result = lge._result_from_local_raw(
        {
            "entities": [
                {"text": "Document", "label": "Document", "score": 0.9},
                {"text": "Local Graph", "label": "Document", "score": 0.9},
                {"text": "Model Runtime", "label": "Document", "score": 0.9},
                {"text": "body", "label": "Document", "score": 0.9},
                {"text": "SQLite", "label": "Product", "score": 0.9},
            ],
            "relations": [],
        },
        task,
        schema=_schema(),
        schema_lens=None,
        text=lge.format_task_text_for_local_model(task, max_tokens=120),
    )

    names = {entity.canonical_name for entity in result.entities}
    assert names == {"SQLite"}


def test_gliner_run_path_extracts_relations_without_multitask_dependency():
    class RunModel:
        def __init__(self):
            self.calls = []

        def run(self, texts, labels, *, threshold, batch_size, **_kwargs):
            self.calls.append((texts, labels, threshold, batch_size))
            if labels and labels[0] == "Person":
                return [
                    [
                        {"text": "Brian Chesky", "label": "Person", "score": 0.95},
                        {"text": "Airbnb", "label": "Organization", "score": 0.93},
                    ]
                ]
            assert labels == [["Brian Chesky <> created_by", "Airbnb <> created_by"]]
            return [[{"text": "Airbnb", "label": "Brian Chesky <> created_by", "score": 0.88}]]

    adapter = object.__new__(lge.GlinerRelexAdapter)
    adapter.model_name = "fake-relex"
    adapter.device = "cpu"
    adapter.model = RunModel()

    raw = adapter.infer_batch(
        ["Brian Chesky founded Airbnb."],
        entity_labels=["Person", "Organization"],
        relation_labels=["created_by"],
    )

    assert raw[0]["entities"][0]["text"] == "Brian Chesky"
    assert raw[0]["relations"] == [
        {
            "source": "Brian Chesky",
            "relation": "created_by",
            "target": "Airbnb",
            "score": 0.88,
        }
    ]


def test_explicit_cue_relations_are_generated_without_llm():
    task = ExtractionTask(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        document_title="Android ML Notes.md",
        heading_path=["Deployment"],
        chunk_kind="body",
        text="The mobile app uses ML Kit. TensorFlow Lite runs on Android.",
    )

    result = lge._result_from_local_raw(
        {
            "entities": [
                {"text": "mobile app", "label": "Product", "score": 0.7},
                {"text": "ML Kit", "label": "Product", "score": 0.9},
                {"text": "TensorFlow Lite", "label": "Product", "score": 0.9},
                {"text": "Android", "label": "Product", "score": 0.8},
            ],
            "relations": [],
        },
        task,
        schema=_schema(),
        schema_lens=None,
        text=lge.format_task_text_for_local_model(task, max_tokens=120),
    )

    triples = {(rel.subject, rel.predicate, rel.object) for rel in result.relations}
    assert ("mobile app", "uses", "ML Kit") in triples
    assert ("TensorFlow Lite", "runs_on", "Android") in triples
    assert result.candidate_facts


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
    assert report.metrics["llm_graph_calls"] == 0
    assert report.metrics["local_graph_relation_chunks"] == 1


@pytest.mark.asyncio
async def test_entity_only_result_preserves_entities_without_inventing_relations(monkeypatch):
    class EntityOnlyAdapter(_FakeAdapter):
        def infer_batch(self, texts, *, entity_labels, relation_labels):
            return [
                {
                    "entities": [
                        {"text": "TensorFlow Lite", "label": "Product", "score": 0.91},
                        {"text": "Android", "label": "Product", "score": 0.88},
                    ],
                    "relations": [],
                }
                for _text in texts
            ]

    monkeypatch.setattr(lge, "_ADAPTER_FACTORY", EntityOnlyAdapter)
    cfg = IngestionConfig(graph_extraction_engine="local_gliner", llm_fallback_enabled=False)

    report = await lge.extract_entities_local_first(
        _tasks(1),
        config=cfg,
        schema=_schema(),
        llm_kwargs={"return_report": True},
    )

    assert isinstance(report, ExtractionBatchReport)
    assert len(report.results[0].entities) == 2
    assert report.results[0].relations == []
    assert report.results[0].candidate_facts == []
    assert report.metrics["local_graph_entity_only_chunks"] == 1
    assert report.metrics["local_graph_relation_chunks"] == 0
    assert report.metrics["llm_graph_calls"] == 0


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
    assert report.metrics["llm_graph_calls"] == 1
    assert sum(1 for result in report.results for rel in result.relations if rel.predicate == "uses") >= 1


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
    cfg = IngestionConfig(
        graph_extraction_engine="hybrid_local_first",
        llm_fallback_enabled=True,
    )

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


@pytest.mark.asyncio
async def test_local_only_missing_dependency_does_not_call_llm(monkeypatch):
    def missing_factory(_model_name: str, _device: str):
        raise lge.LocalGraphDependencyError("missing gliner")

    async def forbidden_llm(_tasks, **_kwargs):
        raise AssertionError("LLM fallback should not run in local_gliner mode")

    monkeypatch.setattr(lge, "_ADAPTER_FACTORY", missing_factory)
    cfg = IngestionConfig(
        graph_extraction_engine="local_gliner",
        llm_fallback_enabled=False,
    )

    report = await lge.extract_entities_local_first(
        _tasks(3),
        config=cfg,
        schema=_schema(),
        llm_extract_func=forbidden_llm,
        llm_kwargs={"return_report": True},
    )

    assert isinstance(report, ExtractionBatchReport)
    assert len(report.results) == 0
    assert len(report.failures) == 3
    assert report.metrics["graph_extraction_engine_used"] == "local_gliner_unavailable"
