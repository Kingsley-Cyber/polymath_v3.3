from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.ghost_a import SummaryResult
from services.ghost_b import EntityItem, ExtractionResult, RelationItem
from services.graph import analytics
from services.graph.entity_quality import (
    ENTITY_QUALITY_VERSION,
    LABEL_CLAIM_LIKE,
    LABEL_CLEAN,
    LABEL_CODE_LIKE,
    LABEL_GENERIC_ROLE,
    LABEL_JOINED_LIST,
    LABEL_TITLE,
    classify_entity_label,
)
from services.graph.neo4j_writer import write_document_graph


def test_document_titles_can_be_long_without_becoming_claim_noise():
    quality = classify_entity_label(
        "Fitting a Unidimensional Model to Multidimensional Item Response Data",
        "Document",
    )
    assert quality.label_quality == LABEL_TITLE
    assert quality.eligible_for_synthesis is True
    assert quality.eligible_for_topic_label is False


def test_long_non_document_sentence_becomes_claim_like():
    quality = classify_entity_label(
        "intuition that performance in anything has a few impossibly clumsy persons",
        "Concept",
    )
    assert quality.label_quality == LABEL_CLAIM_LIKE
    assert quality.eligible_for_topic_label is False
    assert quality.eligible_for_synthesis is False


@pytest.mark.parametrize("label", ["young woman", "speaker", "users"])
def test_generic_roles_are_not_topic_labels(label):
    quality = classify_entity_label(label, "Concept")
    assert quality.label_quality == LABEL_GENERIC_ROLE
    assert quality.eligible_for_topic_label is False


@pytest.mark.parametrize(
    ("label", "entity_type"),
    [
        ("Facebook", "Organization"),
        ("New York", "Location"),
        ("TensorFlow Lite", "Product"),
    ],
)
def test_compact_named_entities_remain_clean(label, entity_type):
    quality = classify_entity_label(label, entity_type)
    assert quality.label_quality == LABEL_CLEAN
    assert quality.eligible_for_topic_label is True
    assert quality.eligible_for_synthesis is True


def test_code_signature_is_code_like_not_topic_label():
    quality = classify_entity_label("void shared(std::shared_ptr<Widget>&)", "Method")
    assert quality.label_quality == LABEL_CODE_LIKE
    assert quality.eligible_for_topic_label is False


def test_joined_pasted_label_is_not_topic_label():
    quality = classify_entity_label(
        "speaker users facebook & new york young woman",
        "Concept",
    )
    assert quality.label_quality == LABEL_JOINED_LIST
    assert quality.eligible_for_topic_label is False


@pytest.mark.asyncio
async def test_neo4j_writer_persists_entity_quality_fields():
    class _Session:
        def __init__(self):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def run(self, query, **kwargs):
            self.calls.append((query, kwargs))
            return MagicMock()

    class _Driver:
        def __init__(self, session):
            self._session = session

        def session(self):
            return self._session

    session = _Session()
    result = ExtractionResult(
        schema_version="polymath.extract.v1",
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        entities=[
            EntityItem(
                canonical_name="intuition that performance in anything has a few impossibly clumsy persons",
                surface_form="intuition that performance in anything has a few impossibly clumsy persons",
                entity_type="Concept",
                confidence=0.8,
            )
        ],
        relations=[],
    )

    await write_document_graph(
        driver=_Driver(session),
        doc_id="d1",
        corpus_id="corp1",
        extraction_results=[result],
        all_chunk_ids=["c1"],
    )

    entity_call = next(kwargs for _, kwargs in session.calls if "rows" in kwargs and kwargs["rows"] and "label_quality" in kwargs["rows"][0])
    row = entity_call["rows"][0]
    assert row["label_quality"] == LABEL_CLAIM_LIKE
    assert row["eligible_for_topic_label"] is False
    assert row["eligible_for_synthesis"] is False
    assert row["entity_quality_version"] == ENTITY_QUALITY_VERSION


