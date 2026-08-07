"""Prove worker Neo4j materialization goes through projection control plane."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _function_ast(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} missing in {path}")


def _names_and_attrs(fn: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                found.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                found.add(node.func.attr)
    return found


def test_worker_neo4j_phase_plans_projection_jobs():
    """_write_neo4j_for_doc must enter via project_document_via_control_plane."""

    path = Path(__file__).resolve().parents[1] / "services/ingestion/worker.py"
    fn = _function_ast(path, "_write_neo4j_for_doc")
    names = _names_and_attrs(fn)
    assert "project_document_via_control_plane" in names
    assert "write_document_graph" in names


def test_ingestion_services_route_neo4j_writes_via_control_plane():
    """No services/*.py may call write_document_graph outside the CP adapter."""

    root = Path(__file__).resolve().parents[1]
    allowed_direct = {
        "services/graph/neo4j_writer.py",
        "services/graph/projection_runner.py",
    }
    # Callers that may pass write_document_graph as write_fn to the CP entry.
    allowed_via_cp = {
        "services/ingestion/worker.py",
        "services/ingestion/graph_backfill.py",
    }
    direct_hits: list[str] = []
    for path in (root / "services").rglob("*.py"):
        rel = str(path.relative_to(root))
        if rel in allowed_direct or rel in allowed_via_cp:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "write_document_graph":
                direct_hits.append(rel)
                break
    assert direct_hits == [], f"direct Neo4j writer callers remain: {direct_hits}"


def test_worker_has_zero_direct_write_document_graph_calls():
    """Worker body must not Call write_document_graph; only pass it as write_fn."""

    path = Path(__file__).resolve().parents[1] / "services/ingestion/worker.py"
    fn = _function_ast(path, "_write_neo4j_for_doc")
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        assert name != "write_document_graph", (
            "direct worker Neo4j write Call remains; must use control plane"
        )


@pytest.mark.asyncio
async def test_replace_projection_reopens_certified_assertion_lane():
    """A document replacement must replay assertions removed by its pre-clear."""

    from services.graph import projection_runner

    plan = {
        "jobs": [
            {"graph_job_id": "assertion-job", "lane": "relation_assertions", "status": "CERTIFIED"},
            {"graph_job_id": "entity-job", "lane": "entity_mentions", "status": "CERTIFIED"},
        ]
    }
    write_fn = AsyncMock()
    with (
        patch.object(projection_runner, "plan_projection_jobs_for_document", AsyncMock(return_value=plan)),
        patch.object(projection_runner, "advance_job", AsyncMock()) as advance,
        patch.object(projection_runner, "run_projection_jobs", AsyncMock(return_value={"executed": 1})) as run,
    ):
        result = await projection_runner.project_document_via_control_plane(
            db=object(),
            neo4j_driver=object(),
            corpus_id="corpus-1",
            doc_id="document-1",
            write_fn=write_fn,
            write_kwargs={"sentinel": True},
        )

    write_fn.assert_awaited_once_with(sentinel=True)
    advance.assert_awaited_once()
    assert advance.await_args.kwargs["graph_job_id"] == "assertion-job"
    assert advance.await_args.kwargs["to_status"] == "PLANNED"
    run.assert_awaited_once()
    assert result["run"] == {"executed": 1}
