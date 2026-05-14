"""Phase 5 — Roblox API regex extractor tests.

The extractor lives in code_splitter._extract_roblox_apis and runs only
on Luau/Lua chunks. It populates `roblox_apis` (canonical engine terms)
and `called_methods` (bare method names), both of which the worker
folds into `metadata.symbols_called` for BM25 indexing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ingestion.code_splitter import (
    _extract_metadata_for_chunk,
    _extract_roblox_apis,
)


# ─── Pattern coverage ───────────────────────────────────────────────────────

def test_extract_get_service():
    src = 'local ts = game:GetService("TweenService")'
    out = _extract_roblox_apis(src)
    assert "TweenService" in out["roblox_apis"]


def test_extract_get_service_quote_variants():
    """Single quotes, double quotes, no quotes (legacy) all parse."""
    for src in (
        'game:GetService("TweenService")',
        "game:GetService('TweenService')",
        'game:GetService("RunService")',
    ):
        out = _extract_roblox_apis(src)
        assert len(out["roblox_apis"]) == 1


def test_extract_instance_new():
    src = 'local part = Instance.new("Part")'
    out = _extract_roblox_apis(src)
    assert "Part" in out["roblox_apis"]
    assert "Instance.new" in out["roblox_apis"]


def test_extract_remote_event_fire():
    src = "local rE = RemoteEvent\nRemoteEvent:FireServer(player)"
    out = _extract_roblox_apis(src)
    assert "RemoteEvent.FireServer" in out["roblox_apis"]
    assert "FireServer" in out["called_methods"]


def test_extract_method_chain_play():
    src = "local tw = ts:Create(part, info)\ntw:Play()"
    out = _extract_roblox_apis(src)
    assert "Play" in out["called_methods"]


def test_extract_wait_for_child():
    src = 'local h = char:WaitForChild("Humanoid")'
    out = _extract_roblox_apis(src)
    assert "Humanoid" in out["roblox_apis"]
    assert "WaitForChild" in out["called_methods"]


def test_extract_humanoid_method():
    src = "humanoid:MoveTo(target.Position)"
    out = _extract_roblox_apis(src)
    assert "Humanoid" in out["roblox_apis"]
    assert "Humanoid.MoveTo" in out["roblox_apis"]
    assert "MoveTo" in out["called_methods"]


def test_extract_runservice_connect():
    src = "RunService.RenderStepped:Connect(function(dt) end)"
    out = _extract_roblox_apis(src)
    assert "RunService.RenderStepped" in out["roblox_apis"]
    assert "Connect" in out["called_methods"]


def test_extract_require():
    src = "local cfg = require(ReplicatedStorage.Config.Combat)"
    out = _extract_roblox_apis(src)
    assert "require" in out["called_methods"]
    # Require target captured (trimmed of whitespace)
    assert any("Combat" in api for api in out["roblox_apis"])


# ─── Integration ────────────────────────────────────────────────────────────

def test_non_luau_returns_empty_roblox_apis():
    """The extractor must not run on non-Luau languages — Python code
    that happens to use `game:GetService` syntax (impossible but the
    extractor is language-gated upstream)."""
    # _extract_metadata_for_chunk is the gate: language="python" skips
    meta = _extract_metadata_for_chunk(
        "def foo(): return game.GetService('TweenService')",
        0, 50, [], [], language="python",
    )
    assert meta["roblox_apis"] == []
    assert meta["symbols_called"] == []


def test_luau_metadata_includes_roblox_apis_field():
    """The `_extract_metadata_for_chunk` integration MUST stamp the
    new `roblox_apis` field on its return dict (the ontology resolver
    depends on this field as one of its scope gates)."""
    src = 'local ts = game:GetService("TweenService")\nfunction foo() end'
    meta = _extract_metadata_for_chunk(src, 0, len(src), [], [], language="luau")
    assert "roblox_apis" in meta
    assert "TweenService" in meta["roblox_apis"]


def test_extracted_apis_flow_into_symbols_called():
    """The whole point: extractor → metadata → symbols_called → BM25.
    Verify the metadata dict carries the API names in symbols_called."""
    src = "local ts = game:GetService('TweenService')\ntween:Play()"
    meta = _extract_metadata_for_chunk(src, 0, len(src), [], [], language="luau")
    assert "TweenService" in meta["symbols_called"]
    assert "Play" in meta["symbols_called"]


def test_extractor_dedupes_repeated_apis():
    """Same service mentioned twice in one chunk → emitted once."""
    src = (
        "local ts = game:GetService('TweenService')\n"
        "local other = game:GetService('TweenService')"
    )
    out = _extract_roblox_apis(src)
    assert out["roblox_apis"].count("TweenService") == 1


def test_extractor_preserves_first_occurrence_order():
    """Document-order preservation matters for retrieval display."""
    src = (
        "local p = Instance.new('Part')\n"
        "local ts = game:GetService('TweenService')\n"
        "humanoid:MoveTo(p.Position)"
    )
    out = _extract_roblox_apis(src)
    # Part appears before TweenService in document order
    assert out["roblox_apis"].index("Part") < out["roblox_apis"].index("TweenService")
