#!/usr/bin/env python3
"""Shadow B probe for the canonical graph-write release gate.

Shadow B proves the registry-backed decision paths:

  * exactly one valid active release  -> would_allow, execution continues
  * incomplete/failed active release  -> would_block, execution continues
  * registry resolved once per run; decisions never mix within one run
  * shadow never changes execution vs the off baseline

Runs four capture phases against the live stack on a disposable corpus:

  baseline_off    gate=off, no active release
  shadow_allow    gate=shadow + registry with one fully-passed active pin
  shadow_block    gate=shadow + registry with one failing active pin
  shadow_mixed    gate=shadow + valid pin + multiple queued jobs (one run)

Report: data_eval/graph_promotion_release_gate_shadow_b.json

Usage (inside the backend container):
    python scripts/shadow_b_graph_release_gate_probe.py
    python scripts/shadow_b_graph_release_gate_probe.py --capture PHASE REGISTRY
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
BACKEND_ROOT = HERE.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.control_plane.release_registry import compute_entry_hash  # noqa: E402

# Reuse the Shadow A capture harness (fixtures, CLI runner, Neo4j counts).
_spec = importlib.util.spec_from_file_location(
    "shadow_a_probe", BACKEND_ROOT / "scripts" / "shadow_a_graph_release_gate_probe.py"
)
sa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sa)

TEST_CORPUS = sa.TEST_CORPUS

PHASES = ("baseline_off", "shadow_allow", "shadow_block", "shadow_mixed")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Registry fixtures
# ---------------------------------------------------------------------------


def passing_pin() -> dict[str, Any]:
    return {
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


def write_registry_fixture(
    directory: Path, *, pin_overrides: dict[str, Any] | None
) -> Path:
    """Registry with exactly one active entry; None -> empty entries list."""

    if pin_overrides is None:
        entries: list[dict[str, Any]] = []
    else:
        pin = passing_pin()
        pin.update(pin_overrides)
        entry = {
            "entry_id": "release:extraction-core-v1.0.0",
            "status": "active",
            "issued_at": "2026-08-03T00:00:00Z",
            "release_pin": pin,
        }
        entry["entry_hash"] = compute_entry_hash(entry)
        entries = [entry]
    path = directory / "release_pins.v1.json"
    path.write_text(
        json.dumps({"schema_version": "release_pins.v1", "entries": entries}),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Capture phase (subprocess entrypoint)
# ---------------------------------------------------------------------------


async def _capture(phase: str, registry_path: str, extra_jobs: int) -> None:
    import asyncio

    from config import get_settings

    gate_mode = str(get_settings().GRAPH_PROMOTION_RELEASE_GATE)

    # Point the fail-closed loader at this phase's registry fixture for all
    # in-process surfaces (runner + manual repair seam).
    import services.control_plane.release_registry as release_registry

    fixture = Path(registry_path)
    release_registry.resolve_registry_path = lambda explicit=None: fixture

    # Resolve-once instrumentation: count loader resolutions for the run.
    load_calls = {"n": 0}
    real_load = release_registry.load_release_registry

    def counting_load(path=None):
        load_calls["n"] += 1
        return real_load(path)

    release_registry.load_release_registry = counting_load

    client, db = sa._mongo_db()
    qdrant, driver = sa._clients()
    capture: dict[str, Any] = {
        "phase": phase,
        "gate_mode": gate_mode,
        "registry_path": str(fixture),
        "captured_at": datetime.utcnow().isoformat(),
        "surfaces": {},
        "cli": {},
    }
    job_ids: list[str] = []
    try:
        capture["neo4j_before"] = await _neo4j_counts(driver)
        job_id = await sa._install_fixtures(db, mode=phase)
        job_ids.append(job_id)
        for i in range(extra_jobs):
            extra = await sa._install_fixtures(db, mode=f"{phase}_extra{i}")
            job_ids.append(extra)

        from services.ingestion.graph_promotion_jobs import run_graph_promotion_jobs

        load_calls["n"] = 0  # measure the runner run only
        runner_result = await run_graph_promotion_jobs(
            db,
            qdrant_client=qdrant,
            neo4j_driver=driver,
            corpus_id=TEST_CORPUS,
            user_id=sa.OPERATOR,
            limit=10,
        )
        capture["runner_load_calls"] = load_calls["n"]
        capture["surfaces"]["run_graph_promotion_jobs"] = {
            "caller": "run_graph_promotion_jobs",
            "operator": sa.OPERATOR,
            "result": runner_result,
        }

        import services.ingestion_service as isvc

        service = isvc.ingestion_service
        if service._db is None:
            await service.connect(db)
        repair_result = await service.backfill_graph_failures(
            corpus_id=TEST_CORPUS, doc_id=sa.TEST_DOC_ID, user_id=sa.OPERATOR
        )
        capture["surfaces"]["ingestion_service.backfill_graph_failures"] = {
            "caller": "ingestion_service.backfill_graph_failures",
            "operator": sa.OPERATOR,
            "result": repair_result,
        }

        env_registry = str(fixture)
        capture["cli"]["polymath_failed_chunk_backfill.py"] = _run_cli(
            "scripts/polymath_failed_chunk_backfill.py",
            ["--corpus-id", TEST_CORPUS, "--limit", "1", "--run-id", f"shadow_b_{phase}_fc"],
            gate_mode,
            env_registry,
        )
        capture["cli"]["polymath_graph_replay_backlog.py"] = _run_cli(
            "scripts/polymath_graph_replay_backlog.py",
            ["--corpus-id", TEST_CORPUS, "--limit", "1", "--run-id", f"shadow_b_{phase}_rp"],
            gate_mode,
            env_registry,
        )
        capture["cli"]["promote_backfilled_relations.py"] = _run_cli(
            "scripts/promote_backfilled_relations.py",
            ["--corpus", TEST_CORPUS],
            gate_mode,
            env_registry,
        )

        capture["neo4j_after"] = await _neo4j_counts(driver)
    finally:
        for jid in job_ids:
            await db["graph_promotion_jobs"].delete_one({"job_id": jid})
        await db["documents"].delete_one(
            {"doc_id": sa.TEST_DOC_ID, "corpus_id": TEST_CORPUS}
        )
        await db["ingest_repair_runs"].delete_many(
            {"run_id": {"$regex": f"^shadow_b_{phase}"}}
        )
        await qdrant.close()
        await driver.close()
        client.close()

    print(json.dumps(capture, default=str, sort_keys=True))


async def _neo4j_counts(driver: Any) -> dict[str, int]:
    return await sa._neo4j_corpus_counts(driver)


def _run_cli(script: str, args: list[str], gate_mode: str, registry_path: str):
    env = os.environ.copy()
    env["GRAPH_PROMOTION_RELEASE_GATE"] = gate_mode
    env["RELEASE_REGISTRY_PATH"] = registry_path
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


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _surface_decisions(capture: dict) -> dict[str, str]:
    """Decision observed per canonical surface in one phase."""

    decisions: dict[str, str] = {}
    runner_shadow = (
        capture["surfaces"]["run_graph_promotion_jobs"]["result"].get(
            "release_gate_shadow"
        )
        or {}
    )
    decisions["run_graph_promotion_jobs"] = runner_shadow.get("decision", "absent")
    repair = capture["surfaces"]["ingestion_service.backfill_graph_failures"]["result"]
    decisions["ingestion_service.backfill_graph_failures"] = (
        repair.get("release_gate_shadow") or {}
    ).get("decision", "absent")
    for name, entry in capture["cli"].items():
        decisions[name] = (entry["payload"].get("release_gate") or {}).get(
            "decision", "absent"
        )
    return decisions


def _normalized_runner(capture: dict) -> dict:
    result = capture["surfaces"]["run_graph_promotion_jobs"]["result"]
    norm = sa._strip_volatile(dict(result))
    norm["results"] = sorted(
        (r.get("doc_id"), r.get("status")) for r in result.get("results", [])
    )
    return norm


def _runner_results(capture: dict) -> list[dict]:
    return capture["surfaces"]["run_graph_promotion_jobs"]["result"].get("results", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", nargs=3, metavar=("PHASE", "REGISTRY", "EXTRA_JOBS"))
    parser.add_argument(
        "--report-path",
        default=os.environ.get(
            "SHADOW_B_REPORT_PATH",
            str(BACKEND_ROOT.parent / "data_eval" / "graph_promotion_release_gate_shadow_b.json"),
        ),
    )
    args = parser.parse_args()

    if args.capture:
        import asyncio

        phase, registry_path, extra_jobs = args.capture
        asyncio.run(_capture(phase, registry_path, int(extra_jobs)))
        return 0

    captures: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="shadow_b_registry_") as tmp:
        tmp_path = Path(tmp)
        allow_registry = tmp_path / "allow"
        block_registry = tmp_path / "block"
        allow_registry.mkdir()
        block_registry.mkdir()
        # Shadow B fixtures: complete valid active pin vs failing pin.
        allow_file = write_registry_fixture(allow_registry, pin_overrides={})
        block_file = write_registry_fixture(
            block_registry, pin_overrides={"calibration": "pending"}
        )
        plan = [
            ("baseline_off", allow_file, "off", 0),
            ("shadow_allow", allow_file, "shadow", 0),
            ("shadow_block", block_file, "shadow", 0),
            ("shadow_mixed", allow_file, "shadow", 2),
        ]
        for phase, registry, gate_mode, extra_jobs in plan:
            env = os.environ.copy()
            env["GRAPH_PROMOTION_RELEASE_GATE"] = gate_mode
            _log(f"[probe] capturing phase={phase} gate={gate_mode}")
            proc = subprocess.run(
                [sys.executable, str(HERE), "--capture", phase, str(registry), str(extra_jobs)],
                cwd=str(BACKEND_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=900,
            )
            if proc.returncode != 0:
                print(proc.stderr, file=sys.stderr)
                _log(f"[probe] capture failed for phase={phase}")
                return 1
            captures[phase] = json.loads(proc.stdout.strip().splitlines()[-1])

    failures: list[str] = []

    allow_decisions = _surface_decisions(captures["shadow_allow"])
    block_decisions = _surface_decisions(captures["shadow_block"])

    valid_release_would_allow = all(
        d == "would_allow" for d in allow_decisions.values()
    )
    invalid_release_would_block = all(
        d == "would_block" for d in block_decisions.values()
    )
    if not valid_release_would_allow:
        failures.append(f"shadow_allow decisions: {allow_decisions}")
    if not invalid_release_would_block:
        failures.append(f"shadow_block decisions: {block_decisions}")

    # Registry resolved once per run: one resolution for the multi-job
    # runner run; every CLI script evaluates the gate exactly once per
    # process (single seam call in each script — static proof below).
    resolved_once = captures["shadow_allow"]["runner_load_calls"] == 1 and captures[
        "shadow_mixed"
    ]["runner_load_calls"] == 1
    if not resolved_once:
        failures.append(
            "registry resolved more than once during a runner run: "
            f"allow={captures['shadow_allow']['runner_load_calls']} "
            f"mixed={captures['shadow_mixed']['runner_load_calls']}"
        )
    seam_calls: dict[str, int] = {}
    for script in (
        "scripts/polymath_failed_chunk_backfill.py",
        "scripts/polymath_graph_replay_backlog.py",
        "scripts/promote_backfilled_relations.py",
    ):
        text = (BACKEND_ROOT / script).read_text(encoding="utf-8")
        seam_calls[script] = text.count("evaluate_cli_graph_write_gate(")
    if any(n != 1 for n in seam_calls.values()):  # exactly one gate call per run
        failures.append(f"CLI seam call counts unexpected: {seam_calls}")

    # Mixed decisions within one run must be impossible.
    mixed_decisions = 0
    mixed_runner = _runner_results(captures["shadow_mixed"])
    run_shadow = captures["shadow_mixed"]["surfaces"]["run_graph_promotion_jobs"][
        "result"
    ].get("release_gate_shadow") or {}
    per_job_decisions = {
        (r.get("release_gate_shadow") or {}).get("decision")
        for r in mixed_runner
        if isinstance(r.get("release_gate_shadow"), dict)
    }
    per_job_decisions.discard(None)
    if len(per_job_decisions) > 1:
        mixed_decisions += len(per_job_decisions) - 1
    if run_shadow.get("decision") and per_job_decisions and run_shadow.get(
        "decision"
    ) not in per_job_decisions:
        mixed_decisions += 1
    if mixed_decisions:
        failures.append("mixed release decisions observed within one run")

    # Shadow execution drift vs the off baseline (additive diagnostics only).
    off_runner = _normalized_runner(captures["baseline_off"])
    drift = 0
    for phase in ("shadow_allow", "shadow_block"):
        phase_runner = _normalized_runner(captures[phase])
        if phase_runner != off_runner:
            drift += 1
            failures.append(f"runner drift vs off baseline in {phase}")
        off_repair = sa._strip_volatile(
            captures["baseline_off"]["surfaces"][
                "ingestion_service.backfill_graph_failures"
            ]["result"]
        )
        phase_repair = sa._strip_volatile(
            captures[phase]["surfaces"][
                "ingestion_service.backfill_graph_failures"
            ]["result"]
        )
        if off_repair != phase_repair:
            drift += 1
            failures.append(f"manual-repair drift vs off baseline in {phase}")
        for name in captures["baseline_off"]["cli"]:
            off_cli = sa._strip_volatile(
                captures["baseline_off"]["cli"][name]["payload"]
            )
            phase_cli = sa._strip_volatile(captures[phase]["cli"][name]["payload"])
            if off_cli != phase_cli or captures["baseline_off"]["cli"][name][
                "exit_code"
            ] != captures[phase]["cli"][name]["exit_code"]:
                drift += 1
                failures.append(f"CLI drift vs off baseline: {phase}/{name}")
    neo4j_drift = sum(
        1
        for phase in PHASES
        if captures[phase]["neo4j_before"] != captures[phase]["neo4j_after"]
    )
    if neo4j_drift:
        drift += neo4j_drift
        failures.append("Neo4j mutation counts changed for the test corpus")

    unobserved = sum(
        1
        for decisions in (allow_decisions, block_decisions)
        for decision in decisions.values()
        if decision == "absent"
    )
    if unobserved:
        failures.append(f"{unobserved} write surface invocations without a decision")

    acceptance = {
        "valid_release_would_allow": "passed" if valid_release_would_allow else "failed",
        "invalid_release_would_block": "passed" if invalid_release_would_block else "failed",
        "registry_resolved_once_per_run": "passed" if resolved_once else "failed",
        "mixed_release_decisions_in_one_run": mixed_decisions,
        "shadow_execution_drift": drift,
        "unobserved_write_surfaces": unobserved,
    }
    status = (
        "passed"
        if not failures
        and valid_release_would_allow
        and invalid_release_would_block
        and resolved_once
        and mixed_decisions == 0
        and drift == 0
        and unobserved == 0
        else "failed"
    )

    report = {
        "schema_version": "graph_release_gate_shadow_b.v1",
        "gate_mode": "shadow",
        "started_at": captures["baseline_off"]["captured_at"],
        "completed_at": datetime.utcnow().isoformat(),
        "phases": {
            phase: {
                "gate_mode": captures[phase]["gate_mode"],
                "decisions": _surface_decisions(captures[phase])
                if phase != "baseline_off"
                else {},
                "runner_load_calls": captures[phase].get("runner_load_calls"),
            }
            for phase in PHASES
        },
        "allow_phase_registry_identity": (
            captures["shadow_allow"]["surfaces"]["run_graph_promotion_jobs"][
                "result"
            ].get("release_gate_shadow")
            or {}
        ).get("registry_identity"),
        "block_phase_missing_conditions": (
            captures["shadow_block"]["surfaces"]["run_graph_promotion_jobs"][
                "result"
            ].get("release_gate_shadow")
            or {}
        ).get("missing_conditions"),
        "acceptance": acceptance,
        "failures": failures,
        "status": status,
    }
    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(acceptance, indent=2, sort_keys=True))
    _log(f"[probe] report written to {report_path}")
    _log(f"[probe] status: {status}")
    for failure in failures:
        _log(f"[probe] FAILURE: {failure}")
    return 0 if status == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
