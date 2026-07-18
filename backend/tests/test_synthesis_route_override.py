from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from config import Settings
from models.schemas import ModelsConfig
from services import chat_orchestrator as chat_module
from services import query_model_resolver
from services.settings import SettingsService


@pytest.mark.asyncio
async def test_override_off_is_exact_rollback_and_does_not_resolve(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled route must not read the role registry")

    monkeypatch.setattr(chat_module, "resolve_query_model_kind", forbidden)
    creds = {"api_base": "https://rollback.test", "api_key": "rollback"}
    result = await chat_module._resolve_synthesis_route_override(
        enabled=False,
        user_id="owner",
        tool_route_active=False,
        model_used="anthropic/minimax-m2.7",
        profile_creds=creds,
        primary_entry_id="minimax",
    )

    assert result["model"] == "anthropic/minimax-m2.7"
    assert result["profile_creds"] is creds
    assert result["primary_entry_id"] == "minimax"
    assert result["trace"] == {
        "enabled": False,
        "eligible": False,
        "applied": False,
        "reason": "disabled",
        "rollback_model": "anthropic/minimax-m2.7",
        "rollback_entry_id": "minimax",
        "candidate_model": None,
        "candidate_entry_id": None,
    }


@pytest.mark.asyncio
async def test_tool_route_is_never_swapped(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("tool route must not read the synthesis role")

    monkeypatch.setattr(chat_module, "resolve_query_model_kind", forbidden)
    result = await chat_module._resolve_synthesis_route_override(
        enabled=True,
        user_id="owner",
        tool_route_active=True,
        model_used="anthropic/tool-model",
        profile_creds={},
        primary_entry_id="tool",
    )

    assert result["model"] == "anthropic/tool-model"
    assert result["trace"]["reason"] == "tool_route_active"
    assert result["trace"]["applied"] is False


@pytest.mark.asyncio
async def test_configured_route_replaces_only_model_credentials_and_entry(monkeypatch):
    async def resolve(user_id, kind):
        assert (user_id, kind) == ("owner", "synthesis")
        return {
            "entry_id": "fast",
            "model": "deepseek/deepseek-v4-flash",
            "api_base": "https://fast.test",
            "api_key": "secret",
            "extra_params": {"disable_thinking": True},
        }

    monkeypatch.setattr(chat_module, "resolve_query_model_kind", resolve)
    result = await chat_module._resolve_synthesis_route_override(
        enabled=True,
        user_id="owner",
        tool_route_active=False,
        model_used="anthropic/minimax-m2.7",
        profile_creds={"api_key": "rollback"},
        primary_entry_id="minimax",
    )

    assert result["model"] == "deepseek/deepseek-v4-flash"
    assert result["profile_creds"] == {
        "api_base": "https://fast.test",
        "api_key": "secret",
        "extra_params": {"disable_thinking": True},
    }
    assert result["primary_entry_id"] == "fast"
    assert result["trace"]["applied"] is True
    assert result["trace"]["rollback_model"] == "anthropic/minimax-m2.7"
    assert result["trace"]["candidate_entry_id"] == "fast"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "error"])
async def test_unavailable_candidate_preserves_rollback_with_named_reason(
    monkeypatch, failure
):
    async def resolve(*_args, **_kwargs):
        if failure == "error":
            raise TimeoutError("registry timeout")
        return None

    monkeypatch.setattr(chat_module, "resolve_query_model_kind", resolve)
    creds = {"api_key": "rollback"}
    result = await chat_module._resolve_synthesis_route_override(
        enabled=True,
        user_id="owner",
        tool_route_active=False,
        model_used="anthropic/minimax-m2.7",
        profile_creds=creds,
        primary_entry_id="minimax",
    )

    assert result["model"] == "anthropic/minimax-m2.7"
    assert result["profile_creds"] is creds
    assert result["trace"]["applied"] is False
    expected = (
        "resolver_error" if failure == "error" else "configured_route_unavailable"
    )
    assert result["trace"]["reason"] == expected


@pytest.mark.asyncio
async def test_synthesis_role_resolves_configured_pool_entry(monkeypatch):
    class FakeSettings:
        async def get_models_raw(self, user_id):
            assert user_id == "owner"
            return {"synthesis": {"pool_entry_id": "fast"}}

    async def resolved(user_id, entry_id):
        assert (user_id, entry_id) == ("owner", "fast")
        return {"entry_id": "fast", "model": "deepseek/deepseek-v4-flash"}

    monkeypatch.setattr(query_model_resolver, "resolve_by_entry_id", resolved)
    monkeypatch.setitem(
        __import__("sys").modules,
        "services.settings",
        SimpleNamespace(settings_service=FakeSettings()),
    )

    result = await query_model_resolver.resolve("owner", "synthesis")
    assert result == {
        "entry_id": "fast",
        "model": "deepseek/deepseek-v4-flash",
    }


