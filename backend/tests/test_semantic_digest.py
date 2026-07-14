from __future__ import annotations

import subprocess
import sys

import pytest
from pydantic import ValidationError

from models.hash_taxonomy import namespace_hash
from models.semantic_digest import (
    DomainProposal,
    FrameProposal,
    LatentConceptProposal,
    MotifProposal,
    SemanticDigestV1,
    SupportedStatement,
)


SCHEMA_HASH_GOLDEN = (
    "sha256:ce106660a46ff7799e79399816dd634645e1b906f80905db3460f70787f97c99"
)


def _payload() -> dict:
    return {
        "schema_version": "semantic_digest.v1",
        "parent_id": "parent:one",
        "summary": "The system updates its baseline after repeated feedback.",
        "central_thesis": "Feedback changes the reference used for later choices.",
        "underlying_meanings": [
            {
                "text": "Repeated outcomes can reshape an internal reference.",
                "supporting_claim_ids": ["claim:one"],
            }
        ],
        "domain_proposals": [
            {
                "registry_id": "domain:control-systems",
                "proposed_label": "Control systems",
                "role": "adjacent",
                "assignment_state": "candidate",
                "supporting_claim_ids": ["claim:one"],
            }
        ],
        "frame_proposals": [
            {
                "frame_id": "MF07",
                "role": "dominant",
                "assignment_state": "candidate",
                "supporting_claim_ids": ["claim:one"],
                "explanation": "The current baseline is revised by feedback.",
            }
        ],
        "latent_concepts": [
            {
                "preferred_label": "adaptive reference",
                "definition": "A reference point updated from observed outcomes.",
                "assignment_state": "candidate",
                "supporting_claim_ids": ["claim:one"],
                "aliases": ["moving baseline"],
            }
        ],
        "motif_proposals": [
            {
                "proposed_label": "feedback-driven adaptation",
                "frame_sequence": ["MF07", "MF15"],
                "abstract_sequence": ["update reference", "stabilize behavior"],
                "supporting_claim_ids": ["claim:one", "claim:two"],
            }
        ],
        "conditions": [
            {
                "text": "Feedback must remain observable.",
                "supporting_claim_ids": ["claim:two"],
            }
        ],
        "exceptions": [],
        "unresolved_interpretations": ["The update cadence is unspecified."],
    }


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_owner_contract_field_set_is_exact():
    assert tuple(SemanticDigestV1.model_fields) == (
        "schema_version",
        "parent_id",
        "summary",
        "central_thesis",
        "underlying_meanings",
        "domain_proposals",
        "frame_proposals",
        "latent_concepts",
        "motif_proposals",
        "conditions",
        "exceptions",
        "unresolved_interpretations",
    )
    assert tuple(SupportedStatement.model_fields) == (
        "text",
        "supporting_claim_ids",
    )
    assert tuple(DomainProposal.model_fields) == (
        "registry_id",
        "proposed_label",
        "role",
        "assignment_state",
        "supporting_claim_ids",
    )
    assert tuple(FrameProposal.model_fields) == (
        "frame_id",
        "role",
        "assignment_state",
        "supporting_claim_ids",
        "explanation",
    )
    assert tuple(LatentConceptProposal.model_fields) == (
        "preferred_label",
        "definition",
        "assignment_state",
        "supporting_claim_ids",
        "aliases",
    )
    assert tuple(MotifProposal.model_fields) == (
        "proposed_label",
        "frame_sequence",
        "abstract_sequence",
        "supporting_claim_ids",
    )


def test_full_owner_contract_round_trips_without_provider_or_store_fields():
    digest = SemanticDigestV1.model_validate(_payload())
    replayed = SemanticDigestV1.model_validate_json(digest.model_dump_json())

    assert replayed == digest
    assert replayed.frame_proposals[0].frame_id == "MF07"
    assert replayed.latent_concepts[0].assignment_state == "candidate"
    assert "entities" not in replayed.model_fields
    assert "mongo_document" not in replayed.model_fields


def test_every_generated_object_schema_is_closed_and_portable():
    schema = SemanticDigestV1.model_json_schema()
    object_nodes = [node for node in _walk(schema) if node.get("type") == "object"]

    assert object_nodes
    assert all(node.get("additionalProperties") is False for node in object_nodes)
    assert all(
        set(node["properties"]) == set(node["required"])
        for node in object_nodes
    )
    assert not any("allOf" in node or "oneOf" in node for node in _walk(schema))


def test_every_array_is_required_and_empty_arrays_are_explicit():
    missing_root_array = _payload()
    missing_root_array.pop("exceptions")
    with pytest.raises(ValidationError, match="exceptions"):
        SemanticDigestV1.model_validate(missing_root_array)

    missing_nested_array = _payload()
    missing_nested_array["underlying_meanings"][0].pop("supporting_claim_ids")
    with pytest.raises(ValidationError, match="supporting_claim_ids"):
        SemanticDigestV1.model_validate(missing_nested_array)

    explicit_empty = _payload()
    explicit_empty["exceptions"] = []
    assert SemanticDigestV1.model_validate(explicit_empty).exceptions == []


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), "semantic_digest.v2"),
        (("frame_proposals", 0, "frame_id"), "MF17"),
        (("frame_proposals", 0, "role"), "primary"),
        (("domain_proposals", 0, "assignment_state"), "source_observed"),
    ],
)
def test_literal_and_enum_drift_fails_closed(path, value):
    payload = _payload()
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        SemanticDigestV1.model_validate(payload)


def test_root_and_nested_extras_fail_closed():
    root_extra = _payload()
    root_extra["neo4j_cypher"] = "MATCH (n) RETURN n"
    with pytest.raises(ValidationError, match="neo4j_cypher"):
        SemanticDigestV1.model_validate(root_extra)

    nested_extra = _payload()
    nested_extra["underlying_meanings"][0]["confidence"] = 0.9
    with pytest.raises(ValidationError, match="confidence"):
        SemanticDigestV1.model_validate(nested_extra)


def test_strict_mode_rejects_scalar_and_container_coercion():
    scalar = _payload()
    scalar["parent_id"] = 7
    with pytest.raises(ValidationError):
        SemanticDigestV1.model_validate(scalar)

    container = _payload()
    container["exceptions"] = "not-an-array"
    with pytest.raises(ValidationError):
        SemanticDigestV1.model_validate(container)


def test_schema_hash_golden_is_exact():
    assert (
        namespace_hash("schema", SemanticDigestV1.model_json_schema())
        == SCHEMA_HASH_GOLDEN
    )


def test_schema_hash_replays_in_a_fresh_process():
    code = """
from models.hash_taxonomy import namespace_hash
from models.semantic_digest import SemanticDigestV1
print(namespace_hash('schema', SemanticDigestV1.model_json_schema()))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == SCHEMA_HASH_GOLDEN
