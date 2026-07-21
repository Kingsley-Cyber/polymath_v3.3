"""Resolve the production summary-provider pool without exposing secrets.

DeepSeek V4 Flash is the owner-pinned primary summary lane. Hy3 remains
ineligible until an operator records a successful three-row summary canary on
that provider entry. The returned report is intentionally secret-free and can
be persisted in batch/job receipts.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

import httpx

from config import get_settings


FLASH_MODEL_MARKER = "deepseek-v4-flash"
HY3_MODEL_MARKERS = ("tencent/hy3", "/hy3", "hy3-preview")
HY3_CANARY_REQUIRED_ROWS = 3


def _entry_dict(ref: Any) -> dict[str, Any]:
    if hasattr(ref, "model_dump"):
        return ref.model_dump()
    return dict(ref or {}) if isinstance(ref, dict) else {}


def _model(entry: dict[str, Any]) -> str:
    return str(entry.get("model") or entry.get("model_name") or "").strip()


def _is_flash(entry: dict[str, Any]) -> bool:
    return FLASH_MODEL_MARKER in _model(entry).lower()


def _is_hy3(entry: dict[str, Any]) -> bool:
    model = _model(entry).lower()
    return any(marker in model for marker in HY3_MODEL_MARKERS)


def _hy3_canary_rows(entry: dict[str, Any]) -> int:
    extra = entry.get("extra_params") or {}
    raw = extra.get("summary_canary_passed_rows")
    if raw is None:
        raw = entry.get("summary_canary_passed_rows")
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _signature(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("provider_preset") or "").strip().lower(),
        str(entry.get("base_url") or "").strip().rstrip("/").lower(),
        _model(entry).lower(),
    )


async def _probe_openai_compatible_key(
    *,
    api_base: str,
    api_key: str,
) -> tuple[bool, str | None]:
    base = str(api_base or "").strip().rstrip("/")
    if not base:
        return False, "missing_api_base"
    url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except Exception as exc:  # noqa: BLE001 - surfaced as secret-free status
        return False, type(exc).__name__
    if 200 <= int(resp.status_code) < 300:
        return True, None
    return False, f"http_{int(resp.status_code)}"


def prepare_summary_provider_pool(
    refs: Iterable[Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter, deduplicate, and priority-order a plaintext provider pool."""

    admitted: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    disabled: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for ref in refs:
        entry = _entry_dict(ref)
        if entry.get("enabled") is False:
            disabled.append(entry)
            continue
        if not _model(entry):
            continue
        if _is_hy3(entry) and _hy3_canary_rows(entry) < HY3_CANARY_REQUIRED_ROWS:
            demoted.append(entry)
            continue
        signature = _signature(entry)
        if signature in seen:
            continue
        seen.add(signature)
        admitted.append(entry)

    admitted.sort(key=lambda entry: (0 if _is_flash(entry) else 1))
    report = {
        "primary_model": _model(admitted[0]) if admitted else None,
        "admitted_provider_count": len(admitted),
        "admitted_models": [_model(entry) for entry in admitted],
        "demoted_provider_count": len(demoted),
        "demoted_models": [_model(entry) for entry in demoted],
        "disabled_provider_count": len(disabled),
        "disabled_models": [_model(entry) for entry in disabled],
        "hy3_canary_required_rows": HY3_CANARY_REQUIRED_ROWS,
    }
    return admitted, report


