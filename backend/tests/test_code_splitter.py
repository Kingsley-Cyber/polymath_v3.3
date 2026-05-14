"""Tests for services.ingestion.code_splitter — embedder-safe AST packing.

The splitter wraps tree_sitter_language_pack.process() (v1.8 high-level API).
We test the contract: every returned slice fits the token cap, AND metadata
(symbols_defined / imports / ast_signature) is populated when the language is
supported. On failure (unsupported language, pack missing, AST can't meet
budget) we expect the sentinel [(source, {})] so the caller can hard-split.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Allow `from services...` style imports when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ingestion import code_splitter
from services.ingestion.code_splitter import pack, _count_tokens


# ─── Happy path: small inputs fit in one slice with metadata ────────────────

def test_pack_below_cap_one_slice_with_metadata():
    src = "def foo(x):\n    return x + 1\n"
    out = pack(src, "python", 960)
    assert len(out) == 1
    text, meta = out[0]
    assert text == src
    assert "foo" in meta.get("symbols_defined", [])
    assert meta.get("ast_signature", "").startswith("def foo")
    assert _count_tokens(text) <= 960


def test_pack_extracts_python_imports():
    src = "import numpy as np\nfrom heapq import heappush\n\ndef f():\n    return 1\n"
    out = pack(src, "python", 960)
    assert len(out) == 1
    _, meta = out[0]
    imports = meta.get("imports", [])
    assert any("numpy" in s for s in imports)
    assert any("heapq" in s for s in imports)


def test_pack_python_class_methods_in_symbols_defined():
    src = """class Foo:
    def __init__(self):
        self.x = 1
    def add(self, n):
        return n + 1

def standalone():
    return 0
"""
    out = pack(src, "python", 960)
    assert len(out) == 1
    _, meta = out[0]
    defined = meta.get("symbols_defined", [])
    assert "Foo" in defined
    assert "standalone" in defined
    # __init__ and add are nested under the class — pack returns them too,
    # since SymbolKind.function tags any function regardless of nesting.
    assert "__init__" in defined or "add" in defined


# ─── Heavy path: oversized inputs split at AST boundaries ────────────────────

def test_pack_three_functions_split_at_function_boundaries():
    # Three reasonably-sized functions, tight token cap → expect ≥ 2 slices.
    src = "\n".join(
        f"def fn_{i}(x):\n    " + "\n    ".join(f"y = x + {j}" for j in range(20)) + "\n    return y\n"
        for i in range(3)
    )
    cap = 80  # well below 3 functions' combined token count
    out = pack(src, "python", cap)
    # Sentinel-shape (single-slice signaling failure) is acceptable; if not,
    # every slice must fit the cap.
    if len(out) == 1 and out[0][1] == {}:
        # Pack couldn't meet the budget; caller would hard-split.
        return
    assert len(out) >= 2
    for text, _ in out:
        assert _count_tokens(text) <= cap


def test_pack_postcondition_holds():
    # Any source/cap combo must either return every slice ≤ cap, OR the
    # single-source sentinel. Never a half-met contract.
    src = "def f():\n    return 1\n" * 50  # repetitive, oversized
    cap = 100
    out = pack(src, "python", cap)
    if len(out) == 1 and out[0][1] == {}:
        # Sentinel — caller's hard-split path. Valid.
        return
    for text, _ in out:
        assert _count_tokens(text) <= cap


# ─── Fallback paths ──────────────────────────────────────────────────────────

def test_pack_unknown_language_returns_input():
    src = "MOVE 1 TO X.\nADD X TO Y."
    out = pack(src, "cobol", 10)  # tight cap forces failure path
    assert out == [(src, {})]


def test_pack_empty_language_returns_input_when_oversized():
    src = "x" * 5000
    out = pack(src, "", 50)
    assert out == [(src, {})]


def test_pack_missing_pack_returns_input(monkeypatch):
    # Force the lazy pack getter to claim unavailability.
    monkeypatch.setattr(code_splitter, "_pack", lambda: None)
    src = "def f():\n    return 1\n" * 50
    out = pack(src, "python", 50)
    assert out == [(src, {})]


def test_pack_empty_source_returns_empty_list():
    assert pack("", "python", 960) == []
    assert pack("   \n\t  ", "python", 960) == []


# ─── Fence preservation ──────────────────────────────────────────────────────

def test_pack_preserves_fence_wrapper_when_oversized():
    body_lines = "\n".join(f"def fn_{i}(): return {i}" for i in range(50))
    src = f"```python\n{body_lines}\n```"
    out = pack(src, "python", 80)
    if len(out) == 1 and out[0][1] == {}:
        return  # sentinel — pack signaled failure
    for text, _ in out:
        assert text.startswith("```")
        assert text.rstrip().endswith("```")


# ─── Real fixture — sample.py ────────────────────────────────────────────────

def test_pack_sample_py_fixture_metadata():
    fixture = Path(__file__).parent / "fixtures" / "sample.py"
    if not fixture.exists():
        pytest.skip("sample.py fixture missing")
    src = fixture.read_text(encoding="utf-8")
    out = pack(src, "python", 960)
    # The fixture is ~250 tokens — easily fits 960. One slice expected.
    assert len(out) == 1
    text, meta = out[0]
    defined = set(meta.get("symbols_defined", []))
    # At minimum: VectorStore class + top-level helpers
    assert "VectorStore" in defined
    assert "normalize" in defined
    assert "cosine_similarity" in defined
    assert any("numpy" in i for i in meta.get("imports", []))
