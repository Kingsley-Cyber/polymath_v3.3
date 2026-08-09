"""Deny-by-default graph-promotion release gate (behavior-changing slice).

Owner mandate (2026-08-03): the canonical Neo4j write-execution boundary
must consult the categorical ReleasePin registry before executing. Nothing
else — classification, durable job creation, shadow records, or graph
inspection — changes. Authority comes EXCLUSIVELY from the active
ReleasePin: neither a complete ReleaseStamp nor a complete TemporalEnvelope
may authorize a write.

Pre-enforcement verification checklist (all encoded below):

  1. no-release candidate → would_block
  2. incomplete release → would_block with exact missing conditions
  3. failed release state → would_block
  4. fully passing release → would_allow
  5. artifact hash mismatch → would_block
  6. blocked job remains durable and retryable
  7. no Neo4j write attempt occurs in enforced blocked mode
  8. reconciliation after release promotion reconsiders the same job once
  9. duplicate execution remains idempotent

Plus: shadow mode records without changing execution; off mode preserves
current behavior; the blocked payload matches the owner-frozen shape.
"""

from __future__ import annotations

import pytest

from models.release_state import ReleasePin
from models.release_stamp import empty_release_stamp
from services.ingestion.graph_promotion_jobs import (
    active_release_pin,
    evaluate_release_gate,
    run_graph_promotion_jobs,
)

# ---------------------------------------------------------------------------
# Fakes (same seam style as tests/test_graph_promotion_jobs.py)
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self.rows = self.rows[:value]
        return self

    async def to_list(self, length=None):
        return list(self.rows if length is None else self.rows[:length])


class _FakeJobsCollection:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.update_one_calls = []
        self.update_many_calls = []

    def find(self, query, projection=None):
        rows = [
            {key: value for key, value in job.items() if key != "_id"}
            for job in self.jobs
            if job.get("corpus_id") == query.get("corpus_id")
            and job.get("status") == query.get("status")
        ]
        return _FakeCursor(rows)

    async def update_one(self, query, update):
        self.update_one_calls.append((dict(query), update))
        modified = 0
        for job in self.jobs:
            if all(job.get(key) == value for key, value in query.items()):
                if "$set" in update:
                    job.update(update["$set"])
                if "$unset" in update:
                    for key in update["$unset"]:
                        job.pop(key, None)
                if "$inc" in update:
                    for key, value in update["$inc"].items():
                        job[key] = int(job.get(key) or 0) + int(value or 0)
                modified = 1
                break
        return type("Result", (), {"modified_count": modified})()

    async def update_many(self, query, update):
        self.update_many_calls.append((dict(query), update))
        return type("Result", (), {"modified_count": 0})()


class _FakeMarkCollection:
    """Records extraction-mark writes; blocked runs must never reach it."""

    def __init__(self):
        self.update_many_calls = []

    async def update_many(self, query, update):
        self.update_many_calls.append((dict(query), update))
        return type("Result", (), {"modified_count": 0})()


class _FakeDb(dict):
    def __getitem__(self, name):
        return dict.__getitem__(self, name)


def _queued_job(job_id="graph-job-1"):
    return {
        "job_id": job_id,
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "user_id": "user-1",
        "status": "queued",
        "neo4j_write_attempts": 0,
    }


def _db_with_jobs(jobs):
    collection = _FakeJobsCollection(jobs)
    ghost_marks = _FakeMarkCollection()
    extraction_marks = _FakeMarkCollection()
    db = _FakeDb(
        {
            "graph_promotion_jobs": collection,
            "ghost_b_extractions": ghost_marks,
            "extraction_jobs": extraction_marks,
        }
    )
    return db, collection, ghost_marks, extraction_marks


