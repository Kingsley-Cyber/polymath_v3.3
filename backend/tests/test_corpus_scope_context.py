"""Read-only selected-corpus catalog tests for corpus_scope.v3."""

from __future__ import annotations

import re
from typing import Any

import pytest

from services.corpus_scope_context import (
    _artifact_spec,
    build_corpus_scope_v3_context,
    clear_corpus_scope_context_cache,
)


def _path(row: dict[str, Any], dotted: str) -> Any:
    value: Any = row
    for part in dotted.split("."):
        if part == "0" and isinstance(value, list):
            return value[0] if value else None
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _matches(row: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(row, item) for item in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches(row, item) for item in expected):
                return False
            continue
        actual = _path(row, key)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$exists" in expected and (actual is not None) != bool(
                expected["$exists"]
            ):
                return False
            if "$regex" in expected:
                surface = (
                    " ".join(str(item) for item in actual)
                    if isinstance(actual, list)
                    else str(actual or "")
                )
                flags = re.I if "i" in str(expected.get("$options") or "") else 0
                if not re.search(str(expected["$regex"]), surface, flags):
                    return False
            continue
        if actual != expected:
            return False
    return True


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def to_list(self, *, length: int | None) -> list[dict[str, Any]]:
        del length
        return [dict(row) for row in self.rows]


class _Collection:
    def __init__(
        self,
        name: str,
        rows: list[dict[str, Any]],
        operations: list[tuple[str, str]],
    ) -> None:
        self.name = name
        self.rows = rows
        self.operations = operations

    def find(self, query: dict, projection: dict) -> _Cursor:
        del projection
        self.operations.append(("find", self.name))
        return _Cursor([row for row in self.rows if _matches(row, query)])

    async def count_documents(self, query: dict, **kwargs: Any) -> int:
        del kwargs
        self.operations.append(("count_documents", self.name))
        return len([row for row in self.rows if _matches(row, query)])


class _DB:
    def __init__(self) -> None:
        self.operations: list[tuple[str, str]] = []
        corpus = "e2e"
        self.rows = {
            "documents": [
                {
                    "corpus_id": corpus,
                    "doc_id": "animator",
                    "title": "The Animator's Survival Kit",
                    "author": "Richard Williams",
                    "document_date": "2001-01-01",
                    "updated_at": "2026-07-18T00:00:00Z",
                },
                {
                    "corpus_id": corpus,
                    "doc_id": "murch",
                    "title": "In the Blink of an Eye",
                    "author": "Walter Murch",
                    "source_published_at": "2004-01-01",
                    "updated_at": "2026-07-18T00:00:00Z",
                },
                {
                    "corpus_id": corpus,
                    "doc_id": "directing",
                    "title": "Directing - Film Techniques and Aesthetics",
                    "author": "Michael Rabiger",
                    "updated_at": "2026-07-18T00:00:00Z",
                },
                {
                    "corpus_id": corpus,
                    "doc_id": "deleted-deakins",
                    "title": "Roger Deakins Masterclass",
                    "author": "Roger Deakins",
                    "status": "deleted",
                    "updated_at": "2026-07-18T00:00:00Z",
                },
                {
                    "corpus_id": "other-corpus",
                    "doc_id": "other-deakins",
                    "title": "Roger Deakins Masterclass",
                    "author": "Roger Deakins",
                    "updated_at": "2026-07-18T00:00:00Z",
                },
            ],
            "parent_chunks": [
                {
                    "corpus_id": corpus,
                    "doc_id": "directing",
                    "parent_id": "p1",
                    "text": "The 2018 drought summer changed the location plan.",
                    "temporal_class": "event",
                    "time_expressions": [
                        {"text": "2018 drought summer", "role": "event_time"}
                    ],
                }
            ],
            "summary_tree": [],
            "chunks": [
                {
                    "corpus_id": corpus,
                    "doc_id": "directing",
                    "chunk_id": "c1",
                    "text": "Figure 2.1 demonstrates the staging axis.",
                },
                {
                    "corpus_id": corpus,
                    "doc_id": "directing",
                    "chunk_id": "c2",
                    "text": "Figure 94 is an unrelated production code.",
                },
            ],
        }

    def __getitem__(self, name: str) -> _Collection:
        return _Collection(name, self.rows.get(name, []), self.operations)


