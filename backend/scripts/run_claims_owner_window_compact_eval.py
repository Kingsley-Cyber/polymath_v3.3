#!/usr/bin/env python3
"""Run the frozen direct/lay/original-negative floors on one standard tier.

The 17-query preregistration and its scoring implementation remain read-only.
This runner hash-verifies the frozen inputs, selects only the preregistered
direct, lay-language, and original-negative rows, and delegates per-query
scoring to the existing Agent-T frozen scorer.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from scripts.run_claim_anchor_micro_ab import _atomic_write
from scripts.run_two_lane_anchoring_ab import (
    PREREG,
    SELECTION,
    _eval_lock,
    _mean,
    _preflight,
    _score_frozen,
    _sha256,
)


COMPACT_SCHEMA = "claims_owner_window_compact_frozen_floor.v1"
FROZEN_SHA256 = {
    PREREG: "8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110",
    SELECTION: "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00",
}
INCLUDED_SHAPES = frozenset(
    {"direct_expert", "direct_fact", "lay_language", "negative_control"}
)
EXPECTED_SHAPE_COUNTS = {
    "direct_expert": 5,
    "direct_fact": 1,
    "lay_language": 4,
    "negative_control": 3,
}
STANDARD_TIERS = ("qdrant_only", "qdrant_mongo", "qdrant_mongo_graph")
REFUSAL_STATE_CLASSIFIER = "existing_frozen_refusal_regex.v1"


def _load_compact_queries() -> tuple[list[dict[str, Any]], dict[str, str]]:
    hashes: dict[str, str] = {}
    for path, expected in FROZEN_SHA256.items():
        actual = _sha256(path)
        hashes[str(path)] = actual
        if actual != expected:
            raise RuntimeError(
                f"frozen input hash mismatch: {path.name} {actual} != {expected}"
            )
    prereg = json.loads(PREREG.read_text(encoding="utf-8"))
    queries = [
        query
        for query in prereg.get("queries") or []
        if query.get("shape") in INCLUDED_SHAPES
    ]
    observed_counts = {
        shape: sum(1 for query in queries if query.get("shape") == shape)
        for shape in EXPECTED_SHAPE_COUNTS
    }
    if observed_counts != EXPECTED_SHAPE_COUNTS or len(queries) != 13:
        raise RuntimeError(
            "compact frozen subset drifted: "
            f"observed={observed_counts} expected={EXPECTED_SHAPE_COUNTS}"
        )
    return queries, hashes


def _chat_temperature_zero(
    *,
    api: str,
    token: str,
    question: str,
    corpus_id: str,
    tier: str,
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{api.rstrip('/')}/api/chat",
        data=json.dumps(
            {
                "message": question,
                "corpus_ids": [corpus_id],
                "retrieval_tier": tier,
                "overrides": {"temperature": 0},
            },
            separators=(",", ":"),
        ).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
    )
    started = time.monotonic()
    current_event = ""
    answer_parts: list[str] = []
    sources: list[dict[str, Any]] = []
    selection: dict[str, Any] | None = None
    traces: list[dict[str, Any]] = []
    done: dict[str, Any] = {}
    error: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            buffer = b""
            while True:
                byte = response.read(1)
                if not byte:
                    break
                buffer += byte
                if not buffer.endswith(b"\n\n"):
                    continue
                block, buffer = buffer, b""
                for line in block.decode("utf-8", "replace").splitlines():
                    if line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    event = json.loads(raw)
                    event_type = str(event.get("type") or current_event)
                    if event_type == "token":
                        answer_parts.append(str(event.get("content") or ""))
                    elif event_type == "error":
                        error = str(event.get("content") or "unknown SSE error")
                    elif event_type == "done":
                        done = event
                    elif event_type == "sources":
                        sources = [
                            {
                                "corpus_id": item.get("corpus_id"),
                                "doc_id": item.get("doc_id"),
                                "doc_name": item.get("doc_name"),
                                "chunk_id": item.get("chunk_id"),
                            }
                            for item in (event.get("sources") or [])
                        ]
                    if event_type == "trace_event" or event.get("trace_event"):
                        trace = dict(event.get("trace_event") or event)
                        traces.append(trace)
                        if trace.get("title") == "Local RAG retrieval":
                            diagnostics = (trace.get("metadata") or {}).get(
                                "retrieval_diagnostics"
                            )
                            if isinstance(diagnostics, dict):
                                candidate = (diagnostics.get("selection") or {}).get(
                                    "two_lane_anchoring"
                                )
                                selection = (
                                    candidate if isinstance(candidate, dict) else None
                                )
    except Exception as exc:  # noqa: BLE001 - durable eval error
        error = f"{type(exc).__name__}: {exc}"

    answerability: dict[str, Any] | None = None
    model_skipped = False
    model_routes: list[str] = []
    prompt_template_hashes: list[str] = []
    for trace in traces:
        metadata = trace.get("metadata") or {}
        if isinstance(metadata.get("answerability"), dict):
            answerability = dict(metadata["answerability"])
        elif trace.get("title") == "Retrieval answerability" and isinstance(
            metadata, dict
        ):
            answerability = dict(metadata)
        if (
            trace.get("title") == "Assistant final answer"
            and metadata.get("model_skipped") is True
        ):
            model_skipped = True
        if trace.get("title") == "Chat model route" and metadata.get("model"):
            model_routes.append(str(metadata["model"]))
        for key in ("prompt_template_hash", "prompt_hash", "template_hash"):
            if metadata.get(key):
                prompt_template_hashes.append(str(metadata[key]))

    model_used = str(done.get("model_used") or "")
    if not model_used and model_routes:
        model_used = model_routes[-1]
    return {
        "answer": "".join(answer_parts),
        "sources": sources,
        "two_lane_anchoring": selection,
        "error": error,
        "wall_s": round(time.monotonic() - started, 3),
        "done_received": bool(done),
        "model_used": model_used,
        "model_skipped": model_skipped,
        "answerability": answerability,
        "prompt_template_hashes": sorted(set(prompt_template_hashes)),
        "request_temperature": 0,
    }


def _finalize(
    rows: list[dict[str, Any]],
    *,
    corpus_id: str,
    expected_model: str,
) -> dict[str, Any]:
    direct = [
        bool(row["doc_hit"])
        for row in rows
        if row["shape"] in {"direct_expert", "direct_fact"}
    ]
    lay = [bool(row["doc_hit"]) for row in rows if row["shape"] == "lay_language"]
    negatives = [
        bool(row["answerability_ok"])
        for row in rows
        if row["shape"] == "negative_control"
    ]
    membership = [
        all(
            str(source.get("corpus_id") or "") == corpus_id
            for source in row.get("sources") or []
        )
        for row in rows
    ]
    metrics = {
        "execution_count": len(rows),
        "direct_execution_count": len(direct),
        "lay_execution_count": len(lay),
        "original_negative_execution_count": len(negatives),
        "direct_doc_hit_rate": _mean(direct),
        "lay_language_doc_hit_rate": _mean(lay),
        "original_negative_refusal_rate": _mean(negatives),
        "original_negative_refusals": sum(1 for value in negatives if value),
        "corpus_citation_membership_rate": _mean(membership),
        "technical_success_rate": _mean(
            [
                not row.get("error")
                and row.get("done_received") is True
                and row.get("model_used") == expected_model
                for row in rows
            ]
        ),
    }
    gates = {
        "execution_closure": (
            len(rows) == 13
            and len(direct) == 6
            and len(lay) == 4
            and len(negatives) == 3
        ),
        "technical_success": metrics["technical_success_rate"] == 1.0,
        "direct": metrics["direct_doc_hit_rate"] >= 0.85,
        "lay": metrics["lay_language_doc_hit_rate"] >= 0.75,
        "original_negatives": metrics["original_negative_refusals"] == 3,
        "corpus_citation_membership": (
            metrics["corpus_citation_membership_rate"] == 1.0
        ),
    }
    return {"metrics": metrics, "gates": gates, "passed": all(gates.values())}


def _run(args: argparse.Namespace) -> dict[str, Any]:
    queries, frozen_hashes = _load_compact_queries()
    preflight = _preflight(args.api)
    rows: list[dict[str, Any]] = []
    for prior_call_index, query in enumerate(queries):
        result = _chat_temperature_zero(
            api=args.api,
            token=args.token,
            question=str(query["question"]),
            corpus_id=args.corpus_id,
            tier=args.tier,
            timeout=args.request_timeout,
        )
        row = {
            "id": query["id"],
            "shape": query["shape"],
            "tier": args.tier,
            **_score_frozen(query, result),
            **result,
            "prior_call_session_state": {
                "preceding_calls_in_process": prior_call_index,
                "history_turn_count": 0,
            },
        }
        row["refusal_state"] = (
            "gate_blocked"
            if row["model_skipped"]
            else ("model_voiced_refusal" if row["refused"] else "answered")
        )
        row["refusal_state_classifier"] = REFUSAL_STATE_CLASSIFIER
        rows.append(row)
        print(
            f"{args.tier} {row['id']} shape={row['shape']} "
            f"hit={row['doc_hit']} answerability={row['answerability_ok']} "
            f"error={row['error']}",
            flush=True,
        )
    final = _finalize(
        rows,
        corpus_id=args.corpus_id,
        expected_model=args.expected_model,
    )
    return {
        "schema_version": COMPACT_SCHEMA,
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "api": args.api,
        "corpus_id": args.corpus_id,
        "tier": args.tier,
        "expected_model": args.expected_model,
        "request_temperature": 0,
        "refusal_state_classifier": REFUSAL_STATE_CLASSIFIER,
        "frozen_hashes": frozen_hashes,
        "selection_rule": {
            "included_shapes": sorted(INCLUDED_SHAPES),
            "excluded_shape": "relationship_multi_document",
            "query_count": 13,
        },
        "embedder_preflight": preflight,
        **final,
        "results": rows,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--tier",
        choices=STANDARD_TIERS,
        default="qdrant_mongo_graph",
        help="exactly one standard tier; graph is the owner-window default",
    )
    parser.add_argument(
        "--api",
        default=os.environ.get("POLYMATH_API", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--token", default=os.environ.get("TOKEN", ""))
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument("--expected-model", default="anthropic/minimax-m2.7")
    parser.add_argument("--lock-wait-seconds", type=int, default=3600)
    parser.add_argument(
        "--lock-owner",
        default="codex/claims-owner-window-harness-20260717",
    )
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or TOKEN is required")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with _eval_lock(args.lock_owner, args.lock_wait_seconds):
        output = _run(args)
        _atomic_write(args.output, output)
    print(
        json.dumps(
            {
                "passed": output["passed"],
                "metrics": output["metrics"],
                "gates": output["gates"],
                "output": str(args.output),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if output["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
