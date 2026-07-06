from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("LITELLM_MASTER_KEY", "test-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-password")

from services.ghost_b import ExtractionBatchReport, ExtractionResult
from services.ingestion import batches, graph_backfill
from models.schemas import IngestionConfig


def test_discover_local_files_filters_and_sorts(tmp_path):
    (tmp_path / "b.pdf").write_bytes(b"b")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "skip.png").write_bytes(b"png")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "c.md").write_text("# c", encoding="utf-8")

    root, files = batches.discover_local_files(
        str(tmp_path),
        recursive=True,
        extensions=["txt", ".md"],
    )

    assert root == tmp_path.resolve()
    assert [path.name for path in files] == ["a.txt", "c.md"]


def test_discover_local_files_respects_max_files(tmp_path):
    for idx in range(5):
        (tmp_path / f"{idx}.pdf").write_bytes(b"x")

    _root, files = batches.discover_local_files(str(tmp_path), max_files=2)

    assert len(files) == 2


def test_runtime_batch_concurrency_caps_instead_of_floors():
    settings = SimpleNamespace(
        INGEST_BATCH_WORKERS=2,
        INGEST_GLOBAL_MAX_DOCS=3,
        INGEST_MAX_ACTIVE_JOBS=16,
    )
    batch = {"options": {"concurrency": 2}}

    assert batches._runtime_batch_concurrency(batch, settings) == 2


def test_runtime_batch_concurrency_uses_worker_default_when_unset():
    settings = SimpleNamespace(
        INGEST_BATCH_WORKERS=4,
        INGEST_GLOBAL_MAX_DOCS=2,
        INGEST_MAX_ACTIVE_JOBS=16,
    )
    batch = {"options": {}}

    assert batches._runtime_batch_concurrency(batch, settings) == 2


def test_normalize_profile_rejects_unknown_value():
    assert batches._normalize_profile("RTX_ASSISTED") == "rtx_assisted"
    assert batches._normalize_profile("MAC_QUERYABLE_FIRST") == "mac_queryable_first"
    assert batches._normalize_profile("") is None
    with pytest.raises(ValueError, match="Unknown ingest profile"):
        batches._normalize_profile("turbo_mystery")


def test_mac_profiles_are_queryable_first():
    assert batches.INGEST_PROFILES["mac_queryable_first"]["concurrency"] == 1
    assert batches.INGEST_PROFILES["mac_queryable_first"]["pass_plan"][0] == "queryable"
    assert batches.INGEST_PROFILES["mac_safe"]["pass_plan"][0] == "queryable"
    assert "extracted" not in batches.INGEST_PROFILES["mac_safe"]["pass_plan"][:-1]


def test_infer_item_stage_from_legacy_phase_rows():
    assert (
        batches._infer_item_stage(
            {"status": batches.ITEM_DONE, "phase": "complete"}
        )
        == "fully_enriched"
    )
    assert (
        batches._infer_item_stage(
            {"status": batches.ITEM_DONE, "phase": "awaiting_summary"}
        )
        == "summary_pending"
    )
    assert (
        batches._infer_item_stage(
            {"status": batches.ITEM_RUNNING, "phase": "mongo"}
        )
        == "extracted"
    )
    assert (
        batches._infer_item_stage(
            {"status": batches.ITEM_STAGED, "phase": "staged", "stage": "queryable"}
        )
        == "queryable"
    )
    assert (
        batches._infer_item_stage(
            {
                "status": batches.ITEM_DONE,
                "phase": "complete",
                "stage": "queryable",
            }
        )
        == "fully_enriched"
    )
    assert (
        batches._infer_item_stage(
            {
                "status": batches.ITEM_DONE,
                "phase": "queryable_with_pending_summary_and_graph",
                "stage": "queryable",
            }
        )
        == "graph_pending"
    )


def test_storage_quota_counts_existing_bytes(tmp_path):
    storage = tmp_path / "spool"
    storage.mkdir()
    (storage / "old.bin").write_bytes(b"x" * 8)

    batches._ensure_storage_quota(
        storage_root=storage,
        incoming_bytes=2,
        max_total_bytes=10,
    )

    with pytest.raises(ValueError, match="quota exceeded"):
        batches._ensure_storage_quota(
            storage_root=storage,
            incoming_bytes=3,
            max_total_bytes=10,
        )


