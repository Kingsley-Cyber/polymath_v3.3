#!/usr/bin/env python3
"""Fail-safe OFF→ON GPU-arbiter promotion; any RED rolls back to OFF."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Callable

from apple_mlx_env_manifest import REQUIRED_KEYS, load_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts/install_apple_mlx_runtime.sh"
HARNESS = REPO_ROOT / "scripts/run_gpu_arbiter_live_gates.py"
LAUNCH_AGENT = "com.polymath.apple-ml"


class PromotionError(RuntimeError):
    """The promotion was RED and the wrapper invoked its fail-safe."""


class PromotionRunner:
    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        uid: int | None = None,
    ) -> None:
        self.runner = runner
        self.uid = os.getuid() if uid is None else uid

    def _run(
        self, command: list[str], *, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def _install(self, manifest_path: Path, values: dict[str, str]) -> None:
        environment = os.environ.copy()
        environment.update({key: values[key] for key in REQUIRED_KEYS})
        result = self._run(
            ["bash", str(INSTALLER), "--env-manifest", str(manifest_path)],
            environment=environment,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "")[-1000:]
            raise PromotionError(f"Apple ML install failed: {detail}")

    def _bootout(self) -> None:
        self._run(
            [
                "launchctl",
                "bootout",
                f"gui/{self.uid}/{LAUNCH_AGENT}",
            ]
        )

    def execute(
        self,
        *,
        off_manifest_path: Path,
        on_manifest_path: Path,
        corpus_id: str,
        auth_token_file: Path,
        output_dir: Path,
    ) -> dict:
        off = load_manifest(off_manifest_path, expected_arbiter_enabled=False)
        on = load_manifest(on_manifest_path, expected_arbiter_enabled=True)
        drift = [
            key
            for key in REQUIRED_KEYS
            if key != "ARBITER_ENABLED" and off[key] != on[key]
        ]
        if drift:
            raise PromotionError(
                f"OFF/ON manifests differ outside ARBITER_ENABLED: {drift}"
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        off_artifact = output_dir / "gpu_arbiter_off.json"
        on_artifact = output_dir / "gpu_arbiter_on.json"
        on_may_be_active = False
        try:
            self._install(off_manifest_path, off)
            capture = self._run(
                [
                    sys.executable,
                    str(HARNESS),
                    "capture-off",
                    "--output",
                    str(off_artifact),
                    "--corpus-id",
                    corpus_id,
                    "--auth-token-file",
                    str(auth_token_file),
                ]
            )
            if capture.returncode != 0:
                raise PromotionError(
                    f"OFF capture was RED: {(capture.stderr or capture.stdout)[-1000:]}"
                )
            on_may_be_active = True
            self._install(on_manifest_path, on)
            run = self._run(
                [
                    sys.executable,
                    str(HARNESS),
                    "run-on",
                    "--output",
                    str(on_artifact),
                    "--corpus-id",
                    corpus_id,
                    "--auth-token-file",
                    str(auth_token_file),
                    "--off-artifact",
                    str(off_artifact),
                ]
            )
            payload = (
                json.loads(on_artifact.read_text(encoding="utf-8"))
                if on_artifact.exists()
                else {}
            )
            if (
                run.returncode != 0
                or payload.get("gates", {}).get("passed") is not True
            ):
                raise PromotionError(
                    f"ON gates were RED: {(run.stderr or run.stdout)[-1000:]}"
                )
            return payload
        except BaseException:
            if on_may_be_active:
                try:
                    self._install(off_manifest_path, off)
                except BaseException:
                    # The wrapper's final invariant is stronger than availability:
                    # it cannot exit a RED path with the ON LaunchAgent running.
                    self._bootout()
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--off-env-manifest", required=True, type=Path)
    parser.add_argument("--on-env-manifest", required=True, type=Path)
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--auth-token-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    def terminate(signum, frame):
        del frame
        raise PromotionError(f"promotion interrupted by signal {signum}")

    prior_sigterm = signal.signal(signal.SIGTERM, terminate)
    try:
        result = PromotionRunner().execute(
            off_manifest_path=args.off_env_manifest,
            on_manifest_path=args.on_env_manifest,
            corpus_id=args.corpus_id,
            auth_token_file=args.auth_token_file,
            output_dir=args.output_dir,
        )
        print(
            json.dumps(
                {
                    "passed": True,
                    "seal_sha256": result.get("seal_sha256"),
                    "output": str(args.output_dir),
                },
                sort_keys=True,
            )
        )
        return 0
    except BaseException as exc:
        print(
            json.dumps(
                {
                    "passed": False,
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        signal.signal(signal.SIGTERM, prior_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
