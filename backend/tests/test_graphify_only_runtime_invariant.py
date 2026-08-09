"""Structural invariants for the single production Graphify extraction route."""

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from models.schemas import ExtractionSettings, IngestionConfig


BACKEND = Path(__file__).resolve().parents[1]
LIVE_ENTRYPOINTS = (
    BACKEND / "services" / "ingestion" / "worker.py",
    BACKEND / "services" / "ingestion" / "graph_backfill.py",
    BACKEND / "routers" / "ingestion.py",
    BACKEND / "routers" / "settings.py",
    BACKEND / "services" / "control_plane" / "desired_state.py",
)
RETIRED_MODULES = {
    "services.ingestion.relex_local",
    "services.runpod_local_extraction",
    "services.runpod_flash_extraction",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_runtime_engine_schema_allows_only_graphify_and_off():
    assert IngestionConfig().extraction_engine == "graphify_cpu"
    assert ExtractionSettings().engine == "graphify_cpu"
    assert IngestionConfig(extraction_engine="off").extraction_engine == "off"
    for retired in ("local", "cloud", "relex_local", "runpod_flash", "gliner", "glirel"):
        with pytest.raises(ValidationError):
            IngestionConfig(extraction_engine=retired)
        with pytest.raises(ValidationError):
            ExtractionSettings(engine=retired)


def test_live_entrypoints_do_not_import_retired_extractors():
    violations = {
        str(path.relative_to(BACKEND)): sorted(_imports(path) & RETIRED_MODULES)
        for path in LIVE_ENTRYPOINTS
        if _imports(path) & RETIRED_MODULES
    }
    assert violations == {}


def test_worker_invokes_graphify_pipeline_and_has_no_retired_router_tokens():
    source = (BACKEND / "services" / "ingestion" / "worker.py").read_text(
        encoding="utf-8"
    )
    assert "run_graphify_pipeline" in source
    for token in (
        "relex_extract_entities",
        "runpod_extract_entities",
        "cloud_extract_entities",
        "_runpod_extractor_for_config",
    ):
        assert token not in source


def test_offline_relex_baseline_is_explicitly_preserved():
    runner = BACKEND / "scripts" / "run_relex_fixture_baseline.py"
    assert runner.is_file()
    source = runner.read_text(encoding="utf-8")
    assert "relex_local" in source
    assert "frozen_baseline" in source
