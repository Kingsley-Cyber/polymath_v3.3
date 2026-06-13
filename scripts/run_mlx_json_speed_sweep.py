#!/usr/bin/env python3
"""Run a same-fixture MLX JSON extraction speed sweep, then remove models.

This runner intentionally executes each model in a subprocess so memory is
released between models. It uses bench_mlx_fused_extraction_model.py because
that benchmark asks for the same Polymath JSON object contract:

    {"entities": [...], "relations": [...], "facts": [...]}

After all runs finish, it removes the exact Hugging Face cache repo directories
for the tested models.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = Path("/tmp/polymath_mlx_json_speed_sweep")
DEFAULT_SAMPLES = REPO_ROOT / "scripts/local_extraction_fixtures/13_building_ai_mobile_apps_10_chunks.jsonl"
DEFAULT_GOLD = REPO_ROOT / "scripts/local_extraction_fixtures/13_building_ai_mobile_apps_gold_v1.json"
HF_HUB = Path.home() / ".cache/huggingface/hub"


MODELS: list[dict[str, str]] = [
    {
        "key": "qwen25_3b",
        "label": "Qwen2.5-3B-Instruct 4bit MLX",
        "repo": "mlx-community/Qwen2.5-3B-Instruct-4bit",
    },
    {
        "key": "llama32_3b",
        "label": "Llama-3.2-3B-Instruct 4bit MLX",
        "repo": "mlx-community/Llama-3.2-3B-Instruct-4bit",
    },
    {
        "key": "smollm2_17b",
        "label": "SmolLM2-1.7B-Instruct 4bit MLX",
        "repo": "Irfanuruchi/SmolLM2-1.7B-Instruct-MLX-4bit",
    },
    {
        "key": "phi35_mini",
        "label": "Phi-3.5-mini-instruct 4bit MLX",
        "repo": "mlx-community/Phi-3.5-mini-instruct-4bit",
    },
]


def cache_dir_for_repo(repo_id: str) -> Path:
    return HF_HUB / ("models--" + repo_id.replace("/", "--"))


def path_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() or item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def run_command(cmd: list[str], *, cwd: Path) -> tuple[int, str, str, float]:
    started = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env={**os.environ, "HF_HUB_DISABLE_XET": "1"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr, time.perf_counter() - started


def load_report(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def extract_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    payload = report.get("payload")
    if isinstance(payload, dict):
        return dict(payload.get("summary") or {})
    return dict(report.get("summary") or {})


def cleanup_model_cache(repo_id: str, *, dry_run: bool) -> dict[str, Any]:
    cache_dir = cache_dir_for_repo(repo_id)
    before_bytes = path_size_bytes(cache_dir)
    result = {
        "repo": repo_id,
        "cache_dir": str(cache_dir),
        "existed_before_cleanup": cache_dir.exists(),
        "size_before_cleanup_bytes": before_bytes,
        "size_before_cleanup": human_size(before_bytes),
        "deleted": False,
        "exists_after_cleanup": cache_dir.exists(),
    }
    if cache_dir.exists() and not dry_run:
        shutil.rmtree(cache_dir)
        result["deleted"] = True
    result["exists_after_cleanup"] = cache_dir.exists()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--max-tokens", type=int, default=900)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--skip-cleanup", action="store_true")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []

    for model in MODELS:
        report_path = args.out_dir / f"{model['key']}_report.json"
        rubric_path = args.out_dir / f"{model['key']}_rubric.json"
        print(f"\n=== {model['label']} ===", flush=True)
        print(f"repo: {model['repo']}", flush=True)
        bench_cmd = [
            sys.executable,
            str(REPO_ROOT / "scripts/bench_mlx_fused_extraction_model.py"),
            "--model",
            model["repo"],
            "--samples",
            str(args.samples),
            "--gold",
            str(args.gold),
            "--out",
            str(report_path),
            "--limit",
            str(args.limit),
            "--max-tokens",
            str(args.max_tokens),
            "--temperature",
            str(args.temperature),
        ]
        code, stdout, stderr, wall_s = run_command(bench_cmd, cwd=REPO_ROOT)
        print(stdout, flush=True)
        if stderr.strip():
            print(stderr[-4000:], file=sys.stderr, flush=True)

        report = load_report(report_path)
        summary = extract_summary(report)
        rubric_summary: dict[str, Any] = {}
        if code == 0 and report:
            rubric_cmd = [
                sys.executable,
                str(REPO_ROOT / "scripts/grade_extraction_quality_rubric.py"),
                "--report",
                str(report_path),
                "--gold",
                str(args.gold),
                "--samples",
                str(args.samples),
                "--out",
                str(rubric_path),
            ]
            rubric_code, rubric_stdout, rubric_stderr, _ = run_command(rubric_cmd, cwd=REPO_ROOT)
            print(rubric_stdout, flush=True)
            if rubric_stderr.strip():
                print(rubric_stderr[-4000:], file=sys.stderr, flush=True)
            rubric = load_report(rubric_path) if rubric_code == 0 else None
            if rubric:
                rubric_summary = dict((rubric.get("rubric") or {}).get("score_100") or {})

        rows.append(
            {
                "key": model["key"],
                "label": model["label"],
                "repo": model["repo"],
                "returncode": code,
                "benchmark_wall_s": wall_s,
                "report_path": str(report_path),
                "rubric_path": str(rubric_path) if rubric_path.exists() else None,
                "stdout_tail": stdout[-4000:],
                "stderr_tail": stderr[-4000:],
                "summary": {
                    "samples": summary.get("samples"),
                    "model_load_s": summary.get("model_load_s"),
                    "chunks_per_hour_wall": summary.get("chunks_per_hour_wall"),
                    "completion_tok_s_median": summary.get("completion_tok_s_median"),
                    "prompt_tokens_total": summary.get("prompt_tokens_total"),
                    "completion_tokens_total": summary.get("completion_tokens_total"),
                    "schema_pass": summary.get("schema_pass"),
                    "truncation_count": summary.get("truncation_count"),
                    "accepted_entities": summary.get("accepted_entities"),
                    "accepted_relations": summary.get("accepted_relations"),
                    "gate_failures": summary.get("gate_failures"),
                    "gold_score": summary.get("gold_score"),
                },
                "rubric_score": rubric_summary,
            }
        )

    cleanup = [
        cleanup_model_cache(model["repo"], dry_run=args.skip_cleanup)
        for model in MODELS
    ]

    output = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "schema": "mlx_json_speed_sweep_v1",
        "samples": str(args.samples),
        "gold": str(args.gold),
        "limit": args.limit,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "models": rows,
        "cleanup": cleanup,
    }
    summary_path = args.out_dir / "summary.json"
    summary_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== SWEEP SUMMARY ===")
    for row in rows:
        summary = row["summary"]
        score = row["rubric_score"]
        status = "ok" if row["returncode"] == 0 else f"failed:{row['returncode']}"
        print(
            f"{row['key']} {status} "
            f"tok/s={summary.get('completion_tok_s_median')} "
            f"chunks/hr={summary.get('chunks_per_hour_wall')} "
            f"schema={summary.get('schema_pass')}/{summary.get('samples')} "
            f"E/R={summary.get('accepted_entities')}/{summary.get('accepted_relations')} "
            f"quality={score.get('quality_score')}"
        )
    print("\n=== CLEANUP ===")
    for item in cleanup:
        print(
            f"{item['repo']} deleted={item['deleted']} "
            f"size={item['size_before_cleanup']} exists_after={item['exists_after_cleanup']}"
        )
    print(f"wrote {summary_path}")


if __name__ == "__main__":
    main()
