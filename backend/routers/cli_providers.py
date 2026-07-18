"""CLI-subscription provider management — T4 in-app setup.

Proxies the host CLI shim (:8090) so Settings can show install/auth state,
spawn login flows, and one-click sync every model each CLI offers into the
user's query_model_pool. The shim owns all host-side truth; this router
only forwards and registers.
"""
import logging
import os
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from routers.auth import get_current_user
from services.settings import settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cli-providers", tags=["cli-providers"])

SHIM_URL = os.environ.get("CLI_SHIM_URL", "http://host.docker.internal:8090")
SHIM_BASE_URL_FOR_ENTRIES = os.environ.get(
    "CLI_SHIM_ENTRY_BASE_URL", "http://host.docker.internal:8090/v1"
)
PRETTY = {
    "chatgpt-cli": "ChatGPT CLI",
    "cursor-cli": "Cursor",
    "antigravity-cli": "Antigravity",
}


def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


@router.get("/status")
async def cli_providers_status(current_user: dict = Depends(get_current_user)):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{SHIM_URL}/providers/status")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"cli shim unreachable: {exc}"
        ) from exc


@router.post("/{name}/login")
async def cli_provider_login(
    name: str, current_user: dict = Depends(get_current_user)
):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{SHIM_URL}/providers/{name}/login")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"cli shim unreachable: {exc}"
        ) from exc


@router.post("/sync")
async def cli_providers_sync(current_user: dict = Depends(get_current_user)):
    """Pull every model each installed+authed CLI offers; register missing
    pool entries for the current user. Idempotent — existing entry_ids are
    left untouched (labels included)."""
    user_id = str(current_user["user_id"])
    discovered: list[tuple[str, str, str]] = []  # (provider, model, label)
    per_provider: dict[str, Any] = {}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            status = (await client.get(f"{SHIM_URL}/providers/status")).json()
            for name, meta in (status.get("providers") or {}).items():
                if not meta.get("installed"):
                    per_provider[name] = {"skipped": "not installed"}
                    continue
                if meta.get("auth") == "login_required":
                    per_provider[name] = {"skipped": "login required"}
                    continue
                resp = await client.get(f"{SHIM_URL}/providers/{name}/models")
                data = resp.json() if resp.status_code == 200 else {}
                models = data.get("models") or []
                for m in models:
                    if isinstance(m, dict):
                        discovered.append(
                            (name, m["id"], m.get("label") or m["id"])
                        )
                    else:
                        discovered.append((name, m, m))
                per_provider[name] = {
                    "models": len(models),
                    "source": data.get("source"),
                }
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"cli shim unreachable: {exc}"
        ) from exc

    raw = await settings_service.get_models_raw(user_id)
    pool = list(raw.get("query_model_pool") or [])
    have = {e.get("entry_id") for e in pool}
    for e in pool:
        if e.get("api_key_ciphertext"):
            e["api_key_ciphertext"] = "[set]"

    added: list[str] = []
    for provider, model, label in discovered:
        entry_id = f"cli-shim__{provider}-{_slug(model)}"
        if entry_id in have:
            continue
        pool.append(
            {
                "entry_id": entry_id,
                "label": f"{PRETTY.get(provider, provider)} — {label}",
                "provider": "custom",
                "base_url": SHIM_BASE_URL_FOR_ENTRIES,
                "api_key_ciphertext": "cli-shim-local",
                "model_name": f"{provider}:{model}",
                "source": "cloud",
                "enabled": True,
            }
        )
        have.add(entry_id)
        added.append(entry_id)

    if added:
        patch = dict(raw)
        patch["query_model_pool"] = pool
        await settings_service.update_models(user_id, patch)

    logger.info(
        "cli-providers sync: user=%s discovered=%d added=%d",
        user_id,
        len(discovered),
        len(added),
    )
    return {
        "discovered": len(discovered),
        "added": len(added),
        "added_entry_ids": added,
        "providers": per_provider,
    }
