"""Resolve the production summary-provider pool without exposing secrets.

DeepSeek V4 Flash is the owner-pinned primary summary lane. Hy3 remains
ineligible until an operator records a successful three-row summary canary on
that provider entry. The returned report is intentionally secret-free and can
be persisted in batch/job receipts.
"""

from __future__ import annotations

from typing import Any, Iterable

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


def prepare_summary_provider_pool(
    refs: Iterable[Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter, deduplicate, and priority-order a plaintext provider pool."""

    admitted: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for ref in refs:
        entry = _entry_dict(ref)
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

    flash_entries = [entry for entry in entries if _is_flash(entry)]
    if not flash_entries:
        default_model = str(
            getattr(get_settings(), "GHOST_A_DEFAULT_MODEL", "")
            or "deepseek/deepseek-v4-flash"
        )
        if FLASH_MODEL_MARKER not in default_model.lower():
            default_model = "deepseek/deepseek-v4-flash"
        entries.insert(
            0,
            {
                "provider_preset": "deepseek",
                "model": default_model,
                "base_url": "https://api.deepseek.com",
                "api_key": shared_key,
                "max_concurrent": max(
                    1, int(getattr(get_settings(), "SUMMARY_MAX_CONCURRENT", 1) or 1)
                ),
                "extra_params": {"disable_thinking": True},
            },
        )
    elif shared_key:
        for entry in flash_entries:
            if not entry.get("api_key"):
                entry["api_key"] = shared_key

    pool, report = prepare_summary_provider_pool(entries)
    report["flash_primary"] = bool(pool and _is_flash(pool[0]))
    report["flash_key_available"] = bool(
        pool and _is_flash(pool[0]) and pool[0].get("api_key")
    )
    report["configured_provider_count"] = len(configured_list)
    report["runtime_provider_count"] = len(runtime_list)
    return pool, report
