from datetime import datetime

import pytest

from services.ingestion.provider_canary_cache import (
    load_cached_canary,
    provider_canary_fingerprint,
    record_canary,
)


class _Collection:
    def __init__(self):
        self.rows = {}

    async def find_one(self, query, _projection=None):
        return self.rows.get(query["_id"])

    async def update_one(self, query, update, **_kwargs):
        self.rows[query["_id"]] = {"_id": query["_id"], **update["$set"]}


@pytest.mark.asyncio
async def test_canary_cache_reuses_credential_across_pool_roles_without_storing_key():
    collection = _Collection()
    db = {"provider_canary_cache": collection}
    entry = {
        "provider_preset": "longcat",
        "model": "openai/LongCat-2.0",
        "base_url": "https://api.longcat.chat/openai/v1",
        "api_key": "credential-must-not-be-stored",
    }

    await record_canary(
        db,
        entry=entry,
        ok=True,
        status=200,
        latency_ms=123,
        now=datetime(2026, 1, 1),
    )
    cached = await load_cached_canary(
        db,
        entry={**entry, "pool_role": "summary"},
        now=datetime(2026, 1, 1),
    )

    assert cached["ok"] is True
    assert cached["cached"] is True
    assert "credential-must-not-be-stored" not in str(collection.rows)


def test_canary_fingerprint_changes_with_credential_and_never_contains_it():
    base = {
        "provider_preset": "longcat",
        "model": "openai/LongCat-2.0",
        "base_url": "https://api.longcat.chat/openai/v1",
    }
    first = provider_canary_fingerprint({**base, "api_key": "first-secret"})
    second = provider_canary_fingerprint({**base, "api_key": "second-secret"})

    assert first != second
    assert "first-secret" not in first
