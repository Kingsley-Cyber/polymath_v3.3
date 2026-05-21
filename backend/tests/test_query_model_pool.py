"""
Sprint 3 tests — unified query_model_pool + models subdoc.

Covers:
  - Cloud entry round-trip: plaintext on save, ciphertext at rest, "[set]"
    mask on GET.
  - Ollama entry round-trip: no api_key, correct source + provider.
  - "[set]" sentinel on update preserves existing ciphertext.
  - hyde.pool_entry_id pointing at a non-existent entry → ValueError.
  - Bulk ollama add: dedupes by (provider, model_name), correct shape.
  - Legacy migration: runs once, idempotent on second call, orphan
    hyde/agentic references get nulled with a warning.
  - query_model_resolver: new pool hit, plaintext key returned.

All tests hit the live Mongo in the polymath_v33-backend-1 container —
queries are tightly scoped by `__ac_user_<n>__` sentinel user_ids and
cleaned up afterward.
"""

from __future__ import annotations

import uuid

import pytest

from models.schemas import ModelsConfig
from services.conversation import conversation_service
from services.secrets import decrypt
from services.settings import settings_service


def _u() -> str:
    """Unique throwaway user id per test."""
    return f"__ac_sprint3_{uuid.uuid4().hex[:8]}__"


async def _cleanup(user_id: str) -> None:
    db = conversation_service._db
    if db is None:
        return
    await db["settings"].delete_many({"user_id": user_id})
    await db["model_pool"].delete_many({"user_id": user_id})
    await db["model_profiles"].delete_many({"user_id": user_id})
    await db["user_query_preferences"].delete_many({"user_id": user_id})


async def _setup():
    """Explicit per-test setup. pytest-asyncio 0.21.x autouse async fixtures
    are unreliable when combined with the settings_service singleton; keep
    the connect/attach pair inline per test to match the pattern in
    test_ghost_b_staging.py."""
    await conversation_service.connect()
    settings_service.attach(conversation_service._db)


async def _teardown():
    await conversation_service.disconnect()


