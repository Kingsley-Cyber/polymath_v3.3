"""Local deterministic T9.1 domain and predicate→superframe resolution.

This module is candidate-only and side-effect free. It performs no provider
calls or durable writes. Domain affinities are built through a separate
serve-only function and never enter assignment/rule IDs or recipe hashes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any, cast, get_args

from models.claim_record import ClaimRecordV1
from models.hash_taxonomy import namespace_hash
from models.local_extraction import EntityType, PredicateType
from models.registry_loader import RegistryError, load_all
from models.semantic_digest import FrameId
from models.semantic_resolution import (
    DomainAffinityPriorV1,
    DomainAffinityServeViewV1,
    DomainAssignmentCandidateV1,
    DomainId,
    DomainResolutionV1,
    DomainScoreComponentsV1,
    DomainSignalV1,
    SuperframeRuleMatchV1,
    SuperframeRuleResolutionV1,
    UnresolvedDomainSignalV1,
)
from services.ingestion.corpus_lexicon import normalize_identity


DOMAIN_RESOLVER_VERSION = "deterministic_domain_resolver.v1"
SUPERFRAME_RESOLVER_VERSION = "deterministic_superframe_rule_resolver.v1"
_KNOWN_DOMAIN_IDS = frozenset(get_args(DomainId))
_KNOWN_FRAME_IDS = frozenset(get_args(FrameId))
_KNOWN_ENTITY_TYPES = frozenset(get_args(EntityType))


def _logical_id(prefix: str, artifact_kind: str, natural_keys: dict[str, Any]) -> str:
    digest = namespace_hash(
        "logical-artifact",
        {"artifact_kind": artifact_kind, "natural_keys": natural_keys},
    ).split(":", 1)[1]
    return f"{prefix}:{digest}"


def _domain_term_index(domain_registry: dict[str, Any]) -> dict[str, str]:
    if domain_registry.get("registry") != "domain_registry":
        raise RegistryError("domain resolver received the wrong registry")
    if domain_registry.get("version") != "v1":
        raise RegistryError("domain resolver requires domain_registry.v1")

    index: dict[str, str] = {}
    observed_ids: set[str] = set()
    for row in domain_registry.get("domains") or []:
        domain_id = row.get("domain_id")
        if domain_id not in _KNOWN_DOMAIN_IDS:
            raise RegistryError(f"domain resolver found unknown domain {domain_id!r}")
        if domain_id in observed_ids:
            raise RegistryError(f"domain resolver found duplicate domain {domain_id}")
        observed_ids.add(domain_id)
        terms = [row.get("name"), *(row.get("members") or [])]
        for term in terms:
            normalized = normalize_identity(term)
            if not normalized:
                raise RegistryError(f"domain {domain_id} has an empty normalized term")
            existing = index.get(normalized)
            if existing is not None and existing != domain_id:
                raise RegistryError(
                    f"domain term collision for {normalized!r}: "
                    f"{existing} vs {domain_id}"
                )
            index[normalized] = cast(str, domain_id)
    if observed_ids != _KNOWN_DOMAIN_IDS:
        missing = sorted(_KNOWN_DOMAIN_IDS - observed_ids)
        raise RegistryError(f"domain registry is incomplete; missing {missing}")
    return index


def resolve_domains(
    *,
    target_artifact_id: str,
    signals: Iterable[DomainSignalV1],
    context_profile_ids: Iterable[str] = (),
    domain_registry: dict[str, Any] | None = None,
    resolution_policy: dict[str, Any] | None = None,
) -> DomainResolutionV1:
    """Resolve explicit concept/heading signals by exact registry membership."""

    if not target_artifact_id.strip():
        raise ValueError("target_artifact_id must be nonempty")
    registries = load_all()
    domain_data = (
        registries["domain"] if domain_registry is None else domain_registry
    )
    policy = (
        registries["domain_resolution"]
        if resolution_policy is None
        else resolution_policy
    )
    if policy.get("registry") != "domain_resolution_policy":
        raise RegistryError("domain resolver received the wrong policy registry")
    if policy.get("version") != "v1":
        raise RegistryError("domain resolver requires domain_resolution_policy.v1")
    if policy.get("normalizer", {}).get("implementation") != (
        "services.ingestion.corpus_lexicon.normalize_identity"
    ):
        raise RegistryError("domain resolver policy does not name the CP5 normalizer")
    if policy.get("predicate_policy", {}).get("domain_bearing") is not False:
        raise RegistryError("PredicateType cannot be domain-bearing")

    term_index = _domain_term_index(domain_data)
    domain_hash = namespace_hash("registry", domain_data)
    policy_hash = namespace_hash("registry", policy)
    recipe_hash = namespace_hash(
        "recipe",
        {
            "resolver": DOMAIN_RESOLVER_VERSION,
            "domain_registry_hash": domain_hash,
            "resolution_policy_hash": policy_hash,
            "normalizer_id": policy["normalizer"]["normalizer_id"],
            "match": "exact_normalized_domain_name_or_member",
            "affinity": "excluded",
        },
    )

    signal_rows = list(signals)
    signal_ids = [item.signal_id for item in signal_rows]
    if len(signal_ids) != len(set(signal_ids)):
        raise ValueError("domain signal IDs must be unique per resolution")

    aggregates: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "claim_matches": 0,
            "heading_matches": 0,
            "claim_evidence": set(),
            "context_evidence": set(),
            "signal_ids": set(),
            "terms": set(),
            "claim_ids": set(),
        }
    )
    unresolved: list[UnresolvedDomainSignalV1] = []
    for signal in signal_rows:
        normalized = normalize_identity(signal.label)
        domain_id = term_index.get(normalized)
        if domain_id is None:
            unresolved.append(
                UnresolvedDomainSignalV1(
                    schema_version="unresolved_domain_signal.v1",
                    unresolved_id=_logical_id(
                        "unresolved-domain",
                        "unresolved_domain_signal",
                        {
                            "target_artifact_id": target_artifact_id,
                            "signal_id": signal.signal_id,
                            "normalized_term": normalized,
                            "signal_kind": signal.signal_kind,
                            "policy_version": policy["version"],
                        },
                    ),
                    target_artifact_id=target_artifact_id,
                    signal_id=signal.signal_id,
                    surface=signal.label,
                    normalized_term=normalized,
                    signal_kind=signal.signal_kind,
                    evidence_ref_ids=sorted(signal.evidence_ref_ids),
                    supporting_claim_ids=sorted(signal.supporting_claim_ids),
                    assignment_state="unresolved",
                    reason="no_exact_domain_registry_match",
                    normalizer_id="corpus_lexicon.normalize_identity.v1",
                )
            )
            continue

        aggregate = aggregates[domain_id]
        aggregate["signal_ids"].add(signal.signal_id)
        aggregate["terms"].add(normalized)
        aggregate["claim_ids"].update(signal.supporting_claim_ids)
        if signal.signal_kind == "claim_concept":
            aggregate["claim_matches"] += 1
            aggregate["claim_evidence"].update(signal.evidence_ref_ids)
        elif signal.signal_kind == "section_heading":
            aggregate["heading_matches"] += 1
            aggregate["context_evidence"].update(signal.evidence_ref_ids)
        else:  # pragma: no cover - DomainSignalV1 closes this vocabulary
            raise RegistryError(f"unknown domain signal kind {signal.signal_kind!r}")

    assignments: list[DomainAssignmentCandidateV1] = []
    for domain_id in sorted(aggregates):
        aggregate = aggregates[domain_id]
        claim_matches = int(aggregate["claim_matches"])
        heading_matches = int(aggregate["heading_matches"])
        if claim_matches and heading_matches:
            method = "exact_claim_concept_and_heading"
        elif claim_matches:
            method = "exact_claim_concept"
        else:
            method = "exact_section_heading"
        assignments.append(
            DomainAssignmentCandidateV1(
                schema_version="domain_assignment_candidate.v1",
                assignment_id=_logical_id(
                    "domain-assignment",
                    "domain_assignment_candidate",
                    {
                        "target_artifact_id": target_artifact_id,
                        "domain_id": domain_id,
                        "domain_registry_version": domain_data["version"],
                        "resolution_policy_version": policy["version"],
                    },
                ),
                target_artifact_id=target_artifact_id,
                domain_id=cast(DomainId, domain_id),
                assignment_role="dominant" if claim_matches else "supporting",
                assignment_state="candidate",
                derivation_method=cast(Any, method),
                matched_signal_ids=sorted(aggregate["signal_ids"]),
                matched_normalized_terms=sorted(aggregate["terms"]),
                evidence_ref_ids=sorted(
                    aggregate["claim_evidence"] | aggregate["context_evidence"]
                ),
                supporting_claim_ids=sorted(aggregate["claim_ids"]),
                score_components=DomainScoreComponentsV1(
                    exact_claim_concept_matches=claim_matches,
                    exact_heading_matches=heading_matches,
                    claim_evidence_ref_count=len(aggregate["claim_evidence"]),
                    context_evidence_ref_count=len(aggregate["context_evidence"]),
                ),
                domain_registry="domain_registry",
                domain_registry_version="v1",
                domain_registry_hash=domain_hash,
                resolution_policy="domain_resolution_policy",
                resolution_policy_version="v1",
                resolution_policy_hash=policy_hash,
                resolution_recipe_hash=recipe_hash,
            )
        )

    return DomainResolutionV1(
        schema_version="domain_resolution.v1",
        target_artifact_id=target_artifact_id,
        assignments=assignments,
        unresolved_signals=sorted(unresolved, key=lambda item: item.unresolved_id),
        context_profile_ids=sorted(set(context_profile_ids)),
        domain_registry_hash=domain_hash,
        resolution_policy_hash=policy_hash,
        resolution_recipe_hash=recipe_hash,
    )


def build_domain_affinity_serve_view(
    resolution: DomainResolutionV1,
    *,
    affinity_registry: dict[str, Any] | None = None,
) -> DomainAffinityServeViewV1:
    """Build a quarantined serving view without changing resolution identity."""

    affinity = (
        load_all()["affinity"]
        if affinity_registry is None
        else affinity_registry
    )
    if affinity.get("registry") != "domain_superframe_affinity":
        raise RegistryError("affinity view received the wrong registry")
    if affinity.get("version") != "v1":
        raise RegistryError("affinity view requires domain_superframe_affinity.v1")
    rows: dict[str, dict[str, Any]] = {}
    for row in affinity.get("affinities") or []:
        domain_id = row.get("domain_id")
        if domain_id not in _KNOWN_DOMAIN_IDS:
            raise RegistryError(f"affinity references unknown domain {domain_id!r}")
        if domain_id in rows:
            raise RegistryError(f"duplicate affinity row for {domain_id}")
        frame_ids = row.get("dominant_superframes") or []
        if any(frame_id not in _KNOWN_FRAME_IDS for frame_id in frame_ids):
            raise RegistryError(f"affinity[{domain_id}] references unknown superframe")
        rows[domain_id] = row

    priors: list[DomainAffinityPriorV1] = []
    for assignment in resolution.assignments:
        row = rows.get(assignment.domain_id)
        if row is None:
            raise RegistryError(f"affinity missing domain {assignment.domain_id}")
        priors.append(
            DomainAffinityPriorV1(
                domain_id=assignment.domain_id,
                dominant_superframe_ids=cast(
                    list[FrameId],
                    list(row["dominant_superframes"]),
                ),
            )
        )
    return DomainAffinityServeViewV1(
        schema_version="domain_affinity_serve_view.v1",
        target_artifact_id=resolution.target_artifact_id,
        priors=priors,
        affinity_registry="domain_superframe_affinity",
        affinity_registry_version="v1",
        affinity_registry_hash=namespace_hash("registry", affinity),
        serve_only=True,
        excluded_from_semantic_identity=True,
        excluded_from_acceptance=True,
    )


def _condition_matches(
    condition: dict[str, Any],
    context: Mapping[str, set[str]],
) -> bool:
    field = condition.get("field")
    operator = condition.get("operator")
    if field not in context:
        raise RegistryError(f"unknown superframe condition field {field!r}")
    if operator != "contains_any":
        raise RegistryError(f"unknown superframe condition operator {operator!r}")
    return bool(context[field] & set(condition.get("values") or []))


def _claim_rule_context(
    claim: ClaimRecordV1,
    entity_types_by_mention_id: Mapping[str, EntityType],
) -> dict[str, set[str]]:
    entity_refs = {
        item.filler_ref
        for item in claim.arguments
        if item.filler_kind == "entity_mention"
    }
    supplied_refs = set(entity_types_by_mention_id)
    if supplied_refs - entity_refs:
        raise RegistryError(
            "entity type map references unknown claim mention IDs: "
            f"{sorted(supplied_refs - entity_refs)}"
        )
    if entity_refs - supplied_refs:
        raise RegistryError(
            "entity type map is missing claim mention IDs: "
            f"{sorted(entity_refs - supplied_refs)}"
        )
    unknown_types = {
        value
        for value in entity_types_by_mention_id.values()
        if value not in _KNOWN_ENTITY_TYPES
    }
    if unknown_types:
        raise RegistryError(
            f"unknown entity types in rule input: {sorted(unknown_types)}"
        )

    subject_tokens: set[str] = set()
    object_entity_types: set[str] = set()
    for argument in claim.arguments:
        if argument.role == "subject":
            subject_tokens.update(normalize_identity(argument.surface).split())
        if argument.role == "object" and argument.filler_kind == "entity_mention":
            object_entity_types.add(entity_types_by_mention_id[argument.filler_ref])
    return {
        "subject_surface_tokens": subject_tokens,
        "object_entity_types": object_entity_types,
    }


def resolve_superframe_rule(
    claim: ClaimRecordV1,
    *,
    entity_types_by_mention_id: Mapping[str, EntityType],
    rule_registry: dict[str, Any] | None = None,
) -> SuperframeRuleResolutionV1:
    """Resolve at most one terminal candidate rule for a compiled claim."""

    registry = (
        load_all()["superframe_rule"] if rule_registry is None else rule_registry
    )
    if registry.get("registry") != "superframe_rule_registry":
        raise RegistryError("superframe resolver received the wrong registry")
    if registry.get("version") != "v1":
        raise RegistryError("superframe resolver requires superframe_rule_registry.v1")
    registry_hash = namespace_hash("registry", registry)
    recipe_hash = namespace_hash(
        "recipe",
        {
            "resolver": SUPERFRAME_RESOLVER_VERSION,
            "rule_registry_hash": registry_hash,
            "terminal_policy": "highest_priority_first_stop",
            "role_bindings": "deferred_to_t9_2",
            "affinity": "excluded",
        },
    )

    if claim.typing_status == "untyped":
        return SuperframeRuleResolutionV1(
            schema_version="superframe_rule_resolution.v1",
            target_claim_id=claim.claim_id,
            predicate_type=None,
            matches=[],
            explicit_abstention_reason="claim_is_untyped",
            rule_registry_hash=registry_hash,
            resolution_recipe_hash=recipe_hash,
        )

    predicate = claim.normalized_predicate
    if predicate is None:  # ClaimRecordV1 already enforces this invariant.
        raise RegistryError("typed claim is missing a normalized predicate")
    controlled = set(load_all()["vocab"]["predicate_types"])
    if predicate not in controlled:
        raise RegistryError(f"unknown controlled predicate {predicate!r}")
    context = _claim_rule_context(claim, entity_types_by_mention_id)

    matching_rule: dict[str, Any] | None = None
    rules = sorted(
        registry.get("rules") or [],
        key=lambda rule: (-int(rule.get("priority", -1)), str(rule.get("rule_id"))),
    )
    for rule in rules:
        predicates = rule.get("predicates") or []
        if any(item not in controlled for item in predicates):
            raise RegistryError(f"{rule.get('rule_id')}: unknown predicate ID")
        if rule.get("frame_id") not in _KNOWN_FRAME_IDS:
            raise RegistryError(f"{rule.get('rule_id')}: unknown superframe ID")
        if predicate not in predicates:
            continue
        if not all(
            _condition_matches(condition, context)
            for condition in rule.get("conditions") or []
        ):
            continue
        matching_rule = rule
        if rule.get("terminal") is True:
            break

    if matching_rule is not None:
        frame_id = matching_rule["frame_id"]
        match = SuperframeRuleMatchV1(
            schema_version="superframe_rule_match.v1",
            match_id=_logical_id(
                "superframe-rule-match",
                "superframe_rule_match",
                {
                    "claim_id": claim.claim_id,
                    "rule_id": matching_rule["rule_id"],
                    "frame_id": frame_id,
                    "rule_registry_version": registry["version"],
                },
            ),
            claim_id=claim.claim_id,
            rule_id=matching_rule["rule_id"],
            frame_id=cast(FrameId, frame_id),
            predicate_type=predicate,
            assignment_state="candidate",
            derivation_method="predicate_superframe_rule",
            evidence_ref_ids=sorted(claim.evidence_sentence_ids),
            priority=matching_rule["priority"],
            terminal=True,
            owner_attention=matching_rule["owner_attention"],
            rule_registry="superframe_rule_registry",
            rule_registry_version="v1",
            rule_registry_hash=registry_hash,
            resolution_recipe_hash=recipe_hash,
        )
        return SuperframeRuleResolutionV1(
            schema_version="superframe_rule_resolution.v1",
            target_claim_id=claim.claim_id,
            predicate_type=predicate,
            matches=[match],
            explicit_abstention_reason=None,
            rule_registry_hash=registry_hash,
            resolution_recipe_hash=recipe_hash,
        )

    abstentions = {
        item.get("predicate"): item.get("reason")
        for item in registry.get("abstentions") or []
    }
    reason = abstentions.get(predicate)
    if reason != "generic_association_is_not_a_mechanism":
        raise RegistryError(
            f"controlled predicate {predicate} has neither a matching rule nor "
            "a recognized explicit abstention"
        )
    return SuperframeRuleResolutionV1(
        schema_version="superframe_rule_resolution.v1",
        target_claim_id=claim.claim_id,
        predicate_type=predicate,
        matches=[],
        explicit_abstention_reason="generic_association_is_not_a_mechanism",
        rule_registry_hash=registry_hash,
        resolution_recipe_hash=recipe_hash,
    )
