"""Reduce entailed OpenIE renderings without losing provenance variants."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Sequence

from models.graphify_contracts import (
    AdaptedOpenIEArgumentV1,
    OpenIEArgumentKind,
    OpenIEPropositionFamilyV1,
    OpenIERawPropositionV1,
    stable_digest,
    stable_id,
)
from services.extraction.graphify_assertion_semantics import (
    nominal_assertion_qualification,
    reported_attribution_source,
)

PROPOSITION_REDUCER_RELEASE = "graphify-openie-proposition-reducer-v2"
_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_NEGATION_RE = re.compile(r"\b(?:not|never|no|neither|nor)\b|n['’]t\b", re.I)
_MODAL_RE = re.compile(r"\b(?:may|might|could|would|should|will|can)\b", re.I)
_AUXILIARIES = frozenset({"is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "has", "have", "had"})


@dataclass(frozen=True)
class PropositionReducerOutput:
    families: tuple[OpenIEPropositionFamilyV1, ...]
    report: dict[str, object]


def _normalize(value: str) -> str:
    return " ".join(_WORD_RE.findall(value.casefold().replace("_", " ")))


def _lemma_token(token: str) -> str:
    irregular = {"built": "build", "said": "say", "denied": "deny", "acquired": "acquire"}
    if token in irregular:
        return irregular[token]
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("ing") and len(token) > 5:
        base = token[:-3]
        return base[:-1] if len(base) > 2 and base[-1] == base[-2] else base
    if token.endswith("ed") and len(token) > 4:
        base = token[:-2]
        return base + "e" if base.endswith(("at", "iz", "iv")) else base
    if token.endswith("es") and len(token) > 4:
        return token[:-2] if token.endswith(("ches", "shes", "xes", "zes")) else token[:-1]
    if token.endswith("s") and len(token) > 3:
        return token[:-1]
    return token


def relation_lemma(surface: str) -> str:
    tokens = _normalize(surface).split()
    content = [
        token for token in tokens
        if token not in _AUXILIARIES and token not in {"not", "never", "may", "might", "could", "would", "should", "will", "can"}
    ]
    if not content:
        return _normalize(surface)
    return " ".join([_lemma_token(content[0]), *content[1:]])


def _argument_key(argument: AdaptedOpenIEArgumentV1) -> str:
    if argument.kind == OpenIEArgumentKind.ENTITY:
        return f"entity:{argument.entity_id}"
    return f"{argument.kind.value.casefold()}:{argument.normalized_value or _normalize(argument.surface)}"


def _qualification(proposition: OpenIERawPropositionV1) -> tuple[str, str, str]:
    link_verbs = [link.verb.casefold() for link in proposition.asserter_links]
    denied = any(verb in {"deny", "refute", "reject"} for verb in link_verbs)
    negated = _NEGATION_RE.search(proposition.relation) or any(
        link.negated for link in proposition.asserter_links
    )
    polarity = "denied" if denied else "negative" if negated else "positive"
    modality = "modal" if _MODAL_RE.search(proposition.relation) else "asserted"
    if proposition.asserter_chain:
        links = ",".join(
            f"{link.asserter}:{link.verb}:{int(link.negated)}"
            for link in proposition.asserter_links
        )
        attribution = "attributed:" + " > ".join(proposition.asserter_chain)
        if links:
            attribution += f" [{links}]"
    else:
        attribution = "direct"
    nominal = nominal_assertion_qualification(
        proposition.evidence_text,
        proposition.subject,
        proposition.relation,
        proposition.object,
    )
    if nominal is not None:
        nominal_polarity, nominal_modality, nominal_attribution = nominal
        if nominal_polarity != "positive":
            polarity = nominal_polarity
        if modality == "asserted":
            modality = nominal_modality
        if attribution == "direct":
            attribution = nominal_attribution
    if attribution == "direct":
        # Peripheral "According to X, P" adjunct: the proposition is reported,
        # not directly asserted — the assertion gate decides its lane.
        reported = reported_attribution_source(proposition.evidence_text)
        if reported is not None:
            attribution = f"reported:according_to:{reported}"
    return polarity, modality, attribution


def reduce_openie_propositions(
    propositions: Sequence[OpenIERawPropositionV1],
    arguments: Sequence[AdaptedOpenIEArgumentV1],
) -> PropositionReducerOutput:
    proposition_by_id = {item.proposition_id: item for item in propositions}
    arguments_by_proposition: dict[str, dict[str, AdaptedOpenIEArgumentV1]] = defaultdict(dict)
    for argument in arguments:
        if argument.proposition_id not in proposition_by_id:
            raise ValueError(f"argument references unknown proposition {argument.proposition_id}")
        if argument.role in arguments_by_proposition[argument.proposition_id]:
            raise ValueError(f"duplicate {argument.role} argument for {argument.proposition_id}")
        arguments_by_proposition[argument.proposition_id][argument.role] = argument
    if any(set(arguments_by_proposition[item.proposition_id]) != {"subject", "object"} for item in propositions):
        raise ValueError("every raw proposition requires subject and object arguments")

    grouped: dict[tuple[str, ...], list[OpenIERawPropositionV1]] = defaultdict(list)
    for proposition in propositions:
        adapted = arguments_by_proposition[proposition.proposition_id]
        polarity, modality, attribution = _qualification(proposition)
        key = (
            proposition.document_id,
            proposition.unit_id,
            _argument_key(adapted["subject"]),
            relation_lemma(proposition.relation),
            _argument_key(adapted["object"]),
            polarity,
            modality,
            attribution,
        )
        grouped[key].append(proposition)

    families: list[OpenIEPropositionFamilyV1] = []
    for key, renderings in sorted(grouped.items()):
        document_id, unit_id, subject_key, lemma, object_key, polarity, modality, attribution = key
        renderings.sort(key=lambda item: (
            not item.extractor_release.endswith(":strict_surface_recovery"),
            -item.confidence,
            item.from_entailment,
            len(item.subject) + len(item.relation) + len(item.object),
            item.proposition_id,
        ))
        representative = renderings[0]
        adapted = arguments_by_proposition[representative.proposition_id]
        rendering_ids = tuple(sorted(item.proposition_id for item in renderings))
        families.append(OpenIEPropositionFamilyV1(
            family_id=stable_id(
                "openie-family", *key, rendering_ids, PROPOSITION_REDUCER_RELEASE,
            ),
            document_id=document_id,
            unit_id=unit_id,
            subject_key=subject_key,
            subject_kind=adapted["subject"].kind,
            relation_lemma=lemma,
            object_key=object_key,
            object_kind=adapted["object"].kind,
            polarity=polarity,
            modality=modality,
            attribution=attribution,
            representative_proposition_id=representative.proposition_id,
            rendering_ids=rendering_ids,
            surface_relations=tuple(sorted({item.relation for item in renderings})),
            evidence_start=representative.evidence_start,
            evidence_end=representative.evidence_end,
            max_confidence=max(item.confidence for item in renderings),
            family_release=PROPOSITION_REDUCER_RELEASE,
        ))
    families.sort(key=lambda item: (
        item.document_id, item.evidence_start, item.unit_id, item.family_id,
    ))
    assigned_ids = [rendering_id for family in families for rendering_id in family.rendering_ids]
    raw_ids = [item.proposition_id for item in propositions]
    family_sizes = Counter(len(item.rendering_ids) for item in families)
    report: dict[str, object] = {
        "schema_version": "polymath.openie_proposition_reducer_report.v1",
        "status": "passed",
        "raw_renderings": len(propositions),
        "proposition_families": len(families),
        "rendering_assignments": len(assigned_ids),
        "conservation": sorted(assigned_ids) == sorted(raw_ids)
        and len(assigned_ids) == len(set(assigned_ids)),
        "collapsed_renderings": len(propositions) - len(families),
        "family_size_counts": {str(key): value for key, value in sorted(family_sizes.items())},
        "qualified_families": sum(
            item.polarity != "positive" or item.modality != "asserted" or item.attribution != "direct"
            for item in families
        ),
        "identity_digest": stable_digest([
            item.model_dump(mode="json") for item in families
        ]),
    }
    return PropositionReducerOutput(tuple(families), report)
