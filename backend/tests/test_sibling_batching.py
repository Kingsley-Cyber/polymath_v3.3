"""Sibling batching: grouping bounds, verbatim attribution, 1:1 child coverage."""

from services.extraction import sibling_batching as sib
from services.ghost_b import (
    EntityItem,
    ExtractionFailureItem,
    ExtractionResult,
    ExtractionTask,
)


def _task(chunk_id: str, text: str, parent: str) -> ExtractionTask:
    return ExtractionTask(
        chunk_id=chunk_id,
        doc_id="doc1",
        corpus_id="corpus1",
        text=text,
        metadata={"parent_id": parent},
    )


def _relation(subject: str, predicate: str, obj: str, evidence: str):
    from services.ghost_b import RelationItem

    return RelationItem(
        subject=subject,
        predicate=predicate,
        object=obj,
        object_kind="entity",
        confidence=0.9,
        evidence_phrase=evidence,
    )


def test_groups_siblings_and_isolates_parentless_tasks():
    tasks = [
        _task("c1", "Harbor accepts telemetry.", "p1"),
        _task("c2", "Northstar Labs operates Harbor.", "p1"),
        _task("c3", "Unrelated section text.", "p2"),
        _task("c4", "No parent recorded here.", ""),
    ]
    groups = sib.group_sibling_tasks(tasks)
    assert [len(g) for g in groups] == [2, 1, 1]
    assert [t.chunk_id for t in groups[0]] == ["c1", "c2"]


def test_group_respects_child_count_bound(monkeypatch):
    monkeypatch.setenv("EXTRACTION_SIBLING_GROUP_MAX_CHILDREN", "2")
    tasks = [_task(f"c{i}", f"sentence {i}", "p1") for i in range(5)]
    groups = sib.group_sibling_tasks(tasks)
    assert [len(g) for g in groups] == [2, 2, 1]


def test_merge_preserves_child_text_verbatim():
    group = [
        _task("c1", "Harbor accepts telemetry.", "p1"),
        _task("c2", "Northstar Labs operates Harbor.", "p1"),
    ]
    merged = sib.merge_group(group, ExtractionTask)
    assert merged.chunk_id == "c1"
    assert "Harbor accepts telemetry." in merged.text
    assert "Northstar Labs operates Harbor." in merged.text


def test_split_attributes_items_to_the_child_holding_the_span():
    group = [
        _task("c1", "Harbor accepts telemetry from sensors.", "p1"),
        _task("c2", "Northstar Labs operates Harbor.", "p1"),
    ]
    result = ExtractionResult(
        schema_version="v2",
        chunk_id="c1",
        doc_id="doc1",
        corpus_id="corpus1",
        entities=[
            EntityItem(
                canonical_name="northstar labs",
                surface_form="Northstar Labs",
                entity_type="Organization",
                confidence=0.9,
            )
        ],
        relations=[
            _relation("harbor", "consumes", "telemetry", "Harbor accepts telemetry"),
            _relation(
                "harbor", "created_by", "northstar labs", "Northstar Labs operates Harbor"
            ),
        ],
    )
    out = sib.split_result(result, group, ExtractionResult)
    assert [r.chunk_id for r in out] == ["c1", "c2"]
    # entity span lives in the second child
    assert [e.canonical_name for e in out[1].entities] == ["northstar labs"]
    assert out[0].entities == []
    # each relation follows its own evidence phrase
    assert [r.predicate for r in out[0].relations] == ["consumes"]
    assert [r.predicate for r in out[1].relations] == ["created_by"]


def test_unmatched_items_fall_back_to_anchor_never_dropped():
    group = [_task("c1", "alpha text", "p1"), _task("c2", "beta text", "p1")]
    result = ExtractionResult(
        schema_version="v2",
        chunk_id="c1",
        doc_id="doc1",
        corpus_id="corpus1",
        relations=[_relation("a", "uses", "b", "phrase absent from both children")],
    )
    out = sib.split_result(result, group, ExtractionResult)
    assert len(out[0].relations) == 1 and out[1].relations == []


def test_expand_report_gives_every_child_exactly_one_result():
    groups = [
        [_task("c1", "alpha text", "p1"), _task("c2", "beta text", "p1")],
        [_task("c3", "gamma text", "p2")],
    ]

    class _Report:
        results = [
            ExtractionResult(
                schema_version="v2",
                chunk_id="c1",
                doc_id="doc1",
                corpus_id="corpus1",
            ),
            ExtractionResult(
                schema_version="v2",
                chunk_id="c3",
                doc_id="doc1",
                corpus_id="corpus1",
            ),
        ]
        failures: list = []
        metrics: dict = {}

    results, failures = sib.expand_report(
        _Report(), groups, result_cls=ExtractionResult, failure_cls=ExtractionFailureItem
    )
    assert sorted(r.chunk_id for r in results) == ["c1", "c2", "c3"]
    assert failures == []


def test_group_failure_fans_out_to_every_child():
    groups = [[_task("c1", "alpha", "p1"), _task("c2", "beta", "p1")]]

    class _Report:
        results: list = []
        failures = [
            ExtractionFailureItem(
                chunk_id="c1",
                doc_id="doc1",
                corpus_id="corpus1",
                error_type="parse_error",
                error_message="boom",
                model="polymath-extract",
                lane=0,
                attempts=1,
            )
        ]
        metrics: dict = {}

    results, failures = sib.expand_report(
        _Report(), groups, result_cls=ExtractionResult, failure_cls=ExtractionFailureItem
    )
    assert sorted(f.chunk_id for f in failures) == ["c1", "c2"]
    assert results == []


def test_disabled_by_default():
    assert sib.batching_enabled() is False
