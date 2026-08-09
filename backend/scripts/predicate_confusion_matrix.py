"""P3 Predicate Confusion Matrix — diagnostic tool for wrong-predicate traces.

Reads unified shadow pipeline JSONL output and groups matched-gold records
where the gate predicate disagrees with the gold predicate into a confusion
matrix: gold_predicate → predicted_top_predicate.

For each confusion cell, reports:
  - count
  - entity type pairs
  - surface predicates observed
  - gold score / rank distribution
  - top score / margin distribution
  - reverse score distribution
  - syntax predicate agreement

Corrective priority (per AGENTS.MD):
  1. Endpoint compatibility
  2. Direction/inverse definition
  3. Surface-predicate mapping
  4. Predicate-pack overlap reduction
  5. Model-facing label wording
  6. Hard-negative collection
  7. Fine-tuning

Usage:
    python scripts/predicate_confusion_matrix.py <pipeline_output.jsonl>
    python scripts/predicate_confusion_matrix.py --run   # run pipeline first
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))


def load_results(path: Path) -> list[dict]:
    """Load pipeline JSONL output."""
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def extract_confusions(results: list[dict]) -> list[dict]:
    """Filter to matched-gold records where predicate disagrees."""
    confusions = []
    for r in results:
        if r.get("matched_gold") and not r.get("pred_match"):
            if r.get("gold_pred") and r.get("gate_pred"):
                confusions.append(r)
    return confusions


def build_matrix(confusions: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group confusions by (gold_pred, gate_pred)."""
    matrix: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in confusions:
        key = (c["gold_pred"], c["gate_pred"])
        matrix[key].append(c)
    return dict(matrix)


def _fmt_scores(values: list[float]) -> str:
    """Format a list of scores as min/median/max."""
    if not values:
        return "n/a"
    if len(values) == 1:
        return f"{values[0]:.3f}"
    return f"{min(values):.3f}/{median(values):.3f}/{max(values):.3f}"


def analyze_cell(records: list[dict]) -> dict:
    """Compute diagnostics for one confusion cell."""
    type_pairs = Counter(
        f"{r.get('subject_type', '?')}→{r.get('object_type', '?')}"
        for r in records
    )
    surface_preds = Counter(
        r.get("surface_predicate", "") or "(none)"
        for r in records
    )
    gold_scores = [r["gold_score"] for r in records if r.get("gold_score") is not None]
    gold_ranks = [r["gold_rank"] for r in records if r.get("gold_rank") is not None]
    top_scores = [r["score"] for r in records if r.get("score") is not None]
    margins = [r["margin"] for r in records if r.get("margin") is not None]
    dir_margins = [r["direction_margin"] for r in records if r.get("direction_margin") is not None]
    syntax_preds = Counter(
        sp
        for r in records
        for sp in (r.get("syntax_predicates") or [])
    )
    statuses = Counter(r.get("status", "?") for r in records)

    # Reverse score for the gold predicate (how strongly the model considers
    # the reverse direction)
    reverse_for_gold = []
    for r in records:
        rev = r.get("reverse_scores", {})
        gp = r.get("gold_pred")
        if gp and gp in rev:
            reverse_for_gold.append(rev[gp])

    return {
        "count": len(records),
        "type_pairs": dict(type_pairs.most_common(5)),
        "surface_predicates": dict(surface_preds.most_common(5)),
        "gold_score": _fmt_scores(gold_scores),
        "gold_rank": _fmt_scores([float(x) for x in gold_ranks]) if gold_ranks else "n/a",
        "top_score": _fmt_scores(top_scores),
        "margin": _fmt_scores(margins),
        "direction_margin": _fmt_scores(dir_margins),
        "reverse_for_gold": _fmt_scores(reverse_for_gold),
        "syntax_predicates": dict(syntax_preds.most_common(5)),
        "statuses": dict(statuses.most_common()),
    }


