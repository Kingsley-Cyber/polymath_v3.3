#!/usr/bin/env python3
"""Run one preregistered Agent-T arm against an already deployed backend.

This harness is deliberately deployment-neutral: the executor sets the
runtime flag, then invokes one OFF or ON arm. It acquires the shared eval lock,
verifies immutable suite hashes, aborts before scoring if the MLX embedder is
not batch-ready, drives the real ``/api/chat`` SSE path, and writes one durable
JSON artifact.

The ON determinism gate requires ``--repeat 2``. The 28-probe negative v2 set
runs on the graph tier by default; use ``--negative-tier`` only if the senior
changes that live-run contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

REPO = Path(__file__).resolve().parents[2]
PREREG = REPO / "backend/evals/runpod_e2e_retrieval_preregister_v1.json"
SELECTION = REPO / "backend/evals/runpod_e2e_15doc_selection_v1.json"
NEGATIVE_V2 = REPO / "backend/evals/e2e_heldout_negative_v2_20260717.json"
LOCK_PATH = Path("/tmp/polymath-eval.lock")

EXPECTED_SHA256 = {
    PREREG: "8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110",
    SELECTION: "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00",
    NEGATIVE_V2: "3b35c14c165f6be89202b809ea01a1cd6ad0f5c0217e4167b86e4b5dc0b09960",
}
REFUSAL_RE = re.compile(
    r"i cannot answer|did not find source evidence|"
    r"cannot answer that as a source-backed|"
    r"(?:do(?:es)?(?: not|n't)|is not|are not)\s+(?:\w+\s+){0,2}?"
    r"(?:address|cover|contain|mention|name|state|establish|detail|describe|"
    r"include|provide|specify|recommend)",
    re.IGNORECASE,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_frozen_inputs() -> dict[str, str]:
    observed: dict[str, str] = {}
    for path, expected in EXPECTED_SHA256.items():
        actual = _sha256(path)
        observed[str(path.relative_to(REPO))] = actual
        if actual != expected:
            raise RuntimeError(
                f"frozen input hash mismatch: {path.name} {actual} != {expected}"
            )
    return observed


@contextmanager
def _eval_lock(owner: str, wait_seconds: int) -> Iterator[None]:
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        try:
            descriptor = os.open(
                LOCK_PATH,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
            )
        except FileExistsError:
            if time.monotonic() >= deadline:
                holder = LOCK_PATH.read_text(errors="replace").strip()
                raise RuntimeError(f"eval lock held by {holder or 'unknown'}")
            time.sleep(60)
            continue
        with os.fdopen(descriptor, "w") as handle:
            handle.write(owner + "\n")
        break
    try:
        yield
    finally:
        try:
            if LOCK_PATH.read_text(errors="replace").strip() == owner:
                LOCK_PATH.unlink()
        except FileNotFoundError:
            pass


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def _preflight(api: str) -> dict[str, Any]:
    payload = _post_json(f"{api}/api/health/embedder/batch-ready", {}, 40.0)
    if not isinstance(payload, dict) or payload.get("status") != "ready":
        raise RuntimeError(f"embedder preflight refused: {payload!r}")
    return payload


def _normalized_doc_name(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _chat(
    *,
    api: str,
    token: str,
    question: str,
    corpus_id: str,
    tier: str,
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{api}/api/chat",
        data=json.dumps(
            {
                "message": question,
                "corpus_ids": [corpus_id],
                "retrieval_tier": tier,
            }
        ).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
    )
    started = time.monotonic()
    answer_parts: list[str] = []
    sources: list[dict[str, Any]] = []
    selection: dict[str, Any] | None = None
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
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    event = json.loads(raw)
                    if event.get("type") == "token":
                        answer_parts.append(str(event.get("content") or ""))
                    if event.get("type") == "error":
                        error = str(event.get("content") or "unknown SSE error")
                    if event.get("type") == "sources":
                        sources = [
                            {
                                "corpus_id": item.get("corpus_id"),
                                "doc_id": item.get("doc_id"),
                                "doc_name": item.get("doc_name"),
                                "chunk_id": item.get("chunk_id"),
                            }
                            for item in (event.get("sources") or [])
                        ]
                    trace = event.get("trace_event") or {}
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
    except Exception as exc:  # noqa: BLE001 - recorded in durable artifact
        error = f"{type(exc).__name__}: {exc}"
    return {
        "answer": "".join(answer_parts),
        "sources": sources,
        "two_lane_anchoring": selection,
        "error": error,
        "wall_s": round(time.monotonic() - started, 3),
    }


def _score_frozen(query: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    expected = {
        _normalized_doc_name(value) for value in query.get("expected_any") or []
    }
    returned = {
        _normalized_doc_name(item.get("doc_name"))
        for item in result.get("sources") or []
        if item.get("doc_name")
    }
    expected_hits = sorted(expected & returned)
    minimum = int(query.get("expected_min_distinct") or 1)
    shape = str(query.get("shape") or "")
    refused = bool(REFUSAL_RE.search(str(result.get("answer") or "")))
    selection = result.get("two_lane_anchoring")
    groups = (selection or {}).get("groups") if isinstance(selection, dict) else []
    pool_has_anchor = any(
        int(group.get("anchors_available") or 0) > 0 for group in (groups or [])
    )
    pool_anchor_candidate_ids = sorted(
        {
            str(candidate_id)
            for group in (groups or [])
            for candidate_id in (group.get("anchor_candidate_ids") or [])
            if str(candidate_id)
        }
    )
    selected_has_anchor = bool(
        isinstance(selection, dict) and int(selection.get("anchor_seats") or 0) > 0
    )
    fingerprint = [
        str(row.get("candidate_id") or "")
        for row in ((selection or {}).get("selected") or [])
    ]
    return {
        "doc_hit": bool(expected_hits),
        "minimum_distinct_ok": len(expected_hits) >= minimum,
        "expected_hits": expected_hits,
        "refused": refused,
        "answerability_ok": refused if shape == "negative_control" else not refused,
        "pool_has_anchor": pool_has_anchor,
        "pool_anchor_candidate_ids": pool_anchor_candidate_ids,
        "selected_has_anchor": selected_has_anchor,
        "allocation_fingerprint": fingerprint,
    }


def _mean(values: list[bool]) -> float:
    return (
        round(sum(1 for value in values if value) / len(values), 4) if values else 0.0
    )


def _run_arm(args: argparse.Namespace) -> dict[str, Any]:
    frozen_hashes = _verify_frozen_inputs()
    prereg = json.loads(PREREG.read_text())
    negative = json.loads(NEGATIVE_V2.read_text())
    corpus_id = str(negative["corpus_id"])
    preflight = _preflight(args.api)

    frozen_results: list[dict[str, Any]] = []
    for repeat in range(1, args.repeat + 1):
        for tier in args.tiers:
            for query in prereg["queries"]:
                result = _chat(
                    api=args.api,
                    token=args.token,
                    question=str(query["question"]),
                    corpus_id=corpus_id,
                    tier=tier,
                    timeout=args.request_timeout,
                )
                row = {
                    "repeat": repeat,
                    "id": query["id"],
                    "shape": query["shape"],
                    "tier": tier,
                    **_score_frozen(query, result),
                    **result,
                }
                frozen_results.append(row)
                print(
                    f"{args.arm} repeat={repeat} {tier} {query['id']} "
                    f"hit={row['doc_hit']} min={row['minimum_distinct_ok']} "
                    f"anchor={row['selected_has_anchor']} error={row['error']}",
                    flush=True,
                )

    negative_results: list[dict[str, Any]] = []
    for query in negative["queries"]:
        result = _chat(
            api=args.api,
            token=args.token,
            question=str(query["question"]),
            corpus_id=corpus_id,
            tier=args.negative_tier,
            timeout=args.request_timeout,
        )
        refused = bool(REFUSAL_RE.search(str(result.get("answer") or "")))
        negative_results.append(
            {
                "id": query["id"],
                "family": query["family"],
                "tier": args.negative_tier,
                "refused": refused,
                **result,
            }
        )
        print(
            f"{args.arm} {args.negative_tier} {query['id']} "
            f"refused={refused} error={result['error']}",
            flush=True,
        )

    primary = [row for row in frozen_results if row["repeat"] == 1]
    direct = [
        row["doc_hit"]
        for row in primary
        if row["shape"] in {"direct_expert", "direct_fact"}
    ]
    lay = [row["doc_hit"] for row in primary if row["shape"] == "lay_language"]
    relationship = [
        row["minimum_distinct_ok"]
        for row in primary
        if row["shape"] == "relationship_multi_document"
    ]
    original_negative = [
        row["answerability_ok"] for row in primary if row["shape"] == "negative_control"
    ]
    corpus_membership = [
        all(
            str(source.get("corpus_id") or "") == corpus_id
            for source in row.get("sources") or []
        )
        for row in primary
    ]
    anchor_eligible = [row for row in primary if row["pool_has_anchor"]]
    anchor_coverage = _mean([row["selected_has_anchor"] for row in anchor_eligible])
    off_anchor_coverage: float | None = None
    if args.arm == "on" and args.off_artifact is not None:
        off_artifact = json.loads(args.off_artifact.read_text())
        off_rows = {
            (row["id"], row["tier"]): row
            for row in off_artifact.get("frozen_results") or []
            if int(row.get("repeat") or 1) == 1
        }
        off_anchor_hits: list[bool] = []
        for row in anchor_eligible:
            off_row = off_rows.get((row["id"], row["tier"]))
            if off_row is None:
                raise RuntimeError(f"OFF artifact missing {row['id']} / {row['tier']}")
            off_selected = {
                f"{source.get('corpus_id')}|{source.get('chunk_id')}"
                for source in off_row.get("sources") or []
                if source.get("corpus_id") and source.get("chunk_id")
            }
            off_anchor_hits.append(
                bool(off_selected & set(row["pool_anchor_candidate_ids"]))
            )
        off_anchor_coverage = _mean(off_anchor_hits)
    flag_shape_ok = all(
        (row["two_lane_anchoring"] is not None) == (args.arm == "on") for row in primary
    )
    technical_ok = not any(
        row.get("error") for row in [*frozen_results, *negative_results]
    )

    determinism_ok: bool | None = None
    if args.repeat >= 2:
        fingerprints: dict[tuple[str, str], list[list[str]]] = {}
        for row in frozen_results:
            fingerprints.setdefault((row["id"], row["tier"]), []).append(
                row["allocation_fingerprint"]
            )
        determinism_ok = all(
            all(value == values[0] for value in values[1:])
            for values in fingerprints.values()
        )

    summary = {
        "arm": args.arm,
        "frozen_executions": len(frozen_results),
        "negative_v2_executions": len(negative_results),
        "direct_doc_hit_rate": _mean(direct),
        "lay_doc_hit_rate": _mean(lay),
        "relationship_minimum_distinct_rate": _mean(relationship),
        "original_negative_refusals": sum(original_negative),
        "original_negative_total": len(original_negative),
        "negative_v2_refusal_rate": _mean([row["refused"] for row in negative_results]),
        "corpus_citation_membership_rate": _mean(corpus_membership),
        "anchor_eligible_executions": len(anchor_eligible),
        "anchor_coverage_rate": anchor_coverage,
        "off_anchor_coverage_rate_for_on_eligible_pool": off_anchor_coverage,
        "runtime_flag_shape_ok": flag_shape_ok,
        "determinism_ok": determinism_ok,
        "technical_ok": technical_ok,
    }
    gates = {
        "technical": technical_ok,
        "runtime_flag_shape": flag_shape_ok,
        "direct": summary["direct_doc_hit_rate"] >= 0.85,
        "lay": summary["lay_doc_hit_rate"] >= 0.75,
        "relationship": summary["relationship_minimum_distinct_rate"] >= 0.75,
        "original_negatives": (
            summary["original_negative_refusals"] == summary["original_negative_total"]
        ),
        "negative_v2": summary["negative_v2_refusal_rate"] >= 1.0,
        "corpus_citation": summary["corpus_citation_membership_rate"] >= 1.0,
    }
    if args.arm == "on":
        gates["anchor_coverage"] = (
            summary["anchor_eligible_executions"] > 0
            and summary["anchor_coverage_rate"] >= 0.90
        )
        gates["determinism"] = args.repeat >= 2 and determinism_ok is True
    summary["gates"] = gates
    summary["all_green"] = all(gates.values())
    return {
        "schema_version": "polymath.two_lane_anchoring_ab_arm.v1",
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "api": args.api,
        "frozen_hashes": frozen_hashes,
        "embedder_preflight": preflight,
        "summary": summary,
        "frozen_results": frozen_results,
        "negative_v2_results": negative_results,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=("off", "on"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--api",
        default=os.environ.get("POLYMATH_API", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--token", default=os.environ.get("TOKEN", ""))
    parser.add_argument(
        "--tiers",
        nargs="+",
        default=["qdrant_only", "qdrant_mongo", "qdrant_mongo_graph"],
        choices=["qdrant_only", "qdrant_mongo", "qdrant_mongo_graph"],
    )
    parser.add_argument(
        "--negative-tier",
        default="qdrant_mongo_graph",
        choices=["qdrant_only", "qdrant_mongo", "qdrant_mongo_graph"],
    )
    parser.add_argument("--repeat", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--off-artifact",
        type=Path,
        help=(
            "Required for ON: completed OFF artifact used to report the "
            "preregistered anchor-coverage baseline over the same eligible pool."
        ),
    )
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument("--lock-wait-seconds", type=int, default=3600)
    parser.add_argument(
        "--lock-owner",
        default="codex/two-lane-anchoring-20260717",
    )
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or TOKEN is required")
    if args.arm == "on" and args.repeat != 2:
        parser.error("ON arm requires --repeat 2 for the preregistered T4 gate")
    if args.arm == "on" and (
        args.off_artifact is None or not args.off_artifact.is_file()
    ):
        parser.error("ON arm requires an existing --off-artifact")
    if args.out.exists():
        parser.error("--out must not already exist")
    return args


def main() -> int:
    args = _parse_args()
    try:
        with _eval_lock(args.lock_owner, args.lock_wait_seconds):
            artifact = _run_arm(args)
    except (OSError, RuntimeError, urllib.error.URLError, ValueError) as exc:
        print(f"PRE-SCORING ABORT: {exc}", file=sys.stderr)
        return 78
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2) + "\n")
    print(json.dumps(artifact["summary"], indent=2))
    print(f"WROTE {args.out}")
    return 0 if artifact["summary"]["all_green"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
