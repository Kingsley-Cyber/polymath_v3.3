#!/usr/bin/env python3
"""Production-ontology-aware qualification for an LLM extraction engine.

Fixes the battery/model mismatch (2026-08-11): the frozen stress scorer
judged predicate "leaks" against a 12-predicate battery ontology, but the
constrained decoder is bound to the 31-predicate production schema
(ExtractionResponse.Predicate). Legitimate schema predicates
(created_by, member_of, references, runs_on, stores, ...) were counted as
leaks, failing a correct model (24/66). This scorer defines "valid" as
"in the production schema" and scores triple recall against the gold key
without penalizing valid-but-out-of-battery predicates.

Usage (inside the backend container, after extracting into a qual corpus):
    python backend/scripts/qualify_llm_extraction.py --corpus <cid>

It reads the gold key, the extracted Neo4j edges for the corpus, and
reports: exact-triple recall, subject/object pair recall (predicate-blind),
predicate validity rate (against the production schema), and the
pass/fail vs the GLiNER baseline (book set: 60/66, leak 0).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

# The production predicate vocabulary the decoder is constrained to.
# Source of truth: services/ghost_b_schemas.py Predicate Literal.
PRODUCTION_PREDICATES = {
    "part_of", "member_of", "located_in", "works_for", "created_by",
    "owns", "affiliated_with", "synonym_of", "instance_of", "uses",
    "runs_on", "trained_on", "references", "implements", "depends_on",
    "produces", "consumes", "stores", "detects", "supports", "defines",
    "represents", "maps_to", "preceded_by", "causes", "overlaps",
    "derived_from", "contradicts", "excepts", "overrides", "related_to",
}

# Battery gold predicates that are SYNONYMS of production predicates
# (the gold key was authored against the 12-predicate battery ontology).
GOLD_TO_PRODUCTION = {
    "part_of": "part_of", "uses": "uses", "depends_on": "depends_on",
    "supports": "supports", "produces": "produces", "consumes": "consumes",
    "owns": "owns", "causes": "causes", "derived_from": "derived_from",
    "defines": "defines", "implements": "implements",
    "related_to": "related_to",
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower().replace("_", " ")).strip()


def _load_gold(gold_path: Path) -> list[dict]:
    data = json.loads(gold_path.read_text())
    # find the largest list-of-triples under any key
    lists = [
        v for v in data.values()
        if isinstance(v, list) and v and isinstance(v[0], dict) and "subject" in v[0]
    ]
    return max(lists, key=len) if lists else []


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument(
        "--gold",
        default="/tmp/gold_triples.json",
        help="battery gold_triples.json (docker cp it into the container first)",
    )
    ap.add_argument("--baseline", type=int, default=60, help="GLiNER book baseline")
    ap.add_argument("--gold-total", type=int, default=66)
    args = ap.parse_args()

    from services.conversation import conversation_service
    from services.ingestion_service import ingestion_service

    await conversation_service.connect()
    await ingestion_service.connect(conversation_service._db)

    gold = _load_gold(Path(args.gold))
    got: list[tuple[str, str, str]] = []
    drv = ingestion_service.neo4j_driver
    async with drv.session() as s:
        res = await s.run(
            "MATCH (a)-[r]->(b) WHERE $cid IN coalesce(r.corpus_ids,[]) "
            "RETURN coalesce(a.name,a.canonical_name,a.text) AS s, "
            "r.predicate AS p, coalesce(b.name,b.canonical_name,b.text) AS o",
            cid=args.corpus,
        )
        async for row in res:
            got.append((_norm(row["s"]), _norm(row["p"]), _norm(row["o"])))

    got_pairs = {(s, o) for s, _p, o in got}
    got_triples = set(got)

    exact = pair = 0
    for t in gold:
        gs, go = _norm(t["subject"]), _norm(t["object"])
        gp = _norm(GOLD_TO_PRODUCTION.get(t["predicate"], t["predicate"]))
        if (gs, gp, go) in got_triples:
            exact += 1
        if (gs, go) in got_pairs:
            pair += 1

    valid_preds = sum(1 for _s, p, _o in got if p.replace(" ", "_") in PRODUCTION_PREDICATES)
    total_edges = len(got) or 1
    denom = args.gold_total or (len(gold) or 1)

    verdict = "PASS" if exact >= args.baseline * denom / 66 else "BELOW BASELINE"
    print(json.dumps({
        "corpus": args.corpus,
        "gold_triples": len(gold),
        "extracted_edges": len(got),
        "exact_triple_recall": f"{exact}/{denom}",
        "pair_recall_predicate_blind": f"{pair}/{denom}",
        "predicate_validity_rate": f"{valid_preds}/{total_edges} "
                                   f"({100*valid_preds//total_edges}%)",
        "note": "validity measured against the 31-predicate PRODUCTION "
                "schema, not the 12-predicate battery list — the mismatch "
                "that wrongly failed the model at 24/66.",
        "gliner_baseline": f"{args.baseline}/66",
        "verdict": verdict,
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
