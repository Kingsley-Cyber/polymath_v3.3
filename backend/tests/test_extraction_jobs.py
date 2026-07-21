import asyncio
from datetime import datetime, timedelta

from services.ingestion.extraction_jobs import (
    _mark_jobs,
    _persist_extraction_rows,
    _persist_skipped_extraction_rows,
    build_extraction_job,
    build_extraction_job_run_update,
    classify_extraction_status,
    extraction_contract_hash,
    extraction_provider_contract,
    extraction_job_id,
    plan_extraction_jobs,
    reconcile_terminal_extraction_jobs,
    run_extraction_jobs,
    with_live_extraction_config,
)


def test_live_corpus_provider_pool_overrides_frozen_document_pool():
    frozen = {
        "doc_id": "doc-1",
        "ingestion_config": {
            "models_linked": False,
            "extraction_models": [{"model": "old-model"}],
        },
    }
    live = {
        "models_linked": False,
        "extraction_models": [{"model": "new-model"}],
    }

    effective = with_live_extraction_config(frozen, live)

    assert effective["ingestion_config"]["extraction_models"] == [
        {"model": "new-model"}
    ]
    assert frozen["ingestion_config"]["extraction_models"] == [
        {"model": "old-model"}
    ]


class _FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def limit(self, limit):
        self.rows = self.rows[:limit]
        return self

    def sort(self, *_args, **_kwargs):
        if _args:
            key = _args[0]
            reverse = len(_args) > 1 and int(_args[1]) < 0
            self.rows.sort(key=lambda row: str(row.get(key) or ""), reverse=reverse)
        return self

    async def to_list(self, length=None):
        if length is None:
            return list(self.rows)
        return list(self.rows[:length])


class _FakeCollection:
    def __init__(self, rows):
        self.rows = list(rows)

    def find(self, query=None, projection=None):
        query = query or {}
        projection = projection or {}
        rows = []
        for row in self.rows:
            if _matches_query(row, query):
                if projection:
                    include_keys = [key for key, include in projection.items() if include]
                    if include_keys:
                        rows.append({key: row.get(key) for key in include_keys if key in row})
                    else:
                        rows.append({
                            key: value
                            for key, value in row.items()
                            if projection.get(key, 1) != 0
                        })
                else:
                    rows.append(dict(row))
        return _FakeCursor(rows)

    async def find_one(self, query=None, projection=None):
        rows = self.find(query or {}, projection).rows
        return dict(rows[0]) if rows else None

    async def count_documents(self, query):
        return sum(1 for row in self.rows if _matches_query(row, query or {}))

    async def bulk_write(self, ops, ordered=False):
        del ordered
        modified_count = 0
        for op in ops:
            op_name = op.__class__.__name__
            matched = [
                idx
                for idx, row in enumerate(self.rows)
                if _matches_query(row, op._filter)
            ]
            if op_name == "ReplaceOne":
                if matched:
                    self.rows[matched[0]] = dict(op._doc)
                    modified_count += 1
                elif getattr(op, "_upsert", False):
                    self.rows.append(dict(op._doc))
                continue

            update = getattr(op, "_doc", {}) or {}
            if not matched and getattr(op, "_upsert", False):
                row = dict(op._filter)
                _apply_update(row, update)
                self.rows.append(row)
                continue
            target_indexes = matched[:1] if op_name == "UpdateOne" else matched
            for idx in target_indexes:
                row = dict(self.rows[idx])
                _apply_update(row, update)
                self.rows[idx] = row
                modified_count += 1
        return type("Result", (), {"bulk_api_result": {}, "modified_count": modified_count})()


class _FakeDB:
    def __init__(self, **collections):
        self.collections = {
            name: _FakeCollection(rows)
            for name, rows in collections.items()
        }

    def __getitem__(self, name):
        return self.collections.setdefault(name, _FakeCollection([]))


def _matches_query(row, query):
    for key, expected in query.items():
        actual = row.get(key)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
            if "$gt" in expected and not (actual is not None and actual > expected["$gt"]):
                return False
        elif actual != expected:
            return False
    return True


def _apply_update(row, update):
    if "$set" not in update and "$unset" not in update and "$setOnInsert" not in update:
        row.clear()
        row.update(dict(update))
        return
    for key, value in (update.get("$set") or {}).items():
        row[key] = value
    for key, value in (update.get("$setOnInsert") or {}).items():
        row.setdefault(key, value)
    for key in (update.get("$unset") or {}):
        row.pop(key, None)


def test_extraction_status_classifies_missing_as_queued():
    assert classify_extraction_status(None) == ("queued", "missing_extraction")


def test_extraction_status_classifies_ok_as_succeeded():
    assert classify_extraction_status({"status": "ok"}) == ("succeeded", "ghost_b_ok")


