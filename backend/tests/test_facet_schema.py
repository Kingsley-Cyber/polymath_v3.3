from types import SimpleNamespace

import pytest

from services.facets import (
    FACET_SCHEMA_VERSION,
    build_ingest_facet_profile,
    canonical_display_name,
    matching_ingest_facets,
    matching_vector_facets,
    metadata_facet_terms,
    metadata_with_facets,
    normalize_facet_id,
)
from services.ingestion.worker import _build_child_dicts, _build_parent_dicts


def _parent(parent_id="p1", heading_path=None):
    return SimpleNamespace(
        parent_id=parent_id,
        doc_id="doc-1",
        corpus_id="corpus-1",
        text="Parent text about private on-device LLM architecture.",
        heading_path=heading_path or ["On Device LLM Architecture"],
        source_tier="tier_a",
        children=[SimpleNamespace(chunk_id="c1")],
        metadata={},
    )


def _child(chunk_id="c1", parent_id="p1", heading_path=None):
    return SimpleNamespace(
        chunk_id=chunk_id,
        parent_id=parent_id,
        doc_id="doc-1",
        corpus_id="corpus-1",
        text="Child text about RAG, Qdrant, and local model privacy.",
        heading_path=heading_path or ["Retrieval Augmented Generation"],
        source_tier="tier_b",
        token_count=12,
        metadata={},
    )


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        if length is None:
            return list(self.rows)
        return list(self.rows)[:length]


class _Collection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query, projection=None):
        del projection
        corpus_filter = set(query.get("corpus_id", {}).get("$in", []))
        rows = [
            row
            for row in self.rows
            if not corpus_filter or row.get("corpus_id") in corpus_filter
        ]
        return _Cursor(rows)


class _Db(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


class _FakeQdrant:
    def __init__(self, points):
        self.points = points

    async def get_collection(self, collection_name):
        del collection_name
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors={"dense": {}}, sparse_vectors={})
            )
        )

    async def query_points(self, **kwargs):
        del kwargs
        return SimpleNamespace(points=self.points)


def test_facet_ids_are_stable_snake_case_and_display_is_canonical():
    assert normalize_facet_id("On Device LLM Architecture") == "on_device_llm_architecture"
    assert (
        normalize_facet_id("on_device_llm_architecture_guide.md")
        == "on_device_llm_architecture"
    )
    assert (
        normalize_facet_id("Evidence-Centered Assessment Design.pdf")
        == "evidence_centered_assessment_design"
    )

    assert canonical_display_name("on_device_llm_architecture") == "On-Device LLM Architecture"
    assert (
        canonical_display_name("evidence-centered assessment design")
        == "Evidence-Centered Assessment Design"
    )


def test_ingest_facet_profile_places_doc_parent_and_child_facets():
    parents = [_parent()]
    children = [_child()]
    profile = build_ingest_facet_profile(
        filename="on_device_llm_architecture_guide.md",
        doc_id="doc-1",
        corpus_id="corpus-1",
        schema_lens={
            "corpus_domains": ["local_ai_architecture"],
            "canonical_families": ["retrieval_augmented_generation"],
            "object_kinds": ["system_design"],
        },
        parents=parents,
        children=children,
    )

    assert profile["schema_version"] == FACET_SCHEMA_VERSION
    assert profile["primary_facet_id"] == "on_device_llm_architecture"
    # P0.5: lens-derived facets attach only with per-document content
    # evidence. "retrieval_augmented_generation" is evidenced (child heading
    # and text); "local_ai_architecture" appears nowhere in the content, so
    # the corpus-lens category no longer stamps the document.
    assert "local_ai_architecture" not in profile["facet_ids"]
    assert "retrieval_augmented_generation" in profile["facet_ids"]

    parent_rows = _build_parent_dicts(
        parents,
        summaries=None,
        parent_facets=profile["parent_facets"],
    )
    child_rows = _build_child_dicts(
        children,
        user_id="user-1",
        child_facets=profile["child_facets"],
    )

    assert parent_rows[0]["facet_ids"]
    assert child_rows[0]["facet_ids"]
    assert (
        parent_rows[0]["metadata"]["semantic_facets"]["schema_version"]
        == FACET_SCHEMA_VERSION
    )
    assert child_rows[0]["metadata"]["semantic_facets"]["source"] == "ingestion"


def test_payload_facets_merge_into_runtime_metadata():
    metadata = metadata_with_facets(
        {"existing": True},
        {
            "facet_ids": ["on_device_llm_architecture"],
            "facet_text": "on device llm architecture",
            "doc_facet_ids": ["private_ai_systems"],
            "content_facet_ids": ["knowledge_graph", "user_modeling"],
            "content_facet_text": "knowledge graph user modeling",
            "content_facet_source": "parent_summary",
            "content_facet_confidence": 0.86,
            "facet_schema_version": FACET_SCHEMA_VERSION,
        },
    )

    assert metadata["existing"] is True
    assert metadata["semantic_facets"]["facet_ids"] == ["on_device_llm_architecture"]
    assert metadata["semantic_facets"]["doc_facet_ids"] == ["private_ai_systems"]
    assert metadata["semantic_facets"]["content_facet_ids"] == [
        "knowledge_graph",
        "user_modeling",
    ]
    terms = metadata_facet_terms(metadata)
    assert "knowledge graph" in terms
    assert "user modeling" in terms