@pytest.mark.asyncio
async def test_system_credential_reference_resolves_without_ciphertext_copy(
    monkeypatch,
):
    class FakeSettings:
        async def get_models_raw(self, user_id):
            assert user_id == "e2e-owner"
            return {
                "query_model_pool": [
                    {
                        "entry_id": "deepseek-api__deepseek-v4-flash",
                        "provider": "deepseek",
                        "model_name": "deepseek-v4-flash",
                        "base_url": "https://api.deepseek.com",
                        "api_key_ciphertext": None,
                        "credential_ref": {
                            "kind": "settings_api_key.v1",
                            "scope": "system",
                            "settings_user_id": "system-key-owner",
                            "provider": "deepseek",
                        },
                        "extra_params": {
                            "thinking": {"type": "disabled"},
                        },
                    }
                ]
            }

        async def get_plaintext_key_by_reference(
            self,
            *,
            settings_user_id,
            provider,
        ):
            assert (settings_user_id, provider) == (
                "system-key-owner",
                "deepseek",
            )
            return "dispatch-only-key"

        async def get_plaintext_keys_for_llm(self, user_id):
            raise AssertionError("exact credential reference must win")

    monkeypatch.setitem(
        sys.modules,
        "services.settings",
        SimpleNamespace(settings_service=FakeSettings()),
    )

    resolved = await query_model_resolver.resolve_by_entry_id(
        "e2e-owner",
        "deepseek-api__deepseek-v4-flash",
    )

    assert resolved == {
        "entry_id": "deepseek-api__deepseek-v4-flash",
        "provider": "deepseek",
        "model": "deepseek/deepseek-v4-flash",
        "api_base": "https://api.deepseek.com",
        "api_key": "dispatch-only-key",
        "extra_params": {"thinking": {"type": "disabled"}},
    }


class _ModelsCollection:
    def __init__(self, models):
        self.models = models

    async def find_one(self, _query, projection=None):
        return {"models": self.models}

    async def update_one(self, _query, update, upsert=False):
        assert upsert is True
        self.models = update["$set"]["models"]


class _ModelsDatabase:
    def __init__(self, models):
        self.collection = _ModelsCollection(models)

    def __getitem__(self, name):
        assert name == "settings"
        return self.collection


@pytest.mark.asyncio
async def test_credential_reference_is_operator_managed_and_preserved():
    reference = {
        "kind": "settings_api_key.v1",
        "scope": "system",
        "settings_user_id": "system-key-owner",
        "provider": "deepseek",
    }
    entry = {
        "entry_id": "deepseek-api__deepseek-v4-flash",
        "provider": "deepseek",
        "model_name": "deepseek-v4-flash",
        "credential_ref": reference,
    }

    empty_service = SettingsService()
    empty_service.attach(_ModelsDatabase({"query_model_pool": []}))
    with pytest.raises(ValueError, match="operator-managed"):
        await empty_service.update_models(
            "owner",
            ModelsConfig(query_model_pool=[entry]).model_dump(),
        )

    seeded_service = SettingsService()
    database = _ModelsDatabase(
        {
            "query_model_pool": [entry],
            "hyde": {},
            "agentic": {},
            "reasoning": {},
            "utility": {},
            "graph_query": {},
            "synthesis": {},
        }
    )
    seeded_service.attach(database)
    round_trip_without_reference = {
        key: value for key, value in entry.items() if key != "credential_ref"
    }

    await seeded_service.update_models(
        "owner",
        ModelsConfig(query_model_pool=[round_trip_without_reference]).model_dump(),
    )

    stored = database.collection.models["query_model_pool"][0]
    assert stored["credential_ref"] == reference
    assert stored["api_key_ciphertext"] is None


def test_synthesis_models_config_defaults_off_and_round_trips():
    default = ModelsConfig()
    assert default.synthesis.pool_entry_id is None

    configured = ModelsConfig(
        query_model_pool=[],
        synthesis={"pool_entry_id": "fast"},
    )
    assert configured.model_dump()["synthesis"] == {"pool_entry_id": "fast"}


def test_runtime_flag_defaults_off_with_both_compose_passthroughs():
    assert Settings.model_fields["SYNTHESIS_ROUTE_OVERRIDE_ENABLED"].default is False

    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.yml").read_text()
    expected = (
        "SYNTHESIS_ROUTE_OVERRIDE_ENABLED: "
        "${SYNTHESIS_ROUTE_OVERRIDE_ENABLED:-false}"
    )
    assert compose.count(expected) == 2
    assert (
        "SYNTHESIS_ROUTE_OVERRIDE_ENABLED=false" in (root / ".env.example").read_text()
    )