class _WriteTracker:
    """Tripwire stand-in for the canonical Neo4j write paths."""

    def __init__(self):
        self.calls = []

    def install(self, monkeypatch):
        async def fake_backfill(**kwargs):
            self.calls.append(("backfill", kwargs.get("doc_id")))
            return {
                "status": "done",
                "remaining_failed_chunks": 0,
                "neo4j_flushed": True,
            }

        async def fake_claims(_db, _driver, *, corpus_id, doc_id):
            self.calls.append(("claims", doc_id))
            return {"status": "done"}

        monkeypatch.setattr(
            "services.ingestion.graph_backfill.backfill_failed_graph_chunks",
            fake_backfill,
        )
        monkeypatch.setattr(
            "services.ingestion.promote.promote_claims_to_graph", fake_claims
        )


def _full_pin(**overrides) -> ReleasePin:
    base = dict(
        release_id="release-test-1",
        engineering_freeze="passed",
        recoverability="passed",
        git_reproducibility="passed",
        closed_world_annotation="passed",
        calibration="passed",
        held_out_qualification="passed",
        graph_write_promotion="passed",
        extractor_hash_matches=True,
        ontology_hash_matches=True,
        acceptance_policy_hash_matches=True,
        schema_hash_matches=True,
    )
    base.update(overrides)
    return ReleasePin(**base)


# ---------------------------------------------------------------------------
# Decision semantics (items 1–5)
# ---------------------------------------------------------------------------


def test_no_release_yields_would_block(monkeypatch):
    monkeypatch.setenv("GRAPH_PROMOTION_RELEASE_GATE", "enforce")
    decision = evaluate_release_gate(None, mode="enforce")
    assert decision["decision"] == "would_block"
    assert "release_bundle_absent" in decision["missing_conditions"]
    assert decision["release_id"] is None


def test_incomplete_release_reports_exact_missing_conditions():
    pin = _full_pin(calibration="pending", held_out_qualification="pending")
    decision = evaluate_release_gate(pin, mode="enforce")
    assert decision["decision"] == "would_block"
    assert set(decision["missing_conditions"]) == {
        "calibration",
        "held_out_qualification",
    }


def test_failed_release_state_blocks():
    pin = _full_pin(graph_write_promotion="pending")
    decision = evaluate_release_gate(pin, mode="enforce")
    assert decision["decision"] == "would_block"
    assert decision["missing_conditions"] == ["graph_write_promotion"]


def test_fully_passing_release_yields_would_allow():
    decision = evaluate_release_gate(_full_pin(), mode="enforce")
    assert decision["decision"] == "would_allow"
    assert decision["missing_conditions"] == []
    assert decision["release_id"] == "release-test-1"


def test_artifact_hash_mismatch_blocks():
    for hash_field in (
        "extractor_hash_matches",
        "ontology_hash_matches",
        "acceptance_policy_hash_matches",
        "schema_hash_matches",
    ):
        pin = _full_pin(**{hash_field: False})
        decision = evaluate_release_gate(pin, mode="enforce")
        assert decision["decision"] == "would_block"
        assert decision["missing_conditions"] == [hash_field]


def test_neither_stamp_nor_envelope_can_authorize_a_write():
    # A COMPLETE ReleaseStamp carries identity, never write authority.
    stamp = empty_release_stamp()
    decision = evaluate_release_gate(stamp, mode="enforce")
    assert decision["decision"] == "would_block"
    assert "release_bundle_absent" in decision["missing_conditions"]

    # A TemporalEnvelope (or any non-ReleasePin object) is treated as no
    # active release.
    class _CompleteLookingEnvelope:
        selection_status = "selected"

    decision = evaluate_release_gate(_CompleteLookingEnvelope(), mode="enforce")
    assert decision["decision"] == "would_block"


def test_active_release_pin_is_none_until_registry_exists():
    assert active_release_pin() is None


