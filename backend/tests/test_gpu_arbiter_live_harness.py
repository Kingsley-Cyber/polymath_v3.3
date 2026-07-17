"""Pure adversarial tests for the sealed Q1-Q5 GPU-arbiter live harness."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace

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


def _latency(
    successful: int,
    *,
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


def _runtime_identity() -> dict:
    return {
        "embedder": {
            "model": "Qwen3-Embedding-0.6B",
            "device": "mps",
            "dimension": harness.EMBED_DIMENSION,
            "batch_size": 256,
        },
        "reranker": {
            "model": "jinaai/jina-reranker-v3",
            "backend": "torch_fp16",
            "device": "mps",
            "cross_encoder": True,
        },
    }


def _health(*, enabled: bool) -> dict:
    identity = _runtime_identity()
    return {
        "embedder": {
            **identity["embedder"],
            "inference_ready": True,
            "gpu_arbiter": {"enabled": enabled},
        },
        "reranker": {
            **identity["reranker"],
            "warmup_complete": True,
            "gpu_arbiter": {"enabled": enabled},
        },
        "arbiter_absent": not enabled,
        "arbiter": None if not enabled else {"status": "ok"},
    }


def _binding() -> dict:
    return {
        "corpus_id": harness.FROZEN_CORPUS_ID,
        "corpus_name": harness.FROZEN_CORPUS_NAME,
        "selection_sha256": harness.FROZEN_SELECTION_SHA256,
        "selection_count": 15,
        "actual_document_count": 15,
        "matches_frozen_corpus": True,
    }


def _spot(verdict: str = "answered") -> dict:
    return {
        "technical_success": True,
        "doc_hit": True,
        "citation_membership_rate": 1.0,
        "all_citations_in_corpus": True,
        "effective_tier": harness.FROZEN_TIER,
        "verdict": verdict,
        "corpus_binding": _binding(),
    }


def _off_artifact() -> dict:
    payload = {
        "schema_version": harness.SCHEMA_VERSION,
        "phase": "off",
        "corpus_id": "corpus-test",
        "fixture": harness.build_fixture(),
        "health": _health(enabled=False),
        "runtime_identity": _runtime_identity(),
        "identity": {
            "embed_vectors": _vectors(),
            "rerank_scores": _scores(),
        },
        "frozen_spot": _spot(),
        "baseline_gate": {"passed": True},
    }
    return harness._seal(payload)


def _scheduler_snapshot(embed: int, rerank: int) -> dict:
    return {
        "status": "ok",
        "scheduler": {
            "grants": {"embed": embed, "rerank": rerank},
            "releases": {"embed": embed, "rerank": rerank},
            "wait_sample_count": {"embed": embed, "rerank": rerank},
            "hold_sample_count": {"embed": embed, "rerank": rerank},
            "hold_p95_ms": {"embed": 100.0, "rerank": 450.0},
        },
    }


def _telemetry() -> dict:
    reranks = harness.SOLO_RERANK_CALLS + harness.Q2_MIN_MIXED_RERANK_CALLS
    return {
        "before": _scheduler_snapshot(1000, 1000),
        "after": _scheduler_snapshot(
            1000 + harness.Q2_EMBED_CALLS,
            1000 + reranks,
        ),
        "delta": {
            counter: {
                "embed": harness.Q2_EMBED_CALLS,
                "rerank": reranks,
            }
            for counter in (
                "grants",
                "releases",
                "wait_sample_count",
                "hold_sample_count",
            )
        },
        "pre_q4_fail_open_alert_count": 0,
    }


def _fail_open() -> dict:
    return {
        "pid": 999,
        "process_identity": {
            "pid": 999,
            "start_identity": "Thu Jul 17 10:00:00 2026",
            "command": _arbiter_command(),
        },
        "old_process_gone_before_probes": True,
        "arbiter_endpoint_down_before_probes": True,
        "probe_errors": [],
        "alerts_seen_in_new_log_bytes": {"embed": True, "rerank": True},
        "recovered": True,
        "replacement_pid": 1000,
        "replacement_identity": {
            "pid": 1000,
            "start_identity": "Thu Jul 17 10:05:00 2026",
            "command": _arbiter_command(),
        },
    }


def _soak() -> dict:
    return {
        "requested_soak_seconds": harness.PRODUCTION_SOAK_SECONDS,
        "kill_at_seconds": harness.PRODUCTION_KILL_AT_SECONDS,
        "failure_started_elapsed_seconds": harness.PRODUCTION_KILL_AT_SECONDS,
        "failure_completed_elapsed_seconds": harness.PRODUCTION_KILL_AT_SECONDS + 2,
        "workers_active_at_kill": [
            "q4-rerank",
            "q4-embed-0",
            "q4-embed-1",
            "q4-embed-2",
        ],
        "embed": _latency(100),
        "rerank": _latency(20),
        "zero_deadlock": True,
        "fail_open": _fail_open(),
        "embed_successes_after_recovery": 10,
        "rerank_successes_after_recovery": 3,
    }


def _mixed() -> dict:
    return {
        "embed": _latency(
            harness.Q2_EMBED_CALLS,
            p95=1.5,
            requested=harness.Q2_EMBED_CALLS,
        ),
        "rerank": _latency(harness.Q2_MIN_MIXED_RERANK_CALLS, p95=0.9),
        "rerank_thread_stopped": True,
        "launch_barrier_passed": True,
        "overlapped_rerank_calls": harness.Q2_MIN_OVERLAPPED_RERANK_CALLS,
    }


def _green_gate_inputs() -> dict:
    telemetry = _telemetry()
    return {
        "off": _off_artifact(),
        "on_vectors": _vectors(),
        "on_scores": _scores(),
        "on_health": _health(enabled=True),
        "on_solo": _latency(
            harness.SOLO_RERANK_CALLS,
            p95=0.5,
            requested=harness.SOLO_RERANK_CALLS,
        ),
        "mixed": _mixed(),
        "arbiter_health": telemetry["after"],
        "telemetry": telemetry,
        "soak": _soak(),
        "frozen_spot": _spot(),
        "canonical_suite": {"exit_code": 0, "passed": True},
    }


def _arbiter_command() -> str:
    return (
        "/runtime/.venv/bin/python -m uvicorn gpu_arbiter.main:app "
        "--host 127.0.0.1 --port 8085 --log-level info"
    )


def test_fixture_and_artifact_seals_detect_tampering():
    fixture = harness.build_fixture()
    harness.verify_fixture(fixture)
    fixture["embed_texts"][0] = "tampered"
    with pytest.raises(harness.HarnessError, match="fixture hash mismatch"):
        harness.verify_fixture(fixture)
    artifact = _off_artifact()
    artifact["corpus_id"] = "wrong"
    with pytest.raises(harness.HarnessError, match="artifact seal mismatch"):
        harness._verify_seal(artifact)


def test_percentile_is_nearest_rank_and_rejects_empty_input():
    assert harness.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert harness.percentile(list(range(1, 21)), 0.95) == 19.0
    with pytest.raises(harness.HarnessError):
        harness.percentile([], 0.95)


def test_latency_summary_uses_one_versioned_nearest_rank_function():
    summary = harness._latency_summary(
        [1.0, 2.0, 100.0, 101.0],
        [],
        requested=4,
    )
    assert summary["latency_seconds"]["p50"] == 2.0
    assert summary["latency_seconds"]["p95"] == 101.0
    assert summary["percentile_method"] == harness.PERCENTILE_METHOD_VERSION
    assert summary["percentile_sample_count"] == 4


@pytest.mark.parametrize("answer", ["", " ", "\n\t"])
def test_empty_or_whitespace_answer_cannot_classify_as_answered(answer):
    assert harness.classify_answer(answer, []) == "empty_answer"


def test_all_q_gates_are_green_only_for_complete_evidence():
    gates = harness.evaluate_on_gates(**_green_gate_inputs())
    assert gates["passed"] is True
    assert gates["q3"]["mixed_to_solo_p95_ratio"] == pytest.approx(1.8)


@pytest.mark.parametrize(
    ("mutate", "red_gate"),
    [
        (lambda value: value["on_vectors"][0].__setitem__(0, 1e-9), "q1"),
        (
            lambda value: value["on_health"]["reranker"].__setitem__("backend", "mlx"),
            "q1",
        ),
        (
            lambda value: value["mixed"].__setitem__("overlapped_rerank_calls", 4),
            "q2",
        ),
        (
            lambda value: value["mixed"]["rerank"].__setitem__("successful", 19),
            "q2",
        ),
        (
            lambda value: value["telemetry"].__setitem__(
                "pre_q4_fail_open_alert_count", 1
            ),
            "q2",
        ),
        (
            lambda value: value["telemetry"]["delta"]["releases"].__setitem__(
                "rerank", 0
            ),
            "q3",
        ),
        (
            lambda value: value["arbiter_health"]["scheduler"][
                "hold_sample_count"
            ].__setitem__("rerank", 0),
            "q3",
        ),
        (
            lambda value: value["soak"].__setitem__(
                "failure_started_elapsed_seconds",
                harness.PRODUCTION_SOAK_SECONDS + 1,
            ),
            "q4",
        ),
        (
            lambda value: value["soak"]["fail_open"][
                "alerts_seen_in_new_log_bytes"
            ].__setitem__("rerank", False),
            "q4",
        ),
        (
            lambda value: value["soak"]["fail_open"].__setitem__(
                "arbiter_endpoint_down_before_probes", False
            ),
            "q4",
        ),
        (
            lambda value: value["soak"]["fail_open"].__setitem__(
                "replacement_pid", value["soak"]["fail_open"]["pid"]
            ),
            "q4",
        ),
        (
            lambda value: value["frozen_spot"].__setitem__(
                "verdict", "model_voiced_refusal"
            ),
            "q5",
        ),
        (
            lambda value: value["frozen_spot"].__setitem__("technical_success", False),
            "q5",
        ),
        (
            lambda value: value["off"]["frozen_spot"].__setitem__(
                "verdict", "gate_blocked"
            ),
            "q5",
        ),
        (
            lambda value: value["frozen_spot"]["corpus_binding"].__setitem__(
                "matches_frozen_corpus", False
            ),
            "q5",
        ),
        (
            lambda value: value["frozen_spot"]["corpus_binding"].__setitem__(
                "selection_sha256", "wrong"
            ),
            "q5",
        ),
    ],
)
def test_adversarial_false_passes_are_red(mutate, red_gate):
    values = _green_gate_inputs()
    mutate(values)
    gates = harness.evaluate_on_gates(**values)
    assert gates["passed"] is False
    assert gates[red_gate]["passed"] is False


def test_stale_preexisting_scheduler_counters_do_not_pass_current_run_gate():
    telemetry = _telemetry()
    telemetry["delta"] = harness.scheduler_delta(
        _scheduler_snapshot(5000, 5000),
        _scheduler_snapshot(5000, 5000),
    )
    assert harness.evaluate_q2(_mixed(), telemetry)["passed"] is False
    assert (
        harness.evaluate_q3(
            _latency(harness.SOLO_RERANK_CALLS, p95=0.5),
            _mixed(),
            telemetry["after"],
            telemetry,
        )["passed"]
        is False
    )


class FakeConfig:
    corpus_id = "corpus-test"
    arbiter_url = "http://arbiter"
    http_timeout_seconds = 0.05
    recovery_timeout_seconds = 0.05
    alert_timeout_seconds = 0.05

    def __init__(self, tmp_path: Path | None = None):
        root = tmp_path or Path("/tmp")
        self.pid_file = root / "gpu-arbiter.pid"
        self.error_log = root / "apple_ml.err.log"


class FakeClient:
    def __init__(self, tmp_path: Path | None = None, *, delayed: bool = False):
        self.config = FakeConfig(tmp_path)
        self.delayed = delayed
        self.scheduler_calls = 0

    def health_snapshot(self, *, expected_enabled: bool):
        return _health(enabled=expected_enabled)

    def embed(self, texts):
        if self.delayed:
            time.sleep(0.003)
        return [[0.0 for _ in range(harness.EMBED_DIMENSION)] for _ in texts]

    def rerank(self, query, documents):
        del query
        if self.delayed:
            time.sleep(0.001)
        return [0.25 for _ in documents]

    def frozen_spot(self):
        return _spot()

    def json(self, method, url, **kwargs):
        del method, url, kwargs
        self.scheduler_calls += 1
        if self.scheduler_calls == 1:
            return _scheduler_snapshot(1000, 1000)
        return _telemetry()["after"]


def test_capture_off_records_runtime_identity_and_answered_corpus(monkeypatch):
    monkeypatch.setattr(
        harness,
        "measure_solo_rerank",
        lambda *args, **kwargs: _latency(
            harness.SOLO_RERANK_CALLS,
            requested=harness.SOLO_RERANK_CALLS,
        ),
    )
    artifact = harness.capture_off(FakeClient())
    harness._verify_seal(artifact)
    assert artifact["runtime_identity"] == _runtime_identity()
    assert artifact["baseline_gate"]["passed"] is True


def test_run_on_integrates_run_scoped_telemetry_and_in_soak_failure(
    monkeypatch, tmp_path
):
    client = FakeClient(tmp_path)
    client.config.error_log.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        harness,
        "measure_solo_rerank",
        lambda *args, **kwargs: _latency(
            harness.SOLO_RERANK_CALLS,
            p95=0.5,
            requested=harness.SOLO_RERANK_CALLS,
        ),
    )
    monkeypatch.setattr(harness, "run_q2_mixed", lambda *args, **kwargs: _mixed())

    def fake_soak(*args, **kwargs):
        assert kwargs["soak_seconds"] == harness.PRODUCTION_SOAK_SECONDS
        assert kwargs["kill_at_seconds"] == harness.PRODUCTION_KILL_AT_SECONDS
        assert callable(kwargs["failure_probe"])
        return _soak()

    artifact = harness.run_on(
        client,
        _off_artifact(),
        run_soak=fake_soak,
        run_failure_probe=lambda *args, **kwargs: _fail_open(),
        run_suite=lambda **kwargs: {"exit_code": 0, "passed": True},
    )
    harness._verify_seal(artifact)
    assert artifact["gates"]["passed"] is True
    assert (
        artifact["q2_q3_scheduler_telemetry"]["delta"]["grants"]["embed"]
        == harness.Q2_EMBED_CALLS
    )
    assert artifact["fail_open_q4"] == artifact["soak_q4"]["fail_open"]


def test_q2_has_fixed_sample_barrier_timestamps_and_real_overlap():
    result = harness.run_q2_mixed(
        FakeClient(delayed=True),
        harness.build_fixture(),
        embed_calls=20,
        embed_concurrency=3,
    )
    assert result["embed"]["successful"] == 20
    assert result["rerank"]["successful"] >= harness.Q2_MIN_MIXED_RERANK_CALLS
    assert result["launch_barrier_passed"] is True
    assert result["overlapped_rerank_calls"] >= harness.Q2_MIN_OVERLAPPED_RERANK_CALLS
    assert all(row["start"] < row["end"] for row in result["rerank_intervals"])
    assert all(row["start"] < row["end"] for row in result["embed_intervals"])


def test_q2_rejects_smaller_than_preregistered_rerank_sample():
    with pytest.raises(harness.HarnessError, match="may not be below"):
        harness.run_q2_mixed(
            FakeClient(),
            harness.build_fixture(),
            embed_calls=1,
            min_rerank_calls=19,
        )


def test_q4_executes_probe_inside_active_soak_and_continues_after_recovery():
    called = {}

    def probe(client, fixture):
        del client, fixture
        called["at"] = time.monotonic()
        time.sleep(0.003)
        return _fail_open()

    result = harness.run_q4_soak(
        FakeClient(delayed=True),
        harness.build_fixture(),
        soak_seconds=0.06,
        kill_at_seconds=0.02,
        failure_probe=probe,
    )
    assert called
    assert len(result["workers_active_at_kill"]) == 4
    assert 0 < result["failure_started_elapsed_seconds"] < 0.06
    assert result["embed_successes_after_recovery"] > 0
    assert result["rerank_successes_after_recovery"] > 0
    assert result["zero_deadlock"] is True


def test_live_client_rejects_reordered_or_duplicate_embedding_indices():
    client = object.__new__(harness.LiveClient)
    client.config = SimpleNamespace(embedder_url="http://embed", http_timeout_seconds=1)
    client.json = lambda *args, **kwargs: {
        "data": [
            {"index": 1, "embedding": [0.0]},
            {"index": 0, "embedding": [0.0]},
        ]
    }
    with pytest.raises(harness.HarnessError, match="indices drifted"):
        client.embed(["a", "b"])


def test_corpus_binding_requires_exact_id_name_selection_sha_and_15_documents():
    selection = json.loads(harness.DEFAULT_SELECTION.read_text())
    names = [row["filename"] for row in selection["selected"]]
    client = object.__new__(harness.LiveClient)
    client.config = SimpleNamespace(
        corpus_id=harness.FROZEN_CORPUS_ID,
        selection_path=harness.DEFAULT_SELECTION,
        backend_url="http://backend",
        auth_token="secret",
        http_timeout_seconds=1,
    )

    def json_response(method, url, **kwargs):
        del method, kwargs
        if url.endswith("/documents?limit=100&offset=0"):
            return [
                {"doc_id": f"doc-{index}", "filename": name}
                for index, name in enumerate(names)
            ]
        return {
            "id": harness.FROZEN_CORPUS_ID,
            "name": harness.FROZEN_CORPUS_NAME,
        }

    client.json = json_response
    binding = client.corpus_binding()
    assert binding["matches_frozen_corpus"] is True
    names.pop()
    assert client.corpus_binding()["matches_frozen_corpus"] is False


def test_whitespace_frozen_spot_is_technical_red_even_with_done_and_sources():
    prereg = json.loads(harness.DEFAULT_PREREG.read_text())
    case = next(
        row for row in prereg["queries"] if row["id"] == harness.FROZEN_QUERY_ID
    )
    expected_name = case["expected_any"][0]
    client = object.__new__(harness.LiveClient)
    client.config = SimpleNamespace(
        corpus_id=harness.FROZEN_CORPUS_ID,
        prereg_path=harness.DEFAULT_PREREG,
    )
    client.corpus_binding = lambda: _binding()
    client.list_documents = lambda: {"doc-1": expected_name}
    client._run_chat_sse = lambda payload: {
        "answer": " \n\t",
        "sources": [
            {
                "doc_id": "doc-1",
                "corpus_id": harness.FROZEN_CORPUS_ID,
            }
        ],
        "traces": [
            {
                "title": "Local RAG retrieval",
                "metadata": {"effective_tier": harness.FROZEN_TIER},
            }
        ],
        "errors": [],
        "done_received": True,
    }
    result = client.frozen_spot()
    assert result["technical_success"] is False
    assert result["verdict"] == "empty_answer"


def test_exact_process_identity_rejects_substring_and_wrong_argv():
    assert harness._is_exact_arbiter_identity(
        {
            "pid": 9,
            "start_identity": "now",
            "command": _arbiter_command(),
        },
        9,
    )
    assert not harness._is_exact_arbiter_identity(
        {
            "pid": 9,
            "start_identity": "now",
            "command": "python malicious-gpu_arbiter.main --port 8085",
        },
        9,
    )


def test_fail_open_probe_proves_down_state_both_alerts_and_new_identity(tmp_path):
    client = FakeClient(tmp_path)
    client.config.pid_file.write_text("999\n", encoding="utf-8")
    client.config.error_log.write_text("old unrelated alert\n", encoding="utf-8")
    state = {"killed": False}

    def identity(pid):
        if pid == 999 and not state["killed"]:
            return {
                "pid": 999,
                "start_identity": "old-start",
                "command": _arbiter_command(),
            }
        if pid == 1000:
            return {
                "pid": 1000,
                "start_identity": "new-start",
                "command": _arbiter_command(),
            }
        return None

    def kill(pid, sig):
        assert (pid, sig) == (999, signal.SIGKILL)
        state["killed"] = True
        client.config.pid_file.write_text("1000\n", encoding="utf-8")
        with client.config.error_log.open("a", encoding="utf-8") as handle:
            handle.write(harness.FAIL_OPEN_ALERTS["embed"] + "\n")
            handle.write(harness.FAIL_OPEN_ALERTS["rerank"] + "\n")

    result = harness.run_fail_open_probe(
        client,
        harness.build_fixture(),
        kill_fn=kill,
        process_identity=identity,
        arbiter_down=lambda: state["killed"],
        sleep_fn=lambda seconds: None,
    )
    assert result["old_process_gone_before_probes"] is True
    assert result["arbiter_endpoint_down_before_probes"] is True
    assert all(result["alerts_seen_in_new_log_bytes"].values())
    assert result["replacement_pid"] == 1000
    assert result["replacement_identity"]["start_identity"] == "new-start"


def test_fail_open_probe_does_not_accept_preexisting_alert_lines(tmp_path):
    client = FakeClient(tmp_path)
    client.config.pid_file.write_text("999\n", encoding="utf-8")
    client.config.error_log.write_text(
        "\n".join(harness.FAIL_OPEN_ALERTS.values()) + "\n",
        encoding="utf-8",
    )
    state = {"killed": False}

    def identity(pid):
        if pid == 999 and not state["killed"]:
            return {
                "pid": 999,
                "start_identity": "old",
                "command": _arbiter_command(),
            }
        if pid == 1000:
            return {
                "pid": 1000,
                "start_identity": "new",
                "command": _arbiter_command(),
            }
        return None

    def kill(pid, sig):
        del pid, sig
        state["killed"] = True
        client.config.pid_file.write_text("1000\n", encoding="utf-8")

    result = harness.run_fail_open_probe(
        client,
        harness.build_fixture(),
        kill_fn=kill,
        process_identity=identity,
        arbiter_down=lambda: state["killed"],
        sleep_fn=lambda seconds: None,
    )
    assert result["alerts_seen_in_new_log_bytes"] == {
        "embed": False,
        "rerank": False,
    }


def test_fail_open_probe_refuses_wrong_pid_without_killing(tmp_path):
    client = FakeClient(tmp_path)
    client.config.pid_file.write_text("999\n", encoding="utf-8")
    killed = []
    with pytest.raises(harness.HarnessError, match="refusing to kill"):
        harness.run_fail_open_probe(
            client,
            harness.build_fixture(),
            kill_fn=lambda *args: killed.append(args),
            process_identity=lambda pid: {
                "pid": pid,
                "start_identity": "old",
                "command": "python unrelated.py",
            },
        )
    assert killed == []


def test_auth_token_file_requires_regular_owned_private_nonsymlink(
    tmp_path, monkeypatch
):
    token = tmp_path / "token"
    token.write_text("secret\n", encoding="utf-8")
    token.chmod(0o600)
    assert harness._load_auth_token(token) == "secret"
    token.chmod(0o644)
    with pytest.raises(harness.HarnessError, match="0600 or stricter"):
        harness._load_auth_token(token)
    token.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(token)
    with pytest.raises(harness.HarnessError, match="securely open"):
        harness._load_auth_token(link)
    current_uid = os.getuid()
    monkeypatch.setattr(harness.os, "getuid", lambda: current_uid + 1)
    with pytest.raises(harness.HarnessError, match="owned"):
        harness._load_auth_token(token)


def test_auth_token_validation_and_read_use_same_nofollow_descriptor(
    tmp_path, monkeypatch
):
    token = tmp_path / "token"
    token.write_text("safe-token\n", encoding="utf-8")
    token.chmod(0o600)
    real_open = os.open
    observed = {}

    def swapping_open(path, flags):
        observed["flags"] = flags
        descriptor = real_open(path, flags)
        token.unlink()
        token.write_text("replacement-token\n", encoding="utf-8")
        token.chmod(0o600)
        return descriptor

    monkeypatch.setattr(harness.os, "open", swapping_open)
    assert harness._load_auth_token(token) == "safe-token"
    assert observed["flags"] & os.O_NOFOLLOW


def test_harness_config_repr_never_contains_token(tmp_path):
    config = harness.HarnessConfig(
        embedder_url="e",
        reranker_url="r",
        arbiter_url="a",
        backend_url="b",
        corpus_id="c",
        auth_token="never-print-me",
        prereg_path=tmp_path / "p",
        selection_path=tmp_path / "s",
        pid_file=tmp_path / "pid",
        error_log=tmp_path / "log",
    )
    assert "never-print-me" not in repr(config)


def test_cli_has_no_production_duration_or_kill_point_override():
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
                "--kill-at-seconds",
                "1",
            ]
        )


def test_cli_returns_nonzero_for_red_artifact(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("secret-token\n", encoding="utf-8")
    token.chmod(0o600)
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
