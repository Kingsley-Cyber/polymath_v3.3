"""Operational B2 materializer boundaries that do not require live services."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.materialize_semantic_digest_claim_inputs import (
    MaterializationError,
    _database,
    _packet_exclusion_ledger_entry,
    _persist_before_census,
    _quantiles,
    _require_tmp_path,
    _route_prices,
)


def test_packet_exclusion_ledger_records_parent_and_document_identity() -> None:
    row_with_claim = SimpleNamespace(
        envelope=SimpleNamespace(body=SimpleNamespace(claims=[object()]))
    )
    row_without_claim = SimpleNamespace(
        envelope=SimpleNamespace(body=SimpleNamespace(claims=[]))
    )

    entry = _packet_exclusion_ledger_entry(
        parent={
            "parent_id": "parent:test",
            "doc_id": "doc:test",
            "child_ids": ["child:b", "child:a"],
        },
        documents={
            "doc:test": {
                "doc_id": "doc:test",
                "source_identity": {"content_sha256": "a" * 64},
            }
        },
        rows_by_child={
            "child:a": row_with_claim,
            "child:b": row_without_claim,
        },
        reason="source_child_without_atomic_claim",
    )

    assert entry["reason"] == "source_child_without_atomic_claim"
    assert entry["parent_id"] == "parent:test"
    assert entry["document_id"] == "doc:test"
    assert entry["document_source_version_id"].startswith("srcv:")
    assert entry["source_child_ids"] == ["child:a", "child:b"]
    assert entry["source_child_without_atomic_claim_ids"] == ["child:b"]


def test_before_census_is_atomically_persisted_under_tmp() -> None:
    output = Path("/tmp") / f"b2-before-census-test-{os.getpid()}.json"
    partial = output.with_suffix(output.suffix + ".partial")
    output.unlink(missing_ok=True)
    partial.unlink(missing_ok=True)
    try:
        metadata = _persist_before_census(
            output,
            {"canonical_writes": 0, "writes_before_receipt_persisted": 0},
        )

        assert json.loads(output.read_text(encoding="utf-8")) == {
            "canonical_writes": 0,
            "writes_before_receipt_persisted": 0,
        }
        assert metadata["file_bytes"] == output.stat().st_size
        assert len(metadata["file_sha256"]) == 64
        assert not partial.exists()
    finally:
        output.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)


def test_database_client_requests_timezone_aware_bson(monkeypatch) -> None:
    calls = {}
    database = object()

    class Client:
        def get_default_database(self):
            return database

    def client_factory(uri, **kwargs):
        calls.update(uri=uri, **kwargs)
        return Client()

    monkeypatch.setattr(
        "scripts.materialize_semantic_digest_claim_inputs.get_settings",
        lambda: SimpleNamespace(
            MONGODB_URI="mongodb://example/polymath",
            MONGODB_DATABASE="polymath",
        ),
    )
    monkeypatch.setattr(
        "scripts.materialize_semantic_digest_claim_inputs.AsyncIOMotorClient",
        client_factory,
    )
    monkeypatch.setattr(
        "scripts.materialize_semantic_digest_claim_inputs.settings_service.attach",
        lambda db: calls.update(attached=db),
    )

    client, resolved_database = asyncio.run(_database())

    assert isinstance(client, Client)
    assert resolved_database is database
    assert calls == {
        "uri": "mongodb://example/polymath",
        "tz_aware": True,
        "attached": database,
    }


def test_raw_export_path_accepts_resolved_tmp_and_rejects_repo_path() -> None:
    accepted = _require_tmp_path(Path("/tmp/claims.jsonl"))

    assert accepted.parent == Path("/tmp").resolve()
    with pytest.raises(MaterializationError, match="must stay under /tmp"):
        _require_tmp_path(Path(__file__).resolve())


def test_quantiles_are_deterministic_and_include_extremes() -> None:
    assert _quantiles([]) == {}
    assert _quantiles([9, 1, 5]) == {
        "0": 1,
        "25": 1,
        "50": 5,
        "75": 9,
        "90": 9,
        "95": 9,
        "99": 9,
        "100": 9,
    }


def test_longcat_cost_cards_resolve_once_without_old_fixed_cost() -> None:
    route = _route_prices()

    assert route["price"]["route_id"] == "longcat-api__longcat-2.0"
    assert route["price"]["uncached_input_usd"] == 0.75
    assert route["price"]["output_usd"] == 2.95
    assert route["parameters"]["max_tokens"] == 8192
