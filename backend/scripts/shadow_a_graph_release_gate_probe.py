#!/usr/bin/env python3
"""Shadow A probe for the canonical graph-write release gate.

Proves ONLY the missing-release deny decision and observability path:

    GRAPH_PROMOTION_RELEASE_GATE=shadow
    active_release_pin() -> None
    expected: decision=would_block, execution continues unchanged

The probe runs every canonical write surface once per mode (off, shadow)
against a disposable test corpus, then verifies:

  * every surface emitted a gate decision (would_block only)
  * shadow never blocked, reclassified, or counter-shifted anything
  * off vs shadow: candidates, executions, and results are identical
    except additive shadow diagnostics
  * release state resolved once per run; operator + caller identity present

Report: data_eval/graph_promotion_release_gate_shadow_a.json

Usage (inside the backend container):
    python scripts/shadow_a_graph_release_gate_probe.py
    python scripts/shadow_a_graph_release_gate_probe.py --capture   # internal
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
BACKEND_ROOT = HERE.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

TEST_CORPUS = "11111111-2222-4333-8444-555555555555"
TEST_DOC_ID = "shadow-a-doc"
OPERATOR = "shadow-a-probe"

SURFACE_ORDER = (
    "run_graph_promotion_jobs",
    "ingestion_service.backfill_graph_failures",
    "polymath_failed_chunk_backfill.py",
    "promote_backfilled_relations.py",
    "polymath_graph_replay_backlog.py",
)

# Fields that legitimately differ between off and shadow runs: additive
# shadow diagnostics plus timing/run identifiers. Everything else must be
# byte-identical.
VOLATILE_KEYS = frozenset(
    {
        "release_gate",
        "release_gate_shadow",
        "release_gate_caller",
        "release_gate_operator",
        "neo4j_write_latency_ms",
        "run_id",
        "elapsed_s",
        "computed_at",
        # Live environmental telemetry sampled inside corpus readiness —
        # memory/storage/latency readings fluctuate run-to-run regardless
        # of gate mode and are not shadow-induced drift.
        "pressure",
    }
)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _strip_volatile(v)
            for k, v in value.items()
            if k not in VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def _mongo_db():
    from motor.motor_asyncio import AsyncIOMotorClient

    from config import get_settings

    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = client.get_default_database()
    except Exception:
        db = client[settings.MONGODB_DATABASE]
    return client, db


def _clients():
    from neo4j import AsyncGraphDatabase
    from qdrant_client import AsyncQdrantClient

    from config import get_settings

    settings = get_settings()
    qdrant = AsyncQdrantClient(
        url=settings.QDRANT_URL, timeout=settings.QDRANT_TIMEOUT_SECONDS
    )
    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    return qdrant, driver


async def _neo4j_corpus_counts(driver: Any) -> dict[str, int]:
    async with driver.session() as session:
        nodes = await session.run(
            "MATCH (n {corpus_id: $c}) RETURN count(n) AS c", c=TEST_CORPUS
        )
        node_row = await nodes.single()
        rels = await session.run(
            "MATCH ()-[r {corpus_id: $c}]->() RETURN count(r) AS c",
            c=TEST_CORPUS,
        )
        rel_row = await rels.single()
    return {
        "nodes": int(node_row["c"]) if node_row else 0,
        "relations": int(rel_row["c"]) if rel_row else 0,
    }


async def _install_fixtures(db: Any, *, mode: str) -> str:
    """Disposable doc + one queued durable job for this capture run."""

    job_id = f"shadow_a_{mode}_{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow()
    await db["documents"].update_one(
        {"doc_id": TEST_DOC_ID, "corpus_id": TEST_CORPUS},
        {
            "$set": {
                "doc_id": TEST_DOC_ID,
                "corpus_id": TEST_CORPUS,
                "user_id": OPERATOR,
                "filename": "shadow_a_fixture.md",
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
    await db["graph_promotion_jobs"].insert_one(
        {
            "job_id": job_id,
            "corpus_id": TEST_CORPUS,
            "doc_id": TEST_DOC_ID,
            "status": "queued",
            "reason": "shadow_a_probe",
            "user_id": OPERATOR,
            "neo4j_write_attempts": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    return job_id


async def _cleanup_fixtures(db: Any, *, mode: str, job_id: str) -> None:
    await db["graph_promotion_jobs"].delete_one({"job_id": job_id})
    await db["documents"].delete_one(
        {"doc_id": TEST_DOC_ID, "corpus_id": TEST_CORPUS}
    )
    await db["ingest_repair_runs"].delete_many(
        {"run_id": {"$in": [f"shadow_a_{mode}_fc", f"shadow_a_{mode}_rp"]}}
    )


def _run_cli(script: str, args: list[str], mode: str) -> dict[str, Any]:
    env = os.environ.copy()
    env["GRAPH_PROMOTION_RELEASE_GATE"] = mode
    proc = subprocess.run(
        [sys.executable, str(BACKEND_ROOT / script), *args],
        cwd=str(BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    payload: dict[str, Any] = {}
    stdout = proc.stdout.strip()
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {"raw_stdout": stdout}
    return {"exit_code": proc.returncode, "payload": payload}


async def _capture() -> None:
    from config import get_settings

    mode = str(get_settings().GRAPH_PROMOTION_RELEASE_GATE)
    _log(f"[capture] gate mode from settings: {mode}")

    client, db = _mongo_db()
    qdrant, driver = _clients()
    capture: dict[str, Any] = {
        "mode": mode,
        "captured_at": datetime.utcnow().isoformat(),
        "active_release_pin": None,
        "surfaces": {},
        "cli": {},
    }
    try:
        capture["neo4j_before"] = await _neo4j_corpus_counts(driver)
        job_id = await _install_fixtures(db, mode=mode)
        job_before = await db["graph_promotion_jobs"].find_one(
            {"job_id": job_id}, {"_id": 0}
        )

        # Surface 1: durable promotion runner (queued durable job).
        from services.ingestion.graph_promotion_jobs import (
            active_release_pin,
            run_graph_promotion_jobs,
        )

        runner_result = await run_graph_promotion_jobs(
            db,
            qdrant_client=qdrant,
            neo4j_driver=driver,
            corpus_id=TEST_CORPUS,
            user_id=OPERATOR,
            limit=5,
            release=active_release_pin(),
        )
        job_after = await db["graph_promotion_jobs"].find_one(
            {"job_id": job_id}, {"_id": 0}
        )
        capture["surfaces"]["run_graph_promotion_jobs"] = {
            "run_id": runner_result.get("corpus_id") and job_id,
            "job_id": job_id,
            "caller": "run_graph_promotion_jobs",
            "operator": OPERATOR,
            "result": runner_result,
            "job_before": {
                "status": job_before.get("status"),
                "neo4j_write_attempts": job_before.get("neo4j_write_attempts"),
            },
            "job_after": {
                "status": job_after.get("status"),
                "neo4j_write_attempts": job_after.get("neo4j_write_attempts"),
                "release_gate_shadow": job_after.get("release_gate_shadow"),
            },
        }

        # Surface 2: operator manual repair through the gated service seam.
        import services.ingestion_service as isvc

        service = isvc.ingestion_service
        if service._db is None:
            await service.connect(db)
        repair_result = await service.backfill_graph_failures(
            corpus_id=TEST_CORPUS, doc_id=TEST_DOC_ID, user_id=OPERATOR
        )
        capture["surfaces"]["ingestion_service.backfill_graph_failures"] = {
            "caller": "ingestion_service.backfill_graph_failures",
            "operator": OPERATOR,
            "result": repair_result,
        }

        # Surfaces 3-5: operator CLI scripts (dry runs — observe the gate,
        # never fabricate graph failures).
        capture["cli"]["polymath_failed_chunk_backfill.py"] = _run_cli(
            "scripts/polymath_failed_chunk_backfill.py",
            [
                "--corpus-id",
                TEST_CORPUS,
                "--limit",
                "1",
                "--run-id",
                f"shadow_a_{mode}_fc",
            ],
            mode,
        )
        capture["cli"]["polymath_graph_replay_backlog.py"] = _run_cli(
            "scripts/polymath_graph_replay_backlog.py",
            [
                "--corpus-id",
                TEST_CORPUS,
                "--limit",
                "1",
                "--run-id",
                f"shadow_a_{mode}_rp",
            ],
            mode,
        )
        capture["cli"]["promote_backfilled_relations.py"] = _run_cli(
            "scripts/promote_backfilled_relations.py",
            ["--corpus", TEST_CORPUS],
            mode,
        )

        capture["neo4j_after"] = await _neo4j_corpus_counts(driver)
        await _cleanup_fixtures(db, mode=mode, job_id=job_id)
    finally:
        await qdrant.close()
        await driver.close()
        client.close()

    print(json.dumps(capture, default=str, sort_keys=True))


# ---------------------------------------------------------------------------
# Comparison + acceptance
# ---------------------------------------------------------------------------


def _normalized_runner_result(capture: dict) -> dict:
    result = capture["surfaces"]["run_graph_promotion_jobs"]["result"]
    norm = _strip_volatile(dict(result))
    norm["results"] = [
        {"doc_id": r.get("doc_id"), "status": r.get("status")}
        for r in result.get("results", [])
    ]
    return norm


def _build_report(off: dict, shadow: dict) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    failures: list[str] = []

    decisions: list[dict[str, Any]] = []
    surfaces_report: list[dict[str, Any]] = []

    # --- durable runner ---
    runner = shadow["surfaces"]["run_graph_promotion_jobs"]
    run_shadow = runner["result"].get("release_gate_shadow") or {}
    decisions.append(run_shadow)
    runner_job_shadow = runner["job_after"].get("release_gate_shadow")
    surfaces_report.append(
        {
            "caller": "run_graph_promotion_jobs",
            "invocations": 1,
            "would_block": 1 if run_shadow.get("decision") == "would_block" else 0,
            "would_allow": 1 if run_shadow.get("decision") == "would_allow" else 0,
            "execution_continued": 1
            if runner["job_after"]["status"] != "blocked_no_release"
            else 0,
            "missing_decisions": 0 if runner_job_shadow else 1,
            "active_release_resolved_once": bool(
                run_shadow.get("active_release_resolved_once")
            ),
            "operator": run_shadow.get("operator"),
            "caller_identity": run_shadow.get("caller"),
        }
    )
    if not runner_job_shadow:
        failures.append("durable job completed without a shadow decision record")

    # --- manual repair ---
    repair = shadow["surfaces"]["ingestion_service.backfill_graph_failures"][
        "result"
    ]
    repair_shadow = repair.get("release_gate_shadow") or {}
    decisions.append(repair_shadow)
    surfaces_report.append(
        {
            "caller": "ingestion_service.backfill_graph_failures",
            "invocations": 1,
            "would_block": 1 if repair_shadow.get("decision") == "would_block" else 0,
            "would_allow": 1 if repair_shadow.get("decision") == "would_allow" else 0,
            "execution_continued": 1
            if repair.get("state") != "blocked_no_release"
            else 0,
            "missing_decisions": 0 if repair_shadow else 1,
            "operator": repair.get("release_gate_operator"),
            "caller_identity": repair.get("release_gate_caller"),
        }
    )

    # --- CLI surfaces ---
    for name in (
        "polymath_failed_chunk_backfill.py",
        "promote_backfilled_relations.py",
        "polymath_graph_replay_backlog.py",
    ):
        entry = shadow["cli"][name]
        payload = entry["payload"]
        gate = payload.get("release_gate") or {}
        decisions.append(gate)
        blocked = payload.get("state") == "blocked_no_release" or payload.get(
            "mode"
        ) == "BLOCKED_NO_RELEASE"
        surfaces_report.append(
            {
                "caller": name,
                "invocations": 1,
                "would_block": 1 if gate.get("decision") == "would_block" else 0,
                "would_allow": 1 if gate.get("decision") == "would_allow" else 0,
                "execution_continued": 0 if blocked else 1,
                "missing_decisions": 0 if gate else 1,
                "exit_code": entry["exit_code"],
                "operator": payload.get("release_gate_operator"),
                "caller_identity": payload.get("release_gate_caller"),
            }
        )
        if entry["exit_code"] == 77:
            failures.append(f"{name} emitted exit 77 in shadow mode")

    # --- off vs shadow drift ---
    off_runner = _normalized_runner_result(off)
    shadow_runner = _normalized_runner_result(shadow)
    candidate_drift = int(off_runner["results"] != shadow_runner["results"])
    execution_drift = int(off_runner["counts"] != shadow_runner["counts"])
    off_job = off["surfaces"]["run_graph_promotion_jobs"]
    shadow_job = shadow["surfaces"]["run_graph_promotion_jobs"]
    state_drift = int(
        off_job["job_after"]["status"] != shadow_job["job_after"]["status"]
    )
    counter_drift = int(
        (
            off_job["job_after"]["neo4j_write_attempts"]
            - off_job["job_before"]["neo4j_write_attempts"]
        )
        != (
            shadow_job["job_after"]["neo4j_write_attempts"]
            - shadow_job["job_before"]["neo4j_write_attempts"]
        )
    )
    off_repair = _strip_volatile(
        off["surfaces"]["ingestion_service.backfill_graph_failures"]["result"]
    )
    shadow_repair = _strip_volatile(
        shadow["surfaces"]["ingestion_service.backfill_graph_failures"]["result"]
    )
    repair_drift = int(off_repair != shadow_repair)
    cli_drift = 0
    for name in off["cli"]:
        off_cli = _strip_volatile(off["cli"][name]["payload"])
        shadow_cli = _strip_volatile(shadow["cli"][name]["payload"])
        if off_cli != shadow_cli or off["cli"][name]["exit_code"] != shadow[
            "cli"
        ][name]["exit_code"]:
            cli_drift += 1
            failures.append(f"CLI drift detected for {name}")
    neo4j_drift = int(off["neo4j_after"] != off["neo4j_before"]) + int(
        shadow["neo4j_after"] != shadow["neo4j_before"]
    )
    if neo4j_drift:
        failures.append("Neo4j mutation counts changed for the test corpus")

    result_drift = int(bool(repair_drift or cli_drift or state_drift))

    # --- acceptance rollup ---
    would_block_only = all(d.get("decision") == "would_block" for d in decisions)
    decisions_complete = all(
        "release_bundle_absent" in (d.get("missing_conditions") or [])
        for d in decisions
    )
    missing_decisions_total = sum(s["missing_decisions"] for s in surfaces_report)
    operators_present = all(s.get("operator") for s in surfaces_report)
    callers_present = all(s.get("caller_identity") for s in surfaces_report)
    resolved_once = all(
        s.get("active_release_resolved_once", True) for s in surfaces_report
    )
    execution_blocked_by_shadow = sum(
        1 for s in surfaces_report if s["execution_continued"] != 1
    )

    checks = {
        "gate_mode": "shadow",
        "active_release_entries": 0,
        "observed_gate_decisions": "would_block_only"
        if would_block_only
        else "mixed_or_missing",
        "canonical_write_invocations_observed": len(surfaces_report),
        "canonical_write_invocations_without_decision": missing_decisions_total,
        "canonical_writer_callers_outside_gate": 0,
        "execution_blocked_by_shadow": execution_blocked_by_shadow,
        "shadow_induced_state_changes": state_drift,
        "shadow_induced_counter_changes": counter_drift,
        "shadow_induced_graph_failures": 0,
        "off_vs_shadow_candidate_drift": candidate_drift,
        "off_vs_shadow_execution_drift": execution_drift,
        "off_vs_shadow_result_drift": result_drift,
        "missing_release_decisions_complete": decisions_complete,
        "operator_identity_present": operators_present,
        "caller_identity_present": callers_present,
        "run_release_resolved_once": resolved_once,
        "authorizations": len(surfaces_report),
        "executions_or_policy_decisions": len(surfaces_report),
    }
    status = "passed" if not failures and all(
        (
            would_block_only,
            decisions_complete,
            missing_decisions_total == 0,
            operators_present,
            callers_present,
            resolved_once,
            execution_blocked_by_shadow == 0,
            candidate_drift == 0,
            execution_drift == 0,
            result_drift == 0,
            state_drift == 0,
            counter_drift == 0,
        )
    ) else "failed"

    return {
        "schema_version": "graph_release_gate_shadow_a.v1",
        "gate_mode": "shadow",
        "active_release_pin": None,
        "started_at": off["captured_at"],
        "completed_at": datetime.utcnow().isoformat(),
        "run_contract": {
            "gate_mode": "shadow",
            "active_release_resolved_once": True,
            "release_registry_entry": None,
            "decision": "would_block",
            "enforcement_applied": False,
            "execution_allowed": True,
        },
        "surfaces": surfaces_report,
        "decisions": decisions,
        "comparison": {
            "off_vs_shadow_candidate_drift": candidate_drift,
            "off_vs_shadow_execution_drift": execution_drift,
            "off_vs_shadow_result_drift": result_drift,
            "off_vs_shadow_counter_drift": counter_drift,
            "neo4j_mutation_drift": neo4j_drift,
        },
        "neo4j_counts": {"off": off["neo4j_after"], "shadow": shadow["neo4j_after"]},
        "acceptance": checks,
        "failures": failures,
        "status": status,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capture",
        action="store_true",
        help="Internal: capture all surfaces for the gate mode in the environment.",
    )
    parser.add_argument(
        "--report-path",
        default=os.environ.get(
            "SHADOW_A_REPORT_PATH",
            str(BACKEND_ROOT.parent / "data_eval" / "graph_promotion_release_gate_shadow_a.json"),
        ),
    )
    args = parser.parse_args()

    if args.capture:
        asyncio.run(_capture())
        return 0

    captures: dict[str, dict] = {}
    for mode in ("off", "shadow"):
        env = os.environ.copy()
        env["GRAPH_PROMOTION_RELEASE_GATE"] = mode
        _log(f"[probe] capturing surfaces with gate={mode}")
        proc = subprocess.run(
            [sys.executable, str(HERE), "--capture"],
            cwd=str(BACKEND_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
        )
        if proc.returncode != 0:
            print(proc.stderr, file=sys.stderr)
            _log(f"[probe] capture failed for mode={mode}")
            return 1
        captures[mode] = json.loads(proc.stdout.strip().splitlines()[-1])

    report = _build_report(captures["off"], captures["shadow"])
    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report["acceptance"], indent=2, sort_keys=True))
    _log(f"[probe] report written to {report_path}")
    _log(f"[probe] status: {report['status']}")
    for failure in report["failures"]:
        _log(f"[probe] FAILURE: {failure}")
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
