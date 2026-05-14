"""Phase 4.5 — graphify augmenter contract tests.

The augmenter is subprocess-based (calls `python -m graphify update`) and
opt-in. Tests mock the subprocess + filesystem so they don't depend on
graphify being installed in the test env, AND they cover the translation
pure-function with realistic graphify JSON shapes captured from a live run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.code_graph_augmenter import (
    GraphifyEnrichment,
    _strip_label,
    _translate,
    augment_code_chunks,
)


# Sample graphify graph.json captured from a real run, then rewritten so
# source_file follows the Pt 11.1 contract: `<chunk_id>.<ext>`. This lets
# _translate's chunk_calls bucketing reverse the mapping back to chunk_id.
_REAL_GRAPHIFY_OUTPUT = {
    "directed": False,
    "multigraph": False,
    "graph": {},
    "nodes": [
        {"label": "combat_chunk_a.luau", "file_type": "code", "source_file": "combat_chunk_a.luau",
         "source_location": "L1", "id": "combat_lua", "community": 1,
         "norm_label": "combat_chunk_a.luau"},
        {"label": "Combat.PunchAttack()", "file_type": "code", "source_file": "combat_chunk_a.luau",
         "source_location": "L3", "id": "combat_punchattack", "community": 1,
         "norm_label": "combat.punchattack()"},
        {"label": "Combat.Hitbox()", "file_type": "code", "source_file": "combat_chunk_a.luau",
         "source_location": "L7", "id": "combat_hitbox", "community": 1,
         "norm_label": "combat.hitbox()"},
        {"label": "sample_chunk_b.py", "file_type": "code", "source_file": "sample_chunk_b.py",
         "source_location": "L1", "id": "sample_py", "community": 0,
         "norm_label": "sample_chunk_b.py"},
        {"label": "normalize()", "file_type": "code", "source_file": "sample_chunk_b.py",
         "source_location": "L4", "id": "normalize", "community": 0,
         "norm_label": "normalize()"},
        {"label": "VectorStore", "file_type": "code", "source_file": "sample_chunk_b.py",
         "source_location": "L7", "id": "vectorstore", "community": 0,
         "norm_label": "vectorstore"},
        {"label": ".__init__()", "file_type": "code", "source_file": "sample_chunk_b.py",
         "source_location": "L8", "id": "vectorstore_init", "community": 0},
        {"label": ".insert()", "file_type": "code", "source_file": "sample_chunk_b.py",
         "source_location": "L10", "id": "vectorstore_insert", "community": 0},
    ],
    "links": [
        {"relation": "contains", "source": "combat_lua", "target": "combat_punchattack"},
        {"relation": "contains", "source": "combat_lua", "target": "combat_hitbox"},
        {"relation": "calls", "context": "call", "source_file": "combat_chunk_a.luau",
         "source_location": "L8", "source": "combat_hitbox", "target": "combat_punchattack"},
        {"relation": "contains", "source": "sample_py", "target": "normalize"},
        {"relation": "contains", "source": "sample_py", "target": "vectorstore"},
        {"relation": "calls", "context": "call", "source_file": "sample_chunk_b.py",
         "source_location": "L11", "source": "vectorstore_insert", "target": "normalize"},
    ],
}


# ─── _strip_label ───────────────────────────────────────────────────────────

def test_strip_label_removes_trailing_parens():
    assert _strip_label("foo()") == "foo"
    assert _strip_label("Combat.PunchAttack()") == "Combat.PunchAttack"
    assert _strip_label("normalize()") == "normalize"


def test_strip_label_removes_leading_dot_for_methods():
    # graphify emits class methods as ".__init__()" — Phase 4 stores them as "__init__"
    assert _strip_label(".__init__()") == "__init__"
    assert _strip_label(".insert()") == "insert"


def test_strip_label_handles_plain_class_names():
    assert _strip_label("VectorStore") == "VectorStore"
    assert _strip_label("Combat") == "Combat"


def test_strip_label_handles_whitespace():
    assert _strip_label("  foo()  ") == "foo"


# ─── _translate (pure function — the heart of the augmenter) ────────────────

def test_translate_extracts_call_edges():
    out = _translate(_REAL_GRAPHIFY_OUTPUT)
    assert isinstance(out, GraphifyEnrichment)
    # Two `calls` edges in the fixture: Hitbox→PunchAttack, insert→normalize
    src_dst = {(s, d) for s, d, _, _ in out.call_edges}
    assert ("Combat.Hitbox", "Combat.PunchAttack") in src_dst
    assert ("insert", "normalize") in src_dst


def test_translate_extracts_communities():
    out = _translate(_REAL_GRAPHIFY_OUTPUT)
    assert out.entity_communities["Combat.PunchAttack"] == 1
    assert out.entity_communities["Combat.Hitbox"] == 1
    assert out.entity_communities["normalize"] == 0
    assert out.entity_communities["VectorStore"] == 0


def test_translate_handles_empty_graph():
    out = _translate({"nodes": [], "links": []})
    assert out.is_empty
    assert out.entity_communities == {}
    assert out.call_edges == []


def test_translate_skips_self_loops_in_call_edges():
    payload = {
        "nodes": [{"id": "a", "label": "Foo()", "community": 0}],
        "links": [{"relation": "calls", "source": "a", "target": "a"}],
    }
    out = _translate(payload)
    assert out.call_edges == []


def test_translate_skips_links_with_unknown_endpoints():
    payload = {
        "nodes": [{"id": "a", "label": "Foo()", "community": 0}],
        "links": [{"relation": "calls", "source": "a", "target": "ghost"}],
    }
    out = _translate(payload)
    assert out.call_edges == []


def test_translate_ignores_contains_edges():
    # `contains` is file → function; we don't promote it to a graph edge
    # because Phase 4's MENTIONS already covers chunk → entity containment.
    out = _translate(_REAL_GRAPHIFY_OUTPUT)
    # 2 `calls` edges out of 6 total links
    assert len(out.call_edges) == 2
    assert out.edge_count == 6  # node/link count is the raw graphify total


def test_translate_call_edges_carry_source_file_and_location():
    out = _translate(_REAL_GRAPHIFY_OUTPUT)
    for src, dst, source_file, source_location in out.call_edges:
        if src == "Combat.Hitbox":
            # source_file is the temp filename graphify saw (Pt 11.1: <chunk_id>.<ext>)
            assert source_file == "combat_chunk_a.luau"
            assert source_location == "L8"


def test_translate_is_empty_property():
    assert GraphifyEnrichment.empty().is_empty is True
    out = _translate(_REAL_GRAPHIFY_OUTPUT)
    assert out.is_empty is False


# ─── augment_code_chunks (subprocess wrapper) ───────────────────────────────

def _make_code_chunk(
    text: str,
    language: str = "python",
    file_path: str | None = None,
    chunk_id: str | None = None,
):
    # Pt 11.1 — chunk_id is now required by _write_temp_inputs (filenames
    # are `<chunk_id>.<ext>` so graphify's source_file reverses back to
    # chunk_id for the symbols_called backfill).
    return SimpleNamespace(
        text=text,
        language=language,
        chunk_id=chunk_id or f"c_{abs(hash((text, language, file_path))) % 100000:05d}",
        metadata={"file_path": file_path} if file_path else {},
    )


def test_augment_empty_input_returns_empty():
    assert augment_code_chunks([]).is_empty


def test_augment_handles_subprocess_failure(monkeypatch, tmp_path):
    """Simulate graphify failing — augmenter must return empty, not raise."""
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stderr="graphify boom", stdout="")
    monkeypatch.setattr("services.code_graph_augmenter.subprocess.run", fake_run)
    chunks = [_make_code_chunk("def foo(): pass")]
    out = augment_code_chunks(chunks)
    assert out.is_empty


def test_augment_handles_missing_graphify(monkeypatch):
    """If `python -m graphify` isn't installed, subprocess raises
    FileNotFoundError — augmenter swallows it."""
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("graphify not found")
    monkeypatch.setattr("services.code_graph_augmenter.subprocess.run", fake_run)
    chunks = [_make_code_chunk("def foo(): pass")]
    out = augment_code_chunks(chunks)
    assert out.is_empty


def test_augment_handles_timeout(monkeypatch):
    import subprocess as sp
    def fake_run(*args, **kwargs):
        raise sp.TimeoutExpired(cmd="graphify", timeout=120)
    monkeypatch.setattr("services.code_graph_augmenter.subprocess.run", fake_run)
    chunks = [_make_code_chunk("def foo(): pass")]
    out = augment_code_chunks(chunks, timeout_seconds=1)
    assert out.is_empty


def test_augment_skips_when_no_writable_chunks():
    """All chunks lack a usable language — nothing to write."""
    chunks = [SimpleNamespace(text="hello", language=None, metadata={})]
    out = augment_code_chunks(chunks)
    assert out.is_empty


def test_augment_happy_path_with_mocked_graphify(monkeypatch, tmp_path):
    """End-to-end: subprocess mocked to write _REAL_GRAPHIFY_OUTPUT into
    the temp dir's graphify-out/graph.json. Translation runs for real."""
    captured = {}

    def fake_run(cmd, capture_output=True, text=True, timeout=120, check=False):
        # The subprocess call passes [..., 'update', tmpdir] — grab tmpdir
        tmpdir = Path(cmd[-1])
        captured["tmpdir"] = tmpdir
        captured["files"] = sorted(p.name for p in tmpdir.iterdir())
        graphify_out = tmpdir / "graphify-out"
        graphify_out.mkdir(parents=True, exist_ok=True)
        (graphify_out / "graph.json").write_text(json.dumps(_REAL_GRAPHIFY_OUTPUT))
        return SimpleNamespace(returncode=0, stderr="", stdout="ok")

    monkeypatch.setattr("services.code_graph_augmenter.subprocess.run", fake_run)

    chunks = [
        _make_code_chunk("function Combat.PunchAttack() end", "luau", "combat.lua"),
        _make_code_chunk("def normalize(v): return v", "python", "sample.py"),
    ]
    out = augment_code_chunks(chunks)
    assert not out.is_empty
    assert len(out.call_edges) == 2
    assert out.entity_communities["Combat.PunchAttack"] == 1

    # Verify the augmenter wrote chunks into the temp dir with the right extensions
    assert any(f.endswith(".lua") or f.endswith(".luau") for f in captured["files"])
    assert any(f.endswith(".py") for f in captured["files"])


