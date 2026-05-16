"""Pt9f — lens block must NOT render preferred_entity_types.

The schema_lens architecture was designed for single-domain corpora.
For heterogeneous libraries (521 books spanning software, psychology,
business, game design, ML, writing, decision theory, personal
development — verified against `book_index 03-22-2026.md`), the
per-corpus "prefer these N entity types" guidance line is harmful:

  - lens averages across all matched domain rules
  - cap=8 truncation arbitrarily strips entity types from the bias
    list (Phase5_Luau_v4 lost `Software` despite software_engineering
    domain matching)
  - every chunk then gets the same corpus-wide bias regardless of its
    own domain (React chunk + Myers-Briggs chunk get identical
    preference instructions)

Pt9f deletes the prompt rendering of `preferred_entity_types`.
The data field stays on `SchemaLens` (no schema migration needed)
but the LLM never sees it. Chunk text drives entity_type selection.

The vocab line ("entity_type one of: ...") still enumerates all 14
universal types, so the LLM has the full menu.

These tests pin the contract:
  • Lens block omits the preferred-entity-type bias line
  • Other lens fields still render (corpus_domains, object_kinds,
    relation_aliases, preferred_relations, canonical_families)
  • Lens dataclass field still exists (no schema migration regression)
"""
from __future__ import annotations

import sys
from types import ModuleType


