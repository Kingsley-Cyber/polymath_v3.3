"""Pt9g — UNIVERSAL_ENTITY_GLOSSES must cover every entry in
UNIVERSAL_ENTITY_SCHEMA + the sentinel.

The Pt9a oversight: I added `Software` and `Standard` to
UNIVERSAL_ENTITY_SCHEMA and to the Pydantic EntityType Literal, but
forgot to add corresponding entries to UNIVERSAL_ENTITY_GLOSSES.
The prompt renderer (_render_vocab_line) silently falls back to
bare-name output for missing entries, leaving the new types
undefined relative to their neighbors. The LLM saw
`Product=built offering` and bare `Software` in the same vocab line,
defaulted to Product for every software-flavored chunk for three
consecutive ingest cycles, and the Software bucket stayed empty.

Tests in this file pin the gloss-coverage invariant so the next
addition can't repeat the same mistake silently:

  • Every entry in UNIVERSAL_ENTITY_SCHEMA has a gloss
  • The sentinel ('other') has a gloss
  • Software's gloss disambiguates from Product
  • Standard's gloss disambiguates from Concept and Document
  • Product's gloss is now "not Software" — explicit disambiguation
    (was "built offering" pre-Pt9g)
  • The rendered vocab line includes Software=<gloss> and
    Standard=<gloss>
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
    UNIVERSAL_ENTITY_GLOSSES,
    UNIVERSAL_ENTITY_SCHEMA,
    SchemaContext,
    _render_vocab_constraint,
)


# ── Gloss coverage invariant ────────────────────────────────────────


def test_every_universal_entity_type_has_a_gloss():
    """Every entry in UNIVERSAL_ENTITY_SCHEMA must have a corresponding
    entry in UNIVERSAL_ENTITY_GLOSSES. The Pt9g lesson: adding a type
    to the schema without adding a gloss silently degrades extraction
    quality because the LLM has no way to distinguish the new type
    from its glossed neighbors."""
    missing = [t for t in UNIVERSAL_ENTITY_SCHEMA if t not in UNIVERSAL_ENTITY_GLOSSES]
    assert not missing, (
        f"Entity types added to UNIVERSAL_ENTITY_SCHEMA without "
        f"corresponding UNIVERSAL_ENTITY_GLOSSES entries: {missing}. "
        f"This was the Pt9a → Pt9g regression — the type appears in the "
        f"prompt but has no definition, so the LLM defaults to a "
        f"glossed neighbor (typically Product). Add a short, "
        f"disambiguating gloss to UNIVERSAL_ENTITY_GLOSSES."
    )


def test_sentinel_also_has_a_gloss():
    """The 'other' sentinel is rendered with [FALLBACK] tag but should
    still have a gloss so the LLM understands it's the last-resort
    bucket (the tag alone isn't self-explanatory)."""
    assert SchemaContext.ENTITY_SENTINEL in UNIVERSAL_ENTITY_GLOSSES


# ── Pt9g-specific entries ───────────────────────────────────────────


def test_software_gloss_distinguishes_from_product():
    """The Software gloss must convey 'libraries/frameworks/code',
    distinct from Product. Without this disambiguation, the LLM
    defaults to Product for software-flavored entities (TensorFlow,
    React, ML Kit, etc.) — verified empirically across 3 ingest
    cycles on Phase5_Luau_v4."""
    g = UNIVERSAL_ENTITY_GLOSSES["Software"]
    assert g, "Software has empty gloss"
    # The gloss must mention at least one of the canonical software
    # categories so the LLM has a referent for what counts:
    assert any(term in g.lower() for term in ("library", "framework", "runtime", "api", "language", "platform")), (
        f"Software gloss '{g}' doesn't mention any canonical software "
        f"category (library/framework/runtime/api/language/platform). "
        f"The LLM needs at least one such anchor to type TensorFlow/"
        f"React/MLKit as Software instead of Product."
    )


def test_standard_gloss_distinguishes_from_concept_and_document():
    """The Standard gloss must signal protocols/specs/formats. Without
    this, the LLM types JSON/HTTP/REST/SQL as Concept or Document."""
    g = UNIVERSAL_ENTITY_GLOSSES["Standard"]
    assert g, "Standard has empty gloss"
    assert any(term in g.lower() for term in ("protocol", "specification", "spec", "format", "schema")), (
        f"Standard gloss '{g}' doesn't mention any canonical spec "
        f"category (protocol/specification/format/schema)."
    )


def test_product_gloss_explicitly_excludes_software():
    """Pt9g tightened Product's gloss from 'built offering' to 'built
    offering not Software' so the disambiguation works both ways:
    Software's gloss tells the LLM what Software IS, and Product's
    gloss tells the LLM what Product ISN'T (Software). Mirrors the
    existing Artifact pattern ('tangible object not a Product')."""
    g = UNIVERSAL_ENTITY_GLOSSES["Product"]
    assert "not Software" in g or "non software" in g.lower() or "non-software" in g.lower(), (
        f"Product gloss '{g}' should explicitly exclude Software to "
        f"break the LLM's training prior that classifies things like "
        f"TensorFlow/React/MLKit as 'products' (Google products, etc.)"
    )


# ── End-to-end: rendered vocab line contains the new entries ───────


def test_rendered_vocab_constraint_contains_software_and_standard_glosses():
    """The vocab line the LLM actually reads must include both new
    types with their glosses. Bare-name rendering (Pt9a state) was
    the bug — Pt9g fixes it by ensuring _render_vocab_line picks up
    the gloss entries."""
    rendered = _render_vocab_constraint(
        list(UNIVERSAL_ENTITY_SCHEMA) + [SchemaContext.ENTITY_SENTINEL],
        UNIVERSAL_ENTITY_GLOSSES,
        SchemaContext.ENTITY_SENTINEL,
    )
    # Software entry rendered with its gloss (Name=gloss form).
    assert "Software=" in rendered, (
        f"Vocab line missing 'Software=' definition. Rendered:\n{rendered}"
    )
    # Standard entry rendered with its gloss.
    assert "Standard=" in rendered, (
        f"Vocab line missing 'Standard=' definition. Rendered:\n{rendered}"
    )
    # Bare 'Software|' or '|Software' (without gloss) must NOT appear —
    # that's the pre-Pt9g bug shape.
    assert "|Software|" not in rendered, (
        f"Software appearing without a gloss. Rendered:\n{rendered}"
    )
    assert "|Standard|" not in rendered, (
        f"Standard appearing without a gloss. Rendered:\n{rendered}"
    )


def test_no_gloss_collisions():
    """Different entity types must have distinct glosses. Two types
    with the same gloss would confuse the LLM about which bucket
    applies."""
    glosses = list(UNIVERSAL_ENTITY_GLOSSES.values())
    assert len(set(glosses)) == len(glosses), (
        f"Duplicate glosses detected — LLM cannot distinguish types "
        f"with identical descriptions. Glosses: {glosses}"
    )