def test_file_item_doc_records_stored_copy(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    source = root / "book.md"
    source.write_text("# Book", encoding="utf-8")
    stored = tmp_path / "spool" / "item.md"

    item = batches._file_item_doc(
        batch_id="batch-1",
        corpus_id="corpus-1",
        user_id="user-1",
        root=root,
        path=source,
        ordinal=0,
        item_id="item-1",
        stored_path=stored,
    )

    assert item["item_id"] == "item-1"
    assert item["stored_path"] == str(stored)
    assert item["stored_bytes"] == source.stat().st_size
    assert item["phase"] == "queued"


class _ItemUpdatesCollection:
    def __init__(self):
        self.updates = []

    async def update_one(self, query, update):
        self.updates.append((query, update))
        return type("Result", (), {"modified_count": 1})()


class _ItemUpdatesDb:
    def __init__(self):
        self.items = _ItemUpdatesCollection()

    def __getitem__(self, name):
        assert name == batches.ITEMS
        return self.items


@pytest.mark.asyncio
async def test_local_batch_item_exception_is_terminal_failed(monkeypatch, tmp_path):
    source = tmp_path / "bad.md"
    source.write_text("# bad", encoding="utf-8")
    db = _ItemUpdatesDb()
    item = {
        "item_id": "item-1",
        "source_path": str(source),
        "filename": source.name,
    }
    batch = {
        "corpus_id": "corpus-1",
        "user_id": "user-1",
        "options": {},
    }

    class FakeIngestionService:
        async def _get_corpus_raw(self, _corpus_id):
            return {"default_ingestion_config": {}}

        async def ingest(self, **kwargs):
            await kwargs["on_doc_id"]("doc-1")
            await kwargs["on_phase"](
                "chunk_failed",
                {"doc_id": "doc-1", "error": "chunker timeout"},
            )
            raise RuntimeError("chunker timeout")

    async def fake_wait_for_slot():
        return None

    async def fake_release_slot():
        return None

    monkeypatch.setattr(batches, "_wait_for_ingest_slot", fake_wait_for_slot)
    monkeypatch.setattr(batches.admission, "release_ingest_slot", fake_release_slot)

    await batches._process_local_item(
        db=db,
        ingestion_service=FakeIngestionService(),
        batch=batch,
        item=item,
    )

    updates = [update["$set"] for _query, update in db.items.updates]
    assert any(update.get("doc_id") == "doc-1" for update in updates)
    final = updates[-1]
    assert final["status"] == batches.ITEM_FAILED
    assert final["phase"] == "failed"
    assert final["failure_stage"] == "worker_exception"
    assert "chunker timeout" in final["error"]


@pytest.mark.asyncio
async def test_local_batch_item_failed_result_does_not_requeue(monkeypatch, tmp_path):
    source = tmp_path / "incomplete.md"
    source.write_text("# incomplete", encoding="utf-8")
    db = _ItemUpdatesDb()
    item = {
        "item_id": "item-2",
        "source_path": str(source),
        "filename": source.name,
    }
    batch = {
        "corpus_id": "corpus-1",
        "user_id": "user-1",
        "options": {},
    }

    class FakeIngestionService:
        async def _get_corpus_raw(self, _corpus_id):
            return {"default_ingestion_config": {}}

        async def ingest(self, **kwargs):
            await kwargs["on_doc_id"]("doc-2")
            await kwargs["on_phase"]("embedding", {"doc_id": "doc-2"})
            return SimpleNamespace(
                status="failed",
                doc_id="doc-2",
                error="Ingest incomplete: qdrant",
            )

    async def fake_wait_for_slot():
        return None

    async def fake_release_slot():
        return None

    monkeypatch.setattr(batches, "_wait_for_ingest_slot", fake_wait_for_slot)
    monkeypatch.setattr(batches.admission, "release_ingest_slot", fake_release_slot)

    await batches._process_local_item(
        db=db,
        ingestion_service=FakeIngestionService(),
        batch=batch,
        item=item,
    )

    final = db.items.updates[-1][1]["$set"]
    assert final["status"] == batches.ITEM_FAILED
    assert final["phase"] == "failed"
    assert final["doc_id"] == "doc-2"
    assert final["failure_stage"] == "worker_result_failed"


@pytest.mark.asyncio
async def test_local_batch_item_queryable_pending_graph_is_not_failed(monkeypatch, tmp_path):
    source = tmp_path / "queryable.md"
    source.write_text("# queryable", encoding="utf-8")
    db = _ItemUpdatesDb()
    item = {
        "item_id": "item-graph-pending",
        "source_path": str(source),
        "filename": source.name,
    }
    batch = {
        "corpus_id": "corpus-1",
        "user_id": "user-1",
        "options": {},
    }

    class FakeIngestionService:
        async def _get_corpus_raw(self, _corpus_id):
            return {"default_ingestion_config": {}}

        async def ingest(self, **kwargs):
            await kwargs["on_doc_id"]("doc-queryable")
            await kwargs["on_phase"](
                "queryable_with_pending_graph",
                {"doc_id": "doc-queryable"},
            )
            return SimpleNamespace(
                status="queryable_with_pending_graph",
                doc_id="doc-queryable",
                error=None,
            )

    async def fake_wait_for_slot():
        return None

    async def fake_release_slot():
        return None

    monkeypatch.setattr(batches, "_wait_for_ingest_slot", fake_wait_for_slot)
    monkeypatch.setattr(batches.admission, "release_ingest_slot", fake_release_slot)

    await batches._process_local_item(
        db=db,
        ingestion_service=FakeIngestionService(),
        batch=batch,
        item=item,
    )

    final = db.items.updates[-1][1]["$set"]
    assert final["status"] == batches.ITEM_DONE
    assert final["phase"] == "queryable_with_pending_graph"
    assert final.get("failure_stage") is None


@pytest.mark.asyncio
async def test_local_batch_item_preserves_combined_pending_phase(monkeypatch, tmp_path):
    source = tmp_path / "queryable-both.md"
    source.write_text("# queryable", encoding="utf-8")
    db = _ItemUpdatesDb()
    item = {
        "item_id": "item-both-pending",
        "source_path": str(source),
        "filename": source.name,
    }
    batch = {
        "corpus_id": "corpus-1",
        "user_id": "user-1",
        "options": {},
    }

    class FakeIngestionService:
        async def _get_corpus_raw(self, _corpus_id):
            return {"default_ingestion_config": {}}

        async def ingest(self, **kwargs):
            await kwargs["on_doc_id"]("doc-queryable")
            return SimpleNamespace(
                status="queryable_with_pending_summary_and_graph",
                doc_id="doc-queryable",
                error=None,
            )

    async def fake_wait_for_slot():
        return None

    async def fake_release_slot():
        return None

    monkeypatch.setattr(batches, "_wait_for_ingest_slot", fake_wait_for_slot)
    monkeypatch.setattr(batches.admission, "release_ingest_slot", fake_release_slot)

    await batches._process_local_item(
        db=db,
        ingestion_service=FakeIngestionService(),
        batch=batch,
        item=item,
    )

    final = db.items.updates[-1][1]["$set"]
    assert final["status"] == batches.ITEM_DONE
    assert final["phase"] == "queryable_with_pending_summary_and_graph"
    assert final.get("failure_stage") is None


class _BatchCursor:
    def __init__(self, rows):
        self.rows = rows
        self._limit = None

    def sort(self, field, direction):
        reverse = direction < 0
        self.rows = sorted(self.rows, key=lambda row: row.get(field), reverse=reverse)
        return self

    def limit(self, limit):
        self._limit = limit
        return self

    async def to_list(self, length=None):
        limit = length or self._limit
        return list(self.rows[:limit] if limit else self.rows)


class _BatchCollection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query, projection=None):
        rows = [
            dict(row)
            for row in self.rows
            if row.get("corpus_id") == query.get("corpus_id")
            and row.get("user_id") == query.get("user_id")
        ]
        return _BatchCursor(rows)


