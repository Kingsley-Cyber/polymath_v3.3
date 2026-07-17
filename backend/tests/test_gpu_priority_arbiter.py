"""Build-only tests for the owner-approved Metal GPU arbiter.

These tests use mocked computation and never contact, stop, or mutate the live
Apple sidecars. Live Q1-Q5 acceptance remains deploy-gated.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import importlib.util
import logging
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SIDECAR_ROOT = Path(
    os.environ.get("SIDECAR_PATH", str(REPO_ROOT / "scripts" / "apple_ml_services"))
)
if str(SIDECAR_ROOT) not in sys.path:
    sys.path.insert(0, str(SIDECAR_ROOT))

from gpu_arbiter.client import ALERT_NAME, GpuArbiterClient  # noqa: E402
from gpu_arbiter.main import PriorityLeaseScheduler  # noqa: E402


def _load_sidecar(relative_path: str, module_name: str):
    path = SIDECAR_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _ExplodingHttp:
    def post(self, *args, **kwargs):
        raise ConnectionError("arbiter intentionally absent")


class _CountingHttp:
    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("disabled client must not perform HTTP")


def test_default_off_is_true_direct_path(monkeypatch):
    monkeypatch.delenv("ARBITER_ENABLED", raising=False)
    http = _CountingHttp()
    client = GpuArbiterClient("embed", http_client=http)
    with client.lease() as result:
        assert result.lease_id is None
        assert result.fail_open is False
    assert client.enabled is False
    assert http.calls == 0


def test_unavailable_is_named_fail_open(caplog):
    logger = logging.getLogger("test.gpu-arbiter")
    client = GpuArbiterClient(
        "rerank",
        enabled=True,
        http_client=_ExplodingHttp(),
        acquire_timeout_seconds=0.01,
        logger=logger,
    )
    with caplog.at_level(logging.WARNING, logger=logger.name):
        with client.lease() as result:
            assert result.lease_id is None
            assert result.fail_open is True
    assert ALERT_NAME == "gpu_arbiter_unavailable"
    assert ALERT_NAME in caplog.text


def test_release_alert_does_not_suppress_required_acquire_alert(caplog):
    logger = logging.getLogger("test.gpu-arbiter.operations")
    client = GpuArbiterClient(
        "embed",
        enabled=True,
        http_client=_ExplodingHttp(),
        logger=logger,
    )
    with caplog.at_level(logging.WARNING, logger=logger.name):
        client.release("synthetic-lease")
        client.acquire()
    assert "operation=release" in caplog.text
    assert "operation=acquire" in caplog.text


@pytest.mark.asyncio
async def test_embed_jumps_waiting_rerank_queue():
    scheduler = PriorityLeaseScheduler(
        max_embed_burst=8,
        rerank_starvation_seconds=10.0,
    )
    active = await scheduler.acquire("rerank", "active", 1000, 500)
    order: list[str] = []

    async def worker(name: str, kind: str):
        lease = await scheduler.acquire(kind, name, 1000, 500)
        order.append(name)
        await scheduler.release(lease.lease_id, name)

    waiting_rerank = asyncio.create_task(worker("rerank-waiting", "rerank"))
    waiting_embed = asyncio.create_task(worker("embed-interactive", "embed"))
    await asyncio.sleep(0)
    await scheduler.release(active.lease_id, "active")
    await asyncio.gather(waiting_rerank, waiting_embed)
    assert order == ["embed-interactive", "rerank-waiting"]


@pytest.mark.asyncio
async def test_starvation_guard_bounds_embed_burst():
    scheduler = PriorityLeaseScheduler(
        max_embed_burst=2,
        rerank_starvation_seconds=10.0,
    )
    active = await scheduler.acquire("rerank", "active", 1000, 500)
    order: list[str] = []

    async def worker(name: str, kind: str):
        lease = await scheduler.acquire(kind, name, 1000, 500)
        order.append(name)
        await scheduler.release(lease.lease_id, name)

    tasks = [asyncio.create_task(worker("rerank-waiting", "rerank"))]
    tasks.extend(
        asyncio.create_task(worker(f"embed-{index}", "embed")) for index in range(4)
    )
    await asyncio.sleep(0)
    await scheduler.release(active.lease_id, "active")
    await asyncio.gather(*tasks)
    assert order[:3] == ["embed-0", "embed-1", "rerank-waiting"]


@pytest.mark.asyncio
async def test_stale_lease_recovery_is_bounded():
    scheduler = PriorityLeaseScheduler(stale_lease_seconds=1.0)
    lease = await scheduler.acquire("embed", "dead-client", 1000, 500)
    assert lease.lease_id
    scheduler.stale_lease_seconds = 0.001
    await asyncio.sleep(0.002)
    assert await scheduler.recover_stale_lease() is True
    recovered = await scheduler.acquire("rerank", "next-client", 1000, 500)
    await scheduler.release(recovered.lease_id, "next-client")
    assert (await scheduler.snapshot())["stale_recoveries"] == 1


@pytest.mark.asyncio
async def test_health_uses_nearest_rank_and_records_per_workload_sample_counts():
    scheduler = PriorityLeaseScheduler()
    scheduler._wait_ms["embed"] = [float(value) for value in range(1, 21)]
    scheduler._hold_ms["embed"] = [float(value) for value in range(1, 21)]
    scheduler._wait_ms["rerank"] = [1.0, 2.0]
    scheduler._hold_ms["rerank"] = [3.0, 4.0]
    scheduler._wait_sample_count = {"embed": 20, "rerank": 2}
    scheduler._hold_sample_count = {"embed": 20, "rerank": 2}
    scheduler._grants = {"embed": 20, "rerank": 2}
    scheduler._releases = {"embed": 20, "rerank": 2}
    snapshot = await scheduler.snapshot()
    assert snapshot["wait_p95_ms"]["embed"] == 19.0
    assert snapshot["hold_p95_ms"]["embed"] == 19.0
    assert snapshot["wait_sample_count"] == {"embed": 20, "rerank": 2}
    assert snapshot["hold_sample_count"] == {"embed": 20, "rerank": 2}
    assert snapshot["releases"] == {"embed": 20, "rerank": 2}
    assert snapshot["release_count"] == 22


@pytest.mark.asyncio
async def test_health_sample_counters_remain_cumulative_past_rolling_window():
    scheduler = PriorityLeaseScheduler()
    scheduler._wait_ms["embed"] = [1.0] * 512
    scheduler._hold_ms["embed"] = [2.0] * 512
    scheduler._wait_sample_count["embed"] = 900
    scheduler._hold_sample_count["embed"] = 899
    snapshot = await scheduler.snapshot()
    assert snapshot["wait_sample_count"]["embed"] == 900
    assert snapshot["hold_sample_count"]["embed"] == 899


@pytest.mark.asyncio
async def test_mocked_mixed_soak_completes_100_embeds_and_reranks():
    """Local scheduling soak; live latency gates remain intentionally deferred."""
    scheduler = PriorityLeaseScheduler(
        max_embed_burst=4,
        rerank_starvation_seconds=0.02,
    )
    embed_latencies: list[float] = []
    rerank_compute: list[float] = []
    solo_rerank_seconds = 0.001

    async def run_embed(index: int):
        started = time.monotonic()
        lease = await scheduler.acquire("embed", f"embed-{index}", 2000, 500)
        await asyncio.sleep(0.0002)
        await scheduler.release(lease.lease_id, f"embed-{index}")
        embed_latencies.append(time.monotonic() - started)

    async def run_rerank(index: int):
        lease = await scheduler.acquire("rerank", f"rerank-{index}", 2000, 500)
        started = time.monotonic()
        await asyncio.sleep(solo_rerank_seconds)
        rerank_compute.append(time.monotonic() - started)
        await scheduler.release(lease.lease_id, f"rerank-{index}")

    tasks = [asyncio.create_task(run_embed(index)) for index in range(100)]
    tasks.extend(asyncio.create_task(run_rerank(index)) for index in range(25))
    await asyncio.gather(*tasks)

    embed_p95 = sorted(embed_latencies)[94]
    rerank_p95 = sorted(rerank_compute)[23]
    print(
        "MOCKED_MIXED_SOAK "
        f"embeds={len(embed_latencies)} embed_p95_s={embed_p95:.6f} "
        f"reranks={len(rerank_compute)} rerank_compute_p95_s={rerank_p95:.6f}"
    )
    assert len(embed_latencies) == 100
    assert len(rerank_compute) == 25
    assert embed_p95 < 2.0
    # Event-loop timer granularity is included; keep a small deterministic
    # allowance while still proving scheduling does not alter compute work.
    assert rerank_p95 <= max(0.01, solo_rerank_seconds * 2)


def test_embed_wrapper_is_bit_identical_for_100_vectors(monkeypatch):
    module = _load_sidecar("embedder_mlx/main.py", "gpu_arbiter_embed_identity")
    vectors = np.arange(100 * module.EMBED_DIM, dtype=np.float32).reshape(
        100, module.EMBED_DIM
    )
    monkeypatch.setattr(module, "_encode_batch", lambda inputs: vectors.copy())

    module._gpu_arbiter.enabled = False
    direct = module._encode_batch_scheduled(["x"] * 100)

    entered = []

    @contextmanager
    def fake_lease():
        entered.append(True)
        yield None

    module._gpu_arbiter.enabled = True
    monkeypatch.setattr(module._gpu_arbiter, "lease", fake_lease)
    scheduled = module._encode_batch_scheduled(["x"] * 100)

    assert entered == [True]
    assert np.array_equal(direct, scheduled)
    assert float(np.max(np.abs(direct - scheduled))) == 0.0
    print(
        "MOCKED_EMBED_IDENTITY "
        f"vectors={direct.shape[0]} dimensions={direct.shape[1]} max_abs_diff=0.0"
    )


def test_torch_rerank_checkpoint_preserves_scores_and_block_calls(monkeypatch):
    module = _load_sidecar("reranker_mlx/main.py", "gpu_arbiter_rerank_identity")

    class FakeModel:
        def _compute_single_batch(self, query, docs, instruction=None):
            return tuple(docs)

        def rerank(self, query, documents):
            # Two existing internal blocks; the wrapper must not change their
            # membership, order, or returned raw scores.
            self._compute_single_batch(query, documents[:2], instruction=None)
            self._compute_single_batch(query, documents[2:], instruction=None)
            return [
                {"index": index, "relevance_score": value}
                for index, value in enumerate((0.1, 0.2, 0.3, 0.4))
            ]

    fake_model = FakeModel()
    monkeypatch.setattr(module, "_torch_model", fake_model)
    documents = ["a", "b", "c", "d"]
    module._gpu_arbiter.enabled = False
    direct = module._score_pairs_torch_scheduled("query", documents)

    leases = []

    @contextmanager
    def fake_lease():
        leases.append(True)
        yield None

    module._gpu_arbiter.enabled = True
    monkeypatch.setattr(module._gpu_arbiter, "lease", fake_lease)
    scheduled = module._score_pairs_torch_scheduled("query", documents)

    assert leases == [True, True]
    assert direct == scheduled
    max_abs_diff = max(abs(left - right) for left, right in zip(direct, scheduled))
    assert max_abs_diff == 0.0
    print(
        "MOCKED_RERANK_IDENTITY "
        f"scores={len(direct)} checkpoints={len(leases)} "
        f"max_abs_diff={max_abs_diff}"
    )


def test_launch_agent_renderer_defaults_dark_and_records_arbiter(tmp_path):
    output = tmp_path / "apple-ml.plist"
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "render_apple_mlx_launch_agent.py"),
        "--output",
        str(output),
        "--label",
        "com.polymath.apple-ml",
        "--runtime-root",
        "/tmp/runtime",
        "--services-dir",
        "/tmp/runtime/apple_ml_services",
        "--log-dir",
        "/tmp/runtime/logs",
        "--embed-model",
        "embed",
        "--reranker-model",
        "rerank",
        "--reranker-backend",
        "torch_fp16",
        "--torch-reranker-model",
        "torch-rerank",
        "--embed-batch-size",
        "32",
        "--start-embedder",
        "true",
        "--start-reranker",
        "true",
        "--start-docling",
        "false",
        "--reranker-score-scale",
        "probability",
        "--arbiter-enabled",
        "false",
        "--arbiter-host",
        "127.0.0.1",
        "--arbiter-port",
        "8085",
        "--arbiter-acquire-timeout-seconds",
        "30",
        "--arbiter-embed-hold-target-ms",
        "2000",
        "--arbiter-rerank-hold-target-ms",
        "500",
        "--arbiter-max-embed-burst",
        "1",
        "--arbiter-rerank-starvation-seconds",
        "0.5",
        "--arbiter-stale-lease-seconds",
        "75",
    ]
    subprocess.run(command, check=True)
    with output.open("rb") as handle:
        payload = plistlib.load(handle)
    environment = payload["EnvironmentVariables"]
    assert environment["ARBITER_ENABLED"] == "false"
    assert environment["ARBITER_HOST"] == "127.0.0.1"
    assert environment["ARBITER_MAX_EMBED_BURST"] == "1"
    assert environment["ARBITER_RERANK_HOLD_TARGET_MS"] == "500"
    assert payload["ProgramArguments"][-1].endswith("apple_ml_services/start.sh")


def test_installer_engraves_reinstall_kickstart_and_plist_drift_gate():
    installer = (REPO_ROOT / "scripts" / "install_apple_mlx_runtime.sh").read_text()
    checker = (REPO_ROOT / "scripts" / "check_apple_mlx_plist_drift.sh").read_text()
    assert 'ARBITER_ENABLED="${ARBITER_ENABLED:?}"' in installer
    assert "render_apple_mlx_launch_agent.py" in installer
    assert "apple_mlx_env_manifest.py" in installer
    assert "apple_mlx_launch_agent_transaction.py" in installer
    assert "plist drift detected" in checker


def test_supervisor_restarts_arbiter_without_interrupting_model_sidecars():
    start = (REPO_ROOT / "scripts" / "apple_ml_services" / "start.sh").read_text()
    assert '"${pid_file##*/}" == "gpu-arbiter.pid"' in start
    assert "restarting it without interrupting model sidecars" in start
    assert (
        'start_service "gpu-arbiter" "gpu_arbiter.main" '
        "ARBITER_HOST ARBITER_PORT false"
    ) in start


def test_rerank_evidence_support_law_remains_default_off():
    config = (REPO_ROOT / "backend" / "config.py").read_text()
    tuning = (
        REPO_ROOT / "backend" / "services" / "answerability_tuning.py"
    ).read_text()
    assert "RERANK_EVIDENCE_SUPPORT: bool = Field(" in config
    assert (
        "default=False"
        in config[
            config.index("RERANK_EVIDENCE_SUPPORT: bool = Field(") : config.index(
                "RERANK_EVIDENCE_SUPPORT: bool = Field("
            )
            + 250
        ]
    )
    assert "def rerank_evidence_support()" in tuning