def test_content_facets_are_created_for_parent_summary_and_child_text():
    parents = [
        _parent(
            heading_path=["Graph RAG Architecture"],
        )
    ]
    parents[0].text = (
        "This parent explains RDF ontologies, semantic networks, graph databases, "
        "and nodes and edges for graph-based reasoning."
    )
    children = [
        _child(
            heading_path=["Adaptive User Models"],
        )
    ]
    children[0].text = (
        "A user model captures a user profile, preferences, and personalization "
        "signals for adaptive systems."
    )
    summaries = [
        SimpleNamespace(
            parent_id="p1",
            summary=(
                "Summary: knowledge graphs and graph RAG organize facts with "
                "ontology terms and semantic relations."
            ),
        )
    ]
    profile = build_ingest_facet_profile(
        filename="mixed_system_notes.md",
        doc_id="doc-1",
        corpus_id="corpus-1",
        parents=parents,
        children=children,
        summaries=summaries,
    )

    parent_meta = profile["parent_facets"]["p1"]
    child_meta = profile["child_facets"]["c1"]

    assert "knowledge_graph" in parent_meta["content_facet_ids"]
    assert parent_meta["content_facet_source"] == "parent_summary"
    assert "user_modeling" in child_meta["content_facet_ids"]

    parent_rows = _build_parent_dicts(
        parents,
        summaries=summaries,
        parent_facets=profile["parent_facets"],
    )
    child_rows = _build_child_dicts(
        children,
        user_id="user-1",
        child_facets=profile["child_facets"],
    )

    assert "knowledge_graph" in parent_rows[0]["content_facet_ids"]
    assert (
        "knowledge_graph"
        in parent_rows[0]["metadata"]["semantic_facets"]["content_facet_ids"]
    )
    assert "user_modeling" in child_rows[0]["content_facet_ids"]


def test_broad_single_words_do_not_stamp_handwritten_content_facets():
    parents = [_parent(heading_path=["General discussion"])]
    parents[0].text = (
        "A choice can create stress, affect control, and change a person's mood."
    )
    profile = build_ingest_facet_profile(
        filename="general_notes.md",
        doc_id="doc-generic",
        corpus_id="corpus-1",
        parents=parents,
        children=[],
        summaries=[],
    )

    content_ids = profile["parent_facets"]["p1"].get("content_facet_ids", [])
    assert "agency_preservation" not in content_ids
    assert "emotional_patterns" not in content_ids


@pytest.mark.asyncio
async def test_matching_ingest_facets_finds_query_named_doc_facets():
    # P0.5: a lens family needs per-document content evidence to attach, so
    # this document carries a heading that actually teaches the category.
    profile = build_ingest_facet_profile(
        filename="Evidence-Centered_Assessment_Design_Layers.pdf.md",
        doc_id="doc-eca",
        corpus_id="corpus-1",
        schema_lens={"canonical_families": ["psychometric_assessment"]},
        parents=[_parent(heading_path=["Psychometric Assessment"])],
        children=[],
    )
    db = _Db(
        documents=_Collection(
            [
                {
                    "doc_id": "doc-eca",
                    "corpus_id": "corpus-1",
                    "filename": "Evidence-Centered_Assessment_Design_Layers.pdf.md",
                    "facet_profile": {
                        key: value
                        for key, value in profile.items()
                        if key not in {"parent_facets", "child_facets"}
                    },
                }
            ]
        )
    )

    rows = await matching_ingest_facets(
        db,
        "How can evidence centered assessment and psychometrics shape the app?",
        ["corpus-1"],
    )

    names = {row["name"] for row in rows}
    assert "evidence_centered_assessment_design_layers" in names
    assert "psychometric_assessment" in names


@pytest.mark.asyncio
async def test_vector_facets_activate_semantic_near_doc_when_lexical_is_thin():
    profile = build_ingest_facet_profile(
        filename="Perceiving Others _ The Psychology of Interpersonal.md",
        doc_id="doc-perceiving",
        corpus_id="corpus-1",
        schema_lens={},
        parents=[],
        children=[],
    )
    db = _Db(
        documents=_Collection(
            [
                {
                    "doc_id": "doc-perceiving",
                    "corpus_id": "corpus-1",
                    "filename": "Perceiving Others _ The Psychology of Interpersonal.md",
                    "facet_profile": {
                        key: value
                        for key, value in profile.items()
                        if key not in {"parent_facets", "child_facets"}
                    },
                }
            ]
        )
    )
    query = "How should interpersonal perception shape a richer user model?"

    lexical_rows = await matching_ingest_facets(db, query, ["corpus-1"])
    assert "perceiving_others_psychology_of_interpersonal" not in {
        row["name"] for row in lexical_rows
    }

    qdrant = _FakeQdrant(
        [
            SimpleNamespace(
                score=0.53,
                payload={
                    "doc_id": "doc-perceiving",
                    "corpus_id": "corpus-1",
                    "doc_name": "Perceiving Others _ The Psychology of Interpersonal.md",
                    "doc_facet_ids": [
                        "perceiving_others_psychology_of_interpersonal"
                    ],
                },
            )
        ]
    )
    vector_rows = await matching_vector_facets(
        db,
        qdrant,
        query,
        [0.1, 0.2, 0.3],
        ["corpus-1"],
    )

    assert vector_rows[0]["name"] == "perceiving_others_psychology_of_interpersonal"
    assert vector_rows[0]["semantic_matched"] is True
    assert vector_rows[0]["source"] == "vector_facet_probe"
