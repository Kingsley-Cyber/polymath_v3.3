"""Pre-warm the HuggingFace cache with the MLX model weights used by
Polymath's Apple Silicon hybrid profile.

Why a script: model pulls are slow (1.5-3 GB combined) and noisy if done
during first inference. Pulling up-front turns "ingest fails on first run"
into a one-time install step.

Models pulled:
    mlx-community/Qwen3-Embedding-0.6B-mxfp8       (embeddings, 1024-dim)
    mlx-community/jina-reranker-v3-4bit-mxfp4      (reranker, cosine scores)

HF_HOME respected; defaults to ~/PolymathRuntime/volumes/hf-cache when
called from install_apple_mlx_runtime.sh.

Usage:
    HF_HOME=~/PolymathRuntime/volumes/hf-cache \\
        ~/PolymathRuntime/apple_ml_services/.venv/bin/python \\
        scripts/pull_apple_mlx_models.py

Re-running is cheap; HuggingFace skips already-cached files.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Models. If you swap to different MLX quantizations, list them here.
MODELS: list[str] = [
    "mlx-community/Qwen3-Embedding-0.6B-mxfp8",
    "mlx-community/jina-reranker-v3-4bit-mxfp4",
]


def _resolve_cache_dir() -> Path:
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]).expanduser().resolve()
    runtime = os.environ.get("POLYMATH_DOCKER_DATA_ROOT") or str(Path.home() / "PolymathRuntime")
    return Path(runtime).expanduser().resolve() / "volumes" / "hf-cache"


def _human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


def main() -> int:
    cache_dir = _resolve_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(cache_dir)

    print(f"[apple-mlx] HF_HOME = {cache_dir}")

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print(
            "ERROR: huggingface_hub not installed. Run install_apple_mlx_runtime.sh "
            "first or pip install -r scripts/apple_ml_services/requirements.txt",
            file=sys.stderr,
        )
        return 1

    for repo_id in MODELS:
        print(f"[apple-mlx] pulling {repo_id}")
        try:
            local_path = snapshot_download(
                repo_id=repo_id,
                cache_dir=str(cache_dir),
                # Skip large junk; mxfp* models keep weights as safetensors / npz.
                allow_patterns=[
                    "*.json",
                    "*.txt",
                    "*.safetensors",
                    "*.npz",
                    "tokenizer*",
                    "merges.txt",
                    "vocab.json",
                ],
            )
            size = sum(p.stat().st_size for p in Path(local_path).rglob("*") if p.is_file())
            print(f"  ok: {local_path}  ({_human_size(size)})")
        except Exception as exc:  # pragma: no cover — surface install issues clearly
            print(f"ERROR pulling {repo_id}: {exc}", file=sys.stderr)
            return 1

    print()
    print("[apple-mlx] cache ready. Sidecars will load these on first request.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
