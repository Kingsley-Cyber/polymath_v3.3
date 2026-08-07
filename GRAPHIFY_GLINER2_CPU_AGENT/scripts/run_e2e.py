#!/usr/bin/env python3
"""Execute repository-specific Graphify commands over both fixed fixtures and aggregate evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

PACK_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = {
    "quality": PACK_ROOT / "fixtures" / "graphify_quality_fixture.md",
    "throughput": PACK_ROOT / "fixtures" / "graphify_throughput_fixture.md",
}


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def render(command: str, context: dict[str, Any]) -> str:
    return command.format(**{key: str(value) for key, value in context.items()})


def run_shell(command: str, *, cwd: Path, env: dict[str, str], timeout: int, log_path: Path) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        exit_code = result.returncode
        output = result.stdout
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        output = (exc.stdout or "") + "\nTIMEOUT\n"
        timed_out = True
    elapsed = time.perf_counter() - start
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8", errors="replace")
    return {"command": command, "exit_code": exit_code, "wall_seconds": elapsed, "timed_out": timed_out, "log": str(log_path)}


def load_report(path: Path, required_keys: list[str]) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"Expected report was not created: {path}")
    report = json.loads(path.read_text(encoding="utf-8"))
    missing = [key for key in required_keys if key not in report]
    if missing:
        raise RuntimeError(f"Report {path} is missing keys: {missing}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(PACK_ROOT / ".agent_state" / "run_config.json"))
    parser.add_argument("--output-dir")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.is_file():
        raise SystemExit(f"Run config missing: {config_path}. Copy templates/run_config.template.json and populate actual commands.")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if "REPLACE_WITH" in json.dumps(config):
        raise SystemExit("Run config still contains REPLACE_WITH placeholders")

    repo_root = (PACK_ROOT / config.get("repo_root", "..")).resolve() if not Path(config.get("repo_root", "..")).is_absolute() else Path(config["repo_root"]).resolve()
    if not repo_root.is_dir():
        raise SystemExit(f"Repository root does not exist: {repo_root}")
    run_root = Path(args.output_dir).resolve() if args.output_dir else PACK_ROOT / "artifacts" / "e2e" / timestamp()
    run_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in config.get("environment", {}).items()})
    timeout = int(config.get("timeout_seconds", 7200))
    required_keys = list(config.get("required_report_keys", []))
    command_receipts = []

    base_context = {
        "quality_fixture": FIXTURES["quality"],
        "throughput_fixture": FIXTURES["throughput"],
        "output_dir": run_root,
        "repo_root": repo_root,
    }

    for index, command in enumerate(config.get("commands", {}).get("preflight", []), 1):
        rendered = render(command, base_context)
        receipt = run_shell(rendered, cwd=repo_root, env=env, timeout=timeout, log_path=run_root / "logs" / f"preflight_{index}.log")
        command_receipts.append(receipt)
        if receipt["exit_code"] != 0:
            raise SystemExit(f"Preflight command failed: {rendered}")

    runs: dict[str, dict[str, list[dict[str, Any]]]] = {"candidate": {"quality": [], "throughput": []}, "baseline": {"quality": [], "throughput": []}}
    providers = ["candidate"]
    if config.get("run_baseline", True):
        providers.append("baseline")

    for provider in providers:
        repetitions = int(config.get("candidate_repetitions", 2)) if provider == "candidate" else 1
        for fixture_name, fixture_path in FIXTURES.items():
            command_template = config["commands"].get(provider, {}).get(fixture_name, "").strip()
            if not command_template:
                if provider == "baseline":
                    continue
                raise SystemExit(f"No {provider}.{fixture_name} command configured")
            for run_index in range(1, repetitions + 1):
                namespace = f"agent_{provider}_{fixture_name}_{run_index}_{timestamp()}"
                report_json = run_root / "reports" / f"{provider}_{fixture_name}_{run_index}.json"
                report_json.parent.mkdir(parents=True, exist_ok=True)
                context = {
                    **base_context,
                    "fixture": fixture_path,
                    "fixture_name": fixture_name,
                    "provider": provider,
                    "run_index": run_index,
                    "namespace": namespace,
                    "report_json": report_json,
                }
                command = render(command_template, context)
                receipt = run_shell(command, cwd=repo_root, env=env, timeout=timeout, log_path=run_root / "logs" / f"{provider}_{fixture_name}_{run_index}.log")
                command_receipts.append(receipt)
                if receipt["exit_code"] != 0:
                    raise SystemExit(f"Command failed: {command}")
                report = load_report(report_json, required_keys)
                runs[provider][fixture_name].append({
                    "namespace": namespace,
                    "report_path": str(report_json),
                    "wall_seconds": receipt["wall_seconds"],
                    "report": report,
                })

    idempotency = {}
    for fixture_name in FIXTURES:
        candidate_runs = runs["candidate"][fixture_name]
        if len(candidate_runs) < 2:
            idempotency[fixture_name] = {"passed": False, "reason": "fewer than two candidate runs"}
            continue
        first = candidate_runs[0]["report"]
        second = candidate_runs[1]["report"]
        passed = first.get("identity_digest") == second.get("identity_digest") and first.get("counts") == second.get("counts")
        idempotency[fixture_name] = {
            "passed": passed,
            "first_digest": first.get("identity_digest"),
            "second_digest": second.get("identity_digest"),
            "counts_equal": first.get("counts") == second.get("counts"),
        }
        if not passed:
            raise SystemExit(f"Idempotency failed for {fixture_name}")

    optional_context = {**base_context}
    for fixture_name in FIXTURES:
        if runs["candidate"][fixture_name]:
            optional_context[f"candidate_{fixture_name}_namespace"] = runs["candidate"][fixture_name][0]["namespace"]
            optional_context[f"candidate_{fixture_name}_report"] = runs["candidate"][fixture_name][0]["report_path"]

    for key in ["projection_rebuild", "projection_verify"]:
        template = config.get("commands", {}).get(key, "").strip()
        if template:
            command = render(template, optional_context)
            receipt = run_shell(command, cwd=repo_root, env=env, timeout=timeout, log_path=run_root / "logs" / f"{key}.log")
            command_receipts.append(receipt)
            if receipt["exit_code"] != 0:
                raise SystemExit(f"{key} command failed")

    for index, command in enumerate(config.get("commands", {}).get("postflight", []), 1):
        rendered = render(command, optional_context)
        receipt = run_shell(rendered, cwd=repo_root, env=env, timeout=timeout, log_path=run_root / "logs" / f"postflight_{index}.log")
        command_receipts.append(receipt)
        if receipt["exit_code"] != 0:
            raise SystemExit(f"Postflight command failed: {rendered}")

    quality = runs["candidate"]["quality"][0]["report"]
    throughput = runs["candidate"]["throughput"][0]["report"]
    baseline_quality = runs["baseline"]["quality"][0] if runs["baseline"]["quality"] else None
    baseline_throughput = runs["baseline"]["throughput"][0] if runs["baseline"]["throughput"] else None

    comparison: dict[str, Any] = {}
    if baseline_throughput:
        comparison["full_graphify_speed_ratio"] = baseline_throughput["wall_seconds"] / max(runs["candidate"]["throughput"][0]["wall_seconds"], 1e-9)
    candidate_entity_seconds = throughput.get("stage_timings", {}).get("entity_census_seconds")
    baseline_entity_seconds = baseline_throughput["report"].get("stage_timings", {}).get("semantic_extraction_seconds") if baseline_throughput else None
    if isinstance(candidate_entity_seconds, (int, float)) and isinstance(baseline_entity_seconds, (int, float)):
        comparison["entity_stage_speed_ratio"] = baseline_entity_seconds / max(candidate_entity_seconds, 1e-9)

    merged_checks = {**quality.get("checks", {}), **throughput.get("checks", {})}
    merged_checks["idempotent_second_run"] = all(item["passed"] for item in idempotency.values())
    summary = {
        "status": "passed",
        "run_root": str(run_root),
        "metrics": {
            "entity": quality.get("metrics", {}).get("entity", {}),
            "relation": quality.get("metrics", {}).get("relation", {}),
            "throughput": throughput.get("metrics", {}).get("throughput", {}),
            "comparison": comparison,
        },
        "checks": merged_checks,
        "counts": {
            "quality": quality.get("counts", {}),
            "throughput": throughput.get("counts", {}),
        },
        "identity_digest": sha256_json({
            "quality": quality.get("identity_digest"),
            "throughput": throughput.get("identity_digest"),
            "idempotency": idempotency,
        }),
        "release_pins": quality.get("release_pins", {}),
        "idempotency": idempotency,
        "runs": {
            provider: {
                fixture: [
                    {key: value for key, value in item.items() if key != "report"}
                    for item in entries
                ]
                for fixture, entries in fixture_runs.items()
            }
            for provider, fixture_runs in runs.items()
        },
        "command_receipts": command_receipts,
    }
    summary_path = run_root / "e2e_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    latest = PACK_ROOT / "artifacts" / "e2e" / "latest"
    if latest.exists() or latest.is_symlink():
        if latest.is_dir() and not latest.is_symlink():
            shutil.rmtree(latest)
        else:
            latest.unlink()
    try:
        latest.symlink_to(run_root, target_is_directory=True)
    except OSError:
        shutil.copytree(run_root, latest)
    print(summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
