#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from state import WorkflowState, StageReceipt, utc_now, write_receipt
from workflow import WORKFLOW, WORKFLOW_BY_NAME

PACK_ROOT = Path(__file__).resolve().parent
STATE_PATH = PACK_ROOT / "work" / "workflow_state.json"


def load_state(require: bool = True) -> Optional[WorkflowState]:
    if not STATE_PATH.exists():
        if require:
            raise SystemExit("Workflow state not initialized. Run: python controller.py init --repo-root <repo>")
        return None
    return WorkflowState.load(STATE_PATH)


def save_state(state: WorkflowState) -> None:
    state.save(STATE_PATH)


def cmd_init(args: argparse.Namespace) -> None:
    repo_root = Path(args.repo_root).resolve()
    if not repo_root.exists():
        raise SystemExit(f"Repo root does not exist: {repo_root}")
    state = WorkflowState(repo_root=str(repo_root), pack_root=str(PACK_ROOT), current_stage=WORKFLOW[0].name)
    save_state(state)
    print(f"Initialized workflow state at {STATE_PATH}")
    print(f"repo_root={repo_root}")


def completed(node_name: str, state: WorkflowState) -> bool:
    return state.completed.get(node_name) == "PASSED"


def next_node(state: WorkflowState):
    for node in WORKFLOW:
        if completed(node.name, state):
            continue
        missing = [dep for dep in node.depends_on if not completed(dep, state)]
        if missing:
            return node, missing
        return node, []
    return None, []


def cmd_status(args: argparse.Namespace) -> None:
    state = load_state()
    print(json.dumps(asdict(state), indent=2, sort_keys=True))
    node, missing = next_node(state)
    if node:
        print("\nNEXT_STAGE:", node.name)
        if missing:
            print("BLOCKED_BY:", ", ".join(missing))
        print("TITLE:", node.title)
        print("DESCRIPTION:", node.description)
    else:
        print("\nWorkflow complete according to state. Run final verification artifacts before declaring production readiness.")


def cmd_next(args: argparse.Namespace) -> None:
    state = load_state()
    node, missing = next_node(state)
    if node is None:
        print("No incomplete workflow stage remains.")
        return
    if missing:
        raise SystemExit(f"Stage {node.name} is blocked by incomplete dependencies: {missing}")
    print(f"NEXT_STAGE: {node.name}")
    print(f"TITLE: {node.title}")
    print(f"DESCRIPTION: {node.description}")
    print("REQUIRED_ARTIFACTS:")
    for art in node.required_artifacts:
        print(f"  - {art}")
    print("VALIDATORS:")
    for val in node.validates:
        print(f"  - {val}")
    print("\nTo execute this stage's built-in guidance/scaffold:")
    print(f"  python controller.py run-stage {node.name}")


def cmd_run_stage(args: argparse.Namespace) -> None:
    state = load_state()
    stage_name = args.stage
    if stage_name not in WORKFLOW_BY_NAME:
        raise SystemExit(f"Unknown stage: {stage_name}")
    node = WORKFLOW_BY_NAME[stage_name]
    missing = [dep for dep in node.depends_on if not completed(dep, state)]
    if missing and not args.force:
        raise SystemExit(f"Stage {stage_name} blocked by dependencies: {missing}")
    module = importlib.import_module(node.stage_module)
    context = {
        "pack_root": str(PACK_ROOT),
        "repo_root": state.repo_root,
        "state": asdict(state),
        "node": asdict(node),
    }
    result = module.run(context)
    status = result.get("status", "NEEDS_AGENT")
    receipt = StageReceipt(
        stage=stage_name,
        status=status,
        started_at=result.get("started_at", utc_now()),
        finished_at=utc_now(),
        inputs_hash=result.get("inputs_hash", ""),
        outputs_hash=result.get("outputs_hash", ""),
        files_changed=result.get("files_changed", []),
        tests=result.get("tests", []),
        metrics=result.get("metrics", {}),
        warnings=result.get("warnings", []),
        errors=result.get("errors", []),
    )
    path = write_receipt(PACK_ROOT, receipt)
    state.receipts.append(str(path.relative_to(PACK_ROOT)))
    if status == "PASSED" or args.mark_passed:
        state.completed[stage_name] = "PASSED"
    else:
        state.completed[stage_name] = status
    state.current_stage = next_node(state)[0].name if next_node(state)[0] else None
    save_state(state)
    print(json.dumps(asdict(receipt), indent=2, sort_keys=True))
    print(f"Receipt: {path}")


def cmd_resume(args: argparse.Namespace) -> None:
    cmd_next(args)


def cmd_verify_pack(args: argparse.Namespace) -> None:
    from scripts.self_test import run_self_tests
    result = run_self_tests(PACK_ROOT)
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result.get("passed"):
        raise SystemExit(1)


def cmd_graph(args: argparse.Namespace) -> None:
    for node in WORKFLOW:
        deps = ",".join(node.depends_on) if node.depends_on else "ROOT"
        print(f"{deps} -> {node.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymath Graphify OpenIE workflow controller")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--repo-root", required=True)
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("next")
    p.set_defaults(func=cmd_next)
    p = sub.add_parser("resume")
    p.set_defaults(func=cmd_resume)
    p = sub.add_parser("run-stage")
    p.add_argument("stage")
    p.add_argument("--force", action="store_true")
    p.add_argument("--mark-passed", action="store_true", help="Only use after external repo tests prove the stage passed")
    p.set_defaults(func=cmd_run_stage)
    p = sub.add_parser("verify-pack")
    p.set_defaults(func=cmd_verify_pack)
    p = sub.add_parser("graph")
    p.set_defaults(func=cmd_graph)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
