#!/usr/bin/env python3
"""Profile-aware Polymath runtime download/setup assistant.

This is the "one command before docker compose" entrypoint for fresh machines.
It detects (or accepts) the local device profile, stages the deterministic
runtime layout, writes the matching .env knobs, and downloads only the model
assets that profile needs.

Profiles:
  apple-mlx   Apple Silicon host-native MLX embed/rerank sidecars.
  rtx         NVIDIA/RTX Docker GPU embedder + llama.cpp reranker.
  cpu-cloud   No local GPU model services; use configured cloud/API models.

Examples:
  python3 scripts/polymath_download.py plan
  python3 scripts/polymath_download.py apply --profile auto
  python3 scripts/polymath_download.py apply --profile rtx --start
  python3 scripts/polymath_download.py apply --profile apple-mlx --skip-docker-up
  python3 scripts/polymath_download.py verify
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

Profile = Literal["apple-mlx", "rtx", "cpu-cloud"]


@dataclass(frozen=True)
class DownloadPlan:
    profile: Profile
    detected: dict[str, Any]
    runtime_root: str
    ingest_source_root: str
    compose_profiles: str
    compose_files: list[str]
    bootstrap_script: str
    bootstrap_stage_models: bool
    apple_mlx_sidecars: bool
    docker_up_command: list[str]
    env: dict[str, str]
    models: list[dict[str, str]]
    notes: list[str]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_runtime_root() -> Path:
    if os.name == "nt":
        return Path("C:/PolymathRuntime")
    return Path.home() / "PolymathRuntime"


def _normalize_path(path: str | Path) -> str:
    return str(Path(path).expanduser()).replace("\\", "/")


def _env_file(repo_root: Path) -> Path:
    return repo_root / ".env"


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _set_env_values(path: Path, updates: dict[str, str], *, dry_run: bool = False) -> None:
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    next_lines: list[str] = []
    for line in existing:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            next_lines.append(line)
            continue
        key, _value = line.split("=", 1)
        clean_key = key.strip()
        if clean_key in remaining:
            next_lines.append(f"{clean_key}={remaining.pop(clean_key)}")
        else:
            next_lines.append(line)
    for key, value in remaining.items():
        next_lines.append(f"{key}={value}")
    if dry_run:
        for key, value in updates.items():
            print(f"[dry-run] set {key}={value}")
        return
    path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")


def _run(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
) -> None:
    printable = " ".join(cmd)
    print(f"[download] {printable}")
    if dry_run:
        return
    subprocess.run(cmd, cwd=str(cwd), env=env, check=True)


def _detect_nvidia() -> dict[str, Any]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"present": False, "reason": "nvidia-smi not found"}
    try:
        proc = subprocess.run(
            [
                exe,
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader",
            ],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - host dependent
        return {"present": False, "reason": str(exc)}
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return {
        "present": proc.returncode == 0 and bool(lines),
        "gpus": lines,
        "reason": proc.stderr.strip() if proc.returncode else "",
    }


def detect_profile() -> tuple[Profile, dict[str, Any]]:
    system = platform.system()
    machine = platform.machine()
    nvidia = _detect_nvidia()
    detected = {
        "system": system,
        "machine": machine,
        "python": platform.python_version(),
        "nvidia": nvidia,
    }
    if system == "Darwin" and machine == "arm64":
        return "apple-mlx", detected
    if nvidia.get("present"):
        return "rtx", detected
    return "cpu-cloud", detected


def build_plan(
    *,
    profile: str,
    runtime_root: str | None,
    ingest_source_root: str | None,
) -> DownloadPlan:
    detected_profile, detected = detect_profile()
    resolved_profile = detected_profile if profile == "auto" else profile
    if resolved_profile not in {"apple-mlx", "rtx", "cpu-cloud"}:
        raise SystemExit(f"Unknown profile: {profile}")

    runtime = Path(runtime_root).expanduser() if runtime_root else _default_runtime_root()
    ingest_root = (
        Path(ingest_source_root).expanduser()
        if ingest_source_root
        else runtime / "ingest-source"
    )
    runtime_s = _normalize_path(runtime)
    ingest_s = _normalize_path(ingest_root)
    models_root = _normalize_path(runtime / "models")
    binds_root = _normalize_path(runtime / "binds")

    common_env = {
        "POLYMATH_DOCKER_DATA_ROOT": runtime_s,
        "POLYMATH_RUNTIME_BINDS_ROOT": binds_root,
        "POLYMATH_CACHE_ROOT": runtime_s,
        "POLYMATH_MODELS_ROOT": models_root,
        "POLYMATH_INGEST_SOURCE_ROOT": ingest_s,
        "GRAPH_CACHE_WARMUP_SKIP_DURING_ACTIVE_INGEST": "true",
        "GRAPH_CACHE_WARMUP_ACTIVE_INGEST_DEFER_SECONDS": "120",
    }

    if resolved_profile == "apple-mlx":
        compose_profiles = "mcp"
        compose_files = ["docker-compose.yml", "docker-compose.apple-mlx.yml"]
        env = {
            **common_env,
            "COMPOSE_PROFILES": compose_profiles,
            "LOCAL_EMBEDDER_ENABLED": "true",
            "LOCAL_RERANKER_ENABLED": "true",
            "RERANKER_SCORE_SCALE": "cosine",
            "RERANKER_MODEL": "mlx-community/jina-reranker-v3-4bit-mxfp4",
            "EMBED_BATCH_SIZE": "32",
            "LOCAL_EMBED_BATCH_SIZE": "32",
            "DOCLING_SIDECAR_POLICY": "off",
        }
        models = [
            {
                "role": "embedder",
                "repo": "mlx-community/Qwen3-Embedding-0.6B-mxfp8",
                "location": f"{runtime_s}/volumes/hf-cache",
            },
            {
                "role": "reranker",
                "repo": "mlx-community/jina-reranker-v3-4bit-mxfp4",
                "location": f"{runtime_s}/volumes/hf-cache",
            },
        ]
        notes = [
            "Apple GPU cannot be passed into Docker; host-native MLX sidecars are used.",
            "Docker services embedder/reranker/docling are disabled by docker-compose.apple-mlx.yml.",
        ]
    elif resolved_profile == "rtx":
        compose_profiles = "local-embed,local-rerank,local-parser,mcp"
        compose_files = ["docker-compose.yml"]
        env = {
            **common_env,
            "COMPOSE_PROFILES": compose_profiles,
            "LOCAL_EMBEDDER_ENABLED": "true",
            "LOCAL_RERANKER_ENABLED": "true",
            "RERANKER_SCORE_SCALE": "probability",
            "RERANKER_MODEL": "qwen3-reranker-0.6b-q8_0",
            "LLAMA_CPP_SERVER_IMAGE": "ghcr.io/ggml-org/llama.cpp:server-cuda",
            "LLAMA_RERANKER_GPU_LAYERS": "99",
            "LOCAL_EMBED_BATCH_SIZE": "8",
            "EMBED_BATCH_SIZE": "8",
            "DOCLING_SIDECAR_POLICY": "auto",
        }
        models = [
            {
                "role": "embedder",
                "repo": "Qwen/Qwen3-Embedding-0.6B",
                "location": f"{models_root}/Qwen3-Embedding-0.6B",
            },
            {
                "role": "reranker",
                "repo": "ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF",
                "location": f"{models_root}/Qwen3-Reranker-0.6B-Q8_0-GGUF",
            },
        ]
        notes = [
            "NVIDIA/RTX uses Docker GPU services and CUDA llama.cpp reranking.",
            "Embedding batch defaults are conservative; raise after watching nvidia-smi.",
        ]
    else:
        compose_profiles = "mcp"
        compose_files = ["docker-compose.yml"]
        env = {
            **common_env,
            "COMPOSE_PROFILES": compose_profiles,
            "LOCAL_EMBEDDER_ENABLED": "false",
            "LOCAL_RERANKER_ENABLED": "false",
            "DOCLING_SIDECAR_POLICY": "off",
        }
        models = []
        notes = [
            "CPU/cloud profile avoids local GPU model downloads.",
            "Configure cloud embeddings/chat models in the UI or .env before heavy ingestion.",
        ]

    docker_up = ["docker", "compose"]
    for compose_file in compose_files:
        docker_up.extend(["-f", compose_file])
    docker_up.extend(["up", "-d", "--build"])

    return DownloadPlan(
        profile=resolved_profile,  # type: ignore[arg-type]
        detected=detected,
        runtime_root=runtime_s,
        ingest_source_root=ingest_s,
        compose_profiles=compose_profiles,
        compose_files=compose_files,
        bootstrap_script="scripts/bootstrap-runtime.ps1" if os.name == "nt" else "scripts/bootstrap-runtime.sh",
        bootstrap_stage_models=resolved_profile == "rtx",
        apple_mlx_sidecars=resolved_profile == "apple-mlx",
        docker_up_command=docker_up,
        env=env,
        models=models,
        notes=notes,
    )


def _write_manifest(repo_root: Path, plan: DownloadPlan, *, dry_run: bool = False) -> Path:
    runtime = Path(plan.runtime_root).expanduser()
    manifest = runtime / "polymath-download-plan.json"
    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(repo_root),
        **asdict(plan),
    }
    if dry_run:
        print(json.dumps(doc, indent=2))
        return manifest
    runtime.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"[download] wrote manifest: {manifest}")
    return manifest


def _bootstrap_args(plan: DownloadPlan) -> list[str]:
    if os.name == "nt":
        args = [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            plan.bootstrap_script,
            "-RuntimeRoot",
            plan.runtime_root,
            "-IngestSourceRoot",
            plan.ingest_source_root,
            "-ComposeProfiles",
            plan.compose_profiles,
            "-GenerateSecrets",
        ]
        if plan.bootstrap_stage_models:
            args.append("-StageModels")
        return args
    args = [
        "bash",
        plan.bootstrap_script,
        "--runtime-root",
        plan.runtime_root,
        "--ingest-source-root",
        plan.ingest_source_root,
        "--compose-profiles",
        plan.compose_profiles,
        "--generate-secrets",
    ]
    if plan.bootstrap_stage_models:
        args.append("--stage-models")
    return args


def apply_plan(
    repo_root: Path,
    plan: DownloadPlan,
    *,
    start: bool,
    skip_docker_up: bool,
    dry_run: bool,
) -> None:
    if plan.apple_mlx_sidecars and (
        platform.system() != "Darwin" or platform.machine() != "arm64"
    ):
        raise SystemExit("apple-mlx profile can only be applied on Darwin/arm64.")

    env = os.environ.copy()
    env.update(plan.env)
    _run(_bootstrap_args(plan), cwd=repo_root, env=env, dry_run=dry_run)
    _set_env_values(_env_file(repo_root), plan.env, dry_run=dry_run)

    if plan.apple_mlx_sidecars:
        setup_args = [
            "bash",
            "scripts/setup_apple_mlx.sh",
            "--runtime-root",
            plan.runtime_root,
            "--ingest-source-root",
            plan.ingest_source_root,
            "--compose-profiles",
            plan.compose_profiles,
            "--skip-bootstrap",
        ]
        if skip_docker_up or not start:
            setup_args.append("--skip-docker-up")
        _run(setup_args, cwd=repo_root, env=env, dry_run=dry_run)
    elif start and not skip_docker_up:
        _run(plan.docker_up_command, cwd=repo_root, env=env, dry_run=dry_run)

    _write_manifest(repo_root, plan, dry_run=dry_run)


def verify_plan(repo_root: Path, plan: DownloadPlan, *, check_running: bool) -> None:
    _set_env_values(_env_file(repo_root), plan.env)
    if os.name == "nt":
        cmd = [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "scripts/check-install.ps1",
            "-RuntimeRoot",
            plan.runtime_root,
        ]
        if check_running:
            cmd.append("-CheckRunning")
    else:
        cmd = ["bash", "scripts/check-install.sh", "--runtime-root", plan.runtime_root]
        if check_running:
            cmd.append("--check-running")
    _run(cmd, cwd=repo_root)


def _print_human_plan(plan: DownloadPlan) -> None:
    print(f"Profile          : {plan.profile}")
    print(f"Detected         : {plan.detected['system']} / {plan.detected['machine']}")
    if plan.detected.get("nvidia", {}).get("present"):
        print(f"NVIDIA           : {plan.detected['nvidia'].get('gpus')}")
    print(f"Runtime root     : {plan.runtime_root}")
    print(f"Ingest root      : {plan.ingest_source_root}")
    print(f"Compose profiles : {plan.compose_profiles}")
    print(f"Compose files    : {', '.join(plan.compose_files)}")
    print(f"Stage models     : {plan.bootstrap_stage_models}")
    print(f"Apple MLX        : {plan.apple_mlx_sidecars}")
    print("Models:")
    if plan.models:
        for model in plan.models:
            print(f"  - {model['role']}: {model['repo']} -> {model['location']}")
    else:
        print("  - none for this profile")
    print("Notes:")
    for note in plan.notes:
        print(f"  - {note}")
    print("Start command:")
    print("  " + " ".join(plan.docker_up_command))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deterministic Polymath runtime/model download assistant.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=["plan", "apply", "verify"])
    parser.add_argument(
        "--profile",
        choices=["auto", "apple-mlx", "rtx", "cpu-cloud"],
        default="auto",
        help="Device/runtime profile. Default: auto-detect.",
    )
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--ingest-source-root", default=None)
    parser.add_argument("--json", action="store_true", help="Print plan JSON.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writes.")
    parser.add_argument("--start", action="store_true", help="Start Docker stack after apply.")
    parser.add_argument("--skip-docker-up", action="store_true", help="Do not start Docker.")
    parser.add_argument(
        "--check-running",
        action="store_true",
        help="During verify, also probe running localhost services.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = _repo_root()
    plan = build_plan(
        profile=args.profile,
        runtime_root=args.runtime_root,
        ingest_source_root=args.ingest_source_root,
    )
    if args.command == "plan":
        if args.json:
            print(json.dumps(asdict(plan), indent=2))
        else:
            _print_human_plan(plan)
        return 0
    if args.command == "apply":
        apply_plan(
            repo_root,
            plan,
            start=bool(args.start),
            skip_docker_up=bool(args.skip_docker_up),
            dry_run=bool(args.dry_run),
        )
        if not args.dry_run:
            verify_plan(repo_root, plan, check_running=False)
        return 0
    if args.command == "verify":
        verify_plan(repo_root, plan, check_running=bool(args.check_running))
        return 0
    raise SystemExit(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
