"""Selective parse-once relation fast path and predicate compiler."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence, get_args

import yaml

from models.graphify_contracts import (
    AssertionDecisionV1,
    CompletedMentionV1,
    DocumentEntityV1,
    EntityTerminalState,
    NormalizedDocumentV1,
    RelationTerminalState,
    SurfaceRelationV1,
    stable_digest,
    stable_id,
)
from services.extraction.canonical import endpoint_mint_policy, normalize_entity_type
from services.extraction.corroboration_gate import evaluate_relation, load_policy
from services.extraction.dep_path_extractor import pair_allowed
from services.extraction.frame_extractor import FrameExtractor
from services.extraction.graphify_normalization import to_original_span
from services.extraction.graphify_assertion_semantics import (
    ASSERTION_NOUN_LEMMAS,
    CLAIM_REJECTION_LEMMAS,
    FALSE_MODIFIER_LEMMAS,
    nominal_assertion_qualification,
)
from services.extraction.graphify_value_ir import extract_temporal_qualifier
from services.extraction.graphify_unit_kind import (
    SEMANTIC_KINDS,
    classify_document_blocks,
)
from services.extraction.syntax_lane import build_union_evidence, generate_syntax_records
from services.extraction.graphify_survey import DocumentSurveyV1
from services.ghost_b_schemas import Predicate

RELATION_RELEASE = "graphify-relation-fast-path-v13"
PREDICATE_COMPILER_RELEASE = "graphify-predicate-compiler-v2"

from services.extraction.config_locator import find_config_dir

_CONFIG_DIR = find_config_dir(__file__)
_PREDICATE_CONFIG = _CONFIG_DIR / "predicate_synonyms.yaml"
_ONTOLOGY_CONFIG = _CONFIG_DIR / "ontology.yaml"
_RELATION_CUE_RE = re.compile(
    r"\b(?:use[sd]?|appl(?:y|ies|ied)|depend(?:s|ed)?|support(?:s|ed)?|produce[sd]?|consume[sd]?|"
    r"own(?:s|ed|ing)?|cause[sd]?|deriv(?:e[sd]?|ed)|define[sd]?|implement(?:s|ed)?|"
    r"store[sd]?|project(?:s|ed)?|power(?:s|ed)?|measure[sd]?|detect(?:s|ed)?|run(?:s|ning)?|"
    r"acquir(?:e[sd]?|ed)|extractor|"
    r"publish(?:es|ed)?|author(?:s|ed)?|curat(?:e[sd]?|ed)|interoperate[sd]?|"
    r"occur(?:s|red)?|serve[sd]?|related|part|component|build|builds|built)\b",
    re.I,
)
_QUALIFIER_RE = re.compile(r"\b(?:may|might|could|would|should|if|claims?|suggests?|recommends?|denied?)\b", re.I)
_META_PREDICATE_RE = re.compile(
    r"\b(?:predicate\s+compiler|compiler)\b[^.\n]*\b(?:maps?|stores?)\b[^.\n]*"
    r"\b(?:predicate|unmapped\s+surface\s+relation|depends[_ ]on|related[_ ]to)\b",
    re.I,
)
logger = logging.getLogger(__name__)
_SPACY_PARSE_LOCK = threading.Lock()
_CLOSED_CLASS_POS = frozenset({"AUX", "DET", "ADP", "CCONJ", "SCONJ", "PART", "PUNCT"})
_OPEN_ONLY_LEMMAS = frozenset({"serve", "publish", "author", "curate", "interoperate", "occur"})
# "X occurred in/at/on/near/during Y" is an explicit event-association surface;
# the closed ontology represents it as related_to (generalizes the former
# 'occurred in'-only rule).
_OCCURRED_PREP_RE = re.compile(r"occur(?:s|red|ring)?\s+(?:in|at|on|near|during)")
_CANONICAL_BY_LEMMA = {
    "use": "uses", "apply": "uses", "depend": "depends_on", "support": "supports",
    "produce": "produces", "consume": "consumes", "own": "owns",
    "cause": "causes", "derive": "derived_from", "define": "defines",
    "implement": "implements", "build": "depends_on",
    "store": "stores", "project": "projects", "power": "powers",
    "measure": "measures", "detect": "detects", "run": "runs_on",
}
_PREPOSITION_BY_LEMMA = {"depend": "on", "derive": "from", "build": "on"}
_OPEN_PREPOSITION_BY_LEMMA = {
    "serve": "in", "interoperate": "with", "occur": None,
}
_ATTRIBUTION_LEMMAS = frozenset({
    "allege", "assert", "believe", "claim", "deny", "indicate", "recommend",
    "refute", "reject", "report", "say", "show", "state", "suggest", "think",
})
# Closed-class light/control/copular verbs that carry no relational content of
# their own; open-verb discovery skips them (attribution verbs are discourse
# operators and are likewise excluded — their complements flow via qualifiers).
_NON_RELATIONAL_VERB_LEMMAS = frozenset({
    "be", "have", "do", "make", "take", "get", "give", "go", "come", "become",
    "remain", "seem", "appear", "keep", "let", "begin", "start", "continue",
    "stop", "finish", "try", "want", "need", "help", "tend", "manage",
})


def _open_verb_candidate(token, lemma: str, canonical: str | None, open_relation: bool) -> bool:
    """A verb outside every configured lemma table may still propose an open
    relation: the surface predicate is preserved and the compiler decides
    (synonym mapping, review, or stored unmapped surface relation)."""
    return (
        canonical is None
        and not open_relation
        and lemma not in {"part", "component", "relate"}
        and token.pos_ == "VERB"
        and token.is_alpha
        and lemma not in _ATTRIBUTION_LEMMAS
        and lemma not in _NON_RELATIONAL_VERB_LEMMAS
    )
_DENIAL_LEMMAS = frozenset({"deny", "refute", "reject"})
_CONDITIONAL_MARKERS = frozenset({"if", "unless"})
_GENERIC_RELATION_ARGUMENTS = frozenset({
    "it", "they", "this", "that", "these", "those", "we", "he", "she",
    "information", "reports", "report", "owner", "goal", "end goal",
    "thing", "result", "data", "software", "service", "artifact",
    "document", "event", "process", "method", "system", "component",
})
_UNIT_BOUNDARY_RE = re.compile(
    r"[.!?]+(?:[\"')\]]+)?(?=\s+(?:[#A-Z0-9]|$)|$)|\n+"
)
_ABBREVIATION_TOKENS = frozenset({
    "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "vs", "etc", "cf", "al",
    "inc", "ltd", "corp", "co", "dept", "fig", "eq", "no", "approx", "est",
})
_MD_STRUCTURE_PREFIX_RE = re.compile(r"(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|\||```|>)")


def _unit_boundary_positions(text: str) -> list[int]:
    """Unit boundaries that survive hard line-wraps and abbreviations.

    A single newline inside a paragraph is a soft wrap, not a sentence end: it
    only bounds a unit when adjacent to markdown structure or after terminal
    punctuation. A period closing a title/Latin abbreviation or a single-letter
    initial does not end a sentence.
    """
    positions: list[int] = []
    for match in _UNIT_BOUNDARY_RE.finditer(text):
        token = match.group(0)
        if token.startswith("\n"):
            if token.count("\n") >= 2:
                positions.append(match.end())
                continue
            line_start = text.rfind("\n", 0, match.start()) + 1
            prev_line = text[line_start:match.start()].strip()
            next_text = text[match.end():match.end() + 8].lstrip(" ")
            if (
                _MD_STRUCTURE_PREFIX_RE.match(next_text)
                or _MD_STRUCTURE_PREFIX_RE.match(prev_line)
                or (prev_line and prev_line[-1] in ".!?:;\"”’)")
            ):
                positions.append(match.end())
            continue
        head = re.search(r"[A-Za-z][A-Za-z.]*$", text[: match.start()])
        if head:
            word = head.group(0).casefold()
            last_segment = word.rsplit(".", 1)[-1]
            if word in _ABBREVIATION_TOKENS or (len(last_segment) == 1 and last_segment.isalpha()):
                continue
        positions.append(match.end())
    return positions


def _valid_endpoint_surface(value: str) -> bool:
    """Reject empty and punctuation-only relation endpoint sentinels."""
    stripped = value.strip()
    return bool(stripped and re.search(r"[A-Za-z0-9]", stripped))


def _mask_markdown_for_parse(value: str) -> str:
    """Replace Markdown delimiters and soft line-wraps with equal-length spaces
    so the parser sees one flowing sentence while offsets stay stable."""
    return re.sub(r"[*_~`\r\n]", " ", value)


@dataclass(frozen=True)
class RelationEligibilityDecision:
    unit_id: str
    document_id: str
    start: int
    end: int
    eligible: bool
    mention_count: int
    reasons: tuple[str, ...]
    unit_kind: str = "prose"
    # Structural lineage (#4, owner-ratified): provenance/structure never
    # disappears between stages — every downstream observation joins back to
    # this row by unit_id.
    heading_path: tuple[str, ...] = ()
    section_id: str = ""
    structural_parent: str = ""
    definition_subject: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id, "document_id": self.document_id,
            "start": self.start, "end": self.end, "eligible": self.eligible,
            "mention_count": self.mention_count, "reasons": list(self.reasons),
            "unit_kind": self.unit_kind,
            "heading_path": list(self.heading_path),
            "section_id": self.section_id,
            "structural_parent": self.structural_parent,
            "definition_subject": self.definition_subject,
        }


@dataclass(frozen=True)
class RelationFastPathOutput:
    eligibility: tuple[RelationEligibilityDecision, ...]
    endpoint_entities: tuple[DocumentEntityV1, ...]
    endpoint_mentions: tuple[CompletedMentionV1, ...]
    surface_relations: tuple[SurfaceRelationV1, ...]
    mapped_relations: tuple[SurfaceRelationV1, ...]
    assertions: tuple[AssertionDecisionV1, ...]
    report: dict[str, object]


@dataclass(frozen=True)
class _Unit:
    unit_id: str
    document_id: str
    start: int
    end: int
    text: str
    mentions: tuple[CompletedMentionV1, ...]
    unit_kind: str = "prose"
    heading_path: tuple[str, ...] = ()
    section_id: str = ""
    structural_parent: str = ""
    definition_subject: str = ""


@dataclass(frozen=True)
class _Proposal:
    subject: CompletedMentionV1
    object: CompletedMentionV1
    surface_predicate: str
    lemma: str
    particle: str
    preposition: str
    dependency_frame: str
    dependency_path: str
    voice: str
    polarity: str
    modality: str
    attribution: str
    canonical_hint: str | None
    confidence: float
    source: str


class PredicateCompiler:
    def __init__(self) -> None:
        predicate_payload = yaml.safe_load(_PREDICATE_CONFIG.read_text(encoding="utf-8"))
        ontology_payload = yaml.safe_load(_ONTOLOGY_CONFIG.read_text(encoding="utf-8"))
        self.synonyms = {
            str(key).casefold(): str(value)
            for key, value in dict(predicate_payload.get("synonyms") or {}).items()
            if value and value != "__DROP__"
        }
        # Surfaces whose grammatical subject is the semantic OBJECT
        # ("X contains Y" asserts Y part_of X): mapping succeeds only with
        # endpoint inversion, marked via :inverse_direction in the rule.
        self.inverse_synonyms = {
            str(key).casefold(): str(value)
            for key, value in dict(predicate_payload.get("inverse_synonyms") or {}).items()
            if value and value != "__DROP__"
        }
        # The canonical predicate contract is broader than ontology.yaml.
        # Missing allowed_pairs means unconstrained, not non-canonical.
        self.allowed_predicates = frozenset((*get_args(Predicate), "consumes"))
        self.config_hash = stable_digest({
            "predicate": predicate_payload,
            "ontology": ontology_payload,
            "release": PREDICATE_COMPILER_RELEASE,
        })

    def compile(
        self,
        *,
        surface: str,
        lemma: str,
        canonical_hint: str | None,
        subject_type: str,
        object_type: str,
        subject_name: str = "",
        object_name: str = "",
        source: str,
    ) -> tuple[str | None, str]:
        normalized_lemma = lemma.casefold().strip()
        occurred_prep = bool(_OCCURRED_PREP_RE.fullmatch(surface.casefold().strip()))
        if normalized_lemma in _OPEN_ONLY_LEMMAS and not occurred_prep:
            return None, f"open:{normalized_lemma}:frozen_policy_abstain"
        if occurred_prep:
            # Explicit event-association surface; declared closed-ontology
            # normalization, never a forced fallback.
            if source.startswith("openie"):
                return "related_to", "mapped:openie:declared_closed_ontology:occurred_prep:related_to"
            return "related_to", f"mapped:{source}:declared_closed_ontology:occurred_prep:related_to"
        candidate = canonical_hint
        inverted = False
        if candidate is None:
            keys = {surface.casefold().strip(), normalized_lemma}
            candidates = {
                value for key, value in self.synonyms.items() if key in keys
            }
            inverse_candidates = {
                value for key, value in self.inverse_synonyms.items() if key in keys
            }
            if not candidates and not inverse_candidates:
                # Meaningful relation with no ontology representation: store as
                # an open/unmapped surface relation — never force related_to,
                # never silently drop.
                open_key = normalized_lemma or surface.casefold().strip() or "surface"
                return None, f"open:{open_key}:unmapped_surface_relation"
            if len(candidates) + len(inverse_candidates) != 1:
                return None, "review:ambiguous_predicate"
            if inverse_candidates:
                # Inverse-direction surface ("X contains Y" = Y part_of X):
                # the semantic subject is the surface object — signature
                # checks and the compiled relation both swap endpoints.
                candidate = next(iter(inverse_candidates))
                inverted = True
                subject_type, object_type = object_type, subject_type
                subject_name, object_name = object_name, subject_name
            else:
                candidate = next(iter(candidates))
        marker = ":inverse_direction" if inverted else ""
        if candidate == "related_to" and "related" not in surface.casefold() and not occurred_prep:
            return None, "review:forced_related_to_prohibited"
        if candidate not in self.allowed_predicates and candidate != "related_to":
            return None, "review:predicate_outside_frozen_ontology"
        subject_ontology = normalize_entity_type(subject_type)
        object_ontology = normalize_entity_type(object_type)
        if not pair_allowed(candidate, subject_ontology, object_ontology):
            compatible_pairs = [
                (candidate_subject, candidate_object)
                for candidate_subject in self._ontology_roles(subject_ontology, subject_name)
                for candidate_object in self._ontology_roles(object_ontology, object_name)
                if pair_allowed(candidate, candidate_subject, candidate_object)
            ]
            if compatible_pairs:
                minimum_coercions = min(
                    int(candidate_subject != subject_ontology)
                    + int(candidate_object != object_ontology)
                    for candidate_subject, candidate_object in compatible_pairs
                )
                compatible_pairs = [
                    (candidate_subject, candidate_object)
                    for candidate_subject, candidate_object in compatible_pairs
                    if int(candidate_subject != subject_ontology)
                    + int(candidate_object != object_ontology) == minimum_coercions
                ]
            if len(compatible_pairs) != 1:
                return candidate, f"review:endpoint_signature:{subject_ontology}:{object_ontology}{marker}"
            resolved_subject, resolved_object = compatible_pairs[0]
            return candidate, (
                f"mapped:{source}:{normalized_lemma}:endpoint_role:"
                f"{resolved_subject}:{resolved_object}{marker}"
            )
        return candidate, f"mapped:{source}:{normalized_lemma}{marker}"

    @staticmethod
    def _ontology_roles(entity_type: str, name: str) -> tuple[str, ...]:
        """Resolve only explicit lexical roles into the frozen ontology.

        The entity lane intentionally uses a coarse Software class for named
        services, APIs, workers, and processes. Relation signatures distinguish
        those functional roles as Method. This adapter does not mutate either
        the entity or ontology; it supplies one deterministic signature role.
        """
        roles = [entity_type]
        if entity_type == "Product":
            # The census "Product" class is a specifically named commercial
            # product, service offering, or manufactured system; such endpoints
            # also satisfy Software-role signatures. Name-independent role.
            roles.append("Software")
        if entity_type == "Software" and re.search(
            r"\b(?:api|service|process|worker|workflow|planner|writer|implementation|prototype|platform)\b",
            name,
            re.I,
        ):
            roles.append("Method")
        if entity_type == "Concept" and re.search(
            r"\b(?:latency|lag|objective|duration|rate|count|throughput)\b",
            name,
            re.I,
        ):
            roles.append("Metric")
        if entity_type == "Event" and re.search(r"\bevent\b", name, re.I) and not re.search(
            r"\bnormalized\s+event\b", name, re.I,
        ):
            roles.append("Concept")
        if entity_type == "Event" and re.search(r"\bnormalized\s+event\b", name, re.I):
            roles.append("Artifact")
        return tuple(dict.fromkeys(roles))


@lru_cache(maxsize=1)
def predicate_compiler() -> PredicateCompiler:
    return PredicateCompiler()


def _unit_rows(
    document: NormalizedDocumentV1,
    survey: DocumentSurveyV1,
    mentions: Sequence[CompletedMentionV1],
) -> tuple[tuple[_Unit, ...], tuple[RelationEligibilityDecision, ...]]:
    units: list[_Unit] = []
    decisions: list[RelationEligibilityDecision] = []
    indexed_mentions = sorted(
        enumerate(mentions),
        key=lambda item: (item[1].normalized_start, item[1].normalized_end, item[0]),
    )
    mention_starts = [item.normalized_start for _index, item in indexed_mentions]
    classified = {
        item.block_id: item for item in classify_document_blocks(document, survey)
    }
    for block_index, block in enumerate(survey.blocks):
        unit_kind = classified[block.block_id].kind
        heading_path: tuple[str, ...] = ()
        for heading in survey.headings:
            if heading.start <= block.start:
                heading_path = heading.path
            else:
                break
        section_id = stable_id("section", document.document_id, *heading_path)
        block_definition_subject = ""
        if unit_kind == "definition":
            overlapping = [
                item.term for item in survey.definitions
                if item.start < block.end and block.start < item.end
            ]
            if overlapping:
                block_definition_subject = overlapping[0]
            else:
                head_line = block.text.strip().splitlines()[0] if block.text.strip() else ""
                if ":" in head_line:
                    block_definition_subject = head_line.split(":", 1)[0].strip().lstrip("#>*- ").strip()
        block_text = block.text.rstrip("\n")
        boundaries = [0, *_unit_boundary_positions(block_text), len(block_text)]
        seen_spans: set[tuple[int, int]] = set()
        for unit_index, (raw_start, raw_end) in enumerate(zip(boundaries, boundaries[1:])):
            while raw_start < raw_end and block_text[raw_start].isspace():
                raw_start += 1
            while raw_end > raw_start and block_text[raw_end - 1].isspace():
                raw_end -= 1
            if raw_end <= raw_start or (raw_start, raw_end) in seen_spans:
                continue
            seen_spans.add((raw_start, raw_end))
            start = block.start + raw_start
            end = block.start + raw_end
            text = document.normalized_text[start:end]
            start_index = bisect_left(mention_starts, start)
            end_index = bisect_left(mention_starts, end)
            local_mentions = tuple(
                mention
                for _original_index, mention in sorted(
                    indexed_mentions[start_index:end_index], key=lambda item: item[0],
                )
                if mention.normalized_end <= end
            )
            cue = bool(_RELATION_CUE_RE.search(text))
            metalinguistic = bool(_META_PREDICATE_RE.search(text))
            reasons: list[str] = []
            eligible = False
            if unit_kind not in SEMANTIC_KINDS:
                # Representation routing (owner-ratified): navigation/metadata/
                # code/table units keep full provenance but never enter
                # semantic NLP — deterministic lanes own them.
                reasons.append(f"unit_kind:{unit_kind}")
            elif metalinguistic:
                reasons.append("metalinguistic_predicate_example")
            elif len(local_mentions) >= 2 and cue:
                eligible = True
                reasons.extend(("two_document_mentions", "relation_cue"))
            elif len(local_mentions) >= 2 and not block.furniture_candidate:
                # Structural eligibility: two entity mentions co-occurring in a
                # prose unit license extraction regardless of predicate
                # vocabulary. The ontology decides how a discovered relation is
                # interpreted — never which language the extractor may see.
                eligible = True
                reasons.append("entity_pair_cooccurrence")
            elif len(local_mentions) >= 1 and cue:
                eligible = True
                reasons.extend(("one_document_mention", "strict_relation_bearing_cue"))
            elif block.furniture_candidate:
                reasons.append("structural_furniture")
            elif not cue:
                reasons.append("no_relation_cue")
            else:
                reasons.append("insufficient_endpoint_evidence")
            unit_id = stable_id(
                "relation-unit", document.document_id, block_index, unit_index, start, end, text,
            )
            decision = RelationEligibilityDecision(
                unit_id, document.document_id, start, end, eligible,
                len(local_mentions), tuple(reasons), unit_kind,
                heading_path, section_id, block.block_id, block_definition_subject,
            )
            decisions.append(decision)
            if eligible:
                units.append(_Unit(
                    unit_id, document.document_id, start, end, text, local_mentions,
                    unit_kind, heading_path, section_id, block.block_id,
                    block_definition_subject,
                ))
    return tuple(units), tuple(decisions)


def evaluate_relation_eligibility(
    documents: Sequence[NormalizedDocumentV1],
    surveys: Sequence[DocumentSurveyV1],
    mentions: Sequence[CompletedMentionV1],
) -> tuple[RelationEligibilityDecision, ...]:
    """Return the deterministic relation-unit census without loading spaCy."""
    mentions_by_document: dict[str, list[CompletedMentionV1]] = {}
    for mention in mentions:
        mentions_by_document.setdefault(mention.document_id, []).append(mention)
    decisions: list[RelationEligibilityDecision] = []
    for document, survey in zip(documents, surveys):
        _units, local_decisions = _unit_rows(
            document, survey, mentions_by_document.get(document.document_id, []),
        )
        decisions.extend(local_decisions)
    return tuple(decisions)


def _local_mentions(unit: _Unit) -> dict[tuple[int, int], CompletedMentionV1]:
    return {
        (mention.normalized_start - unit.start, mention.normalized_end - unit.start): mention
        for mention in unit.mentions
    }


def _previous_sentence_span(text: str, start: int) -> tuple[int, int] | None:
    end = start
    while end > 0 and text[end - 1].isspace():
        end -= 1
    if end <= 0:
        return None
    boundaries = [
        match.end() for match in _UNIT_BOUNDARY_RE.finditer(text[:end])
        if match.end() < end
    ]
    previous_start = boundaries[-1] if boundaries else 0
    while previous_start < end and text[previous_start].isspace():
        previous_start += 1
    return (previous_start, end) if previous_start < end else None


def _resolve_discourse_subject(
    document: NormalizedDocumentV1,
    unit: _Unit,
    doc,
    document_mentions: Sequence[CompletedMentionV1],
    entity_by_id: dict[str, DocumentEntityV1],
) -> tuple[_Unit, DocumentEntityV1 | None, CompletedMentionV1 | None]:
    """Resolve only unique, sentence-initial deterministic subject anaphors."""
    match = re.match(r"(?i)(it|they|that\s+event|the\s+[a-z][a-z-]*)\b", unit.text)
    if match is None:
        return unit, None, None
    local_start, local_end = match.span(1)
    subject_tokens = [
        token for token in doc
        if token.idx < local_end and token.idx + len(token.text) > local_start
        and token.dep_ in {"nsubj", "nsubjpass", "nsubj:pass"}
    ]
    if not subject_tokens:
        return unit, None, None
    surface = unit.text[local_start:local_end]
    lowered = surface.casefold()
    antecedents: list[CompletedMentionV1] = []
    if lowered.startswith("the ") and lowered != "that event":
        head = lowered.split()[-1]
        antecedents = [
            mention for mention in document_mentions
            if mention.normalized_end <= unit.start
            and entity_by_id[mention.entity_id].canonical_name.casefold().split()[-1:] == [head]
        ]
        entity_ids = {item.entity_id for item in antecedents}
        if len(entity_ids) != 1:
            return unit, None, None
        antecedent = max(antecedents, key=lambda item: item.normalized_end)
    else:
        previous_span = _previous_sentence_span(document.normalized_text, unit.start)
        if previous_span is None:
            return unit, None, None
        previous_start, previous_end = previous_span
        previous_text = document.normalized_text[previous_start:previous_end]
        previous_mentions = [
            mention for mention in document_mentions
            if previous_start <= mention.normalized_start
            and mention.normalized_end <= previous_end
            and entity_by_id[mention.entity_id].canonical_name.casefold().strip()
            not in {"a", "an", "the"}
        ]
        cue = _RELATION_CUE_RE.search(previous_text)
        if cue is not None:
            before_cue = [
                mention for mention in previous_mentions
                if mention.normalized_end <= previous_start + cue.start()
            ]
            entity_ids = {item.entity_id for item in before_cue}
            if len(entity_ids) != 1:
                return unit, None, None
            antecedent = max(before_cue, key=lambda item: item.normalized_end)
        else:
            if not previous_mentions:
                return unit, None, None
            antecedent = min(previous_mentions, key=lambda item: item.normalized_start)
            if antecedent.normalized_start - previous_start > 40:
                return unit, None, None
        entity = entity_by_id[antecedent.entity_id]
        canonical = entity.canonical_name.casefold()
        if lowered == "that event" and not (
            normalize_entity_type(entity.entity_type) == "Event"
            or canonical.endswith(" event")
        ):
            return unit, None, None
        if lowered == "they" and not (
            canonical.endswith("s")
            or re.search(r"\b(?:assertions|batches|records|events|metrics)\b", canonical)
        ):
            return unit, None, None
        if lowered == "it" and canonical.endswith("s"):
            return unit, None, None
    entity = entity_by_id[antecedent.entity_id]
    start, end = unit.start + local_start, unit.start + local_end
    original = to_original_span(document, start, end)
    if not original.exact:
        return unit, None, None
    mention_id = stable_id(
        "discourse-subject-mention", document.document_id, entity.entity_id,
        start, end, RELATION_RELEASE,
    )
    mention = CompletedMentionV1(
        mention_id=mention_id, entity_id=entity.entity_id,
        document_id=document.document_id, surface=surface,
        normalized_start=start, normalized_end=end,
        original_start=original.start, original_end=original.end,
        source="variant", context_rule="unique_deterministic_subject_antecedent",
        completion_release=RELATION_RELEASE,
    )
    updated_entity = entity.model_copy(update={
        "mention_ids": tuple(sorted({*entity.mention_ids, mention_id})),
    })
    mentions = tuple(sorted(
        [*unit.mentions, mention],
        key=lambda item: (item.normalized_start, item.normalized_end, item.mention_id),
    ))
    return replace(unit, mentions=mentions), updated_entity, mention


def _mentions_for_token(token, unit: _Unit) -> list[CompletedMentionV1]:
    tokens = [token, *list(token.conjuncts)]
    frontier = list(tokens)
    coordinated = False
    while frontier:
        current = frontier.pop()
        if any(child.dep_ == "cc" for child in current.children):
            coordinated = True
        for child in current.children:
            coordinated_nmod = child.dep_ == "nmod" and (
                any(grandchild.dep_ == "cc" for grandchild in child.children)
                or any(grandchild.dep_ == "conj" for grandchild in child.children)
            )
            if (
                child.dep_ in {"conj", "appos", "npadvmod"} or coordinated_nmod
            ) and child not in tokens:
                tokens.append(child)
                frontier.append(child)
    if not coordinated:
        tokens = [token, *list(token.conjuncts)]
    # A name may live beside the argument head rather than under it:
    # apposition ("Quartzline's archival store, Vaultstone, ...") and
    # naming participles ("a component called Trellis", "a service known as
    # Falcon"). These are general grammatical name positions, always kept.
    for anchor in [token, *list(token.conjuncts)]:
        for child in anchor.children:
            if child.dep_ == "appos" and child not in tokens:
                tokens.append(child)
            if child.dep_ == "acl" and child.lemma_.casefold() in {"call", "name", "dub"}:
                tokens.extend(
                    named for named in _children(child, {"oprd", "dobj", "attr"})
                    if named not in tokens
                )
            if child.dep_ == "acl" and child.lemma_.casefold() == "know":
                tokens.extend(
                    named for named in _prep_objects(child, "as") if named not in tokens
                )
    output: list[CompletedMentionV1] = []
    for item in tokens:
        matches = [
            mention for mention in unit.mentions
            if item.idx < mention.normalized_end - unit.start
            and item.idx + len(item.text) > mention.normalized_start - unit.start
        ]
        if matches:
            output.append(max(matches, key=lambda mention: (
                mention.normalized_end - mention.normalized_start,
                -mention.normalized_start,
            )))
    return list({item.mention_id: item for item in output}.values())


def _children(token, dependencies: set[str]) -> list[Any]:
    return [child for child in token.children if child.dep_ in dependencies]


def _prep_objects(token, preposition: str | None) -> list[Any]:
    output = []
    for child in token.children:
        if child.dep_ not in {"prep", "agent"}:
            continue
        if preposition is not None and child.text.casefold() != preposition:
            continue
        direct = _children(child, {"pobj", "obj"})
        for item in direct:
            if item.lemma_.casefold() == "top":
                nested = _prep_objects(item, "of")
                output.extend(nested or [item])
            else:
                output.append(item)
    return output


def _has_negation(token) -> bool:
    return any(child.dep_ == "neg" for child in token.children)


_ASSERTION_CONTEXT_CONFIG = _CONFIG_DIR / "assertion_context.yaml"


@lru_cache(maxsize=1)
def _assertion_context() -> dict:
    """Assertion-context layer registry (systematic completion 2026-08-08).
    Missing file = empty registry (mechanisms inert)."""
    try:
        payload = yaml.safe_load(_ASSERTION_CONTEXT_CONFIG.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    return {key: frozenset(str(v).casefold() for v in values or ())
            for key, values in payload.items()}


def _inverted_conditional_clause(node) -> bool:
    """Marker-less counterfactual protasis: subject-aux inversion — the
    clause's auxiliary (had/were/should) PRECEDES its subject
    ("Had X depended on Y, ...", "Should X fail, ...")."""
    aux_lemmas = _assertion_context().get("inversion_auxiliaries", frozenset())
    subjects = [c for c in node.children if c.dep_ in {"nsubj", "nsubjpass", "nsubj:pass"}]
    auxes = [c for c in node.children if c.dep_ in {"aux", "auxpass"}
             and c.lower_ in aux_lemmas]  # surface form IS the inversion signal
    return bool(subjects and auxes and min(a.i for a in auxes) < min(s.i for s in subjects))


def _interrogative_scope(token) -> bool:
    """The clause containing the relation is interrogative content: a
    whether/if COMPLEMENT (mark on the clause, complement position), a
    wh-question, or a question-mark sentence. Questions assert nothing."""
    marks = _assertion_context().get("interrogative_marks", frozenset())
    for node in (token, *token.ancestors):
        for child in node.children:
            if child.dep_ == "mark" and child.lemma_.casefold() in marks:
                if child.lemma_.casefold() == "if" and node.dep_ not in {"ccomp", "csubj", "acl"}:
                    continue  # adverbial 'if' is conditional scope, handled there
                return True
    sent = token.sent
    if sent.text.rstrip().endswith("?"):
        return True
    return False


def _adjectival_epistemic_uncertain(token) -> bool:
    """'It is unresolved/unclear/unknown whether|that X ...' — the clause is
    governed by an uncertainty predicate adjective; content is not asserted."""
    adjectives = _assertion_context().get("epistemic_uncertainty_adjectives", frozenset())
    for node in (token, *token.ancestors):
        if node.dep_ in {"ccomp", "csubj", "xcomp", "acl"} and node.i > node.head.i:
            head = node.head
            if head.pos_ == "ADJ" and head.lemma_.casefold() in adjectives:
                return True
            if head.lemma_.casefold() == "be" and any(
                c.dep_ in {"acomp", "attr"} and c.lemma_.casefold() in adjectives
                for c in head.children
            ):
                return True
            for c in head.children:
                if c.dep_ in {"acomp", "attr", "oprd"} and c.lemma_.casefold() in adjectives:
                    return True
    return False


def _directive_scope(token) -> bool:
    """Mandative complements ('requires that X use Y') and imperative roots
    ('Configure X to consume Y') are directives, never facts."""
    directives = _assertion_context().get("directive_governors", frozenset())
    for governor in _embedded_governor_chain(token):
        if governor.lemma_.casefold() in directives:
            return True
    root = token.sent.root
    if (
        root.tag_ == "VB"
        and not any(c.dep_ in {"nsubj", "nsubjpass", "nsubj:pass"} for c in root.children)
        and not token.sent.text.rstrip().endswith("?")
    ):
        return True
    return False


def _negative_adverb(token) -> bool:
    adverbs = _assertion_context().get("negative_adverbs", frozenset())
    for node in (token, token.head):
        for child in node.children:
            lemma = child.lemma_.casefold()
            if child.dep_ == "advmod" and lemma in adverbs:
                return True
            if child.lower_ == "longer" and any(
                g.dep_ == "neg" or g.lower_ == "no" for g in child.children
            ):
                return True
    return False


_EPISTEMIC_CONFIG = _CONFIG_DIR / "epistemic_governors.yaml"


@lru_cache(maxsize=1)
def _epistemic_classes() -> dict[str, str]:
    """Declarative governor-class membership (freeze-exception 2026-08-08).

    The mechanism below is grammatical and fixed; this registry supplies
    members only. Missing file = empty registry (mechanism inert)."""
    try:
        payload = yaml.safe_load(_EPISTEMIC_CONFIG.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    out: dict[str, str] = {}
    for cls, members in (payload.get("classes") or {}).items():
        for lemma in members or ():
            out[str(lemma).casefold()] = str(cls)
    return out


_MODAL_AUX = frozenset({"may", "might", "could", "would", "should"})


def _embedded_governor_chain(token, max_depth: int = 4):
    """Bounded climb through embedded-proposition arcs: for a relation
    predicate inside a complement clause, yield the governing predicates."""
    chain = []
    current = token
    depth = 0
    while current.head.i != current.i and depth < max_depth:
        # A complement clause FOLLOWS its governing predicate in English
        # ("did not establish that X..."); an embedded arc pointing at a
        # LATER governor is a parser-glued independent clause (semicolon
        # joins), never that governor's content.
        if (
            current.dep_ in {"ccomp", "xcomp", "csubj", "advcl", "acl"}
            and current.i > current.head.i
        ):
            chain.append(current.head)
        current = current.head
        depth += 1
    return chain


def _epistemic_nonfact(token) -> bool:
    """An epistemic/evidential governor blocks its embedded proposition when
    the governor is negated ('did not establish that', 'could not confirm
    that') or is itself negative-implicative ('failed to demonstrate').
    Positive evidential governors leave the embedded assertion intact —
    the mechanism contains, it never blanket-demotes."""
    classes = _epistemic_classes()
    for governor in _embedded_governor_chain(token):
        cls = classes.get(governor.lemma_.casefold())
        if cls is None:
            continue
        if cls == "negative_implicative":
            return True
        if _has_negation(governor):
            return True
    return False


def _catenative_chain_context(token) -> tuple[bool, bool, bool]:
    """Inherited assertion context through the xcomp catenative chain:
    'might begin consuming' modalizes 'consuming'; 'was expected to begin
    using' marks the chain prospective. Returns (modal, negated, attitude)."""
    modal = negated = attitude = False
    classes = _epistemic_classes()
    current = token
    depth = 0
    while current.dep_ == "xcomp" and current.head.i != current.i and depth < 4:
        governor = current.head
        if any(
            child.dep_ in {"aux", "auxpass"} and child.lemma_.casefold() in _MODAL_AUX
            for child in governor.children
        ):
            modal = True
        if _has_negation(governor):
            negated = True
        if classes.get(governor.lemma_.casefold()) == "attitude_prospective":
            attitude = True
        current = governor
        depth += 1
    return modal, negated, attitude


def _marked_conditional_clause(token) -> bool:
    markers = _CONDITIONAL_MARKERS | _assertion_context().get("conditional_markers_extra", frozenset())
    return any(
        child.dep_ == "mark" and child.lemma_.casefold() in markers
        for child in token.children
    )


def _conditional_scope(token, predicate) -> bool:
    # The predicate is inside an if/unless adverbial clause.
    if any(
        node.dep_ == "advcl" and _marked_conditional_clause(node)
        for node in (token, *token.ancestors)
    ):
        return True
    # Marker-less counterfactual protasis via subject-aux inversion:
    # "Had X depended on Y, ..." — aux-before-subject with had/were/should
    # is ALWAYS a non-assertion context in English (conditional inversion,
    # question, or negative inversion), so the inversion shape decides on
    # ANY clause label — broken parses relabel the clause but cannot
    # destroy the inversion signal.
    if any(
        _inverted_conditional_clause(node)
        for node in (token, *token.ancestors)
    ):
        return True
    # An if/unless adverbial directly modifies this governing predicate.
    return any(
        child.dep_ == "advcl" and _marked_conditional_clause(child)
        for child in predicate.children
    )


def _attribution_governors(token) -> list[Any]:
    governors = []
    current = token
    while current.head.i != current.i:
        governor = current.head
        if (
            current.dep_ in {"ccomp", "xcomp"}
            and governor.lemma_.casefold() in _ATTRIBUTION_LEMMAS
        ):
            governors.append(governor)
        current = governor
    return governors


def _claim_noun_governor(token) -> Any | None:
    """Return the claim noun whose complement clause contains this token.

    "the statement/claim/assertion that X ..." embeds X under the noun via an
    acl (occasionally ccomp) arc. A proposition inside that complement is the
    noun's content, never a direct assertion of the sentence.
    """
    current = token
    while current.head.i != current.i:
        head = current.head
        if (
            current.dep_ in {"acl", "ccomp"}
            and head.pos_ == "NOUN"
            and head.lemma_.casefold() in ASSERTION_NOUN_LEMMAS
        ):
            return head
        current = head
    return None


def _claim_noun_rejected(noun) -> bool:
    """True when the claim noun's context marks its content false/withdrawn:
    a false-modifier ("the incorrect claim that ..."), a rejecting matrix verb
    ("rejected the statement that ..."), or phrasal walk-back."""
    if any(
        child.dep_ == "amod" and child.lemma_.casefold() in FALSE_MODIFIER_LEMMAS
        for child in noun.children
    ):
        return True
    for ancestor in noun.ancestors:
        if ancestor.pos_ != "VERB":
            continue
        lemma = ancestor.lemma_.casefold()
        if lemma in CLAIM_REJECTION_LEMMAS or lemma in _DENIAL_LEMMAS:
            return True
        if lemma == "walk" and any(
            child.dep_ in {"prt", "advmod"} and child.lemma_.casefold() == "back"
            for child in ancestor.children
        ):
            return True
    return False


def _reported_adjunct_source(predicate) -> str | None:
    """Source of a sentence-PERIPHERAL 'according to X' adjunct, else None.

    Construction-level rule (owner-ratified): only a fronted adjunct
    ("According to X, P") or a comma-detached trailing one ("P, according
    to X") marks the clause as reported. A mid-clause manner/compliance use
    ("operates according to plan") never fires — it sits after the subject
    with no comma boundary.
    """
    sent = predicate.sent
    subject_index = min(
        (child.i for child in predicate.children if child.dep_ in {"nsubj", "nsubjpass"}),
        default=predicate.i,
    )
    for item in sent:
        if item.text.casefold() != "according":
            continue
        pobj = next((c for c in item.children if c.dep_ == "pobj"), None)
        if pobj is None:
            to_child = next((c for c in item.children if c.lemma_.casefold() == "to"), None)
            if to_child is not None:
                pobj = next((c for c in to_child.children if c.dep_ == "pobj"), None)
        if pobj is None:
            continue
        fronted = item.i < subject_index
        comma_detached = item.i > sent.start and item.doc[item.i - 1].text == ","
        if fronted or comma_detached:
            return " ".join(t.text for t in pobj.subtree).strip()
    return None


def _qualifiers(token, _text: str) -> tuple[str, str, str]:
    predicate = (
        token.head
        if token.dep_ in {"attr", "acomp", "oprd"} and token.head.lemma_.casefold() == "be"
        else token
    )
    governors = _attribution_governors(token)
    attribution = " > ".join(item.lemma_.casefold() for item in reversed(governors))
    reported_source = _reported_adjunct_source(predicate)
    if reported_source is not None:
        marker = f"according_to:{reported_source.casefold()}"
        attribution = f"{attribution} > {marker}" if attribution else marker
    claim_noun = _claim_noun_governor(token)
    claim_noun_rejected = claim_noun is not None and _claim_noun_rejected(claim_noun)
    if claim_noun is not None:
        nominal = f"nominal:{claim_noun.lemma_.casefold()}"
        attribution = f"{nominal} > {attribution}" if attribution else nominal
    direct_negated = _has_negation(token) or (predicate is not token and _has_negation(predicate))
    denied = any(item.lemma_.casefold() in _DENIAL_LEMMAS for item in governors) or claim_noun_rejected
    negated_attribution = any(_has_negation(item) for item in governors)
    chain_modal, chain_negated, chain_attitude = _catenative_chain_context(predicate)
    if denied or _epistemic_nonfact(token):
        polarity = "denied"
    elif (
        direct_negated or negated_attribution or chain_negated
        or _negative_adverb(predicate) or (predicate is not token and _negative_adverb(token))
    ):
        polarity = "negative"
    else:
        polarity = "positive"
    if _conditional_scope(token, predicate):
        modality = "conditional"
    elif _interrogative_scope(token):
        modality = "interrogative"
    elif _adjectival_epistemic_uncertain(token):
        modality = "uncertain"
    elif _directive_scope(token):
        modality = "directive"
    elif chain_modal or any(
        child.dep_ == "aux" and child.lemma_.casefold() in {"may", "might", "could", "would", "should"}
        for child in predicate.children
    ):
        modality = "hypothetical"
    elif attribution or chain_attitude:
        modality = "attributed"
    else:
        modality = "asserted"
    return polarity, modality, attribution


def _argument_tokens(token) -> tuple[list[Any], list[Any]]:
    """Return grammatical subject/object tokens for a licensed cue."""
    lemma = token.lemma_.casefold()
    passive_subjects = _children(token, {"nsubjpass", "nsubj:pass"})
    by_agents = _prep_objects(token, "by")
    if not passive_subjects and by_agents and token.dep_ in {"acl", "relcl"} and token.tag_ == "VBN":
        for ancestor in [token.head, *list(token.ancestors)]:
            passive_subjects = _children(ancestor, {"nsubj", "csubj", "nsubjpass", "nsubj:pass"})
            if passive_subjects:
                break
    if passive_subjects and by_agents:
        return by_agents, passive_subjects
    if lemma in {"part", "component", "relate"}:
        copula = token.head if token.head.lemma_.casefold() == "be" else token
        subjects = _children(copula, {"nsubj", "nsubjpass", "nsubj:pass"})
        # Gapping: "A is part of S, and so is B" — mirror of the proposal-side
        # rule so endpoint completion also mints B when the census missed it.
        for conjunct in copula.conjuncts:
            if conjunct.lemma_.casefold() == "be" and any(
                child.dep_ == "advmod" and child.lemma_.casefold() == "so"
                for child in conjunct.children
            ):
                subjects = [*subjects, *_children(conjunct, {"nsubj", "nsubjpass", "nsubj:pass"})]
        return subjects, _prep_objects(token, "of" if lemma in {"part", "component"} else "to")
    subject_dependencies = {"nsubj"}
    if lemma in {"build", "derive"}:
        subject_dependencies.update({"nsubjpass", "nsubj:pass"})
    subjects = _children(token, subject_dependencies)
    if not subjects:
        for auxiliary in _children(token, {"aux", "auxpass"}):
            recovered = _children(auxiliary, {"nsubj", "csubj", "nsubjpass", "nsubj:pass"})
            if recovered:
                subjects = recovered
                break
    required_prep = _PREPOSITION_BY_LEMMA.get(lemma)
    if lemma in _OPEN_ONLY_LEMMAS:
        required_prep = _OPEN_PREPOSITION_BY_LEMMA.get(lemma)
    objects = (
        _prep_objects(token, required_prep)
        if required_prep is not None
        else _children(token, {"dobj", "obj", "attr", "oprd"})
    )
    if lemma in _OPEN_ONLY_LEMMAS or _open_verb_candidate(
        token, lemma, _CANONICAL_BY_LEMMA.get(lemma), lemma in _OPEN_ONLY_LEMMAS,
    ):
        # Open verbs may relate through prepositional or dative complements;
        # surface all of them for endpoint completion (the mint policy gates junk).
        for child in token.children:
            if child.dep_ in {"prep", "dative"} and child.lemma_.casefold() != "by":
                objects = [*objects, *_children(child, {"pobj"})]
    objects = _explicit_list_objects(token.doc, token, objects)
    if not subjects and token.dep_ in {"xcomp", "ccomp", "conj", "relcl"}:
        subjects = _children(token.head, {"nsubj"})
    if not subjects and token.dep_ == "acl" and token.tag_ == "VBG":
        # Active participial modifier: "a lane containing X" — the modified
        # noun is the participle's subject (mirror of the VBN recovery).
        subjects = [token.head]
    if token.dep_ in {"xcomp", "ccomp"} and subjects and all(
        item.lemma_.casefold() in {"it", "they", "he", "she"} for item in subjects
    ):
        controlled = _children(token.head, {"nsubj"})
        if controlled:
            subjects = controlled
    return subjects, objects


def _explicit_list_objects(doc, cue, objects: list[Any]) -> list[Any]:
    """Extend a frame's objects across an explicit comma/and NP list.

    Purely structural: members are NP heads joined by comma (optionally
    "and"/"or") separators; the walk stops at any verb, subject, or
    sentence boundary, so independent clauses never merge. Licensing is
    textual adjacency, not conj arcs — the small parser fractures long
    coordinations into disconnected islands ("... lane containing source
    timestamps, shot boundaries, actor masks, pose tracks ..." parses
    "shot" as a second acl and "pose" as a fresh verb), and arc-following
    strands every member past the first fracture.
    """
    if not objects:
        return objects
    output = list(objects)
    previous_end = _argument_span(doc, output[-1])[1]
    for candidate in doc[output[-1].i + 1:]:
        if candidate.is_sent_end or candidate.text in {".", ";"}:
            break
        if candidate.pos_ in {"VERB", "AUX"}:
            break
        # A subject belongs to the next clause, never to this list.
        if candidate.dep_ in {"nsubj", "nsubjpass", "nsubj:pass"}:
            break
        if candidate.pos_ not in {"NOUN", "PROPN"} or candidate.dep_ == "compound":
            continue
        start, end = _argument_span(doc, candidate)
        separator = doc.text[previous_end:start]
        if not re.fullmatch(r"\s*,\s*(?:and\s+|or\s+)?", separator, re.I):
            if re.fullmatch(r"\s*", separator):
                # Same noun phrase continuing past the accepted head
                # (fractured-arc NP): advance the cursor so the next
                # separator is measured from the phrase end.
                previous_end = max(previous_end, end)
            continue
        output.append(candidate)
        previous_end = end
    return list({item.i: item for item in output}.values())


def _argument_span(doc, token) -> tuple[int, int]:
    chunk = next(
        (item for item in doc.noun_chunks if item.start <= token.i < item.end),
        None,
    )
    if chunk is not None:
        tokens = list(chunk)
    else:
        tokens = sorted(
            [token, *(child for child in token.children if child.dep_ in {
                "compound", "amod", "poss", "case", "nummod",
            })],
            key=lambda item: item.i,
        )
    while tokens and tokens[0].dep_ in {"det", "predet"}:
        tokens.pop(0)
    if not tokens:
        return token.idx, token.idx + len(token.text)
    return tokens[0].idx, tokens[-1].idx + len(tokens[-1].text)


def _trim_markdown_delimiters(value: str, start: int, end: int) -> tuple[int, int]:
    """Keep a dependency span on content, not adjacent Markdown delimiters."""
    while start < end and value[start] in "*_~`":
        start += 1
    while end > start and value[end - 1] in "*_~`":
        end -= 1
    return start, end


def _relation_argument_type(surface: str, predicate: str | None, role: str) -> str:
    lowered = surface.casefold()
    if re.search(r"\b(?:labs?|company|organization)\b", lowered):
        return "organization"
    if re.search(r"\b(?:report|guide|ledger|document)\b", lowered):
        return "document"
    if re.search(r"\b(?:dataset|records|batch|batches|index|projections)\b", lowered):
        return "artifact"
    if re.search(r"\b(?:api|service|cli|gateway|platform)\b", lowered):
        return "software"
    if re.search(r"\b(?:migration|event|incident)\b", lowered):
        return "event"
    if re.search(r"\b(?:process|workflow|planner|writer|worker|implementation|prototype|queries)\b", lowered):
        return "method"
    if re.search(r"\b(?:latency|search|retrieval|contract|loss|nodes|traversal|recoverability|rebuildability)\b", lowered):
        return "concept"
    if predicate in {"produces", "consumes"} and role == "object":
        return "artifact"
    if predicate in {"causes", "defines", "supports"} and role == "object":
        return "concept"
    return "concept"


def _complete_relation_arguments(
    document: NormalizedDocumentV1,
    unit: _Unit,
    doc,
    all_entities: Sequence[DocumentEntityV1],
) -> tuple[_Unit, tuple[DocumentEntityV1, ...], tuple[CompletedMentionV1, ...]]:
    """Complete exact dependency arguments only inside an already eligible unit."""
    existing_spans = {
        (mention.normalized_start, mention.normalized_end): mention for mention in unit.mentions
    }
    entities_by_surface: dict[str, list[DocumentEntityV1]] = {}
    entity_by_id = {entity.entity_id: entity for entity in all_entities}
    for entity in all_entities:
        if entity.document_id == unit.document_id:
            entities_by_surface.setdefault(entity.canonical_name.casefold(), []).append(entity)
    generated_entities: dict[str, DocumentEntityV1] = {}
    generated_mentions: dict[str, CompletedMentionV1] = {}

    for cue in doc:
        lemma = cue.lemma_.casefold()
        canonical = _CANONICAL_BY_LEMMA.get(lemma)
        open_relation = lemma in _OPEN_ONLY_LEMMAS
        if (
            not open_relation and lemma not in {"part", "component", "relate"}
            and canonical is None
            and not _open_verb_candidate(cue, lemma, canonical, open_relation)
        ):
            continue
        subjects, objects = _argument_tokens(cue)
        for role, roots in (("subject", subjects), ("object", objects)):
            tokens = []
            for root in roots:
                tokens.extend([root, *list(root.conjuncts)])
            for token in {item.i: item for item in tokens}.values():
                token_position = unit.start + token.idx
                inferred_type = _relation_argument_type(token.text, canonical, role)
                covering = [
                    mention for (span_start, span_end), mention in existing_spans.items()
                    if span_start <= token_position < span_end
                ]
                if covering:
                    mention = max(
                        covering,
                        key=lambda item: item.normalized_end - item.normalized_start,
                    )
                    entity = entity_by_id.get(mention.entity_id) or generated_entities.get(mention.entity_id)
                    if entity is None:
                        continue
                    inferred_type = _relation_argument_type(
                        mention.surface, canonical, role,
                    )
                    if (
                        entity.state != EntityTerminalState.PROMOTED
                        and normalize_entity_type(entity.entity_type)
                        != normalize_entity_type(inferred_type)
                    ):
                        generated_entities[entity.entity_id] = entity.model_copy(update={
                            "entity_type": inferred_type,
                            "reasons": (*entity.reasons, "strict_relation_role_type"),
                        })
                    continue
                local_start, local_end = _argument_span(doc, token)
                local_start, local_end = _trim_markdown_delimiters(
                    unit.text, local_start, local_end,
                )
                start, end = unit.start + local_start, unit.start + local_end
                if (start, end) in existing_spans:
                    continue
                surface = document.normalized_text[start:end]
                if not _valid_endpoint_surface(surface):
                    continue
                if surface.casefold().strip(" .,:;()") in _GENERIC_RELATION_ARGUMENTS:
                    continue
                decision, core_span = endpoint_mint_policy(surface)
                if decision == "blocked":
                    continue
                # The evidence mention keeps the FULL argument span (it must
                # cover the dependency head token); only entity identity is
                # narrowed to the casing core: "the Redlark database" binds to
                # entity "Redlark" via a full-span mention.
                identity_surface = surface
                if core_span is not None:
                    identity_surface = surface[core_span[0]:core_span[1]].strip() or surface
                if (
                    decision == "core"
                    and len(identity_surface.split()) == 1
                    and not re.search(r"\d", identity_surface)
                    and not identity_surface.isupper()
                    and not unit.text[: (start + (core_span[0] if core_span else 0)) - unit.start].strip()
                    and not re.search(
                        r"[a-z0-9,;:][ \t]+" + re.escape(identity_surface) + r"\b",
                        document.normalized_text,
                    )
                ):
                    # Sentence-initial capital with no other name evidence
                    # anywhere in the document: the casing is positional. A
                    # multi-word phrase keeps its full identity ("Voltage sag",
                    # "Rejected manifests"); a bare singleton stays unresolved.
                    remaining_words = [
                        word for word in surface.split()
                        if word.casefold() not in {"a", "an", "the"}
                    ]
                    if len(remaining_words) >= 2:
                        # Positional capital on a phrase: the full phrase is the
                        # identity even when the bare first word is a known
                        # entity name ("Voltage sag" must not bind to "Voltage").
                        identity_surface = " ".join(remaining_words)
                    elif identity_surface.casefold() not in entities_by_surface:
                        continue
                if len(re.findall(r"[A-Za-z0-9]+", surface)) == 1 and not (
                    surface[:1].isupper()
                    or re.search(r"(?:ability|tion|ment|ness|ity)$", surface, re.I)
                ):
                    continue
                inferred_type = _relation_argument_type(surface, canonical, role)
                candidates = (
                    entities_by_surface.get(identity_surface.casefold(), [])
                    or entities_by_surface.get(surface.casefold(), [])
                )
                if candidates:
                    entity = sorted(candidates, key=lambda item: (
                        item.state != EntityTerminalState.PROMOTED,
                        item.state != EntityTerminalState.DOCUMENT_LOCAL,
                        -item.confidence,
                        item.entity_id,
                    ))[0]
                    if entity.state not in {EntityTerminalState.PROMOTED, EntityTerminalState.DOCUMENT_LOCAL}:
                        entity = entity.model_copy(update={
                            "state": EntityTerminalState.DOCUMENT_LOCAL,
                            "reasons": (*entity.reasons, "strict_relation_argument"),
                        })
                    if (
                        entity.state != EntityTerminalState.PROMOTED
                        and normalize_entity_type(entity.entity_type)
                        != normalize_entity_type(inferred_type)
                    ):
                        entity = entity.model_copy(update={
                            "entity_type": inferred_type,
                            "reasons": (*entity.reasons, "strict_relation_role_type"),
                        })
                else:
                    # Minted endpoints are untyped observations: 'other' is the
                    # declared unknown (signature wildcard). Type evidence, when
                    # it exists, arrives via census/reducer entities instead.
                    entity_type = "unknown"
                    entity_id = stable_id(
                        "relation-endpoint-entity", document.document_id,
                        identity_surface.casefold(), entity_type, RELATION_RELEASE,
                    )
                    entity = DocumentEntityV1(
                        entity_id=entity_id, document_id=document.document_id,
                        canonical_name=identity_surface, entity_type=entity_type,
                        mention_ids=(), state=EntityTerminalState.DOCUMENT_LOCAL,
                        confidence=1.0,
                        reasons=("strict_dependency_argument", "relation_local_completion"),
                        reducer_release=RELATION_RELEASE,
                    )
                original = to_original_span(document, start, end)
                if not original.exact:
                    continue
                mention_id = stable_id(
                    "relation-endpoint-mention", document.document_id, entity.entity_id,
                    start, end, surface, RELATION_RELEASE,
                )
                mention = CompletedMentionV1(
                    mention_id=mention_id, entity_id=entity.entity_id,
                    document_id=document.document_id, surface=surface,
                    normalized_start=start, normalized_end=end,
                    original_start=original.start, original_end=original.end,
                    source="variant", context_rule="strict_dependency_argument",
                    completion_release=RELATION_RELEASE,
                )
                entity = entity.model_copy(update={
                    "mention_ids": tuple(sorted({*entity.mention_ids, mention_id})),
                })
                generated_entities[entity.entity_id] = entity
                generated_mentions[mention_id] = mention
                existing_spans[(start, end)] = mention

    combined = tuple(sorted(
        [*unit.mentions, *generated_mentions.values()],
        key=lambda item: (item.normalized_start, item.normalized_end, item.mention_id),
    ))
    return (
        replace(unit, mentions=combined),
        tuple(sorted(generated_entities.values(), key=lambda item: item.entity_id)),
        tuple(sorted(generated_mentions.values(), key=lambda item: item.mention_id)),
    )


def _direct_dependency_proposals(doc, unit: _Unit) -> list[_Proposal]:
    proposals: list[_Proposal] = []
    for token in doc:
        lemma = token.lemma_.casefold()
        canonical = _CANONICAL_BY_LEMMA.get(lemma)
        open_relation = lemma in _OPEN_ONLY_LEMMAS
        open_verb = _open_verb_candidate(token, lemma, canonical, open_relation)
        if (
            canonical is None and not open_relation and not open_verb
            and lemma not in {"part", "component", "relate"}
        ):
            continue
        polarity, modality, attribution = _qualifiers(token, unit.text)
        subjects: list[Any] = []
        objects: list[Any] = []
        voice = "active"
        preposition = ""
        cue_source_text = token.text

        passive_subjects = _children(token, {"nsubjpass", "nsubj:pass"})
        by_agents = _prep_objects(token, "by")
        if not passive_subjects and by_agents and token.dep_ in {"acl", "relcl"} and token.tag_ == "VBN":
            for ancestor in [token.head, *list(token.ancestors)]:
                passive_subjects = _children(
                    ancestor, {"nsubj", "csubj", "nsubjpass", "nsubj:pass"},
                )
                if passive_subjects:
                    break
        # Conjoined passive participles share their subject and agent
        # ("X is owned and operated by Y"): the subject attaches to the first
        # conjunct, the by-agent often to the last.
        if not passive_subjects and token.dep_ == "conj" and token.tag_ == "VBN":
            passive_subjects = _children(token.head, {"nsubjpass", "nsubj:pass"})
        if passive_subjects and not by_agents and token.tag_ == "VBN":
            for conjunct in token.conjuncts:
                by_agents = _prep_objects(conjunct, "by")
                if by_agents:
                    break
        if passive_subjects and by_agents:
            # Passive with an explicit by-agent is structurally unambiguous for
            # any relational verb: the agent is the subject.
            subjects, objects = by_agents, passive_subjects
            voice = "passive"
            preposition = "by"
        elif lemma in {"part", "component"}:
            copula = token.head if token.head.lemma_.casefold() == "be" else token
            subjects = _children(copula, {"nsubj", "nsubjpass", "nsubj:pass"})
            # Gapping: "A is part of S, and so is B" — the so-copula conjunct
            # shares the elided complement, so its subject joins the frame.
            for conjunct in copula.conjuncts:
                if conjunct.lemma_.casefold() == "be" and any(
                    child.dep_ == "advmod" and child.lemma_.casefold() == "so"
                    for child in conjunct.children
                ):
                    subjects = [*subjects, *_children(conjunct, {"nsubj", "nsubjpass", "nsubj:pass"})]
            objects = _prep_objects(token, "of")
            canonical = "part_of"
            voice = "copular"
            preposition = "of"
        elif lemma == "relate":
            copula = token.head if token.head.lemma_.casefold() == "be" else token
            subjects = _children(copula, {"nsubj", "nsubjpass", "nsubj:pass"})
            objects = _prep_objects(token, "to")
            canonical = "related_to"
            voice = "copular"
            preposition = "to"
        else:
            subject_dependencies = {"nsubj"}
            if lemma in {"build", "derive"}:
                subject_dependencies.update({"nsubjpass", "nsubj:pass"})
                voice = "passive"
            subjects = _children(token, subject_dependencies)
            required_prep = _PREPOSITION_BY_LEMMA.get(lemma)
            if open_relation:
                required_prep = _OPEN_PREPOSITION_BY_LEMMA.get(lemma)
            if required_prep is not None:
                objects = _prep_objects(token, required_prep)
                preposition = required_prep
            else:
                objects = _children(token, {"dobj", "obj", "attr", "oprd"})
                if not objects and (open_verb or open_relation):
                    # Open verbs without a direct object may relate through a
                    # prepositional complement ("works from X", "migrated to Y",
                    # "occurred at Z").
                    for child in token.children:
                        if child.dep_ == "prep" and child.lemma_.casefold() != "by":
                            prep_objects = _children(child, {"pobj"})
                            if prep_objects:
                                objects = prep_objects
                                preposition = child.text.casefold()
                                break
            objects = _explicit_list_objects(doc, token, objects)
            if not subjects and token.dep_ in {"xcomp", "ccomp", "conj"}:
                subjects = _children(token.head, {"nsubj"})
            if not subjects and token.dep_ == "acl" and token.tag_ == "VBG":
                # Active participial modifier: "a lane containing X, ..." —
                # the modified noun is the participle's subject; mirror of
                # the VBN passive-acl recovery above.
                subjects = [token.head]
                voice = "participial"
            if not subjects:
                # Subject stranded on an aux child. When that aux carries a
                # content lemma ("sends" tagged AUX under ROOT "normalized"),
                # the small model misparsed: the aux is the real predicate.
                for auxiliary in _children(token, {"aux", "auxpass"}):
                    recovered = _children(
                        auxiliary, {"nsubj", "csubj", "nsubjpass", "nsubj:pass"},
                    )
                    if recovered:
                        subjects = recovered
                        if auxiliary.lemma_.casefold() not in {
                            "be", "have", "do", "will", "would", "shall",
                            "should", "can", "could", "may", "might", "must", "get",
                        }:
                            cue_source_text = auxiliary.text
                            lemma = auxiliary.lemma_.casefold()
                        break
            if token.dep_ in {"xcomp", "ccomp"} and subjects and all(
                item.lemma_.casefold() in {"it", "they", "he", "she"} for item in subjects
            ):
                controlled = _children(token.head, {"nsubj"})
                if controlled:
                    subjects = controlled
            if token.dep_ == "relcl" and any(
                item.lemma_.casefold() in {"that", "which", "who"} for item in subjects
            ):
                subjects = [token.head]
            if token.dep_ == "relcl" and objects and all(
                item.lemma_.casefold() in {"that", "which", "who", "whom"} for item in objects
            ):
                # Object relative: "Vaultstone, which Quartzline owns" — the
                # relative pronoun stands for the modified head noun.
                objects = [token.head]
            if token.dep_ == "relcl" and not objects:
                objects = [
                    item for item in doc
                    if token.i < item.i < token.head.head.i
                    and item.dep_ in {"dobj", "obj", "nsubj"}
                    and item.pos_ in {"NOUN", "PROPN"}
                ][-1:]

        subject_mentions = [item for arg in subjects for item in _mentions_for_token(arg, unit)]
        object_mentions = [item for arg in objects for item in _mentions_for_token(arg, unit)]
        sentence_left = max(
            unit.text.rfind(".", 0, token.idx),
            unit.text.rfind(";", 0, token.idx),
            unit.text.rfind("\n", 0, token.idx),
        ) + 1
        before_cue = unit.text[sentence_left:token.idx]
        if (
            "," in before_cue
            and re.search(r"\band\b", before_cue, re.I)
            and not _RELATION_CUE_RE.search(before_cue)
        ):
            subject_mentions.extend(
                mention for mention in unit.mentions
                if unit.start + sentence_left <= mention.normalized_start
                and mention.normalized_end <= unit.start + token.idx
            )
        subject_mentions = list({item.mention_id: item for item in subject_mentions}.values())
        object_mentions = list({item.mention_id: item for item in object_mentions}.values())
        if voice == "participial":
            # The participle's subject is the modified head noun (plus any
            # genuinely coordinated heads, all LEFT of the participle).
            # Conj arcs the parser attached rightward belong to the object
            # field — without this guard a fractured list parse turns list
            # members into subjects and mints false facts.
            subject_mentions = [
                item for item in subject_mentions
                if item.normalized_end <= unit.start + token.idx
            ]
        recipient_mentions: list[Any] = []
        recipient_prep = ""
        if open_verb and voice == "active":
            # Transfer frame "verb NP to NP": the to/dative complement is a
            # recipient argument, proposed with its own "<verb> to" surface so
            # the transfer-verb policy (destination -> uses) can apply.
            for child in token.children:
                if child.dep_ == "dative" or (
                    child.dep_ == "prep" and child.lemma_.casefold() == "to"
                ):
                    for pobj in _children(child, {"pobj"}):
                        recipient_mentions.extend(_mentions_for_token(pobj, unit))
                    if recipient_mentions:
                        recipient_prep = child.text.casefold()
                        break
            recipient_mentions = list(
                {item.mention_id: item for item in recipient_mentions}.values()
            )
        if (open_verb or open_relation) and subject_mentions and not object_mentions:
            # The direct object carried no name evidence ("migrated its
            # archives to the Vaultstone store"): relate through the
            # prepositional or dative complement that does.
            for child in token.children:
                if child.dep_ in {"prep", "dative"} and child.lemma_.casefold() != "by":
                    prep_objects = _children(child, {"pobj"})
                    resolved = [
                        item for arg in prep_objects for item in _mentions_for_token(arg, unit)
                    ]
                    if resolved:
                        object_mentions = list({item.mention_id: item for item in resolved}.values())
                        preposition = child.text.casefold()
                        break
        if not subject_mentions or not object_mentions:
            continue
        cue = (
            f"{cue_source_text} {preposition}"
            if ((open_verb or open_relation) and preposition)
            else cue_source_text
        )
        for subject in subject_mentions:
            for object_mention in object_mentions:
                if subject.entity_id == object_mention.entity_id:
                    continue
                local_qualification = nominal_assertion_qualification(
                    unit.text, subject.surface, cue, object_mention.surface,
                )
                local_polarity, local_modality, local_attribution = (
                    local_qualification or (polarity, modality, attribution)
                )
                proposals.append(_Proposal(
                    subject=subject, object=object_mention,
                    surface_predicate=cue, lemma=lemma, particle="",
                    preposition=preposition,
                    dependency_frame=f"direct:{voice}:{token.dep_}",
                    dependency_path=f"{subjects[0].dep_ if subjects else ''}>{token.dep_}>{objects[0].dep_ if objects else ''}",
                    voice=voice, polarity=local_polarity, modality=local_modality,
                    attribution=local_attribution, canonical_hint=canonical,
                    confidence=1.0, source="strict_dependency_cue",
                ))
        for subject in subject_mentions:
            for recipient in recipient_mentions:
                if subject.entity_id == recipient.entity_id:
                    continue
                if any(
                    recipient.entity_id == existing.entity_id
                    for existing in object_mentions
                ):
                    continue
                recipient_cue = f"{cue_source_text} {recipient_prep}"
                local_qualification = nominal_assertion_qualification(
                    unit.text, subject.surface, recipient_cue, recipient.surface,
                )
                recipient_polarity, recipient_modality, recipient_attribution = (
                    local_qualification or (polarity, modality, attribution)
                )
                proposals.append(_Proposal(
                    subject=subject, object=recipient,
                    surface_predicate=recipient_cue, lemma=lemma, particle="",
                    preposition=recipient_prep,
                    dependency_frame=f"direct:{voice}:transfer_recipient",
                    dependency_path=f"nsubj>{token.dep_}>pobj",
                    voice=voice, polarity=recipient_polarity,
                    modality=recipient_modality,
                    attribution=recipient_attribution,
                    canonical_hint=None, confidence=1.0,
                    source="strict_dependency_cue",
                ))
    # Appositive membership: "X, the <descriptor> in Y, ..." — the appositive
    # descriptor's in-complement associates the anchor with Y. Captured as an
    # open surface relation; the compiler and gate decide anything further.
    for token in doc:
        if token.dep_ != "appos":
            continue
        anchor_mentions = _mentions_for_token(token.head, unit)
        if not anchor_mentions:
            continue
        for prep in token.children:
            if prep.dep_ != "prep" or prep.lemma_.casefold() != "in":
                continue
            for pobj in _children(prep, {"pobj"}):
                for object_mention in _mentions_for_token(pobj, unit):
                    for subject in anchor_mentions:
                        if subject.entity_id == object_mention.entity_id:
                            continue
                        proposals.append(_Proposal(
                            subject=subject, object=object_mention,
                            surface_predicate=f"{token.lemma_.casefold()} in",
                            lemma="in", particle="", preposition="in",
                            dependency_frame="direct:appositive:in_membership",
                            dependency_path="appos>prep>pobj",
                            voice="appositive", polarity="positive",
                            modality="asserted", attribution="",
                            canonical_hint=None, confidence=0.9,
                            source="strict_dependency_cue",
                        ))
    return proposals


RELEX_RELATION_LABELS = (
    "is a", "part of", "owns", "member of", "created by", "located in",
    "uses", "depends on", "implements", "supports", "produces", "consumes",
    "derived from", "defines", "measures", "causes", "enables", "precedes",
)
def _relex_relation_labels() -> list[str]:
    """Canonical predicate labels + the active compiled adapter's native
    predicate surfaces (Universal Adapter Compiler). Native surfaces map
    through the compiler's synonym tables when exact, else stay OPEN."""
    labels = list(RELEX_RELATION_LABELS)
    try:
        from services.ontology_adapter.providers.relex import extra_relation_labels
        labels.extend(l for l in extra_relation_labels() if l not in labels)
    except Exception:  # noqa: BLE001 — adapter layer absent = canonical only
        pass
    return labels


_RELEX_NEGATION_RE = re.compile(
    r"\b(?:not|never|no longer|cannot|can't|denie[ds]|refuse[ds]?|without)\b", re.I,
)


def _relex_enabled() -> bool:
    return os.environ.get("GRAPHIFY_RELEX_RELATIONS", "").strip() == "1"


def _relex_accept_threshold() -> float:
    raw = os.environ.get("RELEX_ACCEPT_THRESHOLD", "").strip()
    try:
        return float(raw) if raw else 0.5
    except ValueError:
        return 0.5


def _relex_semantic_proposals(
    units: Sequence[_Unit],
    precomputed: Sequence[dict] | None = None,
) -> list[tuple[_Unit, _Proposal]]:
    """Semantic relation candidates from the host-MPS Relex sidecar.

    The sidecar proposes (head span, predicate, tail span); this joiner
    binds spans to completed mentions by strict overlap and abstains on
    any unbound endpoint — it never mints entities. Qualifier safety is
    text-scoped and conservative: a sentence carrying negation or modal
    markers demotes the proposal to negative/uncertain (QUALIFIED lane),
    because Relex itself carries no polarity signal. The gates remain the
    knowledge authority; source 'relex_semantic' is accepted only above
    the pinned score threshold and only through the unchanged gate chain.

    Single-pass mode: when `precomputed` is supplied (relation candidates
    captured during the entity census, doc-global offsets), it is the
    ONLY source — no second neural pass. Candidates whose endpoints span
    unit boundaries are dropped conservatively (cross-sentence binding is
    coref-era work, not span arithmetic).
    """
    from services.extraction.relex_sidecar_client import (
        RelexRelation, RelexSidecarError, infer,
    )

    eligible = [unit for unit in units if unit.mentions and len(unit.text.split()) >= 3]
    if not eligible:
        return []
    if precomputed is not None:
        pairs = []
        for unit in eligible:
            local = [
                RelexRelation(
                    head_start=int(row["head_start"]) - unit.start,
                    head_end=int(row["head_end"]) - unit.start,
                    head_text=str(row["head_text"]),
                    tail_start=int(row["tail_start"]) - unit.start,
                    tail_end=int(row["tail_end"]) - unit.start,
                    tail_text=str(row["tail_text"]),
                    label=str(row["label"]), score=float(row["score"]),
                )
                for row in precomputed
                if row.get("document_id") == unit.document_id
                and unit.start <= int(row["head_start"]) and int(row["head_end"]) <= unit.end
                and unit.start <= int(row["tail_start"]) and int(row["tail_end"]) <= unit.end
            ]
            pairs.append((unit, local))
    else:
        try:
            results = infer(
                [unit.text for unit in eligible],
                entity_labels=[], relation_labels=_relex_relation_labels(),
            )
        except RelexSidecarError:
            # Fail-closed (O1 worker contract): no silent degradation. The
            # stage fails recoverable and retries once the SAME pinned
            # release is healthy — never a partial semantic completion.
            raise
        pairs = [(unit, list(result.relations)) for unit, result in zip(eligible, results)]
    output: list[tuple[_Unit, _Proposal]] = []
    for unit, unit_relations in pairs:
        def mention_for(start: int, end: int):
            best, best_overlap = None, 0
            for mention in unit.mentions:
                ls = mention.normalized_start - unit.start
                le = mention.normalized_end - unit.start
                overlap = min(le, end) - max(ls, start)
                if overlap > 0 and overlap > best_overlap:
                    best, best_overlap = mention, overlap
            return best

        for relation in unit_relations:
            subject = mention_for(relation.head_start, relation.head_end)
            object_mention = mention_for(relation.tail_start, relation.tail_end)
            if subject is None or object_mention is None:
                continue
            if subject.entity_id == object_mention.entity_id:
                continue
            left = min(relation.head_start, relation.tail_start)
            right = max(relation.head_end, relation.tail_end)
            sentence_left = max(
                unit.text.rfind(".", 0, left), unit.text.rfind("\n", 0, left),
            ) + 1
            sentence_right = unit.text.find(".", right)
            sentence = unit.text[sentence_left:sentence_right + 1 if sentence_right >= 0 else len(unit.text)]
            polarity = "negative" if _RELEX_NEGATION_RE.search(sentence) else "positive"
            modality = "uncertain" if _QUALIFIER_RE.search(sentence) else "asserted"
            label = relation.label.strip().casefold()
            qualification = nominal_assertion_qualification(
                unit.text, subject.surface, label, object_mention.surface,
            )
            if qualification:
                polarity, modality, attribution = qualification
            else:
                attribution = ""
            output.append((unit, _Proposal(
                subject=subject, object=object_mention,
                surface_predicate=label, lemma=label, particle="",
                preposition="",
                dependency_frame="relex:semantic",
                dependency_path="relex",
                voice="semantic", polarity=polarity, modality=modality,
                attribution=attribution, canonical_hint=None,
                confidence=float(relation.score), source="relex_semantic",
            )))
    # One candidate per (unit, pair, predicate): overlapping mention joins
    # produce duplicates; keep the highest-scored observation.
    deduped: dict[tuple, tuple[_Unit, _Proposal]] = {}
    for unit, proposal in output:
        key = (unit.unit_id, proposal.subject.mention_id,
               proposal.object.mention_id, proposal.surface_predicate)
        kept = deduped.get(key)
        if kept is None or proposal.confidence > kept[1].confidence:
            deduped[key] = (unit, proposal)
    return list(deduped.values())


def _syntax_proposals(
    unit: _Unit,
    doc,
    extractor: FrameExtractor,
    entities_by_id: dict[str, DocumentEntityV1],
) -> tuple[list[_Proposal], list[dict[str, Any]], Counter[str]]:
    local = _local_mentions(unit)
    entity_rows = [
        {
            "text": mention.surface,
            "type": entities_by_id[mention.entity_id].entity_type,
            "start": mention.normalized_start - unit.start,
            "end": mention.normalized_end - unit.start,
        }
        for mention in unit.mentions
    ]
    resolved, unmapped = generate_syntax_records(
        unit.text, entity_rows, unit.unit_id, extractor, doc,
    )
    gate_counts: Counter[str] = Counter()
    union = build_union_evidence(
        unit.unit_id,
        {"entities": entity_rows, "relations": [], "raw_pair_scores": []},
        resolved, unmapped, unit.text,
    )
    policy = load_policy()
    for evidence in union:
        gate_counts[evaluate_relation(evidence, policy).status.value] += 1
    proposals: list[_Proposal] = []
    for row in resolved + unmapped:
        subject = local.get((int(row.get("subject_start", -1)), int(row.get("subject_end", -1))))
        object_mention = local.get((int(row.get("object_start", -1)), int(row.get("object_end", -1))))
        if subject is None or object_mention is None or subject.entity_id == object_mention.entity_id:
            continue
        surface_predicate = str(row.get("surface_predicate") or "")
        polarity = str(row.get("polarity") or ("negative" if row.get("negated") else "positive"))
        modality = str(row.get("modality") or "asserted")
        attribution = str(row.get("attribution") or "direct")
        local_qualification = nominal_assertion_qualification(
            unit.text, subject.surface, surface_predicate, object_mention.surface,
        )
        if local_qualification is not None:
            polarity, modality, attribution = local_qualification
        proposals.append(_Proposal(
            subject=subject, object=object_mention,
            surface_predicate=surface_predicate,
            lemma=str(row.get("lemma") or row.get("surface_predicate") or ""),
            particle=str(row.get("particle") or ""),
            preposition=str(row.get("preposition") or ""),
            dependency_frame=f"syntax:{row.get('pattern_id') or 'unknown'}",
            dependency_path=str(row.get("dependency_path") or ""),
            voice=str(row.get("voice") or ""),
            polarity=polarity, modality=modality, attribution=attribution,
            canonical_hint=(str(row["canonical_predicate"]) if row.get("canonical_predicate") else None),
            confidence=float(row.get("confidence") or 0.0), source="existing_syntax_stack",
        ))
    return proposals, resolved + unmapped, gate_counts


_KEY_VALUE_LINE_RE = re.compile(
    r"^(?P<prefix>\s*(?:[-*]\s+)?(?:\*\*)?)"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_ ]{0,40}?)(?:\*\*)?\s*:\s+(?P<value>\S.*?)\s*$"
)
_CONTAINMENT_KEYS = frozenset({"contains", "includes", "comprises", "components", "parts"})


