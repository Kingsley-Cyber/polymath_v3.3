from __future__ import annotations

import asyncio
import copy

from scripts.backfill_summary_tree_metadata import (
    METADATA_FIELDS,
    ORIGIN,
    build_plans,
)
from services.ingestion.summary_tree import (
    ParentSummaryIn,
    aggregate_tree_temporal,
    build_tree,
    common_heading_path,
)
from services.storage.qdrant_writer import _summary_tree_payload


def _rows():
    corpus_id = "c"
    doc_id = "d"
    parents = [
        {
            "parent_id": "p1",
            "heading_path": ["Book", "Chapter 1"],
            "temporal_class": "evergreen",
            "time_expressions": [
                {
                    "text": "2001",
                    "role": "publication_time",
                    "char_start": 10,
                    "char_end": 14,
                }
            ],
        },
        {
            "parent_id": "p2",
            "heading_path": ["Book", "Chapter 1", "Scene"],
            "temporal_class": "versioned",
            "time_expressions": [
                {"text": "2001", "role": "publication_time"},
                {"text": "winter 1911", "role": "event_time"},
            ],
        },
    ]
    tree = [
        {
            "corpus_id": corpus_id,
            "doc_id": doc_id,
            "node_id": "r1",
            "node_type": "rollup",
            "parent_ids": ["p1", "p2"],
            "child_node_ids": [],
            "section_range": "Chapter 1",
            "summary": "A summary.",
            "schema_version": "v1",
        },
        {
            "corpus_id": corpus_id,
            "doc_id": doc_id,
            "node_id": "s1",
            "node_type": "section",
            "parent_ids": [],
            "child_node_ids": ["r1"],
            "section_range": "Chapter 1",
            "summary": "A section summary.",
            "schema_version": "v1",
        },
        {
            "corpus_id": corpus_id,
            "doc_id": doc_id,
            "node_id": "doc",
            "node_type": "document",
            "parent_ids": [],
            "child_node_ids": ["s1"],
            "section_range": "Book",
            "summary": "A document profile.",
            "schema_version": "v1",
        },
    ]
    return tree, parents


def test_plan_rolls_parent_metadata_through_all_tree_levels():
    tree, parents = _rows()
    core_before = [
        {key: copy.deepcopy(value) for key, value in row.items()} for row in tree
    ]
    plans = build_plans(
        tree,
        parents,
        run_id="r",
        captured_at="2026-07-18T00:00:00Z",
    )

    assert len(plans) == 3
    for plan in plans:
        fields = plan["set_fields"]
        assert fields["retrieval_text"]
        assert fields["heading_path"] == ["Book", "Chapter 1"]
        assert fields["temporal_class"] == "evergreen"
        assert fields["time_expressions"] == [
            {"text": "2001", "role": "publication_time"},
            {"text": "winter 1911", "role": "event_time"},
        ]
        assert fields["tree_metadata_provenance"]["origin"] == ORIGIN
        assert fields["tree_metadata_provenance"]["source_parent_count"] == 2
        assert all(not state["present"] for state in plan["pre_image"].values())

    assert tree == core_before


def test_existing_projection_is_idempotently_skipped():
    tree, parents = _rows()
    tree[0]["tree_metadata_provenance"] = {"origin": ORIGIN}
    plans = build_plans(tree, parents, run_id="r", captured_at="now")
    assert {plan["node_id"] for plan in plans} == {"s1", "doc"}


def test_common_heading_path_stops_at_first_divergence():
    rows = [
        {"heading_path": ["Book", "One", "Shared"]},
        {"heading_path": ["Book", "Two", "Shared"]},
    ]
    assert common_heading_path(rows, "fallback") == ["Book"]


def test_temporal_aggregation_dedupes_and_uses_stable_class_tie_order():
    temporal_class, expressions = aggregate_tree_temporal(
        [
            {
                "temporal_class": "evergreen",
                "time_expressions": [{"text": "2019", "role": "publication_time"}],
            },
            {
                "temporal_class": "versioned",
                "time_expressions": [{"text": "2019", "role": "publication_time"}],
            },
        ]
    )
    assert temporal_class == "evergreen"
    assert expressions == [{"text": "2019", "role": "publication_time"}]


def test_future_tree_nodes_carry_projection_fields():
    parents = [
        ParentSummaryIn(
            parent_id="p1",
            summary="First source summary.",
            heading_path=("Book", "Chapter"),
            temporal_class="event",
            time_expressions=({"text": "winter 1911", "role": "event_time"},),
        )
    ]
    nodes = asyncio.run(
        build_tree(
            doc_id="d" * 64,
            corpus_id="c",
            title="Book",
            source_type="pdf",
            parents=parents,
            llm_fn=None,
        )
    )
    assert nodes
    for node in nodes:
        assert node.retrieval_text
        assert node.heading_path
        assert node.temporal_class == "event"
        assert node.time_expressions == [{"text": "winter 1911", "role": "event_time"}]
        assert set(METADATA_FIELDS) - {"tree_metadata_provenance"} <= {
            "retrieval_text",
            "heading_path",
            "temporal_class",
            "time_expressions",
        }


def test_qdrant_projection_carries_the_same_routing_metadata():
    payload = _summary_tree_payload(
        {
            "node_id": "r1",
            "node_type": "rollup",
            "doc_id": "d",
            "corpus_id": "c",
            "section_range": "Chapter",
            "summary": "Summary",
            "retrieval_text": "Chapter Summary",
            "heading_path": ["Book", "Chapter"],
            "temporal_class": "event",
            "time_expressions": [{"text": "winter 1911", "role": "event_time"}],
        }
    )
    assert payload["retrieval_text"] == "Chapter Summary"
    assert payload["heading_path"] == ["Book", "Chapter"]
    assert payload["temporal_class"] == "event"
    assert payload["time_expressions"] == [
        {"text": "winter 1911", "role": "event_time"}
    ]
