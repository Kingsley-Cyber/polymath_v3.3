from __future__ import annotations

from pathlib import Path

import app


class _FakeEnt:
    def __init__(self, label: str, start_char: int, end_char: int) -> None:
        self.label_ = label
        self.start_char = start_char
        self.end_char = end_char


class _FakeSent:
    def __init__(self, text: str, start_char: int, end_char: int) -> None:
        self.text = text[start_char:end_char]
        self.start_char = start_char
        self.end_char = end_char


class _FakeDoc:
    def __init__(self, text: str, ents=(), sents=None) -> None:
        self.ents = list(ents)
        self.sents = list(sents or [_FakeSent(text, 0, len(text))])


def test_flash_endpoint_contract_is_burst_safe() -> None:
    remote = app.extract_batch.__remote_config__
    config = remote["resource_config"].model_dump(mode="json")

    assert config["name"] == "polymath-gliner-relex"
    assert config["workersMin"] == 0
    assert config["workersMax"] == 8
    assert config["scalerType"] == "REQUEST_COUNT"
    assert config["scalerValue"] == 1
    assert config["executionTimeoutMs"] == 1_800_000
    assert config["gpus"] == [
        "NVIDIA L4",
        "NVIDIA RTX A5000",
        "NVIDIA GeForce RTX 4090",
    ]


def test_empty_text_creates_no_inference_window() -> None:
    windows, sentences = app._windows("   ", nlp=object(), max_words=260)
    assert windows == []
    assert sentences == []


def test_windows_reuses_the_caller_provided_doc_without_a_second_parse() -> None:
    text = "One short sentence."
    # nlp=object() is not callable: passing it proves _windows never re-parses
    # when the shared per-chunk doc is supplied (T-HOOK-1 single-parse rule).
    windows, sentences = app._windows(
        text, nlp=object(), max_words=260, doc=_FakeDoc(text)
    )
    assert windows == [(text, 0)]
    assert sentences == [(0, len(text))]


def test_contract_version_is_v3_and_accepts_the_known_compatible_set() -> None:
    assert app._CONTRACT_VERSION == "polymath.runpod_gliner_relex.v3"
    assert app._ACCEPTED_CONTRACT_VERSIONS == frozenset(
        {
            "polymath.runpod_gliner_relex.v2",
            "polymath.runpod_gliner_relex.v3",
        }
    )


def test_regex_family_captures_each_temporal_form_with_exact_offsets() -> None:
    text = (
        "Published on 2024-03-05, revised March 2024. Guidance from 1999 "
        "applies until Q1 2025, when version 2.0 ships."
    )
    expressions, truncated = app._time_expressions(text, None)

    assert truncated is False
    by_surface = {item["text"]: item for item in expressions}
    assert sorted(by_surface) == ["1999", "2.0", "2024-03-05", "March 2024", "Q1 2025"]
    for item in expressions:
        assert item["detector"] == "regex"
        assert text[item["char_start"] : item["char_end"]] == item["text"]
    assert by_surface["2024-03-05"]["char_start"] == text.index("2024-03-05")
    assert by_surface["March 2024"]["char_start"] == text.index("March 2024")
    assert by_surface["1999"]["char_start"] == text.index("1999")
    assert by_surface["Q1 2025"]["char_start"] == text.index("Q1 2025")
    assert by_surface["2.0"]["char_start"] == text.index("2.0")
    # Offset order is deterministic.
    starts = [item["char_start"] for item in expressions]
    assert starts == sorted(starts)


def test_bare_year_inside_a_larger_expression_is_not_double_captured() -> None:
    text = "The audit window opened on 2024-03-05 and closed in Q4 2024."
    expressions, _ = app._time_expressions(text, None)
    assert [item["text"] for item in expressions] == ["2024-03-05", "Q4 2024"]


