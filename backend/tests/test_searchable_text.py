"""Phase 4.5 — searchable text augmentation tests.

Worker's `_searchable_text(chunk)` augments code-chunk text with metadata
tokens (symbols_defined / imports / symbols_called / file_path) before
feeding to the BM25 sparse encoder. This makes lexical search hit
structured-API names even when the chunk body uses local aliases.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ingestion.worker import _searchable_text


def _chunk(text="x", metadata=None):
    return SimpleNamespace(text=text, metadata=metadata or {})


def test_prose_chunk_unchanged():
    """No metadata → return text as-is."""
    c = _chunk(text="A paragraph of prose.", metadata={})
    assert _searchable_text(c) == "A paragraph of prose."


def test_prose_chunk_none_metadata():
    c = SimpleNamespace(text="hello", metadata=None)
    assert _searchable_text(c) == "hello"


def test_code_chunk_appends_symbols_defined():
    c = _chunk(
        text="function Combat.PunchAttack() end",
        metadata={"symbols_defined": ["Combat.PunchAttack", "Combat.Hitbox"]},
    )
    out = _searchable_text(c)
    assert out.startswith("function Combat.PunchAttack() end")
    assert "Combat.PunchAttack" in out
    assert "Combat.Hitbox" in out


def test_code_chunk_appends_imports():
    c = _chunk(
        text="local x = 1",
        metadata={"imports": ["game:GetService(TweenService)", "require(Spring)"]},
    )
    out = _searchable_text(c)
    assert "game:GetService(TweenService)" in out
    assert "require(Spring)" in out


def test_code_chunk_appends_symbols_called():
    c = _chunk(
        text="local ts = game:GetService('TweenService')",
        metadata={"symbols_called": ["Instance.new", "Color3.fromRGB"]},
    )
    out = _searchable_text(c)
    assert "Instance.new" in out
    assert "Color3.fromRGB" in out


def test_code_chunk_tokenizes_file_path():
    """File path becomes both the whole path AND each segment as a BM25
    term so lexical search hits 'CombatModule' even when the query
    doesn't include the full path."""
    c = _chunk(
        text="function PunchAttack() end",
        metadata={"file_path": "ReplicatedStorage/Combat/CombatModule.luau"},
    )
    out = _searchable_text(c)
    assert "ReplicatedStorage/Combat/CombatModule.luau" in out
    assert "ReplicatedStorage" in out
    assert "Combat" in out
    assert "CombatModule" in out
    assert "luau" in out


def test_code_chunk_combines_all_metadata():
    c = _chunk(
        text="function Combat.PunchAttack(player) end",
        metadata={
            "symbols_defined": ["Combat.PunchAttack"],
            "symbols_called": ["Instance.new"],
            "imports": ["game:GetService(TweenService)"],
            "file_path": "Combat/CombatModule.luau",
            "ast_signature": "function Combat.PunchAttack(player)",
        },
    )
    out = _searchable_text(c)
    assert "function Combat.PunchAttack(player) end" in out
    assert "Combat.PunchAttack" in out
    assert "Instance.new" in out
    assert "game:GetService(TweenService)" in out
    assert "CombatModule" in out


def test_caps_symbols_defined_at_thirty():
    """Don't bloat the BM25 input on chunks with absurd metadata."""
    c = _chunk(
        text="x",
        metadata={"symbols_defined": [f"sym_{i}" for i in range(200)]},
    )
    out = _searchable_text(c)
    appended = out.split("\n\n", 1)[1] if "\n\n" in out else ""
    assert appended.count("sym_") <= 30


def test_caps_imports_at_fifteen():
    c = _chunk(text="x", metadata={"imports": [f"imp_{i}" for i in range(50)]})
    out = _searchable_text(c)
    appended = out.split("\n\n", 1)[1] if "\n\n" in out else ""
    assert appended.count("imp_") <= 15


def test_empty_metadata_lists_dont_pollute():
    c = _chunk(
        text="real text",
        metadata={"symbols_defined": [], "imports": [], "symbols_called": []},
    )
    # No tokens → return text unchanged (no trailing whitespace junk)
    assert _searchable_text(c) == "real text"


def test_strips_whitespace_in_tokens():
    c = _chunk(text="x", metadata={"symbols_defined": ["  foo  ", "bar", ""]})
    out = _searchable_text(c)
    assert "foo" in out
    assert "bar" in out
    # The empty string shouldn't introduce double-spaces or weird artifacts
    assert "  " not in out.split("\n\n", 1)[1].strip()


# ─── Phase 5 — roblox_apis BM25 augmentation ────────────────────────────────


def test_code_chunk_appends_roblox_apis():
    """Phase 5: roblox_apis are indexed for BM25 even when not duplicated
    into symbols_called. This is the defensive path for backfill writers
    that populate roblox_apis only."""
    c = _chunk(
        text="local x = 1",
        metadata={"roblox_apis": ["TweenService", "Humanoid.MoveTo", "RunService"]},
    )
    out = _searchable_text(c)
    assert "TweenService" in out
    assert "Humanoid.MoveTo" in out
    assert "RunService" in out


def test_roblox_apis_dedupes_against_symbols_called():
    """When symbols_called already contains a Roblox API name, the
    roblox_apis pass should NOT duplicate it into the token stream.
    First occurrence wins."""
    c = _chunk(
        text="local x = 1",
        metadata={
            "symbols_called": ["TweenService", "FireServer"],
            "roblox_apis": ["TweenService", "Humanoid"],
        },
    )
    out = _searchable_text(c)
    # Both should appear, TweenService should appear exactly once in the
    # appended token stream.
    appended = out.split("\n\n", 1)[1] if "\n\n" in out else ""
    assert appended.count("TweenService") == 1
    assert "Humanoid" in appended  # roblox-only term still indexed
    assert "FireServer" in appended  # symbols_called still indexed


def test_caps_roblox_apis_at_thirty():
    """Defense against payload bloat — only the first 30 are indexed."""
    many = [f"RobloxApi{i}" for i in range(50)]
    c = _chunk(text="x", metadata={"roblox_apis": many})
    out = _searchable_text(c)
    assert "RobloxApi0" in out
    assert "RobloxApi29" in out
    assert "RobloxApi30" not in out
    assert "RobloxApi49" not in out


def test_roblox_apis_with_no_other_metadata():
    """A chunk with ONLY roblox_apis (no symbols_called, no defined, no
    imports) still gets its Roblox terms into the BM25 surface."""
    c = _chunk(
        text="some code",
        metadata={"roblox_apis": ["TweenService", "Animation"]},
    )
    out = _searchable_text(c)
    assert out.startswith("some code")
    assert "TweenService" in out
    assert "Animation" in out
