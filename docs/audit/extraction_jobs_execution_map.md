# Extraction Jobs Execution Map

Branch `graphify/remediation-freeze-v2` of `/Users/king/polymath_v3.3`.
Audit scope: `backend/services/ingestion/extraction_jobs.py::run_extraction_jobs` and every function it calls to execute one job, through the write that sets a job status to `succeeded/promoted/skipped/failed`; plus `job_leases.py` claim/reclaim queries; `ingestion_service.py` wrapper, `_run_owned_repair_lane`, `_backpressure_pause_result`; `control_plane/reconciler.py` execution step of `reconcile_corpus`; and one hop deeper into modules called on the execute path.

---

## 1. Per-function atomic map

### `backend/services/ingestion/extraction_jobs.py`

#### `run_extraction_jobs(db, *, qdrant_client, corpus_id, user_id=None, limit=25, statuses=None)`
- **Signature**: `async def run_extraction_jobs(db: Any, *, qdrant_client: Any, corpus_id: str, user_id: str | None = None, limit: int = 25, statuses: list[str] | None = None) -> dict[str, Any]` (extraction_jobs.py:1235)
- **Imports inside body**:
  - `from services.ingestion.graph_backfill import (_extract_tasks, _load_backfill_config, _run_ghost_b_backfill)` (extraction_jobs.py:1251-1255)
  - `from config import get_settings` (extraction_jobs.py:1327)
- **Bound**: `limit = max(1, min(int(limit or 25), 500))` (extraction_jobs.py:1257)
- **Call reclaim**: `reclaimed = await reclaim_expired_running_jobs(db, collection_name="extraction_jobs", corpus_id=corpus_id, user_id=user_id, now=now)` (extraction_jobs.py:1259-1265)
- **Call terminal reconciliation**: `terminal_reconciliation = await reconcile_terminal_extraction_jobs(db, corpus_id=corpus_id, user_id=user_id, limit=max(1000, limit * 10))` (extraction_jobs.py:1266-1271)
- **Allowed statuses filter**:
  - `allowed_statuses = [s for s in (statuses or sorted(RUNNABLE_STATUSES)) if s in RUNNABLE_STATUSES]` (extraction_jobs.py:1272)
  - `if not allowed_statuses: allowed_statuses = sorted(RUNNABLE_STATUSES)` (extraction_jobs.py:1273-1274)
  - `RUNNABLE_STATUSES = {"queued", "provider_failed", "validation_failed", "failed"}` (extraction_jobs.py:37)
- **Read candidate jobs**:
  - Filter: `{"corpus_id": corpus_id, "status": {"$in": allowed_statuses}}` plus optional `{"doc_id": {"$nin": sorted(active_doc_ids)}}` and `{"user_id": user_id}` (extraction_jobs.py:1280-1284)
  - Query: `jobs = await db["extraction_jobs"].find(query, {"_id": 0}).sort("updated_at", 1).limit(limit).to_list(length=limit)` (extraction_jobs.py:1285)
- **Early return no candidates**: `if not jobs: return {..."claimed": 0...}` (extraction_jobs.py:1286-1297)
- **Claim**: `jobs = await claim_runnable_jobs(db, collection_name="extraction_jobs", jobs=jobs, runnable_statuses=allowed_statuses, now=now, runner="extraction_jobs.run", increment_attempt=False)` (extraction_jobs.py:1300-1308)
- **Early return no claims**: `if not jobs: return {..."candidates": candidate_count, "claimed": 0...}` (extraction_jobs.py:1309-1321)
- **Group by doc_id**: `jobs_by_doc[str(job.get("doc_id") or "")].append(job)` (extraction_jobs.py:1323-1325)
- **Doc concurrency**:
  - `configured_doc_concurrency = max(1, int(get_settings().EXTRACTION_REPAIR_MAX_ACTIVE_DOCS or 1))` (extraction_jobs.py:1329-1332)
  - `effective_doc_concurrency = min(configured_doc_concurrency, len(jobs_by_doc))` (extraction_jobs.py:1333)
  - `doc_semaphore = asyncio.Semaphore(max(1, effective_doc_concurrency))` (extraction_jobs.py:1334)
- **`_run_doc_jobs(doc_id, doc_jobs)`**:
  - **Signature**: nested async function inside `run_extraction_jobs` (extraction_jobs.py:1336)
  - Acquires semaphore: `async with doc_semaphore:` (extraction_jobs.py:1340)
  - **Re-check active ingest and defer**:
    - Condition: `if doc_id in await active_ingest_doc_ids(db, corpus_id=corpus_id):` (extraction_jobs.py:1341-1344)
    - Writes per-job: `{"status": "queued", "reason": "active_ingest_owned", "lease_until": None, "updated_at": deferred_at}` (extraction_jobs.py:1347-1353)
    - Calls `_mark_jobs(db, updates=defer_updates, claimed_jobs=doc_jobs)` (extraction_jobs.py:1355-1359)
    - Returns `({"doc_id": doc_id, "status": "deferred", "reason": "active_ingest_owned", ...}, status_counts)` (extraction_jobs.py:1364-1372)
  - **Read document**: `doc = await db["documents"].find_one({"doc_id": doc_id, "corpus_id": corpus_id}, {"_id": 0})` (extraction_jobs.py:1378-1381)
  - **Missing document skip**:
    - Condition: `if not doc:` (extraction_jobs.py:1383)
    - For each job sets update via `build_extraction_job_run_update(job, skipped_reason="document_missing", now=now)` (extraction_jobs.py:1384-1389)
    - Calls `_mark_jobs(...)` (extraction_jobs.py:1390-1394)
    - Returns `({"doc_id": doc_id, "status": "skipped", "reason": "document_missing"}, _status_counts_from_updates(...))` (extraction_jobs.py:1395-1398)
  - **Try/except around extraction** (extraction_jobs.py:1400-1543):
    - `try:`
      - `_extract_tasks(db=db, corpus_id=corpus_id, doc_id=doc_id, chunk_ids=chunk_ids)` (extraction_jobs.py:1401-1406)
      - `skipped_ids = set(chunk_ids) - {task.chunk_id for task in tasks}` (extraction_jobs.py:1407-1408)
      - For skipped chunks: `build_extraction_job_run_update(job, skipped_reason="no_extractable_text_or_skipped_kind", now=now)` (extraction_jobs.py:1409-1415)
      - If `skipped_ids`: `_persist_skipped_extraction_rows(db, doc_id=doc_id, corpus_id=corpus_id, chunk_ids=sorted(skipped_ids), reason="no_extractable_text_or_skipped_kind")` (extraction_jobs.py:1416-1423)
      - If `tasks`:
        - `_load_backfill_config(db=db, corpus_id=corpus_id, doc=doc)` (extraction_jobs.py:1425-1429)
        - `_run_ghost_b_backfill(db=db, qdrant_client=qdrant_client, corpus_id=corpus_id, tasks=tasks, config=config)` (extraction_jobs.py:1430-1436)
        - `_persist_extraction_rows(db, doc_id=doc_id, corpus_id=corpus_id, results=list(report.results), failures=list(report.failures))` (extraction_jobs.py:1437-1443)
        - Build `success_by_id`/`failure_by_id` from `report.results`/`report.failures` (extraction_jobs.py:1444-1453)
        - For each `job` in `doc_jobs`:
          - `if job["job_id"] in updates: continue` (extraction_jobs.py:1456-1457)
          - `if chunk_id in success_by_id`: `build_extraction_job_run_update(job, succeeded=True, result=..., now=now)` (extraction_jobs.py:1458-1464)
          - `elif chunk_id in failure_by_id`: `build_extraction_job_run_update(job, failure=..., now=now)` (extraction_jobs.py:1465-1470)
          - `else`: `build_extraction_job_run_update(job, failure={"error_type": "NoResult", "error_message": "Ghost B returned neither a result nor a failure for this chunk"}, now=now)` (extraction_jobs.py:1471-1482)
        - `metrics = dict(report.metrics or {})` (extraction_jobs.py:1483)
      - `else` (no tasks): `metrics = {"requested_chunks": len(chunk_ids), "extracted_chunks": 0, "failed_chunks": 0}` (extraction_jobs.py:1484-1489)
      - `_refresh_document_extraction_counts(db, doc_id=doc_id, corpus_id=corpus_id)` (extraction_jobs.py:1491-1495)
      - `_mark_jobs(db, updates=updates, claimed_jobs=doc_jobs)` (extraction_jobs.py:1496-1500)
      - Returns complete doc result (extraction_jobs.py:1505-1517)
    - `except Exception as exc:` (extraction_jobs.py:1518)
      - For each job: `build_extraction_job_run_update(job, error=exc, now=now)` (extraction_jobs.py:1519-1524)
      - `_mark_jobs(db, updates=updates, claimed_jobs=doc_jobs)` (extraction_jobs.py:1525-1529)
      - Returns failed doc result (extraction_jobs.py:1534-1543)
- **Gather**: `completed_docs = await asyncio.gather(*(_run_doc_jobs(doc_id, doc_jobs) for doc_id, doc_jobs in jobs_by_doc.items()))` (extraction_jobs.py:1545-1550)
- **Final return**: merges counts and doc results, returns dict with `status="complete"` (extraction_jobs.py:1558-1572)

#### `claim_runnable_jobs` (job_leases.py:337)
- **Signature**: `async def claim_runnable_jobs(db: Any, *, collection_name: str, jobs: list[dict[str, Any]], runnable_statuses: set[str] | tuple[str, ...] | list[str], now: datetime | None = None, runner: str, lease_seconds: int = DEFAULT_JOB_LEASE_SECONDS, increment_attempt: bool = False, set_fields: dict[str, Any] | None = None, max_attempts: int | None = None) -> list[dict[str, Any]]`
- **Early returns**: `if not jobs or not statuses: return []` (job_leases.py:361-362)
- **max_attempts source**:
  - `if max_attempts is None:` imports `from config import get_settings`; `max_attempts = int(getattr(get_settings(), "INGEST_JOB_MAX_ATTEMPTS", DEFAULT_JOB_MAX_ATTEMPTS) or DEFAULT_JOB_MAX_ATTEMPTS)` (job_leases.py:364-373)
  - `DEFAULT_JOB_MAX_ATTEMPTS = 5` (job_leases.py:24)
- **Lease deadline**: `deadline = lease_deadline(now, lease_seconds=lease_seconds)` (job_leases.py:374); `lease_deadline` returns `now + timedelta(seconds=max(60, int(lease_seconds or 0)))` (job_leases.py:277-282)
- **Per-job claim loop** (job_leases.py:376-441):
  - `job_id = str(job.get("job_id") or "")`; `if not job_id: continue` (job_leases.py:377-379)
  - Attempt count parse; `attempts = 0` on `TypeError/ValueError` (job_leases.py:381-383)
  - **Attempt limit dead-letter**:
    - Condition: `if increment_attempt and attempts >= max(1, int(max_attempts or 0)):` (job_leases.py:384)
    - Last error: `last_error = job.get("last_error") or job.get("error") or job.get("reason")` (job_leases.py:385)
    - Update filter: `{"job_id": job_id, "status": {"$in": statuses}}` (job_leases.py:387-391)
    - Update document:
      ```json
      {
        "$set": {
          "status": "dead_letter",
          "reason": "attempt_limit_exhausted",
          "failure_class": normalize_failure_class(last_error),
          "last_actionable_error": str(last_error or "")[:500],
          "dead_lettered_at": now,
          "updated_at": now,
          "lease_until": None
        },
        "$unset": {"runner": "", "started_at": ""}
      }
      ```
      (job_leases.py:392-403)
    - `upsert=False` (job_leases.py:404)
    - `except Exception: pass` (job_leases.py:406-407)
    - `continue` (job_leases.py:408)
  - **Claim update**:
    - Update document:
      ```json
      {
        "$set": {
          "status": "running",
          "runner": runner,
          "last_run_at": now,
          "lease_until": deadline,
          "updated_at": now,
          **(set_fields or {})
        }
      }
      ```
      (job_leases.py:409-418)
    - If `increment_attempt`: `update["$inc"] = {"attempt_count": 1}` (job_leases.py:419-420)
    - Update filter: `{"job_id": job_id, "status": {"$in": statuses}}` (job_leases.py:422-427)
    - `upsert=False` (job_leases.py:428)
    - `except Exception: continue` (job_leases.py:430-431)
  - If `modified_count <= 0`: `continue` (job_leases.py:432-433)
  - Otherwise append local mutated job copy to `claimed`.
- **Returns**: `claimed` list.

#### `reclaim_expired_running_jobs` (job_leases.py:285)
- **Signature**: `async def reclaim_expired_running_jobs(db: Any, *, collection_name: str, corpus_id: str, user_id: str | None = None, now: datetime | None = None, lease_seconds: int = DEFAULT_JOB_LEASE_SECONDS) -> int`
- `now = now or datetime.utcnow()` (job_leases.py:302)
- `stale_before = now - timedelta(seconds=max(60, int(lease_seconds or 0)))` (job_leases.py:303)
- **Filter** (job_leases.py:304-314):
  ```json
  {
    "corpus_id": corpus_id,
    "status": "running",
    "$or": [
      {"lease_until": {"$lte": now}},
      {"lease_until": {"$exists": False}, "updated_at": {"$lte": stale_before}},
      {"lease_until": None, "updated_at": {"$lte": stale_before}}
    ]
  }
  ```
  - plus `"user_id": user_id` if provided.
- **Update** (job_leases.py:316-330):
  ```json
  {
    "$set": {
      "status": "queued",
      "reason": "lease_expired",
      "updated_at": now,
      "last_reclaimed_at": now
    },
    "$unset": {
      "runner": "",
      "started_at": "",
      "lease_until": ""
    }
  }
  ```
- `except Exception: return 0` (job_leases.py:333-334)

#### `reconcile_terminal_extraction_jobs` (extraction_jobs.py:654)
- **Signature**: `async def reconcile_terminal_extraction_jobs(db: Any, *, corpus_id: str, user_id: str | None = None, limit: int = 5000) -> dict[str, int]`
- `limit = max(1, min(int(limit or 5000), 50000))` (extraction_jobs.py:670)
- `runnable_statuses = sorted(RETRYABLE_STATUSES | BLOCKED_STATUSES)` (extraction_jobs.py:671)
  - `RETRYABLE_STATUSES = {"queued", "provider_failed", "validation_failed", "failed"}` (extraction_jobs.py:35)
  - `BLOCKED_STATUSES = {"blocked_provider_contract"}` (extraction_jobs.py:34)
- **Read filter** (extraction_jobs.py:672-679):
  ```json
  {
    "corpus_id": corpus_id,
    "status": {"$in": ["blocked_provider_contract", "failed", "provider_failed", "queued", "validation_failed"]}
  }
  ```
  - plus `"user_id": user_id` if provided.
  - Projection: `{"_id": 0, "job_id": 1, "chunk_id": 1, "extraction_contract_hash": 1}`
