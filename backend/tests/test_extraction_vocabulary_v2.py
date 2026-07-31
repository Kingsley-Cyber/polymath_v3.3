"""Vocabulary v2 — the root-cause fix, locked by its measured evidence.

v1 asked GLiNER for 25 labels, most of them ONTOLOGICAL CATEGORIES rather than
entity types. A zero-shot model asked for QUALITY correctly returns "good".
That is the model answering the question it was asked, and it is why every
downstream filter was fighting the label set instead of the model.

MEASURED A/B (80 real chunks, GLiNER medium-v2.1, survival through the
deterministic entity quality gate):
    v1 25 labels @0.45 : 9.36 raw/chunk -> 3.29 eligible/chunk (35.1%)
    v2 11 labels @0.45 : 7.46 raw/chunk -> 4.00 eligible/chunk (53.6%)
    v2 11 labels @0.55 : 5.94 raw/chunk -> 3.81 eligible/chunk (64.2%)
22% MORE eligible entities from 21% FEWER raw mentions.

Portable: reads the registry as data. No model, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
V1 = _ROOT / "runpod_flash_extractor/registries/extraction_vocabularies.v1.json"
V2 = _ROOT / "runpod_flash_extractor/registries/extraction_vocabularies.v2.json"

ONTOLOGY_TYPES = {
    "Person", "Organization", "Location", "Event", "Concept", "Method",
    "Product", "Software", "Document", "Standard", "Rule", "Law",
    "Artifact", "TimeReference", "other",
}


@pytest.fixture(scope="module")
def v2():
    return json.loads(V2.read_text())


class TestVocabularyShape:
    def test_v2_is_substantially_smaller(self, v2):
        """Label count is itself a defect: GLiNER degrades as labels grow."""
        v1 = json.loads(V1.read_text())
        assert len(v2["entity_types"]) < len(v1["entity_types"]) / 2

    def test_every_v2_label_maps_to_a_real_ontology_type(self, v2):
        """v1 labels were 100% outside ontology.yaml, which made the
        allowed_pairs gate decorative — it could only pass relations whose
        types it could not check."""
        for label, onto in v2["entity_type_to_ontology"].items():
            assert onto in ONTOLOGY_TYPES, f"{label} -> {onto} is not ontology"

    def test_every_label_has_a_mapping(self, v2):
        assert set(v2["entity_types"]) == set(v2["entity_type_to_ontology"])

    def test_labels_are_lowercase_type_nouns(self, v2):
        """Natural-language descriptive labels were TESTED and are worse: they
        raised the pronoun rate 13.4% -> 21.4%. Simple type nouns won."""
        for label in v2["entity_types"]:
            assert label.islower(), f"{label} is not lowercase"
            assert len(label.split()) == 1, f"{label} is descriptive, not a type noun"


class TestNoiseLabelsAreGone:
    @pytest.mark.parametrize("label", [
        "QUALITY", "BEHAVIOR", "STATE", "PROCESS", "GOAL", "OUTCOME",
        "CONDITION", "METRIC", "SIGNAL", "BASELINE", "POPULATION",
        "INTERVENTION", "GROUP", "AGENT", "TIME_PATTERN",
    ])
    def test_ontological_categories_removed(self, v2, label):
        assert label in v2["dropped_from_v1"]
        assert label.lower() not in v2["entity_types"]

    def test_person_survived_the_cull(self, v2):
        """PERSON measured 0.11 but is a FILTERING problem, not a vocabulary
        one — it is the right label for a real name."""
        assert "person" in v2["entity_types"]


class TestEvidenceIsRecorded:
    def test_ab_measurement_is_carried_with_the_registry(self, v2):
        ab = v2["measured_ab"]
        assert ab["v2_11_labels_thr_0.45"]["eligible_per_chunk"] > \
               ab["v1_25_labels_thr_0.45"]["eligible_per_chunk"], \
               "v2 must beat v1 on eligible yield or it should not ship"
        assert ab["v2_11_labels_thr_0.45"]["survival"] > \
               ab["v1_25_labels_thr_0.45"]["survival"]

    def test_rejected_alternative_is_documented(self, v2):
        """A design that was tried and lost must stay recorded, or someone
        re-proposes it."""
        assert "rejected_alternative" in v2["measured_ab"]

    def test_v1_is_retained_for_replay(self):
        assert V1.exists(), "v1 must remain for replay of v1-era artifacts"


class TestTypeNormalisationDuringRollout:
    def test_both_vocabularies_normalise_to_the_same_ontology_values(self):
        """v1 and v2 chunks coexist during rollout. If they normalised
        differently the corpus would carry two type systems at once."""
        from services.extraction.entity_quality import LABEL_TO_ONTOLOGY
        assert LABEL_TO_ONTOLOGY["ORGANIZATION"] == "Organization"
        assert LABEL_TO_ONTOLOGY["SYSTEM"] == "Software"
        assert LABEL_TO_ONTOLOGY["SOFTWARE"] == "Software"
        assert LABEL_TO_ONTOLOGY["PLACE"] == "Location"
        assert LABEL_TO_ONTOLOGY["LOCATION"] == "Location"


class TestBlueGreenRollout:
    """Flipping the contract hash while pods still run v1 would fail EVERY
    extraction on the lane. Both must be accepted during rollout."""

    def test_both_vocabulary_contracts_are_accepted(self):
        from services.runpod_local_extraction import (
            EXPECTED_ASSET_CONTRACT, EXTRACTION_VOCABULARY_SHA256_V2,
            _accepted_asset_contracts,
        )
        accepted = _accepted_asset_contracts()
        assert len(accepted) == 2
        shas = {c["extraction_vocabulary_sha256"] for c in accepted}
        assert EXPECTED_ASSET_CONTRACT["extraction_vocabulary_sha256"] in shas
        assert EXTRACTION_VOCABULARY_SHA256_V2 in shas

    def test_contracts_differ_ONLY_in_the_vocabulary_hash(self):
        """Accepting two contracts must not widen the door to a different
        MODEL — only to a different label set."""
        from services.runpod_local_extraction import _accepted_asset_contracts
        a, b = _accepted_asset_contracts()
        differing = {k for k in a if a[k] != b.get(k)}
        assert differing == {"extraction_vocabulary_sha256"}, (
            f"contracts differ in more than the vocabulary: {differing}"
        )

    def test_v2_hash_matches_the_registry_on_disk(self):
        """A stale constant would accept a contract nothing can produce."""
        import hashlib
        from pathlib import Path
        from services.runpod_local_extraction import (
            EXTRACTION_VOCABULARY_SHA256_V2,
        )
        path = (Path(__file__).resolve().parents[2]
                / "runpod_flash_extractor/registries/extraction_vocabularies.v2.json")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == \
            EXTRACTION_VOCABULARY_SHA256_V2

    def test_an_unknown_contract_is_still_rejected(self):
        from services.runpod_local_extraction import _accepted_asset_contracts
        rogue = dict(_accepted_asset_contracts()[0])
        rogue["gliner_weights_sha256"] = "0" * 64
        assert rogue not in _accepted_asset_contracts()