class _FailingCountCollection(_Collection):
    async def count_documents(self, query: dict, **kwargs: Any) -> int:
        del query, kwargs
        self.operations.append(("count_documents", self.name))
        raise RuntimeError("catalog unavailable")


class _FailingArtifactDB(_DB):
    def __getitem__(self, name: str) -> _Collection:
        collection_type = (
            _FailingCountCollection if name == "summary_tree" else _Collection
        )
        return collection_type(name, self.rows.get(name, []), self.operations)


class _PoisonedTemporalEnvelopeDB(_DB):
    def __init__(self) -> None:
        super().__init__()
        self.rows["documents"].append(
            {
                "corpus_id": "e2e",
                "doc_id": "future-fiction",
                "title": "A Fictional Production Timeline",
                "document_date": "2099-01-01",
                "updated_at": "2026-07-18T00:00:00Z",
            }
        )


def test_artifact_classifier_covers_numbered_and_locator_qualified_forms() -> None:
    cases = {
        "What does Figure 9.4 demonstrate?": "figure",
        "What is in Table 12.2?": "table",
        "What values appear in the ROI comparison table in chapter 12?": "table",
        "Summarize the VFX-supervisor interview in the appendix.": "interview",
        "List the 10-step checklist that ends the book.": "checklist",
    }
    for query, kind in cases.items():
        assert (_artifact_spec(query) or {}).get("kind") == kind


@pytest.mark.asyncio
async def test_context_named_source_positive_and_absent_are_full_corpus() -> None:
    clear_corpus_scope_context_cache()
    db = _DB()
    positive = await build_corpus_scope_v3_context(
        db,
        query="According to The Animator's Survival Kit, how should timing work?",
        corpus_ids=["e2e"],
    )
    absent = await build_corpus_scope_v3_context(
        db,
        query="What does Roger Deakins' masterclass say about lens flares?",
        corpus_ids=["e2e"],
    )
    visual_story = await build_corpus_scope_v3_context(
        db,
        query="What guidance does Bruce Block's The Visual Story give?",
        corpus_ids=["e2e"],
    )
    author_control = await build_corpus_scope_v3_context(
        db,
        query="What does Walter Murch say about cutting?",
        corpus_ids=["e2e"],
    )
    assert positive["named_source"]["matched_doc_ids"] == ["animator"]
    assert positive["named_source"]["missing"] is False
    assert absent["named_source"]["eligible"] is True
    assert absent["named_source"]["missing"] is True
    assert visual_story["named_source"]["missing"] is True
    assert author_control["named_source"]["matched_doc_ids"] == ["murch"]
    assert all(
        operation[0] in {"find", "count_documents"} for operation in db.operations
    )


@pytest.mark.asyncio
async def test_context_generic_source_reference_is_not_named_absence() -> None:
    clear_corpus_scope_context_cache()
    context = await build_corpus_scope_v3_context(
        _DB(),
        query="What does this source say about timing?",
        corpus_ids=["e2e"],
    )
    assert context["named_source"]["eligible"] is False
    assert context["named_source"]["missing"] is False


@pytest.mark.asyncio
async def test_context_generic_roles_are_not_named_sources() -> None:
    clear_corpus_scope_context_cache()
    db = _DB()
    for query in (
        "What do drawing instructors and cinematographers each say about "
        "guiding the viewer's eye through a frame?",
        "What do Drawing Instructors and Cinematographers each say about framing?",
    ):
        context = await build_corpus_scope_v3_context(
            db,
            query=query,
            corpus_ids=["e2e"],
        )
        assert context["named_source"]["eligible"] is False
        assert context["named_source"]["phrases"] == []
        assert context["named_source"]["missing"] is False


