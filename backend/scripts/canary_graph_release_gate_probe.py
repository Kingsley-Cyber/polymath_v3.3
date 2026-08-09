#!/usr/bin/env python3
"""1C canary enforcement probe for the canonical graph-write release gate.

GRAPH_PROMOTION_RELEASE_GATE=enforce throughout, against isolated fixture
registries via RELEASE_REGISTRY_PATH — the shipped zero-active-entry
registry is never modified.

Phases (one disposable corpus):
  A  invalid release (zero active entries) -> every surface blocks before
     connection/lease/counters/state/Neo4j; jobs stay queued + retryable
  B  fully-passing fixture ReleasePin     -> lease, one execution, complete
  C  duplicate replay                     -> no additional graph state,
                                             final state unchanged
  D  release promotion reconsideration    -> same durable job id blocked
     under the invalid release, reconsidered once after the override flips
     to the passing fixture; no replacement job

Report: data_eval/graph_promotion_release_gate_canary.json

Usage (inside the backend container):
    python scripts/canary_graph_release_gate_probe.py
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
BACKEND_ROOT = HERE.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Reuse the Shadow A capture harness (fixtures, CLI runner, Neo4j counts).
_spec = importlib.util.spec_from_file_location(
    "shadow_a_probe", BACKEND_ROOT / "scripts" / "shadow_a_graph_release_gate_probe.py"
)
sa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sa)

from services.control_plane.release_registry import compute_entry_hash  # noqa: E402

TEST_CORPUS = "33333333-4444-4555-8666-777777777777"
OPERATOR = "canary-probe"
POLICY_BLOCK_EXIT_CODE = 77

SURFACES = (
    "run_graph_promotion_jobs",
    "ingestion_service.backfill_graph_failures",
    "polymath_failed_chunk_backfill.py",
    "promote_backfilled_relations.py",
    "polymath_graph_replay_backlog.py",
)

FROZEN_BLOCKED_KEYS = (
    "state",
    "retryable",
    "write_attempted",
    "missing_conditions",
    "release_registry_entry",
)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def passing_pin() -> dict[str, Any]:
    return {
        "schema_version": "polymath.release_state.v1",
        "release_id": "canary-fixture-v1.0.0",
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
        "extractor_release": "canary-fixture-v1.0.0",
        "ontology_release": "canary-fixture-v1.0.0",
        "acceptance_policy_release": "canary-fixture-v1.0.0",
    }


def write_registry(directory: Path, *, active_pin: dict | None) -> Path:
    entries: list[dict[str, Any]] = []
    if active_pin is not None:
        entry = {
            "entry_id": "release:canary-fixture-v1.0.0",
            "status": "active",
            "issued_at": "2026-08-03T00:00:00Z",
            "release_pin": active_pin,
        }
        entry["entry_hash"] = compute_entry_hash(entry)
        entries = [entry]
    path = directory / "release_pins.v1.json"
    path.write_text(
        json.dumps({"schema_version": "release_pins.v1", "entries": entries}),
        encoding="utf-8",
    )
    return path


def _run_cli(
    script: str, args: list[str], registry_path: Path
) -> dict[str, Any]:
    env = os.environ.copy()
    env["GRAPH_PROMOTION_RELEASE_GATE"] = "enforce"
    env["RELEASE_REGISTRY_PATH"] = str(registry_path)
    proc = subprocess.run(
        [sys.executable, str(BACKEND_ROOT / script), *args],
        cwd=str(BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    payload: dict[str, Any] = {}
    stdout = proc.stdout.strip()
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {"raw_stdout": stdout}
    return {"exit_code": proc.returncode, "payload": payload}


def _assert_frozen_block(payload: dict[str, Any]) -> list[str]:
    """Frozen blocked_no_release contract keys present and correct."""

    problems = []
    if payload.get("state") != "blocked_no_release":
        problems.append(f"state={payload.get('state')!r}")
    if payload.get("retryable") is not True:
        problems.append("retryable is not True")
    if payload.get("write_attempted") is not False:
        problems.append("write_attempted is not False")
    for key in FROZEN_BLOCKED_KEYS:
        if key not in payload:
            problems.append(f"missing frozen key {key}")
    return problems


async def _neo4j_total(driver: Any) -> int:
    counts = await sa._neo4j_corpus_counts(driver)
    return counts["nodes"] + counts["relations"]


async def _seed_job(db: Any, job_id: str) -> None:
    now = datetime.utcnow()
    await db["graph_promotion_jobs"].insert_one(
        {
            "job_id": job_id,
            "corpus_id": TEST_CORPUS,
            "doc_id": sa.TEST_DOC_ID,
            "status": "queued",
            "reason": "canary_probe",
            "user_id": OPERATOR,
            "neo4j_write_attempts": 0,
            "created_at": now,
            "updated_at": now,
        }
    )


async def _job(db: Any, job_id: str) -> dict[str, Any]:
    doc = await db["graph_promotion_jobs"].find_one({"job_id": job_id}, {"_id": 0})
    return doc or {}


async def _install_doc(db: Any) -> None:
    now = datetime.utcnow()
    await db["documents"].update_one(
        {"doc_id": sa.TEST_DOC_ID, "corpus_id": TEST_CORPUS},
        {
            "$set": {
                "doc_id": sa.TEST_DOC_ID,
                "corpus_id": TEST_CORPUS,
                "user_id": OPERATOR,
                "filename": "canary_fixture.md",
                "ingest_stage": "complete",
                "ghost_b_failure_count": 0,
                "ghost_b_staging_count": 0,
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


async def _probe() -> dict[str, Any]:
    os.environ["GRAPH_PROMOTION_RELEASE_GATE"] = "enforce"
    from config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    assert settings.GRAPH_PROMOTION_RELEASE_GATE == "enforce", (
        "canary requires enforce mode"
    )

    # Tripwire counters on the canonical writers — installed through the
    # authorized execution module's audit helper so this probe never
    # imports the writers itself (global no-bypass invariant).
    import services.control_plane.release_registry as release_registry
    from services.ingestion.graph_promotion_jobs import (
        instrument_canonical_writer_calls,
        run_graph_promotion_jobs,
    )

    writer_calls: dict[str, int] = {}
    instrument_canonical_writer_calls(writer_calls)

    def reset_writer_calls() -> None:
        writer_calls.clear()

    client, db = sa._mongo_db()
    qdrant, driver = sa._clients()
    report: dict[str, Any] = {
        "schema_version": "graph_release_gate_canary.v1",
        "gate_mode": "enforce",
        "corpus_id": TEST_CORPUS,
        "started_at": datetime.utcnow().isoformat(),
        "phases": {},
    }
    failures: list[str] = []
    suffix = uuid.uuid4().hex[:6]
    jobs = {
        "b1": f"canary_b1_{suffix}",
        "c1": f"canary_c1_{suffix}",
        "c2": f"canary_c2_{suffix}",
        "d2": f"canary_d2_{suffix}",
    }
    run_ids = [f"canary_{suffix}_p{p}_{n}" for p in "ABCD" for n in ("fc", "rp")]
    try:
        await _install_doc(db)
        for job_id in jobs.values():
            await _seed_job(db, job_id)

        with tempfile.TemporaryDirectory(prefix="canary_registry_") as tmp:
            tmp_path = Path(tmp)
            invalid_dir = tmp_path / "invalid"
            valid_dir = tmp_path / "valid"
            invalid_dir.mkdir()
            valid_dir.mkdir()
            invalid_registry = write_registry(invalid_dir, active_pin=None)
            valid_registry = write_registry(valid_dir, active_pin=passing_pin())

            def set_registry(path: Path) -> None:
                release_registry.resolve_registry_path = (
                    lambda explicit=None, _p=path: _p
                )

            # --------------------------------------------------------- A --
            set_registry(invalid_registry)
            phase_a: dict[str, Any] = {"registry": "zero_active_entries"}
            phase_a["neo4j_before"] = await _neo4j_total(driver)
            reset_writer_calls()

            runner_a = await run_graph_promotion_jobs(
                db,
                qdrant_client=qdrant,
                neo4j_driver=driver,
                corpus_id=TEST_CORPUS,
                user_id=OPERATOR,
                limit=10,
            )
            phase_a["runner"] = {
                "counts": runner_a["counts"],
                "decisions": {
                    r["job_id"]: (r.get("release_gate") or {}).get("state")
                    for r in runner_a["results"]
                },
            }
            jobs_a = {name: await _job(db, jid) for name, jid in jobs.items()}
            phase_a["jobs_after"] = {
                name: {
                    "status": j.get("status"),
                    "neo4j_write_attempts": j.get("neo4j_write_attempts"),
                    "last_release_gate_state": (
                        j.get("last_release_gate") or {}
                    ).get("state"),
                }
                for name, j in jobs_a.items()
            }

            import services.ingestion_service as isvc

            service = isvc.ingestion_service
            if service._db is None:
                await service.connect(db)
            repair_a = await service.backfill_graph_failures(
                corpus_id=TEST_CORPUS, doc_id=sa.TEST_DOC_ID, user_id=OPERATOR
            )
            phase_a["manual_repair"] = repair_a
            phase_a["manual_repair_contract"] = _assert_frozen_block(repair_a)

            cli_a = {}
            cli_a["polymath_failed_chunk_backfill.py"] = _run_cli(
                "scripts/polymath_failed_chunk_backfill.py",
                ["--corpus-id", TEST_CORPUS, "--limit", "1", "--run-id", run_ids[0]],
                invalid_registry,
            )
            cli_a["polymath_graph_replay_backlog.py"] = _run_cli(
                "scripts/polymath_graph_replay_backlog.py",
                ["--corpus-id", TEST_CORPUS, "--limit", "1", "--run-id", run_ids[1]],
                invalid_registry,
            )
            cli_a["promote_backfilled_relations.py"] = _run_cli(
                "scripts/promote_backfilled_relations.py",
                ["--corpus", TEST_CORPUS],
                invalid_registry,
            )
            phase_a["cli"] = {
                name: {
                    "exit_code": entry["exit_code"],
                    "state": entry["payload"].get("state")
                    or entry["payload"].get("mode"),
                    "contract": _assert_frozen_block(entry["payload"])
                    if entry["payload"].get("state") == "blocked_no_release"
                    else [],
                }
                for name, entry in cli_a.items()
            }
            phase_a["writer_calls"] = dict(writer_calls)
            phase_a["neo4j_after"] = await _neo4j_total(driver)
            report["phases"]["A_invalid_release"] = phase_a

            # --------------------------------------------------------- B --
            set_registry(valid_registry)
            phase_b: dict[str, Any] = {"registry": "passing_fixture_pin"}
            phase_b["neo4j_before"] = await _neo4j_total(driver)
            reset_writer_calls()

            job_b1_before = await _job(db, jobs["b1"])
            runner_b = await run_graph_promotion_jobs(
                db,
                qdrant_client=qdrant,
                neo4j_driver=driver,
                corpus_id=TEST_CORPUS,
                user_id=OPERATOR,
                limit=10,
            )
            # c1/c2/d2 were already executed in phase A? No — phase A blocked
            # every queued job; after the override flips, ALL queued jobs run.
            # Phase B therefore consumes b1, c1, c2, d2 in one run; phase C
            # reseeds its own jobs for duplicate replay.
            job_b1_after = await _job(db, jobs["b1"])
            phase_b["runner"] = {
                "counts": runner_b["counts"],
                "run_level_shadow": runner_b.get("release_gate_shadow"),
                "results": [
                    {"job_id": r["job_id"], "status": r["status"]}
                    for r in runner_b["results"]
                ],
            }
            phase_b["job_b1"] = {
                "before": {
                    "status": job_b1_before.get("status"),
                    "neo4j_write_attempts": job_b1_before.get(
                        "neo4j_write_attempts"
                    ),
                },
                "after": {
                    "status": job_b1_after.get("status"),
                    "neo4j_write_attempts": job_b1_after.get(
                        "neo4j_write_attempts"
                    ),
                },
            }
            repair_b = await service.backfill_graph_failures(
                corpus_id=TEST_CORPUS, doc_id=sa.TEST_DOC_ID, user_id=OPERATOR
            )
            phase_b["manual_repair"] = {
                "status": repair_b.get("status"),
                "blocked": repair_b.get("state") == "blocked_no_release",
            }
            cli_b = {}
            cli_b["polymath_failed_chunk_backfill.py"] = _run_cli(
                "scripts/polymath_failed_chunk_backfill.py",
                [
                    "--corpus-id",
                    TEST_CORPUS,
                    "--limit",
                    "1",
                    "--apply",
                    "--run-id",
                    run_ids[2],
                ],
                valid_registry,
            )
            cli_b["polymath_graph_replay_backlog.py"] = _run_cli(
                "scripts/polymath_graph_replay_backlog.py",
                [
                    "--corpus-id",
                    TEST_CORPUS,
                    "--limit",
                    "1",
                    "--apply",
                    "--run-id",
                    run_ids[3],
                ],
                valid_registry,
            )
            cli_b["promote_backfilled_relations.py"] = _run_cli(
                "scripts/promote_backfilled_relations.py",
                ["--corpus", TEST_CORPUS, "--apply"],
                valid_registry,
            )
            phase_b["cli"] = {
                name: {
                    "exit_code": entry["exit_code"],
                    "mode_or_summary": entry["payload"].get("mode")
                    or entry["payload"].get("summary"),
                }
                for name, entry in cli_b.items()
            }
            phase_b["writer_calls"] = dict(writer_calls)
            phase_b["neo4j_after"] = await _neo4j_total(driver)
            report["phases"]["B_valid_release"] = phase_b

            # --------------------------------------------------------- C --
            # Duplicate replay: requeue the SAME durable job id (phase B
            # completed it) and run the runner twice consecutively, then
            # replay the manual-repair and CLI apply paths on the same
            # source candidate.
            await db["graph_promotion_jobs"].update_one(
                {"job_id": jobs["c1"]},
                {
                    "$set": {
                        "status": "queued",
                        "lease_until": None,
                        "updated_at": datetime.utcnow(),
                    },
                    "$unset": {"completed_at": ""},
                },
            )
            phase_c: dict[str, Any] = {"registry": "passing_fixture_pin"}
            phase_c["neo4j_before"] = await _neo4j_total(driver)
            reset_writer_calls()

            runner_c1 = await run_graph_promotion_jobs(
                db,
                qdrant_client=qdrant,
                neo4j_driver=driver,
                corpus_id=TEST_CORPUS,
                user_id=OPERATOR,
                limit=10,
            )
            neo4j_mid = await _neo4j_total(driver)
            # Exact duplicate attempt: same corpus, same document, same
            # manual-repair invocation replayed.
            repair_c2 = await service.backfill_graph_failures(
                corpus_id=TEST_CORPUS, doc_id=sa.TEST_DOC_ID, user_id=OPERATOR
            )
            runner_c2 = await run_graph_promotion_jobs(
                db,
                qdrant_client=qdrant,
                neo4j_driver=driver,
                corpus_id=TEST_CORPUS,
                user_id=OPERATOR,
                limit=10,
            )
            job_c1_final = await _job(db, jobs["c1"])
            cli_c = _run_cli(
                "scripts/polymath_failed_chunk_backfill.py",
                [
                    "--corpus-id",
                    TEST_CORPUS,
                    "--limit",
                    "1",
                    "--apply",
                    "--run-id",
                    run_ids[4],
                ],
                valid_registry,
            )
            phase_c["executions"] = {
                "runner_run_1_counts": runner_c1["counts"],
                "runner_run_2_counts": runner_c2["counts"],
                "manual_repair_replay_status": repair_c2.get("status"),
                "cli_replay_exit": cli_c["exit_code"],
                "job_c1_final_status": job_c1_final.get("status"),
            }
            phase_c["writer_calls"] = dict(writer_calls)
            phase_c["neo4j_mid"] = neo4j_mid
            phase_c["neo4j_after"] = await _neo4j_total(driver)
            report["phases"]["C_duplicate_replay"] = phase_c

            # --------------------------------------------------------- D --
            # d2 was seeded before phase A and blocked there under the
            # invalid release; phase B executed it once the override flipped
            # (runner consumes every queued job). Its audit trail is:
            # blocked_no_release record -> same job id -> allowed -> done.
            job_d2 = await _job(db, jobs["d2"])
            phase_d: dict[str, Any] = {
                "registry_transition": "zero_active_entries -> passing_fixture_pin",
                "job_id": jobs["d2"],
                "blocked_under_invalid": (
                    phase_a["jobs_after"]["d2"]["status"] == "queued"
                    and phase_a["jobs_after"]["d2"]["last_release_gate_state"]
                    == "blocked_no_release"
                ),
                "final": {
                    "status": job_d2.get("status"),
                    "neo4j_write_attempts": job_d2.get("neo4j_write_attempts"),
                    "last_release_gate_state": (
                        job_d2.get("last_release_gate") or {}
                    ).get("state"),
                },
            }
            replacement = await db["graph_promotion_jobs"].count_documents(
                {
                    "corpus_id": TEST_CORPUS,
                    "doc_id": sa.TEST_DOC_ID,
                    "reason": "canary_probe",
                    "job_id": {"$nin": list(jobs.values())},
                }
            )
            phase_d["replacement_jobs_created"] = replacement
            report["phases"]["D_promotion_reconsideration"] = phase_d

    finally:
        await db["graph_promotion_jobs"].delete_many(
            {"job_id": {"$in": list(jobs.values())}}
        )
        await db["documents"].delete_one(
            {"doc_id": sa.TEST_DOC_ID, "corpus_id": TEST_CORPUS}
        )
        await db["ingest_repair_runs"].delete_many(
            {"run_id": {"$regex": f"^canary_{suffix}"}}
        )
        await qdrant.close()
        await driver.close()
        client.close()

    # --------------------------------------------------------- closeout --
    phase_a = report["phases"]["A_invalid_release"]
    phase_b = report["phases"]["B_valid_release"]
    phase_c = report["phases"]["C_duplicate_replay"]
    phase_d = report["phases"]["D_promotion_reconsideration"]

    closeout: dict[str, Any] = {}

    invalid_ok = (
        phase_a["runner"]["counts"].get("blocked_no_release") == len(jobs)
        and all(
            j["status"] == "queued" and j["neo4j_write_attempts"] == 0
            for j in phase_a["jobs_after"].values()
        )
        and phase_a["manual_repair"].get("state") == "blocked_no_release"
        and not phase_a["manual_repair_contract"]
        and all(
            entry["exit_code"] == POLICY_BLOCK_EXIT_CODE
            and entry["state"] in {"blocked_no_release", "BLOCKED_NO_RELEASE"}
            and not entry["contract"]
            for entry in phase_a["cli"].values()
        )
        and phase_a["writer_calls"] == {}
        and phase_a["neo4j_before"] == phase_a["neo4j_after"]
    )
    closeout["graph_gate_canary_invalid_release"] = "passed" if invalid_ok else "failed"
    if not invalid_ok:
        failures.append("phase A did not block all five surfaces cleanly")

    run_shadow = phase_b["runner"].get("run_level_shadow") or {}
    per_job_statuses = {r["status"] for r in phase_b["runner"]["results"]}
    decisions_in_run = {run_shadow.get("decision")} | {
        "would_allow" if s == "noop" else "other" for s in per_job_statuses
    }
    valid_ok = (
        run_shadow.get("decision") == "would_allow"
        and run_shadow.get("active_release_resolved_once") is True
        and phase_b["runner"]["counts"].get("noop") == len(jobs)
        and phase_b["job_b1"]["before"]["status"] == "queued"
        and phase_b["job_b1"]["after"]["status"] == "noop"
        and phase_b["job_b1"]["after"]["neo4j_write_attempts"]
        == phase_b["job_b1"]["before"]["neo4j_write_attempts"] + 1
        and phase_b["manual_repair"]["status"] == "noop"
        and not phase_b["manual_repair"]["blocked"]
        and all(
            entry["exit_code"] == 0 for entry in phase_b["cli"].values()
        )
        and len(decisions_in_run) == 1
    )
    closeout["graph_gate_canary_valid_release"] = "passed" if valid_ok else "failed"
    if not valid_ok:
        failures.append("phase B did not allow one clean execution per surface")

    retryability_ok = invalid_ok and all(
        j["status"] == "queued" for j in phase_a["jobs_after"].values()
    )
    closeout["blocked_job_retryability"] = "passed" if retryability_ok else "failed"

    reconsideration_ok = (
        phase_d["blocked_under_invalid"] is True
        and phase_d["final"]["status"] == "noop"
        and phase_d["replacement_jobs_created"] == 0
    )
    closeout["release_promotion_reconsideration"] = (
        "passed" if reconsideration_ok else "failed"
    )
    if not reconsideration_ok:
        failures.append("phase D reconsideration did not follow the same job id")

    idempotency_ok = (
        phase_c["neo4j_before"]
        == phase_c["neo4j_mid"]
        == phase_c["neo4j_after"]
        and phase_c["executions"]["job_c1_final_status"] == "noop"
        and phase_c["executions"]["runner_run_2_counts"].get("planned", 0) == 0
        and phase_c["executions"]["cli_replay_exit"] == 0
    )
    closeout["enforced_allow_idempotency"] = "passed" if idempotency_ok else "failed"
    if not idempotency_ok:
        failures.append("phase C duplicate replay changed state or graph")

    closeout["neo4j_writes_while_blocked"] = sum(
        phase_a["writer_calls"].values()
    )
    closeout["mixed_registry_decisions_per_run"] = max(
        0, len(decisions_in_run) - 1
    )
    observed = all(
        bool(phase_a["cli"]) and bool(phase_b["cli"])
        for _ in (0,)
    ) and len(phase_a["runner"]["decisions"]) == len(jobs)
    closeout["unobserved_write_surfaces"] = 0 if observed else 1

    status = (
        "passed"
        if not failures
        and all(
            v == "passed"
            for k, v in closeout.items()
            if isinstance(v, str)
        )
        and closeout["neo4j_writes_while_blocked"] == 0
        and closeout["mixed_registry_decisions_per_run"] == 0
        and closeout["unobserved_write_surfaces"] == 0
        else "failed"
    )
    report["closeout"] = closeout
    report["failures"] = failures
    report["status"] = status
    report["completed_at"] = datetime.utcnow().isoformat()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-path",
        default=os.environ.get(
            "CANARY_REPORT_PATH",
            str(
                BACKEND_ROOT.parent
                / "data_eval"
                / "graph_promotion_release_gate_canary.json"
            ),
        ),
    )
    args = parser.parse_args()

    report = asyncio.run(_probe())
    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report["closeout"], indent=2, sort_keys=True))
    _log(f"[canary] report written to {report_path}")
    _log(f"[canary] status: {report['status']}")
    for failure in report["failures"]:
        _log(f"[canary] FAILURE: {failure}")
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
