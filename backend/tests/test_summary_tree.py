"""B3 summary-tree tests — pure structure + fake-LLM generation + owner rules.

    docker exec -i polymath_v33-backend-1 python /app/tests/test_summary_tree.py
"""

from __future__ import annotations

import asyncio
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.ingestion.summary_tree import (  # noqa: E402
    ParentSummaryIn,
    build_profile_input,
    build_tree,
    group_by_section,
    windows,
)


def _parents(n, heading="Ch1"):
    return [
        ParentSummaryIn(
            parent_id=f"p{i:04d}",
            summary=f"Parent {i} establishes point number {i} about the topic.",
            heading_path=(heading,),
            domain="xml" if i % 3 else "schema_design",
            concepts=("XML", "schema") if i % 2 else ("XPath",),
        )
        for i in range(n)
    ]


# ── owner rule: 1,727 parents → ~87 rollups of 12-20, deterministic ────────
def test_goldfarb_windowing():
    wins = windows(_parents(1727))
    assert 87 <= len(wins) <= 144
    assert all(12 <= len(w) <= 20 for w in wins)
    assert sum(len(w) for w in wins) == 1727
    a = [len(w) for w in windows(_parents(1727))]
    b = [len(w) for w in windows(_parents(1727))]
    assert a == b                                     # deterministic


def test_small_section_single_window():
    assert len(windows(_parents(7))) == 1


def test_group_by_section_consecutive():
    ps = _parents(3, "A") + _parents(2, "B") + _parents(1, "A")
    groups = group_by_section(ps)
    assert [g[0] for g in groups] == ["A", "B", "A"]  # reappearing heading = new group
    assert [len(g[1]) for g in groups] == [3, 2, 1]


# ── profile input: 1-2k tokens, never all parents ──────────────────────────
def test_profile_input_bounded():
    async def run():
        return await build_tree(
            doc_id="x" * 24, corpus_id="k", title="Definitive XML",
            source_type="book", parents=_parents(1727), llm_fn=None)
    nodes = asyncio.run(run())
    sections = [n for n in nodes if n.node_type == "section"]
    from services.ingestion.summary_tree import PROFILE_MAX_SECTIONS
    text = build_profile_input("Definitive XML", "book", sections,
                               {"xml": 1151, "schema_design": 576}, {"XML": 900})
    assert len(text.split()) < 1500                   # words << 2k tokens
    assert "Detected domains" in text and "xml(1151)" in text
    assert text.count("- ") <= PROFILE_MAX_SECTIONS


# ── full tree with fake LLM: shapes, stable ids, fallback ──────────────────
def test_tree_shapes_and_stable_ids():
    calls = []

    async def fake_llm(prompt):
        calls.append(prompt)
        return "A dense merged summary."

    async def run():
        return await build_tree(doc_id="d" * 24, corpus_id="k", title="T",
                                source_type="book", parents=_parents(45), llm_fn=fake_llm)
    nodes = asyncio.run(run())
    kinds = {}
    for n in nodes:
        kinds[n.node_type] = kinds.get(n.node_type, 0) + 1
    assert kinds == {"rollup": 3, "section": 1, "document": 1}   # 45 → 3 windows
    rollup_members = [m for n in nodes if n.node_type == "rollup" for m in n.parent_ids]
    assert len(rollup_members) == 45 and len(set(rollup_members)) == 45
    doc = [n for n in nodes if n.node_type == "document"][0]
    assert doc.node_id == f"docsum_{'d' * 12}"
    assert doc.concepts and doc.domains
    nodes2 = asyncio.run(run())
    assert [n.node_id for n in nodes] == [n.node_id for n in nodes2]  # stable/resumable
    # never feed all parents to the profile call: last call is the profile
    assert "Parent 3 establishes" not in calls[-1]


def test_llm_failure_falls_back_extractively():
    async def dead_llm(prompt):
        raise RuntimeError("model down")

    async def run():
        return await build_tree(doc_id="e" * 24, corpus_id="k", title="T",
                                source_type="book", parents=_parents(14), llm_fn=dead_llm)
    nodes = asyncio.run(run())
    assert all(n.summary for n in nodes)              # deterministic fallback, no raise
    assert "Parent 0 establishes point number 0" in nodes[0].summary


def test_single_rollup_section_reuses_summary():
    async def fake(prompt):
        return "merged"
    async def run():
        return await build_tree(doc_id="f" * 24, corpus_id="k", title="T",
                                source_type="book", parents=_parents(10), llm_fn=fake)
    nodes = asyncio.run(run())
    r = [n for n in nodes if n.node_type == "rollup"][0]
    s = [n for n in nodes if n.node_type == "section"][0]
    assert s.summary == r.summary                     # no wasted LLM call


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
