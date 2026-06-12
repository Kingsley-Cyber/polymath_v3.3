"""
Embedding client — dispatcher across local + cloud providers.

Providers (Phase 14.3 + 10.15 refinements):
  local_st     — local Docker sentence-transformers sidecar (fallback + query path)
  modal_tei    — Modal cloud GPU webhook (TEI container)
  siliconflow  — SiliconFlow OpenAI-compatible embeddings API

Architecture invariants:
  - ALL providers must serve the corpus's frozen `embedding_model_id` at
    `embedding_dimension`. The dispatcher asserts dim on every response row;
    mismatch raises before any vector lands in Qdrant.
  - `embed_query` uses the corpus-frozen provider when config is supplied so
    API-ingested corpora query the same vector space.
  - Local fallback is opt-in only. API/Modal failures raise by default instead
    of silently moving a large ingest onto the user's GPU.

Selection:
  worker.py passes `mode=ingestion_config.embed_mode` + `expected_dim` +
  `expected_model_id` into `embed_batch`. On mode-provider availability
  failure, dispatcher raises unless EMBED_ALLOW_LOCAL_FALLBACK=true.
"""

import asyncio
import logging
import time
from typing import Any

import httpx

from config import get_settings

logger = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 32
_MAX_BATCH_SIZE = 512
_LOCAL_TIMEOUT = 600.0
_DEFAULT_DIM = 1024  # fallback when caller doesn't specify (e.g. query path on Qwen3-0.6B)


_LEGACY_MODE_ALIASES = {
    "local_st": "local",
    "modal_tei": "modal",
    "siliconflow": "api",
}


def _decrypt_api_key(value: str | None) -> str | None:
    if not value:
        return None
    try:
        from services.secrets import decrypt

        return decrypt(value) or value
    except Exception:
        return value