@pytest.mark.asyncio
async def test_list_batches_returns_recent_user_batches():
    db = {
        batches.BATCHES: _BatchCollection(
            [
                {
                    "batch_id": "old",
                    "corpus_id": "corpus-1",
                    "user_id": "user-1",
                    "created_at": datetime(2024, 1, 1),
                },
                {
                    "batch_id": "new",
                    "corpus_id": "corpus-1",
                    "user_id": "user-1",
                    "created_at": datetime(2024, 1, 2),
                },
                {
                    "batch_id": "other-user",
                    "corpus_id": "corpus-1",
                    "user_id": "user-2",
                    "created_at": datetime(2024, 1, 3),
                },
            ]
        )
    }

    rows = await batches.list_batches(
        db,
        "corpus-1",
        user_id="user-1",
        limit=1,
    )

    assert [row["batch_id"] for row in rows] == ["new"]


def _matches_query(row, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches_query(row, clause) for clause in expected):
                return False
            continue
        if key == "$and":
            if not all(_matches_query(row, clause) for clause in expected):
                return False
            continue
        actual = row.get(key)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            elif "$exists" in expected:
                exists = key in row
                if bool(expected["$exists"]) != exists:
                    return False
            elif "$lt" in expected:
                if actual is None or not actual < expected["$lt"]:
                    return False
            elif "$in" not in expected:
                raise AssertionError(f"Unsupported query operator in {expected!r}")
        elif actual != expected:
            return False
    return True