def test_extraction_status_classifies_promoted_ok_rows_as_promoted():
    assert classify_extraction_status({"status": "ok", "promoted_at": "now"}) == (
        "promoted",
        "graph_promoted",
    )


def test_extraction_status_classifies_skipped_artifact_as_terminal():
    assert classify_extraction_status(
        {
            "status": "skipped",
            "skip_reason": "no_extractable_text_or_skipped_kind",
        }
    ) == ("skipped", "no_extractable_text_or_skipped_kind")


def test_extraction_status_requeues_stale_contract_rows():
    assert classify_extraction_status(
        {
            "status": "stale_chunk_reference",
            "stale_reason": "stale_extraction_contract_mismatch",
            "repair_action": "requeue_with_current_contract",
        }
    ) == ("queued", "contract_changed")


def test_extraction_status_requeues_stale_chunk_hash_rows():
    assert classify_extraction_status(
        {
            "status": "stale_chunk_reference",
            "stale_reason": "stale_chunk_hash_mismatch",
            "repair_action": "clear_or_reextract_chunk",
        }
    ) == ("queued", "chunk_changed")


def test_extraction_status_skips_unresolvable_stale_chunk_reference():
    assert classify_extraction_status(
        {
            "status": "stale_chunk_reference",
            "stale_reason": "stale_chunk_reference",
            "repair_action": "clear_or_rechunk_doc",
        }
    ) == ("skipped", "stale_chunk_reference")


def test_extraction_status_classifies_schema_errors_as_validation_failed():
    assert classify_extraction_status(
        {
            "status": "error",
            "error_type": "ValidationError",
            "error_message": "invalid JSON schema field",
        }
    ) == ("validation_failed", "validation_error")


def test_extraction_status_classifies_structured_output_rejection_as_provider_contract():
    assert classify_extraction_status(
        {
            "status": "error",
            "error_type": "json_schema_unsupported",
            "error_message": "provider rejected response_format json_schema",
        }
    ) == ("blocked_provider_contract", "provider_contract_unsupported")


def test_extraction_status_classifies_provider_errors():
    assert classify_extraction_status(
        {
            "status": "error",
            "error_type": "HTTPStatusError",
            "error_message": "429 rate limit",
        }
    ) == ("provider_failed", "provider_error")


def test_extraction_job_id_changes_with_contract_hash():
    first = extraction_job_id(
        corpus_id="c",
        doc_id="d",
        chunk_id="chunk",
        chunk_hash="h",
        contract_hash="contract-a",
    )
    second = extraction_job_id(
        corpus_id="c",
        doc_id="d",
        chunk_id="chunk",
        chunk_hash="h",
        contract_hash="contract-b",
    )

    assert first != second
    assert first == extraction_job_id(
        corpus_id="c",
        doc_id="d",
        chunk_id="chunk",
        chunk_hash="h",
        contract_hash="contract-a",
    )


def test_build_extraction_job_carries_chunk_and_contract_identity():
    job = build_extraction_job(
        chunk={
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "chunk_id": "chunk-1",
            "parent_id": "parent-1",
            "text": "Evidence text.",
        },
        doc={
            "doc_id": "doc-1",
            "user_id": "user-1",
            "filename": "book.md",
            "source_identity": {"content_sha256": "source-hash", "source_key": "sha256:source-hash"},
            "ingestion_config": {
                "extraction_engine": "cloud",
                "extraction_models": [{"provider_preset": "vllm-rtx", "model": "polymath-extract"}],
                "use_neo4j": True,
            },
        },
        extraction_row={
            "status": "error",
            "error_type": "parse_error",
            "error_message": "incomplete JSON",
            "attempts": 2,
            "model": "polymath-extract",
            "provider": "local_private_vllm",
            "lane": 0,
            "schema_mode": "json_schema",
            "output_mode": "json_schema",
            "raw_output_artifact_id": "sha256:raw",
            "raw_output_fingerprint": {"sha256": "raw"},
            "prompt_hash": "prompt-hash",
            "prompt_chars": 123,
        },
    )

    assert job["status"] == "validation_failed"
    assert job["reason"] == "validation_error"
    assert job["attempt_count"] == 2
    assert job["chunk_hash"]
    assert job["extraction_contract_hash"]
    assert job["stage_identity"]["identity_version"] == "stage_identity.v1"
    assert job["stage_identity"]["source_file_hash"] == "source-hash"
    assert job["stage_identity"]["source_key"] == "sha256:source-hash"
    assert job["stage_identity"]["chunk_hash"] == job["chunk_hash"]
    assert job["stage_identity"]["extraction_contract_hash"] == job["extraction_contract_hash"]
    assert job["provider_route"]["model"] == "polymath-extract"
    assert job["provider_route"]["provider"] == "local_private_vllm"
    assert job["provider_route"]["lane"] == 0
    assert job["provider_route"]["schema_mode"] == "json_schema"
    assert job["provider_route"]["output_mode"] == "json_schema"
    assert job["provider_route"]["pool_source"] == "extraction_models"
    assert job["provider_route"]["routing_policy"] == "work_stealing"
    assert job["provider_route"]["pool_size"] == 1
    assert job["provider_route"]["candidate_lanes"] == [
        {
            "lane": 0,
            "provider": "local_private_vllm",
            "provider_preset": "vllm-rtx",
            "model": "polymath-extract",
            "schema_mode": "json_schema",
            "output_mode": "json_schema",
            "json_repair_mode": "provider_native",
            "max_concurrent": None,
            "concurrency_policy": "adaptive_vram_85",
            "local_private": True,
        }
    ]
    assert job["provider_contract"]["pool_source"] == "extraction_models"
    assert job["provider_contract"]["routing_policy"] == "work_stealing"
    assert job["provider_contract"]["lanes"][0]["provider_card"]["provider"] == "local_private_vllm"
    assert job["raw_output_artifact_id"] == "sha256:raw"
    assert job["raw_output_fingerprint"] == {"sha256": "raw"}
    assert job["prompt_hash"] == "prompt-hash"
    assert job["prompt_chars"] == 123


