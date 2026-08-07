#!/usr/bin/env python3
"""Promote backfilled relations into Neo4j via the SANCTIONED writer.

WHY A SCRIPT AND NOT A HAND-ROLLED WRITER
    neo4j_writer._upsert_relation does entity-type resolution, corpus stamping,
    predicate refinement, edge-strength scoring and deadlock retry. Re-implementing
    any of that here would produce edges that differ from every other edge in the
    graph. So this drives the gated service method `backfill_graph_failures`,
    the same seam the /graph-backfill endpoint uses, which routes through the
    shared canonical graph-write authorization seam.

WHY DOCS MUST BE UNLATCHED FIRST
    That function only flushes when `write_state.neo4j_written` is not True. Every
    affected doc was already graph-written -- with EMPTY relations, because the
    RunPod lane hardcoded `relations=[]`. So the latch says "done" while the graph
    is missing everything the backfill just created. We clear the latch for exactly
    the docs that gained relations, then let the sanctioned path re-flush.
    Re-flushing is safe: the writer MERGEs, so a repeat pass is idempotent.

SAFETY
    - Touches ONLY docs that actually have backfilled relations.
    - Records the prior latch value per doc, so the unlatch is reversible.
    - DRY-RUN BY DEFAULT; --apply required.
    - allow_extraction=False: never re-runs Ghost B, never spends on a provider.

Usage (inside polymath_v33-backend-1):
    python promote_backfilled_relations.py --corpus <id> --limit 5     # dry run
    python promote_backfilled_relations.py --corpus <id> --apply
    python promote_backfilled_relations.py --all --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, "/app")

BACKFILL_VERSION = "r8a.v2.frame"


async def _run(corpus_id: str | None, apply: bool, limit: int | None) -> dict:
    from motor.motor_asyncio import AsyncIOMotorClient

    from services.ingestion.graph_promotion_jobs import (
        cli_operator_identity,
        evaluate_cli_graph_write_gate,
    )
    import services.ingestion_service as isvc

    # Deny-by-default release gate, evaluated BEFORE the latch is touched,
    # any state mutation, or any Neo4j call. Authority resolves once per run
    # through the shared authorization seam; writes below go through the
    # gated service method, never the low-level writer directly.
    gate = evaluate_cli_graph_write_gate(
        script_name="promote_backfilled_relations",
        operator=cli_operator_identity(),
    )
    if gate["blocked"]:
        # Release-policy block: not a graph failure. Return the frozen
        # blocked_no_release payload with a distinct policy exit marker.
        return {**gate["payload"], "mode": "BLOCKED_NO_RELEASE"}

    # The singleton is normally wired by the FastAPI lifespan. Outside the app
    # we drive its OWN connect() rather than rebuilding the clients here, so the
    # script cannot drift from how the service actually configures Qdrant/Neo4j.
    service = isvc.ingestion_service
    if service._db is None:
        client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
        await service.connect(client.get_database("polymath"))
    db = service._db
    if db is None:
        raise RuntimeError("ingestion_service failed to connect")
    if service._neo4j is None:
        raise RuntimeError(
            "Neo4j driver is not connected — NEO4J_ENABLED must be true to promote"
        )

    # Docs holding at least one backfilled relation.
    match: dict = {
        "relation_backfill.version": BACKFILL_VERSION,
        "relations.0": {"$exists": True},
    }
    if corpus_id:
        match["corpus_id"] = corpus_id
    pipeline = [
        {"$match": match},
        {"$group": {"_id": {"c": "$corpus_id", "d": "$doc_id"},
                    "rels": {"$sum": {"$size": "$relations"}}}},
        {"$sort": {"rels": -1}},
    ]
    targets = [
        {"corpus_id": r["_id"]["c"], "doc_id": r["_id"]["d"], "relations": r["rels"]}
        async for r in db["ghost_b_extractions"].aggregate(pipeline)
    ]
    if limit:
        targets = targets[:limit]

    print(f"documents holding backfilled relations: {len(targets)}", file=sys.stderr)
    total_rels = sum(t["relations"] for t in targets)
    print(f"relations awaiting promotion: {total_rels:,}", file=sys.stderr)
    if not apply:
        return {"mode": "DRY_RUN", "documents": len(targets),
                "relations_pending": total_rels,
                "release_gate": gate["release_gate"],
                "release_gate_caller": gate["script"],
                "release_gate_operator": gate["operator"],
                "sample": targets[:5]}

    ok = failed = 0
    reasons: Counter = Counter()
    t0 = time.time()
    for i, t in enumerate(targets, 1):
        doc = await db["documents"].find_one(
            {"doc_id": t["doc_id"], "corpus_id": t["corpus_id"]},
            {"write_state": 1, "user_id": 1},
        )
        if not doc:
            failed += 1
            reasons["document_row_missing"] += 1
            continue
        prior = bool((doc.get("write_state") or {}).get("neo4j_written"))
        # Unlatch so the sanctioned flush will actually fire. Prior value is
        # recorded on the doc so this is reversible.
        await db["documents"].update_one(
            {"doc_id": t["doc_id"], "corpus_id": t["corpus_id"]},
            {"$set": {
                "write_state.neo4j_written": False,
                "relation_promotion": {
                    "prior_neo4j_written": prior,
                    "backfill_version": BACKFILL_VERSION,
                    "relations": t["relations"],
                },
            }},
        )
        try:
            await service.backfill_graph_failures(
                corpus_id=t["corpus_id"],
                doc_id=t["doc_id"],
                user_id=str(doc.get("user_id") or ""),
                allow_extraction=False,   # never re-run Ghost B, never spend
            )
            ok += 1
        except Exception as exc:  # noqa: BLE001 - one bad doc must not stop the run
            failed += 1
            reasons[type(exc).__name__] += 1
            print(f"  FAILED doc={t['doc_id'][:12]} {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            # Restore the latch so a failed doc is not left mislabeled.
            await db["documents"].update_one(
                {"doc_id": t["doc_id"], "corpus_id": t["corpus_id"]},
                {"$set": {"write_state.neo4j_written": prior}},
            )
        if i % 20 == 0:
            print(f"  {i}/{len(targets)} docs ({time.time()-t0:.0f}s)", file=sys.stderr)

    return {
        "mode": "APPLIED",
        "documents_total": len(targets),
        "documents_promoted": ok,
        "documents_failed": failed,
        "failure_reasons": dict(reasons),
        "relations_pending_before": total_rels,
        "release_gate": gate["release_gate"],
        "release_gate_caller": gate["script"],
        "release_gate_operator": gate["operator"],
        "elapsed_s": round(time.time() - t0, 1),
    }


def main() -> int:
    from services.ingestion.graph_promotion_jobs import (
        RELEASE_POLICY_BLOCK_EXIT_CODE,
    )

    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    if not args.corpus and not args.all:
        print("refusing to run: pass --corpus <id> or --all", file=sys.stderr)
        return 2
    report = asyncio.run(_run(args.corpus, args.apply, args.limit))
    print(json.dumps(report, indent=2, default=str))
    if report.get("mode") == "BLOCKED_NO_RELEASE":
        print(
            "\nBLOCKED BY RELEASE GATE — no latch change, no write. "
            "Retryable once a qualifying active release exists.",
            file=sys.stderr,
        )
        return RELEASE_POLICY_BLOCK_EXIT_CODE
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
