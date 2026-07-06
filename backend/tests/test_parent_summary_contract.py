from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.ingestion.summary_semantics import parse_semantic_summary
from services.ingestion.summary_backfill import _child_context_for_rows
from services.storage import qdrant_writer


class _FakeAsyncCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def __aiter__(self):
        self._iter = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeChunks:
    def __init__(self, rows):
        self.rows = rows

    def find(self, *_args, **_kwargs):
        return _FakeAsyncCursor(self.rows)


class _FakeDb:
    def __init__(self, chunks):
        self.chunks = _FakeChunks(chunks)

    def __getitem__(self, name):
        if name != "chunks":
            raise KeyError(name)
        return self.chunks


class ParentSummaryContractTests(unittest.TestCase):
    def test_parse_semantic_summary_emits_compiler_artifact_fields(self):
        raw = """
        {
          "summary": "Polymath ingestion treats parent summaries as validated retrieval artifacts. Child chunks remain evidence for answer hydration. Graph extraction attaches structured context to stable chunk identifiers.",
          "domain": "machine_learning",
          "semantic_chunk_type": "framework",
          "key_terms": ["Polymath", "Graph extraction"],
          "mechanisms": ["child_evidence_hydration"],
          "central_claim": "Parent summaries are validated retrieval artifacts linked to child evidence.",
          "key_points": [
            {"point": "Parent summaries provide semantic recall.", "supporting_child_ids": ["child_a"]},
            {"point": "Child chunks provide answer evidence.", "supporting_child_ids": ["child_b"]},
            {"point": "Graph extraction adds structured context.", "supporting_child_ids": ["child_c"]}
          ],
          "concept_tags": ["hierarchical rag", "parent summary artifact", "child evidence hydration"],
          "entity_hints": ["Polymath"],
          "retrieval_uses": ["framework", "mechanism", "evidence"],
          "abstraction_level": "medium"
        }
        """
        parsed = parse_semantic_summary(
            raw,
            source_child_ids=["child_a", "child_b", "child_c"],
            source_text="Polymath ingestion and Graph extraction are discussed.",
        )

        self.assertEqual(parsed["schema_version"], "parent_summary.v1")
        self.assertEqual(parsed["summary_type"], "parent_retrieval_replacement")
        self.assertEqual(parsed["source_child_ids"], ["child_a", "child_b", "child_c"])
        self.assertEqual(len(parsed["key_points"]), 3)
        self.assertEqual(parsed["key_points"][0]["supporting_child_ids"], ["child_a"])
        self.assertIn("hierarchical rag", parsed["concept_tags"])
        self.assertIn("framework", parsed["retrieval_uses"])

    def test_qdrant_summary_payload_keeps_summary_and_child_anchors(self):
        captured = {}

        async def fake_assert_collection_owner(client, collection_name, corpus_id):
            return None

        async def fake_collection_layout(client, collection_name):
            return True, False

        async def fake_upsert_points_batched(client, *, collection_name, points, point_label):
            captured["collection_name"] = collection_name
            captured["points"] = points
            captured["point_label"] = point_label

        old_assert = qdrant_writer._assert_collection_owner
        old_layout = qdrant_writer._collection_layout
        old_upsert = qdrant_writer._upsert_points_batched
        qdrant_writer._assert_collection_owner = fake_assert_collection_owner
        qdrant_writer._collection_layout = fake_collection_layout
        qdrant_writer._upsert_points_batched = fake_upsert_points_batched
        try:
            asyncio.run(
                qdrant_writer.upsert_summaries(
                    client=object(),
                    corpus_id="abcdef1234567890",
                    summary_payloads=[
                        {
                            "corpus_id": "abcdef1234567890",
                            "doc_id": "doc_1",
                            "parent_id": "parent_1",
                            "source_tier": "tier_a",
                            "summary": "Parent-level retrieval replacement summary.",
                            "retrieval_text": "Parent summaries provide recall.\nParent-level retrieval replacement summary.\nKey points: Child chunks are evidence.",
                            "summary_type": "parent_retrieval_replacement",
                            "schema_version": "parent_summary.v1",
                            "summary_id": "sum_parent_unit",
                            "source_hash": "a" * 64,
                            "summary_model": "unit-model",
                            "summary_created_at": "2026-07-06T00:00:00+00:00",
                            "validation_status": "valid",
                            "repair_status": "none",
                            "quality_score": 0.91,
                            "quality_flags": ["summary_short"],
                            "summary_text": "Parent-level retrieval replacement summary.",
                            "central_claim": "Parent summaries provide recall.",
                            "key_points": [
                                {"point": "Child chunks are evidence.", "supporting_child_ids": ["child_1"]}
                            ],
                            "concept_tags": ["hierarchical rag"],
                            "retrieval_uses": ["evidence"],
                            "source_child_ids": ["child_1", "child_2"],
                        }
                    ],
                    vectors=[[0.1, 0.2, 0.3]],
                    target_kinds=["hrag"],
                )
            )
        finally:
            qdrant_writer._assert_collection_owner = old_assert
            qdrant_writer._collection_layout = old_layout
            qdrant_writer._upsert_points_batched = old_upsert

        payload = captured["points"][0].payload
        self.assertEqual(payload["schema_version"], "parent_summary.v1")
        self.assertEqual(payload["summary_type"], "parent_retrieval_replacement")
        self.assertEqual(payload["summary_text"], "Parent-level retrieval replacement summary.")
        self.assertEqual(payload["retrieval_text"], "Parent summaries provide recall.\nParent-level retrieval replacement summary.\nKey points: Child chunks are evidence.")
        self.assertEqual(payload["chunk_text"], payload["retrieval_text"])
        self.assertEqual(payload["summary_id"], "sum_parent_unit")
        self.assertEqual(payload["source_hash"], "a" * 64)
        self.assertEqual(payload["summary_model"], "unit-model")
        self.assertEqual(payload["validation_status"], "valid")
        self.assertEqual(payload["repair_status"], "none")
        self.assertEqual(payload["quality_score"], 0.91)
        self.assertEqual(payload["source_child_ids"], ["child_1", "child_2"])
        self.assertEqual(payload["chunk_type"], "summary")

    def test_summary_backfill_hydrates_child_anchor_context(self):
        async def run():
            return await _child_context_for_rows(
                _FakeDb([
                    {
                        "parent_id": "parent_1",
                        "chunk_id": "child_b",
                        "text": "Second child evidence.",
                    },
                    {
                        "parent_id": "parent_1",
                        "chunk_id": "child_a",
                        "text": "First child evidence.",
                    },
                ]),
                "corpus_1",
                [{"parent_id": "parent_1", "child_ids": ["child_a", "child_b"]}],
            )

        context = asyncio.run(run())["parent_1"]
        self.assertEqual(context["source_child_ids"], ["child_a", "child_b"])
        self.assertLess(
            context["child_boundaries"].find("[child_a]"),
            context["child_boundaries"].find("[child_b]"),
        )


if __name__ == "__main__":
    unittest.main()
