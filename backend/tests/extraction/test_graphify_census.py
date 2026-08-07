from __future__ import annotations

from pathlib import Path

from services.extraction.gliner2_cpu_provider import EntityPrediction
from services.extraction.graphify_census import (
    MAX_WINDOW_TOKENS,
    InMemoryRawMentionSink,
    JsonlRawMentionSink,
    build_census_windows,
    run_entity_census,
)
from services.extraction.graphify_normalization import normalize_document
from services.extraction.graphify_survey import survey_document


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], int]] = []

    def predict_entities(self, texts, *, batch_size, threshold, adapters=()):
        self.calls.append((list(texts), batch_size))
        rows = []
        for text in texts:
            needle = next((value for value in ("Polymath", "MongoDB", "ﬃ") if value in text), None)
            if needle is None and "ffi" in text:
                rows.append([[EntityPrediction("f", "software", text.index("ffi"), text.index("ffi") + 1, 0.8)]][0])
            elif needle:
                start = text.index(needle)
                rows.append([EntityPrediction(needle, "software", start, start + len(needle), 0.9)])
            else:
                rows.append([])
        return rows


def test_structure_windows_are_bounded_and_cover_semantic_segments() -> None:
    # unit.kind routing revision (owner-ratified 2026-08-07): windows cover
    # every SEMANTIC segment exactly — heading-only navigation blocks never
    # reach the entity model, but no prose byte is ever skipped.
    from services.extraction.graphify_unit_kind import (
        classify_document_blocks,
        semantic_segments,
    )

    paragraphs = [f"## Heading {index}\n\n" + "word " * 180 for index in range(15)]
    document = normalize_document("doc", "\n\n".join(paragraphs))
    survey = survey_document(document)
    windows = build_census_windows(document, survey)
    segments = semantic_segments(
        classify_document_blocks(document, survey), len(document.normalized_text),
    )
    windowed = [(w.normalized_start, w.normalized_end) for w in windows]
    for start, end in windowed:
        assert any(s <= start and end <= e for s, e in segments)
    covered_chars = sum(end - start for start, end in windowed)
    semantic_chars = sum(end - start for start, end in segments)
    assert covered_chars >= semantic_chars * 0.98  # whitespace seams only
    assert "## Heading" not in " ".join(w.text for w in windows)
    assert all(window.token_count <= MAX_WINDOW_TOKENS for window in windows)
    assert [window.sequence for window in windows] == list(range(len(windows)))


def test_census_conserves_and_restores_document_order() -> None:
    first = normalize_document("b-doc", "Polymath " + "word " * 600)
    second = normalize_document("a-doc", "MongoDB " + "word " * 100)
    documents = [first, second]
    surveys = [survey_document(item) for item in documents]
    sink = InMemoryRawMentionSink()
    provider = FakeProvider()
    output = run_entity_census(documents, surveys, provider, sink)
    assert output.report["conservation"] is True
    assert output.report["emitted_predictions"] == output.report["persisted_records"]
    assert [item.document_id for item in output.mentions] == ["b-doc", "a-doc"]
    assert len(sink.records) == len(output.mentions)
    assert len(provider.calls) == 1


def test_unrepresentable_original_span_is_a_persisted_failure() -> None:
    document = normalize_document("doc", "The ﬃ system")
    sink = InMemoryRawMentionSink()
    output = run_entity_census([document], [survey_document(document)], FakeProvider(), sink)
    mention = output.mentions[0]
    assert mention.terminal_state.value == "alignment_failure"
    assert mention.alignment_error == "original_span_not_exactly_representable"
    assert output.report["alignment_failures"] == 1
    assert len(sink.records) == 1


def test_jsonl_sink_is_idempotent(tmp_path: Path) -> None:
    document = normalize_document("doc", "Polymath is software.")
    output = run_entity_census(
        [document], [survey_document(document)], FakeProvider(),
        JsonlRawMentionSink(tmp_path / "raw_mentions.jsonl"),
    )
    second = JsonlRawMentionSink(tmp_path / "raw_mentions.jsonl")
    second.persist(output.mentions)
    assert len((tmp_path / "raw_mentions.jsonl").read_text().splitlines()) == len(output.mentions)


def test_census_source_has_only_allowed_stage_dependencies() -> None:
    source = (Path(__file__).resolve().parents[2] / "services/extraction/graphify_census.py").read_text()
    forbidden_imports = (
        "import spacy", "from spacy", "neo4j", "qdrant", "embedding",
        "FrameExtractor", "svo_candidates",
    )
    assert not any(value in source for value in forbidden_imports)


class DuplicateSpanProvider:
    """Same span surfaced by two labels: one type, two confidences/facets."""

    def predict_entities(self, texts, *, batch_size, threshold, adapters=()):
        rows = []
        for text in texts:
            if "Polymath" in text:
                start = text.index("Polymath")
                rows.append([
                    EntityPrediction("Polymath", "software", start, start + 8, 0.72, facet="schema_field"),
                    EntityPrediction("Polymath", "software", start, start + 8, 0.91, facet=""),
                ])
            else:
                rows.append([])
        return rows


def test_duplicate_span_predictions_fold_instead_of_conflicting() -> None:
    document = normalize_document("doc", "Polymath " + "word " * 100)
    sink = InMemoryRawMentionSink()
    output = run_entity_census(
        [document], [survey_document(document)], DuplicateSpanProvider(), sink,
    )
    polymath = [item for item in output.mentions if item.surface == "Polymath"]
    assert len(polymath) == 1
    assert polymath[0].confidence == 0.91  # deterministic winner: max confidence
    assert output.report["folded_duplicate_predictions"] == 1
    assert output.report["conservation"] is True
    assert len(sink.records) == len(output.mentions)


def test_misaligned_model_span_reanchors_or_persists_as_failure() -> None:
    from services.extraction.gliner2_cpu_provider import _anchor_span

    text = "CPCS-MX stores two linked representations:\n\n"
    # Unique occurrence: deterministic re-anchor.
    assert _anchor_span(text, "CPCS-MX", 3, 10) == (0, 7)
    # Ambiguous occurrence: emission passes through untouched — the census
    # persists it as an ALIGNMENT_FAILURE mention rather than crashing.
    two = "alpha beta alpha"
    assert _anchor_span(two, "alpha", 2, 7) == (2, 7)
    # Absent surface: unchanged as well.
    assert _anchor_span(text, "zeta", 1, 5) == (1, 5)