def test_build_extraction_job_skips_when_extraction_engine_off():
    job = build_extraction_job(
        chunk={
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "chunk_id": "chunk-1",
            "text": "Vectors-only content.",
        },
        doc={
            "doc_id": "doc-1",
            "ingestion_config": {
                "extraction_engine": "off",
                "use_neo4j": True,
            },
        },
    )

    assert job["status"] == "skipped"
    assert job["reason"] == "extraction_engine_off"


def test_build_extraction_job_skips_when_graph_extraction_disabled():
    job = build_extraction_job(
        chunk={
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "chunk_id": "chunk-1",
            "text": "Vector-only corpus content.",
        },
        doc={
            "doc_id": "doc-1",
            "ingestion_config": {
                "extraction_engine": "cloud",
                "use_neo4j": False,
                "extraction_models": [
                    {"provider_preset": "vllm-rtx", "model": "polymath-extract"}
                ],
            },
        },
        extraction_row={
            "status": "error",
            "error_type": "HTTPStatusError",
            "error_message": "429 rate limit from old run",
        },
    )

    assert job["status"] == "skipped"
    assert job["reason"] == "graph_extraction_disabled"


def test_extraction_contract_hash_tracks_linked_summary_pool():
    base_doc = {
        "ingestion_config": {
            "extraction_engine": "cloud",
            "models_linked": True,
            "summary_models": [
                {
                    "provider_preset": "siliconflow",
                    "model": "tencent/Hy3",
                    "base_url": "https://api.siliconflow.cn/v1",
                    "max_concurrent": 8,
                    "api_key": "secret-one",
                }
            ],
            "extraction_models": [
                {
                    "provider_preset": "vllm-rtx",
                    "model": "polymath-extract",
                    "base_url": "http://192.168.1.83:8000/v1",
                    "max_concurrent": 60,
                }
            ],
            "use_neo4j": True,
        },
    }
    changed_summary_pool = {
        **base_doc,
        "ingestion_config": {
            **base_doc["ingestion_config"],
            "summary_models": [
                {
                    "provider_preset": "deepseek",
                    "model": "deepseek/deepseek-v4-flash",
                    "base_url": "https://api.deepseek.com/v1",
                    "max_concurrent": 45,
                    "api_key": "secret-two",
                }
            ],
        },
    }
    changed_extraction_pool_only = {
        **base_doc,
        "ingestion_config": {
            **base_doc["ingestion_config"],
            "extraction_models": [
                {
                    "provider_preset": "longcat",
                    "model": "LongCat-2.0",
                    "base_url": "https://api.longcat.chat/openai/v1",
                    "max_concurrent": 45,
                    "api_key": "secret-three",
                }
            ],
        },
    }

    assert extraction_contract_hash(base_doc) != extraction_contract_hash(changed_summary_pool)
    assert extraction_contract_hash(base_doc) == extraction_contract_hash(changed_extraction_pool_only)


