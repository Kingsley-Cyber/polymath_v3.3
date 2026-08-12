"""Thin encoder path: windowing, exact offset attribution, determinism, coverage."""

from dataclasses import dataclass

from services.extraction import thin_encoder_path as thin
from services.ghost_b import EntityItem, ExtractionResult, RelationItem


@dataclass
class _Child:
    chunk_id: str
    text: str
    doc_id: str = "doc1"
    corpus_id: str = "corpus1"


@dataclass
class _Rel:
    head_start: int
    head_end: int
    head_text: str
    tail_start: int
    tail_end: int
    tail_text: str
    label: str
    score: float


class _RelexOut:
    def __init__(self, entities=(), relations=()):
        self.entities = tuple(entities)
        self.relations = tuple(relations)


def test_windows_pack_children_and_never_split_one():
    children = [_Child("c1", "a" * 100), _Child("c2", "b" * 100), _Child("c3", "c" * 100)]
    windows = thin.build_windows(children, max_chars=210)
    assert [len(w.placements) for w in windows] == [2, 1]
    # offsets must index the real text exactly
    w0 = windows[0]
    assert w0.text[w0.placements[0].start:w0.placements[0].end] == "a" * 100
    assert w0.text[w0.placements[1].start:w0.placements[1].end] == "b" * 100


def test_oversized_child_gets_its_own_window_never_truncated():
    children = [_Child("big", "x" * 5000), _Child("small", "y" * 10)]
    windows = thin.build_windows(children, max_chars=1000)
    assert len(windows) == 2
    big = windows[0]
    assert big.text[big.placements[0].start:big.placements[0].end] == "x" * 5000


def test_blank_children_are_skipped_but_others_keep_exact_offsets():
    children = [_Child("c1", "   "), _Child("c2", "hello world")]
    windows = thin.build_windows(children, max_chars=500)
    assert len(windows) == 1
    p = windows[0].placements
    assert [x.chunk_id for x in p] == ["c2"]
    assert windows[0].text[p[0].start:p[0].end] == "hello world"


def test_owner_of_is_exact_containment():
    children = [_Child("c1", "alpha"), _Child("c2", "beta"), _Child("c3", "gamma")]
    w = thin.build_windows(children, max_chars=500)[0]
    assert thin.owner_of(w.placements, w.placements[1].start).chunk_id == "c2"
    assert thin.owner_of(w.placements, w.placements[2].end - 1).chunk_id == "c3"


def test_duplicate_text_across_chunks_attributes_by_offset_not_search():
    # identical text in two chunks — a substring search cannot tell them apart
    children = [_Child("c1", "Harbor"), _Child("c2", "Harbor")]
    w = thin.build_windows(children, max_chars=500)[0]
    second = w.placements[1]
    out = thin.results_for_window(
        w,
        _RelexOut(entities=[{"start": second.start, "end": second.end,
                             "text": "Harbor", "label": "Software", "score": 0.99}]),
        entity_cls=EntityItem, relation_cls=RelationItem,
        entity_threshold=0.5, relation_threshold=0.5,
    )
    assert len(out["c2"]["entities"]) == 1
    assert out["c1"]["entities"] == []


def test_cross_chunk_relation_is_captured_and_owned_by_the_subject():
    children = [_Child("c1", "Northstar Labs operates it."), _Child("c2", "Harbor is the platform.")]
    w = thin.build_windows(children, max_chars=500)[0]
    head = w.text.index("Northstar Labs")
    tail = w.text.index("Harbor")
    out = thin.results_for_window(
        w,
        _RelexOut(relations=[_Rel(head, head + 14, "Northstar Labs",
                                  tail, tail + 6, "Harbor", "created_by", 0.9)]),
        entity_cls=EntityItem, relation_cls=RelationItem,
        entity_threshold=0.5, relation_threshold=0.5,
    )
    assert [r.predicate for r in out["c1"]["relations"]] == ["created_by"]
    assert out["c2"]["relations"] == []
    rel = out["c1"]["relations"][0]
    assert rel.subject == "northstar labs" and rel.object == "harbor"
    # evidence must be a verbatim span of the prompted text
    assert rel.evidence_phrase in " ".join(w.text.split())


def test_thresholds_drop_low_confidence_items():
    children = [_Child("c1", "Harbor runs here.")]
    w = thin.build_windows(children, max_chars=500)[0]
    out = thin.results_for_window(
        w,
        _RelexOut(entities=[{"start": 0, "end": 6, "text": "Harbor",
                             "label": "Software", "score": 0.20}]),
        entity_cls=EntityItem, relation_cls=RelationItem,
        entity_threshold=0.5, relation_threshold=0.5,
    )
    assert out["c1"]["entities"] == []


def test_every_child_gets_exactly_one_result_even_with_no_spans():
    children = [_Child("c1", "alpha"), _Child("c2", "beta"), _Child("c3", "gamma")]
    results = thin.assemble_results(children, [], result_cls=ExtractionResult)
    assert [r.chunk_id for r in results] == ["c1", "c2", "c3"]
    assert all(r.entities == [] and r.relations == [] for r in results)


def test_results_are_deduped_and_deterministic():
    children = [_Child("c1", "Harbor uses MongoDB")]
    ent = EntityItem(canonical_name="harbor", surface_form="Harbor",
                     entity_type="Software", confidence=0.9)
    dupe = EntityItem(canonical_name="harbor", surface_form="Harbor",
                      entity_type="Software", confidence=0.8)
    bucket = [{"c1": {"entities": [ent, dupe], "relations": []}}]
    first = thin.assemble_results(children, bucket, result_cls=ExtractionResult)
    second = thin.assemble_results(children, bucket, result_cls=ExtractionResult)
    assert len(first[0].entities) == 1
    assert [e.canonical_name for e in first[0].entities] == [
        e.canonical_name for e in second[0].entities
    ]


def test_label_vocabularies_are_the_frozen_schema():
    entity_labels, predicates = thin.schema_vocabularies()
    assert "Organization" in entity_labels and "TimeReference" in entity_labels
    assert len(entity_labels) == 15
    assert "consumes" in predicates and "related_to" in predicates
    assert len(predicates) == 31


def test_canonical_name_matches_stored_convention():
    assert thin.canonical_name("  Northstar Labs, Inc. ") == "northstar labs inc"
    assert thin.canonical_name("GPT-4o") == "gpt-4o"