def _structured_data_proposals(
    document: NormalizedDocumentV1,
    survey: DocumentSurveyV1,
    document_mentions: Sequence[CompletedMentionV1],
    doc_units: Sequence[_Unit],
    entity_by_id: dict[str, DocumentEntityV1],
    endpoint_entities: dict[str, DocumentEntityV1],
    endpoint_mentions: dict[str, CompletedMentionV1],
) -> list[tuple[_Unit, _Proposal]]:
    """Deterministic structured-data proposer: explicit key-value metadata lines
    ("requires: Gullwing Runtime") relate the document's leading promoted
    entity to the full value span through the key as surface predicate. Builds
    its own line-anchored units (key-value lines are rarely grammar-eligible)
    and resolves or mints the value endpoint from the entire value span. A
    parallel proposer — the compiler interprets, the gate decides."""
    def _subject_candidate(mention: CompletedMentionV1) -> bool:
        entity = entity_by_id.get(mention.entity_id)
        return (
            entity is not None
            and entity.state == EntityTerminalState.PROMOTED
            # An identifier is an attribute of the document, never the
            # implicit subject of its metadata.
            and getattr(entity, "facet", "") != "document_identifier"
        )

    subject: CompletedMentionV1 | None = None
    title_span = (
        (survey.headings[0].start, survey.headings[0].end) if survey.headings else None
    )
    if title_span is not None:
        for mention in document_mentions:
            if (
                mention.normalized_start is not None
                and title_span[0] <= mention.normalized_start
                and (mention.normalized_end or 0) <= title_span[1] + 1
                and _subject_candidate(mention)
            ):
                subject = mention
                break
    if subject is None:
        for mention in document_mentions:
            if _subject_candidate(mention):
                if subject is None or (mention.normalized_start or 0) < (subject.normalized_start or 0):
                    subject = mention
    if subject is None:
        return []
    entities_by_name: dict[str, DocumentEntityV1] = {}
    for entity in entity_by_id.values():
        if entity.document_id != document.document_id:
            continue
        key = entity.canonical_name.casefold()
        current = entities_by_name.get(key)
        if current is None or (
            current.state != EntityTerminalState.PROMOTED
            and entity.state == EntityTerminalState.PROMOTED
        ):
            entities_by_name[key] = entity
    rows: list[tuple[_Unit, _Proposal]] = []
    offset = 0
    for line in document.normalized_text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        match = _KEY_VALUE_LINE_RE.match(stripped)
        if match:
            line_start = offset
            value_start = line_start + match.start("value")
            value_end = line_start + match.end("value")
            value_surface = document.normalized_text[value_start:value_end]
            decision, core_span = endpoint_mint_policy(value_surface)
            if decision != "blocked":
                identity_surface = value_surface
                if core_span is not None:
                    identity_surface = value_surface[core_span[0]:core_span[1]].strip() or value_surface
                entity = entities_by_name.get(identity_surface.casefold())
                if entity is None:
                    entity_id = stable_id(
                        "relation-endpoint-entity", document.document_id,
                        identity_surface.casefold(), "unknown", RELATION_RELEASE,
                    )
                    entity = endpoint_entities.get(entity_id) or DocumentEntityV1(
                        entity_id=entity_id, document_id=document.document_id,
                        canonical_name=identity_surface, entity_type="unknown",
                        mention_ids=(), state=EntityTerminalState.DOCUMENT_LOCAL,
                        confidence=1.0,
                        reasons=("structured_data_value", "relation_local_completion"),
                        reducer_release=RELATION_RELEASE,
                    )
                original = to_original_span(document, value_start, value_end)
                if original.exact and entity.entity_id != subject.entity_id:
                    mention_id = stable_id(
                        "relation-endpoint-mention", document.document_id,
                        entity.entity_id, value_start, value_end, value_surface,
                        RELATION_RELEASE,
                    )
                    obj = CompletedMentionV1(
                        mention_id=mention_id, entity_id=entity.entity_id,
                        document_id=document.document_id, surface=value_surface,
                        normalized_start=value_start, normalized_end=value_end,
                        original_start=original.start, original_end=original.end,
                        source="variant", context_rule="structured_data_value",
                        completion_release=RELATION_RELEASE,
                    )
                    endpoint_entities[entity.entity_id] = entity
                    entity_by_id.setdefault(entity.entity_id, entity)
                    endpoint_mentions[mention_id] = obj
                    surface = re.sub(r"[_\s]+", " ", match.group("key")).strip().casefold()
                    invert = surface in _CONTAINMENT_KEYS
                    left, right = (obj, subject) if invert else (subject, obj)
                    cue = "part of" if invert else surface
                    unit = _Unit(
                        stable_id(
                            "relation-unit", document.document_id, "structured",
                            line_start, line_start + len(stripped), stripped,
                        ),
                        document.document_id, line_start,
                        line_start + len(stripped), stripped, (subject, obj),
                    )
                    rows.append((unit, _Proposal(
                        subject=left, object=right,
                        surface_predicate=cue, lemma=cue, particle="",
                        preposition="",
                        dependency_frame="structured:key_value",
                        dependency_path="key>value",
                        voice="structured", polarity="positive",
                        modality="asserted", attribution="",
                        canonical_hint="part_of" if invert else None,
                        confidence=1.0, source="structured_data",
                    )))
        offset += len(line)
    return rows


