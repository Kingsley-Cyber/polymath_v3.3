#!/usr/bin/env python3
"""Entity gate v1 — sample by MENTION, judge on four dimensions, score.

Sampling by mention rather than by unique canonical_name is deliberate: a
generic noun emitted 400 times does 400x the downstream damage of one emitted
once, so the sample has to reflect real frequency.

    python entity_gate_sample.py sample --out work.jsonl
    python entity_gate_sample.py score  --worksheet judged.jsonl --out result.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/app")

GENRE_BY_CORPUS = {
    "8dfb070a-5cb6-4f15-8769-b253df09d12c": "book",
    "f3849304-268f-46bb-838e-912d84a68e3c": "book",
    "d18713f7-2d12-4b6d-8be8-436c136f03c7": "paper",
    "35fc1f52-c7f9-4867-9343-8e1e711bf759": "paper",
    "149a2dcb-8c61-404e-8006-1df7e3791b5a": "asr",
    "91e2fd28-461a-437d-8949-9581507f9a86": "asr",
}
ONTOLOGY_TYPES = {
    "Person", "Organization", "Location", "Event", "Concept", "Method",
    "Product", "Software", "Document", "Standard", "Rule", "Law",
    "Artifact", "TimeReference", "other",
}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, c - m), min(1.0, c + m))


def do_sample(out: str, per_stratum: int) -> None:
    import pymongo
    db = pymongo.MongoClient(os.environ["MONGODB_URI"]).get_database("polymath")

    by_genre: dict[str, list[dict]] = defaultdict(list)
    for cid, genre in GENRE_BY_CORPUS.items():
        for d in db.ghost_b_extractions.find(
            {"corpus_id": cid, "entities.0": {"$exists": True}},
            {"chunk_id": 1, "text": 1, "entities": 1, "_id": 0},
        ).limit(1500):
            ctx = " ".join((d.get("text") or "").split())[:300]
            for e in d.get("entities") or []:
                by_genre[genre].append({
                    "chunk_id": d.get("chunk_id", ""),
                    "genre": genre,
                    "surface": e.get("surface_form") or "",
                    "canonical": e.get("canonical_name") or "",
                    "entity_type": e.get("entity_type") or "",
                    "confidence": round(float(e.get("confidence") or 0), 3),
                    "object_kind": e.get("object_kind") or "",
                    "context": ctx,
                })

    written = 0
    with open(out, "w", encoding="utf-8") as fh:
        for genre in sorted(by_genre):
            rows = sorted(by_genre[genre],
                          key=lambda r: (r["chunk_id"], r["surface"], r["entity_type"]))
            step = max(1, len(rows) // per_stratum)
            picked = [rows[i] for i in range(0, len(rows), step)][:per_stratum]
            print(f"  {genre}: {len(picked)} sampled of {len(rows):,} mentions "
                  f"(stride {step})", file=sys.stderr)
            for i, r in enumerate(picked):
                r["item_id"] = f"{genre}-{i:03d}"
                r["in_ontology"] = r["entity_type"] in ONTOLOGY_TYPES
                for dim in ("is_entity", "span_correct", "type_correct",
                            "graph_worthy"):
                    r[dim] = None
                r["reason"] = ""
                fh.write(json.dumps(r) + "\n")
                written += 1
    print(json.dumps({"worksheet": out, "items": written}))


def do_score(worksheet: str, out: str) -> int:
    spec_path = (os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                 + "/evals/gliner_entity_gate_v1.json")
    spec = json.load(open(spec_path))
    th = spec["proposed_thresholds"]
    rows = [json.loads(l) for l in open(worksheet, encoding="utf-8") if l.strip()]

    unjudged = [r["item_id"] for r in rows if r.get("graph_worthy") is None]
    if unjudged:
        print(f"REFUSING TO SCORE: {len(unjudged)} unjudged (first {unjudged[:5]})",
              file=sys.stderr)
        return 2

    n = len(rows)
    dims = ("is_entity", "span_correct", "type_correct", "graph_worthy")
    scores = {}
    for d in dims:
        k = sum(1 for r in rows if r.get(d) is True)
        lo, hi = wilson(k, n)
        scores[d] = {"n": n, "pass": k, "rate": round(k / n, 4),
                     "wilson_95": [round(lo, 4), round(hi, 4)]}

    per_stratum = {}
    for g in sorted({r["genre"] for r in rows}):
        sub = [r for r in rows if r["genre"] == g]
        k = sum(1 for r in sub if r.get("graph_worthy") is True)
        lo, hi = wilson(k, len(sub))
        per_stratum[g] = {"n": len(sub), "graph_worthy": round(k / len(sub), 4),
                          "wilson_95": [round(lo, 4), round(hi, 4)],
                          "meets_floor": (k / len(sub)) >= th["per_stratum_floor"]}

    # Type instability: same surface, different types across mentions.
    types_by_surface: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        types_by_surface[r["surface"].strip().lower()].add(r["entity_type"])
    unstable = {s: sorted(t) for s, t in types_by_surface.items() if len(t) > 1}

    outside = [r for r in rows if not r.get("in_ontology")]
    gw = scores["graph_worthy"]
    verdict = "PASS" if (
        gw["rate"] >= th["graph_worthy_floor"]
        and gw["wilson_95"][0] >= th["wilson_lower_bound_required"]
        and all(v["meets_floor"] for v in per_stratum.values())
    ) else "FAIL"

    result = {
        "schema": "polymath.gliner_entity_gate.v1.result",
        "n": n,
        "dimensions": scores,
        "primary_metric": "graph_worthy",
        "per_stratum": per_stratum,
        "thresholds_PROPOSED_owner_must_rule": th,
        "type_instability": {
            "surfaces_with_multiple_types": len(unstable),
            "share": round(len(unstable) / max(1, len(types_by_surface)), 4),
            "examples": dict(list(unstable.items())[:10]),
        },
        "outside_ontology": {
            "count": len(outside),
            "share": round(len(outside) / n, 4),
            "types": dict(Counter(r["entity_type"] for r in outside).most_common(12)),
            "note": "these fail the allowed_pairs gate regardless of quality",
        },
        "VERDICT": verdict,
    }
    open(out, "w").write(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--out", required=True)
    s.add_argument("--per-stratum", type=int, default=45)
    c = sub.add_parser("score")
    c.add_argument("--worksheet", required=True)
    c.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.cmd == "sample":
        do_sample(a.out, a.per_stratum)
        return 0
    return do_score(a.worksheet, a.out)


if __name__ == "__main__":
    raise SystemExit(main())