def test_extraction_provider_contract_is_safe_and_capability_aware():
    contract = extraction_provider_contract(
        {
            "ingestion_config": {
                "models_linked": True,
                "summary_models": [
                    {
                        "provider_preset": "longcat",
                        "model": "LongCat-2.0",
                        "base_url": "https://api.longcat.chat/openai/v1",
                        "max_concurrent": 45,
                        "api_key": "ak_never_persist_this",
                    }
                ],
            },
        }
    )

    assert contract["pool_source"] == "summary_models"
    assert contract["pool_size"] == 1
    assert contract["routing_policy"] == "work_stealing"
    assert contract["lanes"][0]["provider_card"]["schema_mode"] == "json_object_prompt"
    assert contract["lanes"][0]["provider_card"]["json_repair_mode"] == "deterministic_compiler"
    assert "ak_never_persist_this" not in str(contract)


def test_extraction_provider_contract_records_independent_mixed_lane_policy():
    contract = extraction_provider_contract(
        {
            "ingestion_config": {
                "models_linked": False,
                "extraction_models": [
                    {
                        "provider_preset": "vllm-rtx",
                        "model": "polymath-extract",
                        "base_url": "http://192.168.1.83:8000/v1",
                        "max_concurrent": 60,
                        "extra_params": {"managed_vllm": True},
                    },
                    {
                        "provider_preset": "siliconflow",
                        "model": "tencent/Hy3",
                        "base_url": "https://api.siliconflow.com/v1",
                        "max_concurrent": 8,
                    },
                ],
            },
        }
    )

    assert contract["routing_policy"] == "balanced"
    assert contract["lane_capacities"] == [
        {
            "lane": 0,
            "provider": "local_private_vllm",
            "model": "polymath-extract",
            "max_concurrent": 60,
            "concurrency_policy": "adaptive_vram_85",
            "local_private": True,
        },
        {
            "lane": 1,
            "provider": "siliconflow",
            "model": "tencent/Hy3",
            "max_concurrent": 8,
            "concurrency_policy": "static_lane_cap",
            "local_private": False,
        },
    ]


def test_mark_jobs_is_guarded_by_claimed_owner_lease() -> None:
    async def run() -> None:
        first_claim_at = datetime.utcnow()
        first_lease = first_claim_at + timedelta(minutes=10)
        second_claim_at = first_claim_at + timedelta(seconds=30)
        second_lease = second_claim_at + timedelta(minutes=10)
        db = _FakeDB(
            extraction_jobs=[
                {
                    "job_id": "still-owned",
                    "status": "running",
                    "runner": "extraction_jobs.run",
                    "last_run_at": first_claim_at,
                    "lease_until": first_lease,
                },
                {
                    "job_id": "reclaimed",
                    "status": "running",
                    "runner": "extraction_jobs.run",
                    "last_run_at": second_claim_at,
                    "lease_until": second_lease,
                },
            ]
        )

        modified = await _mark_jobs(
            db,
            updates={
                "still-owned": {"status": "succeeded", "lease_until": None},
                "reclaimed": {"status": "succeeded", "lease_until": None},
            },
            claimed_jobs=[
                {
                    "job_id": "still-owned",
                    "status": "running",
                    "runner": "extraction_jobs.run",
                    "last_run_at": first_claim_at,
                    "lease_until": first_lease,
                },
                {
                    "job_id": "reclaimed",
                    "status": "running",
                    "runner": "extraction_jobs.run",
                    "last_run_at": first_claim_at,
                    "lease_until": first_lease,
                },
            ],
        )

        rows = {
            row["job_id"]: row
            for row in db["extraction_jobs"].rows
        }
        assert modified == 1
        assert rows["still-owned"]["status"] == "succeeded"
        assert rows["reclaimed"]["status"] == "running"
        assert rows["reclaimed"]["last_run_at"] == second_claim_at
        assert rows["reclaimed"]["lease_until"] == second_lease

    asyncio.run(run())


def test_plan_extraction_jobs_advances_past_terminal_head_window():
    chunks = [
        {
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "chunk_id": f"chunk-{index:03d}",
            "text": f"Chunk {index}",
            "chunk_hash": f"hash-{index}",
        }
        for index in range(101)
    ]
    db = _FakeDB(
        documents=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "user_id": "user-1",
                "write_state": {"qdrant_written": True, "neo4j_written": False},
                "ingestion_config": {
                    "extraction_engine": "cloud",
                    "extraction_models": [
                        {"provider_preset": "vllm-rtx", "model": "polymath-extract"}
                    ],
                    "use_neo4j": True,
                },
            }
        ],
        chunks=chunks,
        ghost_b_extractions=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": f"chunk-{index:03d}",
                "status": "ok",
            }
            for index in range(100)
        ],
        extraction_jobs=[],
    )

    result = asyncio.run(
        plan_extraction_jobs(db, corpus_id="corpus-1", apply=False, limit=1)
    )

    assert result["planned"] == 1
    assert result["jobs"][0]["chunk_id"] == "chunk-100"
    assert result["jobs"][0]["reason"] == "missing_extraction"
    assert result["source_counts"]["chunks_scanned"] == 101
    assert result["source_counts"]["priority_graph_gap_docs"] == 1


