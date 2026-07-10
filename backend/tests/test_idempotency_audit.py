import pytest

from services.ingestion.idempotency_audit import (
    CONTENT_HASH_FIELDS,
    DOC_AGG_CARD,
    SOURCE_KEY_FIELDS,
    _doc_card,
    _duplicate_group_pipeline,
    _duplicate_group_action,
    audit_corpus_idempotency,
    summarize_identity_audit,
)


def test_doc_card_falls_back_to_nested_source_identity():
    card = _doc_card(
        {
            "doc_id": "doc-1",
            "filename": "book.md",
            "source_identity": {
                "source_key": "sha256:abc",
                "source_kind": "file_bytes",
                "content_sha256": "abc",
                "identity_version": "source_identity.v1",
            },
            "write_state": {"mongo_written": True, "qdrant_written": True},
        }
    )

    assert card["source_key"] == "sha256:abc"
    assert card["source_kind"] == "file_bytes"
    assert card["content_sha256"] == "abc"
    assert card["write_state"] == {
        "mongo_written": True,
        "qdrant_written": True,
        "neo4j_written": False,
        "verified": False,
    }


def test_duplicate_group_pipeline_pushes_field_paths_not_projection_flags():
    pipeline = _duplicate_group_pipeline(
        corpus_id="corpus-1",
        key_fields=SOURCE_KEY_FIELDS,
        limit=10,
    )

    add_fields_stage = pipeline[1]["$addFields"]
    group_stage = pipeline[2]["$group"]
    assert "_identity_group_key" in add_fields_stage
    assert group_stage["_id"] == "$_identity_group_key"
    assert group_stage["docs"]["$push"] == DOC_AGG_CARD
    assert DOC_AGG_CARD["doc_id"] == "$doc_id"
    assert DOC_AGG_CARD["source_identity"] == "$source_identity"
    assert DOC_AGG_CARD["content_sha256"] == "$content_sha256"
    assert DOC_AGG_CARD["source_file_hash"] == "$source_file_hash"


def test_doc_card_falls_back_to_top_level_content_hash_fields():
    card = _doc_card(
        {
            "doc_id": "doc-1",
            "content_sha256": "top-content",
            "source_file_hash": "source-file",
            "source_identity": {},
            "write_state": {},
        }
    )

    assert card["content_sha256"] == "top-content"


def test_duplicate_group_action_selects_best_artifact_owner_deterministically():
    action = _duplicate_group_action(
        [
            {
                "doc_id": "doc-b",
                "filename": "copy-b.pdf",
                "ingest_stage": "parsed",
                "write_state": {"mongo_written": True},
            },
            {
                "doc_id": "doc-a",
                "filename": "copy-a.pdf",
                "ingest_stage": "fully_enriched",
                "write_state": {
                    "mongo_written": True,
                    "qdrant_written": True,
                    "neo4j_written": True,
                    "verified": True,
                },
            },
            {
                "doc_id": "doc-c",
                "filename": "copy-c.pdf",
                "ingest_stage": "fully_enriched",
                "write_state": {
                    "mongo_written": True,
                    "qdrant_written": True,
                    "neo4j_written": True,
                },
            },
        ]
    )

    assert action["canonical_doc_id"] == "doc-a"
    assert action["canonical_doc"]["filename"] == "copy-a.pdf"
    assert action["duplicate_doc_ids"] == ["doc-b", "doc-c"]
    assert action["recommended_action"] == "reuse_canonical_artifacts_for_exact_duplicate"


def test_duplicate_group_action_tiebreaks_by_doc_id():
    action = _duplicate_group_action(
        [
            {
                "doc_id": "doc-z",
                "write_state": {"mongo_written": True, "qdrant_written": True},
            },
            {
                "doc_id": "doc-a",
                "write_state": {"mongo_written": True, "qdrant_written": True},
            },
        ]
    )

    assert action["canonical_doc_id"] == "doc-a"
    assert action["duplicate_doc_ids"] == ["doc-z"]


def test_duplicate_group_action_flags_source_key_collision_when_hashes_differ():
    action = _duplicate_group_action(
        [
            {
                "doc_id": "doc-a",
                "filename": "a.md",
                "source_identity": {"content_sha256": "hash-a"},
                "write_state": {"qdrant_written": True},
            },
            {
                "doc_id": "doc-b",
                "filename": "b.md",
                "source_identity": {"content_sha256": "hash-b"},
                "write_state": {"qdrant_written": True},
            },
        ]
    )

    assert action["source_key_collision"] is True
    assert action["content_hash_count"] == 2
    assert action["recommended_action"] == "repair_source_identity_collision"