class _RecoveryCursor:
    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self._limit = None

    def sort(self, field, direction):
        reverse = direction < 0
        self.rows = sorted(self.rows, key=lambda row: row.get(field, 0), reverse=reverse)
        return self

    def limit(self, limit):
        self._limit = limit
        return self

    async def to_list(self, length=None):
        limit = length or self._limit
        return list(self.rows[:limit] if limit else self.rows)


class _RecoveryCollection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query, projection=None):
        del projection
        return _RecoveryCursor([row for row in self.rows if _matches_query(row, query)])

    def aggregate(self, pipeline):
        batch_id = pipeline[0]["$match"]["batch_id"]
        rows = [row for row in self.rows if row.get("batch_id") == batch_id]
        pre_extraction_phases = {
            "queued",
            "reading",
            "starting_worker",
            "parse",
            "retrieval_setup",
            "chunking",
            "summaries",
            "summary",
            "summary_tree",
            "ghosts",
            "paused_cost_brake",
            "failed",
            "stale",
        }

        def extracted(row):
            return row.get("status") == batches.ITEM_DONE or (
                row.get("status") == batches.ITEM_RUNNING
                and (row.get("phase") or "queued") not in pre_extraction_phases
            )

        result = {
            "_id": None,
            "total_bytes": sum(int(row.get("size_bytes") or 0) for row in rows),
            "done_bytes": sum(
                int(row.get("size_bytes") or 0)
                for row in rows
                if row.get("status") == batches.ITEM_DONE
            ),
            "extracted_files": sum(1 for row in rows if extracted(row)),
            "extracted_bytes": sum(
                int(row.get("size_bytes") or 0) for row in rows if extracted(row)
            ),
        }
        return _RecoveryCursor([result])

    async def find_one(self, query, projection=None):
        del projection
        for row in self.rows:
            if _matches_query(row, query):
                return dict(row)
        return None

    async def update_many(self, query, update):
        modified = 0
        for row in self.rows:
            if _matches_query(row, query):
                row.update(update.get("$set", {}))
                modified += 1
        return type("Result", (), {"modified_count": modified})()

    async def update_one(self, query, update):
        for row in self.rows:
            if _matches_query(row, query):
                row.update(update.get("$set", {}))
                for key, value in (update.get("$inc") or {}).items():
                    row[key] = row.get(key, 0) + value
                return type("Result", (), {"modified_count": 1})()
        return type("Result", (), {"modified_count": 0})()

    async def insert_one(self, doc):
        self.rows.append(dict(doc))
        return type("Result", (), {"inserted_id": doc.get("batch_id")})()

    async def insert_many(self, docs, ordered=False):
        del ordered
        self.rows.extend(dict(doc) for doc in docs)
        return type("Result", (), {"inserted_ids": [doc.get("item_id") for doc in docs]})()

    async def count_documents(self, query):
        return sum(1 for row in self.rows if _matches_query(row, query))


class _RecoveryDb:
    def __init__(self, batch_rows, item_rows):
        self.collections = {
            batches.BATCHES: _RecoveryCollection(batch_rows),
            batches.ITEMS: _RecoveryCollection(item_rows),
        }

    def __getitem__(self, name):
        return self.collections[name]


