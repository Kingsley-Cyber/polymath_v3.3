import os

import pytest

os.environ.setdefault("LITELLM_MASTER_KEY", "test-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

from models.schemas import SourceChunk
from services.conversation import conversation_service
from services.retriever.funnel_a import FunnelA
from services.retriever.funnel_b import FunnelB
from services.retriever.hydrate import hydrate_chunks, hydrate_summary_rerank_texts
from services.storage import qdrant_writer


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    async def to_list(self, length=None):
        if length is None:
            return list(self.rows)
        return list(self.rows)[:length]


class _Collection:
    def __init__(self, rows):
        self.rows = list(rows)

    def find(self, query, projection=None):
        del projection
        rows = list(self.rows)
        for field in ("doc_id", "corpus_id", "chunk_id"):
            if field not in query:
                continue
            expected = query[field]
            if isinstance(expected, dict) and "$in" in expected:
                allowed = set(expected["$in"])
                rows = [row for row in rows if row.get(field) in allowed]
            else:
                rows = [row for row in rows if row.get(field) == expected]
        return _Cursor(rows)


class _Db(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


class _Point:
    def __init__(self, payload):
        self.id = "point-1"
        self.score = 0.87
        self.payload = payload


class _QueryResponse:
    def __init__(self, payload):
        self.points = [_Point(payload)]


class _QdrantClient:
    def __init__(self, payload):
        self.payload = payload

    async def query_points(self, **_kwargs):
        return _QueryResponse(self.payload)


def _chunk(**overrides):
    base = dict(
        chunk_id="child-1",
        parent_id="parent-1",
        doc_id="doc-1",
        corpus_id="corpus-1",
        text="short preview",
        score=0.9,
        source_tier="tier_b",
    )
    base.update(overrides)
    return SourceChunk(**base)


@pytest.mark.asyncio
async def test_hydrate_chunks_prefers_filename_when_source_path_missing(monkeypatch):
    monkeypatch.setattr(
        "services.retriever.hydrate.get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "HYDRATION_MODE": "parent",
                "PARENT_EXCERPT_ENABLED": False,
                "PARENT_EXCERPT_MAX_CHARS": 1600,
            },
        )(),
    )
    fake_db = _Db(
        documents=_Collection(
            [
                {
                    "doc_id": "doc-1",
                    "corpus_id": "corpus-1",
                    "filename": "Thinking, Fast and Slow.md",
                    "parent_chunks": [
                        {
                            "parent_id": "parent-1",
                            "text": "Full parent text",
                            "heading_path": ["Judgment"],
                        }
                    ],
                }
            ]
        ),
        corpora=_Collection([{"corpus_id": "corpus-1", "name": "Books"}]),
        chunks=_Collection([]),
    )
    monkeypatch.setattr(conversation_service, "_db", fake_db)

    hydrated = await hydrate_chunks([_chunk()], ["corpus-1"])

    assert hydrated[0].doc_name == "Thinking, Fast and Slow.md"
    assert hydrated[0].corpus_name == "Books"
    assert hydrated[0].text == "Full parent text"


@pytest.mark.asyncio
async def test_hydrate_chunks_falls_back_to_source_path_basename(monkeypatch):
    fake_db = _Db(
        documents=_Collection(
            [
                {
                    "doc_id": "doc-1",
                    "corpus_id": "corpus-1",
                    "source_path": r"E:\library\aws-notes.txt",
                    "parent_chunks": [{"parent_id": "parent-1", "text": "Body"}],
                }
            ]
        ),
        corpora=_Collection([]),
        chunks=_Collection([]),
    )
    monkeypatch.setattr(conversation_service, "_db", fake_db)

    hydrated = await hydrate_chunks([_chunk()], ["corpus-1"])

    assert hydrated[0].doc_name == "aws-notes.txt"


@pytest.mark.asyncio
async def test_summary_hydration_sets_doc_name_for_global_mode(monkeypatch):
    fake_db = _Db(
        documents=_Collection(
            [
                {
                    "doc_id": "doc-1",
                    "corpus_id": "corpus-1",
                    "filename": "aws.txt",
                    "parent_chunks": [
                        {
                            "parent_id": "parent-1",
                            "summary": "Lambda is the event-driven compute choice.",
                            "heading_path": ["Decision chart"],
                            "chunk_kind": "table",
                        }
                    ],
                }
            ]
        )
    )
    monkeypatch.setattr(conversation_service, "_db", fake_db)

    hydrated = await hydrate_summary_rerank_texts(
        [_chunk(chunk_id="parent-1_summary", text="preview", summary="preview")],
        ["corpus-1"],
    )

    assert hydrated[0].doc_name == "aws.txt"
    assert hydrated[0].summary == "Lambda is the event-driven compute choice."
    assert hydrated[0].chunk_kind == "table"


