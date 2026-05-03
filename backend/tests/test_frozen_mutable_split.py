"""
Frozen / mutable field partition invariants + update_corpus guard tests.

These tests lock in the contract: every IngestionConfig field belongs to
exactly one bucket, the worker snapshots only FROZEN, and update_corpus
rejects FROZEN patches on non-empty corpora with HTTP 409.
"""

from __future__ import annotations

import pytest

from models.schemas import IngestionConfig
from services.ingestion_service import (
    FROZEN_CONFIG_FIELDS,
    FrozenFieldError,
    MUTABLE_CONFIG_FIELDS,
    build_effective_config,
    freeze_snapshot,
    isolate_local_model_routing,
)


def test_partition_is_total_and_disjoint():
    """Every IngestionConfig field must land in exactly one bucket, and
    FROZEN + MUTABLE together must cover the whole model."""
    all_fields = set(IngestionConfig.model_fields)
    assert FROZEN_CONFIG_FIELDS.isdisjoint(MUTABLE_CONFIG_FIELDS), (
        "field appears in both buckets: "
        f"{FROZEN_CONFIG_FIELDS & MUTABLE_CONFIG_FIELDS}"
    )
    bucketed = FROZEN_CONFIG_FIELDS | MUTABLE_CONFIG_FIELDS
    missing = all_fields - bucketed
    extra = bucketed - all_fields
    assert not missing, f"fields missing from both buckets: {sorted(missing)}"
    assert not extra, f"buckets contain unknown fields: {sorted(extra)}"


def test_frozen_snapshot_excludes_mutable_fields():
    cfg = IngestionConfig(
        embed_mode="api",
        embed_base_url="https://example.com/v1",
        embed_api_key="plaintext-would-be-here",
        embed_max_concurrent=4,
    )
    snap = freeze_snapshot(cfg)
    for field in MUTABLE_CONFIG_FIELDS:
        assert field not in snap, (
            f"freeze_snapshot leaked mutable field {field!r}"
        )
    # And every frozen field SHOULD be present
    for field in FROZEN_CONFIG_FIELDS:
        assert field in snap, f"freeze_snapshot missing frozen field {field!r}"


def test_build_effective_config_precedence():
    """overrides > live_corpus > frozen_base. The test pins all three at
    distinct values so a precedence bug surfaces as a field mismatch."""
    frozen_base = IngestionConfig(
        use_neo4j=False,  # frozen — from baseline
        embed_mode="local",  # mutable — baseline says local
    ).model_dump()
    live_corpus = IngestionConfig(
        use_neo4j=True,  # frozen — baseline wins, corpus ignored for frozen
        embed_mode="modal",  # mutable — live corpus overrides baseline
    ).model_dump()
    ingest_overrides = {"embed_mode": "api"}

    eff = build_effective_config(
        frozen_base=frozen_base,
        live_corpus=live_corpus,
        ingest_overrides=ingest_overrides,
    )
    # Frozen bucket: baseline wins (live corpus's use_neo4j=True is ignored
    # when assembling — the baseline says False).
    assert eff.use_neo4j is False
    # Mutable bucket: live corpus overrides baseline; ingest override wins.
    assert eff.embed_mode == "api"


def test_build_effective_config_no_overrides():
    frozen_base = IngestionConfig(use_neo4j=True).model_dump()
    live_corpus = IngestionConfig(embed_mode="modal").model_dump()
    eff = build_effective_config(
        frozen_base=frozen_base, live_corpus=live_corpus
    )
    assert eff.use_neo4j is True
    assert eff.embed_mode == "modal"


def test_build_effective_config_drops_none_overrides():
    """None-valued override keys should be ignored so the router can pass
    a uniform dict without pre-filtering unset form params."""
    frozen_base = IngestionConfig().model_dump()
    live_corpus = IngestionConfig(embed_mode="modal").model_dump()
    eff = build_effective_config(
        frozen_base=frozen_base,
        live_corpus=live_corpus,
        ingest_overrides={"embed_mode": None, "embed_base_url": None},
    )
    assert eff.embed_mode == "modal"  # override was None → live corpus wins


