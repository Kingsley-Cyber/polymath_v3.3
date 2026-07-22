from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from models.research import ResearchJobCreate
from services.research.artifacts import ResearchService
from services.research.worker import run_research_job


class _AsyncCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, *_args):
        return self

    def limit(self, limit):
        self.rows = self.rows[:limit]
        return self

    def __aiter__(self):
        self._iter = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _Collection:
    def __init__(self):
        self.rows = {}

    async def insert_one(self, doc):
        self.rows[doc["_id"]] = dict(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def find_one(self, query, projection=None):
        for row in self.rows.values():
            if all(row.get(k) == v for k, v in query.items() if not k.startswith("$")):
                return {k: v for k, v in row.items() if k != "_id"} if projection else dict(row)
        return None

    def find(self, query, projection=None):
        rows = []
        for row in self.rows.values():
            if all(row.get(k) == v for k, v in query.items()):
                rows.append({k: v for k, v in row.items() if k != "_id"} if projection else dict(row))
        return _AsyncCursor(rows)

    async def update_one(self, query, update):
        row = await self.find_one(query)
        if not row:
            return SimpleNamespace(modified_count=0)
        stored = self.rows[row.get("_id") or row.get("job_id") or row.get("artifact_id")]
        if "$set" in update:
            stored.update(update["$set"])
        if "$addToSet" in update:
            for key, value in update["$addToSet"].items():
                stored.setdefault(key, [])
                if value not in stored[key]:
                    stored[key].append(value)
        return SimpleNamespace(modified_count=1)


class _Db(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = _Collection()
        return dict.__getitem__(self, name)


@pytest.mark.asyncio
async def test_research_job_artifact_download_is_id_level_and_root_scoped(tmp_path, monkeypatch):
    db = _Db()
    service = ResearchService()
    monkeypatch.setenv("POLYMATH_RESEARCH_ARTIFACT_DIR", str(tmp_path))

    job = await service.create_job(
        db,
        user_id="user-1",
        request=ResearchJobCreate(
            question="Map the graph dependencies.",
            corpus_ids=["corp-1"],
            mode="quick",
        ),
    )
    artifact = await service.store_artifact(
        db,
        user_id="user-1",
        job_id=job.job_id,
        filename="../report.md",
        content=b"# Report\n",
        artifact_format="markdown",
        mime_type="text/markdown",
    )

    resolved = await service.resolve_download(
        db,
        user_id="user-1",
        artifact_id=artifact.artifact_id,
    )

    assert resolved is not None
    assert resolved.filename == "report.md"
    assert resolved.path.read_bytes() == b"# Report\n"
    assert Path(resolved.path).resolve().is_relative_to(tmp_path.resolve())
    assert await service.resolve_download(
        db,
        user_id="user-2",
        artifact_id=artifact.artifact_id,
    ) is None


@pytest.mark.asyncio
async def test_run_research_job_renders_markdown_html_and_json_artifacts(tmp_path, monkeypatch):
    db = _Db()
    service = ResearchService()
    monkeypatch.setenv("POLYMATH_RESEARCH_ARTIFACT_DIR", str(tmp_path))

    class FakeRetriever:
        async def retrieve(self, **kwargs):
            return SimpleNamespace(
                chunks=[
                    SimpleNamespace(
                        corpus_id="corp-1",
                        doc_id="doc-1",
                        chunk_id="chunk-1",
                        parent_id="parent-1",
                        text="Graph traversal connects evidence nodes into research context.",
                        summary=None,
                        score=0.95,
                        source_tier="parent",
                        chunk_kind="body",
                        metadata={},
                    )
                ],
                diagnostics={},
            )

    job = await service.create_job(
        db,
        user_id="user-1",
        request=ResearchJobCreate(
            question="How should graph traversal support autoresearch?",
            corpus_ids=["corp-1"],
            mode="quick",
        ),
    )

    final_job = await run_research_job(
        db,
        user_id="user-1",
        job_id=job.job_id,
        retriever=FakeRetriever(),
        graph_driver=None,
        qdrant=None,
    )
    artifacts = await service.list_artifacts(
        db,
        user_id="user-1",
        job_id=job.job_id,
    )
    events = await service.list_events(
        db,
        user_id="user-1",
        job_id=job.job_id,
    )

    assert final_job.status == "done"
    assert {artifact.format for artifact in artifacts} == {"markdown", "html", "json"}
    markdown = next(artifact for artifact in artifacts if artifact.format == "markdown")
    html = next(artifact for artifact in artifacts if artifact.format == "html")
    resolved = await service.resolve_download(
        db,
        user_id="user-1",
        artifact_id=markdown.artifact_id,
    )
    assert resolved is not None
    text = resolved.path.read_text()
    assert "[C1]" in text
    assert "Graph traversal" in text
    resolved_html = await service.resolve_download(
        db,
        user_id="user-1",
        artifact_id=html.artifact_id,
    )
    assert resolved_html is not None
    assert "<html" in resolved_html.path.read_text()
    assert any(event.stage == "context" and event.status == "done" for event in events)
    assert any(event.stage == "retrieval" and event.status == "done" for event in events)


@pytest.mark.asyncio
async def test_run_research_job_graph_lane_fallback_does_not_collapse_report(tmp_path, monkeypatch):
    db = _Db()
    service = ResearchService()
    monkeypatch.setenv("POLYMATH_RESEARCH_ARTIFACT_DIR", str(tmp_path))

    class FakeRetriever:
        async def retrieve(self, **kwargs):
            return SimpleNamespace(
                chunks=[
                    SimpleNamespace(
                        corpus_id="corp-1",
                        doc_id="doc-1",
                        chunk_id="chunk-1",
                        parent_id="parent-1",
                        text="Retrieval evidence must survive graph lane failure.",
                        summary=None,
                        score=0.9,
                        source_tier="parent",
                        chunk_kind="body",
                        metadata={},
                    )
                ],
                diagnostics={},
            )

    async def failing_graph_lane(**kwargs):
        raise TimeoutError("neo4j pressure")

    monkeypatch.setattr("services.research.worker._run_graph_lane", failing_graph_lane)
    job = await service.create_job(
        db,
        user_id="user-1",
        request=ResearchJobCreate(
            question="Can orchestration survive graph failure?",
            corpus_ids=["corp-1"],
            mode="quick",
        ),
    )

    final_job = await run_research_job(
        db,
        user_id="user-1",
        job_id=job.job_id,
        retriever=FakeRetriever(),
        graph_driver=object(),
        qdrant=None,
    )
    artifacts = await service.list_artifacts(
        db,
        user_id="user-1",
        job_id=job.job_id,
    )
    events = await service.list_events(
        db,
        user_id="user-1",
        job_id=job.job_id,
    )

    assert final_job.status == "done"
    assert {artifact.format for artifact in artifacts} == {"markdown", "html", "json"}
    assert any(event.stage == "retrieval" and event.status == "done" for event in events)
    assert any(event.stage == "graph" and event.status == "fallback" for event in events)
    assert any(event.stage == "context" and event.status == "done" for event in events)