# ---------------------------------------------------------------------------
# Enforced blocked mode (items 6–7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enforce_blocked_job_remains_durable_retryable(monkeypatch):
    monkeypatch.setenv("GRAPH_PROMOTION_RELEASE_GATE", "enforce")
    monkeypatch.setattr(
        "services.ingestion.graph_promotion_jobs.release_gate_mode",
        lambda: "enforce",
    )
    db, jobs, ghost_marks, extraction_marks = _db_with_jobs([_queued_job()])
    tracker = _WriteTracker()
    tracker.install(monkeypatch)

    result = await run_graph_promotion_jobs(
        db,
        qdrant_client=None,
        neo4j_driver=object(),
        corpus_id="corpus-1",
        user_id="user-1",
        release=None,  # no active registry entry → deny by default
    )

    # Item 6: durable and retryable — the job is STILL queued.
    assert jobs.jobs[0]["status"] == "queued"
    assert result["counts"]["blocked_no_release"] == 1
    assert result["results"][0]["status"] == "blocked_no_release"

    # Owner-frozen blocked payload shape.
    blocked = result["results"][0]["release_gate"]
    assert blocked == {
        "state": "blocked_no_release",
        "retryable": True,
        "write_attempted": False,
        "missing_conditions": blocked["missing_conditions"],
        "release_registry_entry": None,
    }
    assert "release_bundle_absent" in blocked["missing_conditions"]
    assert jobs.jobs[0]["last_release_gate"]["state"] == "blocked_no_release"

    # Item 7: no write attempt — no lease, no attempt counter, no writer call.
    assert jobs.jobs[0]["neo4j_write_attempts"] == 0
    assert tracker.calls == []
    assert ghost_marks.update_many_calls == []
    assert extraction_marks.update_many_calls == []
    lease_updates = [
        update
        for _query, update in jobs.update_one_calls
        if "$inc" in update
    ]
    assert lease_updates == []


@pytest.mark.asyncio
async def test_enforce_blocks_even_with_incomplete_release(monkeypatch):
    monkeypatch.setattr(
        "services.ingestion.graph_promotion_jobs.release_gate_mode",
        lambda: "enforce",
    )
    db, jobs, ghost_marks, extraction_marks = _db_with_jobs([_queued_job()])
    tracker = _WriteTracker()
    tracker.install(monkeypatch)

    result = await run_graph_promotion_jobs(
        db,
        qdrant_client=None,
        neo4j_driver=object(),
        corpus_id="corpus-1",
        user_id="user-1",
        release=_full_pin(calibration="pending"),
    )

    assert result["counts"]["blocked_no_release"] == 1
    assert jobs.jobs[0]["status"] == "queued"
    assert tracker.calls == []
    assert ghost_marks.update_many_calls == []
    assert result["results"][0]["release_gate"]["missing_conditions"] == [
        "calibration"
    ]


@pytest.mark.asyncio
async def test_enforce_allows_fully_passing_release(monkeypatch):
    monkeypatch.setattr(
        "services.ingestion.graph_promotion_jobs.release_gate_mode",
        lambda: "enforce",
    )
    db, jobs, *_marks = _db_with_jobs([_queued_job()])
    tracker = _WriteTracker()
    tracker.install(monkeypatch)

    result = await run_graph_promotion_jobs(
        db,
        qdrant_client=None,
        neo4j_driver=object(),
        corpus_id="corpus-1",
        user_id="user-1",
        release=_full_pin(),
    )

    assert result["counts"]["blocked_no_release"] == 0
    assert result["counts"]["done"] == 1
    assert tracker.calls == [("backfill", "doc-1")]
    assert jobs.jobs[0]["status"] == "done"