@pytest.mark.asyncio
async def test_recover_local_batch_runners_reclaims_orphaned_running_items(monkeypatch):
    batch_rows = [
        {
            "batch_id": "batch-1",
            "source": "local_folder",
            "user_id": "user-1",
            "status": batches.BATCH_RUNNING,
            "started_at": datetime(2024, 1, 1),
        }
    ]
    item_rows = [
            {
                "item_id": "running",
                "batch_id": "batch-1",
                "source": "local_folder",
                "user_id": "user-1",
                "status": batches.ITEM_RUNNING,
                "lease_until": None,
            },
        {
            "item_id": "queued",
            "batch_id": "batch-1",
            "source": "local_folder",
            "user_id": "user-1",
            "status": batches.ITEM_QUEUED,
        },
        {
            "item_id": "true-failed",
            "batch_id": "batch-1",
            "source": "local_folder",
            "user_id": "user-1",
            "status": batches.ITEM_FAILED,
        },
    ]
    db = _RecoveryDb(batch_rows, item_rows)
    started = []

    def fake_start_local_batch_runner(**kwargs):
        started.append((kwargs["batch_id"], kwargs["user_id"]))
        return True

    monkeypatch.setattr(
        batches,
        "start_local_batch_runner",
        fake_start_local_batch_runner,
    )

    result = await batches.recover_local_batch_runners(
        db=db,
        ingestion_service=object(),
    )

    assert result["reclaimed_items"] == 1
    assert result["candidate_batches"] == 1
    assert result["started_batches"] == 1
    assert started == [("batch-1", "user-1")]
    by_item = {row["item_id"]: row for row in item_rows}
    assert by_item["running"]["status"] == batches.ITEM_FAILED_RECOVERABLE
    assert by_item["running"]["failure_stage"] == "backend_restarted"
    assert by_item["true-failed"]["status"] == batches.ITEM_FAILED


@pytest.mark.asyncio
async def test_recover_local_batch_runners_does_not_start_manifest_only_batch(monkeypatch):
    batch_rows = [
        {
            "batch_id": "batch-1",
            "source": "local_folder",
            "user_id": "user-1",
            "status": batches.BATCH_QUEUED,
        }
    ]
    item_rows = [
        {
            "item_id": "queued",
            "batch_id": "batch-1",
            "source": "local_folder",
            "user_id": "user-1",
            "status": batches.ITEM_QUEUED,
        }
    ]
    db = _RecoveryDb(batch_rows, item_rows)

    def fake_start_local_batch_runner(**_kwargs):
        raise AssertionError("manifest-only batch should not auto-start")

    monkeypatch.setattr(
        batches,
        "start_local_batch_runner",
        fake_start_local_batch_runner,
    )

    result = await batches.recover_local_batch_runners(
        db=db,
        ingestion_service=object(),
    )

    assert result["candidate_batches"] == 1
    assert result["started_batches"] == 0


@pytest.mark.asyncio
async def test_create_upload_batch_stores_browser_files_as_runnable_batch(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        INGEST_FILE_STORAGE_DIR=str(tmp_path / "spool"),
        INGEST_FILE_STORAGE_MAX_BYTES=1024,
        INGEST_BATCH_WORKERS=2,
        INGEST_MAX_ACTIVE_JOBS=16,
    )
    monkeypatch.setattr(batches, "get_settings", lambda: settings)
    db = _RecoveryDb([], [])

    result = await batches.create_upload_batch(
        db=db,
        corpus_id="corpus-1",
        user_id="user-1",
        files=[
            {
                "filename": "quick.md",
                "content_type": "text/markdown",
                "data": b"# Quick",
            }
        ],
        concurrency=4,
        profile="rtx_assisted",
    )

    assert result["source"] == batches.SOURCE_BROWSER_UPLOAD
    assert result["counts"][batches.ITEM_QUEUED] == 1
    assert result["stored_bytes"] == len(b"# Quick")
    assert result["options"]["concurrency"] == 4
    assert result["options"]["profile"] == "rtx_assisted"
    item = db.collections[batches.ITEMS].rows[0]
    assert item["source"] == batches.SOURCE_BROWSER_UPLOAD
    assert item["relative_path"] == "quick.md"
    assert item["source_path"] == "quick.md"
    assert item["stored_path"]
    assert Path(item["stored_path"]).read_bytes() == b"# Quick"


