from __future__ import annotations

import pytest

from db.indexes import _drop_legacy_single_field_unique_index
from models.schemas import SourceChunk
from services.graph.neo4j_writer import corpus_content_key
from services.retriever.merge import merge_pools


class _IndexCollection:
    def __init__(self):
        self.dropped: list[str] = []

    async def index_information(self):
        return {
            "_id_": {"key": [("_id", 1)]},
            "custom_global_node_identity": {
                "key": [("node_id", 1)],
                "unique": True,
            },
            "node_lookup": {"key": [("node_id", 1)]},
            "compound_identity": {
                "key": [("corpus_id", 1), ("node_id", 1)],
                "unique": True,
            },
        }

    async def drop_index(self, name: str):
        self.dropped.append(name)


@pytest.mark.asyncio
async def test_legacy_unique_index_migration_drops_only_global_unique_identity():
    collection = _IndexCollection()

    dropped = await _drop_legacy_single_field_unique_index(collection, "node_id")

    assert dropped == ["custom_global_node_identity"]
    assert collection.dropped == dropped


def _chunk(corpus_id: str) -> SourceChunk:
    return SourceChunk(
        chunk_id="same-child",
        parent_id="same-parent",
        doc_id="same-document",
        corpus_id=corpus_id,
        text=f"evidence from {corpus_id}",
        score=0.8,
        source_tier="child",
    )


def test_merge_keeps_same_content_identity_in_two_corpora():
    left = _chunk("corpus-a")
    right = _chunk("corpus-b")

    merged = merge_pools([left], [right], dedupe_by_parent=True)

    assert [(row.corpus_id, row.chunk_id) for row in merged] == [
        ("corpus-a", "same-child"),
        ("corpus-b", "same-child"),
    ]


def test_relation_provenance_key_qualifies_content_id():
    assert corpus_content_key("corpus-a", "same-child") == "corpus-a|same-child"
    assert corpus_content_key("corpus-b", "same-child") == "corpus-b|same-child"
