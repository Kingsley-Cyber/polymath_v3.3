from __future__ import annotations

import pytest

from models.semantic_digest import SemanticDigestV1
from models.semantic_validator import (
    ClaimScope,
    SemanticValidationContext,
    semantic_validate,
)


def _payload() -> dict:
    return {
        "schema_version": "semantic_digest.v1",
        "parent_id": "parent:one",
        "summary": "Feedback updates the reference used for later choices.",
        "central_thesis": "Observed outcomes can change an internal baseline.",
        "underlying_meanings": [
            {
                "text": "Repeated outcomes reshape a reference.",
                "supporting_claim_ids": ["claim:one"],
            }
        ],
        "domain_proposals": [
            {
                "registry_id": "D09",
                "proposed_label": "Technology and Engineered Systems",
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
                "explanation": "Feedback updates a belief or reference.",
            },
            {
                "frame_id": "MF15",
                "role": "supporting",
                "assignment_state": "corroborated",
                "supporting_claim_ids": ["claim:two"],
                "explanation": "Repeated updates accumulate over time.",
            },
        ],
        "latent_concepts": [
            {
                "preferred_label": "adaptive reference",
                "definition": "A reference updated from observed outcomes.",
                "assignment_state": "candidate",
                "supporting_claim_ids": ["claim:one"],
                "aliases": ["moving baseline"],
            }
        ],
        "motif_proposals": [
            {
                "proposed_label": "feedback-driven adaptation",
                "frame_sequence": ["MF07", "MF15"],
                "abstract_sequence": ["update", "accumulate"],
                "supporting_claim_ids": ["claim:one", "claim:two"],
            }
        ],
        "conditions": [
            {
                "text": "Feedback remains observable.",
                "supporting_claim_ids": ["claim:two"],
            }
        ],
        "exceptions": [
            {
                "text": "No update occurs without feedback.",
                "supporting_claim_ids": ["claim:one"],
            }
        ],
        "unresolved_interpretations": [],
    }


def _digest() -> SemanticDigestV1:
    return SemanticDigestV1.model_validate(_payload())


def _context(
    *,
    claim_grounded_mode: bool = True,
    validated_frame_ids=(),
    self_reference_ids=(),
) -> SemanticValidationContext:
    return SemanticValidationContext.from_owner_registries(
        parent_id="parent:one",
        claims=(
            ClaimScope(claim_id="claim:one", parent_id="parent:one"),
            ClaimScope(claim_id="claim:two", parent_id="parent:one"),
            ClaimScope(claim_id="claim:foreign", parent_id="parent:other"),
        ),
        validated_frame_ids=validated_frame_ids,
        claim_grounded_mode=claim_grounded_mode,
        self_reference_ids=self_reference_ids,
    )


def test_valid_digest_has_no_semantic_errors():
    context = _context()
    assert context.domain_registry_ids == frozenset(
        f"D{number:02d}" for number in range(1, 17)
    )
    assert semantic_validate(_digest(), context) == []


def test_digest_parent_must_match_supplied_parent():
    digest = _digest().model_copy(update={"parent_id": "parent:other"})

    errors = semantic_validate(digest, _context())

    assert errors[0] == (
        "parent_id: digest parent 'parent:other' does not match supplied "
        "parent 'parent:one'"
    )


@pytest.mark.parametrize(
    "field_name",
    [
        "underlying_meanings",
        "domain_proposals",
        "frame_proposals",
        "latent_concepts",
        "motif_proposals",
        "conditions",
        "exceptions",
    ],
)
def test_every_supporting_claim_field_rejects_unknown_claims(field_name):
    payload = _payload()
    payload[field_name][0]["supporting_claim_ids"] = ["claim:missing"]
    digest = SemanticDigestV1.model_validate(payload)

    errors = semantic_validate(digest, _context())

    assert (
        f"{field_name}[0].supporting_claim_ids[0]: unknown claim_id "
        "'claim:missing'"
    ) in errors


