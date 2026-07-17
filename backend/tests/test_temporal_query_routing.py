from __future__ import annotations

import os
import sys
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("LITELLM_MASTER_KEY", "test-litellm-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from models.schemas import RetrievalResult, RetrievalTier, SourceChunk
from services.conversation import conversation_service
from services.retriever import temporal
from services.retriever.evidence_plan import EvidenceLane
from services.retriever.funnel_a import FunnelA
from services.retriever.hydrate import attach_parent_temporal_metadata


class _EmptyNlp:
    meta = {"version": temporal.SPACY_MODEL_VERSION}

    def __call__(self, _text: str):
        return SimpleNamespace(ents=())


class _EntityNlp:
    meta = {"version": temporal.SPACY_MODEL_VERSION}

    def __init__(self, spans: list[tuple[str, str]]):
        self._spans = spans

    def __call__(self, text: str):
        entities = []
        for surface, label in self._spans:
            start = text.index(surface)
            entities.append(
                SimpleNamespace(
                    start_char=start,
                    end_char=start + len(surface),
                    label_=label,
                )
            )
        return SimpleNamespace(ents=entities)


@pytest.fixture(autouse=True)
def _clear_detector_cache():
    temporal.detect_temporal_intent.cache_clear()
    temporal._load_temporal_nlp.cache_clear()
    yield
    temporal.detect_temporal_intent.cache_clear()
    temporal._load_temporal_nlp.cache_clear()


def _chunk(
    chunk_id: str,
    *,
    score: float = 0.5,
    source_tier: str = "summary",
    expression: str | None = None,
    role: str = "event_time",
    temporal_class: str = "event",
) -> SourceChunk:
    metadata = {}
    if expression:
        metadata = temporal.metadata_with_temporal_carrier(
            {},
            {
                "temporal_class": temporal_class,
                "time_expressions": [{"text": expression, "role": role}],
            },
        )
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"parent-{chunk_id}",
        doc_id=f"doc-{chunk_id}",
        corpus_id="corpus-1",
        text=f"evidence {chunk_id}",
        score=score,
        source_tier=source_tier,
        metadata=metadata,
    )


def test_temporal_query_routing_defaults_off(monkeypatch):
    monkeypatch.delenv("TEMPORAL_QUERY_ROUTING_ENABLED", raising=False)
    resolved = Settings(_env_file=None)
    assert resolved.TEMPORAL_QUERY_ROUTING_ENABLED is False


def test_temporal_query_routing_can_be_enabled(monkeypatch):
    monkeypatch.setenv("TEMPORAL_QUERY_ROUTING_ENABLED", "true")
    resolved = Settings(_env_file=None)
    assert resolved.TEMPORAL_QUERY_ROUTING_ENABLED is True


@pytest.mark.parametrize(
    ("query", "family", "surface"),
    [
        ("What changed on 2024-03-04?", "iso_date", "2024-03-04"),
        ("Compare 2001 through 2004.", "year_range", "2001 through 2004"),
        (
            "What happened in 2018 drought summer?",
            "year_event_period",
            "2018 drought summer",
        ),
        ("What changed in winter 1911?", "season_year", "winter 1911"),
        ("What changed in late-2019?", "qualified_year", "late-2019"),
        ("What shipped in Q3 2020?", "quarter", "Q3 2020"),
        ("What happened in June 2006?", "month_year", "June 2006"),
        ("What changed in release v3.2.1?", "version", "v3.2.1"),
        ("What happened in 1929?", "year", "1929"),
    ],
)
def test_qualified_runtime_regex_family_golden_parity(
    monkeypatch, query, family, surface
):
    monkeypatch.setattr(temporal, "_load_temporal_nlp", lambda: _EmptyNlp())
    intent = temporal.detect_temporal_intent(query)

    assert temporal.QUALIFIED_TEMPORAL_PATTERN_VERSION == (
        "runpod_flash_extractor.runtime.v1"
    )
    assert any(
        expression.family == family and expression.text == surface
        for expression in intent.expressions
    )


def test_version_requires_locked_release_or_version_cue(monkeypatch):
    monkeypatch.setattr(temporal, "_load_temporal_nlp", lambda: _EmptyNlp())
    assert not temporal.detect_temporal_intent("The ratio was 3.2 to one.").active
    temporal.detect_temporal_intent.cache_clear()
    assert temporal.detect_temporal_intent("Release 3.2 changed it.").active


@pytest.mark.parametrize(
    "query,surface",
    [
        ("How did noir of the 1940s change?", "the 1940s"),
        ("What changed more than 100 years ago?", "more than 100 years ago"),
    ],
)
def test_pinned_spacy_fallback_covers_non_regex_temporal_surfaces(
    monkeypatch, query, surface
):
    monkeypatch.setattr(
        temporal,
        "_load_temporal_nlp",
        lambda: _EntityNlp([(surface, "DATE")]),
    )
    intent = temporal.detect_temporal_intent(query)
    assert intent.active
    assert any(item.text == surface for item in intent.expressions)
    assert "spacy" in intent.detector_sources


