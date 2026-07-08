from __future__ import annotations

import copy

import pytest

from models.schemas import (
    GlobalIngestionSettings,
    GlobalIngestionSummarySettings,
    IngestionConfig,
)
from services.ingestion_service import IngestionService, _summary_backfill_index_scope
from services.secrets import decrypt, encrypt
from services.settings import SettingsService, settings_service


def test_summary_backfill_limited_generate_indexes_only_generated_parents() -> None:
    clause = {"summary": {"$exists": True, "$nin": [None, ""]}}

    scope, clauses = _summary_backfill_index_scope(
        generate=True,
        limit=200,
        generated_parent_ids=["parent-a", "parent-b"],
        summary_text_clause=clause,
    )

    assert scope == "generated_in_call"
    assert clauses == [
        clause,
        {"parent_id": {"$in": ["parent-a", "parent-b"]}},
    ]


def test_summary_backfill_index_only_rebuilds_all_existing_summaries() -> None:
    clause = {"summary": {"$exists": True, "$nin": [None, ""]}}

    scope, clauses = _summary_backfill_index_scope(
        generate=False,
        limit=200,
        generated_parent_ids=["parent-a"],
        summary_text_clause=clause,
    )

    assert scope == "all_existing_summaries"
    assert clauses == [clause]


def test_summary_backfill_doc_scope_indexes_existing_summaries_inside_scope() -> None:
    clause = {"summary": {"$exists": True, "$nin": [None, ""]}}

    scope, clauses = _summary_backfill_index_scope(
        generate=True,
        limit=200,
        generated_parent_ids=["parent-a"],
        summary_text_clause=clause,
        bounded_by_doc_ids=True,
    )

    assert scope == "doc_scope_existing_summaries"
    assert clauses == [clause]


def test_global_summary_settings_encrypt_mask_and_decrypt() -> None:
    raw = {
        "summary": {
            "enabled": True,
            "max_summary_tokens": 220,
            "max_concurrent": 3,
            "summary_models": [
                {
                    "provider_preset": "openai",
                    "model": "openai/gpt-4o-mini",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "unit-summary-secret",
                    "max_concurrent": 2,
                    "extra_params": {},
                }
            ],
        }
    }

    SettingsService._encrypt_ingestion_keys_in_place(raw)
    stored_key = raw["summary"]["summary_models"][0]["api_key"]
    assert stored_key != "unit-summary-secret"
    assert decrypt(stored_key) == "unit-summary-secret"

    masked = copy.deepcopy(raw)
    SettingsService._mask_ingestion_keys_in_place(masked)
    assert masked["summary"]["summary_models"][0]["api_key"] == "[set]"

    runtime = copy.deepcopy(raw)
    SettingsService._decrypt_ingestion_keys_in_place(runtime)
    assert runtime["summary"]["summary_models"][0]["api_key"] == "unit-summary-secret"

    update = copy.deepcopy(masked)
    update["summary"]["max_concurrent"] = 5
    SettingsService._encrypt_ingestion_keys_in_place(update, raw)
    assert update["summary"]["summary_models"][0]["api_key"] == stored_key


def test_keyless_ollama_summary_update_clears_stale_cloud_key() -> None:
    existing = {
        "summary": {
            "enabled": True,
            "max_summary_tokens": 175,
            "max_concurrent": 8,
            "summary_models": [
                {
                    "provider_preset": "siliconflow",
                    "model": "openai/Qwen/Qwen2.5-7B-Instruct",
                    "base_url": "https://api.siliconflow.com/v1",
                    "api_key": encrypt("stale-cloud-key"),
                    "max_concurrent": 8,
                    "extra_params": {},
                }
            ],
        }
    }
    update = {
        "summary": {
            "enabled": True,
            "max_summary_tokens": 256,
            "max_concurrent": 4,
            "summary_models": [
                {
                    "provider_preset": "ollama",
                    "model": "ollama/minimax-m3:cloud",
                    "base_url": None,
                    "api_key": None,
                    "max_concurrent": 4,
                    "extra_params": {},
                }
            ],
        }
    }

    SettingsService._encrypt_ingestion_keys_in_place(update, existing)

    assert update["summary"]["summary_models"][0]["api_key"] is None