def test_plan_extraction_jobs_excludes_docs_owned_by_active_batch_lease():
    db = _FakeDB(
        ingest_batch_items=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "status": "running",
                "lease_until": datetime.utcnow() + timedelta(minutes=5),
            }
        ],
        documents=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "user_id": "user-1",
                "ingestion_config": {
                    "extraction_engine": "cloud",
                    "use_neo4j": True,
                },
            }
        ],
        chunks=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-1",
                "text": "Do not duplicate this extraction.",
            }
        ],
    )

    result = asyncio.run(
        plan_extraction_jobs(db, corpus_id="corpus-1", apply=False, limit=10)
    )

    assert result["planned"] == 0
    assert result["source_counts"]["active_ingest_docs_excluded"] == 1


def test_run_extraction_jobs_does_not_claim_active_batch_documents():
    db = _FakeDB(
        ingest_batch_items=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "status": "running",
                "lease_until": datetime.utcnow() + timedelta(minutes=5),
            }
        ],
        extraction_jobs=[
            {
                "job_id": "queued-job",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-1",
                "status": "queued",
            }
        ],
    )

    result = asyncio.run(
        run_extraction_jobs(
            db,
            qdrant_client=None,
            corpus_id="corpus-1",
            limit=10,
        )
    )

    assert result["claimed"] == 0
    assert result["active_ingest_docs_excluded"] == 1
    assert db["extraction_jobs"].rows[0]["status"] == "queued"


def test_plan_extraction_jobs_requeues_reconciled_contract_drift_rows():
    db = _FakeDB(
        documents=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "user_id": "user-1",
                "filename": "book.md",
                "updated_at": "doc-v2",
                "ingestion_config": {
                    "extraction_engine": "cloud",
                    "extraction_models": [
                        {"provider_preset": "vllm-rtx", "model": "polymath-extract"}
                    ],
                    "use_neo4j": True,
                },
            }
        ],
        chunks=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-1",
                "parent_id": "parent-1",
                "user_id": "user-1",
                "text": "Fresh chunk text.",
                "chunk_hash": "fresh-hash",
                "updated_at": "chunk-v2",
            }
        ],
        ghost_b_extractions=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-1",
                "status": "stale_chunk_reference",
                "stale_reason": "stale_extraction_contract_mismatch",
                "repair_action": "requeue_with_current_contract",
                "previous_status": "error",
                "extraction_contract_hash": "old-contract",
            }
        ],
        extraction_jobs=[],
    )

    result = asyncio.run(
        plan_extraction_jobs(db, corpus_id="corpus-1", apply=False, limit=10)
    )

    assert result["planned"] == 1
    assert result["counts"] == {"queued": 1}
    job = result["jobs"][0]
    assert job["chunk_id"] == "chunk-1"
    assert job["status"] == "queued"
    assert job["reason"] == "contract_changed"
    assert job["extraction_contract_hash"] != "old-contract"


def test_plan_extraction_jobs_does_not_materialize_vectors_only_docs_as_pending():
    db = _FakeDB(
        documents=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "user_id": "user-1",
                "filename": "vectors-only.md",
                "updated_at": "doc-v1",
                "ingestion_config": {
                    "extraction_engine": "off",
                    "use_neo4j": False,
                },
            }
        ],
        chunks=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-1",
                "parent_id": "parent-1",
                "user_id": "user-1",
                "text": "This corpus intentionally has no graph extraction lane.",
                "chunk_hash": "chunk-hash",
                "updated_at": "chunk-v1",
            }
        ],
        ghost_b_extractions=[],
        extraction_jobs=[],
    )

    result = asyncio.run(
        plan_extraction_jobs(db, corpus_id="corpus-1", apply=False, limit=10)
    )

    assert result["planned"] == 0
    assert result["counts"] == {}
    assert result["source_counts"]["chunks_scanned"] == 1
    assert result["source_counts"]["disabled_extraction_chunks"] == 1