@pytest.mark.asyncio
async def test_neo4j_writer_persists_graphrag_bridge_fields():
    class _Session:
        def __init__(self):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def run(self, query, **kwargs):
            self.calls.append((query, kwargs))
            return MagicMock()

    class _Driver:
        def __init__(self, session):
            self._session = session

        def session(self):
            return self._session

    session = _Session()
    result = ExtractionResult(
        schema_version="polymath.extract.v2",
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        entities=[
            EntityItem(
                canonical_name="OpenAI",
                surface_form="OpenAI",
                entity_type="Organization",
                confidence=0.95,
                aliases=["Open AI"],
                description="AI research organization.",
            ),
            EntityItem(
                canonical_name="Sam Altman",
                surface_form="Sam Altman",
                entity_type="Person",
                confidence=0.9,
            ),
        ],
        relations=[
            RelationItem(
                subject="OpenAI",
                predicate="created_by",
                object="Sam Altman",
                object_kind="entity",
                confidence=0.88,
                predicate_family="Provenance",
                qualifier="co-founder",
                source_sentence="OpenAI was co-founded by Sam Altman.",
                extraction_model="LFM2-1.2B-Extract",
                repaired=False,
            )
        ],
    )

    await write_document_graph(
        driver=_Driver(session),
        doc_id="d1",
        corpus_id="corp1",
        extraction_results=[result],
        all_chunk_ids=["c1"],
        summaries=[
            SummaryResult(
                parent_id="p1",
                doc_id="d1",
                corpus_id="corp1",
                source_tier="tier_a",
                summary="Dense parent summary.",
            )
        ],
    )

    assert any("HAS_SUMMARY" in query for query, _ in session.calls)
    entity_call = next(
        kwargs
        for query, kwargs in session.calls
        if "MENTIONED_IN" in query and kwargs.get("rows")
    )
    openai_row = next(row for row in entity_call["rows"] if row["canonical_name"] == "openai")
    assert openai_row["aliases"] == ["Open AI"]
    assert openai_row["description"] == "AI research organization."
    assert openai_row["embedding_id"].startswith("entity:corp1:openai")
    assert openai_row["doc_ids"] == ["d1"]
    assert openai_row["chunk_ids"] == ["c1"]

    relation_call = next(
        kwargs
        for query, kwargs in session.calls
        if "RELATES_TO" in query and "predicate_family" in query and kwargs.get("rows")
    )
    relation_row = relation_call["rows"][0]
    assert relation_row["predicate_family"] == "Provenance"
    assert relation_row["qualifier"] == "co-founder"
    assert relation_row["source_sentence"] == "OpenAI was co-founded by Sam Altman."
    assert relation_row["chunk_id"] == "c1"
    assert relation_row["doc_id"] == "d1"
    assert relation_row["extraction_model"] == "LFM2-1.2B-Extract"
    assert relation_row["repaired"] is False
    assert relation_row["embedding_id"].startswith("relation:corp1:d1:c1:")


def test_concept_community_label_ignores_ineligible_noisy_node():
    import networkx as nx

    G = nx.Graph()
    G.add_node(
        "noisy",
        canonical_name="intuition that performance in anything has a few impossibly clumsy persons",
        entity_type="Concept",
        label_quality=LABEL_CLAIM_LIKE,
        eligible_for_topic_label=False,
        eligible_for_synthesis=False,
    )
    G.add_node(
        "tf",
        canonical_name="TensorFlow Lite",
        entity_type="Product",
        label_quality=LABEL_CLEAN,
        eligible_for_topic_label=True,
        eligible_for_synthesis=True,
    )
    G.add_node(
        "mlkit",
        canonical_name="ML Kit",
        entity_type="Product",
        label_quality=LABEL_CLEAN,
        eligible_for_topic_label=True,
        eligible_for_synthesis=True,
    )
    G.add_edge("tf", "mlkit", weight=1)
    G.add_edge("noisy", "tf", weight=10)
    communities, entity_map = analytics.compute_concept_communities(
        G,
        {"noisy": 10.0, "tf": 1.0, "mlkit": 0.9},
    )
    assert communities
    labels = " ".join(str(c["label"]) for c in communities).lower()
    assert "intuition" not in labels
    assert any("tensorflow" in str(c["label"]).lower() or "ml" in str(c["label"]).lower() for c in communities)
    assert "noisy" not in entity_map


@pytest.mark.asyncio
async def test_entity_quality_backfill_marks_cache_stale_without_deleting_nodes():
    from services.graph.entity_quality import backfill_entity_quality

    class _Result:
        def __init__(self, rows):
            self._rows = list(rows)

        def __aiter__(self):
            self._iter = iter(self._rows)
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    class _Session:
        def __init__(self):
            self.reads = 0
            self.writes = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def run(self, query, **kwargs):
            if "RETURN e.entity_id" in query:
                self.reads += 1
                if self.reads > 1:
                    return _Result([])
                return _Result([
                    {
                        "entity_id": "entity:speaker",
                        "label": "speaker",
                        "entity_type": "Concept",
                        "observed_entity_types": ["Concept"],
                    }
                ])
            self.writes.append(kwargs)
            return _Result([])

    class _Driver:
        def __init__(self, session):
            self._session = session

        def session(self):
            return self._session

    cache = MagicMock()
    cache.update_many = AsyncMock()
    db = MagicMock()
    db.__getitem__.return_value = cache
    session = _Session()

    result = await backfill_entity_quality(
        _Driver(session),
        db,
        corpus_id="corp1",
        batch_size=10,
    )

    assert result["deleted_entities"] == 0
    assert result["updated_entities"] == 1
    assert result["quality_counts"] == {LABEL_GENERIC_ROLE: 1}
    assert session.writes[0]["rows"][0]["label_quality"] == LABEL_GENERIC_ROLE
    cache.update_many.assert_awaited()
