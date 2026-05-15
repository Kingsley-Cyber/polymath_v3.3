"""Tests for the curated provider catalog + deprecation filter in /api/models.

Pins the contract introduced when /api/models stopped relying solely on
LiteLLM's bundled pricing DB:

  • Every wildcard route in litellm/config.yaml has at least one chat
    model emitted by get_curated_provider_models().
  • Deprecated SKUs in LiteLLM's pricing DB are filtered out of
    get_litellm_models() before they reach the merge.
  • The curated source wins over LiteLLM on duplicate id (curated is the
    maintained list; LiteLLM's pricing DB lags upstream releases).
  • The reachable=true filter treats source="curated" the same as
    source="litellm" — both are cloud sources gated by pool membership.

The tests mock httpx so LiteLLM doesn't have to be running.
"""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Same auth-stub bootstrap as the MCP ingest tool tests — routers/models.py
# transitively imports services.auth → {jose, passlib}. The tests under
# this module never touch the JWT path, so no-op stand-ins are safe.
def _install_auth_stubs_if_missing() -> None:
    try:
        import jose  # noqa: F401
    except ImportError:
        jose_mod = ModuleType("jose")

        class JWTError(Exception):
            pass

        class _Jwt:
            @staticmethod
            def encode(*_a, **_kw):  # pragma: no cover
                raise RuntimeError("jose stub: encode not implemented")

            @staticmethod
            def decode(*_a, **_kw):  # pragma: no cover
                raise RuntimeError("jose stub: decode not implemented")

        jose_mod.JWTError = JWTError
        jose_mod.jwt = _Jwt()
        sys.modules["jose"] = jose_mod

    try:
        import passlib.context  # noqa: F401
    except ImportError:
        passlib_mod = ModuleType("passlib")
        ctx_mod = ModuleType("passlib.context")

        class _CryptContext:
            def __init__(self, *a, **kw):
                pass

            def hash(self, *_a, **_kw):  # pragma: no cover
                raise RuntimeError("passlib stub: hash not implemented")

            def verify(self, *_a, **_kw):  # pragma: no cover
                raise RuntimeError("passlib stub: verify not implemented")

        ctx_mod.CryptContext = _CryptContext
        passlib_mod.context = ctx_mod
        sys.modules["passlib"] = passlib_mod
        sys.modules["passlib.context"] = ctx_mod

    # slowapi (rate limiter used by routers.auth) -----------------------
    try:
        import slowapi  # noqa: F401
    except ImportError:
        slowapi_mod = ModuleType("slowapi")
        util_mod = ModuleType("slowapi.util")

        class _Limiter:
            def __init__(self, *a, **kw):
                pass

            def limit(self, *_a, **_kw):
                # routers.auth uses @limiter.limit("..."); the decorator
                # must return a passthrough so the wrapped function is
                # imported unchanged.
                def _decorator(fn):
                    return fn
                return _decorator

        def _get_remote_address(_request):  # pragma: no cover
            return "0.0.0.0"

        slowapi_mod.Limiter = _Limiter
        util_mod.get_remote_address = _get_remote_address
        sys.modules["slowapi"] = slowapi_mod
        sys.modules["slowapi.util"] = util_mod


_install_auth_stubs_if_missing()


from routers import models as models_router  # noqa: E402
from models.schemas import ModelInfo  # noqa: E402


# ─── Curated catalog: every wildcard route in litellm/config.yaml ─────


# Provider prefixes we expect at least one curated chat model for. This is
# the contract the user asked for: "ensure the discovery list works for
# all set up providers." Each entry must match a wildcard route in
# litellm/config.yaml so the routing actually exists upstream.
_EXPECTED_PROVIDERS = (
    "openai",
    "anthropic",
    "deepseek",
    "gemini",
    "mistral",
    "glm-coding",
    "kimi",
    "minimax",
    "mimo",
    "mimo-coding",
)


