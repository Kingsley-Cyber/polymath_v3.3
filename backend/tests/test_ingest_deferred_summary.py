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
        }

    def __getitem__(self, name: str) -> _Collection:
        return self.collections.setdefault(name, _Collection())


class _IngestionService:
    async def backfill_parent_summaries(self, *_args: Any, **_kwargs: Any) -> dict:
        return {
            "status": "healthy",
            "generated": 1,
            "indexed": 1,
            "generation_errors": [],
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
    result = await batches._run_deferred_summary_backfill(
        db=db,
        batch={
            "batch_id": "batch-12345678",
            "corpus_id": "corpus-1",
            "user_id": "user-1",
            "options": {"profile": "rtx_assisted", "chunk_summarization": True},
        },
        ingestion_service=_IngestionService(),
    )

    assert result["status"] == "healthy"
    run_update = db["ingest_repair_runs"].updates[0]["update"]
    assert "created_at" in run_update["$setOnInsert"]
    assert "created_at" not in run_update["$set"]