def test_augment_writes_files_keyed_by_chunk_id(monkeypatch):
    """Pt 11.1 — _write_temp_inputs uses <chunk_id>.<ext> as filename so
    graphify's source_file output reverses cleanly back to chunk_id.
    Two chunks with the same file_path metadata don't collide because
    they have distinct chunk_ids."""
    written_files = []

    def fake_run(cmd, **kwargs):
        tmpdir = Path(cmd[-1])
        for p in tmpdir.iterdir():
            written_files.append(p.name)
        return SimpleNamespace(returncode=1, stderr="", stdout="")

    monkeypatch.setattr("services.code_graph_augmenter.subprocess.run", fake_run)

    chunks = [
        _make_code_chunk("def foo(): pass", "python", chunk_id="docA_0001"),
        _make_code_chunk("def bar(): pass", "python", chunk_id="docA_0002"),
    ]
    augment_code_chunks(chunks)
    assert sorted(written_files) == ["docA_0001.py", "docA_0002.py"]


def test_augment_skips_chunks_without_chunk_id(monkeypatch):
    """Chunks lacking chunk_id are silently skipped — the backfill
    contract depends on chunk_id being the filename stem."""
    written_files = []

    def fake_run(cmd, **kwargs):
        tmpdir = Path(cmd[-1])
        for p in tmpdir.iterdir():
            written_files.append(p.name)
        return SimpleNamespace(returncode=1, stderr="", stdout="")

    monkeypatch.setattr("services.code_graph_augmenter.subprocess.run", fake_run)
    bad = SimpleNamespace(text="def foo(): pass", language="python", chunk_id=None, metadata={})
    good = _make_code_chunk("def bar(): pass", "python", chunk_id="chunk_42")
    augment_code_chunks([bad, good])
    assert written_files == ["chunk_42.py"]


