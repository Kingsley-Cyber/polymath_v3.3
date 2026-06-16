"""Pt10 — bundle tests for citation gate, HyDE default, model_name validation.

Three separate fixes shipped together as Pt10. Each addresses a real
production failure observed on the Phase5_Luau_v4 corpus:

  Pt10a — citation filter
    Failure: Fowler book's bibliography produced entities like
    "Alpert, Brown and Woolf. Design Patterns Smalltalk Companion.
    Addison-Wesley, 1998." typed as Person. Author surnames like
    "Kai Yu" and "Knight and Dai" leaked from citation contexts.
    Fix: heuristic gate before evidence validation — drops anything
    matching (year + punctuation + (publisher OR length>=8 words)).

  Pt10c — HyDE on balanced query profile
    Failure: cross-domain queries like "how does generative AI apply
    to urban planning" produced wrong-domain retrieval because raw
    query embeddings matched on surface tokens ("design").
    Fix: balanced profile now has hyde_enabled=True (was False).
    Hypothetical-answer generation routes retrieval to actually-
    relevant docs.

  Pt10d — model_name save-time validation
    Failure: production saw two pool entries with bad model_names
    ("deepseek/admin" and "deepseek/DeepSeek-V4-Flash"). Both produced
    400 storms at every chat / synthesis call. Both were typed by hand
    via the UI which had no validation.
    Fix: regex-based validation in model_pool.create / update. Catches
    pool/account name typos and title-case marketing names. Escape
    hatch via extra_params.skip_model_validation=true.
"""
from __future__ import annotations

import sys
from types import ModuleType


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
                def encode(*_a, **_kw):
                    raise RuntimeError("stub")

                @staticmethod
                def decode(*_a, **_kw):
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
                def __init__(self, *a, **kw): pass
                def hash(self, *_a, **_kw): raise RuntimeError("stub")
                def verify(self, *_a, **_kw): raise RuntimeError("stub")

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
                def __init__(self, *a, **kw): pass
                def limit(self, *_a, **_kw):
                    def _d(fn): return fn
                    return _d

            def _get_remote_address(_request): return "0.0.0.0"

            slowapi_mod.Limiter = _Limiter
            util_mod.get_remote_address = _get_remote_address
            sys.modules["slowapi"] = slowapi_mod
            sys.modules["slowapi.util"] = util_mod


_install_stubs_if_missing()


import pytest  # noqa: E402


# ── Pt10a — citation entity filter ──────────────────────────────────


from services.ghost_b import _looks_like_citation  # noqa: E402


def test_citation_full_fowler_reference_is_detected():
    """The exact failure case from production — a Design Patterns book
    citation extracted as a Person entity."""
    name = "Alpert, Brown and Woolf. Design Patterns Smalltalk Companion. Addison-Wesley, 1998."
    assert _looks_like_citation(name) is True


def test_citation_with_oreilly_publisher_detected():
    assert _looks_like_citation(
        "Moroney, L. AI and Machine Learning for On-Device Development. O'Reilly Media, 2021."
    ) is True


def test_citation_with_springer_detected():
    assert _looks_like_citation(
        "Yang, Q., Wang, Z. PsychoGAT: A Novel Psychological Measurement Paradigm. Springer, 2024."
    ) is True


def test_long_text_with_year_no_publisher_detected():
    """8+ words + year + punctuation = looks like a citation even
    without a known publisher."""
    assert _looks_like_citation(
        "Smith, J., Doe, A., Roe, B. Some Paper About Something Important. Journal, 2020."
    ) is True


def test_real_person_with_year_not_dropped():
    """Person name with parenthetical year (e.g. 'Foo Bar (1998)') is
    short and shouldn't trip the citation gate."""
    assert _looks_like_citation("Foo Bar 1998") is False


def test_real_concept_with_no_year_passes():
    assert _looks_like_citation("Domain-Driven Design") is False
    assert _looks_like_citation("Generative AI") is False
    assert _looks_like_citation("TensorFlow Lite") is False


def test_short_name_with_year_not_a_citation():
    """A 3-word entity with a year (e.g. 'iPhone 1998') is not a
    bibliographic citation."""
    assert _looks_like_citation("iPhone 1998") is False
    assert _looks_like_citation("World Cup 2018") is False


def test_empty_input_safe():
    assert _looks_like_citation("") is False
    assert _looks_like_citation(None) is False  # type: ignore[arg-type]


# ── Pt10c — HyDE default ────────────────────────────────────────────


import services.chat_orchestrator as chat_orchestrator_module  # noqa: E402
from models.schemas import ChatRequest, ModelOverrides, SourceChunk  # noqa: E402
from services.chat_orchestrator import (  # noqa: E402
    ChatOrchestrator,
    _chat_source_is_low_value,
    _should_skip_hyde_for_query,
)


