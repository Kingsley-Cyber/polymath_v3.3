from __future__ import annotations

from pathlib import Path

import app


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
