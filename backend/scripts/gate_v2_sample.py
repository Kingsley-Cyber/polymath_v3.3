#!/usr/bin/env python3
"""Gate v2 — build the judging worksheet, then score it.

Two phases, deliberately separated so the thresholds cannot be fitted to the
data (they are already committed in backend/evals/spacy_relation_gate_v2.json):

    sample : emit a JSONL worksheet of relations awaiting judgement
    score  : read the judged worksheet, compute precision + Wilson CI, apply
             the preregistered pass rules

Usage (host venue — needs config/*.yaml):
    PYTHONPATH=backend local_ghost_b/.venv/bin/python \
        backend/scripts/gate_v2_sample.py sample --in sample.jsonl --out work.jsonl
    PYTHONPATH=backend local_ghost_b/.venv/bin/python \
        backend/scripts/gate_v2_sample.py score --worksheet work.jsonl --out result.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

GATE_SPEC = (
    Path(__file__).resolve().parents[1] / "evals" / "spacy_relation_gate_v2.json"
)


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval. Correct at small n, unlike normal approx."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def do_sample(inp: str, out: str, per_stratum: int) -> int:
    from services.extraction.dep_path_extractor import new_counters
    from services.extraction.spacy_relation_adapter import get_spacy_extractor

    chunks = [json.loads(l) for l in open(inp, encoding="utf-8") if l.strip()]
    ext = get_spacy_extractor()

    by_stratum: dict[str, list[dict]] = defaultdict(list)
    for start in range(0, len(chunks), 200):
        batch = chunks[start:start + 200]
        payload = [{"chunk_id": c["chunk_id"], "doc_id": c["doc_id"],
                    "text": c["text"], "entities": c["entities"]} for c in batch]
        ctrs = [new_counters() for _ in payload]
        for c, edges in zip(batch, ext.extract_chunks(
                payload, max_related=10, suppression_counters_list=ctrs)):
            for e in edges:
                by_stratum[c["genre"]].append({
                    "chunk_id": c["chunk_id"],
                    "corpus_name": c.get("corpus_name", ""),
                    "genre": c["genre"],
                    "subject": e["sub"],
                    "predicate": e["pred"],
                    "object": e["obj"],
                    "confidence": e["score"],
                    "evidence": " ".join((e.get("ev") or "").split()),
                })

    written = 0
    with open(out, "w", encoding="utf-8") as fh:
        for genre in sorted(by_stratum):
            rows = sorted(
                by_stratum[genre],
                key=lambda r: (r["chunk_id"], r["subject"], r["predicate"], r["object"]),
            )
            step = max(1, len(rows) // per_stratum)
            picked = [rows[i] for i in range(0, len(rows), step)][:per_stratum]
            print(f"  {genre}: {len(picked)} sampled of {len(rows)} emitted "
                  f"(stride {step})", file=sys.stderr)
            for i, r in enumerate(picked):
                r["item_id"] = f"{genre}-{i:03d}"
                r["judgement"] = None   # CORRECT | WRONG | BORDERLINE
                r["reason"] = ""
                fh.write(json.dumps(r) + "\n")
                written += 1
    print(json.dumps({"worksheet": out, "items": written,
                      "per_stratum": {g: len(v) for g, v in by_stratum.items()}}))
    return written


def do_score(worksheet: str, out: str) -> int:
    spec = json.loads(GATE_SPEC.read_text())
    gate = spec["pass_gate"]
    rows = [json.loads(l) for l in open(worksheet, encoding="utf-8") if l.strip()]

    unjudged = [r["item_id"] for r in rows if not r.get("judgement")]
    if unjudged:
        print(f"REFUSING TO SCORE: {len(unjudged)} unjudged items "
              f"(first: {unjudged[:5]})", file=sys.stderr)
        return 2

    # Preregistered: BORDERLINE counts as WRONG.
    def is_correct(r: dict) -> bool:
        return r["judgement"] == "CORRECT"

    total = len(rows)
    correct = sum(1 for r in rows if is_correct(r))
    lo, hi = wilson(correct, total)

    per_stratum = {}
    for genre in sorted({r["genre"] for r in rows}):
        sub = [r for r in rows if r["genre"] == genre]
        c = sum(1 for r in sub if is_correct(r))
        slo, shi = wilson(c, len(sub))
        per_stratum[genre] = {
            "n": len(sub), "correct": c,
            "precision": round(c / len(sub), 4) if sub else 0.0,
            "wilson_95": [round(slo, 4), round(shi, 4)],
            "meets_stratum_floor": (c / len(sub)) >= gate["per_stratum_rule_floor"]
            if "per_stratum_rule_floor" in gate else (c / len(sub)) >= 0.65,
        }

    borderline = sum(1 for r in rows if r["judgement"] == "BORDERLINE")
    predicates = Counter(r["predicate"] for r in rows)

    point = correct / total if total else 0.0
    passes_point = point >= gate["precision_floor"]
    passes_ci = lo >= gate["ci_requirement"]["rule_lower_bound"] \
        if "rule_lower_bound" in gate["ci_requirement"] else lo >= 0.70
    excluded = [g for g, d in per_stratum.items() if not d["meets_stratum_floor"]]

    verdict = "PASS" if (passes_point and passes_ci and not excluded) else "FAIL"

    result = {
        "schema": "polymath.spacy_relation_gate.v2.result",
        "gate_spec_sha_source": str(GATE_SPEC),
        "n": total,
        "correct": correct,
        "borderline_counted_as_wrong": borderline,
        "precision": round(point, 4),
        "wilson_95": [round(lo, 4), round(hi, 4)],
        "thresholds": {
            "precision_floor": gate["precision_floor"],
            "wilson_lower_bound_required": 0.70,
            "per_stratum_floor": 0.65,
        },
        "passes_point_estimate": passes_point,
        "passes_ci_lower_bound": passes_ci,
        "strata_below_floor_excluded_from_backfill": excluded,
        "per_stratum": per_stratum,
        "distinct_predicates": len(predicates),
        "predicate_distribution": dict(predicates.most_common()),
        "VERDICT": verdict,
    }
    Path(out).write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--in", dest="inp", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--per-stratum", type=int, default=45)
    c = sub.add_parser("score")
    c.add_argument("--worksheet", required=True)
    c.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.cmd == "sample":
        do_sample(args.inp, args.out, args.per_stratum)
        return 0
    return do_score(args.worksheet, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