@pytest.mark.asyncio
async def test_context_preserves_quoted_capitalized_and_possessive_sources() -> None:
    clear_corpus_scope_context_cache()
    db = _DB()
    quoted = await build_corpus_scope_v3_context(
        db,
        query='What does "the visual story" say about contrast?',
        corpus_ids=["e2e"],
    )
    capitalized = await build_corpus_scope_v3_context(
        db,
        query="What does Walter Murch say about cutting?",
        corpus_ids=["e2e"],
    )
    possessive = await build_corpus_scope_v3_context(
        db,
        query="What does roger deakins' masterclass say about lens flares?",
        corpus_ids=["e2e"],
    )
    assert quoted["named_source"]["eligible"] is True
    assert quoted["named_source"]["missing"] is True
    assert capitalized["named_source"]["matched_doc_ids"] == ["murch"]
    assert possessive["named_source"]["eligible"] is True
    assert possessive["named_source"]["missing"] is True


@pytest.mark.asyncio
async def test_context_temporal_envelope_and_cache_are_deterministic() -> None:
    clear_corpus_scope_context_cache()
    db = _DB()
    first = await build_corpus_scope_v3_context(
        db,
        query="Who won the 2026 Academy Award for Best Cinematography?",
        corpus_ids=["e2e"],
    )
    temporal_reads_after_first = [
        item for item in db.operations if item[1] in {"parent_chunks", "summary_tree"}
    ]
    second = await build_corpus_scope_v3_context(
        db,
        query="Who won the 2026 Academy Award for Best Cinematography?",
        corpus_ids=["e2e"],
    )
    temporal_reads_after_second = [
        item for item in db.operations if item[1] in {"parent_chunks", "summary_tree"}
    ]
    assert first["corpus_epoch"] == second["corpus_epoch"]
    assert first["temporal"]["corpus_min_year"] == 2001
    assert first["temporal"]["corpus_max_year"] == 2018
    assert first["temporal"]["out_of_range"] is True
    assert temporal_reads_after_second == temporal_reads_after_first


@pytest.mark.asyncio
async def test_context_temporal_absence_uses_exact_support_not_range_envelope() -> None:
    clear_corpus_scope_context_cache()
    db = _PoisonedTemporalEnvelopeDB()
    unsupported = await build_corpus_scope_v3_context(
        db,
        query="Who won the 2026 Academy Award for Best Cinematography?",
        corpus_ids=["e2e"],
    )
    supported_document_year = await build_corpus_scope_v3_context(
        db,
        query="What was published in 2004?",
        corpus_ids=["e2e"],
    )
    unsupported_inside_envelope = await build_corpus_scope_v3_context(
        db,
        query="What happened in 2017?",
        corpus_ids=["e2e"],
    )
    assert unsupported["temporal"]["corpus_max_year"] == 2099
    assert unsupported["temporal"]["exact_support"] == []
    assert unsupported["temporal"]["out_of_range"] is True
    assert (
        unsupported["temporal"]["support_basis"]
        == "exact_time_expressions_or_document_dates"
    )
    assert supported_document_year["temporal"]["exact_support"] == ["2004"]
    assert supported_document_year["temporal"]["out_of_range"] is False
    assert unsupported_inside_envelope["temporal"]["out_of_range"] is True


@pytest.mark.asyncio
async def test_context_artifact_lookup_is_exact_and_locator_scoped() -> None:
    clear_corpus_scope_context_cache()
    db = _DB()
    absent = await build_corpus_scope_v3_context(
        db,
        query="What does Figure 9.4 in the directing book demonstrate?",
        corpus_ids=["e2e"],
    )
    present = await build_corpus_scope_v3_context(
        db,
        query="What does Figure 2.1 in the directing book demonstrate?",
        corpus_ids=["e2e"],
    )
    assert absent["artifact"]["matched_count"] == 0
    assert absent["artifact"]["lookup_scope"] == "locator_documents"
    assert absent["artifact"]["locator_doc_ids"] == ["directing"]
    assert present["artifact"]["matched_count"] == 1
    assert present["artifact"]["complete"] is True


@pytest.mark.asyncio
async def test_context_artifact_lookup_failure_is_explicitly_incomplete() -> None:
    clear_corpus_scope_context_cache()
    context = await build_corpus_scope_v3_context(
        _FailingArtifactDB(),
        query="What does Figure 9.4 in the directing book demonstrate?",
        corpus_ids=["e2e"],
    )
    assert context["artifact"]["eligible"] is True
    assert context["artifact"]["complete"] is False
