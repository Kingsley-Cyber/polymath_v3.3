"""Storage-boundary contract tests; all databases here are in-memory fakes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from services.storage import mongo_writer, schema_validators


class _FakeCollection:
    def __init__(self):
        self.bulk_calls = []

    async def bulk_write(self, operations, *, ordered):
        self.bulk_calls.append((operations, ordered))


class _FakeDocuments:
    def __init__(self, existing=None):
        self.existing = existing
        self.find_calls = []
        self.replace_calls = []

    async def find_one(self, query, projection):
        self.find_calls.append((query, projection))
        return self.existing

    async def replace_one(self, query, replacement, *, upsert):
        self.replace_calls.append((query, replacement, upsert))


class _FakeDb:
    def __init__(self):
        self.parent_chunks = _FakeCollection()
        self.documents = _FakeDocuments()

    def __getitem__(self, name):
        if name == "parent_chunks":
            return self.parent_chunks
        if name == "documents":
            return self.documents
        raise KeyError(name)


def _parent(**updates):
    row = {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "parent_id": "parent-1",
        "text": "Published in 2024 and revised in 2024.",
        "summary": "A non-empty retrieval summary.",
        "temporal_class": "versioned",
        "time_expressions": [],
        "latent_concepts": [],
    }
    row.update(updates)
    return row


def test_parent_summary_projection_normalizes_capture_defaults():
    row = _parent()
    row.pop("temporal_class")
    row.pop("time_expressions")
    row.pop("latent_concepts")

    normalized = mongo_writer._validate_parent_summary_row(row)

    assert normalized["temporal_class"] == "unknown"
    assert normalized["time_expressions"] == []
    assert normalized["latent_concepts"] == []
    assert normalized["text"] == row["text"]


def test_parent_summary_projection_normalizes_legacy_null_capture_values():
    normalized = mongo_writer._validate_parent_summary_row(
        _parent(
            temporal_class=None,
            time_expressions=None,
            latent_concepts=None,
        )
    )

    assert normalized["temporal_class"] == "unknown"
    assert normalized["time_expressions"] == []
    assert normalized["latent_concepts"] == []


@pytest.mark.parametrize(
    "updates",
    [
        {"summary": ""},
        {"summary": "   "},
        {"temporal_class": "party_time"},
        {"time_expressions": [{"text": "2024", "role": "prediction"}]},
        {
            "time_expressions": [
                {"text": "2024", "role": "publication_time", "char_start": 20}
            ]
        },
        {
            "time_expressions": [
                {
                    "text": "2024",
                    "role": "publication_time",
                    "char_start": 20,
                    "char_end": 10,
                }
            ]
        },
        {
            "time_expressions": [
                {"text": str(i), "role": "unknown"} for i in range(13)
            ]
        },
        {
            "time_expressions": [
                {"text": "2024", "role": "unknown", "surprise": True}
            ]
        },
        {
            "latent_concepts": [
                {
                    "concept": "latent",
                    "evidence_basis": "direct",
                    "aliases": [],
                    "surprise": True,
                }
            ]
        },
    ],
)
def test_parent_summary_projection_rejects_malformed_contract(updates):
    with pytest.raises(ValidationError):
        mongo_writer._validate_parent_summary_row(_parent(**updates))


@pytest.mark.parametrize(
    "expression",
    [
        {
            "text": "2024",
            "role": "publication_time",
            "char_start": 0,
            "char_end": 4,
        },
        {
            "text": "2024",
            "role": "publication_time",
            "char_start": 100,
            "char_end": 104,
        },
        {"text": "2024", "role": "publication_time"},
    ],
)
def test_parent_summary_projection_rejects_unverified_source_spans(expression):
    with pytest.raises(ValueError):
        mongo_writer._validate_parent_summary_row(
            _parent(time_expressions=[expression])
        )


def test_parent_summary_projection_accepts_exact_repeated_source_spans():
    text = "Published in 2024 and revised in 2024."
    first = text.index("2024")
    second = text.rindex("2024")

    normalized = mongo_writer._validate_parent_summary_row(
        _parent(
            text=text,
            time_expressions=[
                {
                    "text": "2024",
                    "role": "publication_time",
                    "char_start": first,
                    "char_end": first + 4,
                },
                {
                    "text": "2024",
                    "role": "revision_time",
                    "char_start": second,
                    "char_end": second + 4,
                },
            ],
        )
    )

    assert [row["char_start"] for row in normalized["time_expressions"]] == [
        first,
        second,
    ]


@pytest.mark.asyncio
async def test_upsert_parent_chunks_rejects_before_bulk_write():
    db = _FakeDb()

    with pytest.raises(ValidationError):
        await mongo_writer.upsert_parent_chunks(
            db,
            [_parent(temporal_class="party_time")],
        )

    assert db.parent_chunks.bulk_calls == []


@pytest.mark.asyncio
async def test_upsert_parent_chunks_allows_structural_row_without_summary():
    db = _FakeDb()
    row = _parent()
    for field in ("summary", "temporal_class", "time_expressions", "latent_concepts"):
        row.pop(field)

    await mongo_writer.upsert_parent_chunks(db, [row])

    assert len(db.parent_chunks.bulk_calls) == 1


def test_parent_schema_mirrors_nested_summary_capture_bounds():
    props = schema_validators.PARENT_CHUNKS_SCHEMA["$jsonSchema"]["properties"]

    latent = props["latent_concepts"]
    assert latent["maxItems"] == 12
    assert latent["items"]["required"] == ["concept", "evidence_basis"]
    assert latent["items"]["additionalProperties"] is False
    assert latent["items"]["properties"]["concept"]["maxLength"] == 60
    assert latent["items"]["properties"]["aliases"]["maxItems"] == 3

    temporal = props["time_expressions"]
    assert temporal["maxItems"] == 12
    item = temporal["items"]
    assert item["additionalProperties"] is False
    assert item["properties"]["text"] == {
        "bsonType": "string",
        "minLength": 1,
        "maxLength": 60,
    }
    assert item["dependencies"] == {
        "char_start": ["char_end"],
        "char_end": ["char_start"],
    }
    assert item["properties"]["char_start"]["minimum"] == 0
    assert item["properties"]["char_end"]["minimum"] == 0


@pytest.mark.asyncio
async def test_upsert_document_replay_preserves_durable_bibliographic_identity():
    db = _FakeDb()
    db.documents = _FakeDocuments(
        {
            "author": "Durable Author",
            "title": "Durable Title",
            "language": "en",
            "document_date": "2020-01-02",
            "source_published_at": "2020-01-02",
            "date_confidence": "high",
            "bibliographic_provenance": {
                "method": "frontmatter_published",
                "source": "frontmatter:published",
                "origin": "backfill_v1",
                "captured_at": "2026-07-13T00:00:00+00:00",
            },
        }
    )

    await mongo_writer.upsert_document(
        db,
        {
            "doc_id": "doc-1",
            "corpus_id": "corpus-1",
            "routing_trace": {"parser": "replay"},
        },
    )

    assert len(db.documents.replace_calls) == 1
    _, replacement, upsert = db.documents.replace_calls[0]
    assert upsert is False
    assert replacement["author"] == "Durable Author"
    assert replacement["language"] == "en"
    assert replacement["document_date"] == "2020-01-02"
    assert replacement["source_published_at"] == "2020-01-02"
    assert replacement["date_confidence"] == "high"
    assert replacement["bibliographic_provenance"]["origin"] == "backfill_v1"


@pytest.mark.asyncio
async def test_upsert_document_retries_when_bibliographic_preimage_changes():
    first = {"author": "First Author"}
    enriched = {
        "author": "First Author",
        "document_date": "2021-04-05",
        "source_published_at": "2021-04-05",
        "date_confidence": "high",
        "bibliographic_provenance": {
            "method": "frontmatter_published",
            "origin": "backfill_v1",
            "captured_at": "2026-07-13T00:00:00+00:00",
        },
    }

    class _RacingDocuments(_FakeDocuments):
        def __init__(self):
            super().__init__()
            self.reads = [first, enriched]

        async def find_one(self, query, projection):
            self.find_calls.append((query, projection))
            return self.reads.pop(0)

        async def replace_one(self, query, replacement, *, upsert):
            self.replace_calls.append((query, replacement, upsert))
            matched = 0 if len(self.replace_calls) == 1 else 1
            return SimpleNamespace(matched_count=matched)

    db = _FakeDb()
    db.documents = _RacingDocuments()

    await mongo_writer.upsert_document(
        db,
        {"doc_id": "doc-1", "corpus_id": "corpus-1"},
    )

    assert len(db.documents.replace_calls) == 2
    assert db.documents.replace_calls[-1][1]["document_date"] == "2021-04-05"