def _plaintext_embedding_pool(api_pool: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in api_pool or []:
        data = entry.model_dump() if hasattr(entry, "model_dump") else dict(entry)
        if data.get("api_key"):
            data["api_key"] = _decrypt_api_key(data.get("api_key"))
        out.append(data)
    return out


def _embedding_batch_size() -> int:
    """Configured request batch size, clamped for provider and memory sanity."""
    value = getattr(get_settings(), "EMBED_BATCH_SIZE", _DEFAULT_BATCH_SIZE)
    if not isinstance(value, (int, float, str)):
        return _DEFAULT_BATCH_SIZE
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_BATCH_SIZE
    return max(1, min(_MAX_BATCH_SIZE, parsed))


async def _local_fallback_or_raise(
    *,
    provider: str,
    reason: Exception | str,
    texts: list[str],
    dim: int,
):
    settings = get_settings()
    message = f"{provider} embedding failed or is unavailable: {reason}"
    if settings.EMBED_ALLOW_LOCAL_FALLBACK:
        logger.warning("%s — falling back to local embedder", message)
        return await _embed_batch_local(texts, dim)
    raise RuntimeError(
        f"{message}. Local embedding fallback is disabled by EMBED_ALLOW_LOCAL_FALLBACK=false."
    )


async def embed_batch(
    texts: list[str],
    mode: str = "local",
    *,
    expected_dim: int | None = None,
    expected_model_id: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    max_concurrent: int | None = None,
    modal_containers: int | None = None,
    api_pool: list[dict[str, Any]] | None = None,
) -> list[list[float]]:
    """Dispatch to the selected embedding provider.

    Args:
        texts: strings to embed
        mode: "local" | "api" | "modal" (IngestionConfig.embed_mode). Legacy
            values "local_st" / "modal_tei" / "siliconflow" are accepted and
            coerced for backward compatibility during the rename window.
        expected_dim: dim the corpus is frozen to. Every response row must
            match — raised as ValueError on mismatch.
        expected_model_id: optional model-drift guard. Cloud providers echo
            `model` back; if given and different, raise.
        base_url: per-corpus API base URL. Required when mode='api'; ignored
            otherwise.
        api_key: per-corpus plaintext API key. The worker decrypts Fernet
            ciphertext before calling. Ignored for local mode.
        max_concurrent: legacy scalar API concurrency hint.
        modal_containers: per-corpus Modal max-containers override. Passed
            to Modal endpoint for auto-scale hint.
        api_pool: optional list of OpenAI-compatible embedding endpoints.
            When provided for mode='api', batches are round-robined across
            entries and each entry's max_concurrent gates in-flight calls.
    """
    if not texts:
        return []

    # Legacy mode rename bridge — callers or on-disk data may still use the
    # old values. Coerce silently so the dispatcher only sees the tri-value.
    mode = _LEGACY_MODE_ALIASES.get(mode, mode)

    settings = get_settings()
    dim = expected_dim if expected_dim is not None else _DEFAULT_DIM

    # ── Modal ────────────────────────────────────────────────────────────
    if mode == "modal":
        from services.settings import settings_service

        modal_cfg = await settings_service.get_system_modal()
        if modal_cfg.enabled and modal_cfg.embedder_url:
            try:
                return await _embed_batch_modal(
                    texts, dim, expected_model_id, modal_cfg.embedder_url
                )
            except Exception as exc:
                return await _local_fallback_or_raise(
                    provider="Modal",
                    reason=exc,
                    texts=texts,
                    dim=dim,
                )
        return await _local_fallback_or_raise(
            provider="Modal",
            reason="embed_mode='modal' but Modal is not deployed/enabled",
            texts=texts,
            dim=dim,
        )

    # ── API (OpenAI-compatible /embeddings, any provider) ────────────────
    if mode == "api":
        if api_pool:
            try:
                return await _embed_batch_api_pool(
                    texts=texts,
                    api_pool=api_pool,
                    expected_dim=dim,
                )
            except Exception as exc:
                return await _local_fallback_or_raise(
                    provider="API embedding pool",
                    reason=exc,
                    texts=texts,
                    dim=dim,
                )

        # Per-corpus creds take precedence. Fall through to SiliconFlow
        # globals when unset so existing siliconflow-mode corpora keep
        # working without per-corpus configuration.
        eff_url = base_url or settings.SILICONFLOW_EMBEDDER_URL
        eff_key = api_key or (settings.SILICONFLOW_API_KEY or None)
        if not (eff_url and eff_key):
            return await _local_fallback_or_raise(
                provider="API embedding",
                reason="base_url or api_key missing (per-corpus and global)",
                texts=texts,
                dim=dim,
            )
        try:
            return await _embed_batch_api(
                texts=texts,
                base_url=eff_url,
                api_key=eff_key,
                expected_dim=dim,
                expected_model_id=expected_model_id,
            )
        except Exception as exc:
            return await _local_fallback_or_raise(
                provider="API embedding",
                reason=exc,
                texts=texts,
                dim=dim,
            )

    # ── Local (default) ─────────────────────────────────────────────────
    return await _embed_batch_local(texts, dim)


async def _embed_batch_api(
    *,
    texts: list[str],
    base_url: str,
    api_key: str,
    expected_dim: int,
    expected_model_id: str | None,
) -> list[list[float]]:
    """Generic OpenAI-compatible /embeddings caller for `embed_mode='api'`.

    Any provider hosting the frozen `embedding_model_id` behind a standard
    /embeddings endpoint works — SiliconFlow, Together.ai, self-hosted TEI
    fronted by nginx, etc. Path suffix is appended if missing.
    """
    settings = get_settings()
    url = base_url.rstrip("/")
    if not url.endswith("/embeddings"):
        url = url + "/embeddings"
    return await _post_openai_compatible(
        url=url,
        headers={"Authorization": f"Bearer {api_key}"},
        texts=texts,
        model_hint=expected_model_id or settings.EMBEDDER_MODEL_NAME,
        expected_dim=expected_dim,
        expected_model_id=expected_model_id,
        timeout=settings.SILICONFLOW_TIMEOUT_SECONDS,
        provider_label="api",
        request_dimensions=True,
    )


def _normalize_api_pool(api_pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, entry in enumerate(api_pool or []):
        model = str(entry.get("model") or "").strip()
        base_url = str(entry.get("base_url") or "").strip()
        api_key = str(entry.get("api_key") or "").strip()
        if not (model and base_url and api_key):
            logger.warning(
                "Skipping incomplete embedding API pool entry idx=%d model=%s base_url_set=%s key_set=%s",
                idx,
                bool(model),
                bool(base_url),
                bool(api_key),
            )
            continue
        out.append(
            {
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
                "provider": entry.get("provider_preset") or f"api_pool_{idx}",
                "max_concurrent": max(
                    1,
                    min(64, int(entry.get("max_concurrent") or 1)),
                ),
            }
        )
    return out


async def _embed_batch_api_pool(
    *,
    texts: list[str],
    api_pool: list[dict[str, Any]],
    expected_dim: int,
) -> list[list[float]]:
    pool = _normalize_api_pool(api_pool)
    if not pool:
        raise ValueError("embedding API pool has no complete entries")

    semaphores = [asyncio.Semaphore(entry["max_concurrent"]) for entry in pool]
    batch_size = _embedding_batch_size()
    batches = [
        (batch_idx, start, texts[start : start + batch_size])
        for batch_idx, start in enumerate(range(0, len(texts), batch_size))
    ]
    attempts: dict[int, int] = {batch_idx: 0 for batch_idx, _, _ in batches}
    disabled_lanes: set[int] = set()
    max_attempts = max(2, len(pool) + 1)
    failures: list[str] = []

    def _lane_for(batch_idx: int, attempt: int) -> int | None:
        for offset in range(len(pool)):
            entry_idx = (batch_idx + attempt + offset) % len(pool)
            if entry_idx not in disabled_lanes:
                return entry_idx
        return None

    def _error_summary(exc: BaseException) -> str:
        text = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            text = f"HTTP {exc.response.status_code}: {exc.response.text[:180]}"
        return text.replace("\n", " ")[:300]

    def _is_lane_fatal(exc: BaseException) -> bool:
        text = str(exc).lower()
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status in {401, 402, 403, 404}:
                return True
        fatal_markers = (
            "invalid api key",
            "unauthorized",
            "insufficient balance",
            "insufficient credits",
            "not enough balance",
            "model drift",
            "dimension mismatch",
            "model does not exist",
            "model not found",
        )
        return any(marker in text for marker in fatal_markers)

    async def _run_batch(
        batch_idx: int,
        start: int,
        batch: list[str],
        entry_idx: int,
    ):
        entry = pool[entry_idx]
        url = entry["base_url"].rstrip("/")
        if not url.endswith("/embeddings"):
            url = url + "/embeddings"
        async with semaphores[entry_idx]:
            vectors = await _post_openai_compatible(
                url=url,
                headers={"Authorization": f"Bearer {entry['api_key']}"},
                texts=batch,
                model_hint=entry["model"],
                expected_dim=expected_dim,
                # Pool entries may use provider-specific model ids. Dimension
                # lock is the invariant that protects the Qdrant collection.
                expected_model_id=None,
                timeout=get_settings().SILICONFLOW_TIMEOUT_SECONDS,
                provider_label=f"embedding_pool:{entry['provider']}",
                request_dimensions=True,
            )
            return batch_idx, start, vectors, entry_idx

    pending = list(batches)
    results: list[tuple[int, list[list[float]]]] = []
    while pending:
        scheduled_meta: list[tuple[int, int, list[str], int]] = []
        for batch_idx, start, batch in pending:
            entry_idx = _lane_for(batch_idx, attempts[batch_idx])
            if entry_idx is None:
                failures.append(
                    f"batch {batch_idx}: no healthy embedding API lanes remain"
                )
                continue
            scheduled_meta.append((batch_idx, start, batch, entry_idx))
        pending = []
        if not scheduled_meta:
            break

        pass_results = await asyncio.gather(
            *(
                _run_batch(batch_idx, start, batch, entry_idx)
                for batch_idx, start, batch, entry_idx in scheduled_meta
            ),
            return_exceptions=True,
        )
        for meta, item in zip(scheduled_meta, pass_results):
            batch_idx, start, batch, entry_idx = meta
            if not isinstance(item, BaseException):
                _batch_idx, result_start, vectors, _entry_idx = item
                results.append((result_start, vectors))
                continue

            attempts[batch_idx] += 1
            entry = pool[entry_idx]
            summary = _error_summary(item)
            if _is_lane_fatal(item):
                disabled_lanes.add(entry_idx)
                logger.warning(
                    "Embedding API lane disabled provider=%s model=%s error=%s",
                    entry["provider"],
                    entry["model"],
                    summary,
                )
            else:
                logger.warning(
                    "Embedding API batch failed provider=%s model=%s attempt=%d/%d error=%s",
                    entry["provider"],
                    entry["model"],
                    attempts[batch_idx],
                    max_attempts,
                    summary,
                )

            if (
                attempts[batch_idx] < max_attempts
                and _lane_for(batch_idx, attempts[batch_idx]) is not None
            ):
                pending.append((batch_idx, start, batch))
            else:
                failures.append(
                    f"batch {batch_idx} via {entry['provider']}: {summary}"
                )
        if pending:
            await asyncio.sleep(min(0.5 * max(attempts.values()), 3.0))

    if failures:
        raise RuntimeError(
            "embedding API pool failed after retries: " + "; ".join(failures[:5])
        )
    ordered: list[list[float] | None] = [None] * len(texts)
    for start, vectors in results:
        ordered[start : start + len(vectors)] = vectors
    if any(v is None for v in ordered):
        raise ValueError("embedding API pool returned incomplete vector set")
    return [v for v in ordered if v is not None]


async def embed_query(text: str, config: dict[str, Any] | None = None) -> list[float]:
    """
    Single query embedding.

    When a corpus config is supplied, use its frozen embedding provider so
    SiliconFlow/API-ingested corpora are queried in the same vector space.
    Callers without corpus context retain the local fallback path.
    """
    if config:
        raw_key = config.get("embed_api_key")
        api_pool = _plaintext_embedding_pool(config.get("embedding_models"))
        results = await embed_batch(
            [text],
            mode=config.get("embed_mode") or "local",
            expected_dim=config.get("embedding_dimension") or _DEFAULT_DIM,
            expected_model_id=config.get("embedding_model_id"),
            base_url=config.get("embed_base_url"),
            api_key=_decrypt_api_key(raw_key),
            max_concurrent=config.get("embed_max_concurrent"),
            modal_containers=config.get("modal_containers"),
            api_pool=api_pool,
        )
        return results[0]
    results = await _embed_batch_local([text], _DEFAULT_DIM)
    return results[0]


async def _embed_batch_local(
    texts: list[str],
    expected_dim: int,
) -> list[list[float]]:
    """Local embedder sidecar — Docker sentence-transformers service."""
    settings = get_settings()
    url = f"{settings.EMBEDDER_URL}/embeddings"
    vectors: list[list[float]] = []
    batch_size = _embedding_batch_size()

    async with httpx.AsyncClient(timeout=_LOCAL_TIMEOUT) as client:
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            batch_vectors = await _embed_local_batch_with_split(
                client=client,
                url=url,
                batch=batch,
                expected_dim=expected_dim,
            )
            vectors.extend(batch_vectors)

    return vectors


async def _post_local_with_retries(
    *,
    client: httpx.AsyncClient,
    url: str,
    batch: list[str],
    expected_dim: int,
) -> list[list[float]]:
    """Retry transient sidecar failures (intermittent 400/5xx/short responses
    observed under GPU contention — PILOT_REPORT resilience #2) before
    raising. Timeouts re-raise immediately so the caller's halve-and-recurse
    path handles them."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return await _post_local_embedding_batch(
                client=client,
                url=url,
                batch=batch,
                expected_dim=expected_dim,
            )
        except httpx.TimeoutException:
            raise
        except (
            httpx.HTTPStatusError,
            httpx.ConnectError,
            # TransportError covers RemoteProtocolError ("Server disconnected
            # without sending a response"), ReadError, WriteError — the
            # embedder dropping a connection mid-request (restart, load
            # spike). One such drop used to kill the doc's whole embed phase
            # because only ConnectError was retried (observed live: doc
            # failed verify with 0 vectors after a single disconnect).
            httpx.TransportError,
            ValueError,
        ) as exc:
            last_exc = exc
            logger.warning(
                "Local embedder transient failure (attempt %d/3, batch=%d): %s",
                attempt + 1, len(batch), exc,
            )
            await asyncio.sleep(1.5 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


async def _embed_local_batch_with_split(
    *,
    client: httpx.AsyncClient,
    url: str,
    batch: list[str],
    expected_dim: int,
) -> list[list[float]]:
    """Embed a local batch, splitting it when a large PDF batch times out."""
    try:
        return await _post_local_with_retries(
            client=client,
            url=url,
            batch=batch,
            expected_dim=expected_dim,
        )
    except httpx.TimeoutException:
        if len(batch) <= 1:
            logger.exception(
                "Local embedder timed out for a singleton text; cannot split further"
            )
            raise

        midpoint = max(1, len(batch) // 2)
        logger.warning(
            "Local embedder timed out for batch_size=%d; retrying as %d/%d",
            len(batch),
            midpoint,
            len(batch) - midpoint,
        )
        left = await _embed_local_batch_with_split(
            client=client,
            url=url,
            batch=batch[:midpoint],
            expected_dim=expected_dim,
        )
        right = await _embed_local_batch_with_split(
            client=client,
            url=url,
            batch=batch[midpoint:],
            expected_dim=expected_dim,
        )
        return left + right


async def _post_local_embedding_batch(
    *,
    client: httpx.AsyncClient,
    url: str,
    batch: list[str],
    expected_dim: int,
) -> list[list[float]]:
    resp = await client.post(url, json={"input": batch, "model": "embed"})
    resp.raise_for_status()
    data = resp.json()
    items = data.get("data") or []
    # COUNT IS A CONTRACT. A silent partial response (seen once under GPU
    # contention) used to flow through unchecked — downstream zips then drop
    # the tail chunk or, worse, misalign summary vectors onto children.
    if len(items) != len(batch):
        raise ValueError(
            f"Local embedder returned {len(items)} vectors for {len(batch)} texts"
        )
    batch_vectors = [
        item["embedding"] for item in sorted(items, key=lambda x: x["index"])
    ]
    for v in batch_vectors:
        if len(v) != expected_dim:
            raise ValueError(
                f"Local embedder dimension mismatch: expected {expected_dim}, got {len(v)}. "
                f"Deploy the embedder container with a model that matches the corpus's frozen dimension."
            )
    return batch_vectors


async def _embed_batch_modal(
    texts: list[str],
    expected_dim: int,
    expected_model_id: str | None,
    embedder_url: str | None = None,
) -> list[list[float]]:
    """
    Modal cloud GPU embedder — OpenAI-compatible /embeddings endpoint.

    Phase 19.3 — `embedder_url` override lets the dispatcher pass the UI-edited
    Mongo URL through. When None, falls back to settings.MODAL_EMBEDDER_URL for
    legacy callers.

    Raises on:
      - non-2xx HTTP status
      - missing `data` field
      - any vector whose length != expected_dim (dimension lock)
      - model_id mismatch if expected_model_id is given and response.model disagrees
    Caller (embed_batch) catches and falls back to local.
    """
    settings = get_settings()
    headers: dict[str, str] = {}
    if settings.MODAL_API_KEY:
        headers["Authorization"] = f"Bearer {settings.MODAL_API_KEY}"

    return await _post_openai_compatible(
        url=embedder_url or settings.MODAL_EMBEDDER_URL,
        headers=headers,
        texts=texts,
        model_hint=expected_model_id or settings.EMBEDDER_MODEL_NAME,
        expected_dim=expected_dim,
        expected_model_id=expected_model_id,
        timeout=settings.MODAL_TIMEOUT_SECONDS,
        provider_label="Modal",
        request_dimensions=False,
    )


async def _embed_batch_siliconflow(
    texts: list[str],
    expected_dim: int,
    expected_model_id: str | None,
) -> list[list[float]]:
    """SiliconFlow cloud API — OpenAI-compatible /embeddings endpoint."""
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.SILICONFLOW_API_KEY}"}

    return await _post_openai_compatible(
        url=settings.SILICONFLOW_EMBEDDER_URL,
        headers=headers,
        texts=texts,
        # SiliconFlow requires the model parameter; use the corpus's frozen id
        model_hint=expected_model_id or "Qwen/Qwen3-Embedding-0.6B",
        expected_dim=expected_dim,
        expected_model_id=expected_model_id,
        timeout=settings.SILICONFLOW_TIMEOUT_SECONDS,
        provider_label="SiliconFlow",
        request_dimensions=True,
    )


def _provider_supports_dimensions(model_hint: str) -> bool:
    """Return true for embedding models where the API accepts dimensions."""
    model = (model_hint or "").lower()
    return "qwen3-embedding" in model or "text-embedding-3" in model


async def _post_openai_compatible(
    *,
    url: str,
    headers: dict[str, str],
    texts: list[str],
    model_hint: str,
    expected_dim: int,
    expected_model_id: str | None,
    timeout: float,
    provider_label: str,
    request_dimensions: bool = False,
) -> list[list[float]]:
    """
    Shared OpenAI-compatible /embeddings POST helper used by all cloud paths.
    Every response row is checked for dimension match; model_id mismatch raises.
    """
    vectors: list[list[float]] = []
    batch_size = _embedding_batch_size()

    async with httpx.AsyncClient(timeout=timeout) as client:
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            payload: dict[str, Any] = {"input": batch, "model": model_hint}
            if request_dimensions and _provider_supports_dimensions(model_hint):
                payload["dimensions"] = expected_dim
            resp = await client.post(
                url,
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            if "data" not in data:
                raise ValueError(f"{provider_label} response missing 'data' field: {data}")

            # Optional model_id drift check
            if expected_model_id:
                returned_model = data.get("model", "")
                if returned_model and returned_model != expected_model_id:
                    raise ValueError(
                        f"{provider_label} model drift: corpus frozen to "
                        f"'{expected_model_id}', endpoint returned '{returned_model}'"
                    )

            batch_vectors = [
                item["embedding"]
                for item in sorted(data["data"], key=lambda x: x["index"])
            ]
            for v in batch_vectors:
                if len(v) != expected_dim:
                    raise ValueError(
                        f"{provider_label} dimension mismatch: expected {expected_dim}, got {len(v)}"
                    )
            vectors.extend(batch_vectors)

    return vectors


# ── Probe helpers (used by health checks + settings UI test buttons) ────────


async def probe_modal(sample_text: str = "health") -> dict:
    """Probe the Modal endpoint — returns {ok, latency_ms, dimension, model, error}."""
    return await _probe_provider(
        mode="modal_tei",
        enabled=get_settings().MODAL_ENABLED,
        url=get_settings().MODAL_EMBEDDER_URL,
        sample_text=sample_text,
        provider_label="Modal",
    )


async def probe_siliconflow(sample_text: str = "health") -> dict:
    """Probe the SiliconFlow endpoint — returns {ok, latency_ms, dimension, model, error}."""
    return await _probe_provider(
        mode="siliconflow",
        enabled=get_settings().SILICONFLOW_ENABLED,
        url=get_settings().SILICONFLOW_EMBEDDER_URL,
        sample_text=sample_text,
        provider_label="SiliconFlow",
    )


async def _probe_provider(
    *,
    mode: str,
    enabled: bool,
    url: str,
    sample_text: str,
    provider_label: str,
) -> dict:
    """
    Call the provider with a 1-text sample and return dim/model metadata.
    `expected_dim` is not asserted here (a probe's job is to report the dim,
    not validate it). Caller can compare against the corpus's frozen dim.
    """
    if not enabled:
        return {"ok": False, "latency_ms": 0.0, "dimension": None, "model": None, "error": f"{provider_label} disabled"}
    if not url:
        return {"ok": False, "latency_ms": 0.0, "dimension": None, "model": None, "error": f"{provider_label} URL empty"}

    start = time.perf_counter()
    try:
        # Bypass the dim check by using a huge expected_dim and catching the ValueError
        # — but cleaner: call _post_openai_compatible with expected_dim=-1 and handle below.
        # Simplest: make a direct HTTP call here.
        settings = get_settings()
        headers: dict[str, str] = {}
        if mode == "modal_tei" and settings.MODAL_API_KEY:
            headers["Authorization"] = f"Bearer {settings.MODAL_API_KEY}"
        if mode == "siliconflow" and settings.SILICONFLOW_API_KEY:
            headers["Authorization"] = f"Bearer {settings.SILICONFLOW_API_KEY}"

        timeout = (
            settings.MODAL_TIMEOUT_SECONDS
            if mode == "modal_tei"
            else settings.SILICONFLOW_TIMEOUT_SECONDS
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                headers=headers,
                json={"input": [sample_text], "model": settings.EMBEDDER_MODEL_NAME},
            )
            resp.raise_for_status()
            data = resp.json()

        latency_ms = (time.perf_counter() - start) * 1000.0
        vec = data["data"][0]["embedding"]
        return {
            "ok": True,
            "latency_ms": round(latency_ms, 2),
            "dimension": len(vec),
            "model": data.get("model"),
            "error": None,
        }
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000.0
        return {
            "ok": False,
            "latency_ms": round(latency_ms, 2),
            "dimension": None,
            "model": None,
            "error": str(exc),
        }
