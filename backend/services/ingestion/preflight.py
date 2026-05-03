"""Preflight checks for ingestion admission.

These checks run before parse/chunk/Ghost A work so a bad embedding route does
not leave large documents stuck as Mongo-only after spending summary tokens.
They are intentionally shallow: API embedding providers are configuration
checked without sending paid embedding requests, while local services are
health-probed on the Docker network.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from config import get_settings
from models.schemas import IngestionConfig


_MODE_ALIASES = {
    "local_st": "local",
    "modal_tei": "modal",
    "siliconflow": "api",
}


def _mode(value: str | None) -> str:
    return _MODE_ALIASES.get(str(value or "local"), str(value or "local"))


def _profile_value(entry: Any, key: str) -> Any:
    if hasattr(entry, "model_dump"):
        entry = entry.model_dump()
    if isinstance(entry, dict):
        return entry.get(key)
    return getattr(entry, key, None)


def _profile_api_key(entry: Any) -> str:
    raw = str(_profile_value(entry, "api_key") or "").strip()
    if not raw or raw in {"[set]", "local"}:
        return raw
    try:
        from services.secrets import decrypt

        return decrypt(raw) or raw
    except Exception:
        return raw


def _vllm_served_model_name(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


def _is_local_vllm_entry(entry: Any, base_url: str) -> bool:
    preset = str(_profile_value(entry, "provider_preset") or "").strip()
    return preset == "vllm-local" or "vllm-" in base_url


def _has_complete_embedding_pool(config: IngestionConfig) -> bool:
    for entry in getattr(config, "embedding_models", None) or []:
        if (
            str(_profile_value(entry, "model") or "").strip()
            and str(_profile_value(entry, "base_url") or "").strip()
            and str(_profile_value(entry, "api_key") or "").strip()
        ):
            return True
    return False


async def _probe_local_embedder(settings: Any) -> dict[str, Any]:
    if not bool(getattr(settings, "LOCAL_EMBEDDER_ENABLED", False)):
        return {
            "ok": False,
            "provider": "local",
            "url": getattr(settings, "EMBEDDER_URL", ""),
            "error": (
                "embed_mode='local' but LOCAL_EMBEDDER_ENABLED=false. "
                "Start the local embedder service/profile or choose an API/modal embed mode."
            ),
        }
    url = str(getattr(settings, "EMBEDDER_URL", "") or "").rstrip("/")
    if not url:
        return {
            "ok": False,
            "provider": "local",
            "url": "",
            "error": "embed_mode='local' but EMBEDDER_URL is empty.",
        }
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{url}/health")
            response.raise_for_status()
        return {
            "ok": True,
            "provider": "local",
            "url": url,
            "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
            "error": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": "local",
            "url": url,
            "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
            "error": str(exc),
        }


def _check_api_embedding(config: IngestionConfig, settings: Any) -> dict[str, Any]:
    if _has_complete_embedding_pool(config):
        return {
            "ok": True,
            "provider": "api_pool",
            "url": "embedding_models",
            "live_probe": False,
            "error": None,
        }
    has_url = bool(
        str(getattr(config, "embed_base_url", "") or "").strip()
        or str(getattr(settings, "SILICONFLOW_EMBEDDER_URL", "") or "").strip()
    )
    has_key = bool(
        str(getattr(config, "embed_api_key", "") or "").strip()
        or str(getattr(settings, "SILICONFLOW_API_KEY", "") or "").strip()
    )
    return {
        "ok": bool(has_url and has_key),
        "provider": "api",
        "url": str(
            getattr(config, "embed_base_url", None)
            or getattr(settings, "SILICONFLOW_EMBEDDER_URL", "")
            or ""
        ),
        "live_probe": False,
        "error": None if has_url and has_key else "API embedding mode requires an embedding URL and API key.",
    }


def _check_modal_embedding(config: IngestionConfig, settings: Any) -> dict[str, Any]:
    enabled = bool(getattr(settings, "MODAL_ENABLED", False))
    url = str(getattr(config, "embed_base_url", None) or getattr(settings, "MODAL_EMBEDDER_URL", "") or "")
    return {
        "ok": bool(enabled and url),
        "provider": "modal",
        "url": url,
        "live_probe": False,
        "error": None if enabled and url else "Modal embedding mode requires MODAL_ENABLED=true and a Modal embedder URL.",
    }


async def check_embedding_preflight(config: IngestionConfig) -> dict[str, Any]:
    settings = get_settings()
    mode = _mode(getattr(config, "embed_mode", "local"))
    if mode == "local":
        result = await _probe_local_embedder(settings)
    elif mode == "api":
        result = _check_api_embedding(config, settings)
    elif mode == "modal":
        result = _check_modal_embedding(config, settings)
    else:
        result = {
            "ok": False,
            "provider": mode,
            "url": None,
            "error": f"Unknown embedding mode: {mode}",
        }
    result["mode"] = mode
    result["dimension"] = getattr(config, "embedding_dimension", None)
    result["model_id"] = getattr(config, "embedding_model_id", None)
    return result


async def _check_model_entry(entry: Any) -> dict[str, Any]:
    model = str(_profile_value(entry, "model") or "").strip()
    base_url = str(_profile_value(entry, "base_url") or "").strip().rstrip("/")
    api_key = _profile_api_key(entry)
    result = {
        "ok": bool(model),
        "model": model,
        "base_url": base_url or None,
        "live_probe": False,
        "error": None if model else "Model entry is missing a model name.",
    }
    if not model or not base_url:
        return result
    if _is_local_vllm_entry(entry, base_url) and "/" not in model:
        result["ok"] = False
        result["error"] = (
            f"vllm-local model {model!r} must include the LiteLLM provider "
            f"prefix, e.g. 'openai/{model}'."
        )
        return result
    start = time.perf_counter()
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base_url}/models", headers=headers)
            response.raise_for_status()
        if _is_local_vllm_entry(entry, base_url):
            served_model = _vllm_served_model_name(model)
            try:
                payload = response.json()
                model_ids = {
                    str(item.get("id") or "")
                    for item in payload.get("data", [])
                    if isinstance(item, dict)
                }
            except Exception:
                model_ids = set()
            if model_ids and served_model not in model_ids:
                result.update(
                    {
                        "ok": False,
                        "live_probe": True,
                        "latency_ms": round(
                            (time.perf_counter() - start) * 1000.0, 2
                        ),
                        "error": (
                            f"vllm-local model {model!r} resolves to served name "
                            f"{served_model!r}, but /models returned {sorted(model_ids)!r}."
                        ),
                    }
                )
                return result
        result.update(
            {
                "ok": True,
                "live_probe": True,
                "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                "error": None,
            }
        )
    except Exception as exc:
        result.update(
            {
                "ok": False,
                "live_probe": True,
                "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                "error": str(exc),
            }
        )
    return result


async def check_llm_model_preflight(config: IngestionConfig) -> dict[str, Any]:
    engine = str(getattr(config, "graph_extraction_engine", "llm") or "llm")
    needs_summary = bool(getattr(config, "chunk_summarization", False))
    needs_extraction = bool(getattr(config, "use_neo4j", True) and engine == "llm")
    checks: dict[str, Any] = {}
    errors: list[str] = []
    warnings: list[str] = []

    async def _check_pool(role: str, entries: list[Any], *, required: bool) -> None:
        if not required:
            checks[role] = {
                "ok": True,
                "entries": [
                    {
                        "model": str(_profile_value(entry, "model") or "").strip(),
                        "base_url": str(_profile_value(entry, "base_url") or "").strip() or None,
                    }
                    for entry in entries
                ],
                "skipped": True,
                "error": None,
            }
            return
        if not entries:
            checks[role] = {"ok": not required, "entries": [], "error": None}
            checks[role]["error"] = f"{role} model pool is empty."
            errors.append(checks[role]["error"])
            return
        entry_results = [await _check_model_entry(entry) for entry in entries]
        ok = any(item.get("ok") for item in entry_results)
        pool_error = None
        if not ok:
            entry_errors = [
                f"{item.get('model') or '<missing model>'}: {item.get('error')}"
                for item in entry_results
                if item.get("error")
            ]
            detail = "; ".join(entry_errors)
            pool_error = f"{role} model pool has no reachable entries."
            if detail:
                pool_error = f"{pool_error} {detail}"
        checks[role] = {"ok": ok, "entries": entry_results, "error": pool_error}
        if not ok:
            if required:
                errors.append(checks[role]["error"])
            else:
                warnings.append(checks[role]["error"])

    await _check_pool(
        "summary",
        list(getattr(config, "summary_models", None) or []),
        required=needs_summary,
    )
    extraction_entries = (
        list(getattr(config, "summary_models", None) or [])
        if getattr(config, "models_linked", False)
        else list(getattr(config, "extraction_models", None) or [])
    )
    await _check_pool("extraction", extraction_entries, required=needs_extraction)
    await _check_pool(
        "repair",
        list(getattr(config, "extraction_repair_models", None) or []),
        required=False,
    )

    return {
        "ok": not errors,
        "required": {
            "summary": needs_summary,
            "extraction": needs_extraction,
        },
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
    }


async def check_qdrant_preflight(qdrant_client: Any) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        await qdrant_client.get_collections()
        return {
            "ok": True,
            "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
            "error": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
            "error": str(exc),
        }


def check_local_graph_preflight(config: IngestionConfig) -> dict[str, Any]:
    return {
        "ok": True,
        "engine": "llm" if getattr(config, "use_neo4j", True) else "disabled",
        "local_graph_required": False,
        "llm_graph_calls_enabled": bool(getattr(config, "use_neo4j", True)),
        "warnings": [],
        "error": None,
    }


async def run_ingest_preflight(
    *,
    config: IngestionConfig,
    qdrant_client: Any,
) -> dict[str, Any]:
    embedding = await check_embedding_preflight(config)
    qdrant = await check_qdrant_preflight(qdrant_client)
    graph = check_local_graph_preflight(config)
    llm_models = await check_llm_model_preflight(config)
    warnings = list(graph.get("warnings") or [])
    warnings.extend(llm_models.get("warnings") or [])
    if bool(graph.get("llm_graph_calls_enabled")):
        warnings.append("Graph extraction is routed through the configured LLM extraction pool.")
    errors: list[str] = []
    if not embedding.get("ok"):
        errors.append(f"embedding unavailable: {embedding.get('error')}")
    if not qdrant.get("ok"):
        errors.append(f"qdrant unavailable: {qdrant.get('error')}")
    if not graph.get("ok"):
        errors.append(f"graph extraction unavailable: {graph.get('error')}")
    if not llm_models.get("ok"):
        errors.extend(f"llm model unavailable: {error}" for error in llm_models.get("errors", []))
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "embedding": embedding,
        "qdrant": qdrant,
        "graph": graph,
        "llm_models": llm_models,
        "summary_llm_enabled": bool(getattr(config, "chunk_summarization", False)),
        "llm_graph_calls_enabled": bool(graph.get("llm_graph_calls_enabled")),
    }
