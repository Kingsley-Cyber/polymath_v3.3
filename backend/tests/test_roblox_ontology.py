"""Phase 5 Gate 1 — scoped Roblox ontology tests.

The resolver lives in `services.graph.roblox_ontology` and MUST NOT
mutate the global entity_type_overrides.json. Returns a Roblox-specific
type only when chunk.language is Luau/Lua OR chunk.metadata.roblox_apis
is non-empty.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.graph.roblox_ontology import (
    _ROBLOX_DOMAINS,
    _ROBLOX_ENTITY_TYPES,
    resolve_code_entity_type,
    roblox_domain_for,
)


def _chunk(language=None, metadata=None):
    return SimpleNamespace(language=language, metadata=metadata or {})


# ─── Scope gate ─────────────────────────────────────────────────────────────

def test_luau_chunk_resolves_robloxclass():
    out = resolve_code_entity_type("Humanoid", _chunk(language="luau"))
    assert out == "RobloxClass"


def test_lua_chunk_resolves_robloxservice():
    """Plain Lua chunks also count — they're indistinguishable from Luau
    at the lookup level."""
    out = resolve_code_entity_type("TweenService", _chunk(language="lua"))
    assert out == "RobloxService"


def test_python_chunk_returns_none():
    """The critical no-pollution assertion: a Python chunk with a
    Humanoid variable name MUST NOT get retyped as RobloxClass.
    This is the whole point of the scope gate."""
    out = resolve_code_entity_type("Humanoid", _chunk(language="python"))
    assert out is None


def test_prose_chunk_returns_none():
    """A book/transcript chunk that happens to mention 'Humanoid'
    (the word) doesn't get re-typed either — Ghost B + the schema
    lens handle prose extraction, not this resolver."""
    out = resolve_code_entity_type("Humanoid", _chunk(language=None))
    assert out is None


def test_chunk_with_roblox_apis_metadata_resolves():
    """Alternative gate: if a chunk has roblox_apis metadata (e.g., a
    markdown fence detected as code-block with the extractor populating
    it), resolution fires even when language is empty."""
    out = resolve_code_entity_type(
        "TweenService",
        _chunk(language=None, metadata={"roblox_apis": ["TweenService"]}),
    )
    assert out == "RobloxService"


def test_unknown_symbol_returns_none_on_luau_chunk():
    """Don't make up types. `foobar` on a Luau chunk → None, not
    'RobloxClass'."""
    out = resolve_code_entity_type("foobar", _chunk(language="luau"))
    assert out is None


def test_empty_name_returns_none():
    assert resolve_code_entity_type("", _chunk(language="luau")) is None
    assert resolve_code_entity_type(None, _chunk(language="luau")) is None  # type: ignore[arg-type]


# ─── Domain lookups ─────────────────────────────────────────────────────────

def test_roblox_domain_for_animation_terms():
    assert roblox_domain_for("TweenService") == "AnimationSystem"
    assert roblox_domain_for("Animation") == "AnimationSystem"


def test_roblox_domain_for_network_terms():
    assert roblox_domain_for("RemoteEvent") == "NetworkReplication"
    assert roblox_domain_for("BindableFunction") == "NetworkReplication"


def test_roblox_domain_for_unknown_returns_none():
    assert roblox_domain_for("foobar") is None
    assert roblox_domain_for("") is None


# ─── No-pollution assertion ─────────────────────────────────────────────────

def test_no_pollution_of_overrides_json():
    """Critical regression guard. entity_type_overrides.json MUST NOT
    contain any Roblox entries. If it does, the scoped design has been
    bypassed and we're polluting non-Roblox corpora globally."""
    overrides_path = (
        Path(__file__).resolve().parents[1]
        / "services" / "graph" / "entity_type_overrides.json"
    )
    if not overrides_path.exists():
        pytest.skip("entity_type_overrides.json not present in this env")
    data = json.loads(overrides_path.read_text(encoding="utf-8"))
    # No Roblox engine term should appear in the global override file.
    for roblox_name in _ROBLOX_ENTITY_TYPES:
        assert roblox_name not in data, (
            f"{roblox_name!r} leaked into entity_type_overrides.json — "
            f"this would re-type non-Roblox uses of the same name "
            f"(books, Python, JS, prose). Phase 5 must keep this scoped."
        )


def test_v1_table_excludes_ambiguous_names():
    """Generic/ambiguous names that collide with non-Roblox semantics
    are deliberately excluded from v1. If someone adds them later,
    update this test AND add a stronger contextual gate."""
    forbidden = ["Spring", "Value", "New", "Service", "Controller",
                 "Component", "Promise", "t", "string", "function", "table"]
    for name in forbidden:
        assert name not in _ROBLOX_ENTITY_TYPES, (
            f"{name!r} is too ambiguous for v1 — it collides with non-Roblox "
            f"semantics even inside Luau corpora (e.g., Fusion's Spring "
            f"physics module). See plan: Phase 5 explicitly excludes this."
        )
