"""
Tests for the bare-model-name migration in IngestionService.

Covers the three storage locations patched by
`IngestionService.migrate_bare_model_names`:

  1. `corpora.default_ingestion_config.summary_models[] / extraction_models[]`
     — per-corpus ingestion pools (ModelProfileRef).
  2. `settings.models.query_model_pool[]` — per-user unified chat pool
     (QueryModelPoolEntry).
  3. `model_pool` collection — Phase E unified pool entries.

The migration must:
  • Add the LiteLLM provider prefix when an entry has a known provider_preset
    but a bare model string.
  • Leave already-prefixed entries untouched.
  • Leave unknown provider_preset entries untouched (defensive — user-authored).
  • Be idempotent — running twice produces no additional rewrites.

Uses an in-process fake Motor-like DB rather than the live Mongo container
so these tests run in any environment (CI, local, pre-push hook).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from services.ingestion_service import IngestionService


# ── Minimal async fake that mimics just the Motor methods the migration uses.


class _FakeCursor:
    def __init__(self, docs: list[dict], projection: dict | None):
        self._docs = docs
        self._projection = projection
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._docs):
            raise StopAsyncIteration
        doc = self._docs[self._idx]
        self._idx += 1
        # Projection is advisory for our purposes — we return the full doc
        # because the migration reads nested paths by dict lookup anyway.
        return doc


class _FakeCollection:
    def __init__(self, docs: list[dict] | None = None):
        self.docs: list[dict] = list(docs or [])

    def find(self, query: dict | None = None, projection: dict | None = None):
        # Simple {}-only support — the migration passes {} as the filter.
        assert not query, "FakeCollection.find only supports empty filters"
        return _FakeCursor(self.docs, projection)

    async def update_one(self, filter_: dict, update: dict):
        set_fields = update.get("$set", {})
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in filter_.items()):
                for path, value in set_fields.items():
                    _deep_set(doc, path, value)
                return type("UpdateResult", (), {"modified_count": 1})()
        return type("UpdateResult", (), {"modified_count": 0})()


def _deep_set(doc: dict, dotted_path: str, value: Any) -> None:
    parts = dotted_path.split(".")
    cur = doc
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


class _FakeDB:
    def __init__(self) -> None:
        self.corpora = _FakeCollection()
        self.settings = _FakeCollection()
        self.model_pool = _FakeCollection()

    def __getitem__(self, name: str) -> _FakeCollection:
        return getattr(self, name)


def _make_service(db: _FakeDB) -> IngestionService:
    svc = IngestionService.__new__(IngestionService)
    svc._db = db
    return svc


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deepseek_bare_model_gets_prefix():
    """provider_preset=deepseek, model=deepseek-chat → deepseek/deepseek-chat."""
    db = _FakeDB()
    db.corpora.docs.append(
        {
            "corpus_id": "c1",
            "name": "alpha",
            "default_ingestion_config": {
                "summary_models": [
                    {
                        "provider_preset": "deepseek",
                        "model": "deepseek-chat",
                        "base_url": "https://api.deepseek.com/v1",
                        "max_concurrent": 1,
                        "extra_params": {},
                    }
                ],
                "extraction_models": [],
            },
        }
    )
    svc = _make_service(db)

    result = await svc.migrate_bare_model_names()

    assert result["corpora_patched"] == 1
    assert result["pool_entries_patched"] == 1
    assert "c1" in result["corpus_ids"]
    patched = db.corpora.docs[0]["default_ingestion_config"]["summary_models"][0]
    assert patched["model"] == "deepseek/deepseek-chat"


@pytest.mark.asyncio
async def test_unknown_preset_preserved():
    """provider_preset='custom' (mapped to openai) — but we leave it alone
    when the user explicitly chose Custom to avoid clobbering hand-crafted
    strings. We *do* apply the prefix when the model is bare AND the preset
    id is registered, so 'custom' with a bare model picks up 'openai/'.

    Scope clarification: this test covers the ACTUAL unknown case — a
    preset id that is NOT in the registry (e.g. from a future UI version
    or manual Mongo edit). Migration must leave those alone.
    """
    db = _FakeDB()
    db.corpora.docs.append(
        {
            "corpus_id": "c-unknown",
            "default_ingestion_config": {
                "summary_models": [
                    {
                        "provider_preset": "some-future-provider",
                        "model": "some-model",
                    }
                ],
                "extraction_models": [],
            },
        }
    )
    svc = _make_service(db)
    result = await svc.migrate_bare_model_names()
    assert result["pool_entries_patched"] == 0
    assert (
        db.corpora.docs[0]["default_ingestion_config"]["summary_models"][0]["model"]
        == "some-model"
    )


@pytest.mark.asyncio
async def test_already_prefixed_preserved():
    """Idempotency — a model string that already contains '/' is left alone."""
    db = _FakeDB()
    db.corpora.docs.append(
        {
            "corpus_id": "c-prefixed",
            "default_ingestion_config": {
                "summary_models": [
                    {
                        "provider_preset": "deepseek",
                        "model": "deepseek/deepseek-reasoner",
                    }
                ],
                "extraction_models": [],
            },
        }
    )
    svc = _make_service(db)
    result = await svc.migrate_bare_model_names()
    assert result["pool_entries_patched"] == 0
    assert (
        db.corpora.docs[0]["default_ingestion_config"]["summary_models"][0]["model"]
        == "deepseek/deepseek-reasoner"
    )


@pytest.mark.asyncio
async def test_empty_preset_legacy_entry_preserved():
    """provider_preset='' (legacy write) with an already-prefixed model —
    migration must not touch it."""
    db = _FakeDB()
    db.corpora.docs.append(
        {
            "corpus_id": "c-legacy",
            "default_ingestion_config": {
                "summary_models": [
                    {"provider_preset": "", "model": "foo/bar"}
                ],
                "extraction_models": [],
            },
        }
    )
    svc = _make_service(db)
    result = await svc.migrate_bare_model_names()
    assert result["pool_entries_patched"] == 0
    assert (
        db.corpora.docs[0]["default_ingestion_config"]["summary_models"][0]["model"]
        == "foo/bar"
    )


@pytest.mark.asyncio
async def test_second_run_is_noop():
    """After a successful migration, a second call patches zero entries."""
    db = _FakeDB()
    db.corpora.docs.append(
        {
            "corpus_id": "c-idem",
            "default_ingestion_config": {
                "summary_models": [
                    {"provider_preset": "anthropic", "model": "claude-sonnet-4-6"}
                ],
                "extraction_models": [],
            },
        }
    )
    svc = _make_service(db)

    first = await svc.migrate_bare_model_names()
    assert first["pool_entries_patched"] == 1

    # Snapshot after first run to prove the second run doesn't re-touch it.
    after_first = copy.deepcopy(db.corpora.docs[0])

    second = await svc.migrate_bare_model_names()
    assert second["pool_entries_patched"] == 0
    assert second["corpora_patched"] == 0
    # Model string is stable on the rerun.
    assert (
        db.corpora.docs[0]["default_ingestion_config"]["summary_models"][0]["model"]
        == "anthropic/claude-sonnet-4-6"
    )
    # The only difference should be the `updated_at` timestamp from the
    # first run — no *further* mutation in the second. Compare model strings.
    assert (
        after_first["default_ingestion_config"]["summary_models"][0]["model"]
        == db.corpora.docs[0]["default_ingestion_config"]["summary_models"][0]["model"]
    )


@pytest.mark.asyncio
async def test_multiple_providers_in_one_corpus():
    """Sanity — summary + extraction pools with different presets all get
    the correct per-provider prefix."""
    db = _FakeDB()
    db.corpora.docs.append(
        {
            "corpus_id": "c-multi",
            "default_ingestion_config": {
                "summary_models": [
                    {"provider_preset": "openai", "model": "gpt-4o"},
                    {"provider_preset": "groq", "model": "llama-3.3-70b-versatile"},
                ],
                "extraction_models": [
                    {"provider_preset": "zai", "model": "glm-4-plus"},
                ],
            },
        }
    )
    svc = _make_service(db)
    result = await svc.migrate_bare_model_names()
    assert result["pool_entries_patched"] == 3
    cfg = db.corpora.docs[0]["default_ingestion_config"]
    assert cfg["summary_models"][0]["model"] == "openai/gpt-4o"
    assert cfg["summary_models"][1]["model"] == "groq/llama-3.3-70b-versatile"
    # Z.AI rides the openai litellm provider (custom base_url at runtime).
    assert cfg["extraction_models"][0]["model"] == "openai/glm-4-plus"


@pytest.mark.asyncio
async def test_settings_query_model_pool_patched():
    """settings.models.query_model_pool[] uses `provider` + `model_name`.
    Migration must rewrite model_name the same way."""
    db = _FakeDB()
    db.settings.docs.append(
        {
            "_id": "settings-doc-1",
            "user_id": "user-1",
            "models": {
                "query_model_pool": [
                    {
                        "entry_id": "e1",
                        "provider": "deepseek",
                        "model_name": "deepseek-chat",
                        "label": "DS",
                    },
                    {
                        "entry_id": "e2",
                        "provider": "openai",
                        "model_name": "openai/gpt-4o",
                        "label": "GPT",
                    },
                ],
            },
        }
    )
    svc = _make_service(db)
    result = await svc.migrate_bare_model_names()
    assert result["settings_users_patched"] == 1
    pool = db.settings.docs[0]["models"]["query_model_pool"]
    assert pool[0]["model_name"] == "deepseek/deepseek-chat"
    # Already-prefixed entry untouched.
    assert pool[1]["model_name"] == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_model_pool_collection_patched():
    """Phase E model_pool collection — same rewrite logic."""
    db = _FakeDB()
    db.model_pool.docs.append(
        {
            "_id": "mp-1",
            "entry_id": "e-deepseek",
            "user_id": "user-1",
            "provider": "deepseek",
            "model_name": "deepseek-chat",
        }
    )
    db.model_pool.docs.append(
        {
            "_id": "mp-2",
            "entry_id": "e-custom",
            "user_id": "user-1",
            "provider": "totally-unknown",
            "model_name": "some-model",
        }
    )
    svc = _make_service(db)
    result = await svc.migrate_bare_model_names()
    assert result["model_pool_entries_patched"] == 1
    assert db.model_pool.docs[0]["model_name"] == "deepseek/deepseek-chat"
    # Unknown provider untouched.
    assert db.model_pool.docs[1]["model_name"] == "some-model"


@pytest.mark.asyncio
async def test_empty_db_noop():
    """No corpora, no settings, no pool → migration returns zeros."""
    db = _FakeDB()
    svc = _make_service(db)
    result = await svc.migrate_bare_model_names()
    assert result == {
        "corpora_patched": 0,
        "pool_entries_patched": 0,
        "corpus_ids": [],
        "settings_users_patched": 0,
        "model_pool_entries_patched": 0,
    }
