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
    _existing_provenance,
    _materialization_time,
    _packet_exclusion_ledger_entry,
    _persist_before_census,
    _persist_source_lineage_manifest,
    _persist_target_backup,
    _persist_write_manifest,
    _quantiles,
    _require_distinct_paths,
    _require_tmp_path,
    _route_prices,
    _source_lineage_sha256,
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


def test_target_backup_is_sorted_and_byte_deterministic() -> None:
    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        def sort(self, field, direction):
            assert (field, direction) == ("_id", 1)
            self.rows = sorted(self.rows, key=lambda row: row["_id"])
            return self

        def __aiter__(self):
            self.iterator = iter(self.rows)
            return self

        async def __anext__(self):
            try:
                return next(self.iterator)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    class Collection:
        def find(self, query):
            assert query == {"corpus_id": "corpus:test"}
            return Cursor(
                [
                    {"_id": "row:b", "corpus_id": "corpus:test"},
                    {"_id": "row:a", "corpus_id": "corpus:test"},
                ]
            )

    class Database:
        def __getitem__(self, name):
            assert name == "semantic_digest_claim_compilations"
            return Collection()

    first = Path("/tmp") / f"claim-target-backup-a-{os.getpid()}.jsonl"
    second = Path("/tmp") / f"claim-target-backup-b-{os.getpid()}.jsonl"
    try:
        first_receipt = asyncio.run(
            _persist_target_backup(
                Database(),
                corpus_id="corpus:test",
                path=first,
            )
        )
        second_receipt = asyncio.run(
            _persist_target_backup(
                Database(),
                corpus_id="corpus:test",
                path=second,
            )
        )

        assert first_receipt["row_count"] == 2
        assert first_receipt["file_sha256"] == second_receipt["file_sha256"]
        assert first.read_bytes() == second.read_bytes()
        assert '"_id":"row:a"' in first.read_text(encoding="utf-8").splitlines()[0]
    finally:
        first.unlink(missing_ok=True)
        second.unlink(missing_ok=True)


def test_source_lineage_manifest_is_pinned_and_byte_deterministic() -> None:
    scope = SimpleNamespace(corpus_id="corpus:test", child_ids=["child:test"])
    source = {
        "child:test": {
            "schema_version": "polymath.extract.local_extraction.v1",
            "doc_id": "doc:test",
            "chunk_id": "child:test",
            "source_version_id": "srcv:test",
            "raw_output_artifact_id": "raw:test",
            "raw_output_fingerprint": {"sha256": "a" * 64},
            "provider": "runpod_local_extraction",
            "model": "urchade/gliner_medium-v2.1",
            "provider_card": {"model_revision": "revision:test"},
            "local_extraction": {"schema_version": "local_extraction.v1"},
            "claim_compilation": {
                "schema_version": "claim_compilation.v1",
                "document_id": "doc:test",
                "child_id": "child:test",
                "claims": [],
                "links": [],
                "rejected_relation_ids": [],
                "unresolved_coreference_spans": [],
                "skipped_predicate_observation_ids": [],
                "same_sentence_repeated_claim_count": 0,
                "cross_sentence_candidate_count": 0,
                "cross_sentence_rejected_count": 0,
                "compiler_recipe_hash": "sha256:" + "b" * 64,
            },
        }
    }
    first = Path("/tmp") / f"claim-source-manifest-a-{os.getpid()}.jsonl"
    second = Path("/tmp") / f"claim-source-manifest-b-{os.getpid()}.jsonl"
    try:
        first_receipt = _persist_source_lineage_manifest(
            first,
            scope=scope,
            rows_by_child=source,
        )
        second_receipt = _persist_source_lineage_manifest(
            second,
            scope=scope,
            rows_by_child=source,
        )

        assert first_receipt["file_sha256"] == second_receipt["file_sha256"]
        assert first_receipt["file_sha256"] == _source_lineage_sha256(scope, source)
        assert first.read_bytes() == second.read_bytes()
        assert "raw:test" in first.read_text(encoding="utf-8")
    finally:
        first.unlink(missing_ok=True)
        second.unlink(missing_ok=True)


def test_write_manifest_seals_sorted_prospective_row_ids() -> None:
    path = Path("/tmp") / f"claim-write-manifest-{os.getpid()}.jsonl"
    rows = [
        {
            "record_type": "planned_row",
            "_id": "row:b",
            "document_id": "doc:test",
            "child_id": "child:b",
            "body_hash": "sha256:" + "b" * 64,
            "raw_artifact_ids": ["raw:b"],
            "expected_disposition": "insert_if_absent",
        },
        {
            "record_type": "planned_row",
            "_id": "row:a",
            "document_id": "doc:test",
            "child_id": "child:a",
            "body_hash": "sha256:" + "a" * 64,
            "raw_artifact_ids": ["raw:a"],
            "expected_disposition": "insert_if_absent",
        },
    ]
    try:
        receipt = _persist_write_manifest(
            path,
            corpus_id="corpus:test",
            input_file_sha256="f" * 64,
            source_lineage_sha256="e" * 64,
            expected_before_count=0,
            rows=rows,
        )
        lines = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]

        assert receipt["row_count"] == 2
        assert lines[0]["record_type"] == "manifest_header"
        assert lines[0]["input_file_sha256"] == "f" * 64
        assert lines[0]["source_lineage_sha256"] == "e" * 64
        assert lines[0]["expected_before_count"] == 0
        assert [line["_id"] for line in lines[1:]] == ["row:a", "row:b"]
        assert [line["raw_artifact_ids"] for line in lines[1:]] == [
            ["raw:a"],
            ["raw:b"],
        ]
    finally:
        path.unlink(missing_ok=True)


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


def test_materializer_paths_reject_output_and_partial_collisions() -> None:
    _require_distinct_paths(
        output="/tmp/claims.jsonl",
        manifest="/tmp/source-lineage.jsonl",
    )
    with pytest.raises(MaterializationError, match="paths collide"):
        _require_distinct_paths(
            output="/tmp/claims.jsonl",
            manifest="/tmp/claims.jsonl",
        )
    with pytest.raises(MaterializationError, match="paths collide"):
        _require_distinct_paths(
            output="/tmp/claims.jsonl",
            manifest="/tmp/claims.jsonl.partial",
        )


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


def test_fixed_materialization_time_is_utc_and_rejects_naive_values() -> None:
    parsed = _materialization_time("2026-07-18T23:58:00-06:00")

    assert parsed.isoformat() == "2026-07-19T05:58:00+00:00"
    with pytest.raises(MaterializationError, match="timezone-aware"):
        _materialization_time("2026-07-18T23:58:00")


def test_existing_claim_provenance_is_explicit_migration_lineage() -> None:
    values = _existing_provenance(
        {
            "model": "fallback-model",
            "provider_card": {
                "model": "urchade/gliner_medium-v2.1",
                "model_revision": "revision:test",
            },
        }
    )

    assert values == {
        "provenance_producer_kind": "migration",
        "provenance_engine": "existing_ghost_claim_materializer.v1",
        "provenance_model_id": "urchade/gliner_medium-v2.1",
        "provenance_model_revision": "revision:test",
    }


def test_longcat_cost_cards_resolve_once_without_old_fixed_cost() -> None:
    route = _route_prices()

    assert route["price"]["route_id"] == "longcat-api__longcat-2.0"
    assert route["price"]["uncached_input_usd"] == 0.75
    assert route["price"]["output_usd"] == 2.95
    assert route["parameters"]["max_tokens"] == 8192