- `if not chunk_ids: return {"scanned": len(jobs), "synchronized": 0}` (extraction_jobs.py:694-695)
- **Read ghost_b_extractions** (extraction_jobs.py:697-716):
  ```json
  {
    "corpus_id": corpus_id,
    "chunk_id": {"$in": chunk_ids},
    "status": {"$in": ["ok", "skipped"]}
  }
  ```
  - Projection: `{"_id": 0, "chunk_id": 1, "extraction_contract_hash": 1, "promoted_at": 1, "raw_output_artifact_id": 1, "raw_output_fingerprint": 1, "prompt_hash": 1, "prompt_chars": 1, "status": 1, "skip_reason": 1, "reason": 1}`
- **Match decision**: `terminal_extraction_artifact_matches_job(job, row)` (extraction_jobs.py:728-729):
  - `if str(extraction_row.get("status") or "") not in {"ok", "skipped"}: return False` (extraction_jobs.py:643)
  - `job_chunk` vs `row_chunk` equality check (extraction_jobs.py:645-648)
  - `job_contract` vs `row_contract` equality check (extraction_jobs.py:649-651)
- **Update per matched job** (extraction_jobs.py:757-767):
  - Filter:
    ```json
    {
      "job_id": job["job_id"],
      "status": {"$in": ["blocked_provider_contract", "failed", "provider_failed", "queued", "validation_failed"]}
    }
    ```
  - Update:
    ```json
    {
      "$set": {
        "status": "<promoted|succeeded|skipped>",
        "reason": "<graph_promoted|ghost_b_ok|<skip_reason/reason/no_extractable_text_or_skipped_kind>",
        "source_status": "<ok|skipped>",
        "lease_until": null,
        "terminal_reconciled_at": now,
        "updated_at": now,
        "raw_output_artifact_id": row.get("raw_output_artifact_id"),
        "raw_output_fingerprint": row.get("raw_output_fingerprint") or {},
        "prompt_hash": row.get("prompt_hash"),
        "prompt_chars": row.get("prompt_chars")
      },
      "$unset": {"runner": "", "started_at": "", "failure": ""}
    }
    ```
    - If `promoted_at`: also sets `"promoted_at": promoted_at` (extraction_jobs.py:754-755)
- `if not ops: return {"scanned": len(jobs), "synchronized": 0}` (extraction_jobs.py:768-769)
- `result = await db["extraction_jobs"].bulk_write(ops, ordered=False)` (extraction_jobs.py:770)

#### `_mark_jobs` (extraction_jobs.py:1178)
- **Signature**: `async def _mark_jobs(db: Any, *, updates: dict[str, dict[str, Any]], claimed_jobs: list[dict[str, Any]] | None = None) -> int`
- Early return: `if not updates: return 0` (extraction_jobs.py:1184-1185)
- For each `(job_id, update)`:
  - Base query: `{"job_id": job_id}` (extraction_jobs.py:1193)
  - If claimed job present, appends `{"status": "running"}` plus optional `runner`, `last_run_at`, `lease_until` to query (extraction_jobs.py:1195-1205)
  - Operation: `UpdateOne(query, {"$set": update, "$unset": {"runner": "", "started_at": ""}})` (extraction_jobs.py:1206-1214)
- `result = await db["extraction_jobs"].bulk_write(ops, ordered=False)` (extraction_jobs.py:1215)
- Returns `int(getattr(result, "modified_count", 0) or 0)` (extraction_jobs.py:1216)

#### `build_extraction_job_run_update` (extraction_jobs.py:505)
- **Signature**: `def build_extraction_job_run_update(job, *, succeeded=False, result=None, failure=None, skipped_reason=None, error=None, now=None) -> dict[str, Any]`
- `now = now or datetime.utcnow()` (extraction_jobs.py:517)
- `attempt_count = int(job.get("attempt_count") or 0) + 1` (extraction_jobs.py:518)
- Base dict:
  ```json
  {
    "attempt_count": attempt_count,
    "updated_at": now,
    "last_run_at": now,
    "lease_until": null
  }
  ```
  (extraction_jobs.py:519-524)
- **Succeeded branch** (`if succeeded:` extraction_jobs.py:525):
  - Calls `_ensure_extraction_artifact_id(result, fallback=job, status="ok")` (extraction_jobs.py:527)
  - Merges route fields from `result` if non-null/non-empty for keys: `model`, `provider`, `lane`, `schema_mode`, `output_mode`, `json_repair_mode`, `semantic_verifier_mode` (extraction_jobs.py:528-539)
  - Returns update dict with `status="succeeded"`, `reason="ghost_b_ok"`, `source_status="ok"`, empty validation errors, validation summary populated from result fields, artifact/fingerprint/prompt fields, `failure: null` (extraction_jobs.py:540-559)
- **Skipped branch** (`if skipped_reason:` extraction_jobs.py:560):
  - Returns `status="skipped"`, `reason=skipped_reason`, `source_status="skipped"`, empty validation errors, `failure: null` (extraction_jobs.py:561-568)
- **Failure branch** (extraction_jobs.py:569-620):
  - If `error is not None`: builds `failure = {"error_type": type(error).__name__, "error_message": str(error)[:1000]}` (extraction_jobs.py:569-573)
  - Calls `_ensure_extraction_artifact_id(failure, fallback=job, status="error")` (extraction_jobs.py:575)
  - Calls `classify_extraction_status({"status": "error", **failure})` to derive `status` and `reason` (extraction_jobs.py:576)
  - Merges route fields from failure: `model`, `lane`, `provider`, `schema_mode`, `output_mode` (extraction_jobs.py:577-588)
  - Sets `validation_errors` to `[failure.get("error_message")]` only when `status == "validation_failed" and failure.get("error_message")` (extraction_jobs.py:595-599)
  - Returns update with derived status/reason, `source_status="error"`, failure subdocument, artifact/fingerprint/prompt fields (extraction_jobs.py:589-620)

#### `_persist_extraction_rows` (extraction_jobs.py:1012)
- **Signature**: `async def _persist_extraction_rows(db: Any, *, doc_id: str, corpus_id: str, results: list[Any], failures: list[Any]) -> None`
- Early return: `if not prepared_rows: return` (extraction_jobs.py:1040-1041)
- Calls `_extraction_identity_context(...)` to load doc/chunks/contract_hash (extraction_jobs.py:1043-1048)
- For each row, stamps identity and artifact id.
- Bulk writes `ReplaceOne` to `ghost_b_extractions` with filter:
  ```json
  {"doc_id": doc_id, "corpus_id": corpus_id, "chunk_id": row["chunk_id"]}
  ```
  (extraction_jobs.py:1058-1064)
- `await db["ghost_b_extractions"].bulk_write(ops, ordered=False)` (extraction_jobs.py:1065)

#### `_persist_skipped_extraction_rows` (extraction_jobs.py:1068)
- **Signature**: `async def _persist_skipped_extraction_rows(db: Any, *, doc_id: str, corpus_id: str, chunk_ids: list[str], reason: str) -> int`
- Early return: `if not clean_ids: return 0` (extraction_jobs.py:1079-1080)
- Reads existing rows from `ghost_b_extractions` with filter:
  ```json
  {"doc_id": doc_id, "corpus_id": corpus_id, "chunk_id": {"$in": clean_ids}}
  ```
  (extraction_jobs.py:1087-1094)
- For each chunk, builds row with `status="skipped"`, `previous_status`, `skip_reason=reason`, `reason=reason`, `reconciled_at=now`, `updated_at=now`, merges previous fields and stamps identity.
- Bulk writes `ReplaceOne` with same doc/corpus/chunk_id filter (extraction_jobs.py:1127-1137)
- Returns modified + upserted count (extraction_jobs.py:1141-1144)

#### `_refresh_document_extraction_counts` (extraction_jobs.py:1147)
- **Signature**: `async def _refresh_document_extraction_counts(db: Any, *, doc_id: str, corpus_id: str) -> dict[str, int]`
- Counts `ghost_b_extractions` ok and error rows (extraction_jobs.py:1153-1158)
- Reads error sample: `await db["ghost_b_extractions"].find({"doc_id": doc_id, "corpus_id": corpus_id, "status": "error"}, {"_id": 0}).sort("updated_at", -1).limit(20).to_list(length=20)` (extraction_jobs.py:1159-1162)
- Updates `documents` with filter `{"doc_id": doc_id, "corpus_id": corpus_id}`:
  ```json
  {
    "$set": {
      "ghost_b_staging_count": ok_count,
      "ghost_b_failure_count": error_count,
      "ghost_b_failures": sample,
      "updated_at": datetime.utcnow()
    },
    "$unset": {"ghost_b_staging": ""}
  }
  ```
  (extraction_jobs.py:1163-1174)

#### `_extraction_identity_context` (extraction_jobs.py:365)
- **Signature**: `async def _extraction_identity_context(db: Any, *, doc_id: str, corpus_id: str, chunk_ids: list[str]) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]], str]`
- Reads `documents`:
  ```json
  {"doc_id": doc_id, "corpus_id": corpus_id}
  ```
  projection includes `doc_id, corpus_id, user_id, filename, updated_at, source_identity, source_key, content_sha256, source_file_hash, ingestion_config, schema_lens` (extraction_jobs.py:373-389)
- If doc found, reads `corpora`:
  ```json
  {"corpus_id": corpus_id}
  ```
  projection `{"_id": 0, "default_ingestion_config": 1}` (extraction_jobs.py:391-394)
  then overlays live config via `with_live_extraction_config` (extraction_jobs.py:395-398)
- Reads `chunks`:
  ```json
  {"doc_id": doc_id, "corpus_id": corpus_id, "chunk_id": {"$in": clean_chunk_ids}}
  ```
  (extraction_jobs.py:401-407)
- Returns `(doc, chunks_by_id, extraction_contract_hash(doc))` (extraction_jobs.py:419)

#### `classify_extraction_status` (extraction_jobs.py:184)
- **Signature**: `def classify_extraction_status(row: dict[str, Any] | None) -> tuple[str, str]`
- Returns `("queued", "missing_extraction")` if no row (extraction_jobs.py:185-186)
- Treats `status in ("deleted", "deleting")` as `("queued", "missing_extraction")` (extraction_jobs.py:188-193)
- `status == "ok"` -> `("promoted", "graph_promoted")` if `promoted_at`, else `("succeeded", "ghost_b_ok")` (extraction_jobs.py:194-197)
- `status == "skipped"` -> `("skipped", <skip_reason/reason/no_extractable_text_or_skipped_kind>)` (extraction_jobs.py:198-203)
- `status == "stale_chunk_reference"` -> maps to `("queued", "contract_changed")`, `("queued", "chunk_changed")`, or `("skipped", "stale_chunk_reference")` depending on `stale_reason`/`repair_action` (extraction_jobs.py:204-217)
- Otherwise token-matches `error_type`/`error_message` to determine `blocked_provider_contract`, `validation_failed`, `provider_failed`, or `failed` (extraction_jobs.py:218-247)

#### `terminal_extraction_artifact_matches_job` (extraction_jobs.py:630)
- **Signature**: `def terminal_extraction_artifact_matches_job(job: dict[str, Any], extraction_row: dict[str, Any]) -> bool`
- Conditions: row status in `{"ok", "skipped"}`; chunk ids equal and non-empty; contract hashes equal and non-empty (extraction_jobs.py:643-651)

#### `active_ingest_doc_ids` (extraction_jobs.py:56)
- **Signature**: `async def active_ingest_doc_ids(db: Any, *, corpus_id: str, now: datetime | None = None) -> set[str]`
- Reads `ingest_batch_items`:
  ```json
  {
    "corpus_id": corpus_id,
    "status": "running",
    "doc_id": {"$nin": [null, ""]},
    "lease_until": {"$gt": now}
  }
  ```
  projection `{"_id": 0, "doc_id": 1}` (extraction_jobs.py:68-76)

### `backend/services/ingestion/job_leases.py`

#### `corpus_lane_lease` (job_leases.py:205)
- **Signature**: `@asynccontextmanager async def corpus_lane_lease(db: Any, *, corpus_id: str, lane: str, owner: str, lease_seconds: int = DEFAULT_LANE_LEASE_SECONDS, adopt_prefix: str | None = None)`
- `DEFAULT_LANE_LEASE_SECONDS = 30 * 60` (job_leases.py:25)
- Calls `acquire_lane_lease` (job_leases.py:217-224)
- If lease acquired, clears `_LOST_LANES` for corpus and starts heartbeat task (job_leases.py:226-260)
  - Heartbeat interval: `max(20.0, min(60.0, float(lease_seconds) / 3.0))` (job_leases.py:236)
- Finally cancels heartbeat and releases lease (job_leases.py:263-274)

#### `acquire_lane_lease` (job_leases.py:64)
- **Signature**: `async def acquire_lane_lease(db: Any, *, corpus_id: str, lane: str, owner: str, now: datetime | None = None, lease_seconds: int = DEFAULT_LANE_LEASE_SECONDS, adopt_prefix: str | None = None, adopt_stale_seconds: float = DEFAULT_LANE_ADOPT_STALE_SECONDS) -> dict[str, Any] | None`
- `DEFAULT_LANE_ADOPT_STALE_SECONDS = 420.0` (job_leases.py:35)
- Generates `lease_id = uuid4().hex`, `key = f"{corpus_id}:{lane}"`, `deadline = lease_deadline(now, lease_seconds=lease_seconds)` (job_leases.py:90-92)
- Claim predicates:
  ```json
  [
    {"lease_until": {"$lte": now}},
    {"lease_until": {"$exists": false}},
    {"owner": owner}
  ]
  ```
  (job_leases.py:101-105)
  - plus `{"owner": {"$regex": f"^{re.escape(adopt_prefix)}"}, "updated_at": {"$lte": now - timedelta(seconds=float(adopt_stale_seconds))}}` if `adopt_prefix` (job_leases.py:106-114)
- `find_one_and_update` filter:
  ```json
  {
    "_id": key,
    "$or": claim_predicates
  }
  ```
  (job_leases.py:120-123)
- Update:
  ```json
  {
    "$set": {
      "corpus_id": corpus_id,
      "lane": lane,
      "owner": owner,
      "lease_id": lease_id,
      "lease_until": deadline,
      "updated_at": now
    },
    "$setOnInsert": {"created_at": now}
  }
  ```
  (job_leases.py:124-135)
- `upsert=True`, `return_document=ReturnDocument.AFTER` (job_leases.py:136)
- Exception handling returns `None`, `compatibility_lease`, or `None` for DuplicateKeyError/AssertionError/KeyError/AttributeError/TypeError/Exception (job_leases.py:138-143)

### `backend/services/ingestion_service.py`