def test_curated_catalog_covers_every_litellm_route():
    """Every provider in _CURATED_PROVIDER_MODELS has at least one chat
    model. The OpenRouter aggregator is intentionally omitted."""
    catalog = models_router._CURATED_PROVIDER_MODELS
    for provider in _EXPECTED_PROVIDERS:
        assert provider in catalog, (
            f"provider {provider!r} is in litellm/config.yaml but has no "
            f"curated catalog entries — picker will be empty for it"
        )
        assert len(catalog[provider]) >= 1, (
            f"provider {provider!r} has an empty curated list"
        )


@pytest.mark.asyncio
async def test_curated_emits_models_for_every_provider():
    """get_curated_provider_models() returns at least one ModelInfo
    per expected provider with the right id shape and source label."""
    result = await models_router.get_curated_provider_models()
    by_provider: dict[str, list[ModelInfo]] = {}
    for m in result:
        by_provider.setdefault(m.provider, []).append(m)

    for provider in _EXPECTED_PROVIDERS:
        assert provider in by_provider, f"no curated emits for {provider}"
        # Every entry must be labeled with source="curated" so the merge
        # priority and reachable filter behave correctly.
        for m in by_provider[provider]:
            assert m.source == "curated", (
                f"{m.id} should have source='curated', got {m.source!r}"
            )
            # Id starts with the provider prefix
            assert m.id.startswith(f"{provider}/"), (
                f"id {m.id!r} should start with {provider!r}/"
            )


@pytest.mark.asyncio
async def test_curated_includes_deepseek_v4():
    """The whole reason this catalog exists: surface V4 names that
    LiteLLM's pricing DB doesn't advertise yet."""
    result = await models_router.get_curated_provider_models()
    ids = {m.id for m in result}
    assert "deepseek/deepseek-v4-flash" in ids
    assert "deepseek/deepseek-v4-pro" in ids


@pytest.mark.asyncio
async def test_curated_includes_thinking_capable_models():
    """Sanity — the providers we wired for thinking-mode dispatch
    (DeepSeek V4, Magistral, GLM) all surface at least one matching
    model so the thinking selector has something to fire on."""
    result = await models_router.get_curated_provider_models()
    ids = {m.id for m in result}
    # DeepSeek V4 → /deepseek-v4(-flash|-pro)?\b/
    assert any(m.startswith("deepseek/deepseek-v4-") for m in ids)
    # Magistral → m.includes("magistral")
    assert any("magistral" in m for m in ids)
    # GLM → /glm-(5|4\.[5-7])/
    assert any("glm-5" in m or "glm-4.5" in m or "glm-4.6" in m for m in ids)


# ─── Deprecation denylist ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_litellm_models_filters_deprecated():
    """Deprecated SKUs in LiteLLM's response don't make it into the
    discovery list. Mocks the upstream /models response so the test
    doesn't require LiteLLM to be running."""
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json = MagicMock(return_value={
        "data": [
            # Deprecated — should be filtered
            {"id": "deepseek/deepseek-chat"},
            {"id": "deepseek/deepseek-coder"},
            {"id": "deepseek/deepseek-r1"},
            {"id": "deepseek/deepseek-reasoner"},
            {"id": "deepseek/deepseek-v3"},
            {"id": "deepseek/deepseek-v3.2"},
            # Currently supported — should pass through
            {"id": "openai/gpt-4o"},
            {"id": "anthropic/claude-3-5-sonnet"},
        ],
    })
    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.get = AsyncMock(return_value=fake_response)

    with patch.object(models_router.httpx, "AsyncClient", return_value=fake_client):
        result = await models_router.get_litellm_models()

    ids = {m.id for m in result}
    assert "openai/gpt-4o" in ids
    assert "anthropic/claude-3-5-sonnet" in ids
    for deprecated in (
        "deepseek/deepseek-chat",
        "deepseek/deepseek-coder",
        "deepseek/deepseek-r1",
        "deepseek/deepseek-reasoner",
        "deepseek/deepseek-v3",
        "deepseek/deepseek-v3.2",
    ):
        assert deprecated not in ids, (
            f"{deprecated} should have been filtered by _DEPRECATED_LITELLM_IDS"
        )


