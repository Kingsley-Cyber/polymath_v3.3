"""Deterministic semantic validation for ``SemanticDigestV1``.

Pydantic proves structure. This module proves that a structurally valid digest
does not escape the supplied parent packet, invent authority, or reference
unsupported semantic objects. It is pure: callers receive precise,
location-indexed errors and decide whether T4.3 performs the one allowed repair
or dead-letters the output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from models.semantic_digest import SemanticDigestV1


FRAME_IDS = frozenset(f"MF{number:02d}" for number in range(1, 17))
_FORBIDDEN_LLM_STATES = frozenset({"source_observed", "validated"})
_ELIGIBLE_PROPOSED_FRAME_STATES = frozenset(
    {"candidate", "corroborated", "unresolved"}
)


@dataclass(frozen=True)
class ClaimScope:
    claim_id: str
    parent_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.claim_id, str) or not self.claim_id:
            raise ValueError("claim_id must be non-empty")
        if not isinstance(self.parent_id, str) or not self.parent_id:
            raise ValueError("claim parent_id must be non-empty")


@dataclass(frozen=True)
class SemanticValidationContext:
    """Immutable evidence/registry scope supplied with one parent packet."""

    parent_id: str
    claims: tuple[ClaimScope, ...]
    domain_registry_ids: frozenset[str]
    validated_frame_ids: frozenset[str] = field(default_factory=frozenset)
    claim_grounded_mode: bool = True
    self_reference_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.parent_id, str) or not self.parent_id:
            raise ValueError("context parent_id must be non-empty")
        if not isinstance(self.claims, tuple) or not all(
            isinstance(claim, ClaimScope) for claim in self.claims
        ):
            raise TypeError("context claims must be a tuple of ClaimScope values")
        for field_name in (
            "domain_registry_ids",
            "validated_frame_ids",
            "self_reference_ids",
        ):
            values = getattr(self, field_name)
            if not isinstance(values, frozenset) or not all(
                isinstance(value, str) for value in values
            ):
                raise TypeError(f"{field_name} must be a frozenset of strings")
        if not isinstance(self.claim_grounded_mode, bool):
            raise TypeError("claim_grounded_mode must be bool")
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("context claim IDs must be unique")
        unknown_frames = self.validated_frame_ids - FRAME_IDS
        if unknown_frames:
            raise ValueError(
                "validated_frame_ids contain unknown IDs: "
                f"{sorted(unknown_frames)}"
            )

    @property
    def claim_parent_by_id(self) -> dict[str, str]:
        return {claim.claim_id: claim.parent_id for claim in self.claims}

    @classmethod
    def from_owner_registries(
        cls,
        *,
        parent_id: str,
        claims: Iterable[ClaimScope],
        validated_frame_ids: Iterable[str] = (),
        claim_grounded_mode: bool = True,
        self_reference_ids: Iterable[str] = (),
    ) -> "SemanticValidationContext":
        """Build context from the checked-in immutable domain registry."""

        from models.registry_loader import load_all

        domain_ids = frozenset(
            row["domain_id"] for row in load_all()["domain"]["domains"]
        )
        return cls(
            parent_id=parent_id,
            claims=tuple(claims),
            domain_registry_ids=domain_ids,
            validated_frame_ids=frozenset(validated_frame_ids),
            claim_grounded_mode=claim_grounded_mode,
            self_reference_ids=frozenset(self_reference_ids),
        )


def _validate_claim_references(
    claim_ids: Sequence[str],
    *,
    path: str,
    context: SemanticValidationContext,
    self_ids: frozenset[str],
    errors: list[str],
) -> None:
    claim_parent_by_id = context.claim_parent_by_id
    for index, claim_id in enumerate(claim_ids):
        location = f"{path}[{index}]"
        if claim_id in self_ids:
            errors.append(
                f"{location}: cross-link {claim_id!r} points to the digest itself"
            )
            continue
        if claim_id not in claim_parent_by_id:
            errors.append(f"{location}: unknown claim_id {claim_id!r}")
            continue
        owner_parent_id = claim_parent_by_id[claim_id]
        if owner_parent_id != context.parent_id:
            errors.append(
                f"{location}: claim_id {claim_id!r} belongs to parent "
                f"{owner_parent_id!r}, not supplied parent {context.parent_id!r}"
            )


def _validate_llm_assignment_state(
    assignment_state: str,
    *,
    path: str,
    errors: list[str],
) -> None:
    if assignment_state in _FORBIDDEN_LLM_STATES:
        errors.append(
            f"{path}: LLM proposal cannot mark itself {assignment_state!r}"
        )


def semantic_validate(
    digest: SemanticDigestV1,
    context: SemanticValidationContext,
) -> list[str]:
    """Return deterministic, location-indexed semantic errors.

    An empty list means that the digest may proceed to the assignment compiler;
    it does not itself activate, persist, or promote any proposal.
    """

    if not isinstance(digest, SemanticDigestV1):
        raise TypeError("semantic_validate requires SemanticDigestV1")
    if not isinstance(context, SemanticValidationContext):
        raise TypeError("semantic_validate requires SemanticValidationContext")

    errors: list[str] = []
    self_ids = frozenset({digest.parent_id, *context.self_reference_ids})

    if digest.parent_id != context.parent_id:
        errors.append(
            f"parent_id: digest parent {digest.parent_id!r} does not match "
            f"supplied parent {context.parent_id!r}"
        )

    for index, statement in enumerate(digest.underlying_meanings):
        _validate_claim_references(
            statement.supporting_claim_ids,
            path=f"underlying_meanings[{index}].supporting_claim_ids",
            context=context,
            self_ids=self_ids,
            errors=errors,
        )

    for index, proposal in enumerate(digest.domain_proposals):
        base = f"domain_proposals[{index}]"
        _validate_llm_assignment_state(
            proposal.assignment_state,
            path=f"{base}.assignment_state",
            errors=errors,
        )
        if proposal.registry_id in self_ids:
            errors.append(
                f"{base}.registry_id: cross-link {proposal.registry_id!r} "
                "points to the digest itself"
            )
        elif (
            proposal.registry_id not in context.domain_registry_ids
            and proposal.assignment_state != "candidate"
        ):
            errors.append(
                f"{base}.registry_id: unknown domain registry id "
                f"{proposal.registry_id!r} requires assignment_state='candidate'"
            )
        _validate_claim_references(
            proposal.supporting_claim_ids,
            path=f"{base}.supporting_claim_ids",
            context=context,
            self_ids=self_ids,
            errors=errors,
        )

    eligible_proposed_frames: set[str] = set()
    for index, proposal in enumerate(digest.frame_proposals):
        base = f"frame_proposals[{index}]"
        _validate_llm_assignment_state(
            proposal.assignment_state,
            path=f"{base}.assignment_state",
            errors=errors,
        )
        if proposal.frame_id not in FRAME_IDS:
            errors.append(
                f"{base}.frame_id: unknown frame id {proposal.frame_id!r}; "
                "expected MF01-MF16"
            )
        elif proposal.frame_id in self_ids:
            errors.append(
                f"{base}.frame_id: cross-link {proposal.frame_id!r} points "
                "to the digest itself"
            )
        if not proposal.supporting_claim_ids:
            errors.append(
                f"{base}.supporting_claim_ids: at least one supporting claim "
                "is required"
            )
        if (
            proposal.frame_id in FRAME_IDS
            and proposal.assignment_state in _ELIGIBLE_PROPOSED_FRAME_STATES
        ):
            eligible_proposed_frames.add(proposal.frame_id)
        _validate_claim_references(
            proposal.supporting_claim_ids,
            path=f"{base}.supporting_claim_ids",
            context=context,
            self_ids=self_ids,
            errors=errors,
        )

    for index, proposal in enumerate(digest.latent_concepts):
        base = f"latent_concepts[{index}]"
        _validate_llm_assignment_state(
            proposal.assignment_state,
            path=f"{base}.assignment_state",
            errors=errors,
        )
        if context.claim_grounded_mode and not proposal.supporting_claim_ids:
            errors.append(
                f"{base}.supporting_claim_ids: at least one supporting claim "
                "is required in claim-grounded mode"
            )
        _validate_claim_references(
            proposal.supporting_claim_ids,
            path=f"{base}.supporting_claim_ids",
            context=context,
            self_ids=self_ids,
            errors=errors,
        )

    available_motif_frames = (
        eligible_proposed_frames | set(context.validated_frame_ids)
    )
    for index, proposal in enumerate(digest.motif_proposals):
        base = f"motif_proposals[{index}]"
        if len(proposal.frame_sequence) < 2:
            errors.append(f"{base}.frame_sequence: at least 2 frames are required")
        for frame_index, frame_id in enumerate(proposal.frame_sequence):
            location = f"{base}.frame_sequence[{frame_index}]"
            if frame_id not in FRAME_IDS:
                errors.append(
                    f"{location}: unknown frame id {frame_id!r}; expected MF01-MF16"
                )
            elif frame_id in self_ids:
                errors.append(
                    f"{location}: cross-link {frame_id!r} points to the digest itself"
                )
            elif frame_id not in available_motif_frames:
                errors.append(
                    f"{location}: frame {frame_id!r} is not present in proposed "
                    "or validated frames"
                )
        _validate_claim_references(
            proposal.supporting_claim_ids,
            path=f"{base}.supporting_claim_ids",
            context=context,
            self_ids=self_ids,
            errors=errors,
        )

    for field_name in ("conditions", "exceptions"):
        for index, statement in enumerate(getattr(digest, field_name)):
            _validate_claim_references(
                statement.supporting_claim_ids,
                path=f"{field_name}[{index}].supporting_claim_ids",
                context=context,
                self_ids=self_ids,
                errors=errors,
            )

    return errors