#### `run_extraction_jobs(self, *, corpus_id, user_id, limit=25, statuses=None)` (ingestion_service.py:2768)
- **Signature**: `async def run_extraction_jobs(self, *, corpus_id: str, user_id: str, limit: int = 25, statuses: list[str] | None = None) -> dict`
- Imports `from services.ingestion.extraction_jobs import run_extraction_jobs` (ingestion_service.py:2776)
- Backpressure check:
  - `paused = await self._backpressure_pause_result(corpus_id=corpus_id, lane_key="extraction_backfill_allowed", operation="extraction_jobs.run")` (ingestion_service.py:2778-2782)
  - `if paused is not None:` adds `claimed=0, counts={}` and returns (ingestion_service.py:2783-2785)
- Defines `_execute_extraction_lane()` closure calling `run_extraction_jobs(self._db, qdrant_client=self._qdrant, corpus_id=corpus_id, user_id=user_id, limit=limit, statuses=statuses)` (ingestion_service.py:2787-2795)
- Calls `_run_owned_repair_lane(corpus_id=corpus_id, lane="extraction", operation="extraction_jobs.run", runner=_execute_extraction_lane)` (ingestion_service.py:2797-2802)
- Materializes readiness and attaches to result if not None (ingestion_service.py:2803-2806)

#### `_backpressure_pause_result(self, *, corpus_id, lane_key, operation, readiness=None)` (ingestion_service.py:1395)
- **Signature**: `async def _backpressure_pause_result(self, *, corpus_id: str, lane_key: str, operation: str, readiness: dict | None = None) -> dict | None`
- Computes readiness if not provided: `readiness = readiness or await self._compute_corpus_readiness_safely(corpus_id)` (ingestion_service.py:1403)
- `pressure = (readiness or {}).get("pressure") or {}` (ingestion_service.py:1404)
- `backpressure = pressure.get("backpressure") or {}` (ingestion_service.py:1405)
- Early return `None`: `if backpressure.get(lane_key) is not False: return None` (ingestion_service.py:1406-1407)
- Returns dict with `status="paused_pressure"`, `corpus_id`, `operation`, `reason=f"{lane_key}=false"`, `pressure`, `readiness` (ingestion_service.py:1408-1415)

#### `_run_owned_repair_lane(self, *, corpus_id, lane, operation, runner)` (ingestion_service.py:1417)
- **Signature**: `async def _run_owned_repair_lane(self, *, corpus_id: str, lane: str, operation: str, runner: Any) -> dict[str, Any]`
- Imports `from services.ingestion.job_leases import corpus_lane_lease` (ingestion_service.py:1427)
- `owner = f"{operation}:{uuid.uuid4().hex}"` (ingestion_service.py:1429)
- Enters `corpus_lane_lease(self._db, corpus_id=corpus_id, lane=lane, owner=owner)` (ingestion_service.py:1430-1435)
- Early return if no lease:
  - `if not lease: return {"status": "lease_busy", "corpus_id": corpus_id, "lane": lane, "operation": operation, "claimed": 0, "counts": {}}` (ingestion_service.py:1436-1444)
- Calls `runner()` and awaits if coroutine (ingestion_service.py:1445-1448)

#### Other `ingestion_service.py` call sites of `self.run_extraction_jobs`
- `_extraction_runner` closure in `run_corpus_commander_cycle` (ingestion_service.py:3325-3330): calls `self.run_extraction_jobs(corpus_id=corpus_id, user_id=user_id, limit=limit)`.
- In `run_bounded_corpus_repair_cycle` (?) caller around ingestion_service.py:3780-3788: when `run_extraction_lane` true, appends `self.run_extraction_jobs(corpus_id=corpus_id, user_id=user_id, limit=extraction_run_limit)` to `lane_coroutines`.

### `backend/services/control_plane/reconciler.py`

#### `reconcile_corpus` execution step (reconciler.py:486-563)
- **Signature**: `async def reconcile_corpus(db: Any, *, ingestion_service: Any, corpus_id: str, user_id: str, doc_census_limit: int | None = None, execute: bool = True) -> dict[str, Any]`
- Executes only if `execute and (gap_totals or outbox_rows or pending_runs)` (reconciler.py:486)
- Computes `lane_flags = _lane_run_flags(settings)` (reconciler.py:487)
- **Inline-batch gate**: counts `ingest_batch_items` with status in `["queued", "staged", "running", "failed_recoverable"]` (reconciler.py:497-502). On exception, defaults `pending_inline = 1` (reconciler.py:503-504).
- If `pending_inline`: sets `lane_flags["run_extraction_jobs"] = lane_flags["run_summary_jobs"] = lane_flags["run_graph_jobs"] = False` (reconciler.py:505-507)
- Calls `ingestion_service.run_bounded_corpus_repair_cycle(...)` with:
  - `corpus_id=corpus_id, user_id=user_id, apply=True`
  - all planning flags `False` (reconciler.py:515-519)
  - `run_extraction_jobs=lane_flags["run_extraction_jobs"]` (reconciler.py:528)
  - `extraction_job_run_limit=int(getattr(settings, "INGEST_AUTO_REPAIR_EXTRACTION_RUN_LIMIT", 100) or 100)` (reconciler.py:529-531)
  - other lane flags and limits.
- Records attempt via `_record_attempt(db, corpus_id=corpus_id, stage="execute", action="run_bounded_corpus_repair_cycle", receipt=execution_receipt, started=started)` (reconciler.py:542-549)
- Wrapped in `try/except Exception` that logs warning and records failed attempt (reconciler.py:550-563)

### `backend/services/ingestion/corpus_repair.py` (one hop from reconciler execute step)

#### `run_bounded_corpus_repair_cycle` extraction run branch (corpus_repair.py:745-802)
- `if run_extraction_job_rows:` (corpus_repair.py:745)
  - If `not apply`: append skipped dry-run step (corpus_repair.py:746-754)
  - `elif qdrant_client is None`: append skipped-no-qdrant step (corpus_repair.py:755-763)
  - Else:
    - `pressure_readiness = await _refresh_backpressure_readiness(db, corpus_id=corpus_id, fallback=readiness_before)` (corpus_repair.py:765-769)
    - If not `_backpressure_allowed(pressure_readiness, "extraction_backfill_allowed")`: append pressure skip step (corpus_repair.py:770-779)
    - Else if `ingestion_service is not None`: `result = await ingestion_service.run_extraction_jobs(corpus_id=corpus_id, user_id=user_id, limit=extraction_job_run_limit)` (corpus_repair.py:781-786)
    - Else: `result = await run_extraction_jobs(db, qdrant_client=qdrant_client, corpus_id=corpus_id, user_id=user_id, limit=extraction_job_run_limit)` (corpus_repair.py:787-794)

### `backend/services/ingestion/graph_backfill.py` (one hop from extraction_jobs execute path)

#### `_extract_tasks` (graph_backfill.py:297)
- **Signature**: `async def _extract_tasks(*, db: AsyncIOMotorDatabase, corpus_id: str, doc_id: str, chunk_ids: list[str] | None = None) -> tuple[list[ExtractionTask], list[str]]`
- Reads `chunks`:
  ```json
  {"doc_id": doc_id, "corpus_id": corpus_id}
  ```
  - If `chunk_ids is not None`: adds `"chunk_id": {"$in": chunk_ids}` (graph_backfill.py:304-306)
  - Projection: `{"chunk_id": 1, "text": 1, "chunk_kind": 1, "_id": 0}` (graph_backfill.py:307-310)
- Builds `ExtractionTask` for each row where `chunk_id` exists, text non-empty after strip, and `not should_skip_ghost_b(str(row.get("chunk_kind") or ChunkKind.BODY))` (graph_backfill.py:312-323)

#### `_load_backfill_config` (graph_backfill.py:280)
- **Signature**: `async def _load_backfill_config(*, db: AsyncIOMotorDatabase, corpus_id: str, doc: dict) -> IngestionConfig`
- Reads `corpora`: `{"corpus_id": corpus_id}` (graph_backfill.py:286)
- Calls `build_effective_config(frozen_base=doc.get("ingestion_config") or live_cfg, live_corpus=live_cfg, ingest_overrides=None)` (graph_backfill.py:288-294)

#### `_run_ghost_b_backfill` (graph_backfill.py:327)
- **Signature**: `async def _run_ghost_b_backfill(*, db: AsyncIOMotorDatabase, qdrant_client: AsyncQdrantClient, corpus_id: str, tasks: list[ExtractionTask], config: IngestionConfig) -> ExtractionBatchReport`
- `engine = str(getattr(config, "extraction_engine", "graphify_cpu") or "graphify_cpu")` (graph_backfill.py:335)
- **Engine off**: `if engine == "off": return ExtractionBatchReport(results=[], failures=[], metrics={..."skipped": True...})` (graph_backfill.py:336-347)
- **Engine ghost_b_llm**: `if engine == "ghost_b_llm":` (graph_backfill.py:348)
  - `if not tasks: return ExtractionBatchReport(results=[], failures=[], metrics={"engine": engine})` (graph_backfill.py:353-356)
  - Builds ghost pool from `config.extraction_models` (graph_backfill.py:361-376)
  - Calls `extract_entities(tasks, schema=schema, pool=pool, return_report=True, enable_facts=_gs().EXTRACTION_ENABLE_FACTS)` (graph_backfill.py:384-390)
  - Wraps/returns `ExtractionBatchReport` (graph_backfill.py:391-396)
- **Unsupported engine**: `if engine != "graphify_cpu": raise RuntimeError(f"unsupported extraction engine: {engine}")` (graph_backfill.py:397-398)
- **No tasks**: `if not tasks: return ExtractionBatchReport(results=[], failures=[], metrics={"engine": engine})` (graph_backfill.py:399-400)
- **graphify_cpu path**: imports `run_graphify_pipeline` and calls:
  ```python
  output = await run_graphify_pipeline(
      db=db,
      corpus_id=corpus_id,
      doc_id=tasks[0].doc_id,
      text="\n\n".join(task.text for task in tasks),
      children=tasks,
      source_uri="graph_backfill",
  )
  ```
  (graph_backfill.py:402-412)
- Returns `output.report` (graph_backfill.py:412)

### `backend/services/ingestion/build_effective_config` (ingestion_service.py:166)
- **Signature**: `def build_effective_config(*, frozen_base: dict, live_corpus: dict, ingest_overrides: dict | None = None) -> IngestionConfig`
- Merges `FROZEN_CONFIG_FIELDS` from `frozen_base`, `MUTABLE_CONFIG_FIELDS` from `live_corpus`, optional `ingest_overrides` (ingestion_service.py:184-199)
- Returns `IngestionConfig(**merged)` (ingestion_service.py:200)
- `FROZEN_CONFIG_FIELDS` / `MUTABLE_CONFIG_FIELDS` NOT EXAMINED.

### `backend/db/queue_integrity.py`

#### `bulk_upsert_durable_jobs` (queue_integrity.py:266)
- **Signature**: `async def bulk_upsert_durable_jobs(collection: Any, ops: list[Any]) -> Any`
- `if not ops: return None` (queue_integrity.py:274-275)
- Tries `collection.bulk_write(ops, ordered=False)` (queue_integrity.py:277)
- On `BulkWriteError` with all errors code 11000, replays as `UpdateOne(op._filter, op._doc, upsert=False)` (queue_integrity.py:284-295)
- Used in `plan_extraction_jobs` via `await bulk_upsert_durable_jobs(db["extraction_jobs"], ops)` (extraction_jobs.py:999)

---

## 2. Job status state machine table

All transitions performed by Mongo `update_one`, `update_many`, or `bulk_write` in the execution path.

| From status(es) | To status | Exact filter | Exact update / $set fields | File:line |
|-----------------|-----------|--------------|---------------------------|-----------|
| `running` | `queued` | `{"corpus_id": corpus_id, "status": "running", "$or": [{"lease_until": {"$lte": now}}, {"lease_until": {"$exists": False}, "updated_at": {"$lte": stale_before}}, {"lease_until": None, "updated_at": {"$lte": stale_before}}]}` (plus optional `"user_id": user_id`) | `$set: {"status": "queued", "reason": "lease_expired", "updated_at": now, "last_reclaimed_at": now}`; `$unset: {"runner": "", "started_at": "", "lease_until": ""}` | job_leases.py:316-330 |
| runnable set `{"blocked_provider_contract", "failed", "provider_failed", "queued", "validation_failed"}` | `dead_letter` | `{"job_id": job_id, "status": {"$in": statuses}}` | `$set: {"status": "dead_letter", "reason": "attempt_limit_exhausted", "failure_class": normalize_failure_class(last_error), "last_actionable_error": str(last_error or "")[:500], "dead_lettered_at": now, "updated_at": now, "lease_until": None}`; `$unset: {"runner": "", "started_at": ""}` | job_leases.py:387-403 |
| runnable set (caller-provided, e.g. allowed_statuses) | `running` | `{"job_id": job_id, "status": {"$in": statuses}}` | `$set: {"status": "running", "runner": runner, "last_run_at": now, "lease_until": deadline, "updated_at": now, **(set_fields or {})}`; optional `$inc: {"attempt_count": 1}` | job_leases.py:422-429 |
| runnable set `{"blocked_provider_contract", "failed", "provider_failed", "queued", "validation_failed"}` | `succeeded` / `promoted` / `skipped` | `{"job_id": job["job_id"], "status": {"$in": runnable_statuses}}` | `$set: {"status": terminal_status, "reason": terminal_reason, "source_status": row_status, "lease_until": None, "terminal_reconciled_at": now, "updated_at": now, "raw_output_artifact_id": ..., "raw_output_fingerprint": ..., "prompt_hash": ..., "prompt_chars": ...}`; if promoted also `"promoted_at": promoted_at`; `$unset: {"runner": "", "started_at": "", "failure": ""}` | extraction_jobs.py:757-767 |
| `running` | `queued` (active ingest re-check) | `{"job_id": job_id, "status": "running", "runner": runner, "last_run_at": last_run_at, "lease_until": lease_until}` (query extended from claimed job snapshot) | `$set: {"status": "queued", "reason": "active_ingest_owned", "lease_until": None, "updated_at": deferred_at}`; `$unset: {"runner": "", "started_at": ""}` | extraction_jobs.py:1193-1214 via _mark_jobs, called at extraction_jobs.py:1355 |
| `running` | `skipped` (document missing) | same extended query pattern as above | `$set: update` where update from `build_extraction_job_run_update(job, skipped_reason="document_missing", now=now)` sets `status="skipped", reason="document_missing", source_status="skipped", failure: null, lease_until: null, attempt_count +1` | extraction_jobs.py:1384-1394 via _mark_jobs |
| `running` | `skipped` (no extractable text / skipped kind) | same extended query pattern | update from `build_extraction_job_run_update(job, skipped_reason="no_extractable_text_or_skipped_kind", now=now)` sets `status="skipped", reason="no_extractable_text_or_skipped_kind", source_status="skipped"` | extraction_jobs.py:1409-1415 via deferred updates then _mark_jobs at extraction_jobs.py:1496 |
| `running` | `succeeded` | same extended query pattern | update from `build_extraction_job_run_update(job, succeeded=True, result=..., now=now)` sets `status="succeeded", reason="ghost_b_ok", source_status="ok"` | extraction_jobs.py:1458-1464 via _mark_jobs at extraction_jobs.py:1496 |
| `running` | derived `status` (validation_failed/provider_failed/failed/etc.) | same extended query pattern | update from `build_extraction_job_run_update(job, failure=..., now=now)` sets `status=<derived>, source_status="error", failure: {...}` | extraction_jobs.py:1465-1482 via _mark_jobs at extraction_jobs.py:1496 |
| `running` | derived `status` (exception path) | same extended query pattern | update from `build_extraction_job_run_update(job, error=exc, now=now)` sets `status=<derived by classify_extraction_status>, source_status="error", failure: {...}` | extraction_jobs.py:1518-1529 via _mark_jobs |