def test_qualified_period_families_retain_complete_temporal_surfaces() -> None:
    text = (
        "Operations resumed in autumn 1996, expanded in early 2007, and "
        "paused during the 2012 migration season. Mid-2014 planning led to "
        "a late 2015 review before Q2 2025."
    )
    expressions, truncated = app._time_expressions(text, None)

    assert truncated is False
    assert [item["text"] for item in expressions] == [
        "autumn 1996",
        "early 2007",
        "2012 migration season",
        "Mid-2014",
        "late 2015",
        "Q2 2025",
    ]
    assert all(item["detector"] == "regex" for item in expressions)
    assert all(
        text[item["char_start"] : item["char_end"]] == item["text"]
        for item in expressions
    )


def test_year_anchored_period_phrase_is_bounded_and_lexical_agnostic() -> None:
    text = (
        "The 2003 coastal migration period ended quietly. "
        "A separate report from 2004 describes the outcome."
    )
    expressions, _ = app._time_expressions(text, None)

    assert [item["text"] for item in expressions] == [
        "2003 coastal migration period",
        "2004",
    ]


def test_simple_year_ranges_are_single_nonoverlapping_expressions() -> None:
    text = "The first series spans 1997–1999; the second ran 2005 through 2008."
    expressions, _ = app._time_expressions(text, None)

    assert [item["text"] for item in expressions] == [
        "1997–1999",
        "2005 through 2008",
    ]


def test_version_strings_require_an_adjacent_release_or_version_token() -> None:
    guarded, _ = app._time_expressions("The team released v1.2 in June.", None)
    assert [item["text"] for item in guarded] == ["v1.2"]

    unguarded, _ = app._time_expressions("The signal ratio stays at 2.5 here.", None)
    assert unguarded == []


def test_role_candidates_come_from_cue_words_inside_the_window() -> None:
    # Each cue sits inside its expression's +/-40 char window; generous
    # neutral padding keeps neighbouring cues outside the other windows.
    text = (
        "Published in March 2021 with broad agreement from reviewers everywhere. "
        "The policy stays fully unchanged and is effective 2022-01-01 for every "
        "region and profile there. They said the group will launch in Q3 2026 "
        "under the very same plan and same name. No cue precedes 1987 "
        "whatsoever in this final plain sentence."
    )
    expressions, _ = app._time_expressions(text, None)
    by_surface = {item["text"]: item for item in expressions}

    assert by_surface["March 2021"]["role_candidates"] == ["publication"]
    assert by_surface["2022-01-01"]["role_candidates"] == ["effective"]
    assert by_surface["Q3 2026"]["role_candidates"] == ["forecast"]
    assert by_surface["1987"]["role_candidates"] == []


def test_spacy_date_ents_are_captured_and_suppress_overlapping_regex() -> None:
    text = "Updated in March 2024 by the maintainers."
    start = text.index("March 2024")
    doc = _FakeDoc(
        text,
        ents=[
            _FakeEnt("DATE", start, start + len("March 2024")),
            _FakeEnt("ORG", text.index("maintainers"), len(text) - 1),
        ],
    )
    expressions, _ = app._time_expressions(text, doc)

    assert len(expressions) == 1
    item = expressions[0]
    assert item["text"] == "March 2024"
    assert item["detector"] == "spacy"
    assert item["char_start"] == start
    assert item["role_candidates"] == ["revision"]


def test_time_expressions_cap_at_64_and_record_truncation() -> None:
    text = " ".join(f"in {1900 + i}" for i in range(80))
    expressions, truncated = app._time_expressions(text, None)

    assert truncated is True
    assert len(expressions) == 64
    assert expressions[0]["text"] == "1900"
    assert expressions[-1]["text"] == "1963"


def test_text_without_temporal_surface_forms_yields_an_empty_list() -> None:
    expressions, truncated = app._time_expressions(
        "Plain prose with no dates at all.", None
    )
    assert expressions == []
    assert truncated is False


