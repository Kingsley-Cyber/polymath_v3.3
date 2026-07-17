#!/usr/bin/env python3
"""Aggregate additive ``/api/chat`` cost traces from eval JSON or raw SSE.

The command never estimates missing usage. A run is CLOSED only when at least
one request ledger exists and every synthesis call is usage- and price-complete.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services.chat_cost_meter import (  # noqa: E402
    CHAT_COST_LEDGER_VERSION,
    aggregate_chat_cost_ledgers,
)


def _walk_request_ledgers(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value.get("schema_version") == CHAT_COST_LEDGER_VERSION:
            found.append(value)
            return found
        for child in value.values():
            found.extend(_walk_request_ledgers(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_request_ledgers(child))
    return found


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def load_ledgers(path: Path) -> list[dict[str, Any]]:
    text = path.read_text()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = _parse_sse(text)
    return _walk_request_ledgers(payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifacts", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    ledgers: list[dict[str, Any]] = []
    for artifact in args.artifacts:
        ledgers.extend(load_ledgers(artifact))
    receipt = aggregate_chat_cost_ledgers(ledgers)
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered)
    print(rendered, end="")
    return 0 if receipt["accounting_state"] == "CLOSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