Filters for `_mark_jobs` (extraction_jobs.py:1193-1205):
- Base: `{"job_id": job_id}`
- If claimed job present: adds `{"status": "running"}`
- If claimed runner non-null: adds `"runner": runner`
- If claimed `last_run_at` non-null: adds `"last_run_at": last_run_at`
- If claimed `lease_until` non-null: adds `"lease_until": lease_until`

---

## 3. Collection I/O table

### `extraction_jobs`

| Operation | Function | Verbatim filter | Verbatim update / projection | File:line |
|-----------|----------|-----------------|------------------------------|-----------|
| Read (candidate jobs) | `run_extraction_jobs` | `{"corpus_id": corpus_id, "status": {"$in": allowed_statuses}}` plus optional `"doc_id": {"$nin": sorted(active_doc_ids)}` and `"user_id": user_id`; projection `{"_id": 0}`; sort `updated_at` asc, limit | — | extraction_jobs.py:1280-1285 |
| Update many | `reclaim_expired_running_jobs` | `{"corpus_id": corpus_id, "status": "running", "$or": [{"lease_until": {"$lte": now}}, {"lease_until": {"$exists": False}, "updated_at": {"$lte": stale_before}}, {"lease_until": None, "updated_at": {"$lte": stale_before}}]}` | `$set {"status":"queued","reason":"lease_expired","updated_at":now,"last_reclaimed_at":now}`; `$unset {"runner":"","started_at":"","lease_until":""}` | job_leases.py:316-330 |
| Read | `reconcile_terminal_extraction_jobs` | `{"corpus_id": corpus_id, "status": {"$in": ["blocked_provider_contract","failed","provider_failed","queued","validation_failed"]}}`; projection `{"_id":0,"job_id":1,"chunk_id":1,"extraction_contract_hash":1}`; limit | — | extraction_jobs.py:672-686 |
| Bulk write (UpdateOne) | `reconcile_terminal_extraction_jobs` | `{"job_id": job["job_id"], "status": {"$in": runnable_statuses}}` | `$set {status, reason, source_status, lease_until:None, terminal_reconciled_at, updated_at, raw_output_artifact_id, raw_output_fingerprint, prompt_hash, prompt_chars}` (+promoted_at if applicable); `$unset {runner, started_at, failure}` | extraction_jobs.py:757-767 |
| Bulk write (UpdateOne) | `claim_runnable_jobs` | `{"job_id": job_id, "status": {"$in": statuses}}` | `$set {status:"running", runner, last_run_at, lease_until, updated_at, **set_fields}`; `$inc {attempt_count:1}` if increment_attempt | job_leases.py:422-429 |
| UpdateOne | `claim_runnable_jobs` (attempt limit) | `{"job_id": job_id, "status": {"$in": statuses}}` | `$set {status:"dead_letter", reason:"attempt_limit_exhausted", failure_class, last_actionable_error, dead_lettered_at, updated_at, lease_until:None}`; `$unset {runner, started_at}` | job_leases.py:387-403 |
| Bulk write (UpdateOne) | `_mark_jobs` | `{"job_id": job_id}` plus optional `{"status":"running"}`, `"runner"`, `"last_run_at"`, `"lease_until"` from claimed snapshot | `$set: update` (caller-provided); `$unset {runner, started_at}` | extraction_jobs.py:1206-1214 |
| Bulk write (UpdateOne) | `plan_extraction_jobs` | `{"job_id": job["job_id"]}` | `$set {**job, updated_at:now, last_planned_at:now}`; `$setOnInsert {created_at:now, lease_until:None}`; `upsert=True` | extraction_jobs.py:983-998 |

### `ingest_lane_leases`

| Operation | Function | Verbatim filter | Verbatim update | File:line |
|-----------|----------|-----------------|-----------------|-----------|
| find_one_and_update | `acquire_lane_lease` | `{"_id": key, "$or": [{"lease_until": {"$lte": now}}, {"lease_until": {"$exists": False}}, {"owner": owner}, {"owner": {"$regex": f"^{re.escape(adopt_prefix)}"}, "updated_at": {"$lte": now - timedelta(seconds=float(adopt_stale_seconds))}}]}` | `$set {corpus_id, lane, owner, lease_id, lease_until, updated_at}`; `$setOnInsert {created_at}`; `upsert=True` | job_leases.py:120-135 |
| delete_one | `release_lane_lease` | `{"_id": f"{corpus_id}:{lane}", "lease_id": lease_id}` | — | job_leases.py:161-166 |
| update_one | `renew_lane_lease` | `{"_id": f"{corpus_id}:{lane}", "lease_id": lease_id}` | `$set {lease_until: now+timedelta(seconds=max(60,int(lease_seconds))), heartbeat_at:now, updated_at:now}` | job_leases.py:187-197 |

### `ingest_batch_items`

| Operation | Function | Verbatim filter | Projection | File:line |
|-----------|----------|-----------------|------------|-----------|
| find | `active_ingest_doc_ids` | `{"corpus_id": corpus_id, "status": "running", "doc_id": {"$nin": [None, ""]}, "lease_until": {"$gt": now}}` | `{"_id": 0, "doc_id": 1}` | extraction_jobs.py:68-76 |
| count_documents | `reconcile_corpus` | `{"corpus_id": corpus_id, "status": {"$in": ["queued", "staged", "running", "failed_recoverable"]}}` | — | reconciler.py:497-502 |

### `documents`

| Operation | Function | Verbatim filter | Verbatim update / projection | File:line |
|-----------|----------|-----------------|------------------------------|-----------|
| find_one | `_extraction_identity_context` | `{"doc_id": doc_id, "corpus_id": corpus_id}` | projection includes `doc_id, corpus_id, user_id, filename, updated_at, source_identity, source_key, content_sha256, source_file_hash, ingestion_config, schema_lens` | extraction_jobs.py:373-389 |
| find_one | `_run_doc_jobs` | `{"doc_id": doc_id, "corpus_id": corpus_id}` | `{"_id": 0}` | extraction_jobs.py:1378-1381 |
| update_one | `_refresh_document_extraction_counts` | `{"doc_id": doc_id, "corpus_id": corpus_id}` | `$set {ghost_b_staging_count, ghost_b_failure_count, ghost_b_failures, updated_at}`; `$unset {ghost_b_staging: ""}` | extraction_jobs.py:1163-1174 |

### `corpora`

| Operation | Function | Verbatim filter | Projection | File:line |
|-----------|----------|-----------------|------------|-----------|
| find_one | `_extraction_identity_context` | `{"corpus_id": corpus_id}` | `{"_id": 0, "default_ingestion_config": 1}` | extraction_jobs.py:391-394 |
| find_one | `plan_extraction_jobs` | `{"corpus_id": corpus_id}` | `{"_id": 0, "default_ingestion_config": 1}` | extraction_jobs.py:812-815 |
| find_one | `_load_backfill_config` | `{"corpus_id": corpus_id}` | — (full document) | graph_backfill.py:286 |

### `chunks`

| Operation | Function | Verbatim filter | Projection | File:line |
|-----------|----------|-----------------|------------|-----------|
| find | `_extraction_identity_context` | `{"doc_id": doc_id, "corpus_id": corpus_id, "chunk_id": {"$in": clean_chunk_ids}}` | `{"_id": 0, "chunk_id": 1, "parent_id": 1, "text": 1, "text_hash": 1, "chunk_hash": 1, "chunk_version": 1, "updated_at": 1}` | extraction_jobs.py:401-417 |
| find | `_extract_tasks` | `{"doc_id": doc_id, "corpus_id": corpus_id}` with optional `"chunk_id": {"$in": chunk_ids}` | `{"chunk_id": 1, "text": 1, "chunk_kind": 1, "_id": 0}` | graph_backfill.py:307-310 |
| find | `plan_extraction_jobs` (error chunk scan) | `{"corpus_id": corpus_id, "chunk_id": {"$in": sorted(error_by_chunk)}}` | `chunk_projection` | extraction_jobs.py:869-872 |
| find | `plan_extraction_jobs` (keyset scan) | `{"corpus_id": corpus_id, "doc_id": {"$in": scan_doc_ids}, "chunk_id": {"$nin": sorted(seen_chunk_ids), "$gt": last_chunk_id}}` (chunk_id filter only if populated) | `chunk_projection` | extraction_jobs.py:906-920 |

### `ghost_b_extractions`

| Operation | Function | Verbatim filter | Verbatim update / projection | File:line |
|-----------|----------|-----------------|------------------------------|-----------|
| find | `reconcile_terminal_extraction_jobs` | `{"corpus_id": corpus_id, "chunk_id": {"$in": chunk_ids}, "status": {"$in": ["ok", "skipped"]}}` | `{"_id": 0, "chunk_id": 1, "extraction_contract_hash": 1, "promoted_at": 1, "raw_output_artifact_id": 1, "raw_output_fingerprint": 1, "prompt_hash": 1, "prompt_chars": 1, "status": 1, "skip_reason": 1, "reason": 1}` | extraction_jobs.py:697-716 |
| find | `plan_extraction_jobs` (error rows) | `{"corpus_id": corpus_id, "doc_id": {"$in": sorted(docs_by_id)}, "status": {"$in": ["error", "stale_chunk_reference"]}}` | `{"_id": 0}`; limit | extraction_jobs.py:859-866 |
| find | `plan_extraction_jobs` (row scan) | `{"corpus_id": corpus_id, "chunk_id": {"$in": chunk_ids}}` | `{"_id": 0}` | extraction_jobs.py:930-933 |
| bulk_write (ReplaceOne) | `_persist_extraction_rows` | `{"doc_id": doc_id, "corpus_id": corpus_id, "chunk_id": row["chunk_id"]}` | full row; `upsert=True` | extraction_jobs.py:1058-1064 |
| find | `_persist_skipped_extraction_rows` | `{"doc_id": doc_id, "corpus_id": corpus_id, "chunk_id": {"$in": clean_ids}}` | `{"_id": 0}` | extraction_jobs.py:1087-1094 |
| bulk_write (ReplaceOne) | `_persist_skipped_extraction_rows` | `{"doc_id": doc_id, "corpus_id": corpus_id, "chunk_id": chunk_id}` | full row; `upsert=True` | extraction_jobs.py:1127-1137 |
| count_documents | `_refresh_document_extraction_counts` | `{"doc_id": doc_id, "corpus_id": corpus_id, "status": "ok"}` | — | extraction_jobs.py:1153 |
| count_documents | `_refresh_document_extraction_counts` | `{"doc_id": doc_id, "corpus_id": corpus_id, "status": "error"}` | — | extraction_jobs.py:1156 |
| find | `_refresh_document_extraction_counts` | `{"doc_id": doc_id, "corpus_id": corpus_id, "status": "error"}` | `{"_id": 0}`; sort `updated_at` desc, limit 20 | extraction_jobs.py:1159-1162 |

### `control_plane_state`

| Operation | Function | Verbatim filter | Verbatim update | File:line |
|-----------|----------|-----------------|-----------------|-----------|
| update_one | `reconcile_corpus` | `{"corpus_id": corpus_id}` | `$set {corpus_id, last_cycle_at, last_receipt}`; `upsert=True` | reconciler.py:588-600 |

### `control_plane_runs` / ledger collections

| Operation | Function | Verbatim filter | Projection | File:line |
|-----------|----------|-----------------|------------|-----------|
| find | `reconcile_corpus` | `{"corpus_id": corpus_id, "status": {"$in": ["intake","reconciling","degraded"]}}` | `{"_id": 0, "run_id": 1, "doc_id": 1, "status": 1}`; sort `updated_at` asc, limit | reconciler.py:368-371 |

NOT EXAMINED: exact ledger collection names and the body of `ledger.reconcile_runs_for_corpus`, `ledger.sweep_outbox`, `ledger.update_run_from_proof`, `ledger.mark_outbox_consumed`.

---

## 4. Caller map

