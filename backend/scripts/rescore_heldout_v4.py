#!/usr/bin/env python3
"""Scorer v4: lead-anchored refusal detection, applied SYMMETRICALLY offline.

Evidence and rationale: docs/baselines/EVAL_POSTS2_COMPARISON_RECEIPT_2026-07-14.md
(documented BEFORE this rescore). The v3 REFUSAL_RE matched honest scoping
phrases ("the sources do not name X specifically, but ...") ANYWHERE in the
answer, mis-grading substantive answers as refusals — and post-change answers
scope more honestly, inflating false positives asymmetrically.

v4 rule: an answer counts as a refusal/absence-acknowledgment ONLY when the
refusal pattern appears in the answer LEAD (the stored answer_head, first 220
chars) — i.e. refusal is the answer's thesis, not a passing hedge. Same regex
vocabulary as v3, same thresholds, same denominators; only the anchoring
changes. Applied to pre AND post runs identically.

Usage: python3 backend/scripts/rescore_heldout_v4.py <eval.json> [...]
Writes <name>_rescored_v4.json next to each input + prints per-file deltas.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Same pattern vocabulary as run_heldout_eval.py v3, unchanged.
REFUSAL_RE = re.compile(
    r"i cannot answer|did not find source evidence|"
    r"cannot answer that as a source-backed|"
    r"(?:do(?:es)?(?: not|n't)|is not|are not)\s+(?:\w+\s+){0,2}?"
    r"(?:address|cover|contain|mention|name|state|establish|detail|describe|"
    r"include|provide|specify|recommend)",
    re.IGNORECASE,
)

LEAD_CHARS = 220  # == stored answer_head length; lead-anchoring by construction


def rescore(path: Path) -> dict:
    data = json.loads(path.read_text())
    changed = []
    for row in data["results"]:
        old_ok = row.get("answerability_ok")
        if row["shape"] == "negative_control":
            # Fabrication-avoidance is a WHOLE-answer property; the runtime
            # scorer graded the full text. Offline we only have the 220-char
            # head, so negatives keep their recorded grade unchanged.
            row["answerability_ok_v3"] = old_ok
            continue
        lead = (row.get("answer_head") or "")[:LEAD_CHARS]
        refused_v4 = bool(REFUSAL_RE.search(lead))
        new_ok = not refused_v4
        if old_ok is not None and new_ok != old_ok:
            changed.append((row["id"], row["shape"], old_ok, new_ok))
        row["refused_v4"] = refused_v4
        row["answerability_ok_v3"] = old_ok
        row["answerability_ok"] = new_ok
    scored = [r for r in data["results"] if not r.get("error")]
    vals = [r["answerability_ok"] for r in scored if r["answerability_ok"] is not None]
    data["summary"]["answerability_ok_rate_v3"] = data["summary"].get("answerability_ok_rate")
    data["summary"]["answerability_ok_rate"] = round(sum(vals) / len(vals), 3) if vals else None
    data["summary"]["scorer"] = "v4-lead-anchored (offline symmetric rescore)"
    by_shape = data["summary"].get("by_shape") or {}
    for shape, bucket in by_shape.items():
        rows = [r for r in scored if r["shape"] == shape]
        bucket["ans_ok"] = sum(1 for r in rows if r["answerability_ok"])
    out = path.with_name(path.stem + "_rescored_v4.json")
    out.write_text(json.dumps(data, indent=2, default=str) + "\n")
    return {"file": path.name, "out": out.name,
            "rate_v3": data["summary"]["answerability_ok_rate_v3"],
            "rate_v4": data["summary"]["answerability_ok_rate"],
            "flips": changed,
            "negatives_v4": by_shape.get("negative_control", {}).get("ans_ok")}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    for arg in sys.argv[1:]:
        r = rescore(Path(arg))
        print(f"\n{r['file']} -> {r['out']}")
        print(f"  answerability_ok_rate: v3={r['rate_v3']} -> v4={r['rate_v4']}  negatives_v4={r['negatives_v4']}/5")
        for qid, shape, old, new in r["flips"]:
            print(f"    flip {qid} {shape}: {old} -> {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
