"""Permanent integration fixture contract (owner directive 2026-08-03).

One retained canary corpus carries a `fixture` block:

    purpose: permanent_integration_fixture
    production_visible: false
    excluded_from_user_search: true

The flag is consumed by IngestionService.list_corpora — fixture corpora never
appear in user-facing lists, while direct access by corpus_id stays intact.
Fail-open: a missing/malformed fixture block never hides a corpus.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from services.ingestion_service import (
    IngestionService,
    _corpus_hidden_from_user_search,
)

BACKEND = Path(__file__).resolve().parents[1]

FIXTURE_DOC = {
    "corpus_id": "fixture-corpus",
    "name": "Relex Canary Book Corpus v2",
    "default_ingestion_config": {},
    "fixture": {
        "purpose": "permanent_integration_fixture",
        "production_visible": False,
        "excluded_from_user_search": True,
    },
}
NORMAL_DOC = {
    "corpus_id": "normal-corpus",
    "name": "ordinary corpus",
    "default_ingestion_config": {},
}


def test_hidden_flag_requires_explicit_true():
    assert _corpus_hidden_from_user_search(FIXTURE_DOC) is True
    assert _corpus_hidden_from_user_search(NORMAL_DOC) is False
    # Fail-open semantics: malformed or partial blocks never hide corpora.
    assert _corpus_hidden_from_user_search({"fixture": "oops"}) is False
    assert (
        _corpus_hidden_from_user_search(
            {"fixture": {"excluded_from_user_search": False}}
        )
        is False
    )
    assert _corpus_hidden_from_user_search({}) is False
    assert _corpus_hidden_from_user_search(None) is False  # type: ignore[arg-type]


def test_list_corpora_excludes_fixture_but_keeps_others():
    inst = IngestionService.__new__(IngestionService)  # no DB needed
    inst._db = None  # __new__ skips __init__; list_corpora passes this through

    async def fake_refresh(docs, refresh_readiness=False):
        return docs

    inst._refresh_corpus_counts = fake_refresh  # type: ignore[method-assign]

    with patch(
        "services.storage.mongo_reader.list_corpora",
        new=lambda db, user_id=None: _fake_docs(),
    ):
        docs = asyncio.run(inst.list_corpora(user_id="u1"))
    ids = [d["corpus_id"] for d in docs]
    assert "fixture-corpus" not in ids
    assert "normal-corpus" in ids


async def _fake_docs():
    return [dict(FIXTURE_DOC), dict(NORMAL_DOC)]


def test_marking_script_writes_owner_mandated_block():
    src = (
        BACKEND / "scripts/mark_permanent_integration_fixture.py"
    ).read_text(encoding="utf-8")
    assert '"purpose": "permanent_integration_fixture"' in src
    assert '"production_visible": False' in src
    assert '"excluded_from_user_search": True' in src
    # Dry-run by default — no writes without --apply.
    assert 'ap.add_argument("--apply", action="store_true"' in src


def test_get_corpus_path_is_not_filtered():
    """Direct access by corpus_id must remain possible for fixtures (eval
    scripts, retrieval, re-ingest probes). Only list_corpora filters."""
    src = (BACKEND / "services/ingestion_service.py").read_text(encoding="utf-8")
    get_corpus_block = src.split("async def get_corpus", 1)[1].split(
        "async def", 1
    )[0]
    assert "_corpus_hidden_from_user_search" not in get_corpus_block
