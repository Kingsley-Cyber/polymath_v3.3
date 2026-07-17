from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts import activate_semantic_digests as cli


def _args(**changes):
    values = {
        "confirm_write": cli.CONFIRMATION,
        "expected_candidate_count": 7,
        "lock_owner": "codex/semantic-activation-20260716",
        "receipt": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_write_authority_requires_exact_lock_census_and_receipt(tmp_path, monkeypatch):
    lock = tmp_path / "polymath-eval.lock"
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(cli, "LOCK_PATH", lock)

    with pytest.raises(cli.ActivationCommandError, match="lock is missing"):
        cli._require_write_authority(_args(receipt=receipt), 7)

    lock.write_text("other-owner\n", encoding="utf-8")
    with pytest.raises(cli.ActivationCommandError, match="owner mismatch"):
        cli._require_write_authority(_args(receipt=receipt), 7)

    lock.write_text("codex/semantic-activation-20260716\n", encoding="utf-8")
    with pytest.raises(cli.ActivationCommandError, match="census drifted"):
        cli._require_write_authority(_args(receipt=receipt), 8)
    with pytest.raises(cli.ActivationCommandError, match="requires --receipt"):
        cli._require_write_authority(_args(receipt=None), 7)

    cli._require_write_authority(_args(receipt=receipt), 7)


def test_receipt_write_is_atomic_canonical_and_hash_bound(tmp_path):
    path = tmp_path / "receipts" / "activation.json"
    receipt = {
        "schema_version": cli.RECEIPT_SCHEMA_VERSION,
        "mode": "census",
        "candidate_count": 7,
    }

    result = cli._write_receipt(path, receipt)

    assert json.loads(path.read_text(encoding="utf-8")) == receipt
    assert result["receipt_hash"].startswith("sha256:")
    assert result["receipt_bytes"] == path.stat().st_size
    assert not path.with_suffix(".json.partial").exists()


@pytest.mark.asyncio
async def test_drain_is_corpus_scoped_and_reconciliation_mode_is_explicit(
    monkeypatch,
):
    calls = {}

    class Worker:
        def __init__(self, db, qdrant, *, owner, corpus_ids):
            calls["init"] = {
                "db": db,
                "qdrant": qdrant,
                "owner": owner,
                "corpus_ids": tuple(corpus_ids),
            }
            self.iteration = 0

        async def drain_batch(self, *, limit, reconciliation_only):
            calls.setdefault("batches", []).append((limit, reconciliation_only))
            self.iteration += 1
            if self.iteration == 1:
                return {
                    "claimed": 1,
                    "applied": 1,
                    "reconciled": 1,
                    "failed": 0,
                    "dead": 0,
                    "ack_pending": 0,
                }
            return {
                "claimed": 0,
                "applied": 0,
                "reconciled": 0,
                "failed": 0,
                "dead": 0,
                "ack_pending": 0,
            }

    monkeypatch.setattr(cli, "SemanticDigestProjectionWorker", Worker)

    totals = await cli._drain(
        "db",
        "qdrant",
        corpus_ids=["corpus:b", "corpus:a"],
        owner="branch",
        batch_limit=128,
        max_batches=3,
        reconciliation_only=True,
    )

    assert calls["init"]["corpus_ids"] == ("corpus:b", "corpus:a")
    assert calls["batches"] == [(32, True), (32, True)]
    assert totals["claimed"] == 1
    assert totals["reconciled"] == 1