# ── Auth-package stubs (same pattern as other unit tests) ──────────
def _install_stubs_if_missing() -> None:
    if "jose" not in sys.modules:
        try:
            import jose  # noqa: F401
        except ImportError:
            jose_mod = ModuleType("jose")

            class JWTError(Exception):
                pass

            class _Jwt:
                @staticmethod
                def encode(*_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

                @staticmethod
                def decode(*_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

            jose_mod.JWTError = JWTError
            jose_mod.jwt = _Jwt()
            sys.modules["jose"] = jose_mod

    if "passlib.context" not in sys.modules:
        try:
            import passlib.context  # noqa: F401
        except ImportError:
            passlib_mod = ModuleType("passlib")
            ctx_mod = ModuleType("passlib.context")

            class _CryptContext:
                def __init__(self, *a, **kw):
                    pass

                def hash(self, *_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

                def verify(self, *_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

            ctx_mod.CryptContext = _CryptContext
            passlib_mod.context = ctx_mod
            sys.modules["passlib"] = passlib_mod
            sys.modules["passlib.context"] = ctx_mod

    if "slowapi" not in sys.modules:
        try:
            import slowapi  # noqa: F401
        except ImportError:
            slowapi_mod = ModuleType("slowapi")
            util_mod = ModuleType("slowapi.util")

            class _Limiter:
                def __init__(self, *a, **kw):
                    pass

                def limit(self, *_a, **_kw):
                    def _decorator(fn):
                        return fn
                    return _decorator

            def _get_remote_address(_request):  # pragma: no cover
                return "0.0.0.0"

            slowapi_mod.Limiter = _Limiter
            util_mod.get_remote_address = _get_remote_address
            sys.modules["slowapi"] = slowapi_mod
            sys.modules["slowapi.util"] = util_mod


_install_stubs_if_missing()


from services.ghost_b import (  # noqa: E402
    SchemaContext,
    SchemaLens,
    _render_schema_lens_block,
    build_user_prompt,
    build_json_object_prompt,
)


def _ready_lens(**overrides) -> SchemaLens:
    defaults = dict(
        lens_id="lens-test",
        version=1,
        status="ready",
        source="test",
        corpus_domains=["product_prd", "software_engineering", "psychology"],
        preferred_entity_types=["Product", "Method", "Concept", "Document",
                                "Rule", "Artifact", "Person", "Organization"],
        preferred_relations=["uses", "implements", "depends_on"],
        relation_aliases={"powered by": "uses"},
        object_kinds=["Library", "Framework", "Therapy", "Disorder"],
        canonical_families=["software_engineering", "clinical_psychology"],
        confidence=0.85,
    )
    defaults.update(overrides)
    return SchemaLens(**defaults)


# ── Pt9f core: the bias line is gone ────────────────────────────────


def test_lens_block_does_not_render_preferred_entity_types():
    """The bias line is the single load-bearing change. Any reintroduction
    of this rendering would re-break heterogeneous libraries."""
    block = _render_schema_lens_block(_ready_lens())
    # Both phrasings of the deprecated bias line:
    assert "prefer these approved entity_type values" not in block
    # Belt-and-suspenders — the specific 8-list MUST NOT show up as a
    # comma-joined preference, regardless of intro wording:
    assert "Product, Method, Concept, Document, Rule, Artifact, Person, Organization" not in block


def test_lens_block_still_renders_corpus_domains():
    block = _render_schema_lens_block(_ready_lens())
    assert "likely corpus domains" in block
    assert "product_prd" in block
    assert "software_engineering" in block


def test_lens_block_still_renders_object_kinds():
    """Object_kind hints stay — they help granularity and don't override
    entity_type. Pt9b/d's domain-aware object_kind steering depends on
    this rendering path."""
    block = _render_schema_lens_block(_ready_lens())
    assert "object kinds to notice" in block
    assert "Library" in block
    assert "Framework" in block


def test_lens_block_still_renders_preferred_relations():
    """Relation preferences stay for now. Comment in code documents
    why (gentler bias ratio: 30 vocab / 10 cap vs entity 14 / 8)."""
    block = _render_schema_lens_block(_ready_lens())
    assert "prefer these approved predicates" in block
    assert "uses" in block


def test_lens_block_still_renders_relation_aliases():
    block = _render_schema_lens_block(_ready_lens())
    assert "relation phrase aliases" in block
    assert "powered by -> uses" in block


def test_lens_block_still_renders_canonical_families():
    block = _render_schema_lens_block(_ready_lens())
    assert "concept families to notice" in block
    assert "software_engineering" in block


# ── End-to-end: both prompt builders drop the bias too ──────────────


def test_jsonl_prompt_omits_entity_type_bias():
    """The full JSONL prompt for a chunk must not contain the
    deprecated bias guidance line."""
    schema = SchemaContext(
        entity_schema=["Person", "Organization", "Concept", "Method",
                       "Product", "Software", "Document", "Standard"],
        relation_schema=["uses", "implements"],
        strict="soft",
    )
    prompt = build_user_prompt(
        chunk_id="c1", doc_id="d1", corpus_id="cor1",
        text="React is a library. PTSD is a disorder.",
        schema=schema,
        schema_lens=_ready_lens(),
        enable_facts=False,
    )
    assert "prefer these approved entity_type values" not in prompt
    # And the universal vocab line IS still present so the LLM has the
    # full menu — verified via the existing pre-Pt9b enum string:
    assert "Software" in prompt
    assert "Standard" in prompt


def test_json_object_prompt_omits_entity_type_bias():
    schema = SchemaContext(
        entity_schema=["Person", "Organization", "Concept", "Method",
                       "Product", "Software", "Document", "Standard"],
        relation_schema=["uses", "implements"],
        strict="soft",
    )
    prompt = build_json_object_prompt(
        chunk_id="c1", doc_id="d1", corpus_id="cor1",
        text="React is a library. PTSD is a disorder.",
        schema=schema,
        schema_lens=_ready_lens(),
        enable_facts=False,
        evidence_max_chars=200,
        fact_value_max_chars=80,
    )
    assert "prefer these approved entity_type values" not in prompt
    assert "Software" in prompt
    assert "Standard" in prompt


# ── SchemaLens dataclass: field still exists (no schema regression) ─


def test_schema_lens_dataclass_still_has_preferred_entity_types_field():
    """The bias line is gone from the prompt. The dataclass field is NOT
    removed — stored lenses in Mongo continue to round-trip through
    SchemaLens.from_dict / to_dict without losing data. We just don't
    consume the field in prompt rendering."""
    lens = _ready_lens(preferred_entity_types=["Software", "Method"])
    assert lens.preferred_entity_types == ["Software", "Method"]
    # Round-trip via to_dict / from_dict — pinned existing behavior:
    payload = lens.to_dict()
    assert payload["preferred_entity_types"] == ["Software", "Method"]
    rehydrated = SchemaLens.from_dict(payload)
    assert rehydrated.preferred_entity_types == ["Software", "Method"]
