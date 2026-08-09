#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description="Compute speed ratio from baseline/refactor timing JSON")
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--refactor", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()
    b = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    r = json.loads(Path(args.refactor).read_text(encoding="utf-8"))
    bw = float(b.get("total_wall_seconds", 0))
    rw = float(r.get("total_wall_seconds", 0))
    result = {"baseline_wall_seconds": bw, "refactor_wall_seconds": rw, "speed_ratio_vs_relex": (bw/rw if rw else 0)}
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.out: Path(args.out).write_text(text, encoding="utf-8")
    print(text)

if __name__ == "__main__": main()
