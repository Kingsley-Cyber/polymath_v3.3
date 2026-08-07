from types import SimpleNamespace

import pytest

from models.schemas import IngestionConfig
from services.ingestion.resource_planner import (
    SystemResources,
    _ru_maxrss_to_mb,
    classify_extraction_backend,
    classify_storage_mode,
    plan_ingestion_resources,
)


def _settings(**overrides):
    values = {
        "EXTRACTION_MAX_CONCURRENT": 8,
        "EXTRACTION_MAX_ACTIVE_DOCS": 1,
        "INGEST_MAX_MODEL_PHASE_DOCS": 1,
        "INGEST_BACKEND_RAM_TARGET_MB": 16_384,
        "INGEST_RSS_SOFT_LIMIT_RATIO": 0.85,
        "EMBED_BATCH_SIZE": 32,
        "QDRANT_INGEST_WRITE_CONCURRENCY": 2,
        "NEO4J_INGEST_WRITE_CONCURRENCY": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_ru_maxrss_normalizes_macos_bytes_and_linux_kib():
    assert _ru_maxrss_to_mb(300 * 1024 * 1024, platform="darwin") == 300
    assert _ru_maxrss_to_mb(300 * 1024, platform="linux") == 300


def test_graphify_cpu_is_the_only_active_extraction_backend():
    assert classify_extraction_backend(
        extraction_engine="graphify_cpu", extraction_pool=[]
    ) == ("local_cpu", ("local_cpu",))
    assert classify_extraction_backend(
        extraction_engine="off", extraction_pool=[]
    ) == ("off", ("off",))
    for retired in ("local", "cloud", "relex_local", "runpod_flash"):
        with pytest.raises(ValueError, match="unsupported extraction engine"):
            classify_extraction_backend(
                extraction_engine=retired, extraction_pool=[object()]
            )


def test_graphify_profile_ignores_provider_pool_and_uses_cpu_lane():
    cfg = IngestionConfig(extraction_engine="graphify_cpu", embed_mode="local")
    profile = plan_ingestion_resources(
        config=cfg,
        extraction_engine=cfg.extraction_engine,
        extraction_pool=[object()],
        settings=_settings(EXTRACTION_MAX_ACTIVE_DOCS=2),
        resources=SystemResources(
            cpu_cores=12,
            ram_total_mb=32_768,
            process_rss_mb=1_024,
            metal_available=True,
        ),
    )
    assert profile.extraction_backend == "local_cpu"
    assert profile.extraction_lanes == ("local_cpu",)
    assert profile.extraction_active_docs == 2
    assert profile.embedding_backend == "local_metal"
    assert profile.recommended_ingest_profile == "mac_queryable_first"


def test_high_rss_warns_and_caps_embedding_batch():
    cfg = IngestionConfig()
    profile = plan_ingestion_resources(
        config=cfg,
        extraction_engine="graphify_cpu",
        extraction_pool=[],
        settings=_settings(EMBED_BATCH_SIZE=64, INGEST_BACKEND_RAM_TARGET_MB=4_096),
        resources=SystemResources(
            cpu_cores=8,
            ram_total_mb=8_192,
            process_rss_mb=3_900,
            metal_available=False,
        ),
    )
    assert profile.warnings
    assert profile.embedding_batch_size == 16


def test_storage_mode_classification():
    assert classify_storage_mode("/Users/king/library") == "local_disk"
    assert classify_storage_mode("/Volumes/Flash Drive/books") == "mounted_volume"
    assert classify_storage_mode("smb://nas/books") == "network_share"
