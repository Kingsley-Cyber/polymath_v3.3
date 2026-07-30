#!/usr/bin/env python3
"""R-pre — measure what the deterministic relation lane actually discards.

Runs on the HOST (Stage C's real venue — the backend container has no
config/*.yaml, so the extractor fails loud there by design).

    PYTHONPATH=backend local_ghost_b/.venv/bin/python \
        backend/scripts/rpre_measure.py --in <sample.jsonl> --out <report.json>

Answers three questions the plan could previously only guess at:

  1. Where do candidate relations die? Per-guard counts, per genre. Before
     R-pre these counters were incremented and garbage-collected.
  2. What is R3's real ceiling? suppressed_conjunct_crossing per chunk is the
     upper bound on what coordination distribution could recover — NOT the
     "6.5x GLiREL" figure, which came from one chunk of a P=0.273 engine.
  3. How much is lost to the entity_type CASING MISMATCH? ontology.yaml uses
     Title Case (Concept); the RunPod-extracted corpora store UPPERCASE
     (CONCEPT). allowed_pairs does an exact tuple match, so uppercase types can
     never satisfy a constrained predicate. Measured by running the sample
     twice — types as stored, then normalized — and diffing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "backend"))

from services.extraction.dep_path_extractor import (  # noqa: E402
    ALL_COUNTER_KEYS, SUPPRESSION_KEYS, new_counters,
)
from services.extraction.spacy_relation_adapter import (  # noqa: E402
    get_spacy_extractor,
)

# ontology.yaml entity_types, keyed by their uppercase form.
_ONTOLOGY_TYPES = [
    "Person", "Organization", "Location", "Event", "Concept", "Method",
    "Product", "Software", "Document", "Standard", "Rule", "Law",
    "Artifact", "TimeReference", "other",
]
_UPPER_TO_TITLE = {t.upper(): t for t in _ONTOLOGY_TYPES}


def normalize_type(raw: str) -> tuple[str, bool]:
    """Return (normalized_type, was_outside_ontology)."""
    if not raw:
        return "", False
    if raw in _ONTOLOGY_TYPES:
        return raw, False
    mapped = _UPPER_TO_TITLE.get(raw.upper())
    if mapped:
        return mapped, False
    return raw, True  # e.g. RunPod's BEHAVIOR / PROCESS / QUALITY / AGENT


def run_pass(chunks: list[dict], normalize: bool) -> dict:
    """One full pass over the sample; returns aggregated stats."""
    ext = get_spacy_extractor()

    totals = dict.fromkeys(ALL_COUNTER_KEYS, 0)
    by_genre: dict[str, dict] = defaultdict(
        lambda: {"chunks": 0, "relations": 0, "rel_bearing": 0,
                 "counters": dict.fromkeys(ALL_COUNTER_KEYS, 0)}
    )
    predicate_dist: Counter = Counter()
    n_relations = 0
    n_rel_bearing = 0
    outside_ontology_types: Counter = Counter()

    BATCH = 200
    t0 = time.time()
    for start in range(0, len(chunks), BATCH):
        batch = chunks[start:start + BATCH]
        payload = []
        for ch in batch:
            ents = []
            for e in ch["entities"]:
                etype = e.get("entity_type") or ""
                if normalize:
                    etype, outside = normalize_type(etype)
                    if outside:
                        outside_ontology_types[etype] += 1
                ents.append({**e, "entity_type": etype})
            payload.append({
                "chunk_id": ch["chunk_id"], "doc_id": ch["doc_id"],
                "text": ch["text"], "entities": ents,
            })

        counters_list = [new_counters() for _ in payload]
        edge_lists = ext.extract_chunks(
            payload, max_related=10, suppression_counters_list=counters_list,
        )

        for ch, edges, ctr in zip(batch, edge_lists, counters_list):
            g = by_genre[ch["genre"]]
            g["chunks"] += 1
            g["relations"] += len(edges)
            n_relations += len(edges)
            if edges:
                g["rel_bearing"] += 1
                n_rel_bearing += 1
            for e in edges:
                predicate_dist[e["pred"]] += 1
            for k, v in ctr.items():
                totals[k] = totals.get(k, 0) + v
                g["counters"][k] = g["counters"].get(k, 0) + v

        done = start + len(batch)
        if done % 1000 == 0 or done == len(chunks):
            print(f"    {done}/{len(chunks)} chunks "
                  f"({time.time() - t0:.0f}s)", file=sys.stderr)

    elapsed = time.time() - t0
    n = len(chunks)
    return {
        "normalized_types": normalize,
        "chunks": n,
        "relations": n_relations,
        "relations_per_chunk": round(n_relations / n, 4) if n else 0.0,
        "rel_bearing_chunk_share": round(n_rel_bearing / n, 4) if n else 0.0,
        "ms_per_chunk": round(elapsed * 1000 / n, 2) if n else 0.0,
        "counters": totals,
        "counters_per_chunk": {
            k: round(v / n, 4) for k, v in totals.items() if v
        },
        "predicate_distribution": dict(predicate_dist.most_common()),
        "outside_ontology_types": dict(outside_ontology_types.most_common(20)),
        "by_genre": {
            g: {
                "chunks": d["chunks"],
                "relations": d["relations"],
                "relations_per_chunk": round(d["relations"] / d["chunks"], 4) if d["chunks"] else 0.0,
                "rel_bearing_share": round(d["rel_bearing"] / d["chunks"], 4) if d["chunks"] else 0.0,
                "counters_per_chunk": {
                    k: round(v / d["chunks"], 4)
                    for k, v in d["counters"].items() if v and d["chunks"]
                },
            }
            for g, d in sorted(by_genre.items())
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    args = ap.parse_args()

    chunks = [json.loads(l) for l in open(args.inp, encoding="utf-8") if l.strip()]
    print(f"loaded {len(chunks)} chunks", file=sys.stderr)

    print("  PASS A: entity types AS STORED", file=sys.stderr)
    pass_a = run_pass(chunks, normalize=False)
    print("  PASS B: entity types NORMALIZED to ontology casing", file=sys.stderr)
    pass_b = run_pass(chunks, normalize=True)

    # R3 ceiling: conjunct-crossing is the upper bound on what coordination
    # distribution could recover. Discount by the ~0.5 genuine rate estimated
    # from hand-judging GLiREL's enumeration output (spec §0).
    cc = pass_b["counters"].get("suppressed_conjunct_crossing", 0)
    n = pass_b["chunks"]
    cc_per_chunk = cc / n if n else 0.0

    report = {
        "schema": "polymath.rpre_measurement.v1",
        "sample": {
            "chunks": len(chunks),
            "by_genre": {
                g: sum(1 for c in chunks if c["genre"] == g)
                for g in sorted({c["genre"] for c in chunks})
            },
        },
        "pass_a_as_stored": pass_a,
        "pass_b_normalized": pass_b,
        "casing_hazard": {
            "description": (
                "ontology.yaml allowed_pairs are Title Case; RunPod-extracted "
                "corpora store UPPERCASE entity types. allowed_pairs does an "
                "exact tuple match, so uppercase can never satisfy a "
                "constrained predicate."
            ),
            "relations_as_stored": pass_a["relations"],
            "relations_normalized": pass_b["relations"],
            "relations_recovered_by_casing_fix": (
                pass_b["relations"] - pass_a["relations"]
            ),
            "allowed_pairs_rejected_as_stored": pass_a["counters"].get(
                "adapter_allowed_pairs_rejected", 0),
            "allowed_pairs_rejected_normalized": pass_b["counters"].get(
                "adapter_allowed_pairs_rejected", 0),
        },
        "r3_ceiling": {
            "suppressed_conjunct_crossing_total": cc,
            "per_chunk": round(cc_per_chunk, 4),
            "genuine_rate_assumption": 0.5,
            "estimated_recoverable_per_chunk": round(cc_per_chunk * 0.5, 4),
            "note": (
                "Upper bound before non-distributive guards (between/among, "
                "symmetric predicates, negated/contrastive conjuncts, list "
                "cap), which only reduce it. Supersedes the '6.5x GLiREL' "
                "planning figure, which rested on one chunk of P=0.273 output."
            ),
        },
        "suppression_ranking": sorted(
            (
                {"guard": k, "total": pass_b["counters"].get(k, 0),
                 "per_chunk": round(pass_b["counters"].get(k, 0) / n, 4) if n else 0.0}
                for k in SUPPRESSION_KEYS
            ),
            key=lambda d: -d["total"],
        ),
    }

    Path(args.out).write_text(json.dumps(report, indent=2))
    print(json.dumps(report["r3_ceiling"], indent=2))
    print(json.dumps(report["casing_hazard"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
