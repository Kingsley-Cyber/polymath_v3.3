"""Preflight checks for ingestion admission.

These checks run before parse/chunk/Ghost A work so a bad embedding route does
not leave large documents stuck as Mongo-only after spending summary tokens.
They are intentionally shallow: API embedding providers are configuration
checked without sending paid embedding requests, while local services are
health-probed on the Docker network.
"""

from __future__ import annotations

import importlib.util
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
    engine = str(getattr(config, "graph_extraction_engine", "local_gliner") or "local_gliner")
    if engine == "local_gliner":
        engine = "local_gliner_relex"
    graph_enabled = bool(getattr(config, "use_neo4j", True))
    if not graph_enabled or engine == "llm":
        return {
            "ok": True,
            "engine": engine,
            "local_graph_required": False,
            "llm_graph_calls_enabled": bool(engine == "llm"),
            "error": None,
        }
    if not bool(getattr(config, "local_graph_extraction_enabled", True)):
        return {
            "ok": False,
            "engine": engine,
            "local_graph_required": True,
            "llm_graph_calls_enabled": bool(getattr(config, "llm_fallback_enabled", False)),
            "error": "Graph extraction is enabled but local_graph_extraction_enabled=false.",
        }
    if engine == "local_glirel_optional":
        return {
            "ok": False,
            "engine": engine,
            "local_graph_required": True,
            "model": None,
            "llm_graph_calls_enabled": bool(getattr(config, "llm_fallback_enabled", False)),
            "error": "GLiREL is evaluation-only and not implemented as a default production lane.",
        }
    dependency_name = "gliner2" if engine == "local_gliner2" else "gliner"
    model_name = (
        getattr(config, "local_gliner2_model", None)
        if engine == "local_gliner2"
        else getattr(config, "local_extractor_model", None)
    )
    if importlib.util.find_spec(dependency_name) is None:
        return {
            "ok": False,
            "engine": engine,
            "local_graph_required": True,
            "model": model_name,
            "llm_graph_calls_enabled": bool(getattr(config, "llm_fallback_enabled", False)),
            "error": f"{dependency_name} dependency is not installed in the backend runtime.",
        }
    warnings: list[str] = []
    cuda_available = None
    try:
        import torch  # type: ignore

        cuda_available = bool(torch.cuda.is_available())
        if not cuda_available:
            warnings.append("CUDA is unavailable; local GLiNER will use CPU and be much slower.")
    except Exception as exc:
        warnings.append(f"Could not inspect CUDA availability: {exc}")
    return {
        "ok": True,
        "engine": engine,
        "local_graph_required": True,
        "model": model_name,
        "cuda_available": cuda_available,
        "llm_graph_calls_enabled": bool(getattr(config, "llm_fallback_enabled", False)),
        "warnings": warnings,
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
    warnings = list(graph.get("warnings") or [])
    if bool(graph.get("llm_graph_calls_enabled")):
        warnings.append("Graph LLM fallback is enabled for this config.")
    errors: list[str] = []
    if not embedding.get("ok"):
        errors.append(f"embedding unavailable: {embedding.get('error')}")
    if not qdrant.get("ok"):
        errors.append(f"qdrant unavailable: {qdrant.get('error')}")
    if not graph.get("ok"):
        errors.append(f"local graph unavailable: {graph.get('error')}")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "embedding": embedding,
        "qdrant": qdrant,
        "graph": graph,
        "summary_llm_enabled": bool(getattr(config, "chunk_summarization", False)),
        "llm_graph_calls_enabled": bool(graph.get("llm_graph_calls_enabled")),
    }
