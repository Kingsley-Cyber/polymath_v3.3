#!/usr/bin/env python3
"""Persistent, dependency-free workflow controller for the Graphify refactor."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

PACK_ROOT = Path(__file__).resolve().parent
WORKFLOW_PATH = PACK_ROOT / "workflow.json"
STATE_PATH = PACK_ROOT / ".agent_state" / "state.json"
FINAL_STATUS_PATH = PACK_ROOT / "artifacts" / "reports" / "workflow_final_status.json"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def run_git(repo_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def git_snapshot(repo_root: Path) -> dict[str, Any]:
    status = run_git(repo_root, "status", "--porcelain")
    return {
        "is_git_repository": run_git(repo_root, "rev-parse", "--is-inside-work-tree") == "true",
        "head": run_git(repo_root, "rev-parse", "HEAD"),
        "branch": run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status) if status is not None else None,
        "status_porcelain": status,
    }


def workflow() -> dict[str, Any]:
    return load_json(WORKFLOW_PATH)


def stages_by_id() -> dict[str, dict[str, Any]]:
    return {stage["id"]: stage for stage in workflow()["stages"]}


def require_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        raise SystemExit("State is not initialized. Run: python3 controller.py init --repo-root <path>")
    return load_json(STATE_PATH)


def get_nested(obj: dict[str, Any], dotted: str) -> Any:
    current: Any = obj
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(dotted)
        current = current[part]
    return current


def condition_met(condition: dict[str, Any] | None, facts: dict[str, Any]) -> bool | None:
    if not condition:
        return True
    fact = condition["fact"]
    if fact not in facts:
        return None
    left = facts[fact]
    right = condition["value"]
    op = condition["op"]
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    if op == "gt":
        return left > right
    if op == "ge":
        return left >= right
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    raise ValueError(f"Unsupported condition operator: {op}")


def refresh_conditional_skips(state: dict[str, Any]) -> bool:
    changed = False
    for stage in workflow()["stages"]:
        stage_state = state["stages"][stage["id"]]
        if stage_state["status"] != "pending" or "condition" not in stage:
            continue
        result = condition_met(stage["condition"], state.get("facts", {}))
        if result is False:
            stage_state.update({
                "status": "skipped",
                "reason": f"Condition evaluated false: {stage['condition']}",
                "completed_at": now(),
            })
            state["history"].append({
                "time": now(),
                "event": "stage_skipped",
                "stage_id": stage["id"],
                "reason": stage_state["reason"],
            })
            changed = True
    return changed


def dependencies_satisfied(stage: dict[str, Any], state: dict[str, Any]) -> bool:
    for dep in stage.get("depends_on", []):
        if state["stages"][dep]["status"] not in {"passed", "skipped"}:
            return False
    return True


def ready_stages(state: dict[str, Any]) -> list[dict[str, Any]]:
    refresh_conditional_skips(state)
    ready: list[dict[str, Any]] = []
    for stage in workflow()["stages"]:
        stage_state = state["stages"][stage["id"]]
        if stage_state["status"] != "pending":
            continue
        if not dependencies_satisfied(stage, state):
            continue
        result = condition_met(stage.get("condition"), state.get("facts", {}))
        if result is True:
            ready.append(stage)
    return ready


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now()
    write_json(STATE_PATH, state)


def cmd_init(args: argparse.Namespace) -> None:
    repo_root = Path(args.repo_root).expanduser().resolve()
    if not repo_root.is_dir():
        raise SystemExit(f"Repository root does not exist: {repo_root}")
    if STATE_PATH.exists() and not args.force:
        state = require_state()
        existing = Path(state["repo_root"]).resolve()
        if existing != repo_root:
            raise SystemExit(
                f"State already points to {existing}. Use --force to reinitialize for {repo_root}."
            )
        print(f"Already initialized for {repo_root}")
        return
    wf = workflow()
    state = {
        "version": 1,
        "workflow_name": wf["name"],
        "workflow_version": wf["version"],
        "repo_root": str(repo_root),
        "pack_root": str(PACK_ROOT),
        "created_at": now(),
        "updated_at": now(),
        "git_initial": git_snapshot(repo_root),
        "facts": {},
        "stages": {
            stage["id"]: {
                "title": stage["title"],
                "status": "pending",
                "started_at": None,
                "completed_at": None,
                "receipt": None,
            }
            for stage in wf["stages"]
        },
        "history": [{"time": now(), "event": "initialized", "repo_root": str(repo_root)}],
    }
    save_state(state)
    print(f"Initialized workflow for {repo_root}")


def cmd_status(_: argparse.Namespace) -> None:
    state = require_state()
    if refresh_conditional_skips(state):
        save_state(state)
    print(f"Repository: {state['repo_root']}")
    print(f"Workflow:   {state['workflow_name']} {state['workflow_version']}")
    print(f"Facts:      {json.dumps(state.get('facts', {}), sort_keys=True)}")
    for stage in workflow()["stages"]:
        item = state["stages"][stage["id"]]
        print(f"{stage['id']:<34} {item['status']:<12} {stage['title']}")


def print_stage(stage: dict[str, Any]) -> None:
    print(f"\n{stage['id']} — {stage['title']}")
    print(f"Goal: {stage['goal']}")
    if stage.get("condition"):
        print(f"Condition: {stage['condition']}")
    print("Actions:")
    for index, instruction in enumerate(stage.get("instructions", []), 1):
        print(f"  {index}. {instruction}")
    print("Receipt must demonstrate:")
    for item in stage.get("receipt_expectations", []):
        print(f"  - {item}")


def cmd_next(_: argparse.Namespace) -> None:
    state = require_state()
    ready = ready_stages(state)
    save_state(state)
    if not ready:
        terminal = workflow()["terminal_stage"]
        if state["stages"][terminal]["status"] == "passed":
            print("All workflow stages have passed. Run: python3 controller.py finalize")
            return
        active = [sid for sid, item in state["stages"].items() if item["status"] == "in_progress"]
        failed = [sid for sid, item in state["stages"].items() if item["status"] == "failed"]
        if active:
            print(f"No new stage is ready; in progress: {', '.join(active)}")
        elif failed:
            print(f"Workflow blocked by failed stages: {', '.join(failed)}")
        else:
            print("No stage is ready. A conditional fact or dependency is missing.")
        return
    for stage in ready:
        print_stage(stage)


def assert_ready(stage_id: str, state: dict[str, Any]) -> dict[str, Any]:
    stages = stages_by_id()
    if stage_id not in stages:
        raise SystemExit(f"Unknown stage: {stage_id}")
    stage = stages[stage_id]
    if state["stages"][stage_id]["status"] not in {"pending", "in_progress", "failed"}:
        raise SystemExit(f"Stage {stage_id} is already {state['stages'][stage_id]['status']}")
    if not dependencies_satisfied(stage, state):
        raise SystemExit(f"Dependencies are not satisfied for {stage_id}")
    result = condition_met(stage.get("condition"), state.get("facts", {}))
    if result is None:
        raise SystemExit(f"Condition fact is missing for {stage_id}: {stage.get('condition')}")
    if result is False:
        raise SystemExit(f"Condition is false for {stage_id}; it should be skipped")
    return stage


def cmd_start(args: argparse.Namespace) -> None:
    state = require_state()
    refresh_conditional_skips(state)
    assert_ready(args.stage_id, state)
    item = state["stages"][args.stage_id]
    item.update({"status": "in_progress", "started_at": item.get("started_at") or now()})
    state["history"].append({"time": now(), "event": "stage_started", "stage_id": args.stage_id})
    save_state(state)
    print(f"Started {args.stage_id}")


def validate_receipt(receipt: dict[str, Any], stage_id: str) -> None:
    required = {"stage_id", "status", "summary", "commands", "artifacts", "metrics", "facts"}
    missing = sorted(required - set(receipt))
    if missing:
        raise SystemExit(f"Receipt is missing required fields: {', '.join(missing)}")
    if receipt["stage_id"] != stage_id:
        raise SystemExit(f"Receipt stage_id {receipt['stage_id']} does not match {stage_id}")
    if receipt["status"] != "passed":
        raise SystemExit("Only passed receipts can complete a stage")
    if not isinstance(receipt["summary"], str) or not receipt["summary"].strip():
        raise SystemExit("Receipt summary must be non-empty")
    if not isinstance(receipt["commands"], list):
        raise SystemExit("Receipt commands must be a list")
    for command in receipt["commands"]:
        if not isinstance(command, dict) or "command" not in command or "exit_code" not in command:
            raise SystemExit("Every command receipt requires command and exit_code")
        if command["exit_code"] != 0:
            raise SystemExit(f"Receipt contains a failing command: {command['command']}")
    if not isinstance(receipt["artifacts"], list):
        raise SystemExit("Receipt artifacts must be a list")
    if not isinstance(receipt["facts"], dict):
        raise SystemExit("Receipt facts must be an object")


def resolve_artifact(value: str | dict[str, Any], repo_root: Path) -> Path:
    if isinstance(value, str):
        if value.startswith("pack:"):
            return (PACK_ROOT / value[5:]).resolve()
        if value.startswith("repo:"):
            return (repo_root / value[5:]).resolve()
        path = Path(value)
        return path.resolve() if path.is_absolute() else (repo_root / path).resolve()
    path = Path(value["path"])
    base = value.get("base", "repo")
    if base == "absolute":
        return path.resolve()
    if base == "pack":
        return (PACK_ROOT / path).resolve()
    return (repo_root / path).resolve()


def cmd_complete(args: argparse.Namespace) -> None:
    state = require_state()
    refresh_conditional_skips(state)
    assert_ready(args.stage_id, state)
    receipt_path = Path(args.receipt).expanduser().resolve()
    if not receipt_path.is_file():
        raise SystemExit(f"Receipt does not exist: {receipt_path}")
    receipt = load_json(receipt_path)
    validate_receipt(receipt, args.stage_id)
    repo_root = Path(state["repo_root"])
    missing_artifacts: list[str] = []
    for artifact in receipt["artifacts"]:
        resolved = resolve_artifact(artifact, repo_root)
        if not resolved.exists():
            missing_artifacts.append(str(resolved))
    if missing_artifacts:
        raise SystemExit("Receipt artifacts do not exist:\n  " + "\n  ".join(missing_artifacts))
    if args.stage_id == "S03_GOLD_ENTITY_SYNTAX_CEILING":
        value = receipt.get("facts", {}).get("gold_pair_recall")
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise SystemExit("S03 receipt must include facts.gold_pair_recall in [0, 1]")
    state.setdefault("facts", {}).update(receipt.get("facts", {}))
    item = state["stages"][args.stage_id]
    item.update({
        "status": "passed",
        "completed_at": now(),
        "receipt": str(receipt_path),
        "summary": receipt["summary"],
    })
    state["history"].append({
        "time": now(),
        "event": "stage_completed",
        "stage_id": args.stage_id,
        "receipt": str(receipt_path),
    })
    refresh_conditional_skips(state)
    save_state(state)
    print(f"Completed {args.stage_id}")


def cmd_fail(args: argparse.Namespace) -> None:
    state = require_state()
    if args.stage_id not in state["stages"]:
        raise SystemExit(f"Unknown stage: {args.stage_id}")
    item = state["stages"][args.stage_id]
    item.update({"status": "failed", "completed_at": now(), "reason": args.reason})
    state["history"].append({
        "time": now(), "event": "stage_failed", "stage_id": args.stage_id, "reason": args.reason
    })
    save_state(state)
    print(f"Failed {args.stage_id}: {args.reason}")


def cmd_verify_pack(_: argparse.Namespace) -> None:
    required = [
        "AGENTS.md", "RUNBOOK.md", "workflow.json", "acceptance_gates.json", "MANIFEST.json",
        "fixtures/graphify_quality_fixture.md", "fixtures/graphify_quality_gold.json",
        "fixtures/graphify_throughput_fixture.md", "fixtures/graphify_throughput_gold.json",
        "scripts/validate_fixtures.py", "scripts/run_e2e.py", "scripts/verify_manifest.py",
    ]
    missing = [item for item in required if not (PACK_ROOT / item).exists()]
    if missing:
        raise SystemExit("Control pack is missing required files: " + ", ".join(missing))
    load_json(WORKFLOW_PATH)
    load_json(PACK_ROOT / "acceptance_gates.json")
    manifest_result = subprocess.run(
        [sys.executable, str(PACK_ROOT / "scripts" / "verify_manifest.py")],
        cwd=str(PACK_ROOT),
    )
    if manifest_result.returncode != 0:
        raise SystemExit(manifest_result.returncode)
    result = subprocess.run(
        [sys.executable, str(PACK_ROOT / "scripts" / "validate_fixtures.py")],
        cwd=str(PACK_ROOT),
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    print("Control pack validation passed")


def cmd_finalize(_: argparse.Namespace) -> None:
    state = require_state()
    refresh_conditional_skips(state)
    terminal = workflow()["terminal_stage"]
    if state["stages"][terminal]["status"] != "passed":
        raise SystemExit(f"Cannot finalize: {terminal} is {state['stages'][terminal]['status']}")
    incomplete = {
        sid: item["status"]
        for sid, item in state["stages"].items()
        if item["status"] not in {"passed", "skipped"}
    }
    if incomplete:
        raise SystemExit(f"Cannot finalize; incomplete stages: {incomplete}")
    final = {
        "implementation_complete": True,
        "e2e_verified": True,
        "speed_gate_passed": bool(state.get("facts", {}).get("speed_gate_passed", False)),
        "quality_gate_passed": bool(state.get("facts", {}).get("quality_gate_passed", False)),
        "held_out_qualification": state.get("facts", {}).get("held_out_qualification", "pending"),
        "production_graph_write_promotion": state.get("facts", {}).get(
            "production_graph_write_promotion", "pending"
        ),
        "workflow_state": {sid: item["status"] for sid, item in state["stages"].items()},
        "facts": state.get("facts", {}),
        "repo_root": state["repo_root"],
        "git_initial": state.get("git_initial"),
        "git_final": git_snapshot(Path(state["repo_root"])),
        "generated_at": now(),
    }
    write_json(FINAL_STATUS_PATH, final)
    print(f"Finalized workflow: {FINAL_STATUS_PATH}")
    if not final["speed_gate_passed"] or not final["quality_gate_passed"]:
        print("Warning: workflow is structurally complete, but one or more measured gates are false.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--repo-root", required=True)
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    status = sub.add_parser("status")
    status.set_defaults(func=cmd_status)

    nxt = sub.add_parser("next")
    nxt.set_defaults(func=cmd_next)

    start = sub.add_parser("start")
    start.add_argument("stage_id")
    start.set_defaults(func=cmd_start)

    complete = sub.add_parser("complete")
    complete.add_argument("stage_id")
    complete.add_argument("--receipt", required=True)
    complete.set_defaults(func=cmd_complete)

    fail = sub.add_parser("fail")
    fail.add_argument("stage_id")
    fail.add_argument("--reason", required=True)
    fail.set_defaults(func=cmd_fail)

    verify = sub.add_parser("verify-pack")
    verify.set_defaults(func=cmd_verify_pack)

    finalize = sub.add_parser("finalize")
    finalize.set_defaults(func=cmd_finalize)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