def test_balanced_profile_has_hyde_enabled():
    """The fix for cross-domain query retrieval. Pre-Pt10c this was
    False; that produced wrong-domain results when queries had
    overloaded surface tokens like 'design'."""
    presets = ChatOrchestrator._QUERY_PROFILE_PRESETS
    assert presets["balanced"]["hyde_enabled"] is True
    assert presets["balanced"]["rerank_top_n"] == 24


def test_fast_profile_still_has_hyde_disabled():
    """The Pt10c change is scoped to balanced. Fast stays cheap.
    Thorough was already True."""
    presets = ChatOrchestrator._QUERY_PROFILE_PRESETS
    assert presets["fast"]["hyde_enabled"] is False


def test_thorough_profile_unchanged():
    presets = ChatOrchestrator._QUERY_PROFILE_PRESETS
    assert presets["thorough"]["hyde_enabled"] is True
    assert presets["thorough"]["retrieval_k"] == 60
    assert presets["thorough"]["rerank_top_n"] == 32


@pytest.mark.asyncio
async def test_profile_rerank_caps_resolve_from_presets():
    orchestrator = ChatOrchestrator()

    balanced = await orchestrator._resolve_query_profile(
        ChatRequest(message="remoteevent validation")
    )
    thorough = await orchestrator._resolve_query_profile(
        ChatRequest(
            message="remoteevent validation",
            overrides=ModelOverrides(query_profile="thorough"),
        )
    )

    assert balanced["rerank_top_n"] == 24
    assert thorough["rerank_top_n"] == 32


def test_hyde_skips_source_constrained_direct_support_queries():
    query = (
        "Based on the retrieved excerpts from Fowler's Patterns of Enterprise "
        "Application Architecture and Myers/Briggs' Gifts Differing, identify "
        "any defensible intersection. Distinguish direct textual support from "
        "inferred design recommendations."
    )
    assert _should_skip_hyde_for_query(query) is True


def test_hyde_skips_specific_definition_relation_queries():
    query = "What is NLP and how does Python relate to it?"
    assert _should_skip_hyde_for_query(query) is True


def test_chat_evidence_filter_rejects_frontmatter_noise():
    noisy = SourceChunk(
        chunk_id="frontmatter",
        parent_id="frontmatter",
        doc_id="doc-frontmatter",
        corpus_id="corpus",
        text=(
            "## Join our book's Discord space\n"
            "# Table of Contents\n"
            "1. Introduction to Python and Code Editors\n"
        ),
        score=0.9,
        source_tier="tier_a",
    )
    substantive = SourceChunk(
        chunk_id="body",
        parent_id="body",
        doc_id="doc-body",
        corpus_id="corpus",
        text="Python code examples show how natural language processing models tokenize text.",
        score=0.9,
        source_tier="tier_a",
    )

    assert _chat_source_is_low_value(noisy, "What is NLP and how does Python relate to it?")
    assert not _chat_source_is_low_value(
        substantive,
        "What is NLP and how does Python relate to it?",
    )


def test_hyde_stays_available_for_open_cross_domain_discovery():
    query = "How could generative AI methods apply to urban planning?"
    assert _should_skip_hyde_for_query(query) is False


@pytest.mark.asyncio
async def test_source_constrained_profile_default_hyde_is_skipped(monkeypatch):
    query = (
        "Based on the retrieved excerpts from Fowler's Patterns of Enterprise "
        "Application Architecture and Myers/Briggs' Gifts Differing, identify "
        "any defensible intersection. Distinguish direct textual support from "
        "inferred design recommendations."
    )
    request = ChatRequest(
        message=query,
        overrides=ModelOverrides(query_profile="thorough"),
    )
    orchestrator = ChatOrchestrator()

    profile = await orchestrator._resolve_query_profile(request)
    assert profile["hyde_enabled"] is True
    assert profile["hyde_explicit"] is False

    async def fail_complete_sync(**_kwargs):
        raise AssertionError("profile-default HyDE should be skipped")

    monkeypatch.setattr(
        chat_orchestrator_module.llm_service,
        "complete_sync",
        fail_complete_sync,
    )

    retrieval_query, applied = await orchestrator._apply_hyde(
        request,
        hyde_explicit=profile["hyde_explicit"],
    )

    assert retrieval_query == query
    assert applied is False