def test_deprecated_denylist_does_not_overlap_curated():
    """Sanity — the curated catalog must never contain a deprecated id.
    If it did, the deprecation filter would strip the litellm copy and
    the curated copy would still surface, defeating the deprecation."""
    deprecated_suffixes = {
        i.split("/", 1)[1] for i in models_router._DEPRECATED_LITELLM_IDS
        if "/" in i
    }
    for provider, names in models_router._CURATED_PROVIDER_MODELS.items():
        if provider != "deepseek":
            continue
        for name in names:
            assert name not in deprecated_suffixes, (
                f"curated deepseek entry {name!r} is also in the "
                f"deprecation denylist — fix one or the other"
            )


# ─── Merge priority ───────────────────────────────────────────────────


def _make(id_: str, source: str, type_: str = "chat") -> ModelInfo:
    return ModelInfo(
        id=id_,
        name=id_,
        provider=id_.split("/", 1)[0] if "/" in id_ else "unknown",
        source=source,
        type=type_,
    )


def test_merge_priority_curated_overrides_litellm():
    """When the same id appears in both litellm and curated, the
    curated version wins. The curated catalog is the maintained source
    of truth for currently-supported provider SKUs; LiteLLM's pricing
    DB lags upstream releases."""
    litellm = [_make("deepseek/deepseek-v4-flash", "litellm")]
    curated = [_make("deepseek/deepseek-v4-flash", "curated")]

    chat, _emb = models_router._merge_and_split(
        ollama=[], litellm=litellm, curated=curated, embedder=[], local=[],
    )
    assert len(chat) == 1
    assert chat[0].source == "curated", "curated should beat litellm on dup id"


def test_merge_priority_ollama_overrides_curated():
    """Ollama (locally-installed) beats curated (catalog) on dup id.
    Locally-running models are authoritatively available; the catalog
    is just documentation that the name routes correctly."""
    ollama = [_make("ollama/glm-5:cloud", "ollama")]
    curated = [_make("ollama/glm-5:cloud", "curated")]

    chat, _emb = models_router._merge_and_split(
        ollama=ollama, litellm=[], curated=curated, embedder=[], local=[],
    )
    assert chat[0].source == "ollama"


def test_merge_priority_embedder_wins_absolutely():
    """The live embedder /info path beats every other source — it
    reports the actually-loaded model with introspected dimension."""
    embedder = [_make("tei/qwen3-embedding", "embedder", type_="embedding")]
    curated = [_make("tei/qwen3-embedding", "curated", type_="embedding")]
    litellm = [_make("tei/qwen3-embedding", "litellm", type_="embedding")]

    _chat, emb = models_router._merge_and_split(
        ollama=[], litellm=litellm, curated=curated, embedder=embedder, local=[],
    )
    assert len(emb) == 1
    assert emb[0].source == "embedder"


# ─── Curated never overlaps with itself ───────────────────────────────


def test_curated_ids_are_unique():
    """A model name should appear under exactly one provider prefix in
    the curated catalog. Cross-provider duplicates would produce
    non-deterministic discovery ordering."""
    seen: set[str] = set()
    for provider, names in models_router._CURATED_PROVIDER_MODELS.items():
        for name in names:
            full_id = f"{provider}/{name}"
            assert full_id not in seen, (
                f"duplicate curated id {full_id!r}"
            )
            seen.add(full_id)


# ─── Chat vs embedding classification ─────────────────────────────────


@pytest.mark.asyncio
async def test_curated_pixtral_is_chat_not_embedding():
    """The _is_embedding_name() helper checks for substrings like
    'embed', 'e5', 'bge', etc. Pixtral / Magistral / etc. must not
    match — they're vision/chat models. This guards against a false
    positive that would mis-route them to the embedding picker."""
    result = await models_router.get_curated_provider_models()
    by_id = {m.id: m for m in result}
    for chat_model in (
        "mistral/pixtral-large-latest",
        "mistral/magistral-small-latest",
        "anthropic/claude-sonnet-4-5",
        "openai/gpt-4o",
        "deepseek/deepseek-v4-flash",
    ):
        assert by_id[chat_model].type == "chat", (
            f"{chat_model} should be type='chat'"
        )
