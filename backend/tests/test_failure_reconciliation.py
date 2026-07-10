import pytest

from services.ingestion.failure_reconciliation import (
    STALE_CHUNK_HASH_MISMATCH,
    STALE_CHUNK_REFERENCE,
    STALE_EXTRACTION_CONTRACT_MISMATCH,
    build_document_failure_reconciliation,
    classify_failure_row_staleness,
    reconcile_ghost_b_failure_metadata,
    repair_action_for_stale_reason,
)


def test_split_error_rows_are_authoritative_over_inline_sample():
    state = build_document_failure_reconciliation(
        doc={
            "doc_id": "doc-1",
            "ghost_b_failure_count": 9,
            "ghost_b_failures": [{"chunk_id": "old-inline"}],
        },
        split_error_rows=[
            {"chunk_id": "chunk-a", "error": "provider_failed"},
            {"chunk_id": "chunk-b", "error": "validation_failed"},
        ],
        live_chunk_ids={"chunk-a", "chunk-b", "old-inline"},
    )

    assert state["remaining_count"] == 2
    assert [row["chunk_id"] for row in state["remaining_failures"]] == ["chunk-a", "chunk-b"]
    assert state["counter_drift"] is True
    assert state["sample_drift"] is True
    assert state["needs_update"] is True


def test_inline_legacy_failures_are_retained_when_chunks_exist():
    state = build_document_failure_reconciliation(
        doc={
            "doc_id": "doc-1",
            "ghost_b_failure_count": 1,
            "ghost_b_failures": [{"chunk_id": "chunk-a", "error": "timeout"}],
        },
        split_error_rows=[],
        live_chunk_ids={"chunk-a"},
    )

    assert state["remaining_count"] == 1
    assert state["stale_total"] == 0
    assert state["orphaned_count"] == 0
    assert state["counter_drift"] is False
    assert state["sample_drift"] is False
    assert state["needs_update"] is False


def test_inline_legacy_failures_are_classified_stale_when_chunks_are_gone():
    state = build_document_failure_reconciliation(
        doc={
            "doc_id": "doc-1",
            "ghost_b_failure_count": 1,
            "ghost_b_failures": [{"chunk_id": "missing-chunk", "error": "timeout"}],
        },
        split_error_rows=[],
        live_chunk_ids={"chunk-a"},
    )

    assert state["remaining_count"] == 0
    assert state["stale_inline_count"] == 1
    assert state["stale_total"] == 1
    assert state["counter_drift"] is True
    assert state["needs_update"] is True


def test_inline_failure_mirror_is_cleared_when_split_row_is_stale():
    state = build_document_failure_reconciliation(
        doc={
            "doc_id": "doc-1",
            "ghost_b_failure_count": 1,
            "ghost_b_failures": [{"chunk_id": "chunk-a", "error": "old contract"}],
        },
        split_error_rows=[],
        live_chunk_ids={"chunk-a"},
        stale_chunk_ids={"chunk-a"},
        stale_split_count=1,
    )

    assert state["remaining_count"] == 0
    assert state["stale_inline_count"] == 1
    assert state["counter_drift"] is True
    assert state["needs_update"] is True


def test_orphaned_failure_count_is_cleared_when_no_failure_rows_exist():
    state = build_document_failure_reconciliation(
        doc={
            "doc_id": "doc-1",
            "ghost_b_failure_count": 7,
            "ghost_b_failures": [],
        },
        split_error_rows=[],
        live_chunk_ids={"chunk-a"},
    )

    assert state["remaining_count"] == 0
    assert state["orphaned_count"] == 7
    assert state["counter_drift"] is True
    assert state["needs_update"] is True


def test_failure_row_is_stale_when_live_chunk_is_missing():
    reason = classify_failure_row_staleness(
        {"chunk_id": "missing", "chunk_hash": "old-hash"},
        chunk=None,
        doc={"doc_id": "doc-1"},
        current_contract_hash="contract-a",
    )

    assert reason == STALE_CHUNK_REFERENCE
    assert repair_action_for_stale_reason(reason) == "clear_or_rechunk_doc"


def test_failure_row_is_stale_when_chunk_hash_drifted():
    reason = classify_failure_row_staleness(
        {
            "chunk_id": "chunk-a",
            "stage_identity": {
                "chunk_hash": "old-hash",
                "extraction_contract_hash": "contract-a",
            },
        },
        chunk={"chunk_id": "chunk-a", "chunk_hash": "new-hash", "text": "new text"},
        doc={"doc_id": "doc-1"},
        current_contract_hash="contract-a",
    )

    assert reason == STALE_CHUNK_HASH_MISMATCH
    assert repair_action_for_stale_reason(reason) == "clear_or_reextract_chunk"


def test_failure_row_is_stale_when_extraction_contract_drifted():
    reason = classify_failure_row_staleness(
        {
            "chunk_id": "chunk-a",
            "chunk_hash": "same-hash",
            "extraction_contract_hash": "contract-a",
        },
        chunk={"chunk_id": "chunk-a", "chunk_hash": "same-hash", "text": "same text"},
        doc={"doc_id": "doc-1"},
        current_contract_hash="contract-b",
    )

    assert reason == STALE_EXTRACTION_CONTRACT_MISMATCH
    assert repair_action_for_stale_reason(reason) == "requeue_with_current_contract"


