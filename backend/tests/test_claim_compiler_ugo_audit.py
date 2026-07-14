from __future__ import annotations

import json

import pytest

from scripts.audit_claim_compiler_ugo import AuditError, _load_rows, _sample_evenly


def test_claim_audit_even_sample_is_deterministic_and_includes_edges() -> None:
    rows = [{"chunk_id": str(index)} for index in range(10)]
    assert [row["chunk_id"] for row in _sample_evenly(rows, 4)] == [
        "0",
        "3",
        "6",
        "9",
    ]
    with pytest.raises(AuditError, match="found 10"):
        _sample_evenly(rows, 11)


def test_claim_audit_loader_rejects_extra_fields_and_filters_blank(tmp_path) -> None:
    path = tmp_path / "chunks.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"doc_id": "d", "chunk_id": "c2", "text": "two"}),
                json.dumps({"doc_id": "d", "chunk_id": "c1", "text": " "}),
                json.dumps({"doc_id": "d", "chunk_id": "c0", "text": "zero"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert [row["chunk_id"] for row in _load_rows(path)] == ["c0", "c2"]

    path.write_text(
        json.dumps({"doc_id": "d", "chunk_id": "c", "text": "x", "extra": 1}),
        encoding="utf-8",
    )
    with pytest.raises(AuditError, match="fields are not exact"):
        _load_rows(path)
