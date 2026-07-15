"""Deterministic spaCy observation capture and claim-candidate compilation.

The module imports no spaCy package.  Callers supply a loaded ``Language``
object, which keeps the production backend free of an accidental heavyweight
runtime dependency and makes parser/model identity explicit in the recipe.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re
from typing import Any, Iterable, cast

from models.hash_taxonomy import namespace_hash
from models.local_extraction import LocalExtractionV1, PredicateMention, PredicateType
from models.registry_loader import normalize_predicate_lemma, registry_hashes
from models.semantic_artifacts import (
    ClaimArgumentCandidate,
    ClaimAssertionCandidate,
    ObservationBundle,
    PredicateObservation,
    QualifierObservation,
    SpanObservation,
    domain_hash,
    make_evidence_ref,
)

_MODAL_FORCE = {
    "may": "possible",
    "might": "possible",
    "can": "possible",
    "could": "possible",
    "probably": "probable",
    "likely": "probable",
    "will": "predicted",
    "would": "possible",
    "should": "recommended",
    "ought": "recommended",
    "must": "required",
    "need": "required",
}
_CONDITION_MARKERS = {
    "if",
    "unless",
    "when",
    "whenever",
    "provided",
    "assuming",
    "under",
}
_EXCEPTION_MARKERS = {"except", "excluding", "apart"}
_ATTRIBUTION_LEMMAS = {
    "argue",
    "claim",
    "conclude",
    "find",
    "hypothesize",
    "observe",
    "report",
    "say",
    "suggest",
}
_CAUSAL_LEMMAS = {
    "affect",
    "cause",
    "decrease",
    "drive",
    "increase",
    "influence",
    "lead",
    "lower",
    "produce",
    "raise",
    "result",
}
_COMPARISON_MARKERS = {"than", "versus", "vs", "compared", "equivalent"}
_TEMPORAL_MARKERS = {"after", "before", "during", "since", "until"}
_TEMPORAL_ENTITY_LABELS = {"DATE", "TIME", "EVENT"}
_TEMPORAL_SURFACE_RE = re.compile(
    r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b|"
    r"\b(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(?:19|20)\d{2}\b|"
    r"\bQ[1-4]\s+(?:19|20)\d{2}\b"
)
_PREDICATE_DEPS = {"ROOT", "conj", "advcl", "ccomp", "xcomp", "relcl"}
_SUBJECT_DEPS = {"nsubj", "nsubjpass", "csubj", "csubjpass"}
_OBJECT_DEPS = {"dobj", "obj", "attr", "oprd", "dative", "acomp"}
_LOCAL_MODALITY_MAP = {
    "possible": "possible",
    "probable": "probable",
    "predicted": "probable",
    "recommended": "recommended",
    "required": "necessary",
}


@dataclass(frozen=True)
class LocalExtractionCompileResult:
    """Owner payload plus non-payload provenance/fallback accounting."""

    extraction: LocalExtractionV1
    normalization_registry: str
    normalization_registry_version: str
    normalization_registry_hash: str
    recipe_hash: str
    observed_predicate_count: int
    matched_predicate_count: int
    unresolved_predicate_count: int
    unresolved_rate: float
    matched_counts: tuple[tuple[str, int], ...]

    def receipt(self) -> dict[str, Any]:
        """Return a text-free run receipt suitable for envelope provenance."""

        return {
            "normalization_registry": self.normalization_registry,
            "normalization_registry_version": self.normalization_registry_version,
            "normalization_registry_hash": self.normalization_registry_hash,
            "recipe_hash": self.recipe_hash,
            "observed_predicate_count": self.observed_predicate_count,
            "matched_predicate_count": self.matched_predicate_count,
            "unresolved_predicate_count": self.unresolved_predicate_count,
            "unresolved_rate": self.unresolved_rate,
            "matched_counts": dict(self.matched_counts),
        }


def _token_span(token: Any) -> tuple[int, int]:
    return int(token.idx), int(token.idx) + len(str(token.text))


def _subtree_span(token: Any) -> tuple[int, int]:
    tokens = list(token.subtree)
    if not tokens:
        return _token_span(token)
    return min(int(item.idx) for item in tokens), max(
        int(item.idx) + len(str(item.text)) for item in tokens
    )


def _span_id(kind: str, start: int, end: int, text: str) -> str:
    digest = domain_hash(
        "semantic-span", {"kind": kind, "start": start, "end": end, "text": text}
    ).split(":", 1)[1]
    return f"observation:{digest}"


def _qualifier_id(
    kind: str, target: str, start: int, end: int, normalized_value: str
) -> str:
    digest = domain_hash(
        "semantic-qualifier",
        {
            "kind": kind,
            "target": target,
            "start": start,
            "end": end,
            "normalized_value": normalized_value,
        },
    ).split(":", 1)[1]
    return f"observation:{digest}"


def _phrase_span(token: Any) -> tuple[int, int]:
    start, end = _subtree_span(token)
    # A preposition's subtree is normally the desired condition/exception.
    # A complementizer can omit its governing clause, so include the head
    # subtree when that head is a subordinate predicate.
    if str(getattr(token, "dep_", "")) in {"mark", "advmod"}:
        head = getattr(token, "head", None)
        if head is not None and str(getattr(head, "pos_", "")) in {"VERB", "AUX"}:
            head_start, head_end = _subtree_span(head)
            start, end = min(start, head_start), max(end, head_end)
    return start, end


def _nearest_predicate_token(
    token: Any, predicate_by_token: dict[int, str]
) -> int | None:
    current = token
    seen: set[int] = set()
    for _ in range(12):
        index = int(getattr(current, "i", -1))
        if index in predicate_by_token:
            return index
        if index in seen:
            break
        seen.add(index)
        head = getattr(current, "head", None)
        if head is None or head is current:
            break
        current = head
    return None


def _predicate_tokens(sentence: Any) -> list[Any]:
    items: list[Any] = []
    for token in sentence:
        pos = str(getattr(token, "pos_", ""))
        dep = str(getattr(token, "dep_", ""))
        # Small English pipelines occasionally tag a sentence-root verb as a
        # NOUN (for example, "discounting lowers price").  Dependency shape
        # is stronger evidence in that narrow case: a ROOT with both a
        # subject and an object is still a predicate candidate.  This is a
        # general repair, not a lexical exception.
        root_argument_shape = (
            dep == "ROOT"
            and any(
                str(getattr(child, "dep_", "")) in _SUBJECT_DEPS
                for child in token.children
            )
            and any(
                str(getattr(child, "dep_", "")) in _OBJECT_DEPS
                for child in token.children
            )
        )
        if (pos in {"VERB", "AUX"} and dep in _PREDICATE_DEPS) or root_argument_shape:
            items.append(token)
    return items


def _children(token: Any, deps: set[str]) -> list[Any]:
    return [child for child in token.children if str(child.dep_) in deps]


def _inherit_subjects(token: Any) -> list[Any]:
    subjects = _children(token, _SUBJECT_DEPS)
    current = token
    for _ in range(4):
        if subjects:
            return subjects
        head = getattr(current, "head", None)
        if head is None or head is current:
            break
        subjects = _children(head, _SUBJECT_DEPS)
        current = head
    return subjects


def _objects(token: Any) -> list[Any]:
    objects = _children(token, _OBJECT_DEPS)
    for prep in [
        child for child in token.children if str(child.dep_) in {"prep", "agent"}
    ]:
        objects.extend(_children(prep, {"pobj"}))
    return objects


def _add_span(
    spans: dict[str, SpanObservation],
    *,
    text: str,
    token: Any,
    kind: str,
    label: str,
    producer: str,
) -> str:
    start, end = (
        _subtree_span(token) if kind in {"subject", "object"} else _token_span(token)
    )
    surface = text[start:end]
    observation_id = _span_id(kind, start, end, surface)
    spans.setdefault(
        observation_id,
        SpanObservation(
            observation_id=observation_id,
            kind=kind,
            label=label,
            text=surface,
            start=start,
            end=end,
            producer=producer,
        ),
    )
    return observation_id


def _root_predicate_id(sentence: Any, predicate_by_token: dict[int, str]) -> str | None:
    for token in sentence:
        if str(getattr(token, "dep_", "")) == "ROOT":
            return predicate_by_token.get(int(token.i))
    return None


def build_spacy_observation_bundle(
    *,
    text: str,
    nlp: Any,
    source_version_id: str,
    hierarchy_node_id: str,
    parser_id: str,
    parser_version: str,
) -> ObservationBundle:
    """Capture exact spans, predicates, and qualifiers from one child text."""

    doc = nlp(text)
    producer = f"spacy:{parser_id}"
    spans: dict[str, SpanObservation] = {}
    predicates: list[PredicateObservation] = []
    qualifiers: list[QualifierObservation] = []
    evidence_refs = []

    for sentence in doc.sents:
        sent_start, sent_end = int(sentence.start_char), int(sentence.end_char)
        evidence = make_evidence_ref(
            text=text,
            start=sent_start,
            end=sent_end,
            source_version_id=source_version_id,
            hierarchy_node_id=hierarchy_node_id,
        )
        evidence_refs.append(evidence)
        predicate_tokens = _predicate_tokens(sentence)
        predicate_by_token: dict[int, str] = {}
        predicate_objects: dict[int, PredicateObservation] = {}
        for token in predicate_tokens:
            predicate_span_id = _add_span(
                spans,
                text=text,
                token=token,
                kind="predicate",
                label=str(token.lemma_ or token.text).lower(),
                producer=producer,
            )
            subject_ids = [
                _add_span(
                    spans,
                    text=text,
                    token=item,
                    kind="subject",
                    label=str(getattr(item, "pos_", "unknown")).lower(),
                    producer=producer,
                )
                for item in _inherit_subjects(token)
            ]
            object_ids = [
                _add_span(
                    spans,
                    text=text,
                    token=item,
                    kind="object",
                    label=str(getattr(item, "pos_", "unknown")).lower(),
                    producer=producer,
                )
                for item in _objects(token)
            ]
            identity = {
                "predicate_span_id": predicate_span_id,
                "subjects": subject_ids,
                "objects": object_ids,
                "evidence_ref_id": evidence.evidence_ref_id,
            }
            observation_id = (
                "observation:"
                + domain_hash("predicate-observation", identity).split(":", 1)[1]
            )
            predicate = PredicateObservation(
                observation_id=observation_id,
                predicate_span_id=predicate_span_id,
                predicate_lemma=str(token.lemma_ or token.text).lower(),
                subject_span_ids=subject_ids,
                object_span_ids=object_ids,
                evidence_ref_id=evidence.evidence_ref_id,
                producer=producer,
            )
            predicates.append(predicate)
            predicate_by_token[int(token.i)] = observation_id
            predicate_objects[int(token.i)] = predicate

        root_id = _root_predicate_id(sentence, predicate_by_token)
        emitted_qualifier_keys: set[tuple[str, int, int, str]] = set()
        for token in sentence:
            lower = str(token.text).lower()
            lemma = str(getattr(token, "lemma_", lower)).lower()
            dep = str(getattr(token, "dep_", ""))
            tag = str(getattr(token, "tag_", ""))
            kind: str | None = None
            normalized = ""
            start, end = _token_span(token)
            target_index = _nearest_predicate_token(token, predicate_by_token)
            target = (
                predicate_by_token.get(target_index)
                if target_index is not None
                else root_id
            )

            if dep == "neg" or lower in {"not", "never", "n't"}:
                kind, normalized = "negation", "negated"
            elif tag == "MD" or lower in _MODAL_FORCE:
                kind, normalized = "modal", _MODAL_FORCE.get(lower, "possible")
            elif lower in _CONDITION_MARKERS:
                kind, normalized = "condition", "conditional"
                start, end = _phrase_span(token)
                # A subordinate condition scopes the main sentence claim, not
                # merely the predicate inside the condition.
                target = root_id or target
            elif lower in _EXCEPTION_MARKERS:
                kind, normalized = "exception", "exception"
                start, end = _phrase_span(token)
                target = root_id or target
            elif lemma in _ATTRIBUTION_LEMMAS:
                kind, normalized = "attribution", lemma
                complement = next(
                    (
                        child
                        for child in token.children
                        if str(getattr(child, "dep_", "")) in {"ccomp", "xcomp"}
                    ),
                    None,
                )
                if complement is not None:
                    target = predicate_by_token.get(int(complement.i), target)
            elif lemma in _CAUSAL_LEMMAS:
                kind, normalized = "causal", lemma
            elif lower in _COMPARISON_MARKERS:
                kind, normalized = "comparison", lower
            elif lower in _TEMPORAL_MARKERS:
                kind, normalized = "temporal", lower
                start, end = _phrase_span(token)

            if kind and target:
                cue = text[start:end]
                emitted_qualifier_keys.add((kind, start, end, target))
                qualifiers.append(
                    QualifierObservation(
                        observation_id=_qualifier_id(
                            kind, target, start, end, normalized
                        ),
                        target_observation_id=target,
                        kind=kind,
                        cue=cue,
                        normalized_value=normalized,
                        start=start,
                        end=end,
                        producer=producer,
                    )
                )

        if root_id:
            temporal_spans: list[tuple[int, int]] = []
            for entity in getattr(doc, "ents", ()):
                if (
                    str(getattr(entity, "label_", "")) in _TEMPORAL_ENTITY_LABELS
                    and sent_start <= int(entity.start_char)
                    and int(entity.end_char) <= sent_end
                ):
                    temporal_spans.append(
                        (int(entity.start_char), int(entity.end_char))
                    )
            temporal_spans.extend(
                (match.start(), match.end())
                for match in _TEMPORAL_SURFACE_RE.finditer(text, sent_start, sent_end)
            )
            for start, end in sorted(set(temporal_spans)):
                key = ("temporal", start, end, root_id)
                if key in emitted_qualifier_keys:
                    continue
                emitted_qualifier_keys.add(key)
                qualifiers.append(
                    QualifierObservation(
                        observation_id=_qualifier_id(
                            "temporal", root_id, start, end, "temporal_reference"
                        ),
                        target_observation_id=root_id,
                        kind="temporal",
                        cue=text[start:end],
                        normalized_value="temporal_reference",
                        start=start,
                        end=end,
                        producer=producer,
                    )
                )

    recipe_hash = semantic_observation_recipe_hash(
        parser_id=parser_id,
        parser_version=parser_version,
    )
    identity = {
        "source_version_id": source_version_id,
        "hierarchy_node_id": hierarchy_node_id,
        "text_hash": domain_hash("normalized-text", text),
        "recipe_hash": recipe_hash,
    }
    return ObservationBundle(
        bundle_id="observation-bundle:"
        + domain_hash("observation-bundle", identity).split(":", 1)[1],
        source_version_id=source_version_id,
        hierarchy_node_id=hierarchy_node_id,
        text_hash=identity["text_hash"],
        text_length=len(text),
        producer=producer,
        producer_version=parser_version,
        recipe_hash=identity["recipe_hash"],
        spans=list(spans.values()),
        predicates=predicates,
        qualifiers=qualifiers,
        evidence_refs=evidence_refs,
    )


def semantic_observation_recipe_hash(*, parser_id: str, parser_version: str) -> str:
    """Return the frozen observation recipe identity without parsing text."""

    if not parser_id.strip() or not parser_version.strip():
        raise ValueError("parser identity must be nonempty")
    recipe = {
        "parser_id": parser_id,
        "parser_version": parser_version,
        "compiler": "semantic_observations.v1",
        "qualifier_rules": "qualifier_rules.v1",
    }
    return domain_hash("semantic-observation-recipe", recipe)


def _modal_force(qualifiers: Iterable[QualifierObservation]) -> str:
    ranking = {
        "asserted": 0,
        "possible": 1,
        "probable": 2,
        "predicted": 3,
        "recommended": 4,
        "required": 5,
    }
    values = [
        item.normalized_value
        for item in qualifiers
        if item.kind == "modal" and item.normalized_value in ranking
    ]
    return max(values, key=lambda item: ranking[item]) if values else "asserted"


def _local_modality(qualifiers: Iterable[QualifierObservation]) -> str:
    observed = list(qualifiers)
    modal = _modal_force(observed)
    if modal in _LOCAL_MODALITY_MAP:
        return _LOCAL_MODALITY_MAP[modal]
    if any(item.kind == "condition" for item in observed):
        return "hypothetical"
    return "asserted"


def compile_local_extraction_v1(
    bundle: ObservationBundle,
    *,
    document_id: str,
    child_id: str,
) -> LocalExtractionCompileResult:
    """Normalize spaCy predicate observations into the owner child contract.

    GLiNER entities and GLiREL/Relex relations are deliberately not fabricated
    here.  T8.2 merges those observation lanes after their own candidate
    boundaries exist. Unknown lemmas remain explicit unresolved spans.
    """

    if child_id != bundle.hierarchy_node_id:
        raise ValueError("child_id must equal the observation hierarchy_node_id")

    spans = {item.observation_id: item for item in bundle.spans}
    qualifiers_by_target: dict[str, list[QualifierObservation]] = defaultdict(list)
    for qualifier in bundle.qualifiers:
        qualifiers_by_target[qualifier.target_observation_id].append(qualifier)

    normalization = load_normalization_identity()
    recipe_hash = local_extraction_recipe_hash()
    predicate_mentions: list[PredicateMention] = []
    unresolved_spans: list[str] = []
    matched_counts: dict[str, int] = defaultdict(int)

    for predicate in bundle.predicates:
        predicate_span = spans.get(predicate.predicate_span_id)
        if predicate_span is None:
            raise ValueError("predicate observation references an unknown span")
        resolved = normalize_predicate_lemma(predicate.predicate_lemma)
        if resolved is None:
            unresolved_spans.append(
                f"{predicate_span.start}:{predicate_span.end}:{predicate_span.text}"
            )
            continue
        predicate_type = cast(PredicateType, resolved["predicate_type"])
        qualifiers = qualifiers_by_target[predicate.observation_id]
        identity = {
            "document_id": document_id,
            "child_id": child_id,
            "predicate_observation_id": predicate.observation_id,
            "predicate_lemma": resolved["lemma"],
            "normalized_predicate": predicate_type,
            "normalization_registry_version": resolved["registry_version"],
            "normalization_registry_hash": normalization["hash"],
            "recipe_hash": recipe_hash,
        }
        predicate_mentions.append(
            PredicateMention(
                predicate_id="predicate:"
                + namespace_hash("logical-artifact", identity).split(":", 1)[1],
                surface_text=predicate_span.text,
                lemma=resolved["lemma"],
                normalized_predicate=predicate_type,
                start_char=predicate_span.start,
                end_char=predicate_span.end,
                negated=any(item.kind == "negation" for item in qualifiers),
                modality=_local_modality(qualifiers),
                confidence=1.0,
            )
        )
        matched_counts[predicate_type] += 1

    observed_count = len(bundle.predicates)
    unresolved_count = len(unresolved_spans)
    extraction = LocalExtractionV1(
        schema_version="local_extraction.v1",
        document_id=document_id,
        child_id=child_id,
        sentence_ids=[item.evidence_ref_id for item in bundle.evidence_refs],
        entities=[],
        predicates=predicate_mentions,
        relations=[],
        unresolved_spans=unresolved_spans,
    )
    return LocalExtractionCompileResult(
        extraction=extraction,
        normalization_registry=normalization["registry"],
        normalization_registry_version=normalization["version"],
        normalization_registry_hash=normalization["hash"],
        recipe_hash=recipe_hash,
        observed_predicate_count=observed_count,
        matched_predicate_count=len(predicate_mentions),
        unresolved_predicate_count=unresolved_count,
        unresolved_rate=(unresolved_count / observed_count if observed_count else 0.0),
        matched_counts=tuple(sorted(matched_counts.items())),
    )


def load_normalization_identity() -> dict[str, str]:
    """Load the active mapping identity once per compilation boundary."""

    from models.registry_loader import load_all

    registry = load_all()["predicate_normalization"]
    return {
        "registry": registry["registry"],
        "version": registry["version"],
        "hash": registry_hashes()["predicate_normalization"],
    }


def local_extraction_recipe_hash() -> str:
    """Return the frozen local-extraction recipe identity without source text."""

    normalization = load_normalization_identity()
    recipe = {
        "compiler": "local_extraction_spacy.v1",
        "normalization_registry": normalization["registry"],
        "normalization_registry_version": normalization["version"],
        "normalization_registry_hash": normalization["hash"],
        "sentence_identity": "observation_bundle.evidence_ref_id",
        "unknown_policy": "unresolved_spans",
        "entity_lane": "not_fabricated_t8_1",
        "relation_lane": "not_fabricated_t8_1",
    }
    return namespace_hash("recipe", recipe)


def _claim_type(
    predicate: PredicateObservation, qualifiers: list[QualifierObservation]
) -> str:
    lemma = predicate.predicate_lemma
    modal = _modal_force(qualifiers)
    if modal in {"recommended", "required"}:
        return "recommendation_or_procedure"
    if lemma in {"define", "mean"}:
        return "definition"
    if lemma == "be" and any(item.kind == "comparison" for item in qualifiers):
        return "comparison_or_contrast"
    if lemma == "be" and predicate.object_span_ids:
        return "definition"
    if lemma in _CAUSAL_LEMMAS or any(item.kind == "causal" for item in qualifiers):
        return "causal"
    if modal == "predicted":
        return "prediction"
    if any(item.kind == "comparison" for item in qualifiers):
        return "comparison_or_contrast"
    return "description_or_observation"


def compile_claim_candidates(
    bundle: ObservationBundle,
) -> list[ClaimAssertionCandidate]:
    """Compile evidence-grounded candidates; never promote them to asserted."""

    spans = {item.observation_id: item for item in bundle.spans}
    evidence = {item.evidence_ref_id: item for item in bundle.evidence_refs}
    qualifiers_by_target: dict[str, list[QualifierObservation]] = defaultdict(list)
    for qualifier in bundle.qualifiers:
        qualifiers_by_target[qualifier.target_observation_id].append(qualifier)

    candidates: list[ClaimAssertionCandidate] = []
    for predicate in bundle.predicates:
        if not predicate.subject_span_ids:
            continue
        predicate_span = spans[predicate.predicate_span_id]
        refs = [
            ("subject", span_id)
            for span_id in predicate.subject_span_ids
            if span_id in spans
        ] + [
            ("object", span_id)
            for span_id in predicate.object_span_ids
            if span_id in spans
        ]
        arguments = [
            ClaimArgumentCandidate(
                role=role,
                surface=spans[span_id].text,
                span_observation_id=span_id,
                evidence_ref_id=predicate.evidence_ref_id,
            )
            for role, span_id in refs
        ]
        observed_qualifiers = qualifiers_by_target[predicate.observation_id]
        polarity = (
            "negated"
            if any(item.kind == "negation" for item in observed_qualifiers)
            else "affirmed"
        )
        modal_force = _modal_force(observed_qualifiers)
        conditions = sorted(
            {item.cue for item in observed_qualifiers if item.kind == "condition"}
        )
        exceptions = sorted(
            {item.cue for item in observed_qualifiers if item.kind == "exception"}
        )
        assertion_mode = (
            "attributed"
            if any(item.kind == "attribution" for item in observed_qualifiers)
            else (
                "hypothetical"
                if conditions and modal_force == "possible"
                else "reported"
            )
        )
        evidence_item = evidence[predicate.evidence_ref_id]
        subject_text = " | ".join(
            item.surface for item in arguments if item.role == "subject"
        )
        object_text = " | ".join(
            item.surface for item in arguments if item.role == "object"
        )
        canonical = " ".join(
            part
            for part in (
                subject_text.lower(),
                polarity.upper(),
                modal_force.upper(),
                predicate.predicate_lemma,
                object_text.lower(),
                "IF " + " | ".join(conditions).lower() if conditions else "",
                "EXCEPT " + " | ".join(exceptions).lower() if exceptions else "",
            )
            if part
        )
        identity = {
            "predicate_observation_id": predicate.observation_id,
            "canonical_proposition": canonical,
            "evidence_ref_id": predicate.evidence_ref_id,
        }
        candidates.append(
            ClaimAssertionCandidate(
                candidate_id="claim-candidate:"
                + domain_hash("claim-candidate", identity).split(":", 1)[1],
                proposition_text=evidence_item.quote,
                canonical_proposition=canonical,
                claim_type=_claim_type(predicate, observed_qualifiers),
                predicate_surface=predicate_span.text,
                predicate_lemma=predicate.predicate_lemma,
                arguments=arguments,
                polarity=polarity,
                modal_force=modal_force,
                assertion_mode=assertion_mode,
                conditions=conditions,
                exceptions=exceptions,
                evidence_ref_ids=[predicate.evidence_ref_id],
                producer=bundle.producer,
            )
        )
    return candidates


def validate_evidence_round_trip(bundle: ObservationBundle, text: str) -> list[str]:
    """Return validation errors instead of silently repairing bad evidence."""

    errors: list[str] = []
    for item in bundle.evidence_refs:
        if item.coordinate_system != "chunk_char":
            continue
        if item.end > len(text) or text[item.start : item.end] != item.quote:
            errors.append(f"evidence_round_trip:{item.evidence_ref_id}")
    for item in bundle.spans:
        if item.end > len(text) or text[item.start : item.end] != item.text:
            errors.append(f"span_round_trip:{item.observation_id}")
    return errors


def normalized_cue(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())