def test_legacy_failure_row_without_hash_is_retained_when_chunk_exists():
    reason = classify_failure_row_staleness(
        {"chunk_id": "chunk-a", "error_type": "timeout"},
        chunk={"chunk_id": "chunk-a", "chunk_hash": "current-hash", "text": "same text"},
        doc={"doc_id": "doc-1"},
        current_contract_hash="contract-a",
    )

    assert reason is None


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self._limit = len(rows)

    def limit(self, value):
        self._limit = value
        return self

    def sort(self, *_args, **_kwargs):
        return self

    async def to_list(self, length=None):
        limit = self._limit if length is None else min(self._limit, length)
        return [dict(row) for row in self.rows[:limit]]


def _value_at(row, dotted):
    current = row
    for part in str(dotted).split("."):
        current = current.get(part) if isinstance(current, dict) else None
    return current


def _matches(row, query):
    for key, expected in (query or {}).items():
        actual = _value_at(row, key)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$gt" in expected and not (actual is not None and actual > expected["$gt"]):
                return False
            continue
        if actual != expected:
            return False
    return True


def _set_path(row, dotted, value):
    target = row
    parts = str(dotted).split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def _unset_path(row, dotted):
    target = row
    parts = str(dotted).split(".")
    for part in parts[:-1]:
        target = target.get(part) if isinstance(target, dict) else None
        if target is None:
            return
    if isinstance(target, dict):
        target.pop(parts[-1], None)


class _Collection:
    def __init__(self, rows=None):
        self.rows = [dict(row) for row in (rows or [])]

    def find(self, query=None, projection=None):
        rows = [row for row in self.rows if _matches(row, query or {})]
        if projection:
            projected = []
            for row in rows:
                out = {}
                for key, include in projection.items():
                    if not include or key == "_id":
                        continue
                    value = _value_at(row, key)
                    if value is not None:
                        _set_path(out, key, value)
                projected.append(out)
            rows = projected
        return _Cursor(rows)

    async def count_documents(self, query):
        return sum(1 for row in self.rows if _matches(row, query or {}))

    async def bulk_write(self, ops, ordered=False):
        del ordered
        modified = 0
        for op in ops:
            op_name = op.__class__.__name__
            matched_indexes = [
                idx for idx, row in enumerate(self.rows) if _matches(row, op._filter)
            ]
            if not matched_indexes and getattr(op, "_upsert", False):
                row = dict(op._filter)
                self._apply_update(row, op._doc)
                self.rows.append(row)
                continue
            targets = matched_indexes[:1] if op_name == "UpdateOne" else matched_indexes
            for idx in targets:
                row = dict(self.rows[idx])
                self._apply_update(row, op._doc)
                self.rows[idx] = row
                modified += 1
        return type("Result", (), {"modified_count": modified})()

    async def insert_one(self, row):
        self.rows.append(dict(row))
        return type("Result", (), {"inserted_id": row.get("_id")})()

    def _apply_update(self, row, update):
        for key, value in (update.get("$set") or {}).items():
            _set_path(row, key, value)
        for key in (update.get("$unset") or {}):
            _unset_path(row, key)


class _Db(dict):
    def __init__(self, **collections):
        super().__init__({name: _Collection(rows) for name, rows in collections.items()})

    def __getitem__(self, name):
        return self.setdefault(name, _Collection())


@pytest.mark.asyncio
async def test_reconcile_marks_missing_chunk_reference_extraction_jobs_skipped():
    db = _Db(
        documents=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "filename": "book.md",
                "ghost_b_failure_count": 1,
                "ghost_b_failures": [{"chunk_id": "missing-chunk", "error": "timeout"}],
                "ingestion_config": {
                    "extraction_engine": "cloud",
                    "use_neo4j": True,
                    "extraction_models": [{"provider_preset": "vllm-rtx", "model": "m"}],
                },
            }
        ],
        chunks=[],
        ghost_b_extractions=[
            {
                "_id": "row-1",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "missing-chunk",
                "status": "error",
                "chunk_hash": "old-hash",
                "error_type": "timeout",
            }
        ],
        extraction_jobs=[
            {
                "job_id": "job-1",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "missing-chunk",
                "status": "provider_failed",
                "reason": "provider_error",
            }
        ],
        ingest_repair_runs=[],
    )

    result = await reconcile_ghost_b_failure_metadata(
        db,
        corpus_id="corpus-1",
        apply=True,
        limit=10,
    )

    ghost_row = db["ghost_b_extractions"].rows[0]
    doc = db["documents"].rows[0]
    job = db["extraction_jobs"].rows[0]
    assert result["stale_split_rows"] == 1
    assert result["documents_cleared"] == 1
    assert result["stale_extraction_jobs_skipped"] == 1
    assert ghost_row["status"] == "stale_chunk_reference"
    assert ghost_row["repair_action"] == "clear_or_rechunk_doc"
    assert doc["ghost_b_failure_count"] == 0
    assert doc["ghost_b_failures"] == []
    assert job["status"] == "skipped"
    assert job["reason"] == "stale_chunk_reference"
    assert job["source_status"] == "stale_chunk_reference"


@pytest.mark.asyncio
async def test_reconcile_clears_inline_mirror_of_previously_stale_split_row():
    db = _Db(
        documents=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "ghost_b_failure_count": 1,
                "ghost_b_failures": [{"chunk_id": "chunk-a", "error": "old contract"}],
                "ingestion_config": {},
            }
        ],
        chunks=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-a",
                "text": "live text",
            }
        ],
        ghost_b_extractions=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-a",
                "status": "stale_chunk_reference",
            }
        ],
        extraction_jobs=[],
        ingest_repair_runs=[],
    )

    result = await reconcile_ghost_b_failure_metadata(
        db,
        corpus_id="corpus-1",
        apply=True,
        limit=10,
    )

    doc = db["documents"].rows[0]
    assert result["documents_cleared"] == 1
    assert doc["ghost_b_failure_count"] == 0
    assert doc["ghost_b_failures"] == []
