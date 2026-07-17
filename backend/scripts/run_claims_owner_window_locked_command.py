#!/usr/bin/env python3
"""Hold the host eval lock across one complete claims-window command.

The child receives the exact lock owner, generated nonce, and not-before
timestamp in environment variables.  Container-side commands must receive
those values explicitly (for example through ``docker exec --env``); they
never inspect the container's unrelated ``/tmp`` lock namespace.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from typing import Sequence

from scripts.run_claims_owner_window_compact_eval import _lock_context


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock-mode",
        choices=("acquire", "assert-held"),
        default="acquire",
    )
    parser.add_argument(
        "--lock-owner",
        default=os.environ.get(
            "POLYMATH_EVAL_LOCK_OWNER",
            "codex/claims-owner-window-harness-20260717",
        ),
    )
    parser.add_argument(
        "--window-nonce",
        default=os.environ.get("POLYMATH_EVAL_WINDOW_NONCE"),
    )
    parser.add_argument(
        "--window-not-before-utc",
        default=os.environ.get("POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC"),
    )
    parser.add_argument("--lock-wait-seconds", type=int, default=3600)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise RuntimeError("a child command is required after --")
    if args.lock_mode == "assert-held" and (
        not args.window_nonce or not args.window_not_before_utc
    ):
        raise RuntimeError(
            "assert-held mode requires the lock nonce and not-before environment"
        )

    with _lock_context(args):
        env = dict(os.environ)
        env.update(
            {
                "POLYMATH_EVAL_LOCK_OWNER": args.lock_owner,
                "POLYMATH_EVAL_WINDOW_NONCE": args.window_nonce,
                "POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC": args.window_not_before_utc,
                "POLYMATH_EVAL_OUTER_LOCK_ATTESTED": "1",
            }
        )
        completed = subprocess.run(command, env=env, check=False)
        return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