def test_ontology_labels_are_humanized_and_round_trip_to_canonical_ids() -> None:
    entity_map = {
        app._label_key(label): label for label in ["TimeReference", "Concept"]
    }
    relation_map = {
        app._label_key(label): label for label in ["located_in", "created_by"]
    }

    assert app._inference_label("TimeReference") == "time reference"
    assert app._inference_label("created_by") == "created by"
    assert app._canonical_label("time reference", entity_map) == "TimeReference"
    assert app._canonical_label("located in", relation_map) == "located_in"


def test_entity_lens_greedily_batches_compatible_type_sets() -> None:
    rows = [
        [
            {"label": "person", "score": 0.9},
            {"label": "artifact", "score": 0.8},
        ],
        [
            {"label": "person", "score": 0.7},
            {"label": "location", "score": 0.95},
        ],
        [
            {"label": "concept", "score": 0.9},
            {"label": "method", "score": 0.85},
        ],
    ]

    groups = app._entity_lens_groups(
        rows,
        allowed_labels={"person", "artifact", "location", "concept", "method"},
        max_labels=3,
    )

    assert groups == [
        {"labels": ["artifact", "location", "person"], "indices": [0, 1]},
        {"labels": ["concept", "method"], "indices": [2]},
    ]


def test_cached_model_path_resolves_the_pinned_snapshot(tmp_path: Path) -> None:
    revision = "abc123"
    snapshot = (
        tmp_path
        / "models--knowledgator--gliner-relex-large-v0.5"
        / "snapshots"
        / revision
    )
    snapshot.mkdir(parents=True)

    resolved = app._cached_model_path(
        "knowledgator/gliner-relex-large-v0.5",
        revision,
        cache_root=tmp_path,
    )

    assert resolved == snapshot


def test_joint_relation_offsets_become_source_backed_wire_artifacts() -> None:
    text = "FACS represents facial movement."
    result = app._extract_task(
        {
            "chunk_id": "chunk-1",
            "doc_id": "doc-1",
            "corpus_id": "corpus-1",
            "text": text,
        },
        entities=[
            [
                {"text": "FACS", "label": "Method", "start": 0, "end": 4, "score": 0.9},
                {
                    "text": "facial movement",
                    "label": "Concept",
                    "start": 16,
                    "end": 31,
                    "score": 0.8,
                },
            ]
        ],
        relations=[
            [
                {
                    "head": {"text": "FACS", "start": 0, "end": 4},
                    "tail": {"text": "facial movement", "start": 16, "end": 31},
                    "relation": "represents",
                    "score": 0.85,
                }
            ]
        ],
        window_offsets=[0],
        sentence_spans=[(0, len(text))],
        entity_label_map={"method": "Method", "concept": "Concept"},
        relation_label_map={"represents": "represents"},
    )

    assert [item["canonical_name"] for item in result["entities"]] == [
        "facs",
        "facial movement",
    ]
    assert result["relations"] == [
        {
            "subject": "facs",
            "predicate": "represents",
            "object": "facial movement",
            "object_kind": "entity",
            "confidence": 0.85,
            "evidence_phrase": text,
            "relation_cue": "",
        }
    ]
    # v3 additive capture fields default to empty/false when absent.
    assert result["time_expressions"] == []
    assert result["time_expressions_truncated"] is False


def test_extract_task_carries_time_expressions_into_the_wire_result() -> None:
    text = "The framework was published in March 2024."
    expressions, truncated = app._time_expressions(text, None)
    result = app._extract_task(
        {
            "chunk_id": "chunk-t",
            "doc_id": "doc-t",
            "corpus_id": "corpus-t",
            "text": text,
        },
        entities=[[]],
        relations=[[]],
        window_offsets=[0],
        sentence_spans=[(0, len(text))],
        time_expressions=expressions,
        time_expressions_truncated=truncated,
    )

    assert result["time_expressions"] == [
        {
            "text": "March 2024",
            "char_start": text.index("March 2024"),
            "char_end": text.index("March 2024") + len("March 2024"),
            "detector": "regex",
            "role_candidates": ["publication"],
        }
    ]
    assert result["time_expressions_truncated"] is False
