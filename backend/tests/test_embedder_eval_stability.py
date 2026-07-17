"""Asserting tests for the fail-closed MLX evaluation-batch contract."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from scripts import run_eval_with_embedder_preflight as eval_wrapper
from services import embedder


class _Response:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.request = httpx.Request("GET", "http://embedder/health")

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=self.request,
                response=httpx.Response(
                    self.status_code,
                    request=self.request,
                ),
            )


def _healthy_payload() -> dict:
    return {
        "status": "ok",
        "model": "Qwen3-Embedding-0.6B-mxfp8",
        "liveness": True,
        "model_loaded": True,
        "inference_ready": True,
        "in_flight": False,
        "queue_depth": 0,
        "last_error": None,
        "warmup": {
            "complete": True,
            "vector_dim": 1024,
            "model": "Qwen3-Embedding-0.6B-mxfp8",
            "error": None,
        },
    }


@pytest.mark.asyncio
async def test_local_timeout_retries_exactly_once(monkeypatch):
    calls = 0

    async def always_timeout(**_kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(embedder, "_post_local_embedding_batch", always_timeout)
    monkeypatch.setattr(embedder.asyncio, "sleep", no_sleep)

    with pytest.raises(httpx.ReadTimeout):
        await embedder._post_local_with_retries(
            client=object(),
            url="http://embedder/embeddings",
            batch=["one"],
            expected_dim=1024,
            workload_class="interactive_query",
        )

    assert calls == 2


@pytest.mark.asyncio
async def test_local_timeout_retry_can_recover(monkeypatch):
    calls = 0

    async def timeout_then_vector(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow")
        return [[0.0] * 1024]

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(embedder, "_post_local_embedding_batch", timeout_then_vector)
    monkeypatch.setattr(embedder.asyncio, "sleep", no_sleep)

    result = await embedder._post_local_with_retries(
        client=object(),
        url="http://embedder/embeddings",
        batch=["one"],
        expected_dim=1024,
        workload_class="interactive_query",
    )

    assert calls == 2
    assert len(result) == 1
    assert len(result[0]) == 1024


def test_eval_health_rejects_busy_or_degraded_sidecar():
    busy = _healthy_payload()
    busy["queue_depth"] = 1
    with pytest.raises(RuntimeError, match="queue_depth"):
        embedder._validate_eval_embedder_health(
            busy,
            expected_dim=1024,
            phase="initial",
        )

    degraded = _healthy_payload()
    degraded["last_error"] = "ReadTimeout"
    with pytest.raises(RuntimeError, match="last_error"):
        embedder._validate_eval_embedder_health(
            degraded,
            expected_dim=1024,
            phase="initial",
        )


@pytest.mark.asyncio
async def test_eval_preflight_warms_same_production_pool(monkeypatch):
    health_responses = [_Response(_healthy_payload()), _Response(_healthy_payload())]

    class _Client:
        async def get(self, url):
            assert url == "http://embedder:8082/health"
            return health_responses.pop(0)

    client = _Client()
    post_seen = {}
    monkeypatch.setattr(
        embedder,
        "get_settings",
        lambda: SimpleNamespace(
            EMBEDDER_URL="http://embedder:8082",
            EMBEDDING_DIMENSION=1024,
        ),
    )
    monkeypatch.setattr(
        embedder,
        "_get_local_http_client",
        lambda timeout: client,
    )

    async def fake_post(**kwargs):
        post_seen.update(kwargs)
        return [[0.0] * 1024]

    monkeypatch.setattr(embedder, "_post_local_with_retries", fake_post)

    receipt = await embedder.preflight_local_embedder_for_eval_batch()

    assert not health_responses
    assert post_seen["client"] is client
    assert post_seen["workload_class"] == "interactive_query"
    assert post_seen["batch"] == [embedder._EVAL_PREFLIGHT_TEXT]
    assert receipt["status"] == "ready"
    assert receipt["query_timeout_seconds"] == 30.0
    assert receipt["timeout_retries"] == 1


def test_eval_wrapper_never_launches_command_after_failed_preflight(monkeypatch):
    launched = False

    def fail_probe(_url, _timeout):
        raise RuntimeError("degraded")

    def should_not_run(*_args, **_kwargs):
        nonlocal launched
        launched = True

    monkeypatch.setattr(eval_wrapper, "probe_embedder", fail_probe)
    monkeypatch.setattr(eval_wrapper.subprocess, "run", should_not_run)

    with pytest.raises(RuntimeError, match="degraded"):
        eval_wrapper.run_after_preflight(
            ["python", "frozen_eval.py"],
            preflight_url="http://backend/preflight",
            timeout_seconds=35.0,
        )

    assert launched is False


def test_eval_wrapper_launches_exact_command_after_green_preflight(monkeypatch):
    command_seen = []

    monkeypatch.setattr(
        eval_wrapper,
        "probe_embedder",
        lambda _url, _timeout: {"status": "ready"},
    )

    def fake_run(command, check):
        command_seen.append((command, check))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(eval_wrapper.subprocess, "run", fake_run)

    result = eval_wrapper.run_after_preflight(
        ["python", "frozen_eval.py", "--arm", "on"],
        preflight_url="http://backend/preflight",
        timeout_seconds=35.0,
    )

    assert result == 7
    assert command_seen == [
        (["python", "frozen_eval.py", "--arm", "on"], False),
    ]


def test_configured_query_timeout_and_outer_deadline_are_thirty_seconds():
    from config import Settings

    field = Settings.model_fields["QUERY_PLAN_EMBED_DEADLINE_SECONDS"]
    assert field.default == 30.0
    assert embedder._QUERY_LOCAL_TIMEOUT == 30.0
    assert embedder._LOCAL_KEEPALIVE_EXPIRY_SECONDS >= 120.0
