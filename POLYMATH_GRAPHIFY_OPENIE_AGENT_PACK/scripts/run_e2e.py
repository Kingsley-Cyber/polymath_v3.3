#!/usr/bin/env python3
from __future__ import annotations
import argparse, subprocess, json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description="E2E harness wrapper. Agent must wire this to the repository's canonical Graphify command.")
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--graphify-cmd", required=True, help="Command template. Use {input} and {out} placeholders.")
    ap.add_argument("--out-dir", default="work/e2e_run")
    args = ap.parse_args()
    pack = Path(__file__).resolve().parents[1]
    out_dir = pack / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    fixtures = [pack/"fixtures/graphify_quality_fixture.md", pack/"fixtures/graphify_throughput_fixture.md"]
    runs = []
    for f in fixtures:
        out = out_dir / f"{f.stem}.out.json"
        cmd = args.graphify_cmd.format(input=str(f), out=str(out))
        proc = subprocess.run(cmd, cwd=args.repo_root, shell=True, text=True, capture_output=True)
        runs.append({"fixture": str(f), "cmd": cmd, "returncode": proc.returncode, "stdout_tail": proc.stdout[-2000:], "stderr_tail": proc.stderr[-2000:], "out": str(out)})
    result = {"runs": runs, "passed": all(r["returncode"] == 0 for r in runs)}
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(1)

if __name__ == "__main__": main()
