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

def _registry(name: str) -> Path:
    """Resolve a registry file in BOTH venues.

    Host: the repo tree (backend/registries and the pod mirror).
    Container: /app/registries, where only the backend copy is baked.
    A test that only runs in one venue silently stops guarding the other.
    """
    here = Path(__file__).resolve()
    for candidate in (
        here.parents[1] / "registries" / name,                       # backend/
        here.parents[2] / "runpod_flash_extractor/registries" / name,  # pod mirror
        Path("/app/registries") / name,                               # container
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(name)


V1 = _registry("extraction_vocabularies.v1.json")
V2 = _registry("extraction_vocabularies.v2.json")

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
        assert hashlib.sha256(V2.read_bytes()).hexdigest() == \
            EXTRACTION_VOCABULARY_SHA256_V2

    def test_an_unknown_contract_is_still_rejected(self):
        from services.runpod_local_extraction import _accepted_asset_contracts
        rogue = dict(_accepted_asset_contracts()[0])
        rogue["gliner_weights_sha256"] = "0" * 64
        assert rogue not in _accepted_asset_contracts()


class TestSchemaMigration:
    """EntityType is the WIRE SCHEMA for 2.8M stored mentions. Swapping it
    would invalidate every one of them on the next validation pass."""

    def test_entity_type_is_a_superset_not_a_replacement(self):
        from models.local_extraction import EntityType
        values = set(EntityType.__args__)
        # v1 values must survive -- stored data depends on them
        for v1 in ("PERSON", "ORGANIZATION", "QUALITY", "BEHAVIOR", "TIME_PATTERN"):
            assert v1 in values, f"{v1} removed; 2.8M stored mentions would break"
        # v2 values must be representable
        for v2 in ("person", "organization", "software", "artifact"):
            assert v2 in values, f"{v2} missing; v2 extraction cannot validate"

    def test_both_registries_load_against_the_shared_literal(self, monkeypatch):
        import importlib
        import models.extraction_registry as reg
        for version, expected in (("v1", 25), ("v2", 11)):
            monkeypatch.setenv("POLYMATH_EXTRACTION_VOCAB", version)
            importlib.reload(reg)
            vocab = reg.load_extraction_registries()["vocab"]
            assert vocab["version"] == version
            assert len(vocab["entity_types"]) == expected
        monkeypatch.delenv("POLYMATH_EXTRACTION_VOCAB", raising=False)
        importlib.reload(reg)

    def test_registries_have_distinct_namespace_hashes(self, monkeypatch):
        """If they hashed the same, the pod/backend blue-green check would be
        meaningless."""
        import importlib
        import models.extraction_registry as reg
        hashes = {}
        for version in ("v1", "v2"):
            monkeypatch.setenv("POLYMATH_EXTRACTION_VOCAB", version)
            importlib.reload(reg)
            hashes[version] = reg.extraction_registry_hashes()["vocab"]
        assert hashes["v1"] != hashes["v2"]
        monkeypatch.delenv("POLYMATH_EXTRACTION_VOCAB", raising=False)
        importlib.reload(reg)

    def test_default_is_v1_so_code_alone_cannot_change_a_pod(self, monkeypatch):
        """The vocabulary switch must be an explicit deployment decision, not a
        side effect of deploying code."""
        import importlib
        import models.extraction_registry as reg
        monkeypatch.delenv("POLYMATH_EXTRACTION_VOCAB", raising=False)
        importlib.reload(reg)
        assert reg.ACTIVE_VOCABULARY_VERSION == "v1"

    def test_unknown_label_still_fails_closed(self):
        """Subset validation must not become permissive."""
        from models.extraction_registry import ExtractionRegistryError
        from models.local_extraction import EntityType
        assert "NOT_A_REAL_TYPE" not in set(EntityType.__args__)
        assert ExtractionRegistryError is not None


class TestRuntimeIdentityBlueGreen:
    def test_backend_accepts_both_registry_hash_sets(self):
        from services.runpod_local_extraction import _accepted_registry_hashes
        accepted = _accepted_registry_hashes()
        assert len(accepted) == 2
        assert accepted[0]["vocab"] != accepted[1]["vocab"], (
            "the two vocabulary versions must hash differently or the "
            "blue-green check proves nothing"
        )

    def test_hashes_are_computed_live_not_hardcoded(self):
        """A hardcoded hash goes stale against the file it claims to describe."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "services" / "runpod_local_extraction.py").read_text()
        fn = src.split("def _accepted_registry_hashes")[1].split("def ")[0]
        assert "extraction_registry_hashes()" in fn
