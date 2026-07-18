#!/usr/bin/env python3
"""Run the preregistered non-gate temporal diagnostic on the E2E corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_settings
from e2e_retrieval_eval import (
    TIERS,
    _atomic_write,
    _mint_token,
    _run_sse,
    _score_execution,
)
from pymongo import MongoClient


PREREG_SHA = "9dcb147ccbfe54779e87307d2826d4565da4c43608354abf889a4ca701eef5d1"
SELECTION_SHA = "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00"
CORPUS_ID = "2c894530-8d57-4432-a6d4-bc14505a698b"


def rate(rows: list[dict[str, Any]], field: str) -> float:
    if not rows:
        return 0.0
    return sum(row.get(field) is True for row in rows) / len(rows)


def summarize(prereg: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    enriched = []
    for row in rows:
        anchor_complete = any(
            bool(value.get("all_anchors_hit"))
            for value in row["evidence_anchor_diagnostics"].values()
        )
        technical_success = (
            not row["errors"]
            and row["done_received"]
            and row["effective_tier"] == row["tier"]
        )
        enriched.append(
            {
                **row,
                "anchor_complete": anchor_complete,
                "technical_success": technical_success,
            }
        )
    per_tier = {}
    for tier in TIERS:
        tier_rows = [row for row in enriched if row["tier"] == tier]
        per_tier[tier] = {
            "execution_count": len(tier_rows),
            "doc_hit_rate": rate(tier_rows, "doc_hit"),
            "anchor_complete_rate": rate(tier_rows, "anchor_complete"),
            "technical_success_rate": rate(tier_rows, "technical_success"),
        }
    total_sources = sum(int(row["source_count"]) for row in enriched)
    member_sources = sum(int(row["source_membership_count"]) for row in enriched)
    expected = len(prereg["queries"]) * len(TIERS)
    metrics = {
        "report_only": True,
        "query_count": len(prereg["queries"]),
        "execution_count": len(enriched),
        "expected_execution_count": expected,
        "execution_closure": len(enriched) == expected,
        "doc_hit_rate": rate(enriched, "doc_hit"),
        "anchor_complete_rate": rate(enriched, "anchor_complete"),
        "technical_success_rate": rate(enriched, "technical_success"),
        "corpus_boundary_precision": (
            member_sources / total_sources if total_sources else 1.0
        ),
        "total_citation_sources": total_sources,
        "per_tier": per_tier,
    }
    technical_ok = (
        metrics["execution_closure"]
        and metrics["technical_success_rate"] == 1.0
        and metrics["corpus_boundary_precision"] == 1.0
    )
    return {"metrics": metrics, "technical_ok": technical_ok}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--prereg", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()
    if args.corpus_id != CORPUS_ID:
        raise RuntimeError("temporal diagnostic corpus identity drifted")
    if not 1 <= args.concurrency <= 6:
        raise RuntimeError("temporal diagnostic concurrency must be between 1 and 6")

    prereg_bytes = args.prereg.read_bytes()
    if hashlib.sha256(prereg_bytes).hexdigest() != PREREG_SHA:
        raise RuntimeError("temporal diagnostic preregistration hash drifted")
    prereg = json.loads(prereg_bytes)
    if tuple(prereg.get("tiers") or ()) != TIERS:
        raise RuntimeError("temporal diagnostic tiers drifted")
    if prereg.get("disposition") != "report_only_non_gate":
        raise RuntimeError("temporal diagnostic disposition drifted")
    if len(prereg.get("queries") or []) != 8:
        raise RuntimeError("temporal diagnostic query count drifted")

    selection_bytes = args.selection.read_bytes()
    if hashlib.sha256(selection_bytes).hexdigest() != SELECTION_SHA:
        raise RuntimeError("document selection hash drifted")
    selection = json.loads(selection_bytes)
    selected_filenames = {str(row["filename"]) for row in selection["selected"]}
    if len(selected_filenames) != 15:
        raise RuntimeError("document selection did not close at 15 filenames")

    settings = get_settings()
    mongo = MongoClient(settings.MONGODB_URI)
    database = mongo[settings.MONGODB_DATABASE]
    try:
        corpus = database["corpora"].find_one(
            {"corpus_id": args.corpus_id}, {"_id": 0, "name": 1}
        )
        if not corpus or corpus.get("name") != "runpod_e2e_15doc_20260715":
            raise RuntimeError(
                "only the response-discovered fresh E2E corpus is admissible"
            )
        document_names = {
            str(row.get("doc_id") or ""): str(
                row.get("original_filename") or row.get("filename") or ""
            )
            for row in database["documents"].find(
                {"corpus_id": args.corpus_id},
                {"_id": 0, "doc_id": 1, "filename": 1, "original_filename": 1},
            )
        }
        if len(document_names) != 15:
            raise RuntimeError(
                f"temporal diagnostic corpus is incomplete: {len(document_names)}"
            )
        token = _mint_token(database, args.corpus_id)

        if args.output.exists():
            state = json.loads(args.output.read_text(encoding="utf-8"))
            if (
                state.get("preregistration_sha256") != PREREG_SHA
                or state.get("corpus_id") != args.corpus_id
            ):
                raise RuntimeError("existing temporal diagnostic journal drifted")
        else:
            state = {
                "schema_version": "runpod_e2e_temporal_diagnostic_results.v1",
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "completed_at_utc": None,
                "preregistration_sha256": PREREG_SHA,
                "corpus_id": args.corpus_id,
                "results": [],
                "summary": None,
            }
            _atomic_write(args.output, state)

        completed = {row["execution_id"] for row in state["results"]}
        pending = [
            (case, tier)
            for case in prereg["queries"]
            for tier in TIERS
            if f"{case['id']}::{tier}" not in completed
        ]

        def run_execution(case: dict[str, Any], tier: str) -> dict[str, Any]:
            execution_id = f"{case['id']}::{tier}"
            print(f"TEMPORAL_START {execution_id}", flush=True)
            raw = _run_sse(
                base=args.base,
                token=token,
                corpus_id=args.corpus_id,
                tier=tier,
                question=case["question"],
                top_k=int(prereg["top_k"]),
            )
            return _score_execution(
                case=case,
                tier=tier,
                raw=raw,
                corpus_id=args.corpus_id,
                document_names=document_names,
                selected_filenames=selected_filenames,
            )

        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = {
                executor.submit(run_execution, case, tier): (case, tier)
                for case, tier in pending
            }
            for future in as_completed(futures):
                case, tier = futures[future]
                scored = future.result()
                state["results"].append(scored)
                state["summary"] = summarize(prereg, state["results"])
                _atomic_write(args.output, state)
                print(
                    "TEMPORAL_DONE "
                    + json.dumps(
                        {
                            "execution_id": scored["execution_id"],
                            "elapsed_seconds": scored["elapsed_seconds"],
                            "doc_hit": scored["doc_hit"],
                            "source_count": scored["source_count"],
                            "technical_ok": not scored["errors"]
                            and scored["done_received"]
                            and scored["effective_tier"] == tier,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )

        query_order = {
            str(case["id"]): index for index, case in enumerate(prereg["queries"])
        }
        tier_order = {tier: index for index, tier in enumerate(TIERS)}
        state["results"].sort(
            key=lambda row: (
                query_order[str(row["query_id"])],
                tier_order[str(row["tier"])],
            )
        )
        state["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        state["summary"] = summarize(prereg, state["results"])
        _atomic_write(args.output, state)
        print(json.dumps(state["summary"], indent=2, sort_keys=True), flush=True)
        return 0 if state["summary"]["technical_ok"] else 1
    finally:
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