| Caller | Function called | Literal argument values | File:line |
|--------|-----------------|-------------------------|-----------|
| `ingestion_service.py run_extraction_jobs` | `self._backpressure_pause_result` | `corpus_id=corpus_id`, `lane_key="extraction_backfill_allowed"`, `operation="extraction_jobs.run"` | ingestion_service.py:2778-2782 |
| `ingestion_service.py run_extraction_jobs` | `self._run_owned_repair_lane` | `corpus_id=corpus_id`, `lane="extraction"`, `operation="extraction_jobs.run"`, `runner=_execute_extraction_lane` | ingestion_service.py:2797-2802 |
| `ingestion_service.py run_extraction_jobs` (closure) | `services.ingestion.extraction_jobs.run_extraction_jobs` | `self._db`, `qdrant_client=self._qdrant`, `corpus_id=corpus_id`, `user_id=user_id`, `limit=limit`, `statuses=statuses` | ingestion_service.py:2788-2795 |
| `ingestion_service.py run_corpus_commander_cycle` closure | `self.run_extraction_jobs` | `corpus_id=corpus_id`, `user_id=user_id`, `limit=limit` | ingestion_service.py:3326-3330 |
| `ingestion_service.py run_bounded_corpus_repair_cycle` (via asyncio.gather) | `self.run_extraction_jobs` | `corpus_id=corpus_id`, `user_id=user_id`, `limit=extraction_run_limit` | ingestion_service.py:3783-3788 |
| `corpus_repair.py run_bounded_corpus_repair_cycle` | `ingestion_service.run_extraction_jobs` | `corpus_id=corpus_id`, `user_id=user_id`, `limit=extraction_job_run_limit` | corpus_repair.py:782-786 |
| `corpus_repair.py run_bounded_corpus_repair_cycle` (no service) | `services.ingestion.extraction_jobs.run_extraction_jobs` | `db`, `qdrant_client=qdrant_client`, `corpus_id=corpus_id`, `user_id=user_id`, `limit=extraction_job_run_limit` | corpus_repair.py:788-794 |
| `control_plane/reconciler.py reconcile_corpus` | `ingestion_service.run_bounded_corpus_repair_cycle` | `corpus_id=corpus_id`, `user_id=user_id`, `apply=True`, all plan flags `False`, `run_extraction_jobs=lane_flags["run_extraction_jobs"]`, `extraction_job_run_limit=int(getattr(settings, "INGEST_AUTO_REPAIR_EXTRACTION_RUN_LIMIT", 100) or 100)` | reconciler.py:510-541 |
| `routers/ingestion.py run_extraction_jobs` endpoint | `ingestion_service.run_extraction_jobs` | `corpus_id=corpus_id`, `user_id=user_id`, `limit=limit`, `statuses=statuses` | routers/ingestion.py:1914-1924 |
| `extraction_jobs.py run_extraction_jobs` | `reclaim_expired_running_jobs` | `db`, `collection_name="extraction_jobs"`, `corpus_id=corpus_id`, `user_id=user_id`, `now=now` | extraction_jobs.py:1259-1265 |
| `extraction_jobs.py run_extraction_jobs` | `reconcile_terminal_extraction_jobs` | `db`, `corpus_id=corpus_id`, `user_id=user_id`, `limit=max(1000, limit * 10)` | extraction_jobs.py:1266-1271 |
| `extraction_jobs.py run_extraction_jobs` | `claim_runnable_jobs` | `db`, `collection_name="extraction_jobs"`, `jobs=jobs`, `runnable_statuses=allowed_statuses`, `now=now`, `runner="extraction_jobs.run"`, `increment_attempt=False` | extraction_jobs.py:1300-1308 |
| `extraction_jobs.py run_extraction_jobs` | `_run_doc_jobs` closure | per doc group | extraction_jobs.py:1545-1550 |
| `extraction_jobs.py _run_doc_jobs` | `_extract_tasks` | `db=db`, `corpus_id=corpus_id`, `doc_id=doc_id`, `chunk_ids=chunk_ids` | extraction_jobs.py:1401-1406 |
| `extraction_jobs.py _run_doc_jobs` | `_load_backfill_config` | `db=db`, `corpus_id=corpus_id`, `doc=doc` | extraction_jobs.py:1425-1429 |
| `extraction_jobs.py _run_doc_jobs` | `_run_ghost_b_backfill` | `db=db`, `qdrant_client=qdrant_client`, `corpus_id=corpus_id`, `tasks=tasks`, `config=config` | extraction_jobs.py:1430-1436 |
| `extraction_jobs.py _run_doc_jobs` | `_persist_extraction_rows` | `db`, `doc_id=doc_id`, `corpus_id=corpus_id`, `results=list(report.results)`, `failures=list(report.failures)` | extraction_jobs.py:1437-1443 |
| `extraction_jobs.py _run_doc_jobs` | `_persist_skipped_extraction_rows` | `db`, `doc_id=doc_id`, `corpus_id=corpus_id`, `chunk_ids=sorted(skipped_ids)`, `reason="no_extractable_text_or_skipped_kind"` | extraction_jobs.py:1416-1423 |
| `extraction_jobs.py _run_doc_jobs` | `_refresh_document_extraction_counts` | `db`, `doc_id=doc_id`, `corpus_id=corpus_id` | extraction_jobs.py:1491-1495 |
| `extraction_jobs.py _run_doc_jobs` | `_mark_jobs` | `db`, `updates=updates`, `claimed_jobs=doc_jobs` | extraction_jobs.py:1355, 1390, 1496, 1525 |

---

## 5. Footer

### Lines read per file vs total length

| File | Lines read | Total file length |
|------|------------|-------------------|
| `backend/services/ingestion/extraction_jobs.py` | 1-1595 (full) | 1595 |
| `backend/services/ingestion/job_leases.py` | 1-523 (full) | 523 |
| `backend/services/ingestion_service.py` | 1380-1499, 2755-2824, 3300-3359, 3760-3819 | 4865 |
| `backend/services/control_plane/reconciler.py` | 1-747 (full) | 747 |
| `backend/services/ingestion/graph_backfill.py` | 224-412 | 889 |
| `backend/services/ingestion/section_classifier.py` | 88-107, 207-226 | 552 |
| `backend/services/ingestion/corpus_repair.py` | 282-371, 745-829 | 1007 |
| `backend/services/ingestion_service.py build_effective_config` | 166-201 | (same file) |
| `backend/db/queue_integrity.py` | 266-295 | 295 |
| `backend/routers/ingestion.py` (call site only) | 1914-1924 | 3432 |
| `backend/services/extraction/graphify_pipeline.py` | 443-542 | 968 |

---

## Part 2 — tick-to-execution chain

Scope: the full call chain from worker tick to extraction execution when the automatic control-plane path is active.

### 1. `backend/main.py` worker tick functions

#### `_ingest_worker_poll_loop()` (main.py:415)
- **Signature**: `async def _ingest_worker_poll_loop() -> None` (main.py:415)
- **Intervals**:
  - `interval = float(getattr(settings, "INGEST_RUNNER_POLL_SECONDS", 10.0) or 10.0)` (main.py:416)
  - `repair_interval = float(getattr(settings, "INGEST_AUTO_REPAIR_POLL_SECONDS", 300.0) or 300.0)` (main.py:417-419)
  - `purge_reclaim_interval = float(getattr(settings, "CORPUS_PURGE_RECLAIM_POLL_SECONDS", 60.0) or 60.0)` (main.py:420-422)
- Loop: `while True:` (main.py:425)
  - `await asyncio.sleep(interval)` (main.py:426)
  - `await _recover_ingest_batches("poll")` (main.py:428)
  - `now = asyncio.get_running_loop().time()` (main.py:433)
  - Purge reclaim: `if now - last_purge_reclaim_tick >= purge_reclaim_interval:` (main.py:434)
  - Auto repair gate: `if bool(getattr(settings, "INGEST_AUTO_REPAIR_ENABLED", True)):` (main.py:437)
    - Tick gate: `if now - last_repair_tick >= repair_interval:` (main.py:438)
      - `last_repair_tick = now` (main.py:439)
      - Spawns background task: `repair_task = asyncio.create_task(_run_auto_corpus_repair_tick("poll"))` (main.py:449-451)
      - Tracks in `background_repair_tasks` set (main.py:452-453)
  - Exceptions swallowed with warning (main.py:456-457)

#### `_run_auto_corpus_repair_tick(reason: str)` (main.py:386)
- **Signature**: `async def _run_auto_corpus_repair_tick(reason: str) -> None` (main.py:386)
- Lock gate: `if auto_repair_lock.locked():` logs skip and returns (main.py:387-389)
- Acquires lock: `async with auto_repair_lock:` (main.py:391)
- V2 gate: `if bool(getattr(settings, "CONTROL_PLANE_V2_ENABLED", True)):` (main.py:392)
  - Imports `from services.control_plane.reconciler import run_reconcile_tick` (main.py:396)
  - Calls: `result = await run_reconcile_tick(conversation_service._db, ingestion_service=ingestion_service)` (main.py:398-401)
- Else branch (V1): `result = await ingestion_service.run_auto_corpus_repair_tick()` (main.py:403)
- Logs: `"Auto corpus repair %s tick: scanned=%d changed=%d"` (main.py:404-409)
- Exceptions: re-raises `CancelledError`; other exceptions logged as warning (main.py:410-413)

### 2. `backend/services/control_plane/reconciler.py`

#### `run_reconcile_tick(db, *, ingestion_service, corpus_limit=None, _corpus_id=None)` (reconciler.py:637)
- **Signature**: `async def run_reconcile_tick(db: Any, *, ingestion_service: Any, corpus_limit: int | None = None, _corpus_id: str | None = None) -> dict[str, Any]` (reconciler.py:637)
- `settings = get_settings()` (reconciler.py:650)
- Corpus limit: `limit = max(1, min(int(corpus_limit or getattr(settings, "INGEST_AUTO_REPAIR_CORPUS_LIMIT", 5) or 5), 100))` (reconciler.py:651)
- Corpus filter:
  ```json
  corpus_filter: dict[str, Any] = {}
  if _corpus_id:
      corpus_filter["corpus_id"] = _corpus_id
  ```
  (reconciler.py:652-654)
- **Corpus selection query**:
  ```json
  await db["corpora"].find(
      with_active_records(corpus_filter),
      {"_id": 0, "corpus_id": 1, "user_id": 1, "updated_at": 1},
  ).sort("updated_at", -1).limit(limit).to_list(length=limit)
  ```
  (reconciler.py:655-664)
- Recheck seconds: `recheck_seconds = float(getattr(settings, "CONTROL_PLANE_V2_RECHECK_SECONDS", 21_600.0) or 21_600.0)` (reconciler.py:665-667)
- Loop over corpora (reconciler.py:670):
  - `corpus_id = str(corpus.get("corpus_id") or "")` (reconciler.py:671)
  - `user_id = str(corpus.get("user_id") or "")` (reconciler.py:672)
  - `if not corpus_id: continue` (reconciler.py:673-674)
  - `scanned += 1` (reconciler.py:675)
  - `actionable = await corpus_has_actionable_work(db, corpus_id=corpus_id)` (reconciler.py:676)
  - **Not actionable path**: `if not actionable:` (reconciler.py:677)
    - Reads `control_plane_state`: `await db[STATE_COLLECTION].find_one({"corpus_id": corpus_id}, {"_id": 0, "last_full_recheck_at": 1})` (reconciler.py:678-680)
    - `recheck_due = (last_recheck is None or (_utcnow() - _as_utc(last_recheck)).total_seconds() >= recheck_seconds)` (reconciler.py:682-686)
    - `if not recheck_due:` appends `{"corpus_id": corpus_id, "status": "idle_all_certified"}` and `continue` (reconciler.py:687-694)
    - Periodic recheck: updates `control_plane_runs` status from `RUN_STATUS_QUERY_READY` to `RUN_STATUS_RECONCILING` (reconciler.py:698-704)
    - Updates `control_plane_state.last_full_recheck_at` (reconciler.py:705-709)
  - Calls `reconcile_corpus(db, ingestion_service=ingestion_service, corpus_id=corpus_id, user_id=user_id)` (reconciler.py:711-718)
  - Exceptions caught and appended as failed receipt (reconciler.py:719-729)
- Computes `changed = sum(1 for r in receipts if r.get("status") == "reconciled" and (r.get("jobs_planned") or r.get("certificates_issued")))` (reconciler.py:730-735)
- Returns `{"status": "ok", "scanned": scanned, "changed": changed, "corpora": receipts}` (reconciler.py:736-741)

#### `corpus_has_actionable_work(db, *, corpus_id)` (reconciler.py:604)
- **Signature**: `async def corpus_has_actionable_work(db: Any, *, corpus_id: str) -> bool`
- Returns `True` if outbox has unconsumed rows:
  ```json
  await db[ledger.OUTBOX_COLLECTION].count_documents(
      {"corpus_id": corpus_id, "consumed_at": None}, limit=1
  )
  ```
  (reconciler.py:611-614)
- Returns `True` if pending runs exist:
  ```json
  await db[ledger.RUNS_COLLECTION].count_documents(
      {"corpus_id": corpus_id, "status": {"$in": list(_PENDING_RUN_STATUSES)}},
      limit=1,
  )
  ```
  (reconciler.py:615-619)
- Else compares doc count vs run count:
  - `run_rows = await db[ledger.RUNS_COLLECTION].count_documents({"corpus_id": corpus_id})` (reconciler.py:621-623)
  - `doc_rows = await db["documents"].count_documents(with_active_records({"corpus_id": corpus_id, "ingest_stage": {"$nin": sorted(EXCLUDED_DOCUMENT_STAGES)}}))` (reconciler.py:626-633)
  - Returns `doc_rows > run_rows` (reconciler.py:634)

#### `reconcile_corpus(db, *, ingestion_service, corpus_id, user_id, doc_census_limit=None, execute=True)` (reconciler.py:342)
- **Signature**: `async def reconcile_corpus(db: Any, *, ingestion_service: Any, corpus_id: str, user_id: str, doc_census_limit: int | None = None, execute: bool = True) -> dict[str, Any]` (reconciler.py:342)
- `settings = get_settings()` (reconciler.py:353)
- `qdrant_client = getattr(ingestion_service, "_qdrant", None)` (reconciler.py:354)
- `doc_census_limit = int(doc_census_limit or getattr(settings, "CONTROL_PLANE_V2_DOC_CENSUS_LIMIT", 100) or 100)` (reconciler.py:355-359)
- Step 1: `backfill = await ledger.reconcile_runs_for_corpus(db, corpus_id=corpus_id)` (reconciler.py:362)
- Step 2: `outbox_rows = await ledger.sweep_outbox(db, corpus_id=corpus_id)` (reconciler.py:363)
- Step 3: read pending runs:
  ```json
  await db[ledger.RUNS_COLLECTION].find(
      {"corpus_id": corpus_id, "status": {"$in": list(_PENDING_RUN_STATUSES)}},
      {"_id": 0, "run_id": 1, "doc_id": 1, "status": 1},
  ).sort("updated_at", 1).limit(doc_census_limit).to_list(length=None)
  ```
  (reconciler.py:368-371)
- Builds `doc_ids` from outbox first then pending runs up to `doc_census_limit` (reconciler.py:372-383)
- Reads `corpora`: `await db["corpora"].find_one({"corpus_id": corpus_id}, {"_id": 0, "corpus_id": 1, "user_id": 1, "default_ingestion_config": 1})` (reconciler.py:385-388)
- Step 4: census loop (reconciler.py:397-469)
  - For each `doc_id`: `census = await collect_doc_artifact_census(db, qdrant_client, corpus_id=corpus_id, doc_id=doc_id, corpus=corpus)` (reconciler.py:398-404)
  - Updates `gap_totals` for source parse, document pipeline, extraction, summary, graph promotion (reconciler.py:421-439)
  - Builds proof and updates run via `ledger.update_run_from_proof(db, run_id=run_id, proof=proof)` (reconciler.py:441-469)
