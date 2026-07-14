"""Provider-neutral semantic observation and claim-candidate contracts.

These models are the first executable slice of
``polymath.artifact_envelope.v1``.  They intentionally stop at candidate
claims: an extractor may observe text and propose a claim, but it cannot mark
that claim asserted without the later evidence/registry validator.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


UNIT_SEPARATOR = b"\x1f"


def canonical_json(value: Any) -> str:
    """Serialize a hash input with a stable, language-portable JSON form."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def domain_hash(domain_tag: str, value: Any) -> str:
    """Hash one canonical value inside an explicit identity namespace."""

    digest = hashlib.sha256(
        domain_tag.encode("utf-8")
        + UNIT_SEPARATOR
        + canonical_json(value).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvidenceRef(StrictModel):
    # Quotes are coordinate-bearing source bytes. Trimming them would break
    # both exact round trips and the quote hash.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    evidence_ref_id: str
    source_version_id: str
    hierarchy_node_id: str
    coordinate_system: Literal["chunk_char", "record_field", "media_offset"]
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1)
    quote_hash: str

    @model_validator(mode="after")
    def validate_bounds_and_hash(self) -> "EvidenceRef":
        if self.end <= self.start:
            raise ValueError("evidence end must be greater than start")
        expected = domain_hash("evidence-quote", self.quote)
        if self.quote_hash != expected:
            raise ValueError("quote_hash does not match the exact quote")
        return self


def make_evidence_ref(
    *,
    text: str,
    start: int,
    end: int,
    source_version_id: str,
    hierarchy_node_id: str,
) -> EvidenceRef:
    """Create a chunk-local evidence reference with an exact round trip."""

    if not (0 <= start < end <= len(text)):
        raise ValueError("evidence offsets are outside the durable child text")
    quote = text[start:end]
    quote_hash = domain_hash("evidence-quote", quote)
    identity = {
        "source_version_id": source_version_id,
        "hierarchy_node_id": hierarchy_node_id,
        "coordinate_system": "chunk_char",
        "start": start,
        "end": end,
        "quote_hash": quote_hash,
    }
    return EvidenceRef(
        evidence_ref_id=f"evidence:{domain_hash('evidence-ref', identity).split(':', 1)[1]}",
        source_version_id=source_version_id,
        hierarchy_node_id=hierarchy_node_id,
        coordinate_system="chunk_char",
        start=start,
        end=end,
        quote=quote,
        quote_hash=quote_hash,
    )


class SpanObservation(StrictModel):
    # ``text`` must remain byte-for-byte aligned with start/end coordinates.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    observation_id: str
    kind: Literal["subject", "predicate", "object", "entity", "concept"]
    label: str = Field(min_length=1)
    text: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    producer: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> "SpanObservation":
        if self.end <= self.start:
            raise ValueError("span end must be greater than start")
        return self


class PredicateObservation(StrictModel):
    observation_id: str
    predicate_span_id: str
    predicate_lemma: str = Field(min_length=1)
    subject_span_ids: list[str] = Field(default_factory=list)
    object_span_ids: list[str] = Field(default_factory=list)
    evidence_ref_id: str
    producer: str = Field(min_length=1)


class QualifierObservation(StrictModel):
    # Qualifier cues also retain exact source coordinates.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    observation_id: str
    target_observation_id: str
    kind: Literal[
        "modal",
        "negation",
        "condition",
        "exception",
        "attribution",
        "comparison",
        "causal",
        "temporal",
    ]
    cue: str = Field(min_length=1)
    normalized_value: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    producer: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> "QualifierObservation":
        if self.end <= self.start:
            raise ValueError("qualifier end must be greater than start")
        return self


class ObservationBundle(StrictModel):
    bundle_id: str
    schema_version: Literal[
        "polymath.observation_bundle.v1"
    ] = "polymath.observation_bundle.v1"
    source_version_id: str
    hierarchy_node_id: str
    text_hash: str
    text_length: int = Field(ge=0)
    producer: str
    producer_version: str
    recipe_hash: str
    spans: list[SpanObservation] = Field(default_factory=list)
    predicates: list[PredicateObservation] = Field(default_factory=list)
    qualifiers: list[QualifierObservation] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    validation_drops: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "ObservationBundle":
        span_ids = {item.observation_id for item in self.spans}
        predicate_ids = {item.observation_id for item in self.predicates}
        evidence_ids = {item.evidence_ref_id for item in self.evidence_refs}
        if len(span_ids) != len(self.spans):
            raise ValueError("span observation IDs must be unique")
        if len(predicate_ids) != len(self.predicates):
            raise ValueError("predicate observation IDs must be unique")
        for predicate in self.predicates:
            referenced = {
                predicate.predicate_span_id,
                *predicate.subject_span_ids,
                *predicate.object_span_ids,
            }
            if not referenced <= span_ids:
                raise ValueError("predicate references an unknown span")
            if predicate.evidence_ref_id not in evidence_ids:
                raise ValueError("predicate references unknown evidence")
        for qualifier in self.qualifiers:
            if qualifier.target_observation_id not in predicate_ids:
                raise ValueError("qualifier references an unknown predicate")
        return self


class ClaimArgumentCandidate(StrictModel):
    role: Literal["subject", "object"]
    surface: str = Field(min_length=1)
    span_observation_id: str
    evidence_ref_id: str


class ClaimAssertionCandidate(StrictModel):
    candidate_id: str
    schema_version: Literal[
        "polymath.claim_candidate.v1"
    ] = "polymath.claim_candidate.v1"
    proposition_text: str = Field(min_length=1)
    canonical_proposition: str = Field(min_length=1)
    claim_type: Literal[
        "definition",
        "description_or_observation",
        "association",
        "causal",
        "comparison_or_contrast",
        "prediction",
        "recommendation_or_procedure",
        "normative",
        "argument_or_inference",
    ]
    predicate_surface: str = Field(min_length=1)
    predicate_lemma: str = Field(min_length=1)
    arguments: list[ClaimArgumentCandidate] = Field(default_factory=list)
    polarity: Literal["affirmed", "negated", "mixed"]
    modal_force: Literal[
        "asserted", "possible", "probable", "predicted", "recommended", "required"
    ]
    assertion_mode: Literal["reported", "attributed", "hypothetical"]
    conditions: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    evidence_ref_ids: list[str] = Field(min_length=1)
    producer: str
    knowledge_status: Literal["candidate"] = "candidate"
    validation_status: Literal["candidate", "accepted", "rejected"] = "candidate"

    @model_validator(mode="after")
    def forbid_unvalidated_assertion(self) -> "ClaimAssertionCandidate":
        if self.validation_status == "accepted":
            raise ValueError(
                "extractors cannot mark a claim accepted; use the assertion validator"
            )
        return self
