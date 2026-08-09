#!/usr/bin/env python3
"""Label existing structural graph projections as noncanonical shadow.

Graph-authority policy (owner directive 2026-08-03): structural/shadow graph
artifacts written by the inline ingest path (MENTIONS, EXPLAINS, CALLS,
ghost_b Facts, claim-less RELATES_TO) are extraction candidates, NEVER
canonical factual evidence. New writes stamp projection_kind /
authority / knowledge_status at the writer; this script backfills the same
vocabulary onto historical artifacts so read paths can fail closed.

WHAT THIS TOUCHES
    Property SETs only — no nodes or relationships are created or deleted.
    Idempotent: every query only matches artifacts missing the label.

CLASSES
    1. MENTIONS edges without projection_kind -> structural_shadow/noncanonical
    2. EXPLAINS edges without projection_kind -> structural_shadow/noncanonical
    3. CALLS edges without projection_kind    -> structural_shadow/noncanonical
    4. ghost_b Fact nodes without knowledge_status ->
       knowledge_status='candidate' + structural_shadow/noncanonical
    5. RELATES_TO without claim_ids and without projection_kind ->
       structural_shadow/noncanonical (includes legacy CLI-promoted edges:
       they lack claim qualification, so fail closed)
    6. RELATES_TO WITH claim_ids but no authority ->
       canonical_candidate/claim_promoted (upgrade path)

SAFETY
    Dry-run by default: prints counts per class, touches nothing.
    --apply executes batched transaction-bounded SET loops.

ENV
    NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD. Host runs must substitute
    'neo4j:7687' -> 'localhost:7687' (the container hostname is not
    resolvable outside the compose network).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

CLASSES = [
    (
        "mentions_shadow",
        "MATCH ()-[m:MENTIONS]->() WHERE m.projection_kind IS NULL",
        "SET m.projection_kind = 'structural_shadow', m.authority = 'noncanonical'",
        "m",
    ),
    (
        "explains_shadow",
        "MATCH ()-[e:EXPLAINS]->() WHERE e.projection_kind IS NULL",
        "SET e.projection_kind = 'structural_shadow', e.authority = 'noncanonical'",
        "e",
    ),
    (
        "calls_shadow",
        "MATCH ()-[c:CALLS]->() WHERE c.projection_kind IS NULL",
        "SET c.projection_kind = 'structural_shadow', c.authority = 'noncanonical'",
        "c",
    ),
    (
        "ghost_b_facts_candidate",
        (
            "MATCH (f:Fact) WHERE f.extractor = 'ghost_b' "
            "AND f.knowledge_status IS NULL"
        ),
        (
            "SET f.knowledge_status = 'candidate', "
            "f.projection_kind = 'structural_shadow', "
            "f.authority = 'noncanonical'"
        ),
        "f",
    ),
    (
        "relates_to_shadow",
        (
            "MATCH ()-[r:RELATES_TO]->() WHERE r.claim_ids IS NULL "
            "AND r.projection_kind IS NULL"
        ),
        (
            "SET r.projection_kind = 'structural_shadow', "
            "r.authority = 'noncanonical'"
        ),
        "r",
    ),
    (
        "relates_to_claim_uplift",
        (
            "MATCH ()-[r:RELATES_TO]->() WHERE r.claim_ids IS NOT NULL "
            "AND r.authority IS NULL"
        ),
        (
            "SET r.authority = 'canonical_candidate', "
            "r.projection_kind = 'claim_promoted'"
        ),
        "r",
    ),
]


def _driver():
    from neo4j import GraphDatabase

    uri = os.environ.get("NEO4J_URI", "bolt://neo4j:7687").replace(
        "neo4j:7687", "localhost:7687"
    )
    return GraphDatabase.driver(
        uri,
        auth=(
            os.environ.get("NEO4J_USER", "neo4j"),
            os.environ["NEO4J_PASSWORD"],
        ),
    )


def count_class(session, match: str, var: str) -> int:
    rec = session.run(
        f"{match} RETURN count({var}) AS n"
    ).single()
    return int(rec["n"]) if rec else 0


def apply_class(session, match: str, set_clause: str, var: str, batch: int) -> int:
    total = 0
    while True:
        rec = session.run(
            f"{match} WITH {var} LIMIT $batch {set_clause} "
            f"RETURN count({var}) AS n",
            batch=batch,
        ).single()
        n = int(rec["n"]) if rec else 0
        if n == 0:
            break
        total += n
        print(f"  ... {total:,} labelled", flush=True)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="execute SETs")
    ap.add_argument("--batch", type=int, default=50_000)
    args = ap.parse_args()

    drv = _driver()
    report: dict = {"apply": args.apply, "classes": {}}
    t0 = time.time()
    with drv.session() as session:
        for name, match, set_clause, var in CLASSES:
            pending = count_class(session, match, var)
            row = {"pending_before": pending}
            if args.apply and pending:
                row["labelled"] = apply_class(
                    session, match, set_clause, var, args.batch
                )
                row["pending_after"] = count_class(session, match, var)
            report["classes"][name] = row
            print(f"{name}: pending={pending:,}", flush=True)
    drv.close()
    report["elapsed_s"] = round(time.time() - t0, 1)
    print("\nsummary:", report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