- Step 5: planning: `if gap_totals:` calls `_plan_gap_lanes(...)` (reconciler.py:472-481)
- **Step 6: execution** (reconciler.py:485-563):
  - Condition: `if execute and (gap_totals or outbox_rows or pending_runs):` (reconciler.py:486)
  - `lane_flags = _lane_run_flags(settings)` (reconciler.py:487)
    - Default V2: all `True` (reconciler.py:311-319)
    - Legacy (`CONTROL_PLANE_V2_RUN_ALL_LANES=false`): uses `INGEST_AUTO_REPAIR_RUN_EXTRACTION` etc. (reconciler.py:320-339)
  - **Inline batch gate**:
    - Tries count:
      ```json
      pending_inline = await db["ingest_batch_items"].count_documents({
          "corpus_id": corpus_id,
          "status": {"$in": ["queued", "staged", "running", "failed_recoverable"]},
      })
      ```
      (reconciler.py:497-502)
    - On exception: `pending_inline = 1` (reconciler.py:503-504)
    - `if pending_inline:` sets `lane_flags["run_extraction_jobs"] = lane_flags["run_summary_jobs"] = lane_flags["run_graph_jobs"] = False` (reconciler.py:505-507)
  - Calls `ingestion_service.run_bounded_corpus_repair_cycle(...)` with literal arguments (reconciler.py:510-541):
    ```python
    execution_receipt = await ingestion_service.run_bounded_corpus_repair_cycle(
        corpus_id=corpus_id,
        user_id=user_id,
        apply=True,
        plan_source_parse_jobs=False,
        plan_document_pipeline_jobs=False,
        plan_extraction_jobs=False,
        plan_summary_jobs=False,
        plan_graph_jobs=False,
        run_source_parse_jobs=lane_flags["run_source_parse_jobs"],
        source_parse_job_run_limit=int(
            getattr(settings, "INGEST_AUTO_REPAIR_SOURCE_PARSE_RUN_LIMIT", 25)
        ),
        run_document_pipeline_jobs=lane_flags["run_document_pipeline_jobs"],
        document_pipeline_job_run_limit=int(
            getattr(settings, "INGEST_AUTO_REPAIR_DOCUMENT_RUN_LIMIT", 25)
        ),
        run_extraction_jobs=lane_flags["run_extraction_jobs"],
        extraction_job_run_limit=int(
            getattr(settings, "INGEST_AUTO_REPAIR_EXTRACTION_RUN_LIMIT", 100)
        ),
        run_summary_jobs=lane_flags["run_summary_jobs"],
        summary_job_run_limit=int(
            getattr(settings, "INGEST_AUTO_REPAIR_SUMMARY_RUN_LIMIT", 100)
        ),
        run_document_summaries=lane_flags["run_document_summaries"],
        run_graph_jobs=lane_flags["run_graph_jobs"],
        graph_run_limit=int(
            getattr(settings, "INGEST_AUTO_REPAIR_GRAPH_RUN_LIMIT", 5)
        ),
    )
    ```
  - Records stage attempt (reconciler.py:542-549)
  - Exceptions caught and logged (reconciler.py:550-563)
- Builds receipt and updates `control_plane_state` (reconciler.py:575-600)
- Returns receipt (reconciler.py:601)

### 3. `backend/services/ingestion_service.py`

#### `run_bounded_corpus_repair_cycle(self, ...)` (ingestion_service.py:3388)
- **Signature**: `async def run_bounded_corpus_repair_cycle(self, *, corpus_id: str, user_id: str, apply: bool = False, reconcile_failures: bool = True, failure_reconcile_limit: int = 5000, backfill_promoted_extraction_marks_rows: bool = True, promoted_extraction_marks_backfill_limit: int = 100, backfill_source_parse_stage_identity_rows: bool = True, source_parse_stage_identity_backfill_limit: int = 1000, backfill_ghost_b_stage_identity_rows: bool = True, ghost_b_stage_identity_backfill_limit: int = 1000, plan_source_parse_jobs: bool = True, source_parse_job_plan_limit: int = 500, run_source_parse_jobs: bool = False, source_parse_job_run_limit: int = 25, plan_document_pipeline_jobs: bool = True, document_pipeline_job_plan_limit: int = 500, run_document_pipeline_jobs: bool = False, document_pipeline_job_run_limit: int = 25, plan_graph_jobs: bool = True, graph_plan_limit: int = 100, graph_max_chunks: int | None = None, plan_extraction_jobs: bool = True, extraction_job_plan_limit: int = 500, run_extraction_jobs: bool = False, extraction_job_run_limit: int = 25, plan_summary_jobs: bool = True, summary_job_plan_limit: int = 500, backfill_summary_stage_identity_rows: bool = True, summary_stage_identity_backfill_limit: int = 1000, run_summary_jobs: bool = False, summary_job_run_limit: int = 25, run_document_summaries: bool = False, document_summary_limit: int = 10, run_graph_jobs: bool = False, graph_run_limit: int = 3, record_run: bool = True, summary_cost_run_id: str | None = None, summary_cost_authority_usd: Any | None = None) -> dict` (ingestion_service.py:3388-3430)
- Imports `from services.ingestion.corpus_repair import run_bounded_corpus_repair_cycle` (ingestion_service.py:3431)
- Passes through every argument, mapping:
  - `source_parse_start_runners=bool(get_settings().INGEST_RUNNERS_ENABLED)` (ingestion_service.py:3453)
  - `plan_source_parse_job_rows=plan_source_parse_jobs`, `run_source_parse_job_rows=run_source_parse_jobs` (ingestion_service.py:3449-3452)
  - `plan_document_pipeline_job_rows=plan_document_pipeline_jobs`, `run_document_pipeline_job_rows=run_document_pipeline_jobs` (ingestion_service.py:3454-3457)
  - `plan_extraction_job_rows=plan_extraction_jobs`, `run_extraction_job_rows=run_extraction_jobs`, `extraction_job_run_limit=extraction_job_run_limit` (ingestion_service.py:3461-3464)
  - `plan_summary_job_rows=plan_summary_jobs`, `run_summary_job_rows=run_summary_jobs` (ingestion_service.py:3465-3470)
  - all other params forwarded literally.

#### `_backpressure_pause_result(self, *, corpus_id, lane_key, operation, readiness=None)` (ingestion_service.py:1395)
- **Signature**: `async def _backpressure_pause_result(self, *, corpus_id: str, lane_key: str, operation: str, readiness: dict | None = None) -> dict | None` (ingestion_service.py:1395)
- Computes readiness: `readiness = readiness or await self._compute_corpus_readiness_safely(corpus_id)` (ingestion_service.py:1403)
- `pressure = (readiness or {}).get("pressure") or {}` (ingestion_service.py:1404)
- `backpressure = pressure.get("backpressure") or {}` (ingestion_service.py:1405)
- Gate: `if backpressure.get(lane_key) is not False: return None` (ingestion_service.py:1406-1407)
- If paused, returns dict: `{"corpus_id": corpus_id, "status": "paused_pressure", "operation": operation, "reason": f"{lane_key}=false", "pressure": pressure, "readiness": readiness}` (ingestion_service.py:1408-1415)
- **Used by `run_extraction_jobs`** with `lane_key="extraction_backfill_allowed"` and `operation="extraction_jobs.run"` (ingestion_service.py:2778-2782)

#### `_run_owned_repair_lane(self, *, corpus_id, lane, operation, runner)` (ingestion_service.py:1417)
- **Signature**: `async def _run_owned_repair_lane(self, *, corpus_id: str, lane: str, operation: str, runner: Any) -> dict[str, Any]` (ingestion_service.py:1417)
- Imports `from services.ingestion.job_leases import corpus_lane_lease` (ingestion_service.py:1427)
- Owner string: `owner = f"{operation}:{uuid.uuid4().hex}"` (ingestion_service.py:1429)
- Enters `corpus_lane_lease(self._db, corpus_id=corpus_id, lane=lane, owner=owner)` (ingestion_service.py:1430-1435)
- Busy gate: `if not lease: return {"status": "lease_busy", "corpus_id": corpus_id, "lane": lane, "operation": operation, "claimed": 0, "counts": {}}` (ingestion_service.py:1436-1444)
- Calls `runner()`; awaits if coroutine; returns result (ingestion_service.py:1445-1448)

#### `run_extraction_jobs(self, *, corpus_id, user_id, limit=25, statuses=None)` (ingestion_service.py:2768)
- **Signature**: `async def run_extraction_jobs(self, *, corpus_id: str, user_id: str, limit: int = 25, statuses: list[str] | None = None) -> dict` (ingestion_service.py:2768)
- Imports `from services.ingestion.extraction_jobs import run_extraction_jobs` (ingestion_service.py:2776)
- Backpressure: `paused = await self._backpressure_pause_result(corpus_id=corpus_id, lane_key="extraction_backfill_allowed", operation="extraction_jobs.run")` (ingestion_service.py:2778-2782)
- If paused: `paused.update({"claimed": 0, "counts": {}}); return paused` (ingestion_service.py:2783-2785)
- Defines closure:
  ```python
  async def _execute_extraction_lane() -> dict:
      return await run_extraction_jobs(
          self._db,
          qdrant_client=self._qdrant,
          corpus_id=corpus_id,
          user_id=user_id,
          limit=limit,
          statuses=statuses,
      )
  ```
  (ingestion_service.py:2787-2795)
- Calls `_run_owned_repair_lane(corpus_id=corpus_id, lane="extraction", operation="extraction_jobs.run", runner=_execute_extraction_lane)` (ingestion_service.py:2797-2802)
- Materializes readiness and attaches if not None (ingestion_service.py:2803-2806)
- Returns result (ingestion_service.py:2806)

### 4. Chain-argument table for one hypothetical tick

The following table lists the literal argument values that reach `ingestion_service.run_extraction_jobs(...)` (and the lower `services.ingestion.extraction_jobs.run_extraction_jobs(...)`) through the automatic control-plane tick path.

| Hop | Function | File:line | Relevant argument literal or derivation |
|-----|----------|-----------|------------------------------------------|
| 1 | `_ingest_worker_poll_loop` | main.py:437 | `INGEST_AUTO_REPAIR_ENABLED` must be true to reach repair spawn |
| 2 | `_ingest_worker_poll_loop` | main.py:438-451 | Repair fires every `INGEST_AUTO_REPAIR_POLL_SECONDS` (default 300) as background task `_run_auto_corpus_repair_tick("poll")` |
| 3 | `_run_auto_corpus_repair_tick` | main.py:392-401 | If `CONTROL_PLANE_V2_ENABLED` (default True) calls `run_reconcile_tick(conversation_service._db, ingestion_service=ingestion_service)` |
| 4 | `run_reconcile_tick` | reconciler.py:651 | `limit = max(1, min(int(corpus_limit or INGEST_AUTO_REPAIR_CORPUS_LIMIT or 5), 100))` |
| 5 | `run_reconcile_tick` | reconciler.py:655-664 | Selects up to `limit` corpora sorted by `updated_at` desc, projection `{"corpus_id":1,"user_id":1,"updated_at":1}` |
| 6 | `run_reconcile_tick` | reconciler.py:676-677 | Calls `corpus_has_actionable_work`; if false and recheck not due, returns `idle_all_certified` |
| 7 | `run_reconcile_tick` | reconciler.py:711-718 | Calls `reconcile_corpus(db, ingestion_service=ingestion_service, corpus_id=corpus_id, user_id=user_id)` |
| 8 | `reconcile_corpus` | reconciler.py:486 | Execution runs only if `execute and (gap_totals or outbox_rows or pending_runs)` |
| 9 | `reconcile_corpus` | reconciler.py:497-507 | If any `ingest_batch_items` with status in `["queued","staged","running","failed_recoverable"]` exist for corpus, sets `lane_flags["run_extraction_jobs"]=False` |
| 10 | `reconcile_corpus` | reconciler.py:510-541 | Calls `ingestion_service.run_bounded_corpus_repair_cycle(corpus_id=corpus_id, user_id=user_id, apply=True, plan_*=False, run_extraction_jobs=lane_flags["run_extraction_jobs"], extraction_job_run_limit=int(getattr(settings,"INGEST_AUTO_REPAIR_EXTRACTION_RUN_LIMIT",100) or 100), ...)` |
| 11 | `ingestion_service.run_bounded_corpus_repair_cycle` | ingestion_service.py:3461-3464 | Forwards `run_extraction_job_rows=run_extraction_jobs` and `extraction_job_run_limit=extraction_job_run_limit` to `corpus_repair.run_bounded_corpus_repair_cycle` |
| 12 | `corpus_repair.run_bounded_corpus_repair_cycle` | corpus_repair.py:745 | Branch `if run_extraction_job_rows:` |
| 13 | `corpus_repair.run_bounded_corpus_repair_cycle` | corpus_repair.py:770-779 | Checks backpressure `extraction_backfill_allowed`; if false, returns pressure-skip step |
| 14 | `corpus_repair.run_bounded_corpus_repair_cycle` | corpus_repair.py:781-786 | Calls `ingestion_service.run_extraction_jobs(corpus_id=corpus_id, user_id=user_id, limit=extraction_job_run_limit)` (statuses defaulted to None) |
| 15 | `ingestion_service.run_extraction_jobs` | ingestion_service.py:2778-2785 | Checks `extraction_backfill_allowed`; returns `paused_pressure` if false |
| 16 | `ingestion_service.run_extraction_jobs` | ingestion_service.py:2797-2802 | Acquires lane lease `lane="extraction"`, `operation="extraction_jobs.run"` |
| 17 | `ingestion_service.run_extraction_jobs` | ingestion_service.py:2788-2795 | Calls `services.ingestion.extraction_jobs.run_extraction_jobs(self._db, qdrant_client=self._qdrant, corpus_id=corpus_id, user_id=user_id, limit=limit, statuses=statuses)` |
| 18 | `services.ingestion.extraction_jobs.run_extraction_jobs` | extraction_jobs.py:1257 | `limit = max(1, min(int(limit or 25), 500))` — note: by this point the limit has been `INGEST_AUTO_REPAIR_EXTRACTION_RUN_LIMIT` (default 100) unless overridden |
| 19 | `services.ingestion.extraction_jobs.run_extraction_jobs` | extraction_jobs.py:1235 signature | Final argument values: `db=self._db`, `qdrant_client=self._qdrant`, `corpus_id=<corpus_id>`, `user_id=<corpus user_id>`, `limit=<INGEST_AUTO_REPAIR_EXTRACTION_RUN_LIMIT or 100>`, `statuses=None` |

### 5. Additional direct/auto repair path via `run_auto_corpus_repair_tick`

The V1 path (when `CONTROL_PLANE_V2_ENABLED` is false) uses `ingestion_service.run_auto_corpus_repair_tick()` (main.py:403). That function is also used by the worker maintenance loop in some configurations. It differs from the reconciler path:

- `run_auto_corpus_repair_tick` reads corpora sorted by `updated_at` desc with its own `INGEST_AUTO_REPAIR_CORPUS_LIMIT` (ingestion_service.py:3499-3522).
- For each corpus it calls `self.run_bounded_corpus_repair_cycle(...)` with `apply=True` but explicitly sets `run_extraction_jobs=False` inside the sequential cycle (ingestion_service.py:3718-3723); it then fans out provider lanes separately:
  - `run_extraction_lane = bool(getattr(settings, "INGEST_AUTO_REPAIR_RUN_EXTRACTION", False))` (ingestion_service.py:3587-3589)
  - If true, appends `self.run_extraction_jobs(corpus_id=corpus_id, user_id=user_id, limit=extraction_run_limit)` where `extraction_run_limit = int(getattr(settings, "INGEST_AUTO_REPAIR_EXTRACTION_RUN_LIMIT", 100) or 100)` (ingestion_service.py:3608-3610, 3780-3788).
- It also has a scheduler backoff decision that may skip the corpus entirely if `decision["should_run"]` is false (ingestion_service.py:3637-3666).

In the V2 path (`CONTROL_PLANE_V2_ENABLED=true`, default), `run_auto_corpus_repair_tick` is **not** used by `_run_auto_corpus_repair_tick`; the reconciler path described in sections 1-4 is the active one.

### Part 2 NOT EXAMINED

