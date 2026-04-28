"""
Three-mode embedder dispatcher tests (local / api / modal) — Phase 21.

Mocks all HTTP. Verifies:
  - mode='local' hits the local sidecar path
  - mode='api' hits the per-corpus OpenAI-compatible endpoint with bearer auth
  - mode='modal' reads global Modal config and fails closed when unset
  - dim mismatch raises before any vector lands in Qdrant
  - legacy mode values ('local_st' / 'modal_tei' / 'siliconflow') are coerced
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import embedder


def _dim_response(dim: int = 1024, count: int = 1, model: str | None = None):
    """Build an OpenAI-compatible /embeddings response with the given dim."""
    body = {
        "data": [
            {"embedding": [0.01] * dim, "index": i} for i in range(count)
        ],
        "model": model or "Qwen/Qwen3-Embedding-0.6B",
    }
    resp = MagicMock()
    resp.json = MagicMock(return_value=body)
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
async def test_local_mode_hits_local_sidecar():
    with patch.object(embedder, "_embed_batch_local", new_callable=AsyncMock) as local_mock:
        local_mock.return_value = [[0.0] * 1024]
        vecs = await embedder.embed_batch(["hello"], mode="local", expected_dim=1024)
    local_mock.assert_awaited_once()
    assert len(vecs) == 1 and len(vecs[0]) == 1024


@pytest.mark.asyncio
async def test_api_mode_hits_generic_openai_endpoint():
    """mode='api' with per-corpus base_url + api_key must POST to
    <base>/embeddings with a Bearer token."""
    captured: dict = {}

    class _Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["auth"] = headers.get("Authorization") if headers else None
            captured["model"] = json.get("model") if json else None
            captured["dimensions"] = json.get("dimensions") if json else None
            return _dim_response(dim=1024, count=len(json["input"]))

    with patch.object(embedder.httpx, "AsyncClient", return_value=_Client()):
        vecs = await embedder.embed_batch(
            ["one"],
            mode="api",
            expected_dim=1024,
            expected_model_id="Qwen/Qwen3-Embedding-0.6B",
            base_url="https://example.com/v1",
            api_key="sk-corpus-secret",
        )

    assert captured["url"].endswith("/embeddings")
    assert captured["url"].startswith("https://example.com/v1")
    assert captured["auth"] == "Bearer sk-corpus-secret"
    assert captured["dimensions"] == 1024
    assert len(vecs) == 1 and len(vecs[0]) == 1024


@pytest.mark.asyncio
async def test_api_mode_embedding_pool_round_robins_batches(monkeypatch):
    captured: list[tuple[str, str, int]] = []

    async def fake_post(
        *,
        url,
        headers,
        texts,
        model_hint,
        expected_dim,
        expected_model_id,
        timeout,
        provider_label,
        request_dimensions=False,
    ):
        captured.append((url, model_hint, len(texts)))
        return [[float(t)] * expected_dim for t in texts]

    monkeypatch.setattr(embedder, "_post_openai_compatible", fake_post)
    texts = [str(i) for i in range(65)]

    vecs = await embedder.embed_batch(
        texts,
        mode="api",
        expected_dim=3,
        api_pool=[
            {
                "model": "embed-a",
                "base_url": "https://a.example/v1",
                "api_key": "sk-a",
                "max_concurrent": 2,
            },
            {
                "model": "embed-b",
                "base_url": "https://b.example/v1",
                "api_key": "sk-b",
                "max_concurrent": 1,
            },
        ],
    )

    assert [int(v[0]) for v in vecs] == list(range(65))
    assert ("https://a.example/v1/embeddings", "embed-a", 32) in captured
    assert ("https://b.example/v1/embeddings", "embed-b", 32) in captured
    assert ("https://a.example/v1/embeddings", "embed-a", 1) in captured


@pytest.mark.asyncio
async def test_api_mode_pool_fails_over_to_healthy_lane(monkeypatch):
    calls: list[str] = []

    async def fake_post(
        *,
        url,
        headers,
        texts,
        model_hint,
        expected_dim,
        expected_model_id,
        timeout,
        provider_label,
        request_dimensions=False,
    ):
        calls.append(model_hint)
        if model_hint == "bad-lane":
            raise RuntimeError("invalid api key")
        return [[float(t)] * expected_dim for t in texts]

    monkeypatch.setattr(embedder, "_post_openai_compatible", fake_post)

    vecs = await embedder.embed_batch(
        ["0", "1"],
        mode="api",
        expected_dim=3,
        api_pool=[
            {
                "model": "bad-lane",
                "base_url": "https://bad.example/v1",
                "api_key": "sk-bad",
                "max_concurrent": 1,
            },
            {
                "model": "healthy-lane",
                "base_url": "https://ok.example/v1",
                "api_key": "sk-ok",
                "max_concurrent": 1,
            },
        ],
    )

    assert calls == ["bad-lane", "healthy-lane"]
    assert [int(v[0]) for v in vecs] == [0, 1]


@pytest.mark.asyncio
async def test_api_mode_missing_creds_fails_closed():
    """No base_url, no api_key, no global siliconflow env must not wake GPU."""
    with patch.object(embedder, "_embed_batch_local", new_callable=AsyncMock) as local_mock, \
         patch.object(embedder, "get_settings") as settings_mock:
        settings_mock.return_value = MagicMock(
            SILICONFLOW_EMBEDDER_URL="",
            SILICONFLOW_API_KEY="",
            EMBEDDER_MODEL_NAME="Qwen3-Embedding-0.6B",
            SILICONFLOW_TIMEOUT_SECONDS=60.0,
            EMBED_ALLOW_LOCAL_FALLBACK=False,
        )
        with pytest.raises(RuntimeError, match="Local embedding fallback is disabled"):
            await embedder.embed_batch(["x"], mode="api", expected_dim=1024)
    local_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_modal_mode_not_deployed_fails_closed():
    modal_cfg = MagicMock(enabled=False, embedder_url="")
    with patch.object(embedder, "_embed_batch_local", new_callable=AsyncMock) as local_mock, \
         patch.object(embedder, "get_settings") as settings_mock, \
         patch("services.settings.settings_service.get_system_modal",
               new_callable=AsyncMock, return_value=modal_cfg):
        settings_mock.return_value = MagicMock(EMBED_ALLOW_LOCAL_FALLBACK=False)
        with pytest.raises(RuntimeError, match="Local embedding fallback is disabled"):
            await embedder.embed_batch(["x"], mode="modal", expected_dim=1024)
    local_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_dim_mismatch_raises_via_api_path():
    """Response vectors of the wrong length must trigger a ValueError
    BEFORE they land in Qdrant. Here the endpoint returns 768-dim but we
    ask for 1024."""
    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            return _dim_response(dim=768, count=len(json["input"]))

    with patch.object(embedder.httpx, "AsyncClient", return_value=_Client()), \
         patch.object(embedder, "_embed_batch_local", new_callable=AsyncMock) as local_mock, \
         patch.object(embedder, "get_settings") as settings_mock:
        settings_mock.return_value = MagicMock(
            EMBEDDER_MODEL_NAME="Qwen3-Embedding-0.6B",
            SILICONFLOW_TIMEOUT_SECONDS=60.0,
            EMBED_ALLOW_LOCAL_FALLBACK=False,
        )
        with pytest.raises(RuntimeError, match="dimension mismatch"):
            await embedder.embed_batch(
                ["x"], mode="api", expected_dim=1024,
                base_url="https://example.com/v1", api_key="sk-x",
            )
    local_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_mode_values_coerced_in_dispatcher():
    """Even if the worker or a cached config hands us a legacy mode string,
    the dispatcher should coerce rather than crashing."""
    with patch.object(embedder, "_embed_batch_local", new_callable=AsyncMock) as local_mock:
        local_mock.return_value = [[0.0] * 1024]
        await embedder.embed_batch(["x"], mode="local_st", expected_dim=1024)
    local_mock.assert_awaited_once()