@pytest.mark.asyncio
async def test_cloud_entry_encrypt_on_save_mask_on_get():
    await _setup()
    user = _u()
    try:
        cfg = ModelsConfig(
            query_model_pool=[{
                "entry_id": "ent-cloud-1",
                "label": "OpenAI GPT-4o",
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key_ciphertext": "sk-plaintext-secret",
                "model_name": "gpt-4o",
                "source": "cloud",
            }],
            hyde={"default_enabled": True, "pool_entry_id": "ent-cloud-1"},
            agentic={"default_enabled": False, "pool_entry_id": None},
        )
        saved = await settings_service.update_models(user, cfg.model_dump())
        # On save/return: masked to "[set]"
        assert saved.query_model_pool[0].api_key_ciphertext == "[set]"
        # At rest: real ciphertext that decrypts back to the plaintext
        raw = await settings_service.get_models_raw(user)
        stored_ct = raw["query_model_pool"][0]["api_key_ciphertext"]
        assert stored_ct and stored_ct != "[set]"
        assert decrypt(stored_ct) == "sk-plaintext-secret"
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_ollama_entry_has_no_api_key_and_correct_source():
    await _setup()
    user = _u()
    try:
        saved = await settings_service.add_ollama_entries(
            user, ["qwen3:1.7b", "llama3.2:3b"]
        )
        assert len(saved.query_model_pool) == 2
        for e in saved.query_model_pool:
            assert e.provider == "ollama"
            assert e.source == "ollama"
            assert e.base_url is None
            assert e.api_key_ciphertext is None
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_empty_models_pool_gets_visible_chat_default_entry():
    await _setup()
    user = _u()
    try:
        db = conversation_service._db
        await db["settings"].insert_one({
            "user_id": user,
            "chat": {"default_chat_model": "deepseek/deepseek-v4-flash"},
            "retrieval": {},
            "models": {
                "query_model_pool": [],
                "hyde": {"default_enabled": False, "pool_entry_id": None},
                "agentic": {"default_enabled": False, "pool_entry_id": None},
                "reasoning": {"default_enabled": False, "pool_entry_id": None},
                "utility": {"default_enabled": False, "pool_entry_id": None},
            },
            "models_migrated": True,
        })

        settings = await settings_service.get_settings(user)

        assert len(settings.models.query_model_pool) == 1
        entry = settings.models.query_model_pool[0]
        assert entry.entry_id == "system-default-chat"
        assert entry.provider == "deepseek"
        assert entry.model_name == "deepseek/deepseek-v4-flash"
        assert entry.enabled is True

        raw = await settings_service.get_models_raw(user)
        assert raw["query_model_pool"][0]["entry_id"] == "system-default-chat"
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_set_sentinel_preserves_existing_ciphertext():
    await _setup()
    user = _u()
    try:
        # 1. Seed with a real key
        await settings_service.update_models(user, ModelsConfig(
            query_model_pool=[{
                "entry_id": "ent-preserve",
                "label": "Anthropic",
                "provider": "anthropic",
                "base_url": "https://api.anthropic.com/v1",
                "api_key_ciphertext": "sk-preserve-me",
                "model_name": "claude-sonnet-4-6",
                "source": "cloud",
            }],
        ).model_dump())
        raw_before = await settings_service.get_models_raw(user)
        ct_before = raw_before["query_model_pool"][0]["api_key_ciphertext"]

        # 2. Update with "[set]" — should preserve ciphertext verbatim
        await settings_service.update_models(user, ModelsConfig(
            query_model_pool=[{
                "entry_id": "ent-preserve",
                "label": "Anthropic (renamed)",
                "provider": "anthropic",
                "base_url": "https://api.anthropic.com/v1",
                "api_key_ciphertext": "[set]",
                "model_name": "claude-sonnet-4-6",
                "source": "cloud",
            }],
        ).model_dump())
        raw_after = await settings_service.get_models_raw(user)
        ct_after = raw_after["query_model_pool"][0]["api_key_ciphertext"]
        assert ct_after == ct_before, "ciphertext was rotated instead of preserved"
        # Label did update
        assert raw_after["query_model_pool"][0]["label"] == "Anthropic (renamed)"
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_hyde_pool_entry_id_validates_against_pool():
    await _setup()
    user = _u()
    try:
        with pytest.raises(ValueError, match="hyde.pool_entry_id"):
            await settings_service.update_models(user, ModelsConfig(
                query_model_pool=[],
                hyde={"default_enabled": True, "pool_entry_id": "ghost-entry"},
            ).model_dump())
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_utility_pool_entry_id_validates_against_pool():
    await _setup()
    user = _u()
    try:
        with pytest.raises(ValueError, match="utility.pool_entry_id"):
            await settings_service.update_models(user, ModelsConfig(
                query_model_pool=[],
                utility={"default_enabled": True, "pool_entry_id": "ghost-entry"},
            ).model_dump())
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_ollama_bulk_add_dedupes_by_model_name():
    await _setup()
    user = _u()
    try:
        await settings_service.add_ollama_entries(user, ["qwen3:1.7b"])
        # Add the same name again + a new one — only the new one should land
        after = await settings_service.add_ollama_entries(
            user, ["qwen3:1.7b", "llama3.2:3b"]
        )
        ollama_names = sorted(
            e.model_name for e in after.query_model_pool if e.provider == "ollama"
        )
        assert ollama_names == ["llama3.2:3b", "qwen3:1.7b"]
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_delete_pool_entry_nulls_hyde_reference():
    await _setup()
    user = _u()
    try:
        await settings_service.update_models(user, ModelsConfig(
            query_model_pool=[{
                "entry_id": "ent-to-delete",
                "label": "gone soon",
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "api_key_ciphertext": "sk-x",
                "model_name": "deepseek-chat",
                "source": "cloud",
            }],
            hyde={"default_enabled": True, "pool_entry_id": "ent-to-delete"},
        ).model_dump())
        after = await settings_service.delete_pool_entry(user, "ent-to-delete")
        assert after.query_model_pool == []
        assert after.hyde.pool_entry_id is None
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_delete_pool_entry_nulls_utility_reference():
    await _setup()
    user = _u()
    try:
        await settings_service.update_models(user, ModelsConfig(
            query_model_pool=[{
                "entry_id": "utility-delete",
                "label": "utility helper",
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key_ciphertext": "sk-x",
                "model_name": "gpt-4o-mini",
                "source": "cloud",
            }],
            utility={"default_enabled": True, "pool_entry_id": "utility-delete"},
        ).model_dump())
        after = await settings_service.delete_pool_entry(user, "utility-delete")
        assert after.query_model_pool == []
        assert after.utility.pool_entry_id is None
        assert after.utility.default_enabled is True
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_legacy_migration_idempotent_and_handles_orphans():
    """Seed all three legacy stores; verify migration collapses them
    into the unified pool, carries over valid hyde ref, nulls orphan
    agentic ref, and second call is a no-op."""
    await _setup()
    user = _u()
    db = conversation_service._db
    try:
        # Seed Phase E model_pool
        await db["model_pool"].insert_one({
            "entry_id": "legacy-pool-1",
            "user_id": user,
            "label": "legacy-pool",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "ciphertext-pool",  # carried verbatim by migration
            "model_name": "gpt-4o-mini",
            "enabled": True,
        })
        # Seed Phase 19.3 model_profiles
        await db["model_profiles"].insert_one({
            "profile_id": "legacy-profile-1",
            "user_id": user,
            "label": "legacy-profile",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "ciphertext-profile",
            "model_name": "deepseek-chat",
        })
        # Seed Phase F prefs: one valid ref (→ copied) + one orphan (→ nulled)
        await db["user_query_preferences"].insert_one({
            "user_id": user,
            "hyde_pool_id": "legacy-pool-1",
            "agentic_pool_id": "does-not-exist",
        })

        result = await settings_service.migrate_legacy_model_stores(user)
        assert result["migrated"] is True
        assert result["pool_entries"] == 2
        assert any("agentic" in o for o in result["orphans"])

        raw = await settings_service.get_models_raw(user)
        ids = {e["entry_id"] for e in raw["query_model_pool"]}
        assert ids == {"legacy-pool-1", "legacy-profile-1"}
        assert raw["hyde"]["pool_entry_id"] == "legacy-pool-1"
        assert raw["agentic"]["pool_entry_id"] is None

        # Second call → no-op
        second = await settings_service.migrate_legacy_model_stores(user)
        assert second["note"] == "already_migrated"
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_resolver_pool_entry_returns_decrypted_key():
    from services import query_model_resolver

    await _setup()
    user = _u()
    try:
        await settings_service.update_models(user, ModelsConfig(
            query_model_pool=[{
                "entry_id": "ent-resolve",
                "label": "SiliconFlow",
                "provider": "siliconflow",
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key_ciphertext": "sk-resolved-plaintext",
                "model_name": "Qwen/Qwen3-Embedding-0.6B",
                "source": "cloud",
            }],
        ).model_dump())
        resolved = await query_model_resolver.resolve_by_entry_id(
            user, "ent-resolve"
        )
        assert resolved is not None
        assert resolved["model"].endswith("Qwen/Qwen3-Embedding-0.6B")
        assert resolved["api_base"] == "https://api.siliconflow.cn/v1"
        assert resolved["api_key"] == "sk-resolved-plaintext"
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_resolver_query_role_falls_back_to_first_enabled_pool_entry():
    from services import query_model_resolver

    await _setup()
    user = _u()
    try:
        await settings_service.update_models(user, ModelsConfig(
            query_model_pool=[{
                "entry_id": "ent-query-default",
                "label": "Query default",
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "api_key_ciphertext": "sk-query-plaintext",
                "model_name": "deepseek-v4-flash",
                "source": "cloud",
                "enabled": True,
            }],
        ).model_dump())

        resolved = await query_model_resolver.resolve(user, "query")

        assert resolved is not None
        assert resolved["model"] == "openai/deepseek-v4-flash"
        assert resolved["api_base"] == "https://api.deepseek.com/v1"
        assert resolved["api_key"] == "sk-query-plaintext"
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_resolver_query_role_skips_disabled_pool_entries():
    from services import query_model_resolver

    await _setup()
    user = _u()
    try:
        await settings_service.update_models(user, ModelsConfig(
            query_model_pool=[
                {
                    "entry_id": "ent-disabled",
                    "label": "Disabled",
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key_ciphertext": "sk-disabled",
                    "model_name": "gpt-4o-mini",
                    "source": "cloud",
                    "enabled": False,
                },
                {
                    "entry_id": "ent-enabled",
                    "label": "Enabled",
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key_ciphertext": "sk-enabled",
                    "model_name": "gpt-4o-mini",
                    "source": "cloud",
                    "enabled": True,
                },
            ],
        ).model_dump())

        resolved = await query_model_resolver.resolve(user, "query")

        assert resolved is not None
        assert resolved["api_key"] == "sk-enabled"
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_resolver_utility_role_returns_configured_pool_entry():
    from services import query_model_resolver

    await _setup()
    user = _u()
    try:
        await settings_service.update_models(user, ModelsConfig(
            query_model_pool=[{
                "entry_id": "ent-utility",
                "label": "Fast utility",
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key_ciphertext": "sk-utility-plaintext",
                "model_name": "gpt-4o-mini",
                "source": "cloud",
            }],
            utility={"default_enabled": True, "pool_entry_id": "ent-utility"},
        ).model_dump())

        resolved = await query_model_resolver.resolve(user, "utility")

        assert resolved is not None
        assert resolved["model"] == "openai/gpt-4o-mini"
        assert resolved["api_base"] == "https://api.openai.com/v1"
        assert resolved["api_key"] == "sk-utility-plaintext"
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_resolver_utility_role_miss_returns_none():
    from services import query_model_resolver

    await _setup()
    user = _u()
    try:
        await settings_service.update_models(user, ModelsConfig(
            query_model_pool=[],
            utility={"default_enabled": False, "pool_entry_id": None},
        ).model_dump())

        assert await query_model_resolver.resolve(user, "utility") is None
    finally:
        await _cleanup(user)
        await _teardown()