- Bodies of `services.control_plane.ledger` functions (`reconcile_runs_for_corpus`, `sweep_outbox`, `update_run_from_proof`, `mark_outbox_consumed`).
- Body of `services.control_plane.desired_state.collect_doc_artifact_census` and the `EXCLUDED_DOCUMENT_STAGES` set.
- Bodies of `services.ingestion.readiness.compute_corpus_readiness` and the `pressure/backpressure` derivation.
- Bodies of `services.ingestion.corpus_repair.reconcile_ghost_b_failure_metadata`, `backfill_ghost_b_stage_identity`, `backfill_source_parse_stage_identity`, `backfill_summary_stage_identity`, `backfill_promoted_extraction_marks`, `_refresh_backpressure_readiness`, `_backpressure_allowed`, `_pressure_skip_step`, `plan_graph_promotion_jobs`.
- Bodies of the other provider lanes (`run_source_parse_jobs`, `run_document_pipeline_jobs`, `run_summary_jobs`, `run_graph_promotion_jobs`).
- `services.ingestion.repair_scheduler` module (`quick_repair_gap_snapshot`, `load_scheduler_state`, `backoff_decision`, `record_scheduler_outcome`).
- `backend/config.py` default values and full field lists (`FROZEN_CONFIG_FIELDS`, `MUTABLE_CONFIG_FIELDS`, all `INGEST_*` settings).

### Part 2 per-file line coverage

| File | Lines read | Total length |
|------|------------|--------------|
| `backend/main.py` | 386-469 | 684 |
| `backend/services/control_plane/reconciler.py` | 1-747 (full) | 747 |
| `backend/services/ingestion_service.py` | 1376-1449, 2768-2806, 3388-3939 | 4865 |
| `backend/services/ingestion/corpus_repair.py` | 282-829 (already read in Part 1; used for argument hop) | 1007 |

---

## Part 3 — memory and stream funnel

Scope: memory allocation and data streaming in the ingest pipeline, from parse through Neo4j write, including batch sizing, subprocess pools, backpressure guards, container caps, and per-collection write amplification.

### 1. Buffering vs streaming per pipeline phase in `worker.py`

#### Parse phase
- `docling_adapter.parse_document(...)` returns a `ParseResult` object held as `parse_result` at the caller site (worker.py calls into `docling_adapter` and `tier_chunker`; parse body NOT EXAMINED).
- The parsed result is passed whole into `_chunk_with_pool(...)` or `_chunk_with_pathological_fallback(...)`: `chunk_entry(parse_result, doc_id, corpus_id, config)` (chunk_subprocess.py:37-45) and `_chunk_via_remote(...)` pickles the entire parse result: `base64.b64encode(pickle.dumps(parse_result))` (worker.py:216).
- **Held in memory**: full `parse_result` until chunk subprocess returns. Release point: after `_chunk_with_pool` returns and caller assigns `parents`, `children`.

#### Chunk phase
- `_chunk_with_pool` dispatches `tier_chunker.chunk(parse_result, doc_id, corpus_id, config)` to a `ProcessPoolExecutor` (worker.py:162-190).
- The subprocess imports only `tier_chunker` to keep child RSS low (chunk_subprocess.py:1-9); the worker module itself is ~1GB RSS per child and is intentionally not imported by children (worker.py:150-153).
- Return value: full parent list and child list held in memory in the parent process.
- `_build_parent_dicts(parents, summaries, ...)` iterates all parents (worker.py:2080-2167).
- `_build_child_dicts(children, ...)` iterates all children (worker.py:2170-2211).
- `_checkpoint_child_chunks` writes all child dicts to Mongo in one `mongo_writer.upsert_chunks(db, child_dicts)` call (worker.py:2462-2494).

#### Summary phase (Ghost A / deterministic)
- `_run_ghosts_parallel` receives full `parents` and `children` lists (worker.py:1175-1195).
- `existing_parent_chunks = await mongo_reader.get_parent_chunks(db, doc_id, corpus_id)` — full parent rows loaded (worker.py:1208-1214).
- Deterministic path: iterates `summary_parents` (a filtered copy of `parents`) and calls `build_deterministic_parent_summary` per parent, accumulating `results: list[SummaryResult]` (worker.py:1468-1545).
- Deprecated LLM path: builds full `tasks: list[SummaryTask]` over all `summary_parents` (worker.py:1576-1589); `summarize_parents(tasks, ...)` returns a full result list.
- `_summarizable_parents(parents)` returns a new list (worker.py:569-575).
- Summary outputs are held until `_write_mongo_all` serializes them into parent dicts.

#### Extraction phase (Ghost B)
- `_run_ghosts_parallel` builds full `body_children` list (filtered copy of `children`) (worker.py:1710-1721).
- For `ghost_b_llm`: builds full `tasks: list[ExtractionTask]` over `body_children` (worker.py:1731-1748); `extract_entities(tasks, ...)` returns `ExtractionBatchReport` with `.results` and `.failures` lists held in memory.
- For `graphify_cpu`: calls `run_graphify_pipeline(...)` with `text="\n\n".join(task.text for task in tasks)` — concatenates ALL body child texts into one giant string (worker.py:1982-1989); returns `GraphifyPipelineOutput` with `.report.results` list.
- `ghost_b_staging = [asdict(r) for r in ghost_b_out]` converts all results to dicts (worker.py:2268-2270).
- `ghost_b_failure_rows = [asdict(f) for f in ghost_b_failures]` converts all failures (worker.py:2271-2273).

#### Embed phase
- `_embed_batch_for_doc` builds:
  - `vector_children = [c for c in children if _is_vectorized_child(c)]` — full filtered child list (worker.py:2725).
  - `child_texts = [c.text for c in vector_children]` — all child texts (worker.py:2734).
  - `summary_texts = [_summary_vector_text(s) for s in summary_list]` — all summary texts (worker.py:2736).
  - `all_texts = [*child_texts, *summary_texts]` — concatenated full list passed to `embed_batch` (worker.py:2737-2749).
  - `all_vectors` returned as full list of vectors for all texts (worker.py:2749).
  - `vec_map = {c.chunk_id: v for c, v in zip(vector_children, child_vecs)}` — full dense vector dict keyed by chunk (worker.py:2772).
  - `summary_vec_map = {s.parent_id: v for s, v in zip(summary_list, summary_vecs)}` — full summary vector dict keyed by parent (worker.py:2773).
- These maps are held through Qdrant write.

#### Sparse/BM25 phase
- `_build_sparse_maps()` builds:
  - `{c.chunk_id: _bm25_encode(_searchable_text(c)) for c in children if c.chunk_id in vec_map}` (worker.py:4485-4489).
  - `{s.parent_id: _bm25_encode(_summary_vector_text(s)) for s in (summaries or [])}` (worker.py:4490-4494).
- Runs on thread pool via `asyncio.to_thread(_build_sparse_maps)` (worker.py:4496-4498).

#### Qdrant write phase
- `_write_qdrant_for_doc` builds `vector_children = [c for c in children if c.chunk_id in vec_map]` (worker.py:2909).
- For each target collection (naive/hrag/graph), it builds fresh `dicts = [_as_payload(c) for c in ...]` and `vecs = [vec_map[c.chunk_id] for c in ...]` and `sparse = [...]` lists, then calls `qdrant_writer.upsert_children(...)` (worker.py:2958-2996).
- `_write_qdrant_summaries_for_doc` builds `summary_payloads` list over all summaries (worker.py:2805-2852), `summary_vecs = [summary_vec_map[summary.parent_id] for summary in summaries]` and `summary_sparse = [...]` (worker.py:2859-2862), then `qdrant_writer.upsert_summaries(...)` (worker.py:2866-2873).

#### Neo4j write phase
- `_write_neo4j_for_doc` receives `children` and `ghost_b_out` lists (worker.py:4806-4819, call site; function body NOT EXAMINED).

#### Document metadata write
- `_write_mongo_all` assembles:
  - `parent_dicts` list (worker.py:2245-2249)
  - `duplicate_candidates` list (worker.py:2253-2262)
  - `child_dicts` list (worker.py:2263-2267)
  - `ghost_b_staging` list (worker.py:2268-2270)
  - `ghost_b_failure_rows` list (worker.py:2271-2273)
  - `doc_record` dict containing the above as fields/counts (worker.py:2280-2316)
- Writes occur via `mongo_writer.upsert_document`, `upsert_parent_chunks`, `upsert_chunks`, `stash_ghost_b`, `stash_ghost_b_failures` (worker.py:2333-2348).

### 2. Batch/buffer size knobs

| Env / setting | Default | Consumption site | Notes |
|-------------|---------|------------------|-------|
| `EMBED_BATCH_SIZE` | 32 | `resource_planner.py:305`; used by `embed_batch` | Clamped to ≤16 for local/MLX, else full value |
| `INGEST_MAX_PARSE_JOBS` | 2 | `worker.py:143` `_PARSE_SEMAPHORE = asyncio.Semaphore(max(1, settings.INGEST_MAX_PARSE_JOBS))` | Process-local parse/chunk concurrency |
| `INGEST_CHUNK_PROCESSES` | 4 | `worker.py:175` | `ProcessPoolExecutor(max_workers=workers)` for chunk stage |
| `INGEST_CHUNK_TASKS_PER_CHILD` | 25 | `worker.py:182-184` | `max_tasks_per_child` for chunk process pool; recycles each child after N docs |
| `TIER_CHUNKER_PATHOLOGICAL_CHAR_THRESHOLD` | 350000 | `worker.py:326-336` | Routes doc to regex/sentence-merge fallback |
| `TIER_CHUNKER_PATHOLOGICAL_SECTION_THRESHOLD` | 5000 | `worker.py:339-352` | Section-count fallback threshold |
| `INGEST_DEDUP_SCAN_RECENT_DOCS` | 250 | `worker.py:1123` | Bounded near-duplicate scan recent-doc cap |
| `QDRANT_UPSERT_BATCH_SIZE` | 256 | `config.py:725` | Max points per Qdrant upsert request |
| `QDRANT_INGEST_WRITE_CONCURRENCY` | 2 | `worker.py:512-514`; `resource_planner.py:332`; `readiness.py:2104-2106` | `_qdrant_write_semaphore()` concurrency cap |
| `NEO4J_INGEST_WRITE_CONCURRENCY` | 1 | `worker.py:519-524`; `resource_planner.py:336`; `readiness.py:2107-2109` | `_neo4j_write_semaphore()` concurrency cap |
| `INGEST_EMBED_DOCS` | 2 | `worker.py:484-495` `_embed_phase_semaphore()` | Docs concurrently in embed phase |
| `INGEST_MAX_MODEL_PHASE_DOCS` | 1 | `config.py:1401-1413`; `_model_phase_doc_limit()` uses `ResourceProfile.model_phase_docs` | Model-phase (ghosts/embed historically) concurrency |
| `INGEST_MANAGED_VLLM_MODEL_PHASE_DOCS` | 2 | `config.py:1477-1487` | Model-phase cap when using managed vLLM/RTX lanes |
| `INGEST_GLOBAL_MAX_DOCS` | 3 | `config.py:1527-1537` | Global cap on documents in flight across all batches |
| `INGEST_MAX_ACTIVE_JOBS` | 16 | `config.py:1488-1496` | Active background ingest jobs retained in memory; over-cap requests fail 429 |
| `INGEST_PROVIDER_MICROBATCH_SIZE` | 4 | `docker-compose.offline-ingest.yml:113` | Provider microbatch size |
| `INGEST_PROVIDER_MICROBATCH_MAX_CHARS` | 60000 | `docker-compose.offline-ingest.yml:114` | Provider microbatch max chars |
| `INGEST_DEFERRED_SUMMARY_BACKFILL_BATCH` | 32 | `config.py:1471-1475` | Parent batch size for deferred summary backfill |
| `INGEST_DEFERRED_SUMMARY_BACKFILL_LIMIT` | 2000 | `config.py:1461-1469` | Max missing summaries per auto backfill run |
| `INGEST_JOB_MAX_ATTEMPTS` | 5 | `job_leases.py:24` default; `docker-compose.offline-ingest.yml:112` | Dead-letter threshold for repair jobs |
| `EXTRACTION_MAX_ACTIVE_DOCS` | 1 | `docker-compose.daily.yml:25` | Per-process extraction concurrency |
| `OFFLINE_INGEST_EMBED_BATCH_SIZE` / `EMBED_BATCH_SIZE` | 64 | `docker-compose.offline-ingest.yml:135` | Override for offline ingest worker |

### 3. Subprocess memory: OpenIE farm and chunk process pools

#### Chunk process pool (`worker.py:145-190`, `chunk_subprocess.py`)
- **Pool creation**: `_chunk_process_pool(recreate=False)` at `worker.py:162-190`.
- **Worker count**: `workers = max(1, int(getattr(get_settings(), "INGEST_CHUNK_PROCESSES", 4)))` (worker.py:175).
- **Context**: `multiprocessing.get_context("spawn")` (worker.py:178).
- **Recycling**: `max_tasks_per_child = max(1, int(getattr(get_settings(), "INGEST_CHUNK_TASKS_PER_CHILD", 25)))` (worker.py:182-184).
- **What child loads**: `chunk_subprocess.py` imports only `services.ingestion.tier_chunker` and its direct deps; it explicitly does NOT import `services.ingestion.worker` (~1GB RSS) (chunk_subprocess.py:1-9, 37-45).
- **Lifetime**: warm pool per worker process, recreated on `BrokenProcessPool`; pathological fallback uses one-shot `ProcessPoolExecutor(max_workers=1, max_tasks_per_child=1)` and immediately shuts it down (worker.py:281-314).
- **Remote chunk cars**: optional `CHUNK_REMOTE_URLS` env sends pickled parse result to remote HTTP chunk service (worker.py:197-241).

#### OpenIE farm (`services/extraction/openie_farm.py`)
- **Purpose**: warm triplet-extract worker pool for Graphify CPU OpenIE lane.
- **Worker count**: `default_worker_count()` reads `GRAPHIFY_OPENIE_WORKERS` env; if unset uses `max(1, min(4, (os.cpu_count() or 2) - 2))` (openie_farm.py:30-38).
- **Context**: `mp.get_context("spawn")` (openie_farm.py:94).
- **What child loads**: each worker imports `triplet_extract.OpenIEExtractor` once (`_WORKER_EXTRACTOR` global) and keeps it resident (openie_farm.py:59-80).
- **Extractor config**: `speed_preset="balanced"`, `deep_search=False`, `resolve_coref=False`, `preserve_latex=False` (openie_farm.py:66-72).
- **Lifetime**: warm across calls; `get_openie_farm(workers)` returns/creates a global singleton `_FARM` and only recreates if worker count changes (openie_farm.py:123-136).
- **Model size**: NOT EXAMINED (triplet_extract package size not inspected).

#### TripletExtractCPUProvider (`services/extraction/graphify_openie.py:117-172`)
- **Parent-process warm instance**: `_EXTRACTOR` global loaded once via `_default_loader()` (graphify_openie.py:105-114).
- **Inference serialized** by `_INFERENCE_LOCK` (graphify_openie.py:99-100, 161-163).
- **Farm mode**: only used when `provider.is_default_loader` and `workers > 1` and `len(eligible) > 1` (graphify_openie.py:317-321).

