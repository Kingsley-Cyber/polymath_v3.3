"""Deterministic ObservationBundle + LocalExtractionV1 claim compiler.

The compiler is intentionally local and candidate-only.  It preserves
untyped predicate surfaces, attaches zero-shot relations only when their
direction agrees with spaCy arguments, and emits only explicit RESULTS_IN
links.  It performs no provider call, persistence, registry expansion, domain
assignment, or frame assignment.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import re
from typing import Iterable, Literal, cast

from models.claim_record import (
    AssertionMode,
    ClaimArgumentV1,
    ClaimAssertionSourceV1,
    ClaimAssertionTemporalV1,
    ClaimAssertionV1,
    ClaimCompilationV1,
    ClaimLinkV1,
    ClaimRecordV1,
    ClaimType,
)
from models.hash_taxonomy import canonical_json_v1, namespace_hash
from models.identifier_recipes import claim_id as make_claim_id
from models.local_extraction import EntityMention, LocalExtractionV1, PredicateMention
from models.registry_loader import normalize_predicate_lemma
from models.semantic_artifacts import (
    EvidenceRef,
    ObservationBundle,
    PredicateObservation,
    QualifierObservation,
    SpanObservation,
)
from services.ingestion.semantic_observations import (
    _claim_type,
    _local_modality,
    load_normalization_identity,
    normalized_cue,
)


COMPILER_VERSION = "claim_compiler.v2"
_RESULT_PHRASE_RE = re.compile(
    r"^(?:result(?:s|ed|ing)?\s+in|lead(?:s|ing|ed)?\s+to)\b",
    re.IGNORECASE,
)
_DISCOURSE_RESULT_RE = re.compile(
    r"^(?:as\s+a\s+result|consequently|therefore|thus)\b[\s,:;-]*",
    re.IGNORECASE,
)


def _parse_unresolved_span(value: str) -> tuple[int, int, str]:
    start_text, separator, remainder = value.partition(":")
    end_text, separator_two, surface = remainder.partition(":")
    if not separator or not separator_two or not surface:
        raise ValueError("unresolved spans must use start:end:surface")
    try:
        start, end = int(start_text), int(end_text)
    except ValueError as exc:
        raise ValueError("unresolved span offsets must be integers") from exc
    if start < 0 or end <= start:
        raise ValueError("unresolved span offsets must form a positive span")
    return start, end, surface


def _predicate_span(
    predicate: PredicateObservation, spans: dict[str, SpanObservation]
) -> SpanObservation:
    span = spans.get(predicate.predicate_span_id)
    if span is None:
        raise ValueError("predicate observation references an unknown span")
    return span


def _validate_and_index_predicates(
    bundle: ObservationBundle,
    extraction: LocalExtractionV1,
    spans: dict[str, SpanObservation],
) -> tuple[dict[str, PredicateMention], set[str]]:
    observations_by_coordinate: dict[tuple[int, int, str], PredicateObservation] = {}
    for observation in bundle.predicates:
        span = _predicate_span(observation, spans)
        key = (span.start, span.end, span.text)
        if key in observations_by_coordinate:
            raise ValueError("predicate observation coordinates must be unique")
        observations_by_coordinate[key] = observation

    typed: dict[str, PredicateMention] = {}
    covered: set[str] = set()
    for mention in extraction.predicates:
        key = (mention.start_char, mention.end_char, mention.surface_text)
        observation = observations_by_coordinate.get(key)
        if observation is None:
            raise ValueError("typed predicate has no exact observation coordinate")
        if observation.predicate_lemma != mention.lemma:
            raise ValueError("typed predicate lemma disagrees with its observation")
        resolved = normalize_predicate_lemma(mention.lemma)
        if (
            resolved is None
            or resolved["predicate_type"] != mention.normalized_predicate
        ):
            raise ValueError(
                "typed predicate disagrees with the active normalization registry"
            )
        if observation.observation_id in covered:
            raise ValueError("predicate observations cannot be typed twice")
        typed[observation.observation_id] = mention
        covered.add(observation.observation_id)

    unresolved: set[str] = set()
    for value in extraction.unresolved_spans:
        key = _parse_unresolved_span(value)
        observation = observations_by_coordinate.get(key)
        if observation is None:
            raise ValueError("unresolved predicate has no exact observation coordinate")
        if observation.observation_id in covered:
            raise ValueError("predicate cannot be both typed and unresolved")
        unresolved.add(observation.observation_id)
        covered.add(observation.observation_id)

    observed_ids = {item.observation_id for item in bundle.predicates}
    if covered != observed_ids:
        raise ValueError(
            "local extraction must account for every predicate observation"
        )
    return typed, unresolved


def _candidate_entities(
    span: SpanObservation, entities: Iterable[EntityMention]
) -> list[EntityMention]:
    candidates = [
        item
        for item in entities
        if (
            span.start <= item.start_char < item.end_char <= span.end
            or item.start_char <= span.start < span.end <= item.end_char
        )
    ]
    exact = [
        item
        for item in candidates
        if item.start_char == span.start and item.end_char == span.end
    ]
    if len(exact) == 1:
        return exact
    return candidates if len(candidates) == 1 else []


def _arguments(
    predicate: PredicateObservation,
    spans: dict[str, SpanObservation],
    entities: list[EntityMention],
) -> list[ClaimArgumentV1]:
    rows: list[ClaimArgumentV1] = []
    for role, span_ids in (
        ("subject", predicate.subject_span_ids),
        ("object", predicate.object_span_ids),
    ):
        for span_id in span_ids:
            span = spans.get(span_id)
            if span is None:
                raise ValueError("predicate argument references an unknown span")
            bound = _candidate_entities(span, entities)
            filler_kind = "entity_mention" if bound else "span_observation"
            filler_ref = bound[0].mention_id if bound else span.observation_id
            rows.append(
                ClaimArgumentV1(
                    role=role,
                    filler_kind=filler_kind,
                    filler_ref=filler_ref,
                    span_observation_id=span.observation_id,
                    surface=span.text,
                    start_char=span.start,
                    end_char=span.end,
                    evidence_sentence_id=predicate.evidence_ref_id,
                )
            )
    return rows


def _assertion_mode(
    qualifiers: list[QualifierObservation], modality: str
) -> AssertionMode:
    if any(item.kind == "attribution" for item in qualifiers):
        return "attributed"
    if modality == "hypothetical" or any(
        item.kind == "condition" for item in qualifiers
    ):
        return "hypothetical"
    return "reported"


def _unique_cues(qualifiers: Iterable[QualifierObservation], kind: str) -> list[str]:
    return sorted({item.cue for item in qualifiers if item.kind == kind})


def _canonical_proposition(
    *,
    arguments: list[ClaimArgumentV1],
    polarity: str,
    modality: str,
    predicate_lemma: str,
    normalized_predicate: str | None,
    conditions: list[str],
    exceptions: list[str],
    temporal_cues: list[str],
) -> str:
    subjects = " | ".join(
        normalized_cue(item.surface) for item in arguments if item.role == "subject"
    )
    objects = " | ".join(
        normalized_cue(item.surface) for item in arguments if item.role == "object"
    )
    predicate = normalized_predicate or f"UNTYPED[{predicate_lemma.lower()}]"
    return " ".join(
        part
        for part in (
            subjects,
            polarity.upper(),
            modality.upper(),
            predicate,
            objects,
            "IF " + " | ".join(map(normalized_cue, conditions)) if conditions else "",
            "EXCEPT " + " | ".join(map(normalized_cue, exceptions))
            if exceptions
            else "",
            "WHEN " + " | ".join(map(normalized_cue, temporal_cues))
            if temporal_cues
            else "",
        )
        if part
    )


def _accepted_relation_ids(
    *,
    predicate: PredicateObservation,
    typed: PredicateMention | None,
    arguments: list[ClaimArgumentV1],
    extraction: LocalExtractionV1,
) -> tuple[list[str], list[str]]:
    if typed is None:
        return [], []
    subjects = {
        item.filler_ref
        for item in arguments
        if item.role == "subject" and item.filler_kind == "entity_mention"
    }
    objects = {
        item.filler_ref
        for item in arguments
        if item.role == "object" and item.filler_kind == "entity_mention"
    }
    accepted: list[str] = []
    rejected: list[str] = []
    for relation in extraction.relations:
        if relation.predicate_id != typed.predicate_id:
            continue
        agrees = (
            relation.source_mention_id in subjects
            and relation.target_mention_id in objects
            and predicate.evidence_ref_id in relation.evidence_sentence_ids
        )
        (accepted if agrees else rejected).append(relation.relation_id)
    return sorted(accepted), sorted(rejected)


def _claim_signature(
    *,
    predicate_observation_id: str,
    claim_type: str,
    canonical_proposition: str,
    predicate_lemma: str,
    normalized_predicate: str | None,
    arguments: list[ClaimArgumentV1],
    polarity: str,
    modality: str,
    assertion_mode: str,
    conditions: list[str],
    exceptions: list[str],
    temporal_cues: list[str],
) -> str:
    return canonical_json_v1(
        {
            "predicate_observation_id": predicate_observation_id,
            "claim_type": claim_type,
            "canonical_proposition": canonical_proposition,
            "predicate_lemma": predicate_lemma,
            "normalized_predicate": normalized_predicate,
            "arguments": [
                {
                    "role": item.role,
                    "filler_kind": item.filler_kind,
                    "filler_ref": item.filler_ref,
                }
                for item in arguments
            ],
            "polarity": polarity,
            "modality": modality,
            "assertion_mode": assertion_mode,
            "conditions": conditions,
            "exceptions": exceptions,
            "temporal_cues": temporal_cues,
        }
    )


def _link_evidence(
    source: ClaimRecordV1,
    target: ClaimRecordV1,
    evidence_order: dict[str, int],
) -> list[str]:
    return sorted(
        set(source.evidence_sentence_ids + target.evidence_sentence_ids),
        key=evidence_order.__getitem__,
    )


def _endpoint_continuity(source: ClaimRecordV1, target: ClaimRecordV1) -> bool:
    source_objects = {
        (item.filler_kind, item.filler_ref)
        for item in source.arguments
        if item.role == "object"
    }
    target_subjects = {
        (item.filler_kind, item.filler_ref)
        for item in target.arguments
        if item.role == "subject"
    }
    if source_objects & target_subjects:
        return True
    source_surfaces = {
        normalized_cue(item.surface)
        for item in source.arguments
        if item.role == "object"
    }
    target_surfaces = {
        normalized_cue(item.surface)
        for item in target.arguments
        if item.role == "subject"
    }
    return bool(source_surfaces & target_surfaces)


def _make_link(
    *,
    source: ClaimRecordV1,
    target: ClaimRecordV1,
    evidence_sentence_ids: list[str],
    derivation_method: Literal["dependency_rule", "discourse_rule"],
    triggering_connective: str,
    rule_id: Literal[
        "claim_results_in.explicit_dependency.v1",
        "claim_results_in.explicit_discourse_continuity.v1",
    ],
) -> ClaimLinkV1:
    identity = {
        "source_claim_id": source.claim_id,
        "relation_type": "RESULTS_IN",
        "target_claim_id": target.claim_id,
        "evidence_sentence_ids": evidence_sentence_ids,
        "derivation_method": derivation_method,
        "triggering_connective": triggering_connective,
        "rule_id": rule_id,
        "compiler_version": COMPILER_VERSION,
    }
    return ClaimLinkV1(
        schema_version="claim_link.v1",
        link_id="claim-link:"
        + namespace_hash("logical-artifact", identity).split(":", 1)[1],
        source_claim_id=source.claim_id,
        relation_type="RESULTS_IN",
        target_claim_id=target.claim_id,
        evidence_sentence_ids=evidence_sentence_ids,
        derivation_method=derivation_method,
        triggering_connective=triggering_connective,
        rule_id=rule_id,
        knowledge_status="candidate",
        validation_status="candidate",
    )


def _result_links(
    claims_with_positions: list[tuple[ClaimRecordV1, int, int]],
    evidence: dict[str, EvidenceRef],
    evidence_order: dict[str, int],
) -> tuple[list[ClaimLinkV1], int, int]:
    links: list[ClaimLinkV1] = []
    cross_sentence_candidates = 0
    cross_sentence_rejected = 0
    by_sentence: dict[str, list[tuple[ClaimRecordV1, int, int]]] = defaultdict(list)
    for row in claims_with_positions:
        by_sentence[row[0].evidence_sentence_ids[0]].append(row)

    for sentence_id, rows in by_sentence.items():
        sentence = evidence[sentence_id]
        ordered = sorted(rows, key=lambda row: (row[1], row[2], row[0].claim_id))
        for source_row, target_row in zip(ordered, ordered[1:]):
            target_offset = target_row[1] - sentence.start
            suffix = sentence.quote[max(0, target_offset) :]
            match = _RESULT_PHRASE_RE.match(suffix)
            if match is None:
                continue
            links.append(
                _make_link(
                    source=source_row[0],
                    target=target_row[0],
                    evidence_sentence_ids=[sentence_id],
                    derivation_method="dependency_rule",
                    triggering_connective=match.group(0),
                    rule_id="claim_results_in.explicit_dependency.v1",
                )
            )

    ordered_sentences = sorted(
        evidence.values(), key=lambda item: (item.start, item.end)
    )
    claims_by_sentence = {
        sentence_id: sorted(rows, key=lambda row: (row[1], row[2], row[0].claim_id))
        for sentence_id, rows in by_sentence.items()
    }
    for first, second in zip(ordered_sentences, ordered_sentences[1:]):
        first_claims = claims_by_sentence.get(first.evidence_ref_id, [])
        second_claims = claims_by_sentence.get(second.evidence_ref_id, [])
        if not first_claims or not second_claims:
            continue
        match = _DISCOURSE_RESULT_RE.match(second.quote.lstrip())
        if match is None:
            continue
        cross_sentence_candidates += 1
        source, target = first_claims[-1][0], second_claims[0][0]
        if not _endpoint_continuity(source, target):
            cross_sentence_rejected += 1
            continue
        links.append(
            _make_link(
                source=source,
                target=target,
                evidence_sentence_ids=_link_evidence(source, target, evidence_order),
                derivation_method="discourse_rule",
                triggering_connective=match.group(0).rstrip(" \t\r\n,:;-"),
                rule_id="claim_results_in.explicit_discourse_continuity.v1",
            )
        )

    unique = {item.link_id: item for item in links}
    return (
        [unique[key] for key in sorted(unique)],
        cross_sentence_candidates,
        cross_sentence_rejected,
    )


_MODAL_FORCE_FROM_LOCAL = {
    "asserted": "asserted",
    "possible": "possible",
    "probable": "probable",
    "necessary": "required",
    "recommended": "recommended",
    "hypothetical": "possible",
}


def project_claim_record_to_assertion(record: ClaimRecordV1) -> ClaimAssertionV1:
    """Project a candidate record into the owner-aligned assertion body."""

    return ClaimAssertionV1(
        schema_version="polymath.claim_assertion.v1",
        claim_id=record.claim_id,
        proposition_text=record.proposition_text,
        canonical_proposition=record.canonical_proposition,
        claim_type=record.claim_type,
        predicate_id=record.normalized_predicate,
        arguments=record.arguments,
        polarity="negated" if record.polarity == "negative" else "affirmed",
        modal_force=_MODAL_FORCE_FROM_LOCAL[record.modality],
        assertion_mode=record.assertion_mode,
        conditions=record.conditions,
        exceptions=record.exceptions,
        semantic_scope={
            "document_id": record.document_id,
            "child_id": record.child_id,
        },
        scope_hash=record.scope_hash,
        temporal=ClaimAssertionTemporalV1(
            cues=record.temporal_cues,
            valid_from=None,
            valid_to=None,
            reference_time=None,
            temporal_status="unresolved",
        ),
        evidence_refs=record.evidence_sentence_ids,
        evidence_episode_ids=[],
        domain_profile_id=None,
        frame_instance_ids=[],
        derivation_parent_ids=[],
        source_compilation=ClaimAssertionSourceV1(
            source_claim_record_id=record.claim_id,
            document_id=record.document_id,
            child_id=record.child_id,
            predicate_observation_id=record.predicate_observation_id,
            predicate_mention_id=record.predicate_id,
            predicate_surface=record.predicate_surface,
            predicate_lemma=record.predicate_lemma,
            normalized_predicate=record.normalized_predicate,
            typing_status=record.typing_status,
            local_polarity=record.polarity,
            local_modality=record.modality,
            source_relation_ids=record.source_relation_ids,
        ),
        knowledge_status="candidate",
        validation_status="candidate",
    )


def restore_claim_record_from_assertion(assertion: ClaimAssertionV1) -> ClaimRecordV1:
    """Prove that the deterministic projection loses no ClaimRecord fields."""

    source = assertion.source_compilation
    return ClaimRecordV1(
        schema_version="claim_record.v1",
        claim_id=assertion.claim_id,
        document_id=source.document_id,
        child_id=source.child_id,
        proposition_text=assertion.proposition_text,
        canonical_proposition=assertion.canonical_proposition,
        claim_type=assertion.claim_type,
        predicate_observation_id=source.predicate_observation_id,
        predicate_id=source.predicate_mention_id,
        predicate_surface=source.predicate_surface,
        predicate_lemma=source.predicate_lemma,
        normalized_predicate=source.normalized_predicate,
        typing_status=source.typing_status,
        arguments=assertion.arguments,
        polarity=source.local_polarity,
        modality=source.local_modality,
        assertion_mode=assertion.assertion_mode,
        conditions=assertion.conditions,
        exceptions=assertion.exceptions,
        temporal_cues=assertion.temporal.cues,
        evidence_sentence_ids=assertion.evidence_refs,
        source_relation_ids=source.source_relation_ids,
        scope_hash=assertion.scope_hash,
        knowledge_status="candidate",
        validation_status="candidate",
    )


def compile_claim_records_v1(
    *,
    bundle: ObservationBundle,
    extraction: LocalExtractionV1,
) -> ClaimCompilationV1:
    """Compile same-child observations into candidate-only atomic claims."""

    if extraction.document_id == "" or extraction.child_id == "":
        raise ValueError("local extraction ownership cannot be empty")
    if extraction.child_id != bundle.hierarchy_node_id:
        raise ValueError(
            "local extraction and observation bundle must share child scope"
        )
    expected_sentences = [item.evidence_ref_id for item in bundle.evidence_refs]
    if extraction.sentence_ids != expected_sentences:
        raise ValueError(
            "local extraction sentence IDs must exactly match observations"
        )

    spans = {item.observation_id: item for item in bundle.spans}
    evidence = {item.evidence_ref_id: item for item in bundle.evidence_refs}
    evidence_order = {
        item.evidence_ref_id: index for index, item in enumerate(bundle.evidence_refs)
    }
    qualifiers_by_target: dict[str, list[QualifierObservation]] = defaultdict(list)
    for qualifier in bundle.qualifiers:
        qualifiers_by_target[qualifier.target_observation_id].append(qualifier)
    typed_by_observation, unresolved = _validate_and_index_predicates(
        bundle, extraction, spans
    )

    normalization = load_normalization_identity()
    recipe = {
        "compiler": COMPILER_VERSION,
        "observation_recipe_hash": bundle.recipe_hash,
        "normalization_registry": normalization["registry"],
        "normalization_registry_version": normalization["version"],
        "normalization_registry_hash": normalization["hash"],
        "normalization_accounting": "typed_or_unresolved_exact_coordinate",
        "entity_binding": "unique_containment_or_exact.v1",
        "relation_policy": "dependency_direction_agreement_observation_only.v1",
        "result_link_rules": "explicit_result_phrase_and_continuity.v1",
        "knowledge_status": "candidate",
    }
    recipe_hash = namespace_hash("recipe", recipe)
    claims: list[ClaimRecordV1] = []
    claims_with_positions: list[tuple[ClaimRecordV1, int, int]] = []
    rejected_relations: set[str] = set()
    unresolved_coreference: set[str] = set()
    skipped_predicates: set[str] = set()

    for predicate in bundle.predicates:
        predicate_span = _predicate_span(predicate, spans)
        typed = typed_by_observation.get(predicate.observation_id)
        is_untyped = predicate.observation_id in unresolved
        arguments = _arguments(predicate, spans, extraction.entities)
        if typed is not None and not any(item.role == "subject" for item in arguments):
            skipped_predicates.add(predicate.observation_id)
            continue
        if not is_untyped and typed is None:
            raise ValueError("predicate accounting drifted during claim compilation")

        qualifiers = qualifiers_by_target[predicate.observation_id]
        modality = typed.modality if typed is not None else _local_modality(qualifiers)
        polarity = (
            "negative"
            if (
                typed.negated
                if typed is not None
                else any(item.kind == "negation" for item in qualifiers)
            )
            else "positive"
        )
        assertion_mode = _assertion_mode(qualifiers, modality)
        conditions = _unique_cues(qualifiers, "condition")
        exceptions = _unique_cues(qualifiers, "exception")
        temporal_cues = _unique_cues(qualifiers, "temporal")
        normalized_predicate = typed.normalized_predicate if typed is not None else None
        canonical = _canonical_proposition(
            arguments=arguments,
            polarity=polarity,
            modality=modality,
            predicate_lemma=predicate.predicate_lemma,
            normalized_predicate=normalized_predicate,
            conditions=conditions,
            exceptions=exceptions,
            temporal_cues=temporal_cues,
        )
        claim_type = cast(ClaimType, _claim_type(predicate, qualifiers))
        source_relation_ids, relation_rejections = _accepted_relation_ids(
            predicate=predicate,
            typed=typed,
            arguments=arguments,
            extraction=extraction,
        )
        rejected_relations.update(relation_rejections)
        scope = {
            "document_id": extraction.document_id,
            "child_id": extraction.child_id,
            "conditions": conditions,
            "exceptions": exceptions,
            "temporal_cues": temporal_cues,
        }
        scope_hash = namespace_hash("scope", scope)
        signature = _claim_signature(
            predicate_observation_id=predicate.observation_id,
            claim_type=claim_type,
            canonical_proposition=canonical,
            predicate_lemma=predicate.predicate_lemma,
            normalized_predicate=normalized_predicate,
            arguments=arguments,
            polarity=polarity,
            modality=modality,
            assertion_mode=assertion_mode,
            conditions=conditions,
            exceptions=exceptions,
            temporal_cues=temporal_cues,
        )
        evidence_ids = [predicate.evidence_ref_id]
        record = ClaimRecordV1(
            schema_version="claim_record.v1",
            claim_id=make_claim_id(
                ownership_namespace=(f"{extraction.document_id}:{extraction.child_id}"),
                knowledge_status="candidate",
                evidence_ref_ids=evidence_ids,
                derivation_parent_ids=[],
                canonical_proposition_signature=signature,
                scope_hash=scope_hash,
            ),
            document_id=extraction.document_id,
            child_id=extraction.child_id,
            proposition_text=evidence[predicate.evidence_ref_id].quote,
            canonical_proposition=canonical,
            claim_type=claim_type,
            predicate_observation_id=predicate.observation_id,
            predicate_id=typed.predicate_id if typed is not None else None,
            predicate_surface=predicate_span.text,
            predicate_lemma=predicate.predicate_lemma,
            normalized_predicate=normalized_predicate,
            typing_status="typed" if typed is not None else "untyped",
            arguments=arguments,
            polarity=polarity,
            modality=modality,
            assertion_mode=assertion_mode,
            conditions=conditions,
            exceptions=exceptions,
            temporal_cues=temporal_cues,
            evidence_sentence_ids=evidence_ids,
            source_relation_ids=source_relation_ids,
            scope_hash=scope_hash,
            knowledge_status="candidate",
            validation_status="candidate",
        )
        claims.append(record)
        claims_with_positions.append((record, predicate_span.start, predicate_span.end))
        for argument in arguments:
            span = spans[argument.span_observation_id]
            if (
                argument.role == "subject"
                and span.label == "pron"
                and argument.filler_kind == "span_observation"
            ):
                unresolved_coreference.add(
                    f"{argument.start_char}:{argument.end_char}:{argument.surface}"
                )

    attached_relations = {
        relation_id for claim in claims for relation_id in claim.source_relation_ids
    }
    all_relations = {item.relation_id for item in extraction.relations}
    rejected_relations.update(all_relations - attached_relations)
    multiplicity: Counter[tuple[str, str, str, str]] = Counter(
        (
            item.evidence_sentence_ids[0],
            item.canonical_proposition,
            item.claim_type,
            item.scope_hash,
        )
        for item in claims
    )
    same_sentence_repeated_claim_count = sum(
        count - 1 for count in multiplicity.values() if count > 1
    )
    links, cross_sentence_candidates, cross_sentence_rejected = _result_links(
        claims_with_positions, evidence, evidence_order
    )
    claims.sort(key=lambda item: item.claim_id)
    return ClaimCompilationV1(
        schema_version="claim_compilation.v1",
        document_id=extraction.document_id,
        child_id=extraction.child_id,
        claims=claims,
        links=links,
        rejected_relation_ids=sorted(rejected_relations),
        unresolved_coreference_spans=sorted(unresolved_coreference),
        skipped_predicate_observation_ids=sorted(skipped_predicates),
        same_sentence_repeated_claim_count=same_sentence_repeated_claim_count,
        cross_sentence_candidate_count=cross_sentence_candidates,
        cross_sentence_rejected_count=cross_sentence_rejected,
        compiler_recipe_hash=recipe_hash,
    )
