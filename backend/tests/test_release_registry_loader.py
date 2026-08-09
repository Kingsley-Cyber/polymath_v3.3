"""Fail-closed active release-registry loader tests (slice 1A).

Covers the owner-mandated fail-closed matrix:

* missing registry / unsupported version
* zero or multiple active entries
* malformed active entry / ReleaseStamp-shaped payload never inferred
* missing categorical state / failed categorical state
* missing entry hash / entry hash mismatch
* registry mutation during one run fails closed
* registry resolved exactly once per run; decisions never mix in one run

Determinism rule: every fixture is built from canonical hashes computed
by the loader itself — no hardcoded digests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from models.release_state import ReleasePin
from services.control_plane.release_registry import (
    RELEASE_REGISTRY_SCHEMA_VERSION,
    begin_registry_run,
    compute_entry_hash,
    load_release_registry,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def passing_pin_payload(**overrides) -> dict:
    payload = {
        "schema_version": "polymath.release_state.v1",
        "release_id": "extraction-core-v1.0.0",
        "engineering_freeze": "passed",
        "recoverability": "passed",
        "git_reproducibility": "passed",
        "closed_world_annotation": "passed",
        "calibration": "passed",
        "held_out_qualification": "passed",
        "graph_write_promotion": "passed",
        "extractor_hash_matches": True,
        "ontology_hash_matches": True,
        "acceptance_policy_hash_matches": True,
        "schema_hash_matches": True,
        "extractor_release": "extraction-core-v1.0.0",
        "ontology_release": "ontology-v1.0.0",
        "acceptance_policy_release": "acceptance-v1.0.0",
    }
    payload.update(overrides)
    return payload


def make_entry(pin_payload: dict | None = None, **overrides) -> dict:
    entry = {
        "entry_id": "release:extraction-core-v1.0.0",
        "status": "active",
        "issued_at": "2026-08-03T00:00:00Z",
        "release_pin": pin_payload
        if pin_payload is not None
        else passing_pin_payload(),
    }
    entry.update(overrides)
    entry["entry_hash"] = compute_entry_hash(entry)
    return entry


def write_registry(
    tmp_path: Path,
    entries: list[dict] | None = None,
    *,
    schema_version: str = RELEASE_REGISTRY_SCHEMA_VERSION,
) -> Path:
    registry = {"schema_version": schema_version, "entries": entries or []}
    path = tmp_path / "release_pins.v1.json"
    path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Unit: the fail-closed matrix
# ---------------------------------------------------------------------------


def test_valid_single_active_entry_resolves_with_identity(tmp_path):
    path = write_registry(tmp_path, [make_entry()])
    resolution = load_release_registry(path)
    assert resolution.status == "ok"
    assert resolution.reason is None
    assert resolution.registry_version == RELEASE_REGISTRY_SCHEMA_VERSION
    assert resolution.registry_hash and resolution.registry_hash.startswith("sha256:")
    assert resolution.entry_id == "release:extraction-core-v1.0.0"
    assert resolution.entry_hash and resolution.entry_hash.startswith("sha256:")
    assert isinstance(resolution.release_pin, ReleasePin)
    assert resolution.release_pin.release_id == "extraction-core-v1.0.0"
    identity = resolution.trace_identity()
    assert set(identity) == {
        "registry_version",
        "registry_hash",
        "entry_id",
        "entry_hash",
        "release_pin",
    }


def test_missing_registry_fails_closed(tmp_path):
    resolution = load_release_registry(tmp_path / "absent.json")
    assert resolution.status == "fail_closed"
    assert resolution.reason == "registry_missing"
    assert resolution.release_pin is None


def test_unsupported_schema_version_fails_closed(tmp_path):
    path = write_registry(tmp_path, [make_entry()], schema_version="release_pins.v2")
    resolution = load_release_registry(path)
    assert resolution.status == "fail_closed"
    assert resolution.reason == "unsupported_version"
    assert resolution.release_pin is None


def test_zero_active_entries_fails_closed(tmp_path):
    path = write_registry(tmp_path, [make_entry(status="proposed")])
    resolution = load_release_registry(path)
    assert resolution.status == "fail_closed"
    assert resolution.reason == "zero_active_entries"
    assert resolution.release_pin is None


def test_multiple_active_entries_fail_closed(tmp_path):
    second = make_entry(entry_id="release:other-v9.9.9")
    path = write_registry(tmp_path, [make_entry(), second])
    resolution = load_release_registry(path)
    assert resolution.status == "fail_closed"
    assert resolution.reason == "multiple_active_entries"
    assert resolution.release_pin is None


def test_malformed_entry_payload_fails_closed(tmp_path):
    entry = make_entry()
    entry.pop("entry_id")
    entry["entry_hash"] = compute_entry_hash(entry)
    path = write_registry(tmp_path, [entry])
    resolution = load_release_registry(path)
    assert resolution.status == "fail_closed"
    assert resolution.reason == "malformed_active_entry"


def test_release_stamp_payload_is_never_inferred(tmp_path):
    """A stamp-shaped payload is rejected outright — never coerced."""

    stamp_pin = {
        "release_pins": {"extractor_release": "extraction-core-v1.0.0"},
        "certificate_id": "cert-1",
        "stamped_at": "2026-08-03T00:00:00Z",
    }
    path = write_registry(tmp_path, [make_entry(pin_payload=stamp_pin)])
    resolution = load_release_registry(path)
    assert resolution.status == "fail_closed"
    assert resolution.reason == "categorical_state_missing"
    assert resolution.release_pin is None


def test_foreign_fields_in_pin_fail_closed(tmp_path):
    path = write_registry(
        tmp_path, [make_entry(pin_payload=passing_pin_payload(extra_field="x"))]
    )
    resolution = load_release_registry(path)
    assert resolution.status == "fail_closed"
    assert resolution.reason == "malformed_active_entry"


def test_missing_categorical_state_fails_closed(tmp_path):
    pin = passing_pin_payload()
    del pin["calibration"]
    path = write_registry(tmp_path, [make_entry(pin_payload=pin)])
    resolution = load_release_registry(path)
    assert resolution.status == "fail_closed"
    assert resolution.reason == "categorical_state_missing"


def test_missing_artifact_hash_proof_fails_closed(tmp_path):
    pin = passing_pin_payload()
    del pin["schema_hash_matches"]
    path = write_registry(tmp_path, [make_entry(pin_payload=pin)])
    resolution = load_release_registry(path)
    assert resolution.status == "fail_closed"
    assert resolution.reason == "categorical_state_missing"


def test_failed_categorical_state_fails_closed(tmp_path):
    path = write_registry(
        tmp_path, [make_entry(pin_payload=passing_pin_payload(calibration="pending"))]
    )
    resolution = load_release_registry(path)
    assert resolution.status == "fail_closed"
    assert resolution.reason == "categorical_state_failed"
    assert resolution.release_pin is None


def test_false_artifact_hash_proof_fails_closed(tmp_path):
    path = write_registry(
        tmp_path,
        [make_entry(pin_payload=passing_pin_payload(extractor_hash_matches=False))],
    )
    resolution = load_release_registry(path)
    assert resolution.status == "fail_closed"
    assert resolution.reason == "categorical_state_failed"


def test_missing_entry_hash_fails_closed(tmp_path):
    entry = make_entry()
    del entry["entry_hash"]
    path = write_registry(tmp_path, [entry])
    resolution = load_release_registry(path)
    assert resolution.status == "fail_closed"
    assert resolution.reason == "entry_hash_missing"


def test_entry_hash_mismatch_fails_closed(tmp_path):
    entry = make_entry()
    entry["entry_hash"] = "sha256:" + "0" * 64
    path = write_registry(tmp_path, [entry])
    resolution = load_release_registry(path)
    assert resolution.status == "fail_closed"
    assert resolution.reason == "entry_hash_mismatch"


def test_registry_malformed_json_fails_closed(tmp_path):
    path = tmp_path / "release_pins.v1.json"
    path.write_text("{not json", encoding="utf-8")
    resolution = load_release_registry(path)
    assert resolution.status == "fail_closed"
    assert resolution.reason == "registry_malformed"


def test_entry_hash_is_key_order_independent(tmp_path):
    """Canonical serialization: identical content, different key order."""

    entry_a = make_entry()
    reversed_entry = {k: entry_a[k] for k in reversed(list(entry_a))}
    assert compute_entry_hash(entry_a) == compute_entry_hash(reversed_entry)


def test_superseded_entries_are_ignored(tmp_path):
    path = write_registry(
        tmp_path,
        [make_entry(status="superseded"), make_entry(status="revoked")],
    )
    resolution = load_release_registry(path)
    assert resolution.reason == "zero_active_entries"
    assert resolution.release_pin is None


# ---------------------------------------------------------------------------
# Run handle: mutation during one run fails closed
# ---------------------------------------------------------------------------


def test_run_handle_detects_registry_mutation(tmp_path):
    path = write_registry(tmp_path, [make_entry()])
    handle = begin_registry_run(path)
    assert handle.resolution.status == "ok"
    assert handle.verify_unchanged() is True
    # Tamper with the registry mid-run.
    path.write_text(
        json.dumps({"schema_version": RELEASE_REGISTRY_SCHEMA_VERSION, "entries": []}),
        encoding="utf-8",
    )
    assert handle.verify_unchanged() is False


def test_run_handle_detects_deleted_registry(tmp_path):
    path = write_registry(tmp_path, [make_entry()])
    handle = begin_registry_run(path)
    path.unlink()
    assert handle.verify_unchanged() is False


def test_fail_closed_handle_stays_stable(tmp_path):
    """A registry granting nothing cannot be mutated into granting something."""

    handle = begin_registry_run(tmp_path / "absent.json")
    assert handle.resolution.status == "fail_closed"
    assert handle.verify_unchanged() is True


# ---------------------------------------------------------------------------
# Integration: the durable runner consumes the registry exactly once
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner_env(tmp_path, monkeypatch):
    """Mongo + registry fixtures wired into the durable runner."""

    import services.control_plane.release_registry as release_registry
    from motor.motor_asyncio import AsyncIOMotorClient

    from config import get_settings

    registry_path = {"path": write_registry(tmp_path, []), "tmp_path": tmp_path}
    monkeypatch.setattr(
        release_registry, "resolve_registry_path", lambda explicit=None: registry_path["path"]
    )

    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client.get_default_database()
    yield db, registry_path
    client.close()


TEST_CORPUS = "22222222-3333-4444-8555-666666666666"
TEST_DOC = "registry-loader-doc"


async def _seed(doc_db, job_ids: list[str]) -> None:
    from datetime import datetime

    now = datetime.utcnow()
    await doc_db["documents"].update_one(
        {"doc_id": TEST_DOC, "corpus_id": TEST_CORPUS},
        {
            "$set": {
                "doc_id": TEST_DOC,
                "corpus_id": TEST_CORPUS,
                "user_id": "loader-test",
                "ingest_stage": "complete",
                "ghost_b_failure_count": 0,
                "write_state": {
                    "qdrant_written": True,
                    "neo4j_written": True,
                    "verified": True,
                },
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    for job_id in job_ids:
        await doc_db["graph_promotion_jobs"].insert_one(
            {
                "job_id": job_id,
                "corpus_id": TEST_CORPUS,
                "doc_id": TEST_DOC,
                "status": "queued",
                "reason": "loader_test",
                "user_id": "loader-test",
                "neo4j_write_attempts": 0,
                "created_at": now,
                "updated_at": now,
            }
        )


async def _cleanup(doc_db, job_ids: list[str]) -> None:
    await doc_db["graph_promotion_jobs"].delete_many({"job_id": {"$in": job_ids}})
    await doc_db["documents"].delete_one(
        {"doc_id": TEST_DOC, "corpus_id": TEST_CORPUS}
    )


@pytest.mark.asyncio
async def test_runner_enforce_allows_with_valid_active_release(runner_env, monkeypatch):
    db, registry_path = runner_env
    registry_path["path"] = write_registry(registry_path["tmp_path"], [make_entry()])
    monkeypatch.setattr(
        "services.ingestion.graph_promotion_jobs.release_gate_mode", lambda: "enforce"
    )
    from services.ingestion.graph_promotion_jobs import run_graph_promotion_jobs

    job_ids = ["loader_allow_1"]
    await _seed(db, job_ids)
    try:
        result = await run_graph_promotion_jobs(
            db,
            qdrant_client=None,
            neo4j_driver=object(),
            corpus_id=TEST_CORPUS,
            user_id="loader-test",
        )
        assert result["counts"]["noop"] == 1
        shadow = result["release_gate_shadow"]
        assert shadow["decision"] == "would_allow"
        assert shadow["release_registry_entry"] == "release:extraction-core-v1.0.0"
        assert shadow["active_release_resolved_once"] is True
        job = await db["graph_promotion_jobs"].find_one({"job_id": job_ids[0]})
        assert job["status"] == "noop"
    finally:
        await _cleanup(db, job_ids)


@pytest.mark.asyncio
async def test_runner_enforce_blocks_with_failed_release(runner_env, monkeypatch):
    db, registry_path = runner_env
    failing = make_entry(pin_payload=passing_pin_payload(calibration="pending"))
    registry_path["path"].write_text(
        json.dumps(
            {"schema_version": RELEASE_REGISTRY_SCHEMA_VERSION, "entries": [failing]}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "services.ingestion.graph_promotion_jobs.release_gate_mode", lambda: "enforce"
    )
    from services.ingestion.graph_promotion_jobs import run_graph_promotion_jobs

    job_ids = ["loader_block_1"]
    await _seed(db, job_ids)
    try:
        result = await run_graph_promotion_jobs(
            db,
            qdrant_client=None,
            neo4j_driver=None,
            corpus_id=TEST_CORPUS,
            user_id="loader-test",
        )
        assert result["counts"]["blocked_no_release"] == 1
        job = await db["graph_promotion_jobs"].find_one({"job_id": job_ids[0]})
        # Blocked before lease: queued, zero write attempts, retryable.
        assert job["status"] == "queued"
        assert job["neo4j_write_attempts"] == 0
        assert job["last_release_gate"]["state"] == "blocked_no_release"
        assert job["last_release_gate"]["write_attempted"] is False
        assert "calibration" in job["last_release_gate"]["missing_conditions"]
    finally:
        await _cleanup(db, job_ids)


@pytest.mark.asyncio
async def test_runner_resolves_registry_exactly_once_per_run(runner_env, monkeypatch):
    db, registry_path = runner_env
    registry_path["path"].write_text(
        json.dumps(
            {"schema_version": RELEASE_REGISTRY_SCHEMA_VERSION, "entries": [make_entry()]}
        ),
        encoding="utf-8",
    )
    import services.control_plane.release_registry as release_registry

    calls = {"n": 0}
    real_load = release_registry.load_release_registry

    def counting_load(path=None):
        calls["n"] += 1
        return real_load(path)

    monkeypatch.setattr(release_registry, "load_release_registry", counting_load)
    from services.ingestion.graph_promotion_jobs import run_graph_promotion_jobs

    job_ids = ["loader_once_1", "loader_once_2"]
    await _seed(db, job_ids)
    try:
        result = await run_graph_promotion_jobs(
            db,
            qdrant_client=None,
            neo4j_driver=object(),
            corpus_id=TEST_CORPUS,
            user_id="loader-test",
            limit=10,
        )
        assert calls["n"] == 1
        # Both jobs consumed the identical run-level decision: decisions
        # can never mix within one run.
        statuses = {r["status"] for r in result["results"]}
        assert statuses == {"noop"}
    finally:
        await _cleanup(db, job_ids)


@pytest.mark.asyncio
async def test_runner_fails_closed_on_registry_mutation_mid_run(
    runner_env, monkeypatch
):
    db, registry_path = runner_env
    registry_path["path"].write_text(
        json.dumps(
            {"schema_version": RELEASE_REGISTRY_SCHEMA_VERSION, "entries": [make_entry()]}
        ),
        encoding="utf-8",
    )
    import services.ingestion.graph_backfill as graph_backfill

    real_backfill = graph_backfill.backfill_failed_graph_chunks
    seen = {"first": False}

    async def mutating_backfill(**kwargs):
        if not seen["first"]:
            seen["first"] = True
            # Simulate the registry changing underneath the run.
            registry_path["path"].write_text(
                json.dumps(
                    {
                        "schema_version": RELEASE_REGISTRY_SCHEMA_VERSION,
                        "entries": [],
                    }
                ),
                encoding="utf-8",
            )
        return await real_backfill(**kwargs)

    # The runner imports the writer locally inside the function; patch the
    # source module symbol it imports from.
    monkeypatch.setattr(graph_backfill, "backfill_failed_graph_chunks", mutating_backfill)

    from services.ingestion.graph_promotion_jobs import run_graph_promotion_jobs

    job_ids = ["loader_mut_1", "loader_mut_2"]
    await _seed(db, job_ids)
    try:
        result = await run_graph_promotion_jobs(
            db,
            qdrant_client=None,
            neo4j_driver=object(),
            corpus_id=TEST_CORPUS,
            user_id="loader-test",
            limit=10,
        )
        statuses = {r["job_id"]: r["status"] for r in result["results"]}
        # One job completed under the original valid resolution; the
        # mutation is detected before the remaining job leases or writes.
        # Order between equal timestamps is not guaranteed, so assert the
        # multiset instead of a specific job id.
        assert sorted(statuses.values()) == ["blocked_registry_mutated", "noop"]
        assert result["counts"]["registry_mutated"] == 1
        blocked_id = next(
            jid for jid, s in statuses.items() if s == "blocked_registry_mutated"
        )
        blocked = await db["graph_promotion_jobs"].find_one({"job_id": blocked_id})
        assert blocked["status"] == "queued"
        assert blocked["neo4j_write_attempts"] == 0
    finally:
        await _cleanup(db, job_ids)
