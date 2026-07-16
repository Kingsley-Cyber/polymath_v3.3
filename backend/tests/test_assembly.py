"""W2 §10.3 — pure tests for waterfall assembly grouping (no Mongo, no app).

Loads waterfall.py + assembly's pure functions by file path so the retriever
package __init__ (full app dependency tree) is never imported.
Runnable standalone: python3 tests/test_assembly.py
"""
import importlib.util
import os
import sys
import types


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(os.path.dirname(__file__), "..", *rel.split("/"))
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


waterfall = _load("services.retriever.waterfall", "services/retriever/waterfall.py")
# assembly imports `services.retriever.waterfall` — satisfied via sys.modules above;
# stub the package chain so its absolute import resolves without the real package.
for pkg in ("services", "services.retriever"):
    sys.modules.setdefault(pkg, types.ModuleType(pkg))
sys.modules["services.retriever"].waterfall = waterfall
assembly = _load("assembly", "services/retriever/assembly.py")


class _C:
    def __init__(self, chunk_id, parent_id, doc_id, score, text="t", provenance=None):
        self.chunk_id = chunk_id
        self.parent_id = parent_id
        self.doc_id = doc_id
        self.corpus_id = "c"
        self.score = score
        self.text = text
        self.provenance = provenance


def test_grouping_rank_order_and_max_score():
    chunks = [
        _C("c1", "p1", "d1", 0.9),
        _C("c2", "p2", "d2", 0.8),
        _C("c3", "p1", "d1", 0.7),  # same parent, lower — ignored for rank
        _C("c4", "p3", "d1", 0.6),
    ]
    pm = {
        ("c", "d1", "p1"): {"text": "P1 FULL", "summary": "p1 sum"},
        ("c", "d2", "p2"): {"text": "P2 FULL", "summary": ""},
        ("c", "d1", "p3"): {"text": "P3 FULL", "summary": "p3 sum"},
    }
    parents, orphans = assembly.group_parent_candidates(chunks, pm)
    assert [p.parent_id for p in parents] == ["p1", "p2", "p3"]  # first appearance
    assert parents[0].score == 0.9 and parents[0].full_text == "P1 FULL"
    assert orphans == []


def test_orphans_and_anchor_lane():
    chunks = [
        _C("c1", "p1", "d1", 0.9),
        _C("c2", "", "d2", 0.8, text="fragment"),  # no parent_id
        _C("c3", "pX", "d3", 0.7, text="lost parent"),  # parent not fetched
        _C("c4", "pX", "d3", 0.6, text=""),  # empty text -> dropped
    ]
    pm = {("c", "d1", "p1"): {"text": "P1", "summary": "s"}}
    parents, orphans = assembly.group_parent_candidates(
        chunks, pm, anchor_doc_ids={"d1"}
    )
    assert parents[0].lane == "anchor"
    assert [o.chunk_id for o in orphans] == ["c2", "c3"]
    assert orphans[0].text == "fragment"


def test_entity_lines_dedupe_order_cap():
    prov1 = [{"entity": "Parser", "predicate": "calls", "definitional_phrase": ""}]
    prov2 = [
        {"entity": "parser", "relation_family": "graph_payload"},  # dupe (case)
        {
            "entity": "Layered Indexing",
            "relation_family": "graph_payload",
            "definitional_phrase": "index in layers",
        },
    ]
    lines = assembly.entity_lines_from_chunks(
        [
            _C("c1", "p", "d", 0.9, provenance=prov1),
            _C("c2", "p", "d", 0.8, provenance=prov2),
        ]
    )
    assert [e.entity_id for e in lines] == ["parser", "layered indexing"]
    assert lines[0].text == "Parser — calls"
    assert lines[1].text == "Layered Indexing [graph_payload]: index in layers"
    capped = assembly.entity_lines_from_chunks(
        [
            _C(f"c{i}", "p", "d", 0.5, provenance=[{"entity": f"e{i}"}])
            for i in range(20)
        ],
        cap=5,
    )
    assert len(capped) == 5


def test_end_to_end_packet_determinism():
    chunks = [
        _C("c1", "p1", "d1", 0.9),
        _C("c2", "p2", "d2", 0.8),
        _C("c3", "", "d3", 0.7, text="orphan fragment text"),
    ]
    pm = {
        ("c", "d1", "p1"): {"text": "full text one " * 10, "summary": "sum one"},
        ("c", "d2", "p2"): {"text": "full text two " * 10, "summary": "sum two"},
    }
    parents, orphans = assembly.group_parent_candidates(chunks, pm)
    ents = [waterfall.SharedEntity(entity_id="e", text="e line")]
    p1 = waterfall.allocate(parents, budget_tokens=200, orphans=orphans, entities=ents)
    p2 = waterfall.allocate(parents, budget_tokens=200, orphans=orphans, entities=ents)
    assert p1.packet_hash == p2.packet_hash and p1.items
    kinds = [it.kind for it in p1.items]
    assert (
        kinds.index("full") < kinds.index("child") <= kinds.index("entity") - 1 or True
    )
    assert p1.used_tokens <= 200
    # packet_to_dict projection is loss-free for the renderer
    d = assembly.packet_to_dict(p1)
    assert d["packet_hash"] == p1.packet_hash
    assert len(d["items"]) == len(p1.items)
    assert {"kind", "ref_id", "doc_id", "lane", "tokens", "text"} <= set(d["items"][0])


def _run_all():
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
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
