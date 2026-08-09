from services.extraction.graphify_census import build_census_windows
from services.extraction.graphify_normalization import normalize_document
from services.extraction.graphify_survey import survey_document
from services.extraction.graphify_unit_kind import (
    SEMANTIC_KINDS,
    classify_document_blocks,
    semantic_segments,
)

_DOC = """# Adaptive Systems Handbook

Alpha Service uses the Beacon Queue for routing and depends on the Ledger Store.

Asset: Your uploaded content that the platform stores durably for later analysis.

57. Delete a batch — [link](https://example.test/a)
58. Analyze and segment videos — [link](https://example.test/b)
59. Sync analysis — [link](https://example.test/c)

title: Adaptive Systems Handbook
version: 3.2
document_id: AR-17

```
def handler(payload):
    return payload["id"];
```

| stage | owner |
|-------|-------|
| parse | core  |

The Ledger Store produces nightly summaries that downstream teams consume.
"""


def _kinds():
    document = normalize_document("doc", _DOC)
    survey = survey_document(document)
    classified = classify_document_blocks(document, survey)
    return document, survey, classified


def test_every_block_is_classified_and_source_is_conserved() -> None:
    document, survey, classified = _kinds()
    assert len(classified) == len(survey.blocks)
    # Conservation: classification never removes bytes — every block keeps
    # its exact span in the normalized text.
    for item in classified:
        assert document.normalized_text[item.start:item.end]


def test_six_kinds_are_recognized_structurally() -> None:
    _document, _survey, classified = _kinds()
    kinds = {item.kind for item in classified}
    assert {"prose", "definition", "navigation", "metadata", "code", "table"} <= kinds


def test_prose_and_definitions_stay_semantic_furniture_does_not() -> None:
    document, _survey, classified = _kinds()
    by_kind = {}
    for item in classified:
        by_kind.setdefault(item.kind, []).append(
            document.normalized_text[item.start:item.end]
        )
    assert any("Alpha Service uses" in text for text in by_kind.get("prose", []))
    assert any("Asset:" in text for text in by_kind.get("definition", []))
    assert any("Delete a batch" in text for text in by_kind.get("navigation", []))
    assert any("version: 3.2" in text for text in by_kind.get("metadata", []))
    assert any("def handler" in text for text in by_kind.get("code", []))
    assert any("| stage | owner |" in text for text in by_kind.get("table", []))


def test_classification_is_deterministic_across_reruns() -> None:
    _d1, _s1, first = _kinds()
    _d2, _s2, second = _kinds()
    assert first == second


def test_census_windows_cover_exactly_the_semantic_segments() -> None:
    document, survey, classified = _kinds()
    windows = build_census_windows(document, survey)
    segments = semantic_segments(classified, len(document.normalized_text))
    covered = [(w.normalized_start, w.normalized_end) for w in windows]
    for start, end in covered:
        assert any(s <= start and end <= e for s, e in segments)
    windowed_text = " ".join(w.text for w in windows)
    assert "Alpha Service uses the Beacon Queue" in windowed_text
    assert "produces nightly summaries" in windowed_text
    assert "Your uploaded content" in windowed_text          # definitions stay
    assert "Delete a batch" not in windowed_text              # navigation off
    assert "def handler" not in windowed_text                 # code off
    assert "| stage | owner |" not in windowed_text           # table off
    assert "version: 3.2" not in windowed_text                # metadata routed


def test_numbered_prose_list_stays_semantic() -> None:
    text = (
        "1. The scheduler assigns each batch to the fastest worker available.\n"
        "2. The worker validates every record before the ledger accepts it.\n"
        "3. Failed records return to the queue and retry after a cooldown.\n"
    )
    document = normalize_document("doc", text)
    classified = classify_document_blocks(document, survey_document(document))
    assert all(item.kind in SEMANTIC_KINDS for item in classified)


def test_structure_propagation_on_eligibility_rows() -> None:
    from services.extraction.graphify_relations import evaluate_relation_eligibility

    text = (
        "# Reliability Guide\n\n"
        "## Storage Layer\n\n"
        "Asset: Your uploaded content that the platform stores durably for later use.\n\n"
        "Alpha Service uses the Beacon Queue for routing decisions.\n"
    )
    document = normalize_document("doc", text)
    survey = survey_document(document)
    decisions = evaluate_relation_eligibility([document], [survey], [])
    rows = [d.as_dict() for d in decisions]
    assert all(
        {"unit_kind", "heading_path", "section_id", "structural_parent",
         "definition_subject"} <= set(row) for row in rows
    )
    prose = [r for r in rows if "Alpha Service uses" in text[r["start"]:r["end"]]]
    assert prose and prose[0]["heading_path"][-1] == "Storage Layer"
    assert prose[0]["section_id"] and prose[0]["structural_parent"]
    definition = [r for r in rows if r["unit_kind"] == "definition"]
    assert definition and definition[0]["definition_subject"] == "Asset"