def test_temporal_carrier_normalizes_existing_summary_payload():
    metadata = temporal.metadata_with_temporal_carrier(
        {"existing": True},
        {
            "temporal_class": "Versioned",
            "time_expressions": [
                {"text": "October 1988", "role": "publication_time"},
                {"text": ""},
                "bad-row",
            ],
        },
    )
    assert metadata["existing"] is True
    assert metadata["temporal"] == {
        "temporal_class": "versioned",
        "time_expressions": [{"text": "October 1988", "role": "publication_time"}],
    }


def test_class_only_candidate_cannot_substitute_for_requested_date(monkeypatch):
    monkeypatch.setattr(temporal, "_load_temporal_nlp", lambda: _EmptyNlp())
    intent = temporal.detect_temporal_intent("What happened in 1929?")
    wrong_date = _chunk("wrong", expression="2004", temporal_class="event")
    details = temporal.temporal_match_details(wrong_date, intent)
    assert details["class_match"] is True
    assert details["matched"] is False


def test_qualified_range_matches_verbatim_component_without_date_parsing(monkeypatch):
    monkeypatch.setattr(temporal, "_load_temporal_nlp", lambda: _EmptyNlp())
    intent = temporal.detect_temporal_intent(
        "What is the revision timeline from October 1988 through 2001?"
    )
    component = _chunk(
        "component",
        expression="2001",
        role="revision_time",
        temporal_class="versioned",
    )
    assert temporal.temporal_match_details(component, intent)["matched"] is True


def test_temporal_reserve_prefers_relevant_graph_anchored_exact_match(monkeypatch):
    monkeypatch.setattr(temporal, "_load_temporal_nlp", lambda: _EmptyNlp())
    intent = temporal.detect_temporal_intent("What happened in 2004?")
    selected = [_chunk("base-1", score=0.9), _chunk("base-2", score=0.8)]
    vector_match = _chunk("vector", expression="2004", score=0.85)
    graph_match = _chunk(
        "graph",
        expression="2004",
        score=0.55,
        source_tier="graph_mode_a",
    )

    output, diagnostics = temporal.reserve_temporal_candidates(
        selected,
        [*selected, vector_match, graph_match],
        intent=intent,
        max_candidates=2,
        tier=RetrievalTier.qdrant_mongo_graph,
    )

    assert [chunk.chunk_id for chunk in output] == ["base-1", "graph"]
    assert diagnostics["reserved"] is True
    assert diagnostics["graph_preferred"] is True


def test_temporal_reserve_never_replaces_protected_evidence(monkeypatch):
    monkeypatch.setattr(temporal, "_load_temporal_nlp", lambda: _EmptyNlp())
    intent = temporal.detect_temporal_intent("What happened in 2004?")
    selected = [_chunk("protected", score=0.9)]
    exact = _chunk("temporal", expression="2004", score=0.8)
    output, diagnostics = temporal.reserve_temporal_candidates(
        selected,
        [*selected, exact],
        intent=intent,
        max_candidates=1,
        tier=RetrievalTier.qdrant_mongo,
        protected_keys={temporal.candidate_key(selected[0])},
    )
    assert output == selected
    assert diagnostics["reason"] == "all_selected_candidates_protected"


class _Cursor:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    async def to_list(self, length=None):
        return self.rows


class _ReadOnlyCollection:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.query = None
        self.projection = None

    def find(self, query, projection):
        self.query = query
        self.projection = projection
        return _Cursor(self.rows)

    def __getattr__(self, name: str):
        if name in {"update_one", "update_many", "insert_one", "delete_one"}:
            raise AssertionError(f"write attempted: {name}")
        raise AttributeError(name)


@pytest.mark.asyncio
async def test_bounded_parent_temporal_hydration_is_read_only(monkeypatch):
    collection = _ReadOnlyCollection(
        [
            {
                "parent_id": "parent-child",
                "doc_id": "doc-child",
                "corpus_id": "corpus-1",
                "temporal_class": "event",
                "time_expressions": [{"text": "2004", "role": "event_time"}],
            }
        ]
    )
    monkeypatch.setattr(
        "services.retriever.hydrate.get_settings",
        lambda: SimpleNamespace(TEMPORAL_QUERY_ROUTING_ENABLED=True),
    )
    monkeypatch.setattr(
        conversation_service,
        "_db",
        {"parent_chunks": collection},
    )

    output = await attach_parent_temporal_metadata(
        [_chunk("child", source_tier="tier_b")], ["corpus-1"]
    )

    clauses = collection.query.get("$and") or [collection.query]
    bounded_clause = next(clause for clause in clauses if "parent_id" in clause)
    assert bounded_clause["parent_id"] == {"$in": ["parent-child"]}
    assert bounded_clause["corpus_id"] == {"$in": ["corpus-1"]}
    assert output[0].metadata["temporal"]["time_expressions"][0]["text"] == "2004"


