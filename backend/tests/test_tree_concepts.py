"""P0.2/P2.1 — deterministic tree concepts (derive_node_concepts + construction).

Extends the summary-tree fixtures in tests/test_summary_tree.py. Standalone:

    docker exec -i polymath_v33-backend-1 python /app/tests/test_tree_concepts.py
"""

from __future__ import annotations

import asyncio
import os
import sys

_TESTS = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_TESTS)
for _path in (_BACKEND, _TESTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from services.ingestion.summary_tree import (  # noqa: E402
    NODE_CONCEPTS_CAP,
    ParentSummaryIn,
    build_and_store_tree,
    build_tree,
    derive_node_concepts,
)
from test_summary_tree import _FakeDb  # noqa: E402 — existing fixture


_ROWS = [
    {
        "key_terms": ["Neural Networks", "XPath"],
        "mechanisms": ["feedback loop"],
        "concept_tags": ["neural networks"],  # dup of key_term within same row
    },
    {
        "key_terms": ["XPath"],
        "mechanisms": [],
        "concept_tags": ["Self-Attention"],
    },
    {
        "key_terms": ["xpath"],
        "concept_tags": ["schema design"],
    },
]


# ── derive_node_concepts: determinism / normalization / cap / ordering ──────
def test_derive_deterministic_and_order_independent():
    a = derive_node_concepts(_ROWS)
    b = derive_node_concepts(_ROWS)
    c = derive_node_concepts(list(reversed(_ROWS)))
    assert a == b == c  # repeat-stable AND row-order independent


def test_derive_snake_case_normalization_and_junk_guard():
    out = derive_node_concepts(
        [
            {
                "key_terms": ["  Neural   Networks ", "Self-Attention", ""],
                "mechanisms": [None, True, {"bad": 1}, ["nested"]],
                "concept_tags": ["C++ / STL"],
            }
        ]
    )
    assert out == ["c_stl", "neural_networks", "self_attention"]
    assert all(term == term.lower() and " " not in term for term in out)


def test_derive_dedups_within_row_and_counts_across_rows():
    out = derive_node_concepts(_ROWS)
    # xpath appears in all 3 rows (freq 3) — first; everything else freq 1,
    # alphabetical; "neural networks" dup within row 1 counted once.
    assert out == [
        "xpath",
        "feedback_loop",
        "neural_networks",
        "schema_design",
        "self_attention",
    ]


def test_derive_frequency_desc_then_alpha_tiebreak():
    rows = [
        {"key_terms": ["beta", "alpha"]},
        {"key_terms": ["beta", "zeta"]},
    ]
    assert derive_node_concepts(rows) == ["beta", "alpha", "zeta"]


def test_derive_cap_keeps_highest_frequency():
    rows = [{"key_terms": [f"term {i:02d}" for i in range(20)]}] + [
        {"key_terms": ["kept"]} for _ in range(3)
    ]
    out = derive_node_concepts(rows)
    assert len(out) == NODE_CONCEPTS_CAP == 16
    assert out[0] == "kept"  # freq 3 beats the 20 freq-1 terms
    assert derive_node_concepts(rows, cap=2) == ["kept", "term_00"]
    assert derive_node_concepts(rows, cap=0) == []
    assert derive_node_concepts([]) == []


# ── construction: every node type carries derived concepts ─────────────────
def _concept_parents(n, heading="Ch1"):
    """Summary-tree fixture parents (mirrors test_summary_tree._parents) whose
    concepts come pre-derived, as build_and_store_tree now supplies them."""
    return [
        ParentSummaryIn(
            parent_id=f"p{i:04d}",
            summary=f"Parent {i} establishes point number {i} about the topic.",
            heading_path=(heading,),
            domain="xml" if i % 3 else "schema_design",
            concepts=tuple(
                derive_node_concepts(
                    [
                        {
                            "key_terms": ["XML", "schema"] if i % 2 else ["XPath"],
                            "mechanisms": ["hierarchical decomposition"],
                            "concept_tags": ["markup languages"],
                        }
                    ]
                )
            ),
        )
        for i in range(n)
    ]


def test_build_tree_passes_concepts_to_every_node_type():
    async def fake(prompt):
        return "merged"

    async def run():
        return await build_tree(
            doc_id="c" * 24,
            corpus_id="k",
            title="T",
            source_type="book",
            parents=_concept_parents(45),
            llm_fn=fake,
        )

    nodes = asyncio.run(run())
    by_type: dict[str, list] = {}
    for node in nodes:
        by_type.setdefault(node.node_type, []).append(node)
    assert set(by_type) == {"rollup", "section", "document"}
    assert all(node.concepts for nodes_ in by_type.values() for node in nodes_)

    parents = {p.parent_id: p for p in _concept_parents(45)}
    expected_union = {
        "xml",
        "schema",
        "xpath",
        "hierarchical_decomposition",
        "markup_languages",
    }
    for rollup in by_type["rollup"]:
        member_union = {
            c for pid in rollup.parent_ids for c in parents[pid].concepts
        }
        assert set(rollup.concepts) == member_union  # union of window parents
    section = by_type["section"][0]
    assert set(section.concepts) == {
        c for r in by_type["rollup"] for c in r.concepts
    }  # section = union of child rollups' derived concepts
    document = by_type["document"][0]
    assert set(document.concepts) == set(section.concepts) == expected_union
    nodes2 = asyncio.run(run())
    assert [n.concepts for n in nodes] == [n.concepts for n in nodes2]  # stable


def test_concepts_without_parent_metadata_stay_empty_not_invented():
    async def run():
        return await build_tree(
            doc_id="h" * 24,
            corpus_id="k",
            title="T",
            source_type="book",
            parents=[
                ParentSummaryIn(parent_id=f"p{i}", summary=f"Point {i}.")
                for i in range(14)
            ],
            llm_fn=None,
        )

    nodes = asyncio.run(run())
    assert all(node.concepts == [] for node in nodes)  # deterministic, no junk


# ── persistence path: Mongo summary_tree rows carry concepts ────────────────
def test_build_and_store_tree_persists_concepts_on_tree_rows():
    db = _FakeDb()
    db["parent_chunks"].rows[0].update(
        {
            "summary": "Polymath summaries preserve child evidence anchors.",
            "key_terms": ["Polymath", "evidence anchors"],
            "mechanisms": ["evidence_anchoring"],
            "concept_tags": ["summary artifact"],
        }
    )

    async def fake(prompt):
        return "Merged summary."

    result = asyncio.run(
        build_and_store_tree(db=db, doc_id="doc_1", corpus_id="corpus_1", llm_fn=fake)
    )
    assert result["summaries_healed"] == 0  # summary already present
    replaced = [record for _q, record, _u in db["summary_tree"].replacements]
    assert replaced, "tree rows must be written"
    expected = ["evidence_anchoring", "evidence_anchors", "polymath", "summary_artifact"]
    for record in replaced:
        assert record["concepts"] == expected  # every node type, snake_case


def test_healed_parent_contributes_concepts_same_run():
    db = _FakeDb()  # parent starts with summary=None → heal path fills fields

    async def fake_llm(prompt):
        if "source_child_ids" in prompt:
            return """
            {
              "summary": "Polymath summaries preserve child evidence anchors.",
              "domain": "machine_learning",
              "semantic_chunk_type": "claim",
              "key_terms": ["Polymath"],
              "mechanisms": ["evidence_anchoring"],
              "central_claim": "Summaries preserve child evidence anchors.",
              "key_points": [
                {"point": "Summaries keep evidence anchors.", "supporting_child_ids": ["child_1"]}
              ],
              "concept_tags": ["summary artifact", "evidence anchors"],
              "entity_hints": ["Polymath"],
              "retrieval_uses": ["claim", "evidence"],
              "abstraction_level": "medium"
            }
            """
        return "Merged summary."

    result = asyncio.run(
        build_and_store_tree(db=db, doc_id="doc_1", corpus_id="corpus_1", llm_fn=fake_llm)
    )
    assert result["summaries_healed"] == 1
    replaced = [record for _q, record, _u in db["summary_tree"].replacements]
    assert replaced
    for record in replaced:
        assert "polymath" in record["concepts"]
        assert "evidence_anchoring" in record["concepts"]
        assert "summary_artifact" in record["concepts"]


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
