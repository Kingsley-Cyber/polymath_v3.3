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


def test_structure_windows_are_bounded_and_cover_document() -> None:
    paragraphs = [f"## Heading {index}\n\n" + "word " * 180 for index in range(15)]
    document = normalize_document("doc", "\n\n".join(paragraphs))
    windows = build_census_windows(document, survey_document(document))
    assert windows[0].normalized_start == 0
    assert windows[-1].normalized_end == len(document.normalized_text)
    assert all(left.normalized_end == right.normalized_start for left, right in zip(windows, windows[1:]))
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