def _surface_record(unit: _Unit, proposal: _Proposal) -> SurfaceRelationV1:
    relation_id = stable_id(
        "surface-relation", unit.document_id, proposal.subject.mention_id,
        proposal.object.mention_id, proposal.surface_predicate, proposal.dependency_frame,
        unit.start, unit.end, RELATION_RELEASE,
    )
    return SurfaceRelationV1(
        relation_id=relation_id, document_id=unit.document_id,
        evidence_text=unit.text, evidence_start=unit.start, evidence_end=unit.end,
        subject_mention_id=proposal.subject.mention_id,
        object_mention_id=proposal.object.mention_id,
        surface_predicate=proposal.surface_predicate, lemma=proposal.lemma,
        particle=proposal.particle, preposition=proposal.preposition,
        dependency_frame=proposal.dependency_frame,
        dependency_path=proposal.dependency_path, voice=proposal.voice,
        polarity=proposal.polarity, modality=proposal.modality,
        attribution=proposal.attribution, canonical_candidate=None,
        temporal=extract_temporal_qualifier(unit.text),
        mapping_rule=f"uncompiled:{proposal.source}",
        mapping_release=PREDICATE_COMPILER_RELEASE,
        terminal_state=RelationTerminalState.REVIEW,
        reasons=("surface_record_persisted_before_mapping",),
    )


