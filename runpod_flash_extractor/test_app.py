from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

import runtime


class FakeModel:
    def batch_predict_entities(self, texts, labels, *, threshold, batch_size):
        assert labels
        assert threshold == runtime.ENTITY_THRESHOLD
        assert 1 <= batch_size <= 32
        batches = []
        for text in texts:
            rows = []
            for surface, label, score in (
                ("Discounting", "METHOD", 0.91),
                ("reference prices", "CONCEPT", 0.88),
                ("2018 drought summer", "TIME_PATTERN", 0.86),
            ):
                start = text.find(surface)
                if start >= 0:
                    rows.append(
                        {
                            "start": start,
                            "end": start + len(surface),
                            "text": surface,
                            "label": label,
                            "score": score,
                        }
                    )
            batches.append(rows)
        return batches


def payload(text: str) -> dict:
    return {
        "contract_version": runtime.CONTRACT_VERSION,
        "batch_id": "locked:test-batch",
        "model_id": runtime.GLINER_MODEL_ID,
        "model_revision": runtime.GLINER_MODEL_REVISION,
        "spacy_pipeline": runtime.SPACY_MODEL,
        "asset_contract": dict(runtime.EXPECTED_ASSET_CONTRACT),
        "tasks": [
            {
                "document_id": "doc:test",
                "child_id": "child:test",
                "source_version_id": "srcv:test",
                "text": text,
            }
        ],
    }


@pytest.fixture(scope="module")
def nlp():
    import spacy

    loaded = spacy.load(runtime.SPACY_MODEL)
    assert spacy.__version__ == runtime.SPACY_VERSION
    assert loaded.meta["version"] == runtime.SPACY_MODEL_VERSION
    return loaded


def test_locked_contract_compiles_entities_predicates_and_temporal(nlp):
    text = (
        "Discounting does not reduce reference prices in winter 1911. "
        "It changed during the 2018 drought summer."
    )
    output = runtime.extract_local_batch(
        payload(text), nlp=nlp, model=FakeModel(), enforce_runtime=False
    )

    assert output["contract_version"] == runtime.CONTRACT_VERSION
    assert output["metrics"]["relations"] == 0
    result = output["results"][0]
    extraction = result["extraction"]
    assert extraction["schema_version"] == "local_extraction.v1"
    assert extraction["relations"] == []
    assert [(row["text"], row["entity_type"]) for row in extraction["entities"]] == [
        ("Discounting", "METHOD"),
        ("reference prices", "CONCEPT"),
        ("2018 drought summer", "TIME_PATTERN"),
    ]
    reduction = next(row for row in extraction["predicates"] if row["lemma"] == "reduce")
    assert reduction["normalized_predicate"] == "DECREASES"
    assert reduction["negated"] is True
    assert extraction["sentence_ids"]
    assert {row["text"] for row in result["temporal_captures"]} >= {
        "winter 1911",
        "2018 drought summer",
    }
    assert result["temporal_captures_truncated"] is False


def test_identical_request_is_byte_stable_except_duration(nlp):
    request = payload("Discounting lowers reference prices in the 2018 drought summer.")
    first = runtime.extract_local_batch(
        request, nlp=nlp, model=FakeModel(), enforce_runtime=False
    )
    second = runtime.extract_local_batch(
        request, nlp=nlp, model=FakeModel(), enforce_runtime=False
    )
    first["metrics"].pop("duration_seconds")
    second["metrics"].pop("duration_seconds")
    assert first == second


def test_legacy_contract_and_asset_drift_fail_closed():
    request = payload("Discounting lowers prices.")
    request["contract_version"] = "polymath.runpod_gliner_relex.v3"
    with pytest.raises(ValueError, match="unsupported extraction contract"):
        runtime.extract_local_batch(request, nlp=object(), model=object(), enforce_runtime=False)

    request = payload("Discounting lowers prices.")
    request["asset_contract"]["gliner_weights_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="asset contract"):
        runtime.extract_local_batch(request, nlp=object(), model=object(), enforce_runtime=False)


def test_task_shape_and_identity_fail_closed():
    request = payload("Discounting lowers prices.")
    request["tasks"][0]["unexpected"] = True
    with pytest.raises(ValueError, match="task fields"):
        runtime.extract_local_batch(request, nlp=object(), model=object(), enforce_runtime=False)

    request = payload("Discounting lowers prices.")
    request["tasks"].append(deepcopy(request["tasks"][0]))
    with pytest.raises(ValueError, match="child_id values must be unique"):
        runtime.extract_local_batch(request, nlp=object(), model=object(), enforce_runtime=False)


def test_source_closure_is_exact_and_credential_free():
    manifest = runtime.source_closure_manifest()
    assert manifest["file_count"] == 13
    assert set(manifest["files"]) == set(runtime._SOURCE_CLOSURE)
    root = Path(runtime.__file__).resolve().parent
    joined = "\n".join((root / path).read_text(encoding="utf-8") for path in manifest["files"])
    for forbidden in (
        "api_key",
        "mongodb://",
        "mongodb+srv://",
        "qdrant_client",
        "neo4j",
        "semantic_gateway_provider_prices",
    ):
        assert forbidden not in joined.lower()


def test_runtime_contract_matches_certified_versions_and_hashes():
    assert runtime.PYTHON_VERSION == "3.11.15"
    assert runtime.SPACY_VERSION == "3.8.14"
    assert runtime.SPACY_MODEL_VERSION == "3.8.0"
    assert runtime.GLINER_VERSION == "0.2.26"
    assert runtime.GLINER_MODEL_REVISION == "40ec419335d09393f298636f471328b722c6da9e"
    assert runtime._raw_registry_hashes() == {
        "extraction_vocabulary_sha256": runtime.EXTRACTION_VOCABULARY_SHA256,
        "predicate_normalization_sha256": runtime.PREDICATE_NORMALIZATION_SHA256,
    }