@pytest.mark.asyncio
async def test_utility_model_test_endpoint_uses_configured_utility_role(monkeypatch):
    from routers import settings as settings_router

    seen: dict[str, object] = {}

    async def fake_resolve(user_id, kind):
        seen["resolve"] = (user_id, kind)
        return {
            "model": "openai/glm-5-turbo",
            "api_base": "https://api.z.ai/api/coding/paas/v4",
            "api_key": "sk-test",
            "extra_params": {"seed": 1},
        }

    async def fake_complete_sync(**kwargs):
        seen["llm"] = kwargs
        return "POLYMATH_UTILITY_OK"

    monkeypatch.setattr(settings_router, "resolve_query_model_kind", fake_resolve)
    monkeypatch.setattr(settings_router.llm_service, "complete_sync", fake_complete_sync)

    result = await settings_router.test_utility_model_connection(
        current_user={"user_id": "user-utility-test"}
    )

    assert result.ok is True
    assert result.status == "ok"
    assert result.model == "openai/glm-5-turbo"
    assert result.output_preview == "POLYMATH_UTILITY_OK"
    assert seen["resolve"] == ("user-utility-test", "utility")
    llm_call = seen["llm"]
    assert llm_call["model"] == "openai/glm-5-turbo"
    assert llm_call["api_base"] == "https://api.z.ai/api/coding/paas/v4"
    assert llm_call["api_key"] == "sk-test"
    assert llm_call["temperature"] == 0.0
    assert llm_call["max_tokens"] == 24


@pytest.mark.asyncio
async def test_utility_model_test_endpoint_handles_missing_utility_role(monkeypatch):
    from routers import settings as settings_router

    async def fake_resolve(_user_id, _kind):
        return None

    monkeypatch.setattr(settings_router, "resolve_query_model_kind", fake_resolve)

    result = await settings_router.test_utility_model_connection(
        current_user={"user_id": "user-utility-test"}
    )

    assert result.ok is False
    assert result.status == "not_configured"
    assert result.model is None
