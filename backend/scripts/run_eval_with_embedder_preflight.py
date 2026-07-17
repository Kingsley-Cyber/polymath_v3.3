#!/usr/bin/env python3
"""Run an eval command only after the backend warms its MLX client pool."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence


DEFAULT_PREFLIGHT_URL = "http://127.0.0.1:8000/api/health/embedder/batch-ready"


def probe_embedder(url: str, timeout_seconds: float) -> dict:
    request = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.HTTPError, ValueError) as exc:
        raise RuntimeError(f"embedder preflight request failed: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ready":
        raise RuntimeError(f"embedder preflight refused: {payload!r}")
    return payload


def run_after_preflight(
    command: Sequence[str],
    *,
    preflight_url: str,
    timeout_seconds: float,
) -> int:
    if not command:
        raise ValueError("eval command is required")
    receipt = probe_embedder(preflight_url, timeout_seconds)
    print(
        "EMBEDDER_PREFLIGHT="
        + json.dumps(receipt, sort_keys=True, separators=(",", ":")),
        flush=True,
    )
    return int(subprocess.run(list(command), check=False).returncode)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Abort before scoring unless the MLX embedder is batch-ready."
    )
    parser.add_argument("--preflight-url", default=DEFAULT_PREFLIGHT_URL)
    parser.add_argument("--timeout-seconds", type=float, default=35.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    try:
        return run_after_preflight(
            command,
            preflight_url=args.preflight_url,
            timeout_seconds=args.timeout_seconds,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"EMBEDDER_PREFLIGHT_ABORT={exc}", file=sys.stderr, flush=True)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