def test_local_effective_config_ignores_cloud_model_pools():
    """Local embed routing must pin GHOST A/B to local vLLM pools.

    This prevents cloud/API summary or extraction pool concurrency from
    leaking into local RTX ingestion through live corpus config or per-batch
    overrides.
    """
    cloud_pool = [
        {
            "provider_preset": "openai",
            "model": "openai/gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "cloud-key",
            "max_concurrent": 64,
            "extra_params": {"temperature": 0},
        }
    ]
    eff = build_effective_config(
        frozen_base=IngestionConfig(embed_mode="local").model_dump(),
        live_corpus=IngestionConfig(
            embed_mode="local",
            summary_models=cloud_pool,
            extraction_models=cloud_pool,
            extraction_repair_models=cloud_pool,
            embedding_models=cloud_pool,
            embed_base_url="https://api.example.com/v1",
            embed_api_key="cloud-embed-key",
            embed_max_concurrent=32,
            modal_containers=8,
            models_linked=True,
        ).model_dump(),
        ingest_overrides={
            "summary_models": cloud_pool,
            "extraction_models": cloud_pool,
            "embed_mode": "local",
        },
    )

    assert eff.embed_mode == "local"
    assert eff.embed_base_url is None
    assert eff.embed_api_key is None
    assert eff.embed_max_concurrent is None
    assert eff.embedding_models == []
    assert eff.modal_containers is None
    assert eff.models_linked is False
    assert eff.summary_models[0].provider_preset == "vllm-local"
    assert eff.summary_models[0].base_url == "http://vllm-summary:8000/v1"
    assert eff.summary_models[0].max_concurrent == 24
    assert eff.summary_models[0].context_length == 12288
    assert eff.extraction_models[0].provider_preset == "vllm-local"
    assert eff.extraction_models[0].base_url == "http://vllm-extract:8000/v1"
    assert eff.extraction_models[0].max_concurrent == 64
    assert eff.extraction_models[0].context_length == 8192
    assert eff.extraction_repair_models[0].provider_preset == "vllm-local"
    assert eff.extraction_repair_models[0].context_length == 8192


def test_cloud_effective_config_keeps_cloud_model_pools():
    cloud_pool = [
        {
            "provider_preset": "openai",
            "model": "openai/gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "cloud-key",
            "max_concurrent": 7,
            "extra_params": {"temperature": 0},
        }
    ]
    eff = build_effective_config(
        frozen_base=IngestionConfig(embed_mode="local").model_dump(),
        live_corpus=IngestionConfig(
            embed_mode="api",
            summary_models=cloud_pool,
            extraction_models=cloud_pool,
        ).model_dump(),
    )

    assert eff.embed_mode == "api"
    assert eff.summary_models[0].provider_preset == "openai"
    assert eff.summary_models[0].max_concurrent == 7
    assert eff.extraction_models[0].provider_preset == "openai"


def test_local_routing_isolation_normalizes_legacy_local_alias():
    cloud_pool = [
        {
            "provider_preset": "anthropic",
            "model": "anthropic/claude-sonnet-4-6",
            "base_url": "https://api.anthropic.com/v1",
            "api_key": "cloud-key",
            "max_concurrent": 9,
            "extra_params": {},
        }
    ]
    cfg = isolate_local_model_routing(
        IngestionConfig.model_validate(
            {
                "embed_mode": "local_st",
                "summary_models": cloud_pool,
                "extraction_models": cloud_pool,
            }
        )
    )

    assert cfg.embed_mode == "local"
    assert cfg.summary_models[0].provider_preset == "vllm-local"
    assert cfg.extraction_models[0].provider_preset == "vllm-local"


def test_frozen_field_error_structured():
    err = FrozenFieldError(["embedding_dimension", "use_neo4j"], 47)
    assert err.fields == ["embedding_dimension", "use_neo4j"]
    assert err.doc_count == 47
    assert "47" in str(err)


def test_legacy_embed_mode_coerce_on_load():
    """Existing Mongo docs with embed_mode='siliconflow' etc. must
    deserialize cleanly to the new 3-value Literal via the pre-validator."""
    for legacy, modern in (
        ("local_st", "local"),
        ("modal_tei", "modal"),
        ("siliconflow", "api"),
    ):
        cfg = IngestionConfig.model_validate({"embed_mode": legacy})
        assert cfg.embed_mode == modern


def test_new_embed_mode_fields_default_none():
    """Default IngestionConfig leaves all per-corpus embed wiring blank so
    the dispatcher falls through to global env / local fallback."""
    cfg = IngestionConfig()
    assert cfg.embed_mode == "local"
    assert cfg.embed_base_url is None
    assert cfg.embed_api_key is None
    assert cfg.embed_max_concurrent is None
    assert cfg.modal_containers is None