# ---------------------------------------------------------------------------
# Shadow and off modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_records_decision_without_changing_execution(monkeypatch):
    monkeypatch.setattr(
        "services.ingestion.graph_promotion_jobs.release_gate_mode",
        lambda: "shadow",
    )
    db, jobs, *_marks = _db_with_jobs([_queued_job()])
    tracker = _WriteTracker()
    tracker.install(monkeypatch)

    result = await run_graph_promotion_jobs(
        db,
        qdrant_client=None,
        neo4j_driver=object(),
        corpus_id="corpus-1",
        user_id="user-1",
        release=None,  # would_block in shadow — execution unchanged
    )

    assert result["counts"]["blocked_no_release"] == 0
    assert result["counts"]["done"] == 1
    assert tracker.calls == [("backfill", "doc-1")]
    shadow = result["results"][0]["release_gate_shadow"]
    assert shadow["decision"] == "would_block"
    assert shadow["mode"] == "shadow"
    completion_set = jobs.update_one_calls[-1][1]["$set"]
    assert completion_set["release_gate_shadow"]["decision"] == "would_block"
    assert jobs.jobs[0]["status"] == "done"


@pytest.mark.asyncio
async def test_off_mode_preserves_current_behavior(monkeypatch):
    monkeypatch.setattr(
        "services.ingestion.graph_promotion_jobs.release_gate_mode",
        lambda: "off",
    )
    db, jobs, *_marks = _db_with_jobs([_queued_job()])
    tracker = _WriteTracker()
    tracker.install(monkeypatch)

    result = await run_graph_promotion_jobs(
        db,
        qdrant_client=None,
        neo4j_driver=object(),
        corpus_id="corpus-1",
        user_id="user-1",
        release=None,
    )

    assert result["counts"]["done"] == 1
    assert result["counts"]["blocked_no_release"] == 0
    assert "release_gate_shadow" not in result["results"][0]
    assert "last_release_gate" not in jobs.jobs[0]
    assert tracker.calls == [("backfill", "doc-1")]


# ---------------------------------------------------------------------------
# Reconciliation and idempotency (items 8–9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciliation_after_release_promotion_reconsiders_once(
    monkeypatch,
):
    monkeypatch.setattr(
        "services.ingestion.graph_promotion_jobs.release_gate_mode",
        lambda: "enforce",
    )
    db, jobs, *_marks = _db_with_jobs([_queued_job()])
    tracker = _WriteTracker()
    tracker.install(monkeypatch)
    kwargs = dict(
        qdrant_client=None,
        neo4j_driver=object(),
        corpus_id="corpus-1",
        user_id="user-1",
    )

    # Run 1: no release → blocked, job stays queued.
    first = await run_graph_promotion_jobs(db, release=None, **kwargs)
    assert first["counts"]["blocked_no_release"] == 1
    assert jobs.jobs[0]["status"] == "queued"

    # Run 2: release promoted → the SAME job is reconsidered exactly once
    # and executed; no second job is created for the same gap.
    second = await run_graph_promotion_jobs(db, release=_full_pin(), **kwargs)
    assert second["counts"]["done"] == 1
    assert second["counts"]["blocked_no_release"] == 0
    assert tracker.calls == [("backfill", "doc-1")]  # exactly one write
    assert jobs.jobs[0]["status"] == "done"
    assert len(jobs.jobs) == 1


@pytest.mark.asyncio
async def test_duplicate_execution_remains_idempotent(monkeypatch):
    monkeypatch.setattr(
        "services.ingestion.graph_promotion_jobs.release_gate_mode",
        lambda: "enforce",
    )
    db, jobs, *_marks = _db_with_jobs([_queued_job()])
    tracker = _WriteTracker()
    tracker.install(monkeypatch)
    kwargs = dict(
        qdrant_client=None,
        neo4j_driver=object(),
        corpus_id="corpus-1",
        user_id="user-1",
        release=_full_pin(),
    )

    first = await run_graph_promotion_jobs(db, **kwargs)
    assert first["counts"]["done"] == 1

    # Second run finds no queued work: the completed job is never re-run.
    second = await run_graph_promotion_jobs(db, **kwargs)
    assert second["counts"]["planned"] == 0
    assert second["results"] == []
    assert tracker.calls == [("backfill", "doc-1")]