@pytest.mark.asyncio
async def test_append_new_files_to_batch_adds_only_unseen_manifest_items(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    existing = root / "existing.md"
    existing.write_text("# existing", encoding="utf-8")
    added = root / "added.md"
    added.write_text("# added", encoding="utf-8")
    existing_stat = existing.stat()
    existing_mtime = datetime.utcfromtimestamp(existing_stat.st_mtime)
    batch_rows = [
        {
            "batch_id": "batch-1",
            "corpus_id": "corpus-1",
            "source": "local_folder",
            "root_path": str(root),
            "recursive": True,
            "extensions": [".md"],
            "store_files": False,
            "user_id": "user-1",
            "status": batches.BATCH_RUNNING,
            "total_source_bytes": existing_stat.st_size,
            "stored_bytes": 0,
        }
    ]
    item_rows = [
        {
            "item_id": "existing-item",
            "batch_id": "batch-1",
            "corpus_id": "corpus-1",
            "source": "local_folder",
            "user_id": "user-1",
            "relative_path": "existing.md",
            "filename": "existing.md",
            "size_bytes": existing_stat.st_size,
            "mtime": existing_mtime,
            "ordinal": 0,
            "status": batches.ITEM_DONE,
        }
    ]
    db = _RecoveryDb(batch_rows, item_rows)

    result = await batches.append_new_files_to_batch(
        db=db,
        batch_id="batch-1",
        user_id="user-1",
    )

    assert result["appended_items"] == 1
    assert result["discovered_files"] == 2
    assert result["counts"][batches.ITEM_DONE] == 1
    assert result["counts"][batches.ITEM_QUEUED] == 1
    by_name = {row["filename"]: row for row in item_rows}
    assert by_name["existing.md"]["status"] == batches.ITEM_DONE
    assert by_name["added.md"]["status"] == batches.ITEM_QUEUED
    assert by_name["added.md"]["ordinal"] == 1


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *_args, **_kwargs):
        return self

    def __aiter__(self):
        self._iter = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def to_list(self, length=None):
        return list(self.rows)


class _FakeCollection:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []
        self.bulk_ops = []

    async def find_one(self, query, projection=None):
        for row in self.rows:
            if all(row.get(k) == v for k, v in query.items()):
                return dict(row)
        return None

    def find(self, query, projection=None):
        rows = []
        for row in self.rows:
            if row.get("doc_id") != query.get("doc_id"):
                continue
            if row.get("corpus_id") != query.get("corpus_id"):
                continue
            chunk_filter = query.get("chunk_id")
            if isinstance(chunk_filter, dict) and "$in" in chunk_filter:
                if row.get("chunk_id") not in chunk_filter["$in"]:
                    continue
            elif chunk_filter is not None and row.get("chunk_id") != chunk_filter:
                continue
            status_filter = query.get("status")
            if status_filter is not None and row.get("status") != status_filter:
                continue
            rows.append(dict(row))
        return _FakeCursor(rows)

    async def update_one(self, query, update):
        self.updates.append((query, update))
        return type("Result", (), {"modified_count": 1})()

    async def bulk_write(self, ops, ordered=False):
        del ordered
        self.bulk_ops.extend(list(ops))
        return type("Result", (), {"bulk_api_result": {}})()

    async def count_documents(self, query):
        count = 0
        for row in self.rows:
            if all(row.get(k) == v for k, v in query.items()):
                count += 1
        return count


class _FakeDb:
    def __init__(self, doc, chunks):
        self.collections = {
            "documents": _FakeCollection([doc]),
            "chunks": _FakeCollection(chunks),
            "corpora": _FakeCollection([{"corpus_id": doc["corpus_id"]}]),
            "parent_chunks": _FakeCollection(doc.get("parent_chunks") or []),
            "ghost_b_extractions": _FakeCollection([]),
        }

    def __getitem__(self, name):
        return self.collections[name]