### 4. Backpressure / memory-aware guards

#### `services/ingestion/pressure.py`
- `build_ingestion_pressure_snapshot(...)` returns pressure/backpressure dict (pressure.py:121-276).
- **RSS guard**:
  - `rss_pressure = _ratio(backend_rss_mb, rss_soft_limit_mb)` (pressure.py:142).
  - If `rss_pressure >= 1.0`: `status = "high"`, reason `"backend_rss_over_soft_limit"`, recommendations include `"pause_nonessential_backfills"`, `"reduce_extraction_backfill_pressure"`, `"let_write_queues_drain"` (pressure.py:158-165).
  - If `rss_pressure >= 0.75`: `status = "elevated"`, reason `"backend_rss_near_soft_limit"`, recommendation `"run_bounded_repairs_only"` (pressure.py:166-169).
- **Mongo storage guard**:
  - `mongo_fs_pressure = _ratio(mongo_fs_used_bytes, mongo_fs_total_bytes)` (pressure.py:145).
  - Warn/stop ratios default 0.85/0.90; over stop sets `status = "high"` with reason `"mongo_storage_over_stop_limit"` (pressure.py:171-186).
- **Qdrant writer guard**: `_writer_pressure_snapshot(qdrant_pressure, default_latency_warn_ms=2000, default_latency_stop_ms=5000, default_queue_warn=1000, default_queue_stop=5000)` (pressure.py:194-200); `qdrant_blocked = qdrant_writer["status"] == "high"` (pressure.py:208).
- **Neo4j writer guard**: `_writer_pressure_snapshot(neo4j_pressure, default_latency_warn_ms=3000, default_latency_stop_ms=10000, default_queue_warn=500, default_queue_stop=2000)` (pressure.py:201-207); `neo4j_blocked = neo4j_writer["status"] == "high"` (pressure.py:209).
- **Backpressure flags produced** (pressure.py:264-276):
  ```json
  {
    "source_parse_allowed": not block_all,
    "document_pipeline_allowed": not block_all and not qdrant_blocked,
    "summary_generation_allowed": not block_all,
    "summary_indexing_allowed": not block_all and not qdrant_blocked,
    "summary_backfill_allowed": not block_all,
    "extraction_backfill_allowed": not block_all,
    "graph_promotion_allowed": not block_all and not neo4j_blocked
  }
  ```
  where `block_all = "backend_rss_over_soft_limit" in reasons or "mongo_storage_over_stop_limit" in reasons` (pressure.py:225-228).

#### `services/ingestion/readiness.py`
- `compute_corpus_readiness(...)` gathers pressure inputs and calls `build_ingestion_pressure_snapshot` (readiness.py:2093-2169).
- **Qdrant memory inputs**:
  - `qdrant_memory_limit_bytes = parse_memory_limit_bytes(getattr(settings, "QDRANT_MEM_LIMIT", None))` (readiness.py:2111-2113).
  - `qdrant_memory_warn_ratio = float(getattr(settings, "QDRANT_MEMORY_WARN_RATIO", 0.85))` (readiness.py:2114-2116).
  - `qdrant_memory_stop_ratio = float(getattr(settings, "QDRANT_MEMORY_STOP_RATIO", 0.90))` (readiness.py:2117-2119).
- `sample_qdrant_pressure(...)` from `services.ingestion.storage_pressure` is called with those thresholds (readiness.py:2133-2139); body NOT EXAMINED.
- **Soft limit source**: `memory_soft_limit_mb(settings)` imported from `services.ingestion.resource_planner`; uses `INGEST_BACKEND_RAM_TARGET_MB` clamped to cgroup limit (readiness.py:2103, resource_planner.py:145-158).

#### `services/ingestion/resource_planner.py`
- `memory_soft_limit_mb(settings)`:
  - `requested = int(getattr(settings, "INGEST_BACKEND_RAM_TARGET_MB", 16_384))` (resource_planner.py:145).
  - `cgroup_limit` detected from cgroup v1/v2 files (resource_planner.py:146-147).
  - `ram_cap = max(512, min(requested, cgroup_limit or requested))` (resource_planner.py:147).
  - `ratio = float(getattr(settings, "INGEST_RSS_SOFT_LIMIT_RATIO", 0.85))` (resource_planner.py:148).
  - Returns `(ram_cap, max(256, int(ram_cap * ratio)))` (resource_planner.py:150).
- `plan_ingestion_resources(...)` sets `rss_high = resources.process_rss_mb >= rss_soft_limit_mb` and reduces remote vLLM doc fanout when true (resource_planner.py:288-294).

#### `services/ingestion/corpus_repair.py`
- `_backpressure_allowed(readiness, key)` returns `backpressure.get(key) is not False` (corpus_repair.py:237-240).
- `_pressure_skip_step(name, readiness, key)` returns `{"name": name, "status": "skipped_pressure", ...}` (corpus_repair.py:243-257).
- `_refresh_backpressure_readiness(db, corpus_id, fallback)` re-reads `compute_corpus_readiness` (corpus_repair.py:260-279).
- Gated execution lanes (all use the above helpers):
  - source_parse at corpus_repair.py:441-453 (key `"source_parse_allowed"`)
  - document_pipeline at corpus_repair.py:513-528 (key `"document_pipeline_allowed"`)
  - summary at corpus_repair.py:637-652 (key `"summary_backfill_allowed"`)
  - extraction at corpus_repair.py:765-779 (key `"extraction_backfill_allowed"`)
  - graph at corpus_repair.py:843-858 (key `"graph_promotion_allowed"`)

#### What these guards do NOT gate
- They do NOT gate inline worker `parse/chunk` phases; those are bounded by `_PARSE_SEMAPHORE` and `INGEST_CHUNK_PROCESSES`.
- They do NOT gate deterministic summarization inside the inline worker; only the repair/backfill summary lanes and provider-based enrichment.
- They do NOT gate the chunk subprocess pool creation or OpenIE farm startup; those always initialize when first used.
- They do NOT gate document-level `INGEST_MAX_ACTIVE_JOBS` admission; that is a separate counter.
- They do NOT gate `INGEST_GLOBAL_MAX_DOCS` concurrency.

### 5. Container cap inventory from compose files

| Service | Compose file | mem_limit | Env default |
|---------|--------------|-----------|-------------|
| mongodb | `docker-compose.yml` | `${MONGO_MEM_LIMIT:-3g}` | 3g |
| qdrant | `docker-compose.yml` | `${QDRANT_MEM_LIMIT:-4g}` | 4g |
| neo4j | `docker-compose.yml` | `${NEO4J_MEM_LIMIT:-3g}` | 3g |
| redis | `docker-compose.yml` | `${REDIS_MEM_LIMIT:-512m}` | 512m |
| searxng | `docker-compose.yml` | `${SEARXNG_MEM_LIMIT:-512m}` | 512m |
| litellm | `docker-compose.yml` | `${LITELLM_MEM_LIMIT:-1g}` | 1g |
| autoheal | `docker-compose.yml` | `${AUTOHEAL_MEM_LIMIT:-128m}` | 128m |
| backend | `docker-compose.yml` | `${BACKEND_MEM_LIMIT:-2g}` | 2g |
| mcp | `docker-compose.yml` | `${MCP_MEM_LIMIT:-1g}` | 1g |
| frontend | `docker-compose.yml` | `${FRONTEND_MEM_LIMIT:-512m}` | 512m |
| cloudflared | `docker-compose.yml` | `${CLOUDFLARED_MEM_LIMIT:-256m}` | 256m |
| mongodb | `docker-compose.heavy-ingest.yml` | 5g | — |
| qdrant | `docker-compose.heavy-ingest.yml` | 4g | — |
| neo4j | `docker-compose.heavy-ingest.yml` | 4g | — |
| redis | `docker-compose.heavy-ingest.yml` | 1g | — |
| backend | `docker-compose.heavy-ingest.yml` | 3g | — |
| mcp | `docker-compose.heavy-ingest.yml` | 2g | — |
| mongodb | `docker-compose.daily.yml` | 3g | — |
| qdrant | `docker-compose.daily.yml` | `${QDRANT_MEM_LIMIT:-8g}` | 8g |
| neo4j | `docker-compose.daily.yml` | 3g | — |
| redis | `docker-compose.daily.yml` | 512m | — |
| backend | `docker-compose.offline-ingest.yml` | `${QUERY_BACKEND_MEM_LIMIT:-6g}` | 6g |
| ingest-worker | `docker-compose.offline-ingest.yml` | `${INGEST_WORKER_MEM_LIMIT:-20g}` | 20g |

### 6. Per-collection write amplification for one document ingest

| Collection | Write type | Batched or per-row | Site | Batch size / notes |
|------------|-----------|-------------------|------|--------------------|
| `documents` | `upsert_document` (ReplaceOne-ish) | per doc | worker.py:2333 | one document row |
| `parent_chunks` | `upsert_parent_chunks` | per doc (all parents at once) | worker.py:2334 | all parents in one bulk op |
| `chunks` | `upsert_chunks` | per doc (all children at once) | worker.py:2335, 2478 | all children in one bulk op; also checkpointed earlier |
| `ghost_b_extractions` | `stash_ghost_b` | per doc (all results at once) | worker.py:2337-2342 | all extraction results in one bulk op |
| `ghost_b_extractions` | `ReplaceOne` in `_persist_extraction_rows` | per doc | extraction_jobs.py:1058-1065 | bulk write of all result/failure rows |
| `ghost_b_error_events` | `insert_one` | per event | worker.py:734 | one row per sampled ghost_b event |
| `ingest_batch_items` | per-item state updates | per item | batches.py (NOT EXAMINED in detail) | item-level lease/phase writes |
| Qdrant children (`naive`, `hrag`, `graph`) | `upsert_children` | per doc per collection | worker.py:2962-2996 | one upsert call per target collection per doc; internal batching by `QDRANT_UPSERT_BATCH_SIZE` |
| Qdrant summaries (`naive`, `hrag`) | `upsert_summaries` | per doc | worker.py:2866-2873 | one upsert call per doc; internal batching |
| Qdrant `polymath_doc_summaries` (Tier-0) | `embed_doc_profile` | per doc | worker.py:4596-4605 | one routing-card point per doc |
| Neo4j graph | `write_document_graph` | per doc | worker.py:4806-4819 (via `_write_neo4j_for_doc`) | one document-level MERGE pass |
| `control_plane_runs` | upserts | per doc per reconcile tick | reconciler.py:698-704 | update_many for certified-run recheck |
| `control_plane_state` | update_one | per corpus per tick | reconciler.py:588-600, 705-709 | one document per corpus |

### Part 3 NOT EXAMINED

- Bodies of `services.ingestion.docling_adapter.parse_document`, `services.ingestion.tier_chunker.chunk`, `services.storage.mongo_writer.*`, `services.storage.qdrant_writer.*`, `services.graph.neo4j_writer.write_document_graph`, `services.embedder.embed_batch`, `services.storage.sparse_encoder.encode_text`, `services.ingestion.verify.verify_ingest`.
- Body of `services.ingestion.storage_pressure.sample_qdrant_pressure` and `qdrant_pressure_from_prometheus`.
- Body of `services.ingestion.resource_planner.plan_ingestion_resources` beyond the memory-soft-limit function.
- Body of `services.extraction.graphify_pipeline.run_graphify_pipeline` beyond the call sites already shown.
- Body of `services.ghost_b.extract_entities` and `services.ghost_a.summarize_parents`.
- Full content of `backend/config.py` beyond the settings read for this audit.
- Any runtime cgroup detection implementation details and actual model sizes of `triplet_extract` or `gliner2`/relex encoders.

### Part 3 per-file line coverage

| File | Lines read | Total length |
|------|------------|--------------|
| `backend/services/ingestion/worker.py` | 1-200, 280-314, 356-524, 569-607, 643-652, 986-1055, 1095-1173, 1175-2051, 2080-2494, 2700-2875, 2906-3067, 4400-5002 | 5102 |
| `backend/services/ingestion/chunk_subprocess.py` | 1-79 (full) | 79 |
| `backend/services/ingestion/summary_backfill.py` | 230-569 | 826 |
| `backend/services/ingestion/pressure.py` | 1-277 (full) | 277 |
| `backend/services/ingestion/readiness.py` | 2090-2229 | 2290 |
| `backend/services/ingestion/resource_planner.py` | 140-330 | NOT EXAMINED total length |
| `backend/services/ingestion/corpus_repair.py` | 237-279, 765-779, 843-858 | 1007 |
| `backend/services/extraction/openie_farm.py` | 1-136 (full) | 136 |
| `backend/services/extraction/graphify_openie.py` | 1-173, 260-489 | 492 |
| `backend/config.py` | 35-59, 700-769, 1080-1171, 1401-1537, 2078-2147 | 3163 |
| `docker-compose.yml` | 25-480 | 887 |
| `docker-compose.heavy-ingest.yml` | 1-37 (full) | 37 |
| `docker-compose.daily.yml` | 1-35 (full) | 35 |
| `docker-compose.offline-ingest.yml` | 1-173 (full) | 173 |

---

### NOT EXAMINED

- Bodies and caller chains of `services.ghost_b.extract_entities`, `services.extraction.graphify_pipeline.run_graphify_pipeline` beyond the call signature shown, `services.graph.neo4j_writer.write_document_graph`, `services.graph.projection_runner.project_document_via_control_plane`, `services.ingestion.worker._build_ghost_pool`, `services.ingestion.model_lifecycle.ensure_model_lifecycle_ready`, `services.ingestion.stage_identity.extraction_stage_identity` and `stage_chunk_hash`, `services.extraction_provider_cards.safe_extraction_pool_contract`, `models.release_stamp.current_release_stamp`, `services.ingestion.readiness.compute_corpus_readiness` function body beyond the pressure call site, `services.ingestion.pressure` module beyond the documented snapshot function, `services.ingestion.corpus_commander.run_corpus_commander_cycle`, `services.control_plane.desired_state.collect_doc_artifact_census`, `services.control_plane.certificate.issue_certificate_if_complete`/`load_certificate`/`build_proof`, `services.control_plane.ledger` functions, `services.storage.record_status.with_active_records`, `services.ingestion.graph_promotion_jobs`, `services.ingestion.source_parse_jobs`, `services.ingestion.document_pipeline_jobs`, `services.ingestion.summary_jobs`, `services.ingestion.organ_repair_jobs`, `_refresh_backpressure_readiness`, `_backpressure_allowed`, `_pressure_skip_step`, `_compute_corpus_readiness_safely`, `_materialize_corpus_readiness_safely`, `_refresh_corpus_counts`, full content of `backend/config.py` settings defaults and `FROZEN_CONFIG_FIELDS`/`MUTABLE_CONFIG_FIELDS`.
- Test files and router internals beyond the single call site.
- Any Neo4j/Qdrant client operations other than those explicitly shown in the called signatures.
