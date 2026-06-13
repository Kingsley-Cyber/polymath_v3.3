#!/usr/bin/env python3
"""bootstrap_models.py — one-command extraction-model install for a fresh clone.

Downloads exactly the model files the local Ghost B extraction lane needs and
prints the env wiring for the machine you ran it on. Cross-platform (Mac
sidecar host, Windows/Linux GPU box) — only needs Python 3.10+ with
`huggingface_hub` (pip install huggingface_hub).

Lanes:
  --gliner torch   urchade/gliner_medium-v2.1 (full snapshot, ~600 MB) — the
                   torch lane used on Apple Silicon (MPS) and CUDA boxes.
  --gliner onnx    onnx-community/gliner_medium-v2.1 SELECTIVE files only
                   (tokenizer + fp32 + fp16, ~1.2 GB instead of the 3 GB+
                   full-repo snapshot gliner would otherwise pull at runtime).
                   Requires onnxruntime-gpu at runtime — on Blackwell+torch-cu130
                   that means the official CUDA-13 ORT nightly; see
                   CONTINUITY/local_ingestion_phase_a/SESSION_STATE_2026-06-11.md.
  --gliner both    both of the above.

  --glirel-custom  fetch the FINE-TUNED Ghost B GLiREL (~1.7 GB) — the one
                   custom production relation model — from HF Hub into
                   models/glirel_ghost_b_v1/best/, where the sidecar loads it
                   by default. This is the relation model that makes the local
                   graph typed; without it, extraction falls back to zero-shot.
  --glirel-zero-shot   also fetch jackboyla/glirel-large-v0 (~1.5 GB), the
                   zero-shot relation fallback used when you don't have the
                   fine-tuned Ghost B GLiREL checkpoint (GLIREL_CKPT_DIR).

Idempotent: re-running verifies and resumes; complete installs are no-ops.

Examples:
  # Full local stack (Mac sidecar): stock GLiNER + custom Ghost B GLiREL
  python scripts/bootstrap_models.py --gliner torch --glirel-custom
  python scripts/bootstrap_models.py --gliner onnx --glirel-custom   # GPU box
  python scripts/bootstrap_models.py --gliner both --glirel-custom

(Maintainer: publish the custom GLiREL once with
 scripts/publish_glirel_to_hf.py before users can --glirel-custom it.)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

GLINER_TORCH_REPO = "urchade/gliner_medium-v2.1"
GLINER_ONNX_REPO = "onnx-community/gliner_medium-v2.1"
GLIREL_ZERO_SHOT_REPO = "jackboyla/glirel-large-v0"
# Fine-tuned Ghost B GLiREL — the one CUSTOM production model. Published to
# HF Hub by scripts/publish_glirel_to_hf.py; override the repo via env or
# --glirel-repo if you forked it to your own account.
GLIREL_CUSTOM_REPO = (
    os.environ.get("GHOST_B_GLIREL_HF_REPO") or "Sambenja1/glirel-ghost-b-v1"
)

# Selective ONNX pull: tokenizer/config + fp32 + fp16. Quantized variants
# (int8/q4/...) are excluded — they failed no gate yet but fp16 already showed
# entity drops with zero speed gain, so smaller variants are presumed worse.
ONNX_INCLUDE = ["*.json", "spm.model", "onnx/model.onnx", "onnx/model_fp16.onnx"]

# Sanity floors (bytes) — catches truncated/aborted downloads masquerading as
# complete. Generous lower bounds, not exact sizes.
MIN_SIZES = {
    "onnx/model.onnx": 500_000_000,
    "onnx/model_fp16.onnx": 250_000_000,
    "tokenizer.json": 1_000_000,
    "pytorch_model.bin": 1_500_000_000,  # fine-tuned GLiREL is ~1.7 GB
}


def _default_dest(repo_root: Path) -> Path:
    return repo_root / "local_ghost_b" / "models"


def _download(repo: str, dest: Path, include: list[str] | None) -> Path:
    from huggingface_hub import snapshot_download
    kwargs = {"repo_id": repo, "local_dir": str(dest)}
    if include:
        kwargs["allow_patterns"] = include
    print(f"→ {repo}" + (f"  (selective: {', '.join(include)})" if include else " (full)"))
    path = Path(snapshot_download(**kwargs))
    return path


def _verify(dest: Path, expect: list[str]) -> list[str]:
    problems = []
    for rel in expect:
        p = dest / rel
        if not p.exists():
            problems.append(f"missing: {p}")
            continue
        floor = MIN_SIZES.get(rel)
        if floor and p.stat().st_size < floor:
            problems.append(f"truncated: {p} ({p.stat().st_size} bytes)")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gliner", choices=["torch", "onnx", "both"], required=True)
    ap.add_argument("--glirel-custom", action="store_true",
                    help="fetch the fine-tuned Ghost B GLiREL (the custom "
                         "production relation model) from HF Hub")
    ap.add_argument("--glirel-repo", default=GLIREL_CUSTOM_REPO,
                    help=f"HF repo for the custom GLiREL (default: {GLIREL_CUSTOM_REPO})")
    ap.add_argument("--glirel-zero-shot", action="store_true",
                    help="also fetch the zero-shot GLiREL fallback model")
    ap.add_argument("--dest", default=None,
                    help="models dir (default: <repo>/local_ghost_b/models)")
    args = ap.parse_args()

    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        print("huggingface_hub is required:  pip install huggingface_hub")
        return 1

    repo_root = Path(__file__).resolve().parents[1]
    dest_root = Path(args.dest).resolve() if args.dest else _default_dest(repo_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    wiring: list[str] = []

    if args.gliner in ("torch", "both"):
        d = dest_root / "gliner_medium_v2.1"
        _download(GLINER_TORCH_REPO, d, None)
        problems += _verify(d, ["gliner_config.json"])
        wiring.append(
            "torch lane: no env needed — pipeline_config.GLINER_MODEL defaults to "
            f"{GLINER_TORCH_REPO} (HF cache also works); local copy at {d}")

    if args.gliner in ("onnx", "both"):
        d = dest_root / "gliner_onnx_medium_v2.1"
        _download(GLINER_ONNX_REPO, d, ONNX_INCLUDE)
        problems += _verify(
            d, ["gliner_config.json", "tokenizer.json", "spm.model",
                "onnx/model.onnx", "onnx/model_fp16.onnx"])
        wiring.append(
            "onnx lane envs for the sidecar process:\n"
            "    GHOST_B_GLINER_ONNX=1\n"
            f"    GHOST_B_GLINER_ONNX_REPO={d}\n"
            "    GHOST_B_GLINER_ONNX_DEVICE=cuda   # or cpu\n"
            "    (point _REPO at this LOCAL DIR — letting gliner snapshot the HF\n"
            "     repo at runtime downloads every quantized variant, 3 GB+)")

    if args.glirel_custom:
        # MUST land at the production loader's default path
        # (<repo>/models/glirel_ghost_b_v1/best), NOT under --dest — that's
        # where ghost_b_local resolves GLiREL when GLIREL_CKPT_DIR is unset.
        d = repo_root / "models" / "glirel_ghost_b_v1" / "best"
        _download(args.glirel_repo, d, None)
        problems += _verify(d, ["glirel_config.json", "labels.json",
                                "pytorch_model.bin"])
        wiring.append(
            f"custom GLiREL (Ghost B v1) at {d} — the extraction sidecar loads "
            "it automatically (no env needed). To use a HF repo id directly "
            f"instead of a local copy, set GLIREL_CKPT_DIR={args.glirel_repo}.")

    if args.glirel_zero_shot:
        d = dest_root / "glirel_large_v0"
        _download(GLIREL_ZERO_SHOT_REPO, d, None)
        problems += _verify(d, ["config.json"])
        wiring.append(
            f"glirel zero-shot fallback at {d} — used automatically when "
            "GLIREL_CKPT_DIR is unset AND no custom checkpoint is present "
            "(fine-tuned Ghost B checkpoint is preferred when you have it)")

    print()
    if problems:
        print("VERIFY FAILED:")
        for p in problems:
            print(" ", p)
        print("Re-run to resume the download.")
        return 1
    print("VERIFY OK — all requested model files present.\n")
    print("Wiring:")
    for w in wiring:
        print("  •", w)
    print("\nValidate the deployment from the app: Settings → Ingestion → "
          "Extraction Engines → Validate (or GET /api/settings/extraction/validate).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
