"""Pure tests for the Q1-Q5 Metal GPU arbiter live harness.

No test contacts a sidecar, backend, provider, corpus, LaunchAgent, or process.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import signal
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = REPO_ROOT / "scripts/run_gpu_arbiter_live_gates.py"
SPEC = importlib.util.spec_from_file_location("gpu_arbiter_live_gates", HARNESS_PATH)
assert SPEC and SPEC.loader
harness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = harness
SPEC.loader.exec_module(harness)


def _vectors(value: float = 0.0) -> list[list[float]]:
    return [
        [value for _ in range(harness.EMBED_DIMENSION)]
        for _ in range(harness.EMBED_SAMPLE_COUNT)
    ]


def _scores(value: float = 0.25) -> list[float]:
    return [value for _ in range(harness.RERANK_SAMPLE_COUNT)]


def _latency_summary(
    *,
    successful: int,
    failed: int = 0,
    p95: float = 0.2,
    requested: int | None = None,
) -> dict:
    return {
        "requested": requested,
        "successful": successful,
        "failed": failed,
        "latency_seconds": {
            "min": p95,
            "p50": p95,
            "p95": p95,
            "max": p95,
        },
        "errors": [] if not failed else ["synthetic failure"],
    }


def _spot(verdict: str = "answered") -> dict:
    return {
        "technical_success": True,
        "doc_hit": True,
        "citation_membership_rate": 1.0,
        "all_citations_in_corpus": True,
        "effective_tier": harness.FROZEN_TIER,
        "verdict": verdict,
    }


def _off_artifact() -> dict:
    payload = {
        "schema_version": harness.SCHEMA_VERSION,
        "phase": "off",
        "corpus_id": "corpus-test",
        "fixture": harness.build_fixture(),
        "identity": {
            "embed_vectors": _vectors(),
            "rerank_scores": _scores(),
        },
        "frozen_spot": _spot(),
        "baseline_gate": {"passed": True},
    }
    return harness._seal(payload)


def _green_gate_inputs() -> dict:
    return {
        "off": _off_artifact(),
        "on_vectors": _vectors(),
        "on_scores": _scores(),
        "on_solo": _latency_summary(
            successful=harness.SOLO_RERANK_CALLS,
            p95=0.5,
            requested=harness.SOLO_RERANK_CALLS,
        ),
        "mixed": {
            "embed": _latency_summary(
                successful=harness.Q2_EMBED_CALLS,
                p95=1.5,
                requested=harness.Q2_EMBED_CALLS,
            ),
            "rerank": _latency_summary(successful=5, p95=0.9),
            "rerank_thread_stopped": True,
        },
        "arbiter_health": {"scheduler": {"hold_p95_ms": {"rerank": 450.0}}},
        "soak": {
            "requested_soak_seconds": harness.PRODUCTION_SOAK_SECONDS,
            "embed": _latency_summary(successful=100),
            "rerank": _latency_summary(successful=10),
            "zero_deadlock": True,
        },
        "fail_open": {
            "probe_errors": [],
            "alert_seen_in_new_log_bytes": True,
            "recovered": True,
        },
        "frozen_spot": _spot(),
        "canonical_suite": {"exit_code": 0, "passed": True},
    }


def test_fixture_and_artifact_seals_detect_tampering():
    fixture = harness.build_fixture()
    harness.verify_fixture(fixture)
    fixture["embed_texts"][0] = "tampered"
    with pytest.raises(harness.HarnessError, match="fixture hash mismatch"):
        harness.verify_fixture(fixture)

    artifact = _off_artifact()
    harness._verify_seal(artifact)
    artifact["corpus_id"] = "wrong"
    with pytest.raises(harness.HarnessError, match="artifact seal mismatch"):
        harness._verify_seal(artifact)


def test_percentile_is_nearest_rank_and_rejects_empty_input():
    assert harness.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert harness.percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
    with pytest.raises(harness.HarnessError):
        harness.percentile([], 0.95)


def test_q1_q5_green_evaluator_records_exact_metrics():
    gates = harness.evaluate_on_gates(**_green_gate_inputs())
    assert gates["passed"] is True
    assert all(gates[name]["passed"] for name in ("q1", "q2", "q3", "q4", "q5"))
    assert gates["q1"]["embed_max_abs_diff"] == 0.0
    assert gates["q1"]["rerank_max_abs_diff"] == 0.0
    assert gates["q3"]["mixed_to_solo_p95_ratio"] == pytest.approx(1.8)


@pytest.mark.parametrize(
    ("mutation", "red_gate"),
    [
        ("identity", "q1"),
        ("embed_timeout", "q2"),
        ("starvation", "q3"),
        ("deadlock", "q4"),
        ("spot_verdict", "q5"),
    ],
)
def test_every_q_gate_fails_closed_on_its_registered_failure(mutation, red_gate):
    values = _green_gate_inputs()
    if mutation == "identity":
        values["on_vectors"][0][0] = 1e-9
    elif mutation == "embed_timeout":
        values["mixed"]["embed"]["successful"] = harness.Q2_EMBED_CALLS - 1
        values["mixed"]["embed"]["failed"] = 1
        values["mixed"]["embed"]["latency_seconds"]["p95"] = 2.0
    elif mutation == "starvation":
        values["mixed"]["rerank"]["latency_seconds"]["p95"] = 1.01
        values["arbiter_health"]["scheduler"]["hold_p95_ms"]["rerank"] = 501.0
    elif mutation == "deadlock":
        values["soak"]["zero_deadlock"] = False
        values["fail_open"]["alert_seen_in_new_log_bytes"] = False
    elif mutation == "spot_verdict":
        values["frozen_spot"]["verdict"] = "model_voiced_refusal"
    gates = harness.evaluate_on_gates(**values)
    assert gates["passed"] is False
    assert gates[red_gate]["passed"] is False


def test_classifier_uses_registered_three_states():
    assert (
        harness.classify_answer(
            "The evidence says FACS codes facial movement.",
            [
                {
                    "title": "Assistant final answer",
                    "metadata": {"model_skipped": True},
                }
            ],
        )
        == "gate_blocked"
    )
    assert (
        harness.classify_answer(
            "I cannot answer from the selected corpus.",
            [],
        )
        == "model_voiced_refusal"
    )
    assert (
        harness.classify_answer(
            "FACS objectively codes facial muscle movements.",
            [],
        )
        == "answered"
    )


class FakeConfig:
    corpus_id = "corpus-test"
    arbiter_url = "http://arbiter"
    http_timeout_seconds = 0.01
    recovery_timeout_seconds = 0.01
    alert_timeout_seconds = 0.01

    def __init__(self, tmp_path: Path | None = None):
        root = tmp_path or Path("/tmp")
        self.pid_file = root / "gpu-arbiter.pid"
        self.error_log = root / "apple_ml.err.log"


class FakeClient:
    def __init__(self, tmp_path: Path | None = None):
        self.config = FakeConfig(tmp_path)

    def health_snapshot(self, *, expected_enabled: bool):
        return {
            "arbiter_absent": not expected_enabled,
            "arbiter": (
                None
                if not expected_enabled
                else {
                    "status": "ok",
                    "scheduler": {"hold_p95_ms": {"rerank": 450.0}},
                }
            ),
        }

    def embed(self, texts):
        return [[0.0 for _ in range(harness.EMBED_DIMENSION)] for _ in texts]

    def rerank(self, query, documents):
        del query
        return [0.25 for _ in documents]

    def frozen_spot(self):
        return _spot()

    def json(self, method, url):
        del method, url
        return {
            "status": "ok",
            "scheduler": {"hold_p95_ms": {"rerank": 450.0}},
        }


def test_capture_off_orchestration_is_sealed_and_green(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(
        harness,
        "measure_solo_rerank",
        lambda *args, **kwargs: _latency_summary(
            successful=harness.SOLO_RERANK_CALLS,
            requested=harness.SOLO_RERANK_CALLS,
        ),
    )
    artifact = harness.capture_off(client)
    harness._verify_seal(artifact)
    assert artifact["phase"] == "off"
    assert artifact["baseline_gate"]["passed"] is True
    assert artifact["fixture"] == harness.build_fixture()


def test_q2_mixed_orchestration_completes_requested_embeds():
    client = FakeClient()
    result = harness.run_q2_mixed(
        client,
        harness.build_fixture(),
        embed_calls=8,
        embed_concurrency=2,
    )
    assert result["embed"]["successful"] == 8
    assert result["embed"]["failed"] == 0
    assert result["rerank"]["successful"] > 0
    assert result["rerank"]["failed"] == 0
    assert result["rerank_thread_stopped"] is True


def test_q4_soak_accepts_test_only_function_override():
    client = FakeClient()
    result = harness.run_q4_soak(
        client,
        harness.build_fixture(),
        soak_seconds=0.02,
    )
    assert result["requested_soak_seconds"] == 0.02
    assert result["embed"]["successful"] > 0
    assert result["rerank"]["successful"] > 0
    assert result["embed"]["failed"] == 0
    assert result["rerank"]["failed"] == 0
    assert result["zero_deadlock"] is True


def test_run_on_orchestration_uses_injected_soak_and_failure_probe(monkeypatch):
    client = FakeClient()
    off = _off_artifact()
    monkeypatch.setattr(
        harness,
        "measure_solo_rerank",
        lambda *args, **kwargs: _latency_summary(
            successful=harness.SOLO_RERANK_CALLS,
            p95=0.5,
            requested=harness.SOLO_RERANK_CALLS,
        ),
    )
    monkeypatch.setattr(
        harness,
        "run_q2_mixed",
        lambda *args, **kwargs: {
            "embed": _latency_summary(
                successful=harness.Q2_EMBED_CALLS,
                p95=1.5,
                requested=harness.Q2_EMBED_CALLS,
            ),
            "rerank": _latency_summary(successful=5, p95=0.9),
            "rerank_thread_stopped": True,
        },
    )

    def fake_soak(*args, **kwargs):
        assert kwargs["soak_seconds"] == harness.PRODUCTION_SOAK_SECONDS
        return {
            "requested_soak_seconds": harness.PRODUCTION_SOAK_SECONDS,
            "embed": _latency_summary(successful=10),
            "rerank": _latency_summary(successful=2),
            "zero_deadlock": True,
        }

    def fake_failure(*args, **kwargs):
        return {
            "probe_errors": [],
            "alert_seen_in_new_log_bytes": True,
            "recovered": True,
        }

    def fake_suite(*args, **kwargs):
        return {
            "command": ["synthetic-canonical-suite"],
            "exit_code": 0,
            "passed": True,
            "output_tail": "15 passed",
        }

    artifact = harness.run_on(
        client,
        off,
        run_soak=fake_soak,
        run_failure_probe=fake_failure,
        run_suite=fake_suite,
    )
    harness._verify_seal(artifact)
    assert artifact["gates"]["passed"] is True


def test_run_on_stops_before_live_calls_when_canonical_suite_is_red():
    client = FakeClient()

    def explode_health(*args, **kwargs):
        raise AssertionError("live health must not run after suite RED")

    client.health_snapshot = explode_health
    artifact = harness.run_on(
        client,
        _off_artifact(),
        run_suite=lambda **kwargs: {
            "exit_code": 1,
            "passed": False,
            "output_tail": "synthetic test failure",
        },
    )
    assert artifact["gates"]["passed"] is False
    assert artifact["gates"]["q5"]["passed"] is False
    assert artifact["gates"]["stopped_after"] == "canonical_suite_q5_red"
    assert "health" not in artifact


def test_run_on_stops_before_soak_and_kill_when_q1_is_red(monkeypatch):
    class DriftClient(FakeClient):
        def embed(self, texts):
            vectors = super().embed(texts)
            vectors[0][0] = 1e-9
            return vectors

    monkeypatch.setattr(
        harness,
        "run_q2_mixed",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Q2 must not run after Q1 RED")
        ),
    )
    artifact = harness.run_on(
        DriftClient(),
        _off_artifact(),
        run_suite=lambda **kwargs: {"exit_code": 0, "passed": True},
    )
    assert artifact["gates"]["passed"] is False
    assert artifact["gates"]["q1"]["passed"] is False
    assert artifact["gates"]["stopped_after"] == "q1_red"
    assert "soak_q4" not in artifact
    assert "fail_open_q4" not in artifact


def test_fail_open_probe_refuses_unrelated_pid(tmp_path):
    client = FakeClient(tmp_path)
    client.config.pid_file.write_text("999\n", encoding="utf-8")
    client.config.error_log.write_text("", encoding="utf-8")
    killed = []
    with pytest.raises(harness.HarnessError, match="refusing to kill"):
        harness.run_fail_open_probe(
            client,
            harness.build_fixture(),
            kill_fn=lambda pid, sig: killed.append((pid, sig)),
            process_command=lambda pid: "/usr/bin/python unrelated.py",
        )
    assert killed == []


def test_fail_open_probe_checks_new_log_bytes_and_recovery(tmp_path):
    client = FakeClient(tmp_path)
    client.config.pid_file.write_text("999\n", encoding="utf-8")
    client.config.error_log.write_text("old log\n", encoding="utf-8")
    killed = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        with client.config.error_log.open("a", encoding="utf-8") as handle:
            handle.write(f"{harness.FAIL_OPEN_ALERT} synthetic\n")

    result = harness.run_fail_open_probe(
        client,
        harness.build_fixture(),
        kill_fn=fake_kill,
        process_command=lambda pid: (
            "python -m uvicorn gpu_arbiter.main:app --port 8085"
        ),
        sleep_fn=lambda seconds: None,
    )
    assert killed == [(999, signal.SIGKILL)]
    assert result["probe_errors"] == []
    assert result["alert_seen_in_new_log_bytes"] is True
    assert result["recovered"] is True


def test_canonical_suite_uses_frozen_test_list_and_propagates_failure():
    observed = {}

    def fake_runner(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return harness.subprocess.CompletedProcess(
            command, returncode=7, stdout="failed", stderr=""
        )

    result = harness.run_canonical_suite(
        repo_root=Path("/tmp/review-worktree"),
        runner=fake_runner,
    )
    assert result["exit_code"] == 7
    assert result["passed"] is False
    assert observed["command"][-len(harness.CANONICAL_SUITE_TESTS) :] == list(
        harness.CANONICAL_SUITE_TESTS
    )
    assert "/tmp/review-worktree:/repo" in observed["command"]
    assert observed["kwargs"]["timeout"] == harness.CANONICAL_SUITE_TIMEOUT_SECONDS


def test_production_cli_has_no_soak_duration_override():
    parser = harness._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "run-on",
                "--output",
                "/tmp/on.json",
                "--corpus-id",
                "corpus",
                "--auth-token-file",
                "/tmp/token",
                "--off-artifact",
                "/tmp/off.json",
                "--soak-seconds",
                "1",
            ]
        )


def test_cli_returns_nonzero_when_capture_gate_is_red(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("secret-token\n", encoding="utf-8")
    output = tmp_path / "off.json"
    red = harness._seal(
        {
            "schema_version": harness.SCHEMA_VERSION,
            "phase": "off",
            "baseline_gate": {"passed": False},
        }
    )
    monkeypatch.setattr(harness, "capture_off", lambda client: red)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(HARNESS_PATH),
            "capture-off",
            "--output",
            str(output),
            "--corpus-id",
            "corpus",
            "--auth-token-file",
            str(token),
        ],
    )
    assert harness.main() == 1
    assert json.loads(output.read_text())["baseline_gate"]["passed"] is False


def test_cli_returns_nonzero_when_any_on_gate_is_red(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("secret-token\n", encoding="utf-8")
    off_path = tmp_path / "off.json"
    off_path.write_text(json.dumps(_off_artifact()), encoding="utf-8")
    output = tmp_path / "on.json"
    red_gates = {
        "passed": False,
        **{name: {"passed": name != "q3"} for name in ("q1", "q2", "q3", "q4", "q5")},
    }
    red = harness._seal(
        {
            "schema_version": harness.SCHEMA_VERSION,
            "phase": "on",
            "gates": red_gates,
        }
    )
    monkeypatch.setattr(harness, "run_on", lambda client, off, **kwargs: red)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(HARNESS_PATH),
            "run-on",
            "--output",
            str(output),
            "--corpus-id",
            "corpus",
            "--auth-token-file",
            str(token),
            "--off-artifact",
            str(off_path),
        ],
    )
    assert harness.main() == 1
    assert json.loads(output.read_text())["gates"]["q3"]["passed"] is False
