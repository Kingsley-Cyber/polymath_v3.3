import pytest

from services.retriever.summary_tree_navigator import SummaryTreeNavigator
from services.retriever.tier0_router import DocumentRoute
from services.retriever.summary_tree_navigator import (
    TreeNodeCandidate,
    select_collapsed_tree_nodes,
)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length):
        return self.rows[:length]


class _Collection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, *_args, **_kwargs):
        return _Cursor(self.rows)


def test_collapsed_tree_chooses_the_relevant_abstraction_levels():
    selected = select_collapsed_tree_nodes(
        [
            TreeNodeCandidate("section_story", "section", 0.88, 120),
            TreeNodeCandidate("rollup_motion", "rollup", 0.84, 90),
            TreeNodeCandidate("rollup_audience", "rollup", 0.81, 100),
            TreeNodeCandidate("section_unrelated", "section", 0.51, 100),
        ],
        max_tokens=400,
    )

    assert [item.node_id for item in selected] == [
        "section_story",
        "rollup_motion",
        "rollup_audience",
    ]


def test_collapsed_tree_honors_token_budget_without_dropping_best_node():
    selected = select_collapsed_tree_nodes(
        [
            TreeNodeCandidate("best", "section", 0.91, 300),
            TreeNodeCandidate("second", "rollup", 0.89, 250),
            TreeNodeCandidate("third", "rollup", 0.86, 80),
        ],
        max_tokens=400,
        cliff_min_gap=1.0,
    )

    assert [item.node_id for item in selected] == ["best", "third"]


@pytest.mark.asyncio
async def test_navigator_descends_selected_section_to_source_parent_ids():
    rows = [
        {
            "node_id": "section_story",
            "node_type": "section",
            "corpus_id": "c1",
            "doc_id": "d1",
            "summary": "Visual story and opening scene direction",
            "child_node_ids": ["rollup_opening"],
        },
        {
            "node_id": "rollup_opening",
            "node_type": "rollup",
            "corpus_id": "c1",
            "doc_id": "d1",
            "summary": "Opening shot, camera movement, and audience attention",
            "parent_ids": ["parent_1", "parent_2"],
        },
        {
            "node_id": "rollup_unrelated",
            "node_type": "rollup",
            "corpus_id": "c1",
            "doc_id": "d1",
            "summary": "Accounting and warehouse reconciliation",
            "parent_ids": ["parent_noise"],
        },
    ]

    async def fake_embed(texts, _config):
        return [[0.0, 1.0] if "Accounting" in text else [1.0, 0.0] for text in texts]

    navigator = SummaryTreeNavigator()
    routes, diagnostics = await navigator.navigate(
        lane_vectors={"opening": [1.0, 0.0]},
        document_routes={"opening": [DocumentRoute("opening", "c1", "d1", 0.88)]},
        db={"summary_tree": _Collection(rows)},
        embed_fn=fake_embed,
    )

    assert diagnostics["strategy"] == "document_gated_adaptive_tree_descent"
    assert routes["opening"][0].section_ids == ("section_story",)
    assert routes["opening"][0].parent_ids == ("parent_1", "parent_2")
    assert "parent_noise" not in routes["opening"][0].parent_ids


@pytest.mark.asyncio
async def test_navigator_uses_preembedded_tree_points_without_embedding_nodes():
    rows = [
        {
            "node_id": "section_story",
            "node_type": "section",
            "corpus_id": "c1",
            "doc_id": "d1",
            "summary": "Story direction",
            "child_node_ids": ["rollup_opening"],
        },
        {
            "node_id": "rollup_opening",
            "node_type": "rollup",
            "corpus_id": "c1",
            "doc_id": "d1",
            "summary": "Opening movement",
            "parent_ids": ["parent_1"],
        },
    ]
    calls = []

    async def fake_tree_search(_client, _corpus_id, **kwargs):
        calls.append(kwargs["node_type"])
        if kwargs["node_type"] == "section":
            return [
                {
                    "node_id": "section_story",
                    "score": 0.91,
                    "token_estimate": 10,
                    "child_node_ids": ["rollup_opening"],
                    "lexicon_ids": ["lex-facs"],
                }
            ]
        return [
            {
                "node_id": "rollup_opening",
                "score": 0.88,
                "token_estimate": 10,
                "parent_ids": ["parent_1"],
                "lexicon_ids": ["lex-facs", "lex-laban"],
            }
        ]

    async def fail_embed(*_args, **_kwargs):
        raise AssertionError("tree nodes must not be embedded at query time")

    routes, diagnostics = await SummaryTreeNavigator().navigate(
        lane_vectors={"opening": [1.0, 0.0]},
        document_routes={"opening": [DocumentRoute("opening", "c1", "d1", 0.88)]},
        db={"summary_tree": _Collection(rows)},
        qdrant_client=object(),
        tree_search_fn=fake_tree_search,
        embed_fn=fail_embed,
    )

    assert calls == ["section", "rollup"]
    assert diagnostics["vector_source"] == "qdrant_preembedded"
    assert routes["opening"][0].parent_ids == ("parent_1",)
    assert routes["opening"][0].lexicon_ids == ("lex-facs", "lex-laban")


@pytest.mark.asyncio
async def test_navigator_batches_preembedded_tree_queries_per_corpus_and_level():
    calls = []

    async def fail_single_search(*_args, **_kwargs):
        raise AssertionError("single-point tree search should not run")

    async def fake_batch_search(_client, corpus_id, *, queries):
        calls.append((corpus_id, [query["node_type"] for query in queries]))
        output = []
        for query in queries:
            doc_id = query["doc_id"]
            if query["node_type"] == "section":
                output.append(
                    [
                        {
                            "node_id": f"section-{doc_id}",
                            "score": 0.9,
                            "token_estimate": 10,
                            "child_node_ids": [f"rollup-{doc_id}"],
                        }
                    ]
                )
            else:
                output.append(
                    [
                        {
                            "node_id": f"rollup-{doc_id}",
                            "score": 0.85,
                            "token_estimate": 10,
                            "parent_ids": [f"parent-{doc_id}"],
                        }
                    ]
                )
        return output

    routes, diagnostics = await SummaryTreeNavigator().navigate(
        lane_vectors={"opening": [1.0, 0.0]},
        document_routes={
            "opening": [
                DocumentRoute("opening", "c1", "d1", 0.9),
                DocumentRoute("opening", "c1", "d2", 0.8),
            ]
        },
        qdrant_client=object(),
        tree_search_fn=fail_single_search,
        tree_batch_search_fn=fake_batch_search,
    )

    assert calls == [
        ("c1", ["section", "section"]),
        ("c1", ["rollup", "rollup"]),
    ]
    assert diagnostics["vector_source"] == "qdrant_preembedded"
    assert [route.parent_ids for route in routes["opening"]] == [
        ("parent-d1",),
        ("parent-d2",),
    ]
