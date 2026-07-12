"""Pre-warm and verify the MLX model cache for the Apple Silicon profile.

Model pulls are slow and noisy if they happen during first inference. Pulling
up-front turns "ingest stalls on first run" into a deterministic install step.
Re-running is cheap; Hugging Face skips already-cached files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class ModelSpec:
    role: str
    env_var: str
    default_repo_id: str
    required_files: tuple[str, ...]

    @property
    def repo_id(self) -> str:
        return os.environ.get(self.env_var, self.default_repo_id).strip()


MODEL_SPECS: tuple[ModelSpec, ...] = (
    ModelSpec(
        role="embedder",
        env_var="APPLE_MLX_EMBED_MODEL_ID",
        default_repo_id="mlx-community/Qwen3-Embedding-0.6B-mxfp8",
        required_files=(
            "config.json",
            "config_sentence_transformers.json",
            "model.safetensors",
            "model.safetensors.index.json",
            "modules.json",
            "tokenizer.json",
            "tokenizer_config.json",
        ),
    ),
    ModelSpec(
        role="reranker",
        env_var="APPLE_MLX_RERANKER_MODEL_ID",
        default_repo_id="mlx-community/jina-reranker-v3-4bit-mxfp4",
        required_files=(
            "config.json",
            "model.safetensors",
            "model.safetensors.index.json",
            "modeling.py",
            "tokenizer.json",
            "tokenizer_config.json",
        ),
    ),
    ModelSpec(
        role="reranker_cross_encoder",
        env_var="APPLE_TORCH_RERANKER_MODEL_ID",
        default_repo_id="jinaai/jina-reranker-v3",
        required_files=(
            "config.json",
            "model.safetensors",
            "modeling.py",
            "tokenizer.json",
            "tokenizer_config.json",
        ),
    ),
)

ALLOW_PATTERNS: list[str] = [
    "*.json",
    "*.txt",
    "*.md",
    "*.py",
    "*.safetensors",
    "*.npz",
    "*.model",
    "tokenizer*",
    "merges.txt",
    "vocab.json",
]


def _resolve_hf_home() -> Path:
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]).expanduser().resolve()
    runtime = os.environ.get("POLYMATH_DOCKER_DATA_ROOT") or str(Path.home() / "PolymathRuntime")
    return Path(runtime).expanduser().resolve() / "volumes" / "hf-cache"


def _resolve_hub_cache_dir(hf_home: Path) -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"]).expanduser().resolve()
    return hf_home / "hub"


def _human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and verify Polymath Apple MLX model snapshots.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Verify the cache without network downloads.",
    )
    parser.add_argument(
        "--manifest",
        default="",
        help=(
            "Optional manifest output path. Default: "
            "$HF_HOME/polymath-apple-mlx-models.json"
        ),
    )
    return parser.parse_args()


def _snapshot_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _verify_snapshot(spec: ModelSpec, local_path: Path) -> dict:
    missing = [name for name in spec.required_files if not (local_path / name).exists()]
    if missing:
        raise RuntimeError(
            f"{spec.role} snapshot for {spec.repo_id} is incomplete; "
            f"missing: {', '.join(missing)}"
        )
    size = _snapshot_size(local_path)
    return {
        "role": spec.role,
        "repo_id": spec.repo_id,
        "local_path": str(local_path),
        "size_bytes": size,
        "size": _human_size(size),
        "required_files": list(spec.required_files),
    }


def main() -> int:
    args = _parse_args()
    hf_home = _resolve_hf_home()
    hub_cache_dir = _resolve_hub_cache_dir(hf_home)
    hub_cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(hf_home)
    os.environ["HF_HUB_CACHE"] = str(hub_cache_dir)

    print(f"[apple-mlx] HF_HOME = {hf_home}")
    print(f"[apple-mlx] HF_HUB_CACHE = {hub_cache_dir}")

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print(
            "ERROR: huggingface_hub not installed. Run install_apple_mlx_runtime.sh "
            "first or pip install -r scripts/apple_ml_services/requirements.txt",
            file=sys.stderr,
        )
        return 1

    manifest: list[dict] = []

    for spec in MODEL_SPECS:
        repo_id = spec.repo_id
        action = "checking" if args.check_only else "pulling"
        print(f"[apple-mlx] {action} {repo_id} ({spec.role})")
        try:
            local_path = snapshot_download(
                repo_id=repo_id,
                cache_dir=str(hub_cache_dir),
                allow_patterns=ALLOW_PATTERNS,
                local_files_only=args.check_only,
            )
            entry = _verify_snapshot(spec, Path(local_path))
            manifest.append(entry)
            print(f"  ok: {entry['local_path']}  ({entry['size']})")
        except Exception as exc:  # pragma: no cover — surface install issues clearly
            print(f"ERROR {action} {repo_id}: {exc}", file=sys.stderr)
            return 1

    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest
        else hf_home / "polymath-apple-mlx-models.json"
    )
    manifest_doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hf_home": str(hf_home),
        "hf_hub_cache": str(hub_cache_dir),
        "models": manifest,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest_doc, indent=2), encoding="utf-8")

    print()
    print(f"[apple-mlx] cache ready. Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
