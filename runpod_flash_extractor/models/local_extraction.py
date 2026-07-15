"""Strict provider-neutral contract for deterministic child extraction.

The five models and their field names are the owner-delivered
``LocalExtractionV1`` boundary.  Vocabulary aliases mirror the immutable
``extraction_vocabularies.v1.json`` snapshot; golden tests make drift between
the Python type contract and registry data fail closed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


EntityType = Literal[
    "PERSON",
    "ORGANIZATION",
    "PLACE",
    "PRODUCT",
    "DOCUMENT",
    "AGENT",
    "GROUP",
    "SYSTEM",
    "PROCESS",
    "BEHAVIOR",
    "STATE",
    "QUALITY",
    "CONCEPT",
    "METHOD",
    "SIGNAL",
    "BASELINE",
    "METRIC",
    "RESOURCE",
    "CONSTRAINT",
    "GOAL",
    "INTERVENTION",
    "OUTCOME",
    "CONDITION",
    "POPULATION",
    "TIME_PATTERN",
]
PredicateType = Literal[
    "CAUSES",
    "INFLUENCES",
    "INCREASES",
    "DECREASES",
    "UPDATES",
    "SIGNALS",
    "MEASURES",
    "COMPARES_AGAINST",
    "ENABLES",
    "INHIBITS",
    "REQUIRES",
    "CONSTRAINS",
    "RESULTS_IN",
    "APPLIES_UNDER",
    "PART_OF",
    "USED_FOR",
    "ASSOCIATED_WITH",
]
Modality = Literal[
    "asserted",
    "possible",
    "probable",
    "necessary",
    "recommended",
    "hypothetical",
]
Polarity = Literal["positive", "negative"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EntityMention(StrictModel):
    mention_id: str
    text: str
    entity_type: EntityType
    start_char: int
    end_char: int
    canonical_label: str
    confidence: float

    @model_validator(mode="after")
    def validate_span_and_confidence(self) -> "EntityMention":
        if self.end_char <= self.start_char or self.start_char < 0:
            raise ValueError("entity mention offsets must form a positive span")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("entity mention confidence must be between 0 and 1")
        return self


class PredicateMention(StrictModel):
    predicate_id: str
    surface_text: str
    lemma: str
    normalized_predicate: PredicateType
    start_char: int
    end_char: int
    negated: bool
    modality: Modality
    confidence: float

    @model_validator(mode="after")
    def validate_span_and_confidence(self) -> "PredicateMention":
        if self.end_char <= self.start_char or self.start_char < 0:
            raise ValueError("predicate mention offsets must form a positive span")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("predicate mention confidence must be between 0 and 1")
        return self


class RelationCandidate(StrictModel):
    relation_id: str
    source_mention_id: str
    predicate_id: str
    target_mention_id: str
    relation_type: PredicateType
    condition_mention_ids: list[str]
    temporal_mention_ids: list[str]
    evidence_sentence_ids: list[str]
    confidence: float

    @model_validator(mode="after")
    def validate_confidence(self) -> "RelationCandidate":
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("relation confidence must be between 0 and 1")
        return self


class LocalExtractionV1(StrictModel):
    schema_version: Literal["local_extraction.v1"]
    document_id: str
    child_id: str
    sentence_ids: list[str]
    entities: list[EntityMention]
    predicates: list[PredicateMention]
    relations: list[RelationCandidate]
    unresolved_spans: list[str]

    @model_validator(mode="after")
    def validate_reference_closure(self) -> "LocalExtractionV1":
        mention_ids = [item.mention_id for item in self.entities]
        predicate_ids = [item.predicate_id for item in self.predicates]
        relation_ids = [item.relation_id for item in self.relations]
        if len(mention_ids) != len(set(mention_ids)):
            raise ValueError("entity mention IDs must be unique")
        if len(predicate_ids) != len(set(predicate_ids)):
            raise ValueError("predicate IDs must be unique")
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("relation IDs must be unique")
        if len(self.sentence_ids) != len(set(self.sentence_ids)):
            raise ValueError("sentence IDs must be unique")

        mentions = set(mention_ids)
        predicates = {item.predicate_id: item for item in self.predicates}
        sentences = set(self.sentence_ids)
        for relation in self.relations:
            if relation.source_mention_id not in mentions:
                raise ValueError("relation references an unknown source mention")
            if relation.target_mention_id not in mentions:
                raise ValueError("relation references an unknown target mention")
            if relation.predicate_id not in predicates:
                raise ValueError("relation references an unknown predicate")
            qualifier_mentions = {
                *relation.condition_mention_ids,
                *relation.temporal_mention_ids,
            }
            if not qualifier_mentions <= mentions:
                raise ValueError("relation qualifier references an unknown mention")
            if not set(relation.evidence_sentence_ids) <= sentences:
                raise ValueError("relation references an unknown evidence sentence")
            if (
                relation.relation_type
                != predicates[relation.predicate_id].normalized_predicate
            ):
                raise ValueError("relation type must match its normalized predicate")
        return self
