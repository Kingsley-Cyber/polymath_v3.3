"""Batch options must not open cloud enrichment without cost authority."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.ingestion import batches as batch_mod


@pytest.mark.asyncio
async def test_create_local_batch_omits_cost_run_without_authority(tmp_path: Path, monkeypatch):
    src = tmp_path / "doc.md"
    src.write_text("# hello\n\nbody text for ingest batch gate.\n", encoding="utf-8")
    monkeypatch.setattr(
        batch_mod,
        "discover_local_files",
        lambda *_a, **_k: (tmp_path, [src]),
    )

    inserted: dict = {}

    class _Batches:
        async def insert_one(self, doc):
            inserted["batch"] = doc
            return SimpleNamespace(inserted_id="x")

    class _Items:
        async def insert_many(self, docs, **_kwargs):
            inserted["items"] = docs
            return SimpleNamespace(inserted_ids=["a"])

    db = MagicMock()
    db.__getitem__.side_effect = lambda name: (
        _Batches() if name == batch_mod.BATCHES else _Items()
    )

    async def _refresh(_db, batch_id, user_id=None):
        doc = dict(inserted["batch"])
        doc.pop("_id", None)
        return doc

    monkeypatch.setattr(batch_mod, "refresh_batch_counts", _refresh)

    batch = await batch_mod.create_local_batch(
        db=db,
        corpus_id="corpus-det",
        user_id="user-1",
        root_path=str(tmp_path),
        recursive=False,
        extensions=[".md"],
        store_files=False,
        use_neo4j=True,
        chunk_summarization=True,
        profile="mac_safe",
        summary_cost_authority_usd=None,
    )

    opts = batch["options"]
    assert opts["chunk_summarization"] is True
    assert opts["summary_cost_run_id"] is None
    assert opts["summary_cost_authority_usd"] is None
    assert batch["batch_id"]


@pytest.mark.asyncio
async def test_create_local_batch_sets_cost_run_when_authority_present(tmp_path: Path, monkeypatch):
    src = tmp_path / "doc.md"
    src.write_text("# hello\n\nbody\n", encoding="utf-8")
    monkeypatch.setattr(
        batch_mod,
        "discover_local_files",
        lambda *_a, **_k: (tmp_path, [src]),
    )

    inserted: dict = {}

    class _Batches:
        async def insert_one(self, doc):
            inserted["batch"] = doc
            return SimpleNamespace(inserted_id="x")

    class _Items:
        async def insert_many(self, docs, **_kwargs):
            return SimpleNamespace(inserted_ids=["a"])

    db = MagicMock()
    db.__getitem__.side_effect = lambda name: (
        _Batches() if name == batch_mod.BATCHES else _Items()
    )

    async def _refresh(_db, batch_id, user_id=None):
        doc = dict(inserted["batch"])
        doc.pop("_id", None)
        return doc

    monkeypatch.setattr(batch_mod, "refresh_batch_counts", _refresh)

    batch = await batch_mod.create_local_batch(
        db=db,
        corpus_id="corpus-enrich",
        user_id="user-1",
        root_path=str(tmp_path),
        recursive=False,
        extensions=[".md"],
        store_files=False,
        chunk_summarization=True,
        profile="mac_safe",
        summary_cost_authority_usd="5.00",
    )

    opts = batch["options"]
    assert opts["summary_cost_run_id"] == batch["batch_id"]
    assert opts["summary_cost_authority_usd"] == "5.00"