def test_claim_must_belong_to_supplied_parent():
    payload = _payload()
    payload["conditions"][0]["supporting_claim_ids"] = ["claim:foreign"]

    errors = semantic_validate(SemanticDigestV1.model_validate(payload), _context())

    assert errors == [
        "conditions[0].supporting_claim_ids[0]: claim_id 'claim:foreign' "
        "belongs to parent 'parent:other', not supplied parent 'parent:one'"
    ]


def test_unknown_domain_is_allowed_only_as_candidate():
    candidate = _payload()
    candidate["domain_proposals"][0]["registry_id"] = "domain:new"
    assert semantic_validate(
        SemanticDigestV1.model_validate(candidate), _context()
    ) == []

    promoted = _payload()
    promoted["domain_proposals"][0]["registry_id"] = "domain:new"
    promoted["domain_proposals"][0]["assignment_state"] = "corroborated"
    errors = semantic_validate(
        SemanticDigestV1.model_validate(promoted), _context()
    )
    assert errors == [
        "domain_proposals[0].registry_id: unknown domain registry id "
        "'domain:new' requires assignment_state='candidate'"
    ]


def test_every_frame_id_is_checked_even_after_structural_validation():
    digest = _digest()
    invalid_frame = digest.frame_proposals[0].model_copy(
        update={"frame_id": "MF17"}
    )
    digest = digest.model_copy(
        update={"frame_proposals": [invalid_frame, *digest.frame_proposals[1:]]}
    )

    errors = semantic_validate(digest, _context())

    assert errors[0] == (
        "frame_proposals[0].frame_id: unknown frame id 'MF17'; "
        "expected MF01-MF16"
    )
    assert any(
        error.startswith("motif_proposals[0].frame_sequence[0]:")
        for error in errors
    )


def test_motif_frame_ids_are_checked_even_after_structural_validation():
    digest = _digest()
    motif = digest.motif_proposals[0].model_copy(
        update={"frame_sequence": ["MF07", "MF17"]}
    )
    digest = digest.model_copy(update={"motif_proposals": [motif]})

    errors = semantic_validate(digest, _context())

    assert errors == [
        "motif_proposals[0].frame_sequence[1]: unknown frame id 'MF17'; "
        "expected MF01-MF16"
    ]


def test_frame_proposal_requires_a_supporting_claim():
    payload = _payload()
    payload["frame_proposals"][0]["supporting_claim_ids"] = []

    errors = semantic_validate(SemanticDigestV1.model_validate(payload), _context())

    assert (
        "frame_proposals[0].supporting_claim_ids: at least one supporting "
        "claim is required"
    ) in errors


def test_latent_concept_support_depends_on_claim_grounded_mode():
    payload = _payload()
    payload["latent_concepts"][0]["supporting_claim_ids"] = []
    digest = SemanticDigestV1.model_validate(payload)

    grounded_errors = semantic_validate(digest, _context())
    interim_errors = semantic_validate(
        digest,
        _context(claim_grounded_mode=False),
    )

    assert grounded_errors == [
        "latent_concepts[0].supporting_claim_ids: at least one supporting "
        "claim is required in claim-grounded mode"
    ]
    assert interim_errors == []


def test_motif_requires_two_frames():
    payload = _payload()
    payload["motif_proposals"][0]["frame_sequence"] = ["MF07"]

    errors = semantic_validate(SemanticDigestV1.model_validate(payload), _context())

    assert errors == [
        "motif_proposals[0].frame_sequence: at least 2 frames are required"
    ]


def test_motif_frames_must_be_proposed_or_externally_validated():
    payload = _payload()
    payload["frame_proposals"] = [payload["frame_proposals"][0]]
    digest = SemanticDigestV1.model_validate(payload)

    errors = semantic_validate(digest, _context())
    accepted = semantic_validate(
        digest,
        _context(validated_frame_ids=("MF15",)),
    )

    assert errors == [
        "motif_proposals[0].frame_sequence[1]: frame 'MF15' is not present "
        "in proposed or validated frames"
    ]
    assert accepted == []


