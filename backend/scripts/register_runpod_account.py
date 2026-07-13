#!/usr/bin/env python3
"""Register or update one Runpod account for multi-account burst routing.

P2.7c operator helper. Runs INSIDE the backend container (motor + config
``get_settings`` pattern, same as ``polymath_summary_backfill_scoped.py``):

    docker exec -e RUNPOD_ACCOUNT_KEY=... polymath_v33-backend-1 \\
        python scripts/register_runpod_account.py --name acct2 --endpoint-id ep-xyz

Security house rules:
  - The API key is read ONLY from the ``RUNPOD_ACCOUNT_KEY`` environment
    variable. There is no argv or stdin key path, and the key is never
    printed, logged, or stored in plaintext: it is Fernet-encrypted via
    ``services.secrets.encrypt`` into ``api_keys.runpod_accounts.<name>``.
  - The printed result is the account list WITHOUT keys (only which account
    names have a stored ciphertext).

The account row itself is upserted (replace-by-name) into the additive
``ingestion.runpod_flash.accounts`` list; the legacy single
``endpoint_id`` + ``api_keys.runpod`` pair is left untouched and keeps
working as the implicit "default" account.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
BACKEND_ROOT = HERE.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from models.schemas import RunpodFlashAccount

# Account names become Mongo field keys under ``api_keys.runpod_accounts.``
# so dots/dollar signs (operators) and whitespace are refused outright.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,60}$")


def _mongo_db() -> tuple[AsyncIOMotorClient, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = client.get_default_database()
    except Exception:  # noqa: BLE001 - URI without default database
        db = client[settings.MONGODB_DATABASE]
    return client, db


async def _settings_doc(db: Any) -> dict[str, Any] | None:
    """Locate the system settings document (single-admin pattern)."""
    for query in (
        {
            "$or": [
                {"ingestion.runpod_flash": {"$exists": True}},
                {"api_keys.runpod": {"$exists": True}},
            ]
        },
        {"ingestion": {"$exists": True}},
        {},
    ):
        doc = await db["settings"].find_one(query)
        if doc:
            return doc
    return None


async def run(args: argparse.Namespace) -> dict[str, Any]:
    api_key = os.environ.get("RUNPOD_ACCOUNT_KEY") or ""
    if not api_key.strip():
        raise SystemExit(
            "RUNPOD_ACCOUNT_KEY is not set. Export the account API key in the "
            "environment (never pass it as an argument): "
            "docker exec -e RUNPOD_ACCOUNT_KEY=... <container> python "
            "scripts/register_runpod_account.py ..."
        )
    if not _NAME_PATTERN.match(args.name):
        raise SystemExit(
            "--name must match [A-Za-z0-9_-]{1,60}: it becomes the key-store "
            "field api_keys.runpod_accounts.<name>"
        )

    from services.secrets import encrypt

    client, db = _mongo_db()
    try:
        doc = await _settings_doc(db)
        if not doc:
            raise SystemExit(
                "No settings document found in Mongo; configure Settings once "
                "via the app before registering Runpod accounts."
            )
        raw_accounts = list(
            (((doc.get("ingestion") or {}).get("runpod_flash") or {}).get("accounts"))
            or []
        )
        # --embed-endpoint-id omitted (None) preserves the stored value on a
        # replace-by-name upsert; pass an explicit empty string to clear it.
        existing_embed_endpoint_id = ""
        for row in raw_accounts:
            if isinstance(row, dict) and str(row.get("name") or "") == args.name:
                existing_embed_endpoint_id = str(row.get("embed_endpoint_id") or "")
                break
        account = RunpodFlashAccount(
            name=args.name,
            endpoint_id=args.endpoint_id,
            embed_endpoint_id=(
                args.embed_endpoint_id
                if args.embed_endpoint_id is not None
                else existing_embed_endpoint_id
            ),
            enabled=not args.disable,
            max_workers=args.max_workers,
            request_concurrency=args.request_concurrency,
            weight=args.weight,
        )
        replaced = False
        for index, row in enumerate(raw_accounts):
            if isinstance(row, dict) and str(row.get("name") or "") == account.name:
                raw_accounts[index] = account.model_dump()
                replaced = True
                break
        if not replaced:
            raw_accounts.append(account.model_dump())

        await db["settings"].update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "ingestion.runpod_flash.accounts": raw_accounts,
                    f"api_keys.runpod_accounts.{account.name}": encrypt(
                        api_key.strip()
                    ),
                }
            },
        )

        refreshed = await db["settings"].find_one({"_id": doc["_id"]})
        stored_accounts = (
            ((refreshed or {}).get("ingestion") or {}).get("runpod_flash") or {}
        ).get("accounts") or []
        stored_key_names = sorted(
            (((refreshed or {}).get("api_keys") or {}).get("runpod_accounts") or {})
        )
        return {
            "action": "replaced" if replaced else "added",
            "account": account.name,
            "accounts": [
                RunpodFlashAccount(**row).model_dump()
                for row in stored_accounts
                if isinstance(row, dict)
            ],
            "encrypted_keys_stored_for": stored_key_names,
            "legacy_default_key_present": bool(
                ((refreshed or {}).get("api_keys") or {}).get("runpod")
            ),
        }
    finally:
        client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--name", required=True, help="Account name (unique key).")
    parser.add_argument(
        "--endpoint-id", required=True, help="Runpod serverless endpoint id."
    )
    parser.add_argument(
        "--embed-endpoint-id",
        default=None,
        help=(
            "Optional Runpod endpoint id for the burst EMBEDDING worker "
            "(runpod_flash_embedder; embed mode 'runpod'). Omitted: any "
            "stored value is preserved on replace. Pass '' to clear."
        ),
    )
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--request-concurrency", type=int, default=8)
    parser.add_argument("--weight", type=float, default=1.0)
    parser.add_argument(
        "--disable",
        action="store_true",
        help="Register the account disabled (excluded from routing).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(parse_args())), indent=2, sort_keys=True))