@pytest.mark.asyncio
async def test_source_constrained_explicit_hyde_toggle_is_honored(monkeypatch):
    query = (
        "Based on the retrieved excerpts from Fowler's Patterns of Enterprise "
        "Application Architecture and Myers/Briggs' Gifts Differing, identify "
        "any defensible intersection. Distinguish direct textual support from "
        "inferred design recommendations."
    )
    request = ChatRequest(
        message=query,
        overrides=ModelOverrides(
            query_profile="thorough",
            hyde_enabled=True,
            hyde_model="test/hyde",
        ),
    )
    orchestrator = ChatOrchestrator()
    calls = {"count": 0}

    async def fake_complete_sync(**_kwargs):
        calls["count"] += 1
        return "A hypothetical answer for retrieval."

    monkeypatch.setattr(
        chat_orchestrator_module.llm_service,
        "complete_sync",
        fake_complete_sync,
    )
    chat_orchestrator_module._HYDE_FAILURE_CACHE.clear()

    profile = await orchestrator._resolve_query_profile(request)
    assert profile["hyde_enabled"] is True
    assert profile["hyde_explicit"] is True

    retrieval_query, applied = await orchestrator._apply_hyde(
        request,
        hyde_explicit=profile["hyde_explicit"],
    )

    assert retrieval_query == "A hypothetical answer for retrieval."
    assert applied is True
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_hyde_without_dedicated_model_inherits_active_chat_model(monkeypatch):
    request = ChatRequest(
        message="How could retrieval tables improve AWS architecture study notes?",
        overrides=ModelOverrides(
            hyde_enabled=True,
            model="deepseek/deepseek-v4-flash",
        ),
    )
    orchestrator = ChatOrchestrator()
    calls = {"model": None}

    async def no_hyde_pool(_user_id, kind):
        assert kind == "hyde"
        return None

    async def fake_complete_sync(**kwargs):
        calls["model"] = kwargs.get("model")
        return "A hypothetical AWS architecture answer for retrieval."

    monkeypatch.setattr(chat_orchestrator_module, "resolve_query_model_kind", no_hyde_pool)
    monkeypatch.setattr(chat_orchestrator_module.settings, "HYDE_MODEL", "env/hyde")
    monkeypatch.setattr(
        chat_orchestrator_module.llm_service,
        "complete_sync",
        fake_complete_sync,
    )
    chat_orchestrator_module._HYDE_FAILURE_CACHE.clear()

    retrieval_query, applied = await orchestrator._apply_hyde(
        request,
        user_id="user-1",
        hyde_explicit=True,
        fallback_model=request.overrides.model,
    )

    assert retrieval_query == "A hypothetical AWS architecture answer for retrieval."
    assert applied is True
    assert calls["model"] == "deepseek/deepseek-v4-flash"


# ── Pt10d — model_name validation ───────────────────────────────────


from services.model_pool import (  # noqa: E402
    InvalidModelNameError,
    validate_model_name,
)


def test_admin_pool_name_typo_blocked():
    """The exact failure observed on the live system. User typed pool
    name into model field. Must raise."""
    with pytest.raises(InvalidModelNameError, match="pool/account name"):
        validate_model_name(provider="deepseek", model_name="admin")
    with pytest.raises(InvalidModelNameError, match="pool/account name"):
        validate_model_name(provider="deepseek", model_name="deepseek/admin")


def test_titlecase_capitalization_blocked_with_lowercase_hint():
    """Second observed failure — DeepSeek-V4-Flash typed instead of
    deepseek-v4-flash."""
    with pytest.raises(InvalidModelNameError, match="wrong capitalization"):
        validate_model_name(provider="deepseek", model_name="deepseek/DeepSeek-V4-Flash")
    with pytest.raises(InvalidModelNameError, match="wrong capitalization"):
        validate_model_name(provider="deepseek", model_name="DeepSeek-V4-Flash")


def test_valid_deepseek_model_names_pass():
    # All four production model ids must validate.
    for name in (
        "deepseek/deepseek-chat",
        "deepseek/deepseek-reasoner",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "deepseek-chat",  # bare form
        "deepseek-v4-flash",
    ):
        # Should not raise.
        validate_model_name(provider="deepseek", model_name=name)


def test_valid_openai_model_names_pass():
    for name in (
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "openai/o1",
        "openai/o3-mini",
        "gpt-4o",
        "o1-preview",
    ):
        validate_model_name(provider="openai", model_name=name)


def test_valid_anthropic_model_names_pass():
    for name in (
        "anthropic/claude-3-5-sonnet-20241022",
        "anthropic/claude-4-opus",
        "claude-3-7-sonnet",
    ):
        validate_model_name(provider="anthropic", model_name=name)


def test_skip_validation_bypass_works():
    """Escape hatch for novel models not yet in the registry."""
    # Without bypass, this raises.
    with pytest.raises(InvalidModelNameError):
        validate_model_name(provider="deepseek", model_name="deepseek-v99-future")
    # With bypass, it passes.
    validate_model_name(
        provider="deepseek",
        model_name="deepseek-v99-future",
        allow_skip=True,
    )


def test_unknown_provider_falls_through():
    """We don't enforce on providers we haven't catalogued. The
    provider's own API is the safety net for those."""
    validate_model_name(provider="exotic-provider", model_name="anything-goes")


def test_empty_model_name_blocked():
    with pytest.raises(InvalidModelNameError, match="required"):
        validate_model_name(provider="deepseek", model_name="")
    with pytest.raises(InvalidModelNameError, match="required"):
        validate_model_name(provider="deepseek", model_name=None)  # type: ignore[arg-type]


def test_unknown_deepseek_model_blocked():
    """Random unknown DeepSeek variant — caller must opt out via
    skip_model_validation if they intend it."""
    with pytest.raises(InvalidModelNameError, match="not a known"):
        validate_model_name(provider="deepseek", model_name="deepseek-vfoo")
