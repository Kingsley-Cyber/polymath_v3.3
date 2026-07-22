"""Durable research jobs, artifact metadata, and safe file downloads."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from models.research import (
    ResearchArtifact,
    ResearchArtifactFormat,
    ResearchBudgets,
    ResearchJob,
    ResearchJobCreate,
    ResearchJobStatus,
    ResearchTraceEvent,
)


def _now() -> datetime:
    return datetime.utcnow()


def _mode_budgets(mode: str) -> ResearchBudgets:
    if mode == "quick":
        return ResearchBudgets(
            max_subquestions=4,
            max_tool_calls=12,
            max_graph_hops=1,
            max_evidence_items=40,
            max_output_tokens=6000,
        )
    if mode == "deep":
        return ResearchBudgets(
            max_subquestions=12,
            max_tool_calls=80,
            max_graph_hops=3,
            max_evidence_items=160,
            max_output_tokens=24000,
        )
    return ResearchBudgets()


def _safe_leaf_name(name: str) -> str:
    leaf = Path(name or "artifact").name.strip()
    leaf = re.sub(r"[^A-Za-z0-9._ -]+", "_", leaf).strip(" .")
    return leaf or "artifact"


def _artifact_root() -> Path:
    configured = os.environ.get("POLYMATH_RESEARCH_ARTIFACT_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    app_root = Path("/app")
    if app_root.exists() and os.access(app_root, os.W_OK):
        return (app_root / "runtime" / "research_artifacts").resolve()
    return (Path(__file__).resolve().parents[3] / "runtime" / "research_artifacts").resolve()


def _require_under_root(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("research artifact path escapes artifact root") from exc
    return resolved


@dataclass(frozen=True)
class ResearchArtifactDownload:
    path: Path
    filename: str
    mime_type: str
    artifact: ResearchArtifact


class ResearchService:
    jobs_collection = "research_jobs"
    artifacts_collection = "research_artifacts"
    events_collection = "research_trace_events"

    @property
    def artifact_root(self) -> Path:
        return _artifact_root()

    async def create_job(
        self,
        db: Any,
        *,
        user_id: str,
        request: ResearchJobCreate,
    ) -> ResearchJob:
        now = _now()
        job_id = f"research_{uuid4().hex}"
        budgets = request.budgets or _mode_budgets(request.mode)
        doc = {
            "_id": job_id,
            "job_id": job_id,
            "user_id": user_id,
            "status": "queued",
            "question": request.question.strip(),
            "corpus_ids": [str(cid) for cid in request.corpus_ids],
            "mode": request.mode,
            "budgets": budgets.model_dump(),
            "artifact_ids": [],
            "error": None,
            "metadata": dict(request.metadata or {}),
            "created_at": now,
            "updated_at": now,
        }
        await db[self.jobs_collection].insert_one(doc)
        await self.add_event(
            db,
            user_id=user_id,
            job_id=job_id,
            stage="job",
            status="queued",
            message="Research job queued.",
            metadata={"mode": request.mode, "corpus_ids": doc["corpus_ids"]},
        )
        return ResearchJob.model_validate(doc)

    async def list_jobs(
        self,
        db: Any,
        *,
        user_id: str,
        limit: int = 50,
        status: str | None = None,
    ) -> list[ResearchJob]:
        query: dict[str, Any] = {"user_id": user_id}
        if status:
            query["status"] = status
        cursor = (
            db[self.jobs_collection]
            .find(query, {"_id": 0})
            .sort("created_at", -1)
            .limit(max(1, min(int(limit or 50), 100)))
        )
        return [ResearchJob.model_validate(row) async for row in cursor]

    async def get_job(self, db: Any, *, user_id: str, job_id: str) -> ResearchJob | None:
        row = await db[self.jobs_collection].find_one(
            {"job_id": job_id, "user_id": user_id},
            {"_id": 0},
        )
        return ResearchJob.model_validate(row) if row else None

    async def update_job_status(
        self,
        db: Any,
        *,
        user_id: str,
        job_id: str,
        status: ResearchJobStatus,
        error: str | None = None,
    ) -> ResearchJob | None:
        update: dict[str, Any] = {
            "status": status,
            "updated_at": _now(),
        }
        if error is not None:
            update["error"] = error
        await db[self.jobs_collection].update_one(
            {"job_id": job_id, "user_id": user_id},
            {"$set": update},
        )
        await self.add_event(
            db,
            user_id=user_id,
            job_id=job_id,
            stage="job",
            status=status,
            message=f"Research job {status}.",
            metadata={"error": error} if error else {},
        )
        return await self.get_job(db, user_id=user_id, job_id=job_id)

    async def add_event(
        self,
        db: Any,
        *,
        user_id: str,
        job_id: str,
        stage: str,
        status: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> ResearchTraceEvent:
        now = _now()
        event_id = f"research_event_{uuid4().hex}"
        doc = {
            "_id": event_id,
            "event_id": event_id,
            "job_id": job_id,
            "user_id": user_id,
            "stage": stage,
            "status": status,
            "message": message,
            "metadata": dict(metadata or {}),
            "created_at": now,
        }
        await db[self.events_collection].insert_one(doc)
        return ResearchTraceEvent.model_validate(doc)

    async def list_events(
        self,
        db: Any,
        *,
        user_id: str,
        job_id: str,
        limit: int = 200,
    ) -> list[ResearchTraceEvent]:
        cursor = (
            db[self.events_collection]
            .find({"job_id": job_id, "user_id": user_id}, {"_id": 0})
            .sort("created_at", 1)
            .limit(max(1, min(int(limit or 200), 1000)))
        )
        return [ResearchTraceEvent.model_validate(row) async for row in cursor]

    async def store_artifact(
        self,
        db: Any,
        *,
        user_id: str,
        job_id: str,
        filename: str,
        content: bytes,
        artifact_format: ResearchArtifactFormat,
        mime_type: str,
    ) -> ResearchArtifact:
        job = await self.get_job(db, user_id=user_id, job_id=job_id)
        if job is None:
            raise FileNotFoundError("research job not found")
        root = self.artifact_root
        artifact_id = f"research_artifact_{uuid4().hex}"
        safe_filename = _safe_leaf_name(filename)
        job_dir = _require_under_root(root / job_id, root)
        job_dir.mkdir(parents=True, exist_ok=True)
        path = _require_under_root(job_dir / f"{artifact_id}_{safe_filename}", root)
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        now = _now()
        doc = {
            "_id": artifact_id,
            "artifact_id": artifact_id,
            "job_id": job_id,
            "user_id": user_id,
            "format": artifact_format,
            "filename": safe_filename,
            "mime_type": mime_type,
            "storage_path": str(path),
            "sha256": digest,
            "size_bytes": len(content),
            "created_at": now,
        }
        await db[self.artifacts_collection].insert_one(doc)
        await db[self.jobs_collection].update_one(
            {"job_id": job_id, "user_id": user_id},
            {
                "$addToSet": {"artifact_ids": artifact_id},
                "$set": {"updated_at": now},
            },
        )
        await self.add_event(
            db,
            user_id=user_id,
            job_id=job_id,
            stage="artifact",
            status="created",
            message=f"Research artifact created: {safe_filename}",
            metadata={
                "artifact_id": artifact_id,
                "format": artifact_format,
                "sha256": digest,
            },
        )
        return ResearchArtifact.model_validate(doc)

    async def list_artifacts(
        self,
        db: Any,
        *,
        user_id: str,
        job_id: str,
    ) -> list[ResearchArtifact]:
        job = await self.get_job(db, user_id=user_id, job_id=job_id)
        if job is None:
            return []
        cursor = (
            db[self.artifacts_collection]
            .find({"job_id": job_id, "user_id": user_id}, {"_id": 0})
            .sort("created_at", 1)
        )
        return [ResearchArtifact.model_validate(row) async for row in cursor]

    async def resolve_download(
        self,
        db: Any,
        *,
        user_id: str,
        artifact_id: str,
    ) -> ResearchArtifactDownload | None:
        row = await db[self.artifacts_collection].find_one(
            {"artifact_id": artifact_id, "user_id": user_id},
            {"_id": 0},
        )
        if not row:
            return None
        artifact = ResearchArtifact.model_validate(row)
        job = await self.get_job(db, user_id=user_id, job_id=artifact.job_id)
        if job is None:
            return None
        root = self.artifact_root
        path = _require_under_root(Path(artifact.storage_path), root)
        if not path.is_file():
            raise FileNotFoundError("research artifact file is missing")
        return ResearchArtifactDownload(
            path=path,
            filename=artifact.filename,
            mime_type=artifact.mime_type,
            artifact=artifact,
        )


research_service = ResearchService()
