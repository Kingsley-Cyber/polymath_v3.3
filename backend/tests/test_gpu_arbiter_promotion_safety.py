"""Build-only tests for manifest, transactional plist, and RED rollback laws."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from apple_mlx_env_manifest import (  # noqa: E402
    ManifestError,
    REQUIRED_KEYS,
    SCHEMA_VERSION,
    load_manifest,
    write_manifest,
)
from apple_mlx_launch_agent_transaction import (  # noqa: E402
    LaunchAgentTransaction,
    TransactionError,
)
from run_gpu_arbiter_promotion import PromotionError, PromotionRunner  # noqa: E402


def _environment(enabled: bool) -> dict[str, str]:
    values = {
        "POLYMATH_DOCKER_DATA_ROOT": "/Users/test/PolymathRuntime",
        "APPLE_MLX_EMBED_MODEL_ID": "mlx-community/Qwen3-Embedding-0.6B-mxfp8",
        "APPLE_MLX_RERANKER_MODEL_ID": "mlx-community/jina-reranker-v3-4bit-mxfp4",
        "APPLE_RERANKER_BACKEND": "torch_fp16",
        "APPLE_TORCH_RERANKER_MODEL_ID": "jinaai/jina-reranker-v3",
        "RERANKER_SCORE_SCALE": "probability",
        "EMBED_BATCH_SIZE": "256",
        "START_EMBEDDER": "true",
        "START_RERANKER": "true",
        "START_DOCLING": "false",
        "ARBITER_ENABLED": "true" if enabled else "false",
        "ARBITER_HOST": "127.0.0.1",
        "ARBITER_PORT": "8085",
        "ARBITER_ACQUIRE_TIMEOUT_SECONDS": "30",
        "ARBITER_EMBED_HOLD_TARGET_MS": "2000",
        "ARBITER_RERANK_HOLD_TARGET_MS": "500",
        "ARBITER_MAX_EMBED_BURST": "1",
        "ARBITER_RERANK_STARVATION_SECONDS": "0.5",
        "ARBITER_STALE_LEASE_SECONDS": "75",
    }
    assert set(values) == set(REQUIRED_KEYS)
    return values


def _manifest(tmp_path: Path, enabled: bool) -> Path:
    path = tmp_path / ("on.json" if enabled else "off.json")
    path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "environment": _environment(enabled),
            }
        ),
        encoding="utf-8",
    )
    return path


def _completed(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_manifest_requires_every_explicit_value_and_exact_process_match(tmp_path):
    path = _manifest(tmp_path, False)
    values = load_manifest(
        path,
        process_environment=_environment(False),
        expected_arbiter_enabled=False,
    )
    assert values["EMBED_BATCH_SIZE"] == "256"
    payload = json.loads(path.read_text())
    del payload["environment"]["EMBED_BATCH_SIZE"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManifestError, match="incomplete"):
        load_manifest(path)

    path = _manifest(tmp_path, False)
    mismatched = _environment(False)
    mismatched["EMBED_BATCH_SIZE"] = "32"
    with pytest.raises(ManifestError, match="differs"):
        load_manifest(path, process_environment=mismatched)


def test_setup_can_write_a_complete_private_manifest_without_implicit_installer_defaults(
    tmp_path,
):
    path = tmp_path / "environment.json"
    write_manifest(path, _environment(False))
    assert path.stat().st_mode & 0o777 == 0o600
    assert (
        load_manifest(path, expected_arbiter_enabled=False)["EMBED_BATCH_SIZE"] == "256"
    )
    setup = (SCRIPTS / "setup_apple_mlx.sh").read_text()
    assert "--write-manifest" in setup
    assert '--env-manifest "${mlx_env_manifest}"' in setup


def test_manifest_off_on_pair_cannot_hide_model_or_batch_drift(tmp_path):
    off = _manifest(tmp_path, False)
    on = _manifest(tmp_path, True)
    payload = json.loads(on.read_text())
    payload["environment"]["EMBED_BATCH_SIZE"] = "32"
    on.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PromotionError, match="differ outside"):
        PromotionRunner().execute(
            off_manifest_path=off,
            on_manifest_path=on,
            corpus_id="unused",
            auth_token_file=tmp_path / "unused",
            output_dir=tmp_path / "outputs",
        )


def test_transaction_restores_exact_prior_plist_after_bootstrap_failure(tmp_path):
    target = tmp_path / "agent.plist"
    target.write_bytes(b"prior-plist")
    target.chmod(0o600)
    expected = tmp_path / "expected.plist"
    expected.write_bytes(b"new-plist")
    bootstrap_calls = 0

    def runner(command, **kwargs):
        nonlocal bootstrap_calls
        del kwargs
        if command[1:2] == ["bootstrap"]:
            bootstrap_calls += 1
            if bootstrap_calls <= 5:
                return _completed(command, 1, stderr="synthetic bootstrap failure")
        return _completed(command)

    transaction = LaunchAgentTransaction(
        target_plist=target,
        label="com.test",
        uid=501,
        runner=runner,
        sleep_fn=lambda seconds: None,
    )
    with pytest.raises(TransactionError, match="prior plist was restored"):
        transaction.deploy(expected, ["smoke"])
    assert target.read_bytes() == b"prior-plist"
    assert target.stat().st_mode & 0o777 == 0o600
    assert bootstrap_calls == 6


def test_transaction_restores_prior_plist_after_smoke_failure(tmp_path):
    target = tmp_path / "agent.plist"
    target.write_bytes(b"prior")
    expected = tmp_path / "expected.plist"
    expected.write_bytes(b"new")

    def runner(command, **kwargs):
        del kwargs
        if command == ["smoke"]:
            return _completed(command, 9, stderr="smoke red")
        return _completed(command)

    transaction = LaunchAgentTransaction(
        target_plist=target,
        label="com.test",
        uid=501,
        runner=runner,
        sleep_fn=lambda seconds: None,
    )
    with pytest.raises(TransactionError, match="prior plist was restored"):
        transaction.deploy(expected, ["smoke"])
    assert target.read_bytes() == b"prior"


class FakePromotion(PromotionRunner):
    def __init__(self, *, verdict: str, rollback_fails: bool = False):
        super().__init__(runner=lambda *args, **kwargs: _completed(args[0]), uid=501)
        self.verdict = verdict
        self.rollback_fails = rollback_fails
        self.installs: list[str] = []
        self.bootouts = 0

    def _install(self, manifest_path, values):
        del values
        phase = "on" if manifest_path.name == "on.json" else "off"
        self.installs.append(phase)
        if self.rollback_fails and self.installs == ["off", "on", "off"]:
            raise PromotionError("synthetic rollback failure")

    def _bootout(self):
        self.bootouts += 1

    def _run(self, command, *, environment=None):
        del environment
        if "capture-off" in command:
            output = Path(command[command.index("--output") + 1])
            output.write_text(json.dumps({"baseline_gate": {"passed": True}}))
            return _completed(command)
        if "run-on" in command:
            if self.verdict == "interrupt":
                raise KeyboardInterrupt
            output = Path(command[command.index("--output") + 1])
            green = self.verdict == "green"
            output.write_text(json.dumps({"gates": {"passed": green}}))
            return _completed(command, 0 if green else 1)
        raise AssertionError(command)


@pytest.mark.parametrize("verdict", ["red", "interrupt"])
def test_promotion_red_or_interrupt_automatically_reinstalls_off(tmp_path, verdict):
    runner = FakePromotion(verdict=verdict)
    exception = KeyboardInterrupt if verdict == "interrupt" else PromotionError
    with pytest.raises(exception):
        runner.execute(
            off_manifest_path=_manifest(tmp_path, False),
            on_manifest_path=_manifest(tmp_path, True),
            corpus_id="corpus",
            auth_token_file=tmp_path / "token",
            output_dir=tmp_path / "outputs",
        )
    assert runner.installs == ["off", "on", "off"]
    assert runner.bootouts == 0


def test_promotion_boots_out_service_if_off_rollback_itself_fails(tmp_path):
    runner = FakePromotion(verdict="red", rollback_fails=True)
    with pytest.raises(PromotionError):
        runner.execute(
            off_manifest_path=_manifest(tmp_path, False),
            on_manifest_path=_manifest(tmp_path, True),
            corpus_id="corpus",
            auth_token_file=tmp_path / "token",
            output_dir=tmp_path / "outputs",
        )
    assert runner.installs == ["off", "on", "off"]
    assert runner.bootouts == 1


def test_promotion_green_is_only_path_that_leaves_on(tmp_path):
    runner = FakePromotion(verdict="green")
    payload = runner.execute(
        off_manifest_path=_manifest(tmp_path, False),
        on_manifest_path=_manifest(tmp_path, True),
        corpus_id="corpus",
        auth_token_file=tmp_path / "token",
        output_dir=tmp_path / "outputs",
    )
    assert payload["gates"]["passed"] is True
    assert runner.installs == ["off", "on"]
    assert runner.bootouts == 0


def test_installer_requires_manifest_and_transactional_deploy():
    installer = (SCRIPTS / "install_apple_mlx_runtime.sh").read_text()
    assert "--env-manifest PATH is mandatory" in installer
    assert "apple_mlx_env_manifest.py" in installer
    assert "--check-process-environment" in installer
    assert "apple_mlx_launch_agent_transaction.py" in installer
    assert "launchctl bootstrap" not in installer
