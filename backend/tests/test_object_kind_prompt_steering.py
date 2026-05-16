"""Pt9d — domain-aware object_kind prompt steering tests.

Pt9b shipped the extraction plumbing but the prompt asked for object_kind
as an optional field with a kitchen-sink list ("library|framework|disorder|
trait|therapy|protocol|format|..."). Without domain steering, the LLM saw
12+ scattered examples that didn't match any single chunk's domain and
emitted empty object_kind on 80%+ of entities — turning Pt9b's pipeline
investment into expensive storage overhead.

Pt9d binds the prompt's object_kind cheat sheet to the SchemaLens's
domain-specific `object_kinds` list. A software corpus that triggers the
software_engineering domain sees [Library, Framework, Application,
Service, API, Language, Platform, Engine, Tool, Database]. A psychology
corpus sees [Disorder, Syndrome, Trait, Therapy, Technique, Assessment,
Theory, Phenomenon]. The LLM gets a focused list, not noise.

These tests pin:
  • _render_object_kind_hint returns lens-specific kinds when lens provided
  • Falls back to a tight generic list when no lens is provided
  • Both prompts (JSONL + JSON_OBJECT) embed the hint
  • Three new domain rules (software_engineering, psychology,
    business_strategy) have non-empty object_kinds
  • Trigger detection actually fires for sample text from each domain
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
    _OBJECT_KIND_FALLBACK_HINTS,
    _render_object_kind_hint,
    build_json_object_prompt,
    build_user_prompt,
)
from services.ghost_b import SchemaLens  # noqa: E402
from services.ingestion.schema_lens import _DOMAIN_RULES  # noqa: E402


# ── _render_object_kind_hint behavior ───────────────────────────────


def test_render_object_kind_hint_uses_lens_kinds():
    lens = SchemaLens(
        lens_id="test",
        version=1,
        status="ready",
        source="test",
        corpus_domains=["software_engineering"],
        preferred_entity_types=["Software", "Method"],
        preferred_relations=["uses"],
        relation_aliases={},
        object_kinds=["Library", "Framework", "Service", "API"],
        canonical_families=[],
        confidence=0.9,
    )
    hint = _render_object_kind_hint(lens)
    assert hint == "Library|Framework|Service|API"


def test_render_object_kind_hint_falls_back_to_generic_when_no_lens():
    """No lens → tight generic list, NOT the original kitchen sink."""
    hint = _render_object_kind_hint(None)
    # The hint should match the fallback, joined by pipes.
    assert hint == "|".join(_OBJECT_KIND_FALLBACK_HINTS)
    # And the fallback list must stay short — the failure mode of Pt9b's
    # original prompt was a sprawling list that confused the LLM.
    assert len(_OBJECT_KIND_FALLBACK_HINTS) <= 8


def test_render_object_kind_hint_fallback_when_lens_object_kinds_empty():
    """A lens with no object_kinds (e.g. a corpus that matched no domain
    rule) still falls back to the generic list rather than emitting an
    empty hint."""
    lens = SchemaLens(
        lens_id="test",
        version=1,
        status="ready",
        source="test",
        corpus_domains=[],
        preferred_entity_types=[],
        preferred_relations=[],
        relation_aliases={},
        object_kinds=[],
        canonical_families=[],
        confidence=0.0,
    )
    hint = _render_object_kind_hint(lens)
    assert hint == "|".join(_OBJECT_KIND_FALLBACK_HINTS)


def test_render_object_kind_hint_accepts_dict_form():
    """SchemaLens stored in Mongo deserializes as a dict; the renderer
    must accept that shape too (not just the dataclass instance)."""
    lens_dict = {
        "lens_id": "test",
        "version": 1,
        "status": "ready",
        "source": "test",
        "object_kinds": ["Disorder", "Therapy", "Trait"],
    }
    hint = _render_object_kind_hint(lens_dict)
    assert hint == "Disorder|Therapy|Trait"


def test_render_object_kind_hint_truncates_to_10():
    """Keep the prompt budget under control — cap at 10 kinds even if the
    lens has more."""
    many_kinds = [f"Kind{i}" for i in range(20)]
    lens = SchemaLens(
        lens_id="test",
        version=1,
        status="ready",
        source="test",
        corpus_domains=[],
        preferred_entity_types=[],
        preferred_relations=[],
        relation_aliases={},
        object_kinds=many_kinds,
        canonical_families=[],
        confidence=0.0,
    )
    hint = _render_object_kind_hint(lens)
    assert hint.count("|") == 9  # 10 kinds → 9 separators


# ── JSONL prompt embeds the hint ────────────────────────────────────


def test_jsonl_prompt_uses_lens_object_kinds():
    """The JSONL prompt's entity line shape must include the lens-derived
    object_kind list, not the deprecated kitchen sink."""
    lens = SchemaLens(
        lens_id="test", version=1, status="ready", source="test",
        corpus_domains=["software_engineering"],
        preferred_entity_types=["Software"],
        preferred_relations=["uses"],
        relation_aliases={},
        object_kinds=["Library", "Framework", "API"],
        canonical_families=[],
        confidence=0.9,
    )
    schema = SchemaContext(
        entity_schema=["Software", "Method", "Concept"],
        relation_schema=["uses"],
        strict="soft",
    )
    prompt = build_user_prompt(
        chunk_id="c1", doc_id="d1", corpus_id="cor1",
        text="React is a library", schema=schema, schema_lens=lens,
        enable_facts=False,
    )
    # Lens-derived kinds appear:
    assert "Library|Framework|API" in prompt
    # The deprecated kitchen-sink phrase does NOT appear:
    assert "library|framework|disorder|trait|therapy|protocol|format" not in prompt


def test_jsonl_prompt_falls_back_to_generic_without_lens():
    schema = SchemaContext(
        entity_schema=["Software", "Method"],
        relation_schema=["uses"],
        strict="soft",
    )
    prompt = build_user_prompt(
        chunk_id="c1", doc_id="d1", corpus_id="cor1",
        text="sample", schema=schema, schema_lens=None,
        enable_facts=False,
    )
    fallback_hint = "|".join(_OBJECT_KIND_FALLBACK_HINTS)
    assert fallback_hint in prompt


# ── JSON_OBJECT prompt embeds the hint ──────────────────────────────


def test_json_object_prompt_uses_lens_object_kinds():
    lens = SchemaLens(
        lens_id="test", version=1, status="ready", source="test",
        corpus_domains=["psychology"],
        preferred_entity_types=["Concept", "Method"],
        preferred_relations=["causes"],
        relation_aliases={},
        object_kinds=["Disorder", "Therapy", "Trait", "Theory"],
        canonical_families=[],
        confidence=0.9,
    )
    schema = SchemaContext(
        entity_schema=["Concept", "Method", "Person"],
        relation_schema=["causes"],
        strict="soft",
    )
    prompt = build_json_object_prompt(
        chunk_id="c1", doc_id="d1", corpus_id="cor1",
        text="PTSD is treated with CBT.", schema=schema, schema_lens=lens,
        enable_facts=False,
        evidence_max_chars=200,
        fact_value_max_chars=80,
    )
    # The psychology-specific kinds appear in the entity shape:
    assert "Disorder|Therapy|Trait|Theory" in prompt
    # The deprecated kitchen-sink list is gone:
    assert "library | framework | disorder | therapy | protocol | format" not in prompt


# ── Domain rules: new domains exist with object_kinds ───────────────


def _find_domain(domain_name: str) -> dict | None:
    for rule in _DOMAIN_RULES:
        if rule.get("domain") == domain_name:
            return rule
    return None


def test_software_engineering_domain_exists_with_object_kinds():
    rule = _find_domain("software_engineering")
    assert rule is not None, (
        "software_engineering domain missing — corpora about React/Flutter/"
        "PostgreSQL/etc. need a domain that triggers a software lens"
    )
    kinds = rule.get("object_kinds") or []
    # Core software kinds must be present.
    assert "Library" in kinds
    assert "Framework" in kinds
    assert "Service" in kinds
    assert "API" in kinds


def test_psychology_domain_exists_with_object_kinds():
    rule = _find_domain("psychology")
    assert rule is not None, (
        "psychology domain missing — corpora about therapy / DSM / "
        "personality science need a domain that triggers a psych lens"
    )
    kinds = rule.get("object_kinds") or []
    assert "Disorder" in kinds
    assert "Therapy" in kinds
    assert "Trait" in kinds


def test_business_strategy_domain_exists_with_object_kinds():
    rule = _find_domain("business_strategy")
    assert rule is not None
    kinds = rule.get("object_kinds") or []
    assert "Strategy" in kinds
    assert "Metric" in kinds
    assert "BusinessModel" in kinds


# ── Trigger detection: sample text actually matches the new domains ─


def _sample_matches_domain(sample: str, domain_name: str) -> bool:
    """Lightweight check that ANY trigger word in the named domain appears
    in the lowered sample text. This is the same matching the lens-builder
    sampler uses — substring on lowered text."""
    rule = _find_domain(domain_name)
    if rule is None:
        return False
    sample_lc = sample.lower()
    triggers = rule.get("triggers") or []
    return any(trig.lower() in sample_lc for trig in triggers)


def test_software_engineering_triggers_on_react_text():
    sample = "React and Flutter are popular frameworks for building apps."
    assert _sample_matches_domain(sample, "software_engineering")


def test_software_engineering_triggers_on_python_stack():
    sample = "We use FastAPI and PostgreSQL with SQLAlchemy ORM."
    assert _sample_matches_domain(sample, "software_engineering")


def test_psychology_triggers_on_therapy_text():
    sample = "CBT and DBT are evidence-based psychotherapy modalities for PTSD."
    assert _sample_matches_domain(sample, "psychology")


def test_business_strategy_triggers_on_strategy_text():
    sample = "Your value proposition determines market positioning."
    assert _sample_matches_domain(sample, "business_strategy")


def test_non_matching_text_does_not_trigger():
    """Sanity check — a chunk about cooking shouldn't trigger software or
    psychology or business domains."""
    sample = "Caramelize the onions over medium heat until golden brown."
    assert not _sample_matches_domain(sample, "software_engineering")
    assert not _sample_matches_domain(sample, "psychology")
    assert not _sample_matches_domain(sample, "business_strategy")
