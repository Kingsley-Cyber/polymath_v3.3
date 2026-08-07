"""Control Plane V2 — artifact census, certificates, ledger, reconciler.

Pins the invariants the V2 refactor exists for:

  • The exact production failure: chunks in Mongo + zero Qdrant vectors +
    empty durable queues must census as a document_pipeline gap and invoke
    the planner — never "no actionable work".
  • Certificates are insert-only and are the ONLY source of query_ready=true.
  • Hermes regression: a document whose legacy flags lie (ingest_stage
    "fully_enriched", write_state verified) must still return
    query_ready=false with non-empty missing.qdrant_child_ids.
  • Observation failure means unknown — readiness_source "unavailable",
    never a legacy-flag fallback.
  • Actionability comes from the run ledger + outbox, never queue counts
    (anti-`no_actionable_gaps`).
  • Execution failures append a stage_attempts receipt with failure_class.
  • Flag-off parity: CONTROL_PLANE_V2_RUN_ALL_LANES=false honors the legacy
    INGEST_AUTO_REPAIR_RUN_* lane flags.

Everything runs against hand-rolled Mongo/Qdrant fakes (same pattern as
tests/test_extraction_jobs.py) — no live stores.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.control_plane import ledger
from services.control_plane.certificate import (
    CERTIFICATE_COLLECTION,
    build_proof,
    contract_fingerprint,
    certificate_id_for,
    doc_readiness_proof,
    issue_certificate_if_complete,
)
from services.control_plane.desired_state import (
    STAGE_DOCUMENT_PIPELINE,
    collect_doc_artifact_census,
)
from services.control_plane.reconciler import (
    _lane_run_flags,
    _plan_limit,
    corpus_has_actionable_work,
    reconcile_corpus,
)


# ─── Fake Mongo (extends the test_extraction_jobs pattern) ────────────────


def _matches(row: dict[str, Any], query: dict[str, Any] | None) -> bool:
    for key, expected in (query or {}).items():
        if key == "$and":
            if not all(_matches(row, sub) for sub in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches(row, sub) for sub in expected):
                return False
            continue
        actual = row.get(key)
        if isinstance(expected, dict) and any(
            str(k).startswith("$") for k in expected
        ):
            if "$exists" in expected and (key in row) != bool(expected["$exists"]):
                return False
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
            if "$gt" in expected and not (
                actual is not None and actual > expected["$gt"]
            ):
                return False
        elif actual != expected:
            return False
    return True


def _apply_update(row: dict[str, Any], update: dict[str, Any], *, inserting: bool) -> None:
    for key, value in (update.get("$set") or {}).items():
        row[key] = value
    if inserting:
        for key, value in (update.get("$setOnInsert") or {}).items():
            row.setdefault(key, value)
    for key in (update.get("$unset") or {}):
        row.pop(key, None)


class _Result:
    def __init__(self, modified_count: int = 0):
        self.modified_count = modified_count
        self.upserted_id = None


class _FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, *args, **_kwargs):
        if args:
            key = args[0]
            reverse = len(args) > 1 and int(args[1]) < 0
            self.rows.sort(key=lambda row: str(row.get(key) or ""), reverse=reverse)
        return self

    def limit(self, limit):
        self.rows = self.rows[: int(limit)]
        return self

    async def to_list(self, length=None):
        if length is None:
            return list(self.rows)
        return list(self.rows[: int(length)])


class _FakeCollection:
    def __init__(self, rows=None):
        self.rows = [dict(r) for r in (rows or [])]

    def _project(self, row, projection):
        if not projection:
            return dict(row)
        include_keys = [k for k, inc in projection.items() if inc]
        if include_keys:
            return {k: row.get(k) for k in include_keys if k in row}
        return {k: v for k, v in row.items() if projection.get(k, 1) != 0}

    def find(self, query=None, projection=None):
        return _FakeCursor(
            self._project(row, projection or {})
            for row in self.rows
            if _matches(row, query or {})
        )

    async def find_one(self, query=None, projection=None):
        rows = self.find(query, projection).rows
        return dict(rows[0]) if rows else None

    async def count_documents(self, query=None, limit=None):
        count = sum(1 for row in self.rows if _matches(row, query or {}))
        return min(count, int(limit)) if limit else count

    async def insert_one(self, doc):
        self.rows.append(dict(doc))
        return SimpleNamespace(inserted_id=len(self.rows))

    async def update_one(self, query, update, upsert=False):
        for idx, row in enumerate(self.rows):
            if _matches(row, query or {}):
                new_row = dict(row)
                _apply_update(new_row, update, inserting=False)
                self.rows[idx] = new_row
                return _Result(1)
        if upsert:
            row = {
                k: v for k, v in (query or {}).items() if not isinstance(v, dict)
            }
            _apply_update(row, update, inserting=True)
            self.rows.append(row)
        return _Result(0)

    async def update_many(self, query, update):
        modified = 0
        for idx, row in enumerate(self.rows):
            if _matches(row, query or {}):
                new_row = dict(row)
                _apply_update(new_row, update, inserting=False)
                self.rows[idx] = new_row
                modified += 1
        return _Result(modified)

    async def delete_many(self, query):
        kept = [row for row in self.rows if not _matches(row, query or {})]
        deleted = len(self.rows) - len(kept)
        self.rows = kept
        return SimpleNamespace(deleted_count=deleted)


class _FakeDB:
    def __init__(self, **collections):
        self.collections = {
            name: _FakeCollection(rows) for name, rows in collections.items()
        }

    def __getitem__(self, name):
        return self.collections.setdefault(name, _FakeCollection())


class _FakeQdrant:
    """Returns the same payload set for any collection ('*' bucket)."""

    def __init__(self, payloads=None, error: Exception | None = None):
        self.payloads = list(payloads or [])
        self.error = error

    async def scroll(
        self,
        collection_name=None,
        scroll_filter=None,
        limit=None,
        offset=None,
        with_payload=None,
        with_vectors=False,
    ):
        del collection_name, scroll_filter, limit, offset, with_payload, with_vectors
        if self.error is not None:
            raise self.error
        return [SimpleNamespace(payload=dict(p)) for p in self.payloads], None


# ─── Fixture data — the real stranded-document shape ──────────────────────

_NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)
_CFG = {
    "use_neo4j": False,
    "chunk_summarization": False,
    "target_qdrant_collections": ["naive"],
}


def _corpus_row() -> dict[str, Any]:
    return {
        "corpus_id": "c1",
        "user_id": "u1",
        "status": "active",
        "default_ingestion_config": dict(_CFG),
        "updated_at": _NOW,
    }


def _doc_row() -> dict[str, Any]:
    # The Hermes shape: every legacy flag claims success.
    return {
        "corpus_id": "c1",
        "doc_id": "d1",
        "user_id": "u1",
        "filename": "paper.pdf",
        "status": "active",
        "ingest_stage": "fully_enriched",
        "ingestion_config": dict(_CFG),
        "write_state": {
            "mongo_written": True,
            "qdrant_written": True,
            "verified": True,
        },
    }


def _chunk_rows() -> list[dict[str, Any]]:
    return [
        {"corpus_id": "c1", "doc_id": "d1", "chunk_id": "ch1", "chunk_kind": "body"},
        {"corpus_id": "c1", "doc_id": "d1", "chunk_id": "ch2", "chunk_kind": "body"},
    ]


def _db(**overrides) -> _FakeDB:
    base: dict[str, Any] = {
        "documents": [_doc_row()],
        "corpora": [_corpus_row()],
        "chunks": _chunk_rows(),
        "parent_chunks": [],
        "ghost_b_extractions": [],
    }
    base.update(overrides)
    return _FakeDB(**base)


def _vector_payloads() -> list[dict[str, Any]]:
    return [
        {"chunk_id": "ch1", "chunk_type": "child"},
        {"chunk_id": "ch2", "chunk_type": "child"},
    ]


# ─── Census: desired vs observed with exact IDs ───────────────────────────


@pytest.mark.asyncio
async def test_census_reports_exact_missing_vector_ids_for_stranded_doc():
    """The production failure: Mongo chunks exist, Qdrant is empty, the
    legacy flags all say done. The census must name the missing IDs."""
    census = await collect_doc_artifact_census(
        _db(), _FakeQdrant([]), corpus_id="c1", doc_id="d1"
    )

    assert census["complete"] is False
    assert census["missing"]["qdrant_child_ids"] == ["ch1", "ch2"]
    assert census["gap_stages"] == [STAGE_DOCUMENT_PIPELINE]
    assert census["observation_errors"] == []


@pytest.mark.asyncio
async def test_census_all_clear_when_vectors_present():
    census = await collect_doc_artifact_census(
        _db(), _FakeQdrant(_vector_payloads()), corpus_id="c1", doc_id="d1"
    )

    assert census["complete"] is True
    assert census["gap_stages"] == []
    assert census["missing"]["qdrant_child_ids"] == []


@pytest.mark.asyncio
async def test_census_observation_failure_is_never_complete():
    census = await collect_doc_artifact_census(
        _db(),
        _FakeQdrant(error=RuntimeError("qdrant down")),
        corpus_id="c1",
        doc_id="d1",
    )

    assert census["complete"] is False
    assert census["observation_errors"]


@pytest.mark.asyncio
async def test_census_excluded_stages_have_no_desired_artifacts():
    doc = _doc_row()
    doc["ingest_stage"] = "skipped_duplicate"
    census = await collect_doc_artifact_census(
        _db(documents=[doc]), _FakeQdrant([]), corpus_id="c1", doc_id="d1"
    )

    assert census["excluded"] is True
    assert census["complete"] is True
    assert census["gap_stages"] == []


@pytest.mark.asyncio
async def test_census_ignores_soft_delete_tombstones():
    """doc_id is content-derived: delete → re-ingest resurrects the document
    over lingering chunk/parent tombstones. The census must scope reads with
    with_active_records() exactly like the planners, or it demands vectors
    and summaries no planner will ever create — an unfixable gap."""
    chunks = _chunk_rows() + [
        {
            "corpus_id": "c1",
            "doc_id": "d1",
            "chunk_id": "ch_dead",
            "chunk_kind": "body",
            "status": "deleted",
        }
    ]
    parents = [
        {
            "corpus_id": "c1",
            "doc_id": "d1",
            "parent_id": "p_dead",
            "summary": "",
            "status": "deleted",
        }
    ]
    census = await collect_doc_artifact_census(
        _db(chunks=chunks, parent_chunks=parents),
        _FakeQdrant(_vector_payloads()),  # vectors only for the live chunks
        corpus_id="c1",
        doc_id="d1",
    )

    assert census["complete"] is True
    assert census["missing"]["qdrant_child_ids"] == []
    assert census["missing"]["summary_ids"] == []
    assert census["required"]["chunks"] == 2


@pytest.mark.asyncio
async def test_census_tombstones_only_is_a_source_parse_gap():
    """All chunks tombstoned = nothing durable exists: the doc is a
    source_parse gap, never a vector gap on dead chunk IDs."""
    chunks = [dict(row, status="deleted") for row in _chunk_rows()]
    census = await collect_doc_artifact_census(
        _db(chunks=chunks), _FakeQdrant([]), corpus_id="c1", doc_id="d1"
    )

    assert census["missing"]["chunk_source"] is True
    assert census["missing"]["qdrant_child_ids"] == []
    assert census["gap_stages"] == ["source_parse"]


# ─── Certificates: insert-only, only source of query_ready ────────────────


@pytest.mark.asyncio
async def test_certificate_issued_only_from_all_clear_census():
    db = _db()
    incomplete = await collect_doc_artifact_census(
        db, _FakeQdrant([]), corpus_id="c1", doc_id="d1"
    )
    assert await issue_certificate_if_complete(db, incomplete) is None

    broken = await collect_doc_artifact_census(
        db, _FakeQdrant(error=RuntimeError("down")), corpus_id="c1", doc_id="d1"
    )
    assert await issue_certificate_if_complete(db, broken) is None

    complete = await collect_doc_artifact_census(
        db, _FakeQdrant(_vector_payloads()), corpus_id="c1", doc_id="d1"
    )
    cert = await issue_certificate_if_complete(db, complete)
    assert cert is not None
    assert cert["certificate_id"].startswith("cert_")
    assert cert["corpus_id"] == "c1" and cert["doc_id"] == "d1"


@pytest.mark.asyncio
async def test_certificate_reissue_is_insert_only_noop():
    db = _db()
    census = await collect_doc_artifact_census(
        db, _FakeQdrant(_vector_payloads()), corpus_id="c1", doc_id="d1"
    )
    first = await issue_certificate_if_complete(db, census)
    second = await issue_certificate_if_complete(db, census)

    assert first["certificate_id"] == second["certificate_id"]
    assert len(db[CERTIFICATE_COLLECTION].rows) == 1


@pytest.mark.asyncio
async def test_contract_change_produces_new_certificate_identity():
    fp_a = contract_fingerprint({"summary_contract_hash": "a"})
    fp_b = contract_fingerprint({"summary_contract_hash": "b"})
    assert fp_a != fp_b
    assert certificate_id_for(
        corpus_id="c1", doc_id="d1", fingerprint=fp_a
    ) != certificate_id_for(corpus_id="c1", doc_id="d1", fingerprint=fp_b)


def test_proof_query_ready_requires_certificate():
    census = {
        "corpus_id": "c1",
        "doc_id": "d1",
        "complete": True,
        "excluded": False,
        "missing": {},
        "required": {},
        "observation_errors": [],
    }
    without_cert = build_proof(census, certificate=None)
    assert without_cert["query_ready"] is False

    with_cert = build_proof(census, certificate={"certificate_id": "cert_x"})
    assert with_cert["query_ready"] is True
    assert with_cert["certificate_id"] == "cert_x"
    assert with_cert["readiness_source"] == "certificate.v1"


def test_proof_observation_errors_yield_unavailable_never_ready():
    census = {
        "corpus_id": "c1",
        "doc_id": "d1",
        "complete": False,
        "excluded": False,
        "missing": {},
        "required": {},
        "observation_errors": ["qdrant.scroll(naive): timeout"],
    }
    proof = build_proof(census, certificate=None)
    assert proof["readiness_source"] == "unavailable"
    assert proof["query_ready"] is False


# ─── Hermes regression: lying legacy flags cannot go green ────────────────


@pytest.mark.asyncio
async def test_hermes_regression_lying_flags_still_not_query_ready():
    """ingest_stage=fully_enriched + verified write_state + zero vectors:
    the proof must say not ready and name the missing vector IDs."""
    proof = await doc_readiness_proof(
        _db(), _FakeQdrant([]), corpus_id="c1", doc_id="d1"
    )

    assert proof["query_ready"] is False
    assert proof["certificate_id"] is None
    assert proof["missing"]["qdrant_child_ids"] == ["ch1", "ch2"]
    assert proof["missing_counts"]["qdrant_child_ids"] == 2
    assert proof["readiness_source"] == "certificate.v1"


@pytest.mark.asyncio
async def test_hermes_regression_store_failure_has_no_legacy_fallback():
    proof = await doc_readiness_proof(
        _db(),
        _FakeQdrant(error=RuntimeError("connection refused")),
        corpus_id="c1",
        doc_id="d1",
    )

    assert proof["query_ready"] is False
    assert proof["readiness_source"] == "unavailable"
    assert proof["certificate_id"] is None


@pytest.mark.asyncio
async def test_doc_readiness_proof_issues_certificate_when_complete():
    db = _db()
    proof = await doc_readiness_proof(
        db, _FakeQdrant(_vector_payloads()), corpus_id="c1", doc_id="d1"
    )

    assert proof["query_ready"] is True
    assert proof["certificate_id"]
    assert len(db[CERTIFICATE_COLLECTION].rows) == 1


# ─── Ledger: durable runs, proof-derived status, outbox ───────────────────


@pytest.mark.asyncio
async def test_create_ingestion_run_is_idempotent_with_outbox_intent():
    db = _FakeDB()
    first = await ledger.create_ingestion_run(
        db, corpus_id="c1", doc_id="d1", user_id="u1", source="upload"
    )
    second = await ledger.create_ingestion_run(
        db, corpus_id="c1", doc_id="d1", user_id="u1", source="upload"
    )

    assert first["run_id"] == second["run_id"]
    assert first["run_id"] == ledger.run_id_for(corpus_id="c1", doc_id="d1")
    assert len(db[ledger.RUNS_COLLECTION].rows) == 1
    assert db[ledger.RUNS_COLLECTION].rows[0]["status"] == ledger.RUN_STATUS_INTAKE
    outbox = db[ledger.OUTBOX_COLLECTION].rows
    assert len(outbox) == 1 and outbox[0]["consumed_at"] is None


@pytest.mark.asyncio
async def test_update_run_from_proof_derives_status_from_proof():
    db = _FakeDB()
    run = await ledger.create_ingestion_run(db, corpus_id="c1", doc_id="d1")
    run_id = run["run_id"]

    async def _status_for(proof):
        await ledger.update_run_from_proof(db, run_id=run_id, proof=proof)
        return (await ledger.get_run(db, run_id=run_id))["status"]

    assert (
        await _status_for({"query_ready": True, "certificate_id": "cert_x"})
        == ledger.RUN_STATUS_QUERY_READY
    )
    assert (
        await _status_for({"query_ready": False, "readiness_source": "unavailable"})
        == ledger.RUN_STATUS_RECONCILING
    )
    assert (
        await _status_for(
            {
                "query_ready": False,
                "readiness_source": "certificate.v1",
                "blocking": [
                    {"stage": "extraction", "status": "dead_letter", "count": 3}
                ],
            }
        )
        == ledger.RUN_STATUS_DEGRADED
    )
    assert await _status_for({"excluded": True}) == ledger.RUN_STATUS_EXCLUDED


@pytest.mark.asyncio
async def test_stage_attempts_are_append_only_with_attempt_numbers():
    db = _FakeDB()
    first = await ledger.record_stage_attempt(
        db, corpus_id="c1", stage="execute", action="run", status="ok"
    )
    second = await ledger.record_stage_attempt(
        db, corpus_id="c1", stage="execute", action="run", status="failed",
        failure_class="RuntimeError",
    )

    assert (first["attempt_no"], second["attempt_no"]) == (1, 2)
    assert len(db[ledger.STAGE_ATTEMPTS_COLLECTION].rows) == 2


# ─── Anti-no_actionable_gaps: ledger actionability, never queue counts ────


@pytest.mark.asyncio
async def test_active_docs_without_run_rows_are_actionable():
    """Six stranded docs + empty queues was 'no_actionable_gaps' in V1.
    In V2 an active doc without a run row is always actionable work."""
    db = _db()  # one active doc, no runs, no outbox, all queues empty
    assert await corpus_has_actionable_work(db, corpus_id="c1") is True


@pytest.mark.asyncio
async def test_pending_run_is_actionable_even_with_empty_queues():
    db = _db()
    await ledger.create_ingestion_run(db, corpus_id="c1", doc_id="d1")
    await ledger.mark_outbox_consumed(
        db, run_ids=[ledger.run_id_for(corpus_id="c1", doc_id="d1")]
    )
    assert await corpus_has_actionable_work(db, corpus_id="c1") is True


@pytest.mark.asyncio
async def test_certified_corpus_with_consumed_outbox_is_idle():
    db = _db()
    run = await ledger.create_ingestion_run(db, corpus_id="c1", doc_id="d1")
    await ledger.update_run_from_proof(
        db, run_id=run["run_id"], proof={"query_ready": True}
    )
    await ledger.mark_outbox_consumed(db, run_ids=[run["run_id"]])
    assert await corpus_has_actionable_work(db, corpus_id="c1") is False


# ─── Delete cascade: control-plane state must not outlive the document ───


@pytest.mark.asyncio
async def test_document_delete_retires_run_and_outbox():
    """A deleted document must not keep a query_ready run row or an
    unconsumed outbox intent — otherwise the ledger shows a ready doc that
    no longer exists and the reconciler plans work for a tombstone."""
    from services.storage.mongo_writer import retire_document_derived_state

    db = _db()
    run = await ledger.create_ingestion_run(db, corpus_id="c1", doc_id="d1")
    await ledger.update_run_from_proof(
        db,
        run_id=run["run_id"],
        proof={"query_ready": True, "certificate_id": "cert_x"},
    )
    db["documents"].rows[0]["status"] = "deleted"  # the cascade's doc write

    counts = await retire_document_derived_state(
        db, corpus_id="c1", doc_id="d1"
    )

    assert counts["ingestion_runs"] == 1
    assert counts["control_plane_outbox"] == 1
    row = db[ledger.RUNS_COLLECTION].rows[0]
    assert row["status"] == ledger.RUN_STATUS_EXCLUDED
    assert row["certificate_id"] is None
    assert row["last_intake_reason"] == "document_deleted"
    assert all(
        o["consumed_at"] is not None for o in db[ledger.OUTBOX_COLLECTION].rows
    )
    assert await corpus_has_actionable_work(db, corpus_id="c1") is False


@pytest.mark.asyncio
async def test_corpus_delete_retires_all_runs_and_outbox():
    from services.storage.mongo_writer import retire_corpus_derived_state

    db = _db()
    await ledger.create_ingestion_run(db, corpus_id="c1", doc_id="d1")
    await ledger.create_ingestion_run(db, corpus_id="c1", doc_id="d2")

    counts = await retire_corpus_derived_state(db, corpus_id="c1")

    assert counts["ingestion_runs"] == 2
    assert counts["control_plane_outbox"] == 2
    assert all(
        r["status"] == ledger.RUN_STATUS_EXCLUDED
        for r in db[ledger.RUNS_COLLECTION].rows
    )
    assert all(
        o["consumed_at"] is not None for o in db[ledger.OUTBOX_COLLECTION].rows
    )


# ─── Reconciler: census → plan → execute → receipts ───────────────────────


def _fake_ingestion_service(qdrant, **kwargs):
    return SimpleNamespace(
        _qdrant=qdrant,
        run_bounded_corpus_repair_cycle=AsyncMock(
            return_value={"status": "ok", "applied": True}
        ),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_reconcile_corpus_plans_repair_for_stranded_doc(monkeypatch):
    """End-to-end on the exact failing case: gap → planner invoked with a
    census-derived limit → executor driven → receipts written."""
    import services.ingestion.document_pipeline_jobs as dp_jobs

    plan_calls: list[dict[str, Any]] = []

    async def fake_plan(db, *, corpus_id, user_id, apply, limit, **kwargs):
        plan_calls.append(
            {"corpus_id": corpus_id, "apply": apply, "limit": limit}
        )
        return {"status": "ok", "planned": 2}

    monkeypatch.setattr(dp_jobs, "plan_document_pipeline_jobs", fake_plan)

    db = _db()
    service = _fake_ingestion_service(_FakeQdrant([]))
    receipt = await reconcile_corpus(
        db, ingestion_service=service, corpus_id="c1", user_id="u1"
    )

    # Ledger backfill created the run row for the pre-V2 document.
    assert receipt["ledger_backfill"] == {"docs": 1, "created": 1}
    assert receipt["docs_censused"] == 1
    assert receipt["docs_with_gaps"] == 1
    assert receipt["certificates_issued"] == 0

    # Gap → planner invoked with apply=True and a census-derived limit.
    assert plan_calls and plan_calls[0]["apply"] is True
    assert plan_calls[0]["limit"] >= 2
    assert receipt["jobs_planned"] == 2
    assert receipt["gap_totals"][STAGE_DOCUMENT_PIPELINE] == 2

    # Executor driven; outbox intent consumed.
    service.run_bounded_corpus_repair_cycle.assert_awaited_once()
    assert receipt["executed"] is True
    assert receipt["outbox_consumed"] == 1

    # Run row carries the proof projection, not legacy flags.
    run = db[ledger.RUNS_COLLECTION].rows[0]
    assert run["status"] == ledger.RUN_STATUS_RECONCILING
    assert run["proof"]["query_ready"] is False
    assert run["proof"]["missing_counts"]["qdrant_child_ids"] == 2

    # Every planner/executor invocation left a stage_attempts receipt.
    stages = {r["stage"] for r in db[ledger.STAGE_ATTEMPTS_COLLECTION].rows}
    assert STAGE_DOCUMENT_PIPELINE in stages and "execute" in stages


@pytest.mark.asyncio
async def test_reconcile_corpus_certifies_healthy_doc_without_planning():
    db = _db()
    service = _fake_ingestion_service(_FakeQdrant(_vector_payloads()))
    receipt = await reconcile_corpus(
        db, ingestion_service=service, corpus_id="c1", user_id="u1"
    )

    assert receipt["certificates_issued"] == 1
    assert receipt["gap_totals"] == {}
    assert receipt["jobs_planned"] == 0
    run = db[ledger.RUNS_COLLECTION].rows[0]
    assert run["status"] == ledger.RUN_STATUS_QUERY_READY
    assert run["certificate_id"]
    assert len(db[CERTIFICATE_COLLECTION].rows) == 1


@pytest.mark.asyncio
async def test_reconcile_corpus_execution_failure_writes_failure_receipt(
    monkeypatch,
):
    import services.ingestion.document_pipeline_jobs as dp_jobs

    async def fake_plan(db, **kwargs):
        return {"status": "ok", "planned": 1}

    monkeypatch.setattr(dp_jobs, "plan_document_pipeline_jobs", fake_plan)

    db = _db()
    service = _fake_ingestion_service(_FakeQdrant([]))
    service.run_bounded_corpus_repair_cycle = AsyncMock(
        side_effect=RuntimeError("runpod endpoint gone")
    )
    receipt = await reconcile_corpus(
        db, ingestion_service=service, corpus_id="c1", user_id="u1"
    )

    # The tick survives; the failure is a durable stage_attempts receipt.
    assert receipt["status"] == "reconciled"
    assert receipt["executed"] is False
    failures = [
        r
        for r in db[ledger.STAGE_ATTEMPTS_COLLECTION].rows
        if r["stage"] == "execute" and r["status"] == "failed"
    ]
    assert len(failures) == 1
    assert failures[0]["failure_class"] == "RuntimeError"
    assert "runpod endpoint gone" in failures[0]["receipt"]["error"]


@pytest.mark.asyncio
async def test_reconcile_observation_failure_never_certifies(monkeypatch):
    db = _db()
    service = _fake_ingestion_service(
        _FakeQdrant(error=RuntimeError("qdrant down"))
    )
    receipt = await reconcile_corpus(
        db, ingestion_service=service, corpus_id="c1", user_id="u1"
    )

    assert receipt["certificates_issued"] == 0
    assert receipt["observation_failures"] == 1
    assert db[CERTIFICATE_COLLECTION].rows == []
    run = db[ledger.RUNS_COLLECTION].rows[0]
    assert run["status"] == ledger.RUN_STATUS_RECONCILING
    assert run["proof"]["readiness_source"] == "unavailable"


# ─── Planner limits and lane flags ────────────────────────────────────────


def test_plan_limit_covers_the_census_gap_keyspace():
    assert _plan_limit(0) == 500          # floor
    assert _plan_limit(10) == 500         # small gaps use the floor
    assert _plan_limit(5000) == 5100      # gap + headroom
    assert _plan_limit(20_000) == 10_000  # hard cap


def test_lane_flags_run_all_lanes_by_default():
    flags = _lane_run_flags(SimpleNamespace())
    assert all(flags.values())


def test_lane_flags_flag_off_honors_legacy_lane_settings():
    settings = SimpleNamespace(
        CONTROL_PLANE_V2_RUN_ALL_LANES=False,
        INGEST_AUTO_REPAIR_RUN_SOURCE_PARSE=True,
        INGEST_AUTO_REPAIR_RUN_DOCUMENT_PIPELINE=False,
        INGEST_AUTO_REPAIR_RUN_EXTRACTION=False,
        INGEST_AUTO_REPAIR_RUN_SUMMARIES=False,
        INGEST_AUTO_REPAIR_RUN_GRAPH=True,
    )
    flags = _lane_run_flags(settings)
    assert flags["run_source_parse_jobs"] is True
    assert flags["run_document_pipeline_jobs"] is False
    assert flags["run_extraction_jobs"] is False
    assert flags["run_summary_jobs"] is False
    assert flags["run_graph_jobs"] is True
