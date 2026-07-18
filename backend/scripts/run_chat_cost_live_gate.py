#!/usr/bin/env python3
"""Run the single preregistered live gate for the P7 chat cost ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


TRACE_TITLE = "Chat synthesis cost ledger"
LEDGER_VERSION = "polymath.chat_cost_ledger.v1"
QUESTION_ID = "q021"
QUESTION = "What is 'dry testing' and how is it used to validate products?"
EXPECTED_MODEL = "anthropic/minimax-m2.7"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(payload + "\n")
    os.replace(temporary, path)


def _independent_cost(ledger: dict[str, Any]) -> str:
    total = Decimal("0")
    for row in ledger.get("calls") or []:
        price = row.get("price") or {}
        input_tokens = int(row["input_tokens"])
        output_tokens = int(row["output_tokens"])
        unit = Decimal(str(price["price_unit_tokens"]))
        computed = (
            Decimal(input_tokens) * Decimal(str(price["input_usd_per_unit"]))
            + Decimal(output_tokens) * Decimal(str(price["output_usd_per_unit"]))
        ) / unit
        if computed != Decimal(str(row["computed_cost_usd"])):
            raise RuntimeError(f"cost arithmetic drifted for {row.get('call_id')}")
        total += computed
    rendered = format(total, "f").rstrip("0").rstrip(".")
    return rendered or "0"


def _run(api: str, token: str, corpus_id: str, timeout: float) -> dict[str, Any]:
    body = {
        "message": QUESTION,
        "corpus_ids": [corpus_id],
        "retrieval_tier": "qdrant_mongo",
        "overrides": {
            "temperature": 0,
            "hyde_enabled": False,
            "agentic_mode": False,
        },
    }
    request = urllib.request.Request(
        f"{api.rstrip('/')}/api/chat",
        data=_canonical_bytes(body),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    started = time.monotonic()
    answer_parts: list[str] = []
    source_ids: list[str] = []
    ledgers: list[dict[str, Any]] = []
    event_counts: dict[str, int] = {}
    errors: list[str] = []
    done_count = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"unexpected chat status {response.status}")
        if "text/event-stream" not in str(response.headers.get("Content-Type")):
            raise RuntimeError("chat response was not SSE")
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            encoded = line[5:].strip()
            if not encoded or encoded == "[DONE]":
                continue
            event = json.loads(encoded)
            event_type = str(event.get("type") or "unknown")
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
            if event_type == "token":
                answer_parts.append(str(event.get("content") or ""))
            elif event_type == "sources":
                source_ids.extend(
                    str(row.get("chunk_id") or row.get("doc_id") or "")
                    for row in event.get("sources") or []
                    if row.get("chunk_id") or row.get("doc_id")
                )
            elif event_type == "error":
                errors.append(str(event.get("content") or "unknown SSE error"))
            elif event_type == "done":
                done_count += 1
            trace = event.get("trace_event") or {}
            if trace.get("title") == TRACE_TITLE:
                ledger = (trace.get("metadata") or {}).get("chat_cost_ledger")
                if isinstance(ledger, dict):
                    ledgers.append(ledger)

    elapsed = round(time.monotonic() - started, 3)
    if errors:
        raise RuntimeError(f"chat emitted errors: {errors}")
    if done_count != 1:
        raise RuntimeError(f"expected one done event, observed {done_count}")
    if len(ledgers) != 1:
        raise RuntimeError(f"expected one cost ledger, observed {len(ledgers)}")
    ledger = ledgers[0]
    if ledger.get("schema_version") != LEDGER_VERSION:
        raise RuntimeError("cost ledger schema drifted")
    if ledger.get("accounting_state") != "CLOSED":
        raise RuntimeError("cost ledger is not CLOSED")
    if ledger.get("unmetered_synthesis_call_count") != 0:
        raise RuntimeError("cost ledger contains unmetered synthesis calls")
    if ledger.get("synthesis_call_count") != 1:
        raise RuntimeError("designed gate did not make exactly one synthesis call")
    if ledger.get("metered_synthesis_call_count") != 1:
        raise RuntimeError("designed synthesis call was not metered")
    if int(ledger.get("input_tokens") or 0) <= 0:
        raise RuntimeError("input usage was not captured")
    if int(ledger.get("output_tokens") or 0) <= 0:
        raise RuntimeError("output usage was not captured")
    calls = ledger.get("calls") or []
    if len(calls) != 1 or calls[0].get("model") != EXPECTED_MODEL:
        raise RuntimeError("designed call did not use the preregistered model")
    independently_computed = _independent_cost(ledger)
    if independently_computed != ledger.get("computed_cost_usd"):
        raise RuntimeError("request total does not reproduce from trace rows")

    answer = "".join(answer_parts)
    return {
        "schema_version": "polymath.p7_live_gate.v1",
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "GREEN",
        "question_id": QUESTION_ID,
        "question_sha256": _sha256(QUESTION.encode()),
        "corpus_id": corpus_id,
        "request_contract": {
            "retrieval_tier": "qdrant_mongo",
            "temperature": 0,
            "model": EXPECTED_MODEL,
        },
        "event_counts": event_counts,
        "source_identity_sha256": _sha256(_canonical_bytes(sorted(source_ids))),
        "source_count": len(source_ids),
        "answer_sha256": _sha256(answer.encode()),
        "answer_characters": len(answer),
        "elapsed_seconds": elapsed,
        "independently_computed_cost_usd": independently_computed,
        "chat_cost_ledger": ledger,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    args = parser.parse_args()
    token = os.environ.get("TOKEN") or ""
    if not token:
        raise SystemExit("TOKEN is required")
    receipt = _run(args.api, token, args.corpus_id, args.timeout_seconds)
    _atomic_write(args.out, receipt)
    print(
        "P7_GATE="
        + json.dumps(
            {
                "status": receipt["status"],
                "input_tokens": receipt["chat_cost_ledger"]["input_tokens"],
                "output_tokens": receipt["chat_cost_ledger"]["output_tokens"],
                "computed_cost_usd": receipt["chat_cost_ledger"][
                    "computed_cost_usd"
                ],
                "elapsed_seconds": receipt["elapsed_seconds"],
                "artifact": str(args.out),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