async def resolve_summary_provider_pool(
    *,
    configured_refs: Iterable[Any] | None,
    runtime_refs: Iterable[Any] | None = None,
    user_id: str | None = None,
    db: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve a credential-complete pool with DeepSeek Flash pinned first.

    Shared keys are decrypted only at dispatch time. They are never included
    in the returned report or persisted into corpus configuration.
    """

    from services.secrets import decrypt
    from services.settings import settings_service

    configured_list = list(configured_refs or [])
    runtime_list = list(runtime_refs or [])
    entries = [
        _entry_dict(ref)
        for ref in [*configured_list, *runtime_list]
    ]

    def _plaintext(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("gAAAAA"):
            return decrypt(value)
        return value

    for entry in entries:
        for field in ("api_key", "lifecycle_api_key"):
            if entry.get(field):
                entry[field] = _plaintext(entry[field])

    shared_key = None
    if user_id:
        try:
            keys = await settings_service.get_plaintext_keys_for_llm(user_id)
            shared_key = keys.get("deepseek")
        except Exception:
            shared_key = None
    if not shared_key:
        try:
            shared_key = await settings_service.get_plaintext_key_any_user("deepseek")
        except Exception:
            shared_key = None
    if not shared_key and db is not None:
        try:
            row = await db["settings"].find_one(
                {"api_keys.deepseek": {"$exists": True, "$ne": ""}},
                {"_id": 0, "api_keys.deepseek": 1},
            )
            ciphertext = ((row or {}).get("api_keys") or {}).get("deepseek")
            shared_key = decrypt(ciphertext) if ciphertext else None
        except Exception:
            # SettingsService is authoritative in the normal app lifecycle;
            # lightweight tests/adapters may not expose a settings collection.
            shared_key = None

    configured_enabled_entries = [
        entry for entry in entries if entry.get("enabled") is not False and _model(entry)
    ]
    flash_entries = [
        entry
        for entry in entries
        if entry.get("enabled") is not False and _is_flash(entry)
    ]
    shared_flash_key_probe_ok = None
    shared_flash_key_error = None
    needs_shared_flash_key = bool(shared_key) and (
        (not configured_enabled_entries and not flash_entries)
        or any(not entry.get("api_key") for entry in flash_entries)
    )
    if needs_shared_flash_key:
        shared_flash_key_probe_ok, shared_flash_key_error = (
            await _probe_openai_compatible_key(
                api_base="https://api.deepseek.com/v1",
                api_key=shared_key,
            )
        )

    if not flash_entries and not configured_enabled_entries:
        default_model = str(
            getattr(get_settings(), "GHOST_A_DEFAULT_MODEL", "")
            or "deepseek/deepseek-v4-flash"
        )
        if FLASH_MODEL_MARKER not in default_model.lower():
            default_model = "deepseek/deepseek-v4-flash"
        if shared_key and shared_flash_key_probe_ok:
            entries.insert(
                0,
                {
                    "provider_preset": "deepseek",
                    "model": default_model,
                    "base_url": "https://api.deepseek.com",
                    "api_key": shared_key,
                    "max_concurrent": max(
                        1,
                        int(getattr(get_settings(), "SUMMARY_MAX_CONCURRENT", 1) or 1),
                    ),
                    "extra_params": {"disable_thinking": True},
                },
            )
    elif shared_key and shared_flash_key_probe_ok:
        for entry in flash_entries:
            if not entry.get("api_key"):
                entry["api_key"] = shared_key

    # Provider-agnostic key attachment (owner order 2026-07-19): any entry
    # still missing a key resolves its provider_preset against the shared
    # encrypted key store, so future summary providers (longcat, siliconflow,
    # moonshot, …) wire in from corpus config alone — no code change, no
    # plaintext keys in corpus documents.
    presets_missing = {
        str(entry.get("provider_preset") or "").strip().lower()
        for entry in entries
        if not entry.get("api_key")
    }
    presets_missing.discard("")
    if presets_missing:
        provider_keys: dict[str, str] = {}
        if user_id:
            try:
                provider_keys = dict(
                    await settings_service.get_plaintext_keys_for_llm(user_id) or {}
                )
            except Exception:
                provider_keys = {}
        for preset in sorted(presets_missing):
            if not provider_keys.get(preset):
                try:
                    any_key = await settings_service.get_plaintext_key_any_user(
                        preset
                    )
                except Exception:
                    any_key = None
                if any_key:
                    provider_keys[preset] = any_key
        for entry in entries:
            if not entry.get("api_key"):
                preset = str(entry.get("provider_preset") or "").strip().lower()
                if provider_keys.get(preset):
                    entry["api_key"] = provider_keys[preset]

    pool, report = prepare_summary_provider_pool(entries)
    report["flash_primary"] = bool(pool and _is_flash(pool[0]))
    report["flash_key_available"] = bool(
        pool and _is_flash(pool[0]) and pool[0].get("api_key")
    )
    if needs_shared_flash_key:
        report["shared_flash_key_probe_ok"] = bool(shared_flash_key_probe_ok)
        report["shared_flash_key_error"] = shared_flash_key_error
        report["shared_flash_key_fingerprint"] = hashlib.sha256(
            shared_key.encode("utf-8")
        ).hexdigest()[:12]
    report["configured_provider_count"] = len(configured_list)
    report["runtime_provider_count"] = len(runtime_list)
    return pool, report
