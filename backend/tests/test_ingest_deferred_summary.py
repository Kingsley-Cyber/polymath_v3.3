from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from services.ingestion import batches


class _AsyncCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __aiter__(self) -> "_AsyncCursor":
        self._iter = iter(self._rows)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _Collection:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.updates: list[dict[str, Any]] = []

    def find(self, *_args: Any, **_kwargs: Any) -> _AsyncCursor:
        return _AsyncCursor(self.rows)

    async def find_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    async def count_documents(self, query: dict[str, Any]) -> int:
        rows = list(self.rows)
        doc_scope = query.get("doc_id") or {}
        if isinstance(doc_scope, dict) and doc_scope.get("$in") is not None:
            allowed = set(doc_scope["$in"])
            rows = [row for row in rows if row.get("doc_id") in allowed]
        if "summary" in query:
            rows = [row for row in rows if str(row.get("summary") or "").strip()]
        return len(rows)

    async def update_one(
        self,
        _filter: dict[str, Any],
        update: dict[str, Any],
        *,
        upsert: bool = False,
    ) -> None:
        set_on_insert = set((update.get("$setOnInsert") or {}).keys())
        set_fields = set((update.get("$set") or {}).keys())
        overlap = set_on_insert & set_fields
        assert not overlap, f"conflicting Mongo update paths: {sorted(overlap)}"
        self.updates.append({"filter": _filter, "update": update, "upsert": upsert})


class _Db:
    def __init__(self) -> None:
        self.collections = {
            batches.ITEMS: _Collection([{"doc_id": "doc-1"}, {"doc_id": "doc-1"}]),
            batches.BATCHES: _Collection(),
            "ingest_repair_runs": _Collection(),
            "parent_chunks": _Collection(
                [{"doc_id": "doc-1", "chunk_kind": "body", "summary": "done"}]
            ),
        }

    def __getitem__(self, name: str) -> _Collection:
        return self.collections.setdefault(name, _Collection())


class _IngestionService:
    def __init__(self) -> None:
        self.plan_calls: list[dict[str, Any]] = []
        self.run_calls: list[dict[str, Any]] = []

    async def backfill_parent_summaries(self, *_args: Any, **_kwargs: Any) -> dict:
        raise AssertionError("batch completion must use durable summary jobs")

    async def plan_summary_jobs(self, **kwargs: Any) -> dict:
        self.plan_calls.append(kwargs)
        return {
            "status": "complete",
            "planned": 1,
            "counts": {"queued": 1},
        }

    async def run_summary_jobs(self, **kwargs: Any) -> dict:
        self.run_calls.append(kwargs)
        is_parent = kwargs.get("kinds") == ["retrieval_parent_summary"]
        prior_parent_calls = sum(
            call.get("kinds") == ["retrieval_parent_summary"]
            for call in self.run_calls[:-1]
        )
        if is_parent and prior_parent_calls:
            return {
                "status": "empty",
                "claimed": 0,
                "counts": {},
                "runner_results": {},
            }
        return {
            "status": "complete",
            "claimed": 1,
            "counts": {"succeeded": 1},
            "runner_results": {},
            "batch_reconciliation": {"status": "complete", "promoted": 1},
        }


@pytest.mark.asyncio
async def test_deferred_summary_run_record_has_no_created_at_update_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_build_item_config(**_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(chunk_summarization=True)

    monkeypatch.setattr(
        batches,
        "get_settings",
        lambda: SimpleNamespace(
            INGEST_DEFERRED_SUMMARY_BACKFILL_ENABLED=True,
            INGEST_DEFERRED_SUMMARY_BACKFILL_LIMIT=5,
            INGEST_DEFERRED_SUMMARY_BACKFILL_BATCH=2,
        ),
    )
    monkeypatch.setattr(batches, "_build_item_config", fake_build_item_config)

    db = _Db()
    ingestion_service = _IngestionService()
    result = await batches._run_deferred_summary_backfill(
        db=db,
        batch={
            "batch_id": "batch-12345678",
            "corpus_id": "corpus-1",
            "user_id": "user-1",
            "options": {"profile": "rtx_assisted", "chunk_summarization": True},
        },
        ingestion_service=ingestion_service,
    )

    assert result["status"] == "complete"
    assert result["parent_summary_jobs"]["plan"]["planned"] == 1
    assert result["parent_summary_jobs"]["missing_parent_count"] == 0
    assert result["document_summary_jobs"]["plan"]["planned"] == 1
    assert result["document_summary_jobs"]["run"]["counts"] == {"succeeded": 1}
    assert ingestion_service.plan_calls[0]["kinds"] == [
        "retrieval_parent_summary"
    ]
    assert ingestion_service.plan_calls[0]["doc_ids"] == ["doc-1"]
    assert ingestion_service.plan_calls[1]["kinds"] == ["document_summary"]
    assert ingestion_service.run_calls[0]["statuses"] == ["queued"]
    assert ingestion_service.run_calls[-1]["kinds"] == ["document_summary"]
    run_update = db["ingest_repair_runs"].updates[0]["update"]
    assert "created_at" in run_update["$setOnInsert"]
    assert "created_at" not in run_update["$set"]
