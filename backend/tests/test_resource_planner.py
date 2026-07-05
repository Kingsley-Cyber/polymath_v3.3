from __future__ import annotations

from types import SimpleNamespace

from models.schemas import IngestionConfig, ModelProfileRef
from services.ingestion.resource_planner import (
    SystemResources,
    classify_storage_mode,
    plan_ingestion_resources,
)


def _settings(**overrides):
    base = {
        "EXTRACTION_MAX_CONCURRENT": 8,
        "EXTRACTION_GLOBAL_MAX_CONCURRENT": 180,
        "EXTRACTION_MAX_ACTIVE_DOCS": 1,
        "EXTRACTION_MANAGED_VLLM_MAX_ACTIVE_DOCS": 2,
        "INGEST_MAX_MODEL_PHASE_DOCS": 1,
        "INGEST_MANAGED_VLLM_MODEL_PHASE_DOCS": 2,
        "INGEST_BACKEND_RAM_TARGET_MB": 16_384,
        "INGEST_RSS_SOFT_LIMIT_RATIO": 0.85,
        "INGEST_REMOTE_VLLM_TWO_DOC_RSS_RATIO": 0.75,
        "EMBED_BATCH_SIZE": 32,
        "QDRANT_INGEST_WRITE_CONCURRENCY": 2,
        "NEO4J_INGEST_WRITE_CONCURRENCY": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _rtx_pool(concurrency: int = 60) -> list[ModelProfileRef]:
    return [
        ModelProfileRef(
            provider_preset="vllm-rtx",
            model="openai/polymath-extract",
            base_url="http://192.168.1.83:8000/v1",
            max_concurrent=concurrency,
        )
    ]


def test_remote_vllm_profile_uses_one_doc_under_tight_backend_cap():
    cfg = IngestionConfig(
        extraction_engine="cloud",
        models_linked=False,
        extraction_models=_rtx_pool(),
        embed_mode="local",
    )
    profile = plan_ingestion_resources(
        config=cfg,
        extraction_engine="cloud",
        extraction_pool=cfg.extraction_models,
        settings=_settings(),
        resources=SystemResources(
            cpu_cores=12,
            ram_total_mb=65_536,
            cgroup_limit_mb=4_096,
            process_rss_mb=3_600,
            metal_available=True,
        ),
    )

    assert "remote_vllm" in profile.extraction_lanes
    assert profile.extraction_max_concurrent == 60
    assert profile.extraction_active_docs == 1
    assert profile.model_phase_docs == 1
    assert profile.ram_cap_mb == 4_096
    assert profile.embedding_backend == "local_metal"
    assert profile.warnings


def test_remote_vllm_profile_can_admit_two_docs_when_roomy():
    cfg = IngestionConfig(
        extraction_engine="cloud",
        models_linked=False,
        extraction_models=_rtx_pool(),
        embed_mode="local",
    )
    profile = plan_ingestion_resources(
        config=cfg,
        extraction_engine="cloud",
        extraction_pool=cfg.extraction_models,
        settings=_settings(),
        resources=SystemResources(
            cpu_cores=12,
            ram_total_mb=65_536,
            cgroup_limit_mb=32_768,
            process_rss_mb=1_024,
            metal_available=True,
        ),
    )

    assert profile.extraction_active_docs == 2
    assert profile.model_phase_docs == 2
    assert profile.embedding_batch_size >= 64
    assert "does not reserve Mac Metal" in " ".join(profile.notes)


def test_remote_vllm_profile_admits_two_docs_under_low_4gb_pressure():
    cfg = IngestionConfig(
        extraction_engine="cloud",
        models_linked=False,
        extraction_models=_rtx_pool(),
        embed_mode="local",
    )
    profile = plan_ingestion_resources(
        config=cfg,
        extraction_engine="cloud",
        extraction_pool=cfg.extraction_models,
        settings=_settings(),
        resources=SystemResources(
            cpu_cores=10,
            ram_total_mb=12_288,
            cgroup_limit_mb=4_096,
            process_rss_mb=1_024,
            metal_available=False,
        ),
    )

    assert profile.extraction_active_docs == 2
    assert profile.model_phase_docs == 2
    assert profile.extraction_max_concurrent == 60


def test_remote_vllm_profile_keeps_two_docs_under_moderate_5gb_pressure():
    cfg = IngestionConfig(
        extraction_engine="cloud",
        models_linked=False,
        extraction_models=_rtx_pool(),
        embed_mode="local",
    )
    profile = plan_ingestion_resources(
        config=cfg,
        extraction_engine="cloud",
        extraction_pool=cfg.extraction_models,
        settings=_settings(),
        resources=SystemResources(
            cpu_cores=12,
            ram_total_mb=65_536,
            cgroup_limit_mb=5_120,
            process_rss_mb=2_747,
            metal_available=True,
        ),
    )

    assert profile.rss_soft_limit_mb == 4_352
    assert profile.extraction_active_docs == 2
    assert profile.model_phase_docs == 2


def test_openai_cloud_pool_is_not_classified_as_remote_vllm():
    cfg = IngestionConfig(
        extraction_engine="cloud",
        models_linked=False,
        extraction_models=[
            ModelProfileRef(
                provider_preset="openai",
                model="openai/gpt-4o",
                base_url="https://api.openai.com/v1",
                max_concurrent=8,
            )
        ],
        embed_mode="api",
    )
    profile = plan_ingestion_resources(
        config=cfg,
        extraction_engine="cloud",
        extraction_pool=cfg.extraction_models,
        settings=_settings(),
        resources=SystemResources(cpu_cores=8, ram_total_mb=16_384),
    )

    assert profile.extraction_backend == "cloud_api"
    assert profile.embedding_backend == "remote"
    assert profile.extraction_max_concurrent == 8


def test_local_mac_llm_with_local_metal_embeddings_pins_doc_fanout():
    cfg = IngestionConfig(
        extraction_engine="local",
        embed_mode="local",
    )
    profile = plan_ingestion_resources(
        config=cfg,
        extraction_engine="local",
        extraction_pool=[],
        settings=_settings(
            EXTRACTION_MAX_ACTIVE_DOCS=4,
            INGEST_MAX_MODEL_PHASE_DOCS=4,
            EMBED_BATCH_SIZE=64,
        ),
        resources=SystemResources(
            cpu_cores=12,
            ram_total_mb=32_768,
            process_rss_mb=1_024,
            metal_available=True,
        ),
    )

    assert profile.extraction_backend == "local_mac_llm"
    assert profile.extraction_active_docs == 1
    assert profile.model_phase_docs == 1
    assert profile.embedding_batch_size == 16


def test_storage_mode_classification():
    assert classify_storage_mode("/Users/king/library") == "local_disk"
    assert classify_storage_mode("/Volumes/Flash Drive/books") == "mounted_volume"
    assert classify_storage_mode("smb://nas/books") == "network_share"