@pytest.mark.asyncio
async def test_graph_backfill_replays_from_chunks_when_staging_missing(monkeypatch):
    doc = {
        "doc_id": "doc-1",
        "corpus_id": "corpus-1",
        "user_id": "user-1",
        "filename": "book.pdf",
        "ingestion_config": {"use_neo4j": True, "target_qdrant_collections": ["graph"]},
        "write_state": {
            "mongo_written": True,
            "qdrant_written": True,
            "neo4j_written": False,
            "verified": None,
        },
        "ghost_b_failures": [],
        "ghost_b_staging": [],
        "parent_chunks": [{"parent_id": "p1"}],
        "updated_at": datetime.utcnow(),
    }
    chunks = [
        {
            "doc_id": "doc-1",
            "corpus_id": "corpus-1",
            "chunk_id": "chunk-body",
            "text": "substantive body text",
            "chunk_kind": "body",
        },
        {
            "doc_id": "doc-1",
            "corpus_id": "corpus-1",
            "chunk_id": "chunk-code",
            "text": "print('skip ghost b')",
            "chunk_kind": "code",
        },
    ]
    db = _FakeDb(doc, chunks)
    seen = {}

    async def fake_load_config(**kwargs):
        return IngestionConfig()

    async def fake_run_ghost_b_backfill(**kwargs):
        tasks = kwargs["tasks"]
        seen["task_ids"] = [task.chunk_id for task in tasks]
        return ExtractionBatchReport(
            results=[
                ExtractionResult(
                    schema_version="test",
                    chunk_id=tasks[0].chunk_id,
                    doc_id="doc-1",
                    corpus_id="corpus-1",
                    text=tasks[0].text,
                )
            ],
            failures=[],
            metrics={},
        )

    async def fake_write_graph_results(**kwargs):
        seen["written_chunk_ids"] = kwargs["all_chunk_ids"]
        seen["written_results"] = kwargs["extraction_results"]

    monkeypatch.setattr(graph_backfill, "_load_backfill_config", fake_load_config)
    monkeypatch.setattr(
        graph_backfill,
        "_run_ghost_b_backfill",
        fake_run_ghost_b_backfill,
    )
    monkeypatch.setattr(graph_backfill, "_write_graph_results", fake_write_graph_results)

    result = await graph_backfill.backfill_failed_graph_chunks(
        db=db,
        qdrant_client=object(),
        neo4j_driver=object(),
        corpus_id="corpus-1",
        doc_id="doc-1",
        user_id="user-1",
    )

    assert result["status"] == "replayed_from_chunks"
    assert result["full_replay"] is True
    assert seen["task_ids"] == ["chunk-body"]
    assert seen["written_chunk_ids"] == ["chunk-body", "chunk-code"]
    update = db["documents"].updates[-1][1]["$set"]
    assert update["write_state.neo4j_written"] is True


@pytest.mark.asyncio
async def test_graph_backfill_flushes_from_extraction_collection(monkeypatch):
    doc = {
        "doc_id": "doc-2",
        "corpus_id": "corpus-1",
        "user_id": "user-1",
        "filename": "book.pdf",
        "ingestion_config": {"use_neo4j": True, "target_qdrant_collections": ["graph"]},
        "write_state": {
            "mongo_written": True,
            "qdrant_written": True,
            "neo4j_written": False,
            "verified": None,
        },
        "ghost_b_failures": [],
        "ghost_b_staging_count": 1,
        "parent_count": 1,
        "updated_at": datetime.utcnow(),
    }
    chunks = [
        {
            "doc_id": "doc-2",
            "corpus_id": "corpus-1",
            "chunk_id": "chunk-body",
            "text": "substantive body text",
            "chunk_kind": "body",
        }
    ]
    db = _FakeDb(doc, chunks)
    db.collections["ghost_b_extractions"] = _FakeCollection(
        [
            {
                "doc_id": "doc-2",
                "corpus_id": "corpus-1",
                "chunk_id": "chunk-body",
                "schema_version": "test",
                "entities": [],
                "relations": [],
                "facts": [],
                "status": "ok",
            }
        ]
    )
    seen = {}

    async def fake_write_graph_results(**kwargs):
        seen["written_results"] = kwargs["extraction_results"]
        seen["parent_count"] = kwargs["parent_count"]

    monkeypatch.setattr(graph_backfill, "_write_graph_results", fake_write_graph_results)

    result = await graph_backfill.backfill_failed_graph_chunks(
        db=db,
        qdrant_client=object(),
        neo4j_driver=object(),
        corpus_id="corpus-1",
        doc_id="doc-2",
        user_id="user-1",
    )

    assert result["status"] == "flushed_to_neo4j"
    assert len(seen["written_results"]) == 1
    assert seen["parent_count"] == 1