class _Point:
    id = "point-1"
    score = 0.87

    def __init__(self, payload: dict):
        self.payload = payload


class _QdrantClient:
    def __init__(self, payload: dict):
        self.payload = payload

    async def query_points(self, **_kwargs):
        return SimpleNamespace(points=[_Point(self.payload)])


@pytest.mark.asyncio
async def test_funnel_a_preserves_existing_qdrant_temporal_payload(monkeypatch):
    funnel_a_module = importlib.import_module("services.retriever.funnel_a")
    qdrant_writer_module = importlib.import_module("services.storage.qdrant_writer")
    qdrant_writer_module._COLLECTION_LAYOUT_CACHE["temporal-test"] = (False, False)
    monkeypatch.setattr(
        funnel_a_module,
        "get_settings",
        lambda: SimpleNamespace(TEMPORAL_QUERY_ROUTING_ENABLED=True),
    )
    funnel = FunnelA.__new__(FunnelA)
    funnel.client = _QdrantClient(
        {
            "chunk_id": "parent-1_summary",
            "parent_id": "parent-1",
            "doc_id": "doc-1",
            "corpus_id": "corpus-1",
            "chunk_text": "Timeline summary",
            "source_tier": "summary",
            "temporal_class": "versioned",
            "time_expressions": [{"text": "October 1988", "role": "publication_time"}],
        }
    )

    chunks = await funnel._search_collection("temporal-test", [0.1], None, 1)
    assert chunks[0].metadata["temporal"]["temporal_class"] == "versioned"
    assert chunks[0].metadata["temporal"]["time_expressions"][0]["text"] == (
        "October 1988"
    )


def test_dated_relationship_support_keeps_original_temporal_context(monkeypatch):
    from services import chat_orchestrator

    monkeypatch.setattr(
        chat_orchestrator.settings,
        "TEMPORAL_QUERY_ROUTING_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(temporal, "_load_temporal_nlp", lambda: _EmptyNlp())
    lane = EvidenceLane(
        name="side-a",
        label="lighthouse design",
        concept_key="lighthouse_design",
        aliases=(),
        search_terms=("lighthouse", "design"),
        query="How did lighthouse design change?",
    )
    variants = chat_orchestrator._evidence_support_query_variants(
        lane,
        "How did lighthouse design change in 2004?",
    )
    assert len(variants) == 3
    assert "2004" in variants[-1]


@pytest.mark.asyncio
async def test_legacy_cache_key_separates_temporal_flag_versions(monkeypatch):
    import services.retriever as retriever_module

    service = retriever_module.RetrieverOrchestrator()
    captured: list[tuple] = []

    async def artifact_epoch(_corpus_ids):
        return "epoch"

    async def uncached(**kwargs):
        return RetrievalResult(
            chunks=[_chunk("result")],
            requested_tier=kwargs["retrieval_tier"],
            effective_tier=kwargs["retrieval_tier"],
        )

    async def no_repair(result, _kwargs):
        return result

    def capture_hash(*parts):
        captured.append(parts)
        return repr(parts)

    monkeypatch.setattr(service, "_corpus_artifact_epoch", artifact_epoch)
    monkeypatch.setattr(service, "_retrieve_uncached", uncached)
    monkeypatch.setattr(service, "_repair_cross_corpus_missing_concepts", no_repair)
    monkeypatch.setattr(retriever_module, "hash_key", capture_hash)
    retriever_module._RETRIEVAL_CACHE.clear()
    monkeypatch.setattr(
        retriever_module.settings,
        "TEMPORAL_QUERY_ROUTING_ENABLED",
        False,
        raising=False,
    )
    kwargs = {
        "query": "What happened in 2004?",
        "corpus_ids": ["corpus-1"],
        "retrieval_tier": RetrievalTier.qdrant_only,
    }
    await service.retrieve(**kwargs)
    off_key = captured[-1]
    monkeypatch.setattr(
        retriever_module.settings,
        "TEMPORAL_QUERY_ROUTING_ENABLED",
        True,
        raising=False,
    )
    await service.retrieve(**kwargs)
    on_key = captured[-1]

    assert off_key != on_key
    assert off_key[-2:] == (False, temporal.TEMPORAL_ROUTING_VERSION)
    assert on_key[-2:] == (True, temporal.TEMPORAL_ROUTING_VERSION)
