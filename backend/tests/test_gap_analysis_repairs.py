import pytest

from services.ingestion.summary_tree import ParentSummaryIn, group_by_section
from services.ingestion.tier_chunker import _make_children
from services.retriever.funnel_a import FunnelA
from services.retriever.planned_fusion import reserve_planned_finalists
from services.retriever.summary_tree_navigator import SummaryTreeNavigator
from services.retriever.tier0_router import DocumentRoute
from services.storage import qdrant_writer
from services.text_quality import is_separator_only_text
from models.schemas import SourceChunk


def _chunk(chunk_id: str, corpus_id: str, score: float) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"parent-{chunk_id}",
        doc_id=f"doc-{chunk_id}",
        corpus_id=corpus_id,
        text=f"evidence {chunk_id}",
        score=score,
        source_tier="vector",
    )


def test_separator_guard_is_narrow_and_preserves_code_and_equations():
    assert is_separator_only_text("| --- | :---: | === |") is True
    assert is_separator_only_text("----------------") is True
    assert is_separator_only_text("x = y / 2") is False
    assert is_separator_only_text("def f():") is False


def test_child_builder_drops_separator_only_child():
    children, next_index = _make_children(
        "parent-1",
        "doc-1",
        "corpus-1",
        "| --- | :---: |",
        ["Table"],
        "tier_a",
        0,
        child_target_tokens=128,
    )

    assert children == []
    assert next_index == 0


def test_page_labels_do_not_fragment_summary_tree_sections():
    parents = [
        ParentSummaryIn("p1", "one", ("Chapter One",)),
        ParentSummaryIn("p2", "two", ("Page 2",)),
        ParentSummaryIn("p3", "three", ("P. 3 of 20",)),
        ParentSummaryIn("p4", "four", ("Chapter Two",)),
        ParentSummaryIn("p5", "five", ("Page iv",)),
    ]

    groups = group_by_section(parents)

    assert [(heading, [row.parent_id for row in rows]) for heading, rows in groups] == [
        ("Chapter One", ["p1", "p2", "p3"]),
        ("Chapter Two", ["p4", "p5"]),
    ]


@pytest.mark.asyncio
async def test_unmodeled_summary_is_not_written(monkeypatch):
    called = False

    async def capture(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(qdrant_writer, "_upsert_points_batched", capture)
    await qdrant_writer.upsert_summaries(
        object(),
        "corpus-1",
        [
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "parent_id": "parent-1",
                "source_tier": "tier_a",
                "summary": "Raw parent text copied as a placeholder.",
                "summary_model": "",
            }
        ],
        [[0.1, 0.2]],
        ["hrag"],
    )

    assert called is False


@pytest.mark.asyncio
async def test_summary_search_excludes_explicit_placeholder_model():
    funnel = FunnelA.__new__(FunnelA)
    captured = []

    async def capture(_collection, _vector, query_filter, _limit, **_kwargs):
        captured.append(query_filter)
        return []

    funnel._search_collection = capture
    await funnel._search_scoped(
        [0.1, 0.2],
        corpus_scope=["corpus-1"],
        collections=["collection-1"],
        top_k=5,
    )

    assert any(
        condition.key == "summary_model"
        and condition.match.value == ""
        for condition in captured[0].must_not
    )


def test_finalist_reservation_skips_irrelevant_selected_corpus():
    strong = _chunk("strong", "alpha", 0.96)
    weak = _chunk("weak", "beta", 0.06)

    selected, diagnostics = reserve_planned_finalists(
        [strong, weak],
        [strong],
        required_lane_ids=[],
        corpus_ids=["alpha", "beta"],
        max_candidates=4,
    )

    assert [chunk.chunk_id for chunk in selected] == ["strong"]
    assert diagnostics["corpus_reservations"] == {"alpha": "strong"}
    assert diagnostics["skipped_corpus_reservations"] == ["beta"]


@pytest.mark.asyncio
async def test_preembedded_single_child_section_skips_rollup_search():
    calls = []

    async def fail_single(*_args, **_kwargs):
        raise AssertionError("single search should not run")

    async def batch_search(_client, _corpus_id, *, queries):
        calls.append([query["node_type"] for query in queries])
        assert all(query["node_type"] == "section" for query in queries)
        return [
            [
                {
                    "node_id": "section-1",
                    "score": 0.91,
                    "token_estimate": 12,
                    "child_node_ids": ["rollup-1"],
                    "passthrough_rollup_id": "rollup-1",
                    "passthrough_parent_ids": ["parent-1", "parent-2"],
                    "passthrough_lexicon_ids": ["lex-1"],
                }
            ]
        ]

    routes, diagnostics = await SummaryTreeNavigator().navigate(
        lane_vectors={"primary": [1.0, 0.0]},
        document_routes={
            "primary": [DocumentRoute("primary", "corpus-1", "doc-1", 0.9)]
        },
        qdrant_client=object(),
        tree_search_fn=fail_single,
        tree_batch_search_fn=batch_search,
    )

    assert calls == [["section"]]
    assert routes["primary"][0].parent_ids == ("parent-1", "parent-2")
    assert routes["primary"][0].lexicon_ids == ("lex-1",)
    assert diagnostics["vector_source"] == "qdrant_preembedded"