def suggest_corrective(gold_pred: str, gate_pred: str, analysis: dict) -> list[str]:
    """Suggest corrective actions based on the confusion pattern."""
    suggestions = []

    # 1. Endpoint compatibility: if type pairs are inconsistent with gold_pred
    type_pairs = analysis["type_pairs"]
    if type_pairs:
        suggestions.append(
            f"1. Check endpoint compatibility: types={list(type_pairs.keys())[:3]}"
        )

    # 2. Direction/inverse: if reverse_for_gold is high
    rev = analysis.get("reverse_for_gold", "n/a")
    if rev != "n/a" and "/" in rev:
        parts = rev.split("/")
        try:
            max_rev = float(parts[-1])
            if max_rev > 0.3:
                suggestions.append(
                    f"2. Direction/inverse: reverse_for_gold max={max_rev:.3f} — "
                    f"consider inverse definition"
                )
        except ValueError:
            pass

    # 3. Surface-predicate mapping
    surfaces = analysis["surface_predicates"]
    if surfaces:
        top_surface = list(surfaces.keys())[0]
        if top_surface != "(none)":
            suggestions.append(
                f"3. Surface mapping: '{top_surface}' → gate chose '{gate_pred}' "
                f"but gold is '{gold_pred}'"
            )

    # 4. Predicate-pack overlap
    suggestions.append(
        f"4. Pack overlap: '{gold_pred}' vs '{gate_pred}' — check if both are "
        f"in the same predicate pack and whether disambiguation is needed"
    )

    # 5. Gold rank
    rank_str = analysis.get("gold_rank", "n/a")
    if rank_str != "n/a":
        try:
            # If gold is rank 2+ consistently, the model scores the wrong
            # predicate higher
            suggestions.append(
                f"5. Gold rank distribution: {rank_str} — "
                f"{'model ranks gold low' if '2' in rank_str or '3' in rank_str else 'gold usually top-ranked (margin issue)'}"
            )
        except Exception:
            pass

    return suggestions


def print_report(matrix: dict[tuple[str, str], list[dict]], total_confusions: int) -> None:
    """Print the formatted confusion matrix report."""
    print("=" * 90)
    print("P3 PREDICATE CONFUSION MATRIX")
    print(f"Total wrong-predicate traces: {total_confusions}")
    print("=" * 90)

    # Sort by count descending
    sorted_cells = sorted(matrix.items(), key=lambda x: -len(x[1]))

    # Summary table
    print(f"\n{'Gold Predicate':<20s} {'Predicted':<20s} {'Count':>5s}  {'Gold Score':>14s}  {'Top Score':>14s}")
    print("-" * 80)
    for (gold, gate), records in sorted_cells:
        analysis = analyze_cell(records)
        print(f"{gold:<20s} {gate:<20s} {analysis['count']:>5d}  "
              f"{analysis['gold_score']:>14s}  {analysis['top_score']:>14s}")

    # Detailed per-cell analysis
    print(f"\n{'=' * 90}")
    print("DETAILED ANALYSIS")
    print("=" * 90)

    for (gold, gate), records in sorted_cells:
        analysis = analyze_cell(records)
        print(f"\n--- {gold} → {gate} (n={analysis['count']}) ---")
        print(f"  Type pairs:       {analysis['type_pairs']}")
        print(f"  Surface preds:    {analysis['surface_predicates']}")
        print(f"  Gold score:       {analysis['gold_score']}")
        print(f"  Gold rank:        {analysis['gold_rank']}")
        print(f"  Top score:        {analysis['top_score']}")
        print(f"  Margin:           {analysis['margin']}")
        print(f"  Direction margin: {analysis['direction_margin']}")
        print(f"  Reverse (gold):   {analysis['reverse_for_gold']}")
        print(f"  Syntax preds:     {analysis['syntax_predicates']}")
        print(f"  Gate statuses:    {analysis['statuses']}")

        suggestions = suggest_corrective(gold, gate, analysis)
        if suggestions:
            print(f"  Corrective actions:")
            for s in suggestions:
                print(f"    {s}")

    # Aggregate: most confused gold predicates
    print(f"\n{'=' * 90}")
    print("AGGREGATE: Most confused gold predicates")
    print("=" * 90)
    gold_totals: Counter = Counter()
    for (gold, _), records in matrix.items():
        gold_totals[gold] += len(records)
    for gold, count in gold_totals.most_common(10):
        print(f"  {gold:<25s} {count:>3d} wrong-predicate traces")

    # Aggregate: most frequent wrong predictions
    print(f"\nAGGREGATE: Most frequent wrong predictions")
    gate_totals: Counter = Counter()
    for (_, gate), records in matrix.items():
        gate_totals[gate] += len(records)
    for gate, count in gate_totals.most_common(10):
        print(f"  {gate:<25s} {count:>3d} times predicted incorrectly")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"ERROR: {path} not found")
        return 1

    results = load_results(path)
    print(f"Loaded {len(results)} records from {path.name}")

    confusions = extract_confusions(results)
    if not confusions:
        print("No wrong-predicate traces found. Nothing to analyze.")
        return 0

    matrix = build_matrix(confusions)
    print_report(matrix, len(confusions))

    # Write JSON output for downstream tooling
    out_path = path.parent / "predicate_confusion_matrix.json"
    json_matrix = {}
    for (gold, gate), records in sorted(matrix.items(), key=lambda x: -len(x[1])):
        key = f"{gold} -> {gate}"
        json_matrix[key] = analyze_cell(records)
    with open(out_path, "w") as f:
        json.dump(json_matrix, f, indent=2)
    print(f"\nJSON matrix written to: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