def test_plan_extraction_jobs_supersedes_old_jobs_when_extraction_is_disabled():
    db = _FakeDB(
        documents=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "user_id": "user-1",
                "filename": "vectors-only.md",
                "updated_at": "doc-v2",
                "ingestion_config": {
                    "extraction_engine": "off",
                    "use_neo4j": False,
                },
            }
        ],
        chunks=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-1",
                "parent_id": "parent-1",
                "user_id": "user-1",
                "text": "This document used to have extraction jobs.",
                "chunk_hash": "chunk-hash",
                "updated_at": "chunk-v2",
            }
        ],
        ghost_b_extractions=[],
        extraction_jobs=[
            {
                "job_id": "old-job",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-1",
                "status": "queued",
                "reason": "missing_extraction",
            }
        ],
    )

    result = asyncio.run(
        plan_extraction_jobs(db, corpus_id="corpus-1", apply=True, limit=10)
    )

    assert result["planned"] == 0
    assert result["superseded"] == 1
    assert db["extraction_jobs"].rows[0]["status"] == "superseded"
    assert db["extraction_jobs"].rows[0]["reason"] == "extraction_contract_disabled"


def test_extraction_job_run_update_marks_success_terminal():
    update = build_extraction_job_run_update(
        {"attempt_count": 2, "provider_route": {"pool_source": "summary_models"}},
        succeeded=True,
        result={
            "model": "polymath-extract",
            "provider": "local_private_vllm",
            "lane": 0,
            "schema_mode": "json_schema",
            "output_mode": "json_schema",
            "raw_output_artifact_id": "sha256:raw",
            "raw_output_fingerprint": {"sha256": "raw"},
            "prompt_hash": "prompt-hash",
            "prompt_chars": 456,
            "evidence_drop_count": 2,
            "validation_rejection_count": 2,
        },
    )

    assert update["status"] == "succeeded"
    assert update["reason"] == "ghost_b_ok"
    assert update["attempt_count"] == 3
    assert update["lease_until"] is None
    assert update["validation_errors"] == []
    assert update["provider_route"]["provider"] == "local_private_vllm"
    assert update["provider_route"]["model"] == "polymath-extract"
    assert update["raw_output_artifact_id"] == "sha256:raw"
    assert update["raw_output_fingerprint"] == {"sha256": "raw"}
    assert update["prompt_hash"] == "prompt-hash"
    assert update["prompt_chars"] == 456
    assert update["validation_summary"]["evidence_drops"] == 2
    assert update["validation_summary"]["validation_rejections"] == 2


def test_extraction_job_run_update_derives_artifact_id_when_raw_hash_missing():
    update = build_extraction_job_run_update(
        {
            "attempt_count": 0,
            "doc_id": "doc-1",
            "corpus_id": "corpus-1",
            "chunk_id": "chunk-1",
            "chunk_hash": "chunk-hash",
            "extraction_contract_hash": "contract-hash",
            "provider_route": {"pool_source": "extraction_models"},
        },
        succeeded=True,
        result={
            "chunk_id": "chunk-1",
            "entities": [],
            "relations": [],
            "prompt_hash": "prompt-sha",
        },
    )

    assert update["status"] == "succeeded"
    assert update["raw_output_artifact_id"].startswith("derived:")
    assert update["prompt_hash"] == "prompt-sha"


def test_extraction_job_run_update_classifies_provider_failure():
    update = build_extraction_job_run_update(
        {"attempt_count": 0, "provider_route": {"pool_source": "extraction_models"}},
        failure={
            "model": "tencent/Hy3",
            "lane": 1,
            "provider": "siliconflow",
            "schema_mode": "json_object_prompt",
            "output_mode": "json_object_prompt",
            "json_repair_mode": "deterministic_compiler",
            "error_type": "HTTPStatusError",
            "error_message": "429 rate limit",
            "raw_output_artifact_id": "sha256:failed-raw",
            "raw_output_fingerprint": {"sha256": "failed-raw", "chars": 12},
            "prompt_hash": "prompt-sha",
            "prompt_chars": 111,
        },
    )

    assert update["status"] == "provider_failed"
    assert update["reason"] == "provider_error"
    assert update["provider_route"]["model"] == "tencent/Hy3"
    assert update["provider_route"]["lane"] == 1
    assert update["provider_route"]["provider"] == "siliconflow"
    assert update["provider_route"]["schema_mode"] == "json_object_prompt"
    assert update["provider_route"]["output_mode"] == "json_object_prompt"
    assert update["failure"]["json_repair_mode"] == "deterministic_compiler"
    assert update["raw_output_artifact_id"] == "sha256:failed-raw"
    assert update["raw_output_fingerprint"] == {"sha256": "failed-raw", "chars": 12}
    assert update["prompt_hash"] == "prompt-sha"
    assert update["prompt_chars"] == 111


def test_extraction_job_run_update_classifies_schema_mode_rejection_as_provider_contract_block():
    update = build_extraction_job_run_update(
        {"attempt_count": 0, "provider_route": {"pool_source": "extraction_models"}},
        failure={
            "model": "polymath-extract",
            "lane": 0,
            "provider": "local_private_vllm",
            "schema_mode": "json_schema",
            "output_mode": "json_schema",
            "error_type": "json_schema_unsupported",
            "error_message": "response_format was rejected",
        },
    )

    assert update["status"] == "blocked_provider_contract"
    assert update["reason"] == "provider_contract_unsupported"
    assert update["validation_errors"] == []
    assert update["failure"]["provider"] == "local_private_vllm"
    assert update["failure"]["schema_mode"] == "json_schema"


