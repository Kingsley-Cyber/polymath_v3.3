"""Phase 4 auto-warm tests.

Pins the contract for the debounced post-ingest analytics cache warm:

  • Single ingest completion → exactly one emerge_domains call after
    the debounce window.
  • Bulk ingest (N rapid completions for the same corpus) → exactly
    one emerge_domains call. The debounce cancels-and-replaces.
  • Different corpora are isolated — concurrent batches don't
    interfere.
  • emerge_domains raising → logged, task removed from tracking,
    ingest path is never blocked or crashed.
  • db=None → silent no-op.

Uses a short debounce (0.05s) so tests run fast. Mocks emerge_domains
so no Neo4j/Qdrant/Mongo is required.
"""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Auth-package stubs (same pattern as prior phase tests) ──────────
def _install_stubs_if_missing() -> None:
    if "jose" not in sys.modules:
        try:
            import jose  # noqa: F401
        except ImportError:
            jose_mod = ModuleType("jose")

            class JWTError(Exception):
                pass

            class _Jwt:
                @staticmethod
                def encode(*_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

                @staticmethod
                def decode(*_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

            jose_mod.JWTError = JWTError
            jose_mod.jwt = _Jwt()
            sys.modules["jose"] = jose_mod

    if "passlib.context" not in sys.modules:
        try:
            import passlib.context  # noqa: F401
        except ImportError:
            passlib_mod = ModuleType("passlib")
            ctx_mod = ModuleType("passlib.context")

            class _CryptContext:
                def __init__(self, *a, **kw):
                    pass

                def hash(self, *_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

                def verify(self, *_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

            ctx_mod.CryptContext = _CryptContext
            passlib_mod.context = ctx_mod
            sys.modules["passlib"] = passlib_mod
            sys.modules["passlib.context"] = ctx_mod

    if "slowapi" not in sys.modules:
        try:
            import slowapi  # noqa: F401
        except ImportError:
            slowapi_mod = ModuleType("slowapi")
            util_mod = ModuleType("slowapi.util")

            class _Limiter:
                def __init__(self, *a, **kw):
                    pass

                def limit(self, *_a, **_kw):
                    def _decorator(fn):
                        return fn
                    return _decorator

            def _get_remote_address(_request):  # pragma: no cover
                return "0.0.0.0"

            slowapi_mod.Limiter = _Limiter
            util_mod.get_remote_address = _get_remote_address
            sys.modules["slowapi"] = slowapi_mod
            sys.modules["slowapi.util"] = util_mod


_install_stubs_if_missing()


from services.graph import cache_warmup  # noqa: E402


@pytest.fixture(autouse=True)
def reset_pending_tasks():
    """Clear the module-level task dict between tests so prior tests
    don't leak pending tasks into the next one."""
    cache_warmup._PENDING_WARMUP_TASKS.clear()
    yield
    # Clean up any tasks still hanging on at end-of-test so they don't
    # warn about unawaited coroutines.
    for t in list(cache_warmup._PENDING_WARMUP_TASKS.values()):
        if not t.done():
            t.cancel()
    cache_warmup._PENDING_WARMUP_TASKS.clear()


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_call_schedules_pending_task():
    """One call → a task is registered as pending."""
    fake_emerge = AsyncMock()
    with patch(
        "services.graph.analytics.emerge_domains",
        new=fake_emerge,
    ):
        cache_warmup.schedule_metrics_warmup_after_ingest(
            qdrant=MagicMock(),
            neo4j_driver=MagicMock(),
            db=MagicMock(),
            corpus_id="corp-1",
            debounce_seconds=0.05,
        )
        assert cache_warmup.is_warmup_pending("corp-1")
        # Wait for the debounce + rebuild to complete.
        await asyncio.sleep(0.15)
        fake_emerge.assert_awaited_once()
        # Task self-cleans on completion.
        assert not cache_warmup.is_warmup_pending("corp-1")


@pytest.mark.asyncio
async def test_debounce_collapses_rapid_calls_to_one_rebuild():
    """The key Phase-4 win: 50 rapid completions for the same corpus
    → exactly ONE emerge_domains invocation."""
    fake_emerge = AsyncMock()
    with patch(
        "services.graph.analytics.emerge_domains",
        new=fake_emerge,
    ):
        for _ in range(50):
            cache_warmup.schedule_metrics_warmup_after_ingest(
                qdrant=MagicMock(),
                neo4j_driver=MagicMock(),
                db=MagicMock(),
                corpus_id="corp-1",
                debounce_seconds=0.05,
            )
            # Tiny yield so the cancel-and-replace actually happens
            # (otherwise we'd schedule 50 tasks in one synchronous
            # burst before any of them got CPU).
            await asyncio.sleep(0.001)

        # Wait for the LAST scheduled task's debounce + rebuild.
        await asyncio.sleep(0.2)

    fake_emerge.assert_awaited_once()


@pytest.mark.asyncio
async def test_different_corpora_run_independently():
    """Concurrent ingest batches for different corpora each get their
    own rebuild."""
    fake_emerge = AsyncMock()
    with patch(
        "services.graph.analytics.emerge_domains",
        new=fake_emerge,
    ):
        cache_warmup.schedule_metrics_warmup_after_ingest(
            qdrant=MagicMock(), neo4j_driver=MagicMock(), db=MagicMock(),
            corpus_id="corp-A", debounce_seconds=0.05,
        )
        cache_warmup.schedule_metrics_warmup_after_ingest(
            qdrant=MagicMock(), neo4j_driver=MagicMock(), db=MagicMock(),
            corpus_id="corp-B", debounce_seconds=0.05,
        )
        cache_warmup.schedule_metrics_warmup_after_ingest(
            qdrant=MagicMock(), neo4j_driver=MagicMock(), db=MagicMock(),
            corpus_id="corp-C", debounce_seconds=0.05,
        )
        await asyncio.sleep(0.2)

    assert fake_emerge.await_count == 3
    called_corpus_ids = {
        call.args[3] for call in fake_emerge.await_args_list
    }
    assert called_corpus_ids == {"corp-A", "corp-B", "corp-C"}


@pytest.mark.asyncio
async def test_emerge_domains_failure_logs_and_cleans_up():
    """If emerge_domains raises, the failure is caught, the task is
    removed from tracking, and the ingest is never affected."""
    fake_emerge = AsyncMock(side_effect=RuntimeError("neo4j down"))
    with patch(
        "services.graph.analytics.emerge_domains",
        new=fake_emerge,
    ):
        cache_warmup.schedule_metrics_warmup_after_ingest(
            qdrant=MagicMock(), neo4j_driver=MagicMock(), db=MagicMock(),
            corpus_id="corp-1", debounce_seconds=0.05,
        )
        await asyncio.sleep(0.15)
    # emerge_domains was called once (and raised), and the task is no
    # longer pending. Caller (worker.py) saw no exception.
    fake_emerge.assert_awaited_once()
    assert not cache_warmup.is_warmup_pending("corp-1")


@pytest.mark.asyncio
async def test_db_none_is_silent_noop():
    """db=None → no task created, no log spam at WARNING+, no call."""
    fake_emerge = AsyncMock()
    with patch(
        "services.graph.analytics.emerge_domains",
        new=fake_emerge,
    ):
        cache_warmup.schedule_metrics_warmup_after_ingest(
            qdrant=MagicMock(), neo4j_driver=MagicMock(), db=None,
            corpus_id="corp-1", debounce_seconds=0.05,
        )
        await asyncio.sleep(0.1)
    fake_emerge.assert_not_called()
    assert not cache_warmup.is_warmup_pending("corp-1")


@pytest.mark.asyncio
async def test_active_ingest_batch_defers_rebuild(monkeypatch):
    """When enabled, auto-warm waits until a running durable batch settles."""

    class _Batches:
        def __init__(self):
            self.calls = 0

        async def find_one(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"_id": "batch-1"}
            return None

    class _Db:
        def __init__(self):
            self.batches = _Batches()

        def __getitem__(self, name):
            assert name == "ingest_batches"
            return self.batches

    monkeypatch.setenv("GRAPH_CACHE_WARMUP_SKIP_DURING_ACTIVE_INGEST", "true")
    monkeypatch.setenv("GRAPH_CACHE_WARMUP_ACTIVE_INGEST_DEFER_SECONDS", "0.01")
    fake_emerge = AsyncMock()
    fake_db = _Db()

    with patch(
        "services.graph.analytics.emerge_domains",
        new=fake_emerge,
    ):
        cache_warmup.schedule_metrics_warmup_after_ingest(
            qdrant=MagicMock(), neo4j_driver=MagicMock(), db=fake_db,
            corpus_id="corp-1", debounce_seconds=0.01,
        )
        await asyncio.sleep(0.015)
        fake_emerge.assert_not_called()
        await asyncio.sleep(0.08)

    fake_emerge.assert_awaited_once()
    assert fake_db.batches.calls >= 2
    assert not cache_warmup.is_warmup_pending("corp-1")


@pytest.mark.asyncio
async def test_pending_corpus_ids_lists_in_flight_only():
    """Diagnostic helper returns only corpora with non-done tasks."""
    fake_emerge = AsyncMock()
    with patch(
        "services.graph.analytics.emerge_domains",
        new=fake_emerge,
    ):
        cache_warmup.schedule_metrics_warmup_after_ingest(
            qdrant=MagicMock(), neo4j_driver=MagicMock(), db=MagicMock(),
            corpus_id="corp-X", debounce_seconds=0.3,  # long debounce
        )
        cache_warmup.schedule_metrics_warmup_after_ingest(
            qdrant=MagicMock(), neo4j_driver=MagicMock(), db=MagicMock(),
            corpus_id="corp-Y", debounce_seconds=0.3,
        )
        pending = cache_warmup.pending_corpus_ids()
        assert set(pending) == {"corp-X", "corp-Y"}
        # Cleanup
        for t in cache_warmup._PENDING_WARMUP_TASKS.values():
            t.cancel()


@pytest.mark.asyncio
async def test_cancellation_during_sleep_skips_rebuild():
    """If a pending task is cancelled before its sleep completes,
    emerge_domains is never called."""
    fake_emerge = AsyncMock()
    with patch(
        "services.graph.analytics.emerge_domains",
        new=fake_emerge,
    ):
        cache_warmup.schedule_metrics_warmup_after_ingest(
            qdrant=MagicMock(), neo4j_driver=MagicMock(), db=MagicMock(),
            corpus_id="corp-1", debounce_seconds=1.0,  # long enough to cancel
        )
        # Cancel immediately
        task = cache_warmup._PENDING_WARMUP_TASKS["corp-1"]
        task.cancel()
        await asyncio.sleep(0.05)
    fake_emerge.assert_not_called()


@pytest.mark.asyncio
async def test_after_completion_new_call_spawns_fresh_task():
    """Once a warmup completes, a NEW ingest completion correctly
    spawns a fresh task (the tracking dict drops the completed entry)."""
    fake_emerge = AsyncMock()
    with patch(
        "services.graph.analytics.emerge_domains",
        new=fake_emerge,
    ):
        cache_warmup.schedule_metrics_warmup_after_ingest(
            qdrant=MagicMock(), neo4j_driver=MagicMock(), db=MagicMock(),
            corpus_id="corp-1", debounce_seconds=0.05,
        )
        await asyncio.sleep(0.15)
        assert fake_emerge.await_count == 1
        # Now fire a NEW completion — should spawn a fresh task.
        cache_warmup.schedule_metrics_warmup_after_ingest(
            qdrant=MagicMock(), neo4j_driver=MagicMock(), db=MagicMock(),
            corpus_id="corp-1", debounce_seconds=0.05,
        )
        await asyncio.sleep(0.15)
    # Both invocations completed → emerge_domains called twice.
    assert fake_emerge.await_count == 2
