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
    build_and_store_tree,
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


class _FakeFind:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    async def to_list(self, length=None):
        return list(self.rows if length is None else self.rows[:length])


class _FakeCollection:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.updates = []
        self.replacements = []

    async def find_one(self, *_args, **_kwargs):
        return dict(self.rows[0]) if self.rows else None

    def find(self, *_args, **_kwargs):
        return _FakeFind(self.rows)

    async def update_one(self, query, update):
        self.updates.append((query, update))
        for row in self.rows:
            if all(row.get(k) == v for k, v in query.items()):
                row.update(update.get("$set", {}))
                break

    async def replace_one(self, query, record, upsert=False):
        self.replacements.append((query, record, upsert))


class _FakeDb:
    def __init__(self):
        self.collections = {
            "documents": _FakeCollection([{
                "doc_id": "doc_1",
                "corpus_id": "corpus_1",
                "filename": "artifact.md",
                "source_type": "markdown",
            }]),
            "parent_chunks": _FakeCollection([{
                "parent_id": "parent_1",
                "doc_id": "doc_1",
                "corpus_id": "corpus_1",
                "summary": None,
                "text": "Polymath summaries preserve child evidence anchors.",
                "heading_path": ["Overview"],
                "domain": "machine_learning",
                "chunk_kind": "body",
                "child_ids": ["child_1"],
            }]),
            "summary_tree": _FakeCollection(),
            "corpora": _FakeCollection([{
                "corpus_id": "corpus_1",
                "description": "Owner corpus note: use this as reference context.",
            }]),
            "ghost_b_extractions": _FakeCollection([{
                "doc_id": "doc_1",
                "corpus_id": "corpus_1",
                "status": "ok",
                "entities": [{"canonical_name": "Polymath"}],
            }]),
        }

    def __getitem__(self, name):
        return self.collections[name]


def test_heal_missing_summary_persists_parent_artifact_fields():
    db = _FakeDb()

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

    result = asyncio.run(build_and_store_tree(
        db=db,
        doc_id="doc_1",
        corpus_id="corpus_1",
        llm_fn=fake_llm,
    ))

    assert result["summaries_healed"] == 1
    update = db["parent_chunks"].updates[0][1]["$set"]
    assert update["schema_version"] == "parent_summary.v1"
    assert update["summary_type"] == "parent_retrieval_replacement"
    assert update["source_child_ids"] == ["child_1"]
    assert update["key_points"][0]["supporting_child_ids"] == ["child_1"]
    doc_profile = db["documents"].updates[-1][1]["$set"]["doc_profile"]
    assert doc_profile["doc_artifact"]["artifact_version"] == "polymath.doc_artifact.v1"
    assert doc_profile["doc_artifact"]["field_provenance"]["owner_intent"] == "corpus_description"
    assert doc_profile["doc_artifact"]["synthesis_hint"]


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