def test_run_extraction_jobs_does_not_claim_provider_contract_blocks():
    db = _FakeDB(
        extraction_jobs=[
            {
                "job_id": "blocked-job",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-1",
                "status": "blocked_provider_contract",
                "reason": "provider_contract_unsupported",
                "attempt_count": 3,
            }
        ]
    )

    result = asyncio.run(
        run_extraction_jobs(
            db,
            qdrant_client=None,
            corpus_id="corpus-1",
            limit=10,
        )
    )

    assert result["claimed"] == 0
    assert db["extraction_jobs"].rows[0]["status"] == "blocked_provider_contract"
    assert db["extraction_jobs"].rows[0]["attempt_count"] == 3


def test_reconcile_terminal_extraction_jobs_closes_matching_retry_rows():
    db = _FakeDB(
        extraction_jobs=[
            {
                "job_id": "job-1",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-1",
                "status": "validation_failed",
                "extraction_contract_hash": "contract-1",
            },
            {
                "job_id": "job-2",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-2",
                "status": "validation_failed",
                "extraction_contract_hash": "new-contract",
            },
            {
                "job_id": "job-3",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-3",
                "status": "failed",
                "extraction_contract_hash": "contract-3",
            },
        ],
        ghost_b_extractions=[
            {
                "corpus_id": "corpus-1",
                "chunk_id": "chunk-1",
                "status": "ok",
                "extraction_contract_hash": "contract-1",
                "raw_output_artifact_id": "sha256:one",
            },
            {
                "corpus_id": "corpus-1",
                "chunk_id": "chunk-2",
                "status": "ok",
                "extraction_contract_hash": "old-contract",
            },
            {
                "corpus_id": "corpus-1",
                "chunk_id": "chunk-3",
                "status": "skipped",
                "skip_reason": "no_extractable_text_or_skipped_kind",
                "extraction_contract_hash": "contract-3",
            },
        ],
    )

    result = asyncio.run(
        reconcile_terminal_extraction_jobs(db, corpus_id="corpus-1")
    )

    assert result == {"scanned": 3, "synchronized": 2}
    assert db["extraction_jobs"].rows[0]["status"] == "succeeded"
    assert db["extraction_jobs"].rows[0]["reason"] == "ghost_b_ok"
    assert db["extraction_jobs"].rows[0]["raw_output_artifact_id"] == "sha256:one"
    assert db["extraction_jobs"].rows[1]["status"] == "validation_failed"
    assert db["extraction_jobs"].rows[2]["status"] == "skipped"
    assert (
        db["extraction_jobs"].rows[2]["reason"]
        == "no_extractable_text_or_skipped_kind"
    )


def test_run_extraction_jobs_reconciles_success_before_claiming():
    db = _FakeDB(
        extraction_jobs=[
            {
                "job_id": "job-1",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-1",
                "status": "validation_failed",
                "extraction_contract_hash": "contract-1",
            }
        ],
        ghost_b_extractions=[
            {
                "corpus_id": "corpus-1",
                "chunk_id": "chunk-1",
                "status": "ok",
                "extraction_contract_hash": "contract-1",
            }
        ],
    )

    result = asyncio.run(
        run_extraction_jobs(
            db,
            qdrant_client=None,
            corpus_id="corpus-1",
            limit=10,
            statuses=["validation_failed"],
        )
    )

    assert result["claimed"] == 0
    assert result["terminal_reconciliation"]["synchronized"] == 1
    assert db["extraction_jobs"].rows[0]["status"] == "succeeded"


def test_extraction_job_run_update_classifies_skipped_chunks():
    update = build_extraction_job_run_update(
        {"attempt_count": 1},
        skipped_reason="no_extractable_text_or_skipped_kind",
    )

    assert update["status"] == "skipped"
    assert update["source_status"] == "skipped"
    assert update["attempt_count"] == 2