def test_identity_audit_summary_reports_review_before_missing_identity():
    summary = summarize_identity_audit(
        corpus_id="corpus-1",
        doc_total=4,
        source_keyed_documents=3,
        content_hash_documents=2,
        duplicate_source_key_groups=[{"doc_count": 2}],
        duplicate_content_hash_groups=[{"doc_count": 2}],
        missing_source_identity=[{"doc_id": "doc-missing"}],
    )

    assert summary["status"] == "needs_review"
    assert summary["missing_source_identity_count"] == 1
    assert summary["duplicate_source_key_group_count"] == 1
    assert summary["duplicate_source_key_doc_count"] == 2
    assert summary["duplicate_content_hash_group_count"] == 1
    assert summary["duplicate_content_hash_doc_count"] == 2


def test_identity_audit_summary_reports_incomplete_identity_without_duplicates():
    summary = summarize_identity_audit(
        corpus_id="corpus-1",
        doc_total=4,
        source_keyed_documents=3,
        content_hash_documents=3,
    )

    assert summary["status"] == "incomplete_identity"
    assert summary["missing_source_identity_count"] == 1


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def limit(self, _limit):
        return self

    async def to_list(self, length=None):
        if length is None:
            return list(self.rows)
        return list(self.rows)[:length]


class _FakeDocuments:
    def __init__(self):
        self.pipelines = []
        self.find_queries = []

    def _query_mentions(self, query, key):
        if isinstance(query, dict):
            if key in query:
                return True
            return any(self._query_mentions(value, key) for value in query.values())
        if isinstance(query, list):
            return any(self._query_mentions(value, key) for value in query)
        return False

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        match_stage = pipeline[0]["$match"]
        key_field = pipeline[2]["$group"]["_id"]
        assert key_field == "$_identity_group_key"
        if self._query_mentions(match_stage, "source_key") or self._query_mentions(
            match_stage, "source_identity.source_key"
        ):
            return _FakeCursor(
                [
                    {
                        "_id": "sha256:abc",
                        "doc_count": 2,
                        "docs": [
                            {
                                "doc_id": "doc-1",
                                "filename": "one.md",
                                "source_key": "sha256:abc",
                                "source_identity": {"content_sha256": "abc"},
                            },
                            {
                                "doc_id": "doc-2",
                                "filename": "two.md",
                                "source_key": "sha256:abc",
                                "source_identity": {"content_sha256": "abc"},
                            },
                        ],
                    }
                ]
            )
        return _FakeCursor([])

    async def count_documents(self, query):
        if self._query_mentions(query, "source_key") or self._query_mentions(
            query, "source_identity.source_key"
        ):
            return 2
        if any(
            self._query_mentions(query, field)
            for field in CONTENT_HASH_FIELDS
        ):
            return 2
        return 3

    def find(self, query, projection):
        self.find_queries.append((query, projection))
        return _FakeCursor(
            [
                {
                    "doc_id": "doc-3",
                    "filename": "legacy.md",
                    "source_identity": {},
                }
            ]
        )


class _FakeDb(dict):
    def __init__(self):
        super().__init__({"documents": _FakeDocuments()})


@pytest.mark.asyncio
async def test_audit_corpus_idempotency_returns_bounded_groups_and_missing_examples():
    db = _FakeDb()

    result = await audit_corpus_idempotency(
        db,
        corpus_id="corpus-1",
        group_limit=5,
        missing_limit=5,
    )

    assert result["status"] == "needs_review"
    assert result["doc_total"] == 3
    assert result["source_keyed_documents"] == 2
    assert result["duplicate_source_key_group_count"] == 1
    assert result["duplicate_source_key_groups"][0]["source_key"] == "sha256:abc"
    assert result["duplicate_source_key_groups"][0]["canonical_doc_id"] == "doc-1"
    assert result["duplicate_source_key_groups"][0]["duplicate_doc_ids"] == ["doc-2"]
    assert (
        result["duplicate_source_key_groups"][0]["recommended_action"]
        == "reuse_canonical_artifacts_for_exact_duplicate"
    )
    assert [doc["doc_id"] for doc in result["duplicate_source_key_groups"][0]["docs"]] == [
        "doc-1",
        "doc-2",
    ]
    assert result["missing_source_identity"][0]["doc_id"] == "doc-3"
