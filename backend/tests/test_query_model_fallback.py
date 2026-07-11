from __future__ import annotations

import pytest

from services import chat_orchestrator as chat_module
from services import query_model_resolver as resolver
from services.settings import settings_service


@pytest.mark.asyncio
async def test_fallback_candidates_prefer_utility_and_skip_extraction(monkeypatch):
    entries = [
        {
            "entry_id": "primary",
            "provider": "longcat",
            "model_name": "LongCat-2.0",
            "enabled": True,
        },
        {
            "entry_id": "extract-only",
            "provider": "vllm-rtx",
            "model_name": "polymath-extract",
            "enabled": True,
        },
        {
            "entry_id": "utility",
            "provider": "siliconflow",
            "model_name": "tencent/Hy3",
            "enabled": True,
        },
    ]

    async def fake_models(_user_id):
        return {
            "query_model_pool": entries,
            "utility": {"pool_entry_id": "utility"},
        }

    async def fake_resolve(_user_id, entry_id):
        entry = next(item for item in entries if item["entry_id"] == entry_id)
        return {
            "entry_id": entry_id,
            "provider": entry["provider"],
            "model": f"openai/{entry['model_name']}",
            "api_base": "https://provider.invalid/v1",
            "api_key": "not-a-real-secret",
            "extra_params": {},
        }

    monkeypatch.setattr(settings_service, "get_models_raw", fake_models)
    monkeypatch.setattr(resolver, "resolve_by_entry_id", fake_resolve)

    candidates = await resolver.resolve_fallback_candidates(
        "user",
        primary_model="openai/LongCat-2.0",
        primary_entry_id="primary",
        limit=2,
    )

    assert [item["entry_id"] for item in candidates] == ["utility"]


@pytest.mark.asyncio
async def test_direct_model_excludes_only_first_matching_account(monkeypatch):
    entries = [
        {
            "entry_id": "account-a",
            "provider": "longcat",
            "model_name": "LongCat-2.0",
            "enabled": True,
        },
        {
            "entry_id": "account-b",
            "provider": "longcat",
            "model_name": "LongCat-2.0",
            "enabled": True,
        },
    ]

    async def fake_models(_user_id):
        return {"query_model_pool": entries, "utility": {}}

    async def fake_resolve(_user_id, entry_id):
        return {
            "entry_id": entry_id,
            "provider": "longcat",
            "model": "openai/LongCat-2.0",
            "api_base": "https://provider.invalid/v1",
            "api_key": f"credential-for-{entry_id}",
            "extra_params": {},
        }

    monkeypatch.setattr(settings_service, "get_models_raw", fake_models)
    monkeypatch.setattr(resolver, "resolve_by_entry_id", fake_resolve)

    candidates = await resolver.resolve_fallback_candidates(
        "user",
        primary_model="openai/LongCat-2.0",
        limit=1,
    )

    assert [item["entry_id"] for item in candidates] == ["account-b"]


@pytest.mark.asyncio
async def test_chat_fallback_prefers_configured_pool(monkeypatch):
    configured = {
        "entry_id": "fallback-entry",
        "provider": "siliconflow",
        "model": "openai/tencent/Hy3",
        "api_base": "https://provider.invalid/v1",
        "api_key": "not-a-real-secret",
        "extra_params": {"disable_thinking": True},
    }

    async def fake_candidates(*_args, **_kwargs):
        return [configured]

    monkeypatch.setattr(chat_module, "resolve_fallback_candidates", fake_candidates)

    selected = await chat_module._resolve_chat_fallback(
        "user",
        primary_model="anthropic/minimax-m2.7",
        primary_entry_id="primary-entry",
    )

    assert selected == configured


@pytest.mark.asyncio
async def test_chat_fallback_uses_static_only_when_pool_is_empty(monkeypatch):
    async def no_candidates(*_args, **_kwargs):
        return []

    monkeypatch.setattr(chat_module, "resolve_fallback_candidates", no_candidates)

    selected = await chat_module._resolve_chat_fallback(
        "user",
        primary_model="openai/primary",
        primary_entry_id=None,
    )

    assert selected is not None
    assert selected["entry_id"] is None
    assert selected["model"] == chat_module._CHAT_FALLBACK_MODEL