def test_persist_extraction_rows_stamps_stage_identity_and_artifact_ids():
    db = _FakeDB(
        documents=[
            {
                "doc_id": "doc-1",
                "corpus_id": "corpus-1",
                "source_identity": {"content_sha256": "source-hash"},
                "source_key": "sha256:source-hash",
                "updated_at": "doc-v1",
                "ingestion_config": {
                    "extraction_engine": "cloud",
                    "extraction_models": [{"provider_preset": "vllm-rtx", "model": "m"}],
                    "use_neo4j": True,
                },
            }
        ],
        chunks=[
            {
                "doc_id": "doc-1",
                "corpus_id": "corpus-1",
                "chunk_id": "chunk-ok",
                "text": "ok text",
                "chunk_hash": "ok-hash",
                "chunk_version": "chunk-v1",
            },
            {
                "doc_id": "doc-1",
                "corpus_id": "corpus-1",
                "chunk_id": "chunk-fail",
                "text": "failed text",
                "text_hash": "fail-hash",
            },
        ],
        ghost_b_extractions=[],
    )

    asyncio.run(
        _persist_extraction_rows(
            db,
            doc_id="doc-1",
            corpus_id="corpus-1",
            results=[{"chunk_id": "chunk-ok", "entities": [], "relations": []}],
            failures=[
                {
                    "chunk_id": "chunk-fail",
                    "error_type": "parse_error",
                    "error_message": "bad json",
                    "raw_output_fingerprint": {"sha256": "raw-fail"},
                    "prompt_hash": "prompt-fail",
                }
            ],
        )
    )

    rows = {row["chunk_id"]: row for row in db["ghost_b_extractions"].rows}
    assert rows["chunk-ok"]["status"] == "ok"
    assert rows["chunk-ok"]["chunk_hash"] == "ok-hash"
    assert rows["chunk-ok"]["stage_identity"]["source_file_hash"] == "source-hash"
    assert rows["chunk-ok"]["raw_output_artifact_id"].startswith("derived:")
    assert rows["chunk-fail"]["status"] == "error"
    assert rows["chunk-fail"]["chunk_hash"] == "fail-hash"
    assert rows["chunk-fail"]["raw_output_artifact_id"] == "sha256:raw-fail"
    assert rows["chunk-fail"]["prompt_hash"] == "prompt-fail"


def test_persist_extraction_rows_stamps_live_corpus_contract():
    live_config = {
        "extraction_engine": "cloud",
        "models_linked": False,
        "extraction_models": [{"provider_preset": "longcat", "model": "LongCat-2.0"}],
        "use_neo4j": True,
    }
    db = _FakeDB(
        corpora=[
            {
                "corpus_id": "corpus-1",
                "default_ingestion_config": live_config,
            }
        ],
        documents=[
            {
                "doc_id": "doc-1",
                "corpus_id": "corpus-1",
                "updated_at": "doc-v1",
                "ingestion_config": {
                    "extraction_engine": "cloud",
                    "models_linked": False,
                    "extraction_models": [{"model": "retired-model"}],
                    "use_neo4j": True,
                },
            }
        ],
        chunks=[
            {
                "doc_id": "doc-1",
                "corpus_id": "corpus-1",
                "chunk_id": "chunk-1",
                "text": "Evidence text.",
                "chunk_hash": "chunk-hash",
            }
        ],
        ghost_b_extractions=[],
    )

    asyncio.run(
        _persist_extraction_rows(
            db,
            doc_id="doc-1",
            corpus_id="corpus-1",
            results=[{"chunk_id": "chunk-1", "entities": [], "relations": []}],
            failures=[],
        )
    )

    expected_doc = with_live_extraction_config(
        db["documents"].rows[0],
        live_config,
    )
    row = db["ghost_b_extractions"].rows[0]
    assert row["extraction_contract_hash"] == extraction_contract_hash(expected_doc)


def test_persist_skipped_extraction_rows_closes_error_and_preserves_audit():
    db = _FakeDB(
        documents=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "user_id": "user-1",
                "updated_at": "doc-v1",
                "ingestion_config": {
                    "extraction_engine": "cloud",
                    "extraction_models": [{"model": "extract-model"}],
                    "use_neo4j": True,
                },
            }
        ],
        chunks=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-1",
                "parent_id": "parent-1",
                "text": "References.",
                "chunk_hash": "chunk-hash",
            }
        ],
        ghost_b_extractions=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-1",
                "status": "error",
                "error_type": "parse_error",
                "error_message": "old provider failure",
            }
        ],
    )

    asyncio.run(
        _persist_skipped_extraction_rows(
            db,
            doc_id="doc-1",
            corpus_id="corpus-1",
            chunk_ids=["chunk-1"],
            reason="no_extractable_text_or_skipped_kind",
        )
    )

    row = db["ghost_b_extractions"].rows[0]
    assert row["status"] == "skipped"
    assert row["previous_status"] == "error"
    assert row["skip_reason"] == "no_extractable_text_or_skipped_kind"
    assert row["error_type"] == "parse_error"
    assert row["stage_identity"]["chunk_hash"] == "chunk-hash"
    assert row["raw_output_artifact_id"].startswith("derived:")