def test_rejected_frame_proposal_does_not_authorize_a_motif_reference():
    payload = _payload()
    payload["frame_proposals"][1]["assignment_state"] = "rejected"

    errors = semantic_validate(SemanticDigestV1.model_validate(payload), _context())

    assert errors == [
        "motif_proposals[0].frame_sequence[1]: frame 'MF15' is not present "
        "in proposed or validated frames"
    ]


@pytest.mark.parametrize(
    "proposal_field",
    ["domain_proposals", "frame_proposals", "latent_concepts"],
)
def test_llm_proposal_cannot_mark_itself_validated(proposal_field):
    payload = _payload()
    payload[proposal_field][0]["assignment_state"] = "validated"

    errors = semantic_validate(SemanticDigestV1.model_validate(payload), _context())

    assert (
        f"{proposal_field}[0].assignment_state: LLM proposal cannot mark "
        "itself 'validated'"
    ) in errors


def test_source_observed_bypass_is_rejected_semantically():
    digest = _digest()
    proposal = digest.frame_proposals[0].model_copy(
        update={"assignment_state": "source_observed"}
    )
    digest = digest.model_copy(
        update={"frame_proposals": [proposal, *digest.frame_proposals[1:]]}
    )

    errors = semantic_validate(digest, _context())

    assert errors[0] == (
        "frame_proposals[0].assignment_state: LLM proposal cannot mark "
        "itself 'source_observed'"
    )


def test_claim_cross_link_cannot_point_to_digest_itself():
    payload = _payload()
    payload["exceptions"][0]["supporting_claim_ids"] = ["parent:one"]

    errors = semantic_validate(SemanticDigestV1.model_validate(payload), _context())

    assert errors == [
        "exceptions[0].supporting_claim_ids[0]: cross-link 'parent:one' "
        "points to the digest itself"
    ]


def test_domain_cross_link_cannot_point_to_digest_itself():
    payload = _payload()
    payload["domain_proposals"][0]["registry_id"] = "parent:one"

    errors = semantic_validate(SemanticDigestV1.model_validate(payload), _context())

    assert errors == [
        "domain_proposals[0].registry_id: cross-link 'parent:one' points "
        "to the digest itself"
    ]


def test_explicit_artifact_self_reference_is_rejected():
    payload = _payload()
    payload["underlying_meanings"][0]["supporting_claim_ids"] = ["digest:one"]

    errors = semantic_validate(
        SemanticDigestV1.model_validate(payload),
        _context(self_reference_ids=("digest:one",)),
    )

    assert errors == [
        "underlying_meanings[0].supporting_claim_ids[0]: cross-link "
        "'digest:one' points to the digest itself"
    ]


def test_context_rejects_duplicate_claim_ids_and_unknown_validated_frames():
    with pytest.raises(ValueError, match="claim IDs must be unique"):
        SemanticValidationContext(
            parent_id="parent:one",
            claims=(
                ClaimScope("claim:one", "parent:one"),
                ClaimScope("claim:one", "parent:two"),
            ),
            domain_registry_ids=frozenset({"D01"}),
        )

    with pytest.raises(ValueError, match="unknown IDs"):
        SemanticValidationContext(
            parent_id="parent:one",
            claims=(),
            domain_registry_ids=frozenset({"D01"}),
            validated_frame_ids=frozenset({"MF17"}),
        )

    with pytest.raises(TypeError, match="tuple of ClaimScope"):
        SemanticValidationContext(
            parent_id="parent:one",
            claims=[],  # type: ignore[arg-type]
            domain_registry_ids=frozenset({"D01"}),
        )


def test_validator_rejects_untyped_inputs_at_its_boundary():
    with pytest.raises(TypeError, match="SemanticDigestV1"):
        semantic_validate(_payload(), _context())  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="SemanticValidationContext"):
        semantic_validate(_digest(), {})  # type: ignore[arg-type]
