"""Minimal fail-closed registry boundary for LocalExtractionV1.

The extraction runtime needs exactly two immutable data files. Keeping this
loader independent of the semantic/domain registries makes the RunPod image's
source closure small and prevents summary or provider policy from entering the
credential-free worker.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from models.hash_taxonomy import namespace_hash
from models.local_extraction import (
    EntityType,
    Modality,
    Polarity,
    PredicateType,
)


REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registries"
FILES = {
    "vocab": "extraction_vocabularies.v1.json",
    "predicate_normalization": "predicate_normalization.v1.json",
}


class ExtractionRegistryError(ValueError):
    """An extraction registry is missing, malformed, or inconsistent."""


def _read(name: str) -> dict[str, Any]:
    path = REGISTRY_DIR / FILES[name]
    if not path.is_file():
        raise ExtractionRegistryError(f"extraction registry file missing: {path}")
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ExtractionRegistryError(f"extraction registry must be an object: {path}")
    return value


@lru_cache(maxsize=1)
def load_extraction_registries() -> dict[str, dict[str, Any]]:
    vocab = _read("vocab")
    expected_vocab_fields = {
        "registry",
        "version",
        "source",
        "policy_note",
        "entity_types",
        "predicate_types",
        "modalities",
        "polarities",
    }
    if set(vocab) != expected_vocab_fields:
        raise ExtractionRegistryError("extraction vocabulary fields are not exact")
    if vocab["registry"] != "extraction_vocabularies" or vocab["version"] != "v1":
        raise ExtractionRegistryError("extraction vocabulary identity drifted")
    model_literals = {
        "entity_types": list(EntityType.__args__),
        "predicate_types": list(PredicateType.__args__),
        "modalities": list(Modality.__args__),
        "polarities": list(Polarity.__args__),
    }
    for key, expected in model_literals.items():
        if vocab[key] != expected or len(expected) != len(set(expected)):
            raise ExtractionRegistryError(f"{key} drifted from LocalExtractionV1")

    normalization = _read("predicate_normalization")
    expected_normalization_fields = {
        "registry",
        "version",
        "authority",
        "owner_ratification_required",
        "source",
        "unknown_policy",
        "default_predicate",
        "match_field",
        "negation_modality_polarity_out_of_scope",
        "changes_require_new_version",
        "normalizations",
    }
    if set(normalization) != expected_normalization_fields:
        raise ExtractionRegistryError("predicate normalization fields are not exact")
    required_header = {
        "registry": "predicate_normalization",
        "version": "v1",
        "authority": "executor-proposed, owner-ratifiable",
        "owner_ratification_required": True,
        "unknown_policy": "unresolved_spans",
        "default_predicate": None,
        "match_field": "spacy_lemma_lowercase",
        "negation_modality_polarity_out_of_scope": True,
        "changes_require_new_version": True,
    }
    for key, expected in required_header.items():
        if normalization[key] != expected:
            raise ExtractionRegistryError(f"predicate normalization {key} drifted")
    rows = normalization["normalizations"]
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or set(row) != {"predicate_type", "lemmas"}
        for row in rows
    ):
        raise ExtractionRegistryError("predicate normalization rows are malformed")
    if [row["predicate_type"] for row in rows] != vocab["predicate_types"]:
        raise ExtractionRegistryError("predicate normalization coverage/order drifted")
    all_lemmas: list[str] = []
    for row in rows:
        lemmas = row["lemmas"]
        if not isinstance(lemmas, list) or lemmas != sorted(set(lemmas)):
            raise ExtractionRegistryError("predicate lemmas must be sorted and unique")
        if any(
            not isinstance(lemma, str)
            or not lemma
            or lemma != lemma.strip().lower()
            or not lemma.replace("-", "").isalpha()
            for lemma in lemmas
        ):
            raise ExtractionRegistryError("predicate lemmas must be lowercase words")
        all_lemmas.extend(lemmas)
    if len(all_lemmas) != len(set(all_lemmas)):
        raise ExtractionRegistryError("predicate lemmas must map uniquely")
    return {"vocab": vocab, "predicate_normalization": normalization}


def extraction_registry_hashes() -> dict[str, str]:
    return {name: namespace_hash("registry", _read(name)) for name in FILES}


def normalize_predicate_lemma(lemma: str) -> dict[str, str] | None:
    normalized = str(lemma or "").strip().lower()
    registry = load_extraction_registries()["predicate_normalization"]
    for row in registry["normalizations"]:
        if normalized in row["lemmas"]:
            return {
                "lemma": normalized,
                "predicate_type": row["predicate_type"],
                "registry": registry["registry"],
                "registry_version": registry["version"],
                "authority": registry["authority"],
            }
    return None