@pytest.mark.asyncio
async def test_summary_hydration_sets_doc_name_when_parent_summary_missing(monkeypatch):
    fake_db = _Db(
        documents=_Collection(
            [
                {
                    "doc_id": "doc-1",
                    "corpus_id": "corpus-1",
                    "filename": "Personality Assessment Handbook.md",
                    "parent_chunks": [],
                }
            ]
        ),
        parent_chunks=_Collection([]),
    )
    monkeypatch.setattr(conversation_service, "_db", fake_db)

    hydrated = await hydrate_summary_rerank_texts(
        [_chunk(chunk_id="parent-1_summary", text="preview", summary="preview")],
        ["corpus-1"],
    )

    assert hydrated[0].doc_name == "Personality Assessment Handbook.md"
    assert hydrated[0].text == "preview"


@pytest.mark.asyncio
async def test_qdrant_child_and_summary_payloads_include_readable_doc_label(monkeypatch):
    captured = []

    async def _noop_assert(*_args, **_kwargs):
        return None

    async def _layout(*_args, **_kwargs):
        return True, False

    async def _capture(_client, *, collection_name, points, point_label, **_kwargs):
        captured.append((collection_name, point_label, points))

    monkeypatch.setattr(qdrant_writer, "_assert_collection_owner", _noop_assert)
    monkeypatch.setattr(qdrant_writer, "_collection_layout", _layout)
    monkeypatch.setattr(qdrant_writer, "_upsert_points_batched", _capture)

    await qdrant_writer.upsert_children(
        client=object(),
        corpus_id="corpus-12345678",
        chunks=[
            {
                "chunk_id": "child-1",
                "parent_id": "parent-1",
                "doc_id": "doc-1",
                "corpus_id": "corpus-12345678",
                "filename": "aws.txt",
                "text": "Lambda handles event-driven compute.",
                "source_tier": "tier_b",
                "facet_ids": ["event_driven_compute"],
                "facet_text": "event driven compute",
                "doc_facet_ids": ["aws_lambda"],
                "facet_schema_version": "polymath.facets.v1",
            }
        ],
        vectors=[[0.1, 0.2]],
        target_kinds=["naive"],
    )
    await qdrant_writer.upsert_summaries(
        client=object(),
        corpus_id="corpus-12345678",
        summary_payloads=[
            {
                "parent_id": "parent-1",
                "doc_id": "doc-1",
                "corpus_id": "corpus-12345678",
                "filename": "aws.txt",
                "summary": "AWS service recommendations.",
                "source_tier": "tier_a",
                "facet_ids": ["aws_service_recommendations"],
                "facet_text": "aws service recommendations",
                "doc_facet_ids": ["aws_lambda"],
                "facet_schema_version": "polymath.facets.v1",
            }
        ],
        vectors=[[0.3, 0.4]],
        target_kinds=["naive"],
    )

    child_payload = captured[0][2][0].payload
    summary_payload = captured[1][2][0].payload
    assert child_payload["filename"] == "aws.txt"
    assert child_payload["doc_name"] == "aws.txt"
    assert child_payload["facet_ids"] == ["event_driven_compute"]
    assert child_payload["doc_facet_ids"] == ["aws_lambda"]
    assert child_payload["facet_schema_version"] == "polymath.facets.v1"
    assert child_payload["entity_ids"] == []
    assert child_payload["relation_predicates"] == []
    assert child_payload["fact_types"] == []
    assert child_payload["has_relations"] is False
    assert child_payload["promote_version"] == ""
    assert summary_payload["filename"] == "aws.txt"
    assert summary_payload["doc_name"] == "aws.txt"
    assert summary_payload["facet_ids"] == ["aws_service_recommendations"]
    assert summary_payload["doc_facet_ids"] == ["aws_lambda"]
    assert summary_payload["facet_schema_version"] == "polymath.facets.v1"


@pytest.mark.asyncio
async def test_qdrant_funnels_read_payload_doc_name(monkeypatch):
    async def _layout(*_args, **_kwargs):
        return True, False

    monkeypatch.setattr(qdrant_writer, "_collection_layout", _layout)

    child_payload = {
        "chunk_id": "child-1",
        "parent_id": "parent-1",
        "doc_id": "doc-1",
        "corpus_id": "corpus-1",
        "doc_name": "aws.txt",
        "chunk_text": "Lambda handles events.",
        "source_tier": "tier_b",
        "chunk_type": "child",
        "facet_ids": ["event_driven_compute"],
        "facet_text": "event driven compute",
        "doc_facet_ids": ["aws_lambda"],
        "facet_schema_version": "polymath.facets.v1",
    }
    child_funnel = FunnelB.__new__(FunnelB)
    child_funnel.client = _QdrantClient(child_payload)

    child_chunks = await child_funnel._search_collection("collection", [0.1], None, 1)

    assert child_chunks[0].doc_name == "aws.txt"
    assert child_chunks[0].metadata["semantic_facets"]["facet_ids"] == [
        "event_driven_compute"
    ]

    summary_payload = {
        **child_payload,
        "chunk_id": "parent-1_summary",
        "chunk_type": "summary",
        "chunk_text": "AWS service recommendations.",
    }
    summary_funnel = FunnelA.__new__(FunnelA)
    summary_funnel.client = _QdrantClient(summary_payload)

    summary_chunks = await summary_funnel._search_collection("collection", [0.1], None, 1)

    assert summary_chunks[0].doc_name == "aws.txt"
    assert summary_chunks[0].metadata["semantic_facets"]["doc_facet_ids"] == [
        "aws_lambda"
    ]
