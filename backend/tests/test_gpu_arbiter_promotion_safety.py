"""Build-only tests for manifest, transactional plist, and RED rollback laws."""

from __future__ import annotations

import json
from pathlib import Path
import re
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
from apple_mlx_launchctl import (  # noqa: E402
    service_absence_proven,
    wait_for_service_absence,
)
from run_gpu_arbiter_promotion import (  # noqa: E402
    LAUNCH_AGENT,
    PromotionError,
    PromotionRunner,
)


def _environment(enabled: bool) -> dict[str, str]:
    values = {
        "POLYMATH_DOCKER_DATA_ROOT": "/Users/test/PolymathRuntime",
        "APPLE_MLX_EMBED_MODEL_ID": "mlx-community/Qwen3-Embedding-0.6B-mxfp8",
        "APPLE_MLX_RERANKER_MODEL_ID": "mlx-community/jina-reranker-v3-4bit-mxfp4",
        "APPLE_RERANKER_BACKEND": "torch_fp16",
        "APPLE_TORCH_RERANKER_MODEL_ID": "jinaai/jina-reranker-v3",
        "RERANKER_SCORE_SCALE": "probability",
        "EMBEDDER_MODEL_NAME": "Qwen3-Embedding-0.6B",
        "EMBED_BATCH_SIZE": "256",
        "EMBED_MAX_LENGTH": "512",
        "EMBEDDER_REQUEST_TIMEOUT_SECONDS": "60",
        "EMBEDDER_QUEUE_TIMEOUT_SECONDS": "30",
        "EMBEDDER_WARMUP_TIMEOUT_SECONDS": "30",
        "MLX_CACHE_LIMIT_GB": "1.0",
        "RERANKER_CAL_MU": "0.2",
        "RERANKER_CAL_T": "0.12",
        "RERANKER_CAL_VERSION": "cal.v1-provisional",
        "RERANKER_BATCH_SIZE": "16",
        "RERANKER_MAX_DOC_CHARS": "6000",
        "RERANKER_MAX_QUERY_CHARS": "2000",
        "RERANKER_REQUEST_TIMEOUT_SECONDS": "60",
        "RERANKER_QUEUE_TIMEOUT_SECONDS": "5",
        "RERANKER_WARM_ON_STARTUP": "true",
        "RERANKER_WARMUP_CANDIDATE_SHAPES": "16,24",
        "RERANKER_WARMUP_CANDIDATES": "16",
        "RERANKER_WARMUP_DOC_CHARS": "768",
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


def _not_found(command):
    label = command[-1].rsplit("/", 1)[-1]
    return _completed(
        command,
        113,
        stderr=(f'Could not find service "{label}" in domain for user gui: 501'),
    )


def test_launchctl_absence_requires_canonical_status_and_diagnostic():
    command = ["launchctl", "print", "gui/501/com.test"]
    service = command[-1]
    assert service_absence_proven(_not_found(command), service)
    assert service_absence_proven(
        _completed(
            command,
            113,
            stderr=(
                "Bad request.\n"
                'Could not find service "com.test" in domain for user gui: 501'
            ),
        ),
        service,
    )
    assert not service_absence_proven(
        _completed(
            command,
            1,
            stderr='Could not find service "com.test" in domain for user gui: 501',
        ),
        service,
    )
    assert not service_absence_proven(
        _completed(command, 113, stderr="Operation not permitted"),
        service,
    )
    assert not service_absence_proven(
        _completed(
            command,
            113,
            stderr='Could not find service "com.test" in domain for user gui: 999',
        ),
        service,
    )
    assert not service_absence_proven(
        _completed(
            command,
            113,
            stderr=(
                "Operation not permitted\n"
                'Could not find service "com.test" in domain for user gui: 501'
            ),
        ),
        service,
    )
    assert not service_absence_proven(
        _completed(
            command,
            113,
            stderr=(
                'Could not find service "com.test" in domain for user gui: 501 '
                "(fabricated)"
            ),
        ),
        service,
    )
    assert not service_absence_proven(
        _completed(
            ["launchctl", "print", "gui/999/com.test"],
            113,
            stderr='Could not find service "com.test" in domain for user gui: 501',
        ),
        service,
    )


def test_launchctl_absence_poll_accepts_only_later_exact_state():
    command = ["launchctl", "print", "gui/501/com.test"]
    responses = [
        _completed(command, 0, stdout="service is unloading"),
        _not_found(command),
    ]
    sleeps: list[float] = []
    result = wait_for_service_absence(
        lambda: responses.pop(0),
        command[-1],
        sleep_fn=sleeps.append,
    )
    assert service_absence_proven(result, command[-1])
    assert sleeps == [0.1]


def test_launchctl_absence_poll_does_not_retry_unknown_error():
    command = ["launchctl", "print", "gui/501/com.test"]
    calls = 0

    def unknown():
        nonlocal calls
        calls += 1
        return _completed(command, 1, stderr="Operation not permitted")

    result = wait_for_service_absence(
        unknown,
        command[-1],
        sleep_fn=lambda seconds: None,
    )
    assert service_absence_proven(result, command[-1]) is False
    assert calls == 1


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


def test_manifest_covers_every_sidecar_computation_environment_read():
    reads = set()
    pattern = re.compile(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"')
    for relative in (
        "apple_ml_services/embedder_mlx/main.py",
        "apple_ml_services/reranker_mlx/main.py",
    ):
        reads.update(pattern.findall((SCRIPTS / relative).read_text()))
    # These aliases are fallback expressions only. Their primary model-ID
    # variables are mandatory and nonblank, so the aliases cannot affect the
    # deployed computation.
    inert_aliases = {"EMBED_MODEL_ID", "RERANKER_MODEL_ID"}
    assert reads - inert_aliases <= set(REQUIRED_KEYS)
    assert {
        "EMBED_MAX_LENGTH",
        "EMBEDDER_MODEL_NAME",
        "RERANKER_BATCH_SIZE",
        "RERANKER_MAX_DOC_CHARS",
        "RERANKER_MAX_QUERY_CHARS",
        "RERANKER_CAL_MU",
        "RERANKER_CAL_T",
        "RERANKER_CAL_VERSION",
    } <= set(REQUIRED_KEYS)
    renderer = (SCRIPTS / "render_apple_mlx_launch_agent.py").read_text()
    missing_from_plist = [key for key in REQUIRED_KEYS if f'"{key}"' not in renderer]
    assert missing_from_plist == []


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
        if command[1:2] == ["print"]:
            return _not_found(command)
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
        if command[1:2] == ["print"]:
            return _not_found(command)
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


def test_transaction_fails_loudly_when_bootout_does_not_unload_service(tmp_path):
    target = tmp_path / "agent.plist"
    target.write_bytes(b"prior")
    expected = tmp_path / "expected.plist"
    expected.write_bytes(b"new")

    def runner(command, **kwargs):
        del kwargs
        if command[1:2] == ["bootout"]:
            return _completed(command, 1, stderr="bootout denied")
        if command[1:2] == ["print"]:
            return _completed(command, 0, stdout="service remains loaded")
        return _completed(command)

    transaction = LaunchAgentTransaction(
        target_plist=target,
        label="com.test",
        uid=501,
        runner=runner,
        sleep_fn=lambda seconds: None,
    )
    with pytest.raises(TransactionError, match="service state is UNKNOWN"):
        transaction.deploy(expected, ["smoke"])
    assert target.read_bytes() == b"prior"


def test_transaction_permission_failure_is_unknown_not_absent(tmp_path):
    target = tmp_path / "agent.plist"

    def permission_denied(command, **kwargs):
        del kwargs
        return _completed(command, 1, stderr="Operation not permitted")

    transaction = LaunchAgentTransaction(
        target_plist=target,
        label="com.test",
        uid=501,
        runner=permission_denied,
    )
    with pytest.raises(TransactionError, match="did not prove service absence"):
        transaction._bootout()


def test_transaction_wrong_domain_not_found_is_unknown_not_absent(tmp_path):
    target = tmp_path / "agent.plist"

    def wrong_domain(command, **kwargs):
        del kwargs
        if command[1:2] == ["print"]:
            return _completed(
                command,
                113,
                stderr=(
                    'Could not find service "com.test" ' "in domain for user gui: 999"
                ),
            )
        return _completed(command, 1, stderr="bootout denied")

    transaction = LaunchAgentTransaction(
        target_plist=target,
        label="com.test",
        uid=501,
        runner=wrong_domain,
    )
    with pytest.raises(TransactionError, match="did not prove service absence"):
        transaction._bootout()


class FakeResponse:
    def __init__(self, payload: dict):
        self.status = 200
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_off_postcondition_requires_both_disabled_health_and_closed_8085():
    health = {
        "http://127.0.0.1:8082/health": {"gpu_arbiter": {"enabled": False}},
        "http://127.0.0.1:8081/health": {"gpu_arbiter": {"enabled": False}},
    }

    def urlopen(url, timeout):
        del timeout
        return FakeResponse(health[url])

    def absent(address, timeout):
        del address, timeout
        raise ConnectionRefusedError

    runner = PromotionRunner(
        runner=lambda command, **kwargs: _completed(command),
        urlopen=urlopen,
        create_connection=absent,
        uid=501,
    )
    assert runner._verify_off_postcondition() == {
        "embed_arbiter_disabled": True,
        "rerank_arbiter_disabled": True,
        "arbiter_8085_absent": True,
    }
    health["http://127.0.0.1:8081/health"]["gpu_arbiter"]["enabled"] = True
    with pytest.raises(PromotionError, match="OFF postcondition failed"):
        runner._verify_off_postcondition()


def test_emergency_bootout_checks_command_and_loaded_state():
    def absent(address, timeout):
        del address, timeout
        raise ConnectionRefusedError

    def still_loaded(command, **kwargs):
        del kwargs
        if command[1] == "bootout":
            return _completed(command, 1, stderr="denied")
        return _completed(command, 0, stdout="still loaded")

    runner = PromotionRunner(
        runner=still_loaded,
        create_connection=absent,
        uid=501,
    )
    with pytest.raises(PromotionError, match="did not unload"):
        runner._bootout()

    def already_absent(command, **kwargs):
        del kwargs
        if command[1] == "print":
            return _not_found(command)
        return _completed(command, 1, stderr="service was already absent")

    PromotionRunner(
        runner=already_absent,
        create_connection=absent,
        uid=501,
    )._bootout()

    class OpenConnection:
        def close(self):
            pass

    runner = PromotionRunner(
        runner=already_absent,
        create_connection=lambda *args, **kwargs: OpenConnection(),
        uid=501,
    )
    with pytest.raises(PromotionError, match="still listening"):
        runner._bootout()


def test_emergency_permission_failure_is_unknown_even_when_8085_is_closed():
    def permission_denied(command, **kwargs):
        del kwargs
        return _completed(command, 1, stderr="Operation not permitted")

    def absent(address, timeout):
        del address, timeout
        raise ConnectionRefusedError

    runner = PromotionRunner(
        runner=permission_denied,
        create_connection=absent,
        uid=501,
    )
    with pytest.raises(PromotionError, match="did not prove service absence") as exc:
        runner._bootout()
    assert "absence was verified" not in str(exc.value)


def test_emergency_wrong_domain_is_unknown_even_when_8085_is_closed():
    def wrong_domain(command, **kwargs):
        del kwargs
        if command[1] == "print":
            return _completed(
                command,
                113,
                stderr=(
                    f'Could not find service "{LAUNCH_AGENT}" '
                    "in domain for user gui: 999"
                ),
            )
        return _completed(command, 1, stderr="bootout denied")

    def absent(address, timeout):
        del address, timeout
        raise ConnectionRefusedError

    runner = PromotionRunner(
        runner=wrong_domain,
        create_connection=absent,
        uid=501,
    )
    with pytest.raises(PromotionError, match="did not prove service absence") as exc:
        runner._bootout()
    assert "absence was verified" not in str(exc.value)


class FakePromotion(PromotionRunner):
    def __init__(
        self,
        *,
        verdict: str,
        rollback_fails: bool = False,
        off_proof_fails: bool = False,
    ):
        super().__init__(runner=lambda *args, **kwargs: _completed(args[0]), uid=501)
        self.verdict = verdict
        self.rollback_fails = rollback_fails
        self.off_proof_fails = off_proof_fails
        self.installs: list[str] = []
        self.bootouts = 0
        self.off_proofs = 0
        self.local_only_commands: list[bool] = []

    def _install(self, manifest_path, values):
        del values
        phase = "on" if manifest_path.name == "on.json" else "off"
        self.installs.append(phase)
        if self.rollback_fails and self.installs == ["off", "on", "off"]:
            raise PromotionError("synthetic rollback failure")

    def _bootout(self):
        self.bootouts += 1

    def _verify_off_postcondition(self):
        self.off_proofs += 1
        if self.off_proof_fails and self.off_proofs > 1:
            raise PromotionError("synthetic OFF proof failure")
        return {
            "embed_arbiter_disabled": True,
            "rerank_arbiter_disabled": True,
            "arbiter_8085_absent": True,
        }

    def _run(self, command, *, environment=None):
        del environment
        self.local_only_commands.append("--local-only" in command)
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
    with pytest.raises(PromotionError, match="postcondition VERIFIED"):
        runner.execute(
            off_manifest_path=_manifest(tmp_path, False),
            on_manifest_path=_manifest(tmp_path, True),
            corpus_id="corpus",
            auth_token_file=tmp_path / "token",
            output_dir=tmp_path / "outputs",
        )
    assert runner.installs == ["off", "on", "off"]
    assert runner.bootouts == 0
    assert runner.off_proofs == 2


def test_promotion_boots_out_service_if_off_rollback_itself_fails(tmp_path):
    runner = FakePromotion(verdict="red", rollback_fails=True)
    with pytest.raises(PromotionError, match="was NOT proven"):
        runner.execute(
            off_manifest_path=_manifest(tmp_path, False),
            on_manifest_path=_manifest(tmp_path, True),
            corpus_id="corpus",
            auth_token_file=tmp_path / "token",
            output_dir=tmp_path / "outputs",
        )
    assert runner.installs == ["off", "on", "off"]
    assert runner.bootouts == 1


def test_promotion_never_claims_restored_when_off_postcondition_is_red(tmp_path):
    runner = FakePromotion(verdict="red", off_proof_fails=True)
    with pytest.raises(PromotionError, match="OFF restoration was NOT proven") as exc:
        runner.execute(
            off_manifest_path=_manifest(tmp_path, False),
            on_manifest_path=_manifest(tmp_path, True),
            corpus_id="corpus",
            auth_token_file=tmp_path / "token",
            output_dir=tmp_path / "outputs",
        )
    assert "postcondition VERIFIED" not in str(exc.value)
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
    assert runner.off_proofs == 1


def test_local_only_promotion_marks_both_harness_phases(tmp_path):
    runner = FakePromotion(verdict="green")
    payload = runner.execute(
        off_manifest_path=_manifest(tmp_path, False),
        on_manifest_path=_manifest(tmp_path, True),
        corpus_id="corpus",
        auth_token_file=tmp_path / "token",
        output_dir=tmp_path / "outputs",
        local_only=True,
    )
    assert payload["gates"]["passed"] is True
    assert runner.local_only_commands == [True, True]
    assert runner.installs == ["off", "on"]


def test_installer_requires_manifest_and_transactional_deploy():
    installer = (SCRIPTS / "install_apple_mlx_runtime.sh").read_text()
    assert "--env-manifest PATH is mandatory" in installer
    assert "apple_mlx_env_manifest.py" in installer
    assert "--check-process-environment" in installer
    assert "apple_mlx_launch_agent_transaction.py" in installer
    assert "launchctl bootstrap" not in installer
    setup = (SCRIPTS / "setup_apple_mlx.sh").read_text()
    assert 'test "$RERANKER_SCORE_SCALE" = "probability"' in setup
    assert 'test "$RERANKER_SCORE_SCALE" = "cosine"' not in setup
