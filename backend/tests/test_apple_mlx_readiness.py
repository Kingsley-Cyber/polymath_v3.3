from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_apple_compose_sets_safe_ingest_backpressure_defaults():
    compose = (ROOT / "docker-compose.apple-mlx.yml").read_text()

    for marker in [
        "INGEST_MAX_ACTIVE_JOBS: ${APPLE_INGEST_MAX_ACTIVE_JOBS:-4}",
        "INGEST_MAX_PARSE_JOBS: ${APPLE_INGEST_MAX_PARSE_JOBS:-1}",
        "INGEST_MAX_MODEL_PHASE_DOCS: ${APPLE_INGEST_MAX_MODEL_PHASE_DOCS:-1}",
        "GRAPH_CACHE_WARMUP_DEBOUNCE_SECONDS: ${APPLE_GRAPH_CACHE_WARMUP_DEBOUNCE_SECONDS:-300}",
        "EMBEDDER_MEMORY_GUARD_ENABLED: ${APPLE_EMBEDDER_MEMORY_GUARD_ENABLED:-true}",
        "EMBEDDER_MIN_FREE_MB: ${APPLE_EMBEDDER_MIN_FREE_MB:-2048}",
    ]:
        assert marker in compose


def test_apple_sidecars_expose_unified_memory_contract():
    requirements = (ROOT / "scripts/apple_ml_services/requirements.txt").read_text()
    assert "psutil" in requirements

    for rel in [
        "scripts/apple_ml_services/embedder_mlx/main.py",
        "scripts/apple_ml_services/reranker_mlx/main.py",
        "scripts/apple_ml_services/docling_svc/main.py",
    ]:
        source = (ROOT / rel).read_text()
        assert "psutil.virtual_memory()" in source
        assert '"gpu_free_mb"' in source
        assert '"gpu_total_mb"' in source
        assert '"memory_pressure"' in source


def test_worker_uses_configurable_graph_warmup_debounce():
    source = (ROOT / "backend/services/ingestion/worker.py").read_text()

    assert "settings.GRAPH_CACHE_WARMUP_ENABLED" in source
    assert "settings.GRAPH_CACHE_WARMUP_DEBOUNCE_SECONDS" in source


def test_apple_reranker_uses_official_mlx_contract_and_no_zero_score_scaffold():
    source = (ROOT / "scripts/apple_ml_services/reranker_mlx/main.py").read_text()

    assert 'MODEL_ID = "jinaai/jina-reranker-v3-mlx"' in source
    assert "MLXReranker" in source
    assert "projector.safetensors" in source
    assert "class RankedResult" in source
    assert "results: list[RankedResult]" in source
    assert "scores: list[float]" not in source
    assert "return [0.0] * len(documents)" not in source


def test_apple_model_pull_includes_official_reranker_helper():
    source = (ROOT / "scripts/pull_apple_mlx_models.py").read_text()

    assert '"jinaai/jina-reranker-v3-mlx"' in source
    assert '"rerank.py"' in source


def test_apple_smoke_fails_hard_when_reranker_is_not_ready_or_misordered():
    source = (ROOT / "scripts/smoke_apple_mlx.sh").read_text()

    assert '"$(echo "${RERANK_INFO}" | jq -r \'.ready\')" != "true"' in source
    assert "expected results[{index,score,text}]" in source
    assert "exit 1" in source


def test_installer_refreshes_repo_sidecars_by_default():
    source = (ROOT / "scripts/install_apple_mlx_runtime.sh").read_text()

    assert "POLYMATH_APPLE_MLX_PROTECT_HOST_SIDECARS:-0" in source
    assert "By default the repo is canonical" in source