def _percentile(values: Sequence[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return ordered[index]


def _crosses_competing_relation_cue(unit: _Unit, proposal: _Proposal, candidate: str | None) -> bool:
    if candidate is None or proposal.subject.normalized_end >= proposal.object.normalized_start:
        return False
    between = unit.text[
        proposal.subject.normalized_end - unit.start:proposal.object.normalized_start - unit.start
    ]
    matches = list(_RELATION_CUE_RE.finditer(between))
    if len(matches) < 2:
        return False
    token = matches[-1].group(0).casefold()
    roots = {
        "use": "uses", "depend": "depends_on", "support": "supports",
        "produce": "produces", "consume": "consumes", "own": "owns",
        "cause": "causes", "deriv": "derived_from", "define": "defines",
        "implement": "implements", "store": "stores", "detect": "detects",
        "acquir": "owns",
    }
    competing = next((value for root, value in roots.items() if token.startswith(root)), None)
    return competing is not None and competing != candidate


def run_relation_fast_path(
    documents: Sequence[NormalizedDocumentV1],
    surveys: Sequence[DocumentSurveyV1],
    mentions: Sequence[CompletedMentionV1],
    entities: Sequence[DocumentEntityV1],
    relex_candidates: Sequence[dict] | None = None,
) -> RelationFastPathOutput:
    entity_by_id = {entity.entity_id: entity for entity in entities}
    document_by_id = {document.document_id: document for document in documents}
    mentions_by_document: dict[str, list[CompletedMentionV1]] = {}
    for mention in mentions:
        mentions_by_document.setdefault(mention.document_id, []).append(mention)
    all_units: list[_Unit] = []
    all_decisions: list[RelationEligibilityDecision] = []
    unit_counts_by_document: list[tuple[NormalizedDocumentV1, int, int]] = []
    for document, survey in zip(documents, surveys):
        units, decisions = _unit_rows(
            document, survey, mentions_by_document.get(document.document_id, []),
        )
        unit_counts_by_document.append((document, len(all_units), len(all_units) + len(units)))
        all_units.extend(units)
        all_decisions.extend(decisions)

    extractor = FrameExtractor()
    # The shared spaCy Language is not safe under concurrent pipe() calls;
    # the factory coordinator overlaps documents, so the parse itself is the
    # one serialized section (pure-Python compile below runs concurrently).
    with _SPACY_PARSE_LOCK:
        docs = list(extractor._nlp.pipe(  # noqa: SLF001
            [_mask_markdown_for_parse(unit.text) for unit in all_units], batch_size=64,
        ))
    endpoint_entities: dict[str, DocumentEntityV1] = {}
    endpoint_mentions: dict[str, CompletedMentionV1] = {}
    completed_units: list[_Unit] = []
    discourse_resolved_mentions = 0
    for unit, doc in zip(all_units, docs):
        unit, discourse_entity, discourse_mention = _resolve_discourse_subject(
            document_by_id[unit.document_id], unit, doc,
            mentions_by_document.get(unit.document_id, []), entity_by_id,
        )
        if discourse_entity is not None and discourse_mention is not None:
            endpoint_entities[discourse_entity.entity_id] = discourse_entity
            endpoint_mentions[discourse_mention.mention_id] = discourse_mention
            entity_by_id[discourse_entity.entity_id] = discourse_entity
            discourse_resolved_mentions += 1
        completed_unit, generated_entities, generated_mentions = _complete_relation_arguments(
            document_by_id[unit.document_id], unit, doc, tuple(entity_by_id.values()),
        )
        completed_units.append(completed_unit)
        for entity in generated_entities:
            endpoint_entities[entity.entity_id] = entity
            entity_by_id[entity.entity_id] = entity
        for mention in generated_mentions:
            endpoint_mentions[mention.mention_id] = mention
    all_units = completed_units
    proposal_rows: list[tuple[_Unit, _Proposal]] = []
    gate_counts: Counter[str] = Counter()
    syntax_records = 0
    direct_records = 0
    pair_counts: list[int] = []
    mention_counts: list[int] = []
    conjunction_counts: list[int] = []
    dense_structure_units: list[str] = []
    closed_class_mentions: set[str] = set()
    for unit, doc in zip(all_units, docs):
        # Strictly aligned entity mentions attached to the shared Doc so every
        # Doc consumer sees the same accepted spans (contract: one Doc).
        entity_spans = []
        for mention in unit.mentions:
            span = doc.char_span(
                mention.normalized_start - unit.start,
                mention.normalized_end - unit.start,
                alignment_mode="strict",
            )
            if span is not None:
                entity_spans.append(span)
                # Structural endpoint eligibility (#2, owner-ratified): a span
                # whose syntactic head — or every token — is closed-class can
                # never anchor a relation endpoint. POS decides, not words:
                # "Deployment CAN cause…" (AUX) is vetoed while "the CAN
                # stores paint" (NOUN) stays eligible.
                if span.root.pos_ in _CLOSED_CLASS_POS or all(
                    token.pos_ in _CLOSED_CLASS_POS for token in span
                ):
                    closed_class_mentions.add(mention.mention_id)
        doc.spans["polymath_entities"] = entity_spans
        syntax, raw_syntax, local_gate_counts = _syntax_proposals(unit, doc, extractor, entity_by_id)
        direct = _direct_dependency_proposals(doc, unit)
        gate_counts.update(local_gate_counts)
        syntax_records += len(raw_syntax)
        direct_records += len(direct)
        deduped: dict[tuple[str, str, str], _Proposal] = {}
        for proposal in [*direct, *syntax]:
            key = (
                proposal.subject.mention_id, proposal.object.mention_id,
                proposal.canonical_hint or proposal.lemma,
            )
            existing = deduped.get(key)
            if existing is None or (proposal.source == "strict_dependency_cue" and existing.source != proposal.source):
                deduped[key] = proposal
        unit_pairs = len({(item.subject.mention_id, item.object.mention_id) for item in deduped.values()})
        unit_conjunctions = sum(1 for item in doc if item.dep_ == "conj")
        pair_counts.append(unit_pairs)
        mention_counts.append(len(unit.mentions))
        conjunction_counts.append(unit_conjunctions)
        if len(unit.mentions) >= 8 or unit_conjunctions >= 6 or unit_pairs >= 12:
            # Pathologically dense unit: labeled for routing, never discarded.
            dense_structure_units.append(unit.unit_id)
        proposal_rows.extend((unit, item) for item in deduped.values())

    structured_proposals = 0
    survey_by_document = {survey.document_id: survey for survey in surveys}
    for document, start_index, end_index in unit_counts_by_document:
        doc_units = completed_units[start_index:end_index]
        for unit, proposal in _structured_data_proposals(
            document, survey_by_document[document.document_id],
            mentions_by_document.get(document.document_id, []),
            doc_units, entity_by_id, endpoint_entities, endpoint_mentions,
        ):
            proposal_rows.append((unit, proposal))
            structured_proposals += 1

    relex_proposals = 0
    if _relex_enabled():
        # Semantic candidate lane: joins the SAME union and faces the SAME
        # compiler and gates as every structural lane. Appended LAST so the
        # mapping loop has already judged every structural row when a relex
        # row is scored — corroboration reads those verdicts. When the
        # census ran the single-pass provider, its captured candidates are
        # consumed here and NO second neural pass happens.
        for unit, proposal in _relex_semantic_proposals(
            all_units, precomputed=relex_candidates,
        ):
            proposal_rows.append((unit, proposal))
            relex_proposals += 1

    surface_records = [_surface_record(unit, proposal) for unit, proposal in proposal_rows]
    compiler = predicate_compiler()
    mapped: list[SurfaceRelationV1] = []
    assertions: list[AssertionDecisionV1] = []
    invalid_endpoint_rejections = 0
    # Pairs a TRUSTED structural lane deemed assertable (accepted/open) in a
    # unit. A relex row may accept only on such corroboration: if structure
    # held the pair back as qualified/review (negation, modality, claim
    # scope, attribution), the structural verdict governs and the semantic
    # lane can never override it. Burned sealed-v1 counterexample: "It was
    # incorrectly reported ... that the March Blackout Event caused data
    # loss" — Relex proposes causes(event, loss); no trusted lane asserts
    # the pair; the row stays REVIEW.
    assertable_pairs: set[tuple[str, frozenset]] = set()
    for surface, (_unit, proposal) in zip(surface_records, proposal_rows):
        subject_type = entity_by_id[proposal.subject.entity_id].entity_type
        object_type = entity_by_id[proposal.object.entity_id].entity_type
        invalid_endpoint = not all((
            _valid_endpoint_surface(proposal.subject.surface),
            _valid_endpoint_surface(proposal.object.surface),
            _valid_endpoint_surface(entity_by_id[proposal.subject.entity_id].canonical_name),
            _valid_endpoint_surface(entity_by_id[proposal.object.entity_id].canonical_name),
        ))
        if invalid_endpoint:
            candidate = None
            rule = "reject:invalid_endpoint_sentinel"
            state = RelationTerminalState.REJECTED
            invalid_endpoint_rejections += 1
        else:
            candidate, rule = compiler.compile(
                surface=proposal.surface_predicate, lemma=proposal.lemma,
                canonical_hint=proposal.canonical_hint,
                subject_type=subject_type, object_type=object_type,
                subject_name=entity_by_id[proposal.subject.entity_id].canonical_name,
                object_name=entity_by_id[proposal.object.entity_id].canonical_name,
                source=proposal.source,
            )
        if invalid_endpoint:
            pass
        elif proposal.subject.entity_id == proposal.object.entity_id:
            # Self-referential endpoints assert nothing (#2 invariant —
            # mirrors the OpenIE assembler rule).
            candidate = None
            rule = "reject:self_referential_endpoints"
            state = RelationTerminalState.REJECTED
        elif rule.startswith("open:"):
            state = RelationTerminalState.OPEN
        elif candidate is not None and (
            surface.polarity != "positive"
            or surface.modality != "asserted"
            or surface.attribution not in {"", "direct"}
        ):
            state = RelationTerminalState.QUALIFIED
        elif rule.startswith("review:"):
            state = RelationTerminalState.REVIEW
        elif _crosses_competing_relation_cue(_unit, proposal, candidate):
            state = RelationTerminalState.REVIEW
            rule = "review:competing_coordinated_relation_cue"
        elif (
            proposal.subject.mention_id in closed_class_mentions
            or proposal.object.mention_id in closed_class_mentions
        ):
            # Structural endpoint eligibility veto (#2): the observation
            # survives with full provenance; it just never promotes.
            state = RelationTerminalState.REVIEW
            rule = "review:endpoint_head_closed_class"
        elif not all(
            entity_by_id[item.entity_id].state
            in {EntityTerminalState.PROMOTED, EntityTerminalState.DOCUMENT_LOCAL}
            for item in (proposal.subject, proposal.object)
        ):
            # Acceptance requires promotable endpoint identities — same rule
            # the OpenIE assertion lane enforces. A review-state entity can
            # carry a qualified or review record, never a graph fact.
            state = RelationTerminalState.REVIEW
            rule = "review:endpoint_entity_not_promotable"
        elif proposal.source in {"strict_dependency_cue", "structured_data"}:
            state = RelationTerminalState.ACCEPTED
        elif (
            proposal.source == "relex_semantic"
            and candidate is not None
            and proposal.confidence >= _relex_accept_threshold()
            and (
                _unit.unit_id,
                frozenset((proposal.subject.entity_id, proposal.object.entity_id)),
                candidate,
            ) in assertable_pairs
        ):
            # Semantic acceptance class: SAME-CANONICAL corroboration only —
            # a trusted structural lane already ACCEPTED this exact triple
            # in this unit; the relex row strengthens evidence and never
            # introduces a new canonical edge. Round-1 measurement:
            # threshold-only acceptance leaked embedded claims (sealed-v1,
            # claim-scope family). Round-2 measurement: pair-level
            # corroboration let relex name structurally-OPEN pairs and put
            # battery-negative endpoints into the graph. Widening this
            # (open-pair naming) is blocked on the owner-pending junk-class
            # promotion guards; until then novel relex knowledge is
            # measured in the REVIEW/OPEN lanes, never promoted.
            state = RelationTerminalState.ACCEPTED
            rule = f"{rule}:relex_corroborated"
        else:
            state = RelationTerminalState.REVIEW
        if (
            proposal.source != "relex_semantic"
            and state == RelationTerminalState.ACCEPTED
            and candidate is not None
        ):
            assertable_pairs.add((
                _unit.unit_id,
                frozenset((proposal.subject.entity_id, proposal.object.entity_id)),
                candidate,
            ))
        reasons = (rule, f"source:{proposal.source}")
        update: dict[str, Any] = {
            "canonical_candidate": candidate,
            "mapping_rule": rule,
            "terminal_state": state,
            "reasons": reasons,
        }
        if candidate is not None and ":inverse_direction" in rule:
            # Inverse-direction synonym ("X contains Y" = Y part_of X): the
            # compiled relation swaps endpoints; the uncompiled surface
            # record above keeps the observed direction as provenance.
            update["subject_mention_id"] = surface.object_mention_id
            update["object_mention_id"] = surface.subject_mention_id
        mapped_relation = surface.model_copy(update=update)
        mapped.append(mapped_relation)
        assertions.append(AssertionDecisionV1(
            decision_id=stable_id("assertion-decision", surface.relation_id, state.value, candidate),
            relation_id=surface.relation_id, document_id=surface.document_id,
            status=state, canonical_predicate=candidate,
            score=proposal.confidence, reasons=reasons,
            policy_release=RELATION_RELEASE,
        ))
    if len(surface_records) != len(mapped) or len(mapped) != len(assertions):
        raise RuntimeError("relation decision conservation failed")
    eligible_count = len(all_units)
    report: dict[str, object] = {
        "schema_version": "polymath.relation_fast_path_report.v1",
        "status": "passed",
        "relation_release": RELATION_RELEASE,
        "predicate_compiler_release": PREDICATE_COMPILER_RELEASE,
        "predicate_compiler_hash": compiler.config_hash,
        "eligibility_decisions": len(all_decisions),
        "eligible_units": eligible_count,
        "relation_eligible_rate": eligible_count / len(all_decisions) if all_decisions else 0.0,
        "unit_kind_counts": dict(Counter(item.unit_kind for item in all_decisions)),
        "closed_class_mention_ids": sorted(closed_class_mentions),
        "closed_class_mentions": len(closed_class_mentions),
        "spacy_parses": len(docs),
        "parse_once": len(docs) == eligible_count,
        "statistical_ner_enabled": False,
        "syntax_records": syntax_records,
        "strict_dependency_records": direct_records,
        "structured_data_proposals": structured_proposals,
        "relex_semantic_proposals": relex_proposals,
        "relation_local_endpoint_entities": len(endpoint_entities),
        "relation_local_endpoint_mentions": len(endpoint_mentions),
        "discourse_resolved_mentions": discourse_resolved_mentions,
        "surface_records": len(surface_records),
        "mapped_records": len(mapped),
        "assertion_decisions": len(assertions),
        "decision_conservation": len(surface_records) == len(assertions),
        "invalid_endpoint_rejections": invalid_endpoint_rejections,
        "wildcard_non_rejected_endpoints": sum(
            not _valid_endpoint_surface(proposal.subject.surface)
            or not _valid_endpoint_surface(proposal.object.surface)
            for proposal, decision in zip(
                (row[1] for row in proposal_rows), assertions,
            )
            if decision.status != RelationTerminalState.REJECTED
        ),
        "terminal_state_counts": dict(sorted(Counter(item.status.value for item in assertions).items())),
        "gate_diagnostic_counts": dict(sorted(gate_counts.items())),
        "pair_tail": {
            "p50": _percentile(pair_counts, 0.50),
            "p95": _percentile(pair_counts, 0.95),
            "p99": _percentile(pair_counts, 0.99),
            "max": max(pair_counts, default=0),
        },
        "structure_telemetry": {
            "mentions_per_unit": {
                "p50": _percentile(mention_counts, 0.50),
                "p95": _percentile(mention_counts, 0.95),
                "p99": _percentile(mention_counts, 0.99),
                "max": max(mention_counts, default=0),
            },
            "grammar_pairs_per_unit": {
                "p50": _percentile(pair_counts, 0.50),
                "p95": _percentile(pair_counts, 0.95),
                "p99": _percentile(pair_counts, 0.99),
                "max": max(pair_counts, default=0),
            },
            "coordination_arcs_per_unit": {
                "p50": _percentile(conjunction_counts, 0.50),
                "p95": _percentile(conjunction_counts, 0.95),
                "p99": _percentile(conjunction_counts, 0.99),
                "max": max(conjunction_counts, default=0),
            },
            "grammar_capture_yield": (
                round(sum(pair_counts) / sum(mention_counts), 4)
                if sum(mention_counts) else 0.0
            ),
            "dense_structure_units": len(dense_structure_units),
            "dense_structure_unit_ids": dense_structure_units[:20],
        },
        "identity_digest": stable_digest({
            "eligibility": [item.as_dict() for item in all_decisions],
            "surface": [item.model_dump(mode="json") for item in surface_records],
            "mapped": [item.model_dump(mode="json") for item in mapped],
            "assertions": [item.model_dump(mode="json") for item in assertions],
        }),
    }
    return RelationFastPathOutput(
        tuple(all_decisions),
        tuple(sorted(endpoint_entities.values(), key=lambda item: item.entity_id)),
        tuple(sorted(endpoint_mentions.values(), key=lambda item: item.mention_id)),
        tuple(surface_records), tuple(mapped), tuple(assertions), report,
    )


def openie_fact_merge_disposition(
    key: tuple[str | None, str | None, str | None],
    span: tuple[int, int],
    accepted_keys: set,
    qualified_spans_by_key: dict,
) -> str:
    """Evidence-scoped FACT merge contract (owner-ratified 2026-08-07).

    Fact identity (subject, predicate, object) dedupes globally, but a
    QUALIFIED syntax record vetoes an OpenIE FACT only when both read the
    SAME evidence (overlapping spans). Different evidence contexts coexist:
    a direct assertion in sentence A is never silenced by an attributed
    restatement in sentence B — the safe result applies only to genuine
    same-evidence status conflicts.
    """
    if key in accepted_keys:
        return "duplicate"
    for start, end in qualified_spans_by_key.get(key, ()):
        if start < span[1] and span[0] < end:
            return "blocked_same_evidence_qualified"
    return "promote"