# ─── Pt 11.1 — chunk_calls bucketing ────────────────────────────────────────

def test_translate_populates_chunk_calls():
    """Pt 11.1 — `calls` edges are bucketed per-chunk_id via
    Path(source_file).stem so the worker can backfill symbols_called."""
    out = _translate(_REAL_GRAPHIFY_OUTPUT)
    # combat_chunk_a.luau → chunk_calls["combat_chunk_a"]
    assert "combat_chunk_a" in out.chunk_calls
    assert "Combat.PunchAttack" in out.chunk_calls["combat_chunk_a"]
    # sample_chunk_b.py → chunk_calls["sample_chunk_b"]
    assert "sample_chunk_b" in out.chunk_calls
    assert "normalize" in out.chunk_calls["sample_chunk_b"]


def test_translate_dedupes_calls_within_chunk():
    """If graphify reports a function called twice in the same chunk,
    the bucket only lists it once."""
    payload = {
        "nodes": [
            {"id": "src", "label": "Caller()"},
            {"id": "dst", "label": "TweenService"},
        ],
        "links": [
            {"relation": "calls", "source_file": "chunk_x.luau",
             "source_location": "L5", "source": "src", "target": "dst"},
            {"relation": "calls", "source_file": "chunk_x.luau",
             "source_location": "L12", "source": "src", "target": "dst"},
        ],
    }
    out = _translate(payload)
    assert out.chunk_calls["chunk_x"] == ["TweenService"]


def test_translate_ignores_calls_with_missing_source_file():
    """Defensive: a malformed graph.json where source_file is None
    should not populate chunk_calls (and shouldn't crash)."""
    payload = {
        "nodes": [
            {"id": "src", "label": "Caller()"},
            {"id": "dst", "label": "Callee()"},
        ],
        "links": [
            {"relation": "calls", "source_file": None,
             "source": "src", "target": "dst"},
            {"relation": "calls", "source_file": "",
             "source": "src", "target": "dst"},
        ],
    }
    out = _translate(payload)
    assert out.chunk_calls == {}
    # call_edges still populated (with empty source_file) — the
    # legacy data path doesn't filter on source_file presence.
    assert len(out.call_edges) == 2


def test_translate_chunk_calls_empty_on_empty_graph():
    out = _translate({"nodes": [], "links": []})
    assert out.chunk_calls == {}


def test_empty_enrichment_has_chunk_calls_dict():
    """GraphifyEnrichment.empty() must expose chunk_calls as an empty dict
    so downstream `.get(chunk_id, [])` calls work without isinstance guards."""
    from services.code_graph_augmenter import GraphifyEnrichment
    e = GraphifyEnrichment.empty()
    assert e.chunk_calls == {}
    assert e.chunk_calls.get("any_id", []) == []