@pytest.mark.asyncio
async def test_global_summary_defaults_fill_empty_corpus_pool(monkeypatch) -> None:
    async def fake_runtime_settings(user_id: str | None = None):
        return GlobalIngestionSettings(
            summary=GlobalIngestionSummarySettings(
                enabled=True,
                max_summary_tokens=256,
                max_concurrent=4,
                summary_models=[
                    {
                        "provider_preset": "deepseek",
                        "model": "deepseek/deepseek-chat",
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key": "unit-runtime-key",
                        "max_concurrent": 2,
                        "extra_params": {},
                    }
                ],
            )
        )

    monkeypatch.setattr(
        settings_service,
        "get_runtime_ingestion_settings",
        fake_runtime_settings,
    )

    cfg = IngestionConfig(summary_models=[], max_summary_tokens=175)
    merged = await IngestionService._apply_global_summary_defaults(
        user_id="user-1",
        ingestion_config=cfg,
    )

    assert merged.summary_models
    assert merged.summary_models[0].model == "deepseek/deepseek-chat"
    assert merged.summary_models[0].api_key == "unit-runtime-key"
    assert merged.summary_models[0].max_concurrent == 2
    assert merged.max_summary_tokens == 256


@pytest.mark.asyncio
async def test_explicit_corpus_summary_pool_wins(monkeypatch) -> None:
    async def fake_runtime_settings(user_id: str | None = None):
        return GlobalIngestionSettings(
            summary=GlobalIngestionSummarySettings(
                enabled=True,
                max_summary_tokens=256,
                max_concurrent=4,
                summary_models=[
                    {
                        "provider_preset": "openai",
                        "model": "openai/gpt-4o-mini",
                        "base_url": None,
                        "api_key": "unit-global-key",
                        "max_concurrent": 2,
                        "extra_params": {},
                    }
                ],
            )
        )

    monkeypatch.setattr(
        settings_service,
        "get_runtime_ingestion_settings",
        fake_runtime_settings,
    )

    cfg = IngestionConfig(
        summary_models=[
            {
                "provider_preset": "ollama",
                "model": "ollama/qwen3:1.7b",
                "base_url": None,
                "api_key": None,
                "max_concurrent": 1,
                "extra_params": {},
            }
        ],
        max_summary_tokens=300,
    )
    merged = await IngestionService._apply_global_summary_defaults(
        user_id="user-1",
        ingestion_config=cfg,
    )

    assert len(merged.summary_models) == 1
    assert merged.summary_models[0].model == "ollama/qwen3:1.7b"
    assert merged.max_summary_tokens == 300


@pytest.mark.asyncio
async def test_masked_settings_key_is_rehydrated_for_new_corpus(monkeypatch) -> None:
    async def fake_runtime_settings(user_id: str | None = None):
        return GlobalIngestionSettings(
            summary=GlobalIngestionSummarySettings(
                enabled=True,
                max_summary_tokens=175,
                max_concurrent=4,
                summary_models=[
                    {
                        "provider_preset": "openai",
                        "model": "openai/gpt-4o-mini",
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "unit-settings-plaintext",
                        "max_concurrent": 2,
                        "extra_params": {},
                    }
                ],
            )
        )

    monkeypatch.setattr(
        settings_service,
        "get_runtime_ingestion_settings",
        fake_runtime_settings,
    )

    cfg = IngestionConfig(
        summary_models=[
            {
                "provider_preset": "openai",
                "model": "openai/gpt-4o-mini",
                "base_url": "https://api.openai.com/v1",
                "api_key": "[set]",
                "max_concurrent": 2,
                "extra_params": {},
            }
        ],
    )
    merged = await IngestionService._apply_global_summary_defaults(
        user_id="user-1",
        ingestion_config=cfg,
    )

    assert merged.summary_models[0].api_key == "unit-settings-plaintext"
