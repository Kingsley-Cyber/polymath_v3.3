"""Deterministic entity quality gate.

MEASURED PROBLEM (gliner_entity_gate_v1, n=135)
    span_correct 0.956 · type_correct 0.874 · graph_worthy 0.296
    The tagger reads spans accurately and assigns plausible types. It emits
    things that are not entities: pronouns, bare generic nouns, adjectives and
    document artifacts. High span+type with low graph-worthiness is the whole
    diagnosis.

ROOT CAUSE IS THE LABEL SET, NOT THE MODEL
    runpod_flash_extractor/registries/extraction_vocabularies.v1.json asks
    GLiNER for 25 labels, most of which are ONTOLOGICAL CATEGORIES rather than
    entity types: QUALITY, BEHAVIOR, STATE, PROCESS, GOAL, OUTCOME, CONDITION,
    METRIC, SIGNAL, BASELINE, POPULATION, INTERVENTION.

    Ask a zero-shot NER model for "QUALITY" and it correctly returns "good" and
    "painful". Ask for "BEHAVIOR" and it returns "habits", "desires",
    "addicted". That is the model doing exactly what it was told. GLiNER's
    documented weakness is common-noun misclassification, and an abstract label
    set maximises it.

    Measured graph-worthiness per label on the gate sample:
      ORGANIZATION 0.71 · PRODUCT 0.62 · SYSTEM 0.60 · DOCUMENT 0.50
      METHOD 0.50 · CONCEPT 0.40 · RESOURCE 0.33
      PERSON 0.11 · BEHAVIOR 0.11 · PLACE 0.12
      QUALITY 0.00 · GROUP 0.00 · PROCESS 0.00 · METRIC 0.00
      CONDITION 0.00 · INTERVENTION 0.00 · TIME_PATTERN 0.00

WHY THIS IS DETERMINISTIC AND NOT ANOTHER MODEL
    Every rule below is a pure function of (surface, type, POS, corpus
    statistics). Same input, same verdict, forever. No thresholds learned at
    runtime, no model call, no network. Each rejection increments a NAMED
    counter, so nothing is dropped silently — repo law.

    PERSON is filtered, NOT dropped: it scores 0.11 only because it catches
    pronouns and role nouns. It is the right label for "David Mamet".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

QUALITY_GATE_VERSION = "polymath.entity_quality.v1"

# ---------------------------------------------------------------------------
# R1 — closed-class surfaces. Never an entity, in any domain.
# ---------------------------------------------------------------------------
_PRONOUNS = frozenset({
    "i", "me", "my", "mine", "myself", "we", "us", "our", "ours", "ourselves",
    "you", "your", "yours", "yourself", "yourselves",
    "he", "him", "his", "himself", "she", "her", "hers", "herself",
    "it", "its", "itself", "they", "them", "their", "theirs", "themselves",
    "this", "that", "these", "those", "who", "whom", "whose", "which", "what",
    "anyone", "anybody", "anything", "someone", "somebody", "something",
    "everyone", "everybody", "everything", "no one", "nobody", "nothing",
    "one", "ones", "other", "others", "another", "each", "either", "neither",
    "all", "some", "any", "none", "both", "few", "many", "several",
    "here", "there", "everywhere", "anywhere", "somewhere",
})

_FUNCTION_WORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "as", "of",
    "in", "on", "at", "by", "for", "with", "from", "to", "into", "onto",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "have", "has", "had", "will", "would", "can", "could", "may", "might",
    "not", "no", "yes", "so", "such", "very", "just", "also", "too", "only",
})

# ---------------------------------------------------------------------------
# R2 — document artifacts. Structure, not content.
# ---------------------------------------------------------------------------
_LOCATOR_RE = re.compile(
    r"^(page|pg|p|figure|fig|table|tbl|chapter|ch|section|sec|appendix|"
    r"exhibit|part|step|practice|lesson|unit|module|volume|vol|note)"
    r"\s*[\d.\-–:]*$",
    re.I,
)
# Raw identifiers: epub ids, hashes, file stems, snake/kebab machine names.
_MACHINE_ID_RE = re.compile(
    r"^[A-Za-z]*[_\-]?\d{4,}[A-Za-z0-9_\-]*$|"      # Lume_9780307763662_...
    r"^[0-9a-f]{16,}$|"                              # hex digests
    r"^[A-Za-z0-9]+([_\-][A-Za-z0-9]+){3,}$"         # a_b_c_d machine names
)
_NON_WORD_RE = re.compile(r"^[\W\d_]+$")

# ---------------------------------------------------------------------------
# R4 — labels measured to yield ~zero graph-worthy entities.
# These are ontological CATEGORIES, not entity types. Dropping them is the
# single highest-yield change available without touching the pod image.
# ---------------------------------------------------------------------------
NOISE_LABELS = frozenset({
    "QUALITY", "GROUP", "PROCESS", "METRIC", "CONDITION", "INTERVENTION",
    "BEHAVIOR", "STATE", "SIGNAL", "BASELINE", "GOAL", "OUTCOME",
    "POPULATION", "TIME_PATTERN",
})

# ---------------------------------------------------------------------------
# R6 — collapse the 25-label pod vocabulary onto ontology.yaml's 14 types, so
# allowed_pairs can actually constrain something. MEASURED: 100% of sampled
# mentions carried a type outside the ontology, which made the typed gate
# decorative.
# ---------------------------------------------------------------------------
LABEL_TO_ONTOLOGY: dict[str, str] = {
    "PERSON": "Person",
    "AGENT": "Person",
    "ORGANIZATION": "Organization",
    "PLACE": "Location",
    "PRODUCT": "Product",
    "DOCUMENT": "Document",
    "SYSTEM": "Software",
    "METHOD": "Method",
    "CONCEPT": "Concept",
    "RESOURCE": "Artifact",
    "CONSTRAINT": "Rule",
    "EVENT": "Event",
    "STANDARD": "Standard",
    "LAW": "Law",
    "ARTIFACT": "Artifact",
    "TIMEREFERENCE": "TimeReference",
    # --- vocabulary v2 (lowercase, ontology-aligned) -----------------------
    # v2 labels ARE ontology types, so these are identity maps. They exist so
    # both vocabularies can be in flight during the pod rollout: chunks
    # extracted under v1 and v2 must normalise to the same values, or the
    # allowed_pairs gate would see two type systems in one corpus.
    "SOFTWARE": "Software",
    "LOCATION": "Location",
    "CONCEPT_V2": "Concept",
}

# Generic role/abstract nouns that are real words but useless as graph nodes.
# Deliberately SHORT and hand-curated: the corpus-frequency rule (R5) is the
# general mechanism; this list only covers high-frequency offenders that appear
# in every business/technical corpus.
_GENERIC_NOUNS = frozenset({
    "company", "customer", "customers", "client", "clients", "user", "users",
    "person", "people", "individual", "individuals", "firm", "business",
    "organization", "team", "group", "manager", "managers", "employee",
    "employees", "member", "members", "audience", "consumer", "consumers",
    "product", "products", "service", "services", "market", "markets",
    "system", "systems", "process", "processes", "method", "methods",
    "data", "information", "content", "value", "values", "price", "prices",
    "cost", "costs", "time", "way", "ways", "part", "parts", "type", "types",
    "form", "forms", "state", "states", "level", "levels", "point", "points",
    "area", "areas", "case", "cases", "number", "numbers", "result",
    "results", "example", "examples", "feature", "features", "item", "items",
    "thing", "things", "work", "order", "line", "site", "page", "text",
    "document", "report", "reports", "goal", "goals", "objective",
    "objectives", "strategy", "strategies", "approach", "approaches",
    "model", "models", "tool", "tools", "platform", "solution", "solutions",
    "resource", "resources", "experience", "performance", "quality",
    "behavior", "behaviour", "attention", "interest", "focus", "effort",
    "change", "changes", "difference", "impact", "effect", "effects",
    "benefit", "benefits", "risk", "risks", "issue", "issues", "problem",
    "problems", "need", "needs", "option", "options", "choice", "choices",
    "decision", "decisions", "action", "actions", "activity", "activities",
    "step", "steps", "stage", "stages", "phase", "phases", "role", "roles",
    "structure", "element", "elements", "component", "components", "aspect",
    "aspects", "factor", "factors", "condition", "conditions", "situation",
    "context", "environment", "framework", "standard", "standards", "policy",
    "policies", "rule", "rules", "principle", "principles", "concept",
    "concepts", "idea", "ideas", "theory", "theories", "practice",
    "practices", "technique", "techniques", "skill", "skills", "knowledge",
    "insight", "insights", "perspective", "view", "views", "opinion",
    "belief", "beliefs", "attitude", "emotion", "feeling", "feelings",
    "motivation", "intention", "purpose", "reason", "reasons", "cause",
    "causes", "source", "sources", "history", "background", "detail",
    "details", "fact", "facts", "evidence", "support", "distribution",
    "attributes", "characteristics", "narrative", "location", "workload",
    # Added after measuring residual survivors on the gate sample.
    "loyalty", "beats", "questions", "account", "accounts", "police",
    "stations", "campus", "studies", "study", "industry", "industries",
    "share", "reach", "returns", "return", "growth", "scale", "volume",
    "revenue", "profit", "margin", "margins", "budget", "budgets",
})

#: Floor for spans carrying proper-noun orthography. Lower than the common-noun
#: floor because GLiNER under-scores short proper nouns (see judge_entity).
PROPER_NOUN_CONFIDENCE_FLOOR = 0.35

# Acronyms and CamelCase are proper nouns even when a single token: AWS, S3,
# CloudFront, FearNot!. A lone Capitalized word is NOT enough on its own —
# sentence-initial common nouns capitalize too — unless it is also an acronym
# or internally capitalized.
_ACRONYM_RE = re.compile(r"^[A-Z][A-Z0-9]{1,}$")
_CAMEL_RE = re.compile(r"^[A-Z][a-z]+[A-Z][A-Za-z0-9]*")


def _is_strong_name(raw: str) -> bool:
    """Orthography that marks a NAME even for a single token.

    Stricter than _is_proper_noun_candidate: a lone Capitalized word does not
    qualify, because sentence-initial common nouns capitalize too.
    """
    tokens = raw.split()
    if not tokens:
        return False
    if any(_ACRONYM_RE.match(t.strip(".,!?")) for t in tokens):
        return True
    if any(_CAMEL_RE.match(t) for t in tokens):
        return True
    if len(tokens) > 1 and any(t[:1].isupper() for t in tokens[1:]):
        return True
    return False


def _is_proper_noun_candidate(raw: str) -> bool:
    """True when orthography marks this as a name, independent of model score."""
    tokens = raw.split()
    if not tokens:
        return False
    if any(_ACRONYM_RE.match(t.strip(".,!?")) for t in tokens):
        return True
    if any(_CAMEL_RE.match(t) for t in tokens):
        return True
    # Multi-token with a capitalized token after the first: "Amazon RDS",
    # "Jacob's Pillow Dance Festival". Position 0 alone is ambiguous.
    if len(tokens) > 1 and any(t[:1].isupper() for t in tokens[1:]):
        return True
    # Single capitalized token that is NOT a known generic ("Lumet", "Andromeda").
    if len(tokens) == 1 and tokens[0][:1].isupper():
        return tokens[0].lower() not in _GENERIC_NOUNS
    return False


# Types whose whole point is a NAMED referent. A location or organisation that
# is not a name ("block", "campus", "flea market", "police", "travel industry")
# is a common noun wearing a type label. MEASURED graph-worthiness: PLACE 0.12,
# and the surviving PLACE noise after the confidence split was entirely unnamed.
# Los Angeles survives; barren place does not.
NAMED_REQUIRED_TYPES = frozenset({"PLACE", "ORGANIZATION", "LOCATION"})

# Demonstrative/quantifier prefixes: "This pillow", "each customer". The span is
# a reference to a thing, not the thing's name.
_DEICTIC_PREFIX = frozenset({
    "this", "that", "these", "those", "such", "each", "every", "another",
    "same", "other", "any", "some", "certain", "various", "several",
})

REJECT_KEYS = (
    "entity_rejected_deictic",
    "entity_rejected_unnamed_for_type",
    "entity_rejected_pronoun",
    "entity_rejected_function_word",
    "entity_rejected_locator",
    "entity_rejected_machine_id",
    "entity_rejected_non_word",
    "entity_rejected_too_short",
    "entity_rejected_noise_label",
    "entity_rejected_generic_noun",
    "entity_rejected_not_nominal",
    "entity_rejected_low_confidence",
    "entity_rejected_corpus_frequency",
    "entity_type_remapped_to_ontology",
    "entity_type_unmappable",
)


@dataclass
class EntityVerdict:
    keep: bool
    reason: str = ""
    ontology_type: str = ""
    original_type: str = ""
    notes: dict[str, Any] = field(default_factory=dict)


def new_reject_counters() -> dict[str, int]:
    return dict.fromkeys(REJECT_KEYS, 0)


def _inc(c: dict[str, int] | None, k: str) -> None:
    if c is not None:
        c[k] = c.get(k, 0) + 1


def judge_entity(
    surface: str,
    entity_type: str,
    *,
    confidence: float = 1.0,
    min_confidence: float = 0.60,
    head_pos: str | None = None,
    corpus_doc_frequency: float | None = None,
    max_doc_frequency: float = 0.02,
    counters: dict[str, int] | None = None,
) -> EntityVerdict:
    """Decide whether one entity mention may become a graph node.

    Pure and deterministic: identical inputs always yield the identical verdict.

    head_pos             optional spaCy POS of the span head. When supplied, a
                         non-nominal head is rejected — this is what removes
                         "painful", "addicted", "good".
    corpus_doc_frequency share of corpus chunks containing this canonical form.
                         The general mechanism for genericness: a name in 40% of
                         chunks is a common noun no matter how it is spelled;
                         one in 0.01% is specific. Corpus-adaptive, and still
                         deterministic because it is computed from a fixed
                         corpus snapshot.
    """
    raw = (surface or "").strip()
    low = raw.lower()
    etype = (entity_type or "").strip()

    if len(raw) < 2:
        _inc(counters, "entity_rejected_too_short")
        return EntityVerdict(False, "too short to name anything")
    if _NON_WORD_RE.match(raw):
        _inc(counters, "entity_rejected_non_word")
        return EntityVerdict(False, "punctuation or digits only")
    if low in _PRONOUNS:
        _inc(counters, "entity_rejected_pronoun")
        return EntityVerdict(False, "pronoun: referent is unresolved here")
    if low in _FUNCTION_WORDS:
        _inc(counters, "entity_rejected_function_word")
        return EntityVerdict(False, "function word")
    if _LOCATOR_RE.match(raw):
        _inc(counters, "entity_rejected_locator")
        return EntityVerdict(False, "document locator, not content")
    if _MACHINE_ID_RE.match(raw):
        _inc(counters, "entity_rejected_machine_id")
        return EntityVerdict(False, "machine identifier, not content")
    first = low.split()[0] if low.split() else ""
    if first in _DEICTIC_PREFIX:
        _inc(counters, "entity_rejected_deictic")
        return EntityVerdict(False, "deictic reference, not a name")
    if etype.upper() in NOISE_LABELS:
        _inc(counters, "entity_rejected_noise_label")
        return EntityVerdict(
            False,
            f"label {etype} is an ontological category, not an entity type "
            f"(measured graph-worthiness ~0)",
        )
    if head_pos is not None and head_pos not in ("NOUN", "PROPN"):
        _inc(counters, "entity_rejected_not_nominal")
        return EntityVerdict(False, f"head POS {head_pos} is not nominal")
    # Confidence floor is SPLIT by orthography, not flat.
    #
    # MEASURED: a flat 0.60 floor destroyed 13 of the 14 good entities the gate
    # lost -- AWS (0.49), Amazon (0.54), CloudFront (0.40), CloudWatch (0.48),
    # Amazon RDS (0.54), Andromeda (0.40), Lumet (0.59), FearNot! (0.59).
    # GLiNER's confidence is poorly calibrated on SHORT PROPER NOUNS: there is
    # little context to condition on, so a real company name scores like a
    # coin flip.
    #
    # Capitalization is independent evidence the model's score does not carry.
    # A capitalized token in running text is a strong named-entity signal in
    # English, so proper-noun candidates get a lower floor and common nouns
    # keep the strict one. Deterministic: orthography is a property of the
    # string, not of a model.
    if _is_proper_noun_candidate(raw):
        floor = min(min_confidence, PROPER_NOUN_CONFIDENCE_FLOOR)
    else:
        floor = min_confidence
    if confidence < floor:
        _inc(counters, "entity_rejected_low_confidence")
        return EntityVerdict(
            False, f"confidence {confidence:.2f} below floor {floor:.2f}"
        )

    # Genericness. A multi-word span containing a proper noun survives even if
    # its head is generic ("Marketing Metrics", "Amazon S3").
    if low in _GENERIC_NOUNS and not any(w[:1].isupper() for w in raw.split()):
        _inc(counters, "entity_rejected_generic_noun")
        return EntityVerdict(False, "bare generic noun: not a specific referent")
    # R5 — CORPUS FREQUENCY. The general mechanism for genericness, and the
    # reason the hand-curated list above can stay short. A name appearing in
    # 40% of a corpus's chunks is a common noun in that corpus no matter how it
    # is spelled; one in 0.01% is specific. Corpus-adaptive yet deterministic:
    # computed from a fixed corpus snapshot, so the same snapshot always yields
    # the same verdict.
    #
    # Applied unless orthography independently marks a NAME (acronym, CamelCase,
    # or multi-token with internal capitals). A lone capitalized word is NOT
    # exempt -- "Space", "Relationship", "Architecture" capitalize at sentence
    # start and are exactly the open-vocabulary generics a fixed list misses.
    if (corpus_doc_frequency is not None
            and corpus_doc_frequency > max_doc_frequency
            and not _is_strong_name(raw)):
        _inc(counters, "entity_rejected_corpus_frequency")
        return EntityVerdict(
            False,
            f"appears in {corpus_doc_frequency:.1%} of chunks: a common noun "
            f"in this corpus",
        )

    if etype.upper() in NAMED_REQUIRED_TYPES and not _is_proper_noun_candidate(raw):
        _inc(counters, "entity_rejected_unnamed_for_type")
        return EntityVerdict(
            False, f"{etype} requires a named referent; this is a common noun"
        )

    mapped = LABEL_TO_ONTOLOGY.get(etype.upper())
    if mapped is None:
        _inc(counters, "entity_type_unmappable")
        return EntityVerdict(
            True, "kept, but type is outside ontology.yaml",
            ontology_type="other", original_type=etype,
        )
    _inc(counters, "entity_type_remapped_to_ontology")
    return EntityVerdict(True, "", ontology_type=mapped, original_type=etype)


def filter_entities(
    entities: list[dict],
    *,
    doc_frequency: dict[str, float] | None = None,
    min_confidence: float = 0.60,
    max_doc_frequency: float = 0.02,
    counters: dict[str, int] | None = None,
) -> list[dict]:
    """Apply the gate to a chunk's entity list, returning survivors.

    Survivors carry `entity_type` remapped onto ontology.yaml, plus
    `source_entity_type` preserving what the tagger actually said, so nothing
    is lost and the remap is auditable.
    """
    out: list[dict] = []
    for e in entities or []:
        surface = e.get("surface_form") or e.get("text") or e.get("surface") or ""
        canon = (e.get("canonical_name") or e.get("canonical_label")
                 or surface).strip().lower()
        verdict = judge_entity(
            surface,
            e.get("entity_type") or "",
            confidence=float(e.get("confidence") or 0.0),
            min_confidence=min_confidence,
            corpus_doc_frequency=(doc_frequency or {}).get(canon),
            max_doc_frequency=max_doc_frequency,
            counters=counters,
        )
        if not verdict.keep:
            continue
        row = dict(e)
        row["source_entity_type"] = verdict.original_type
        row["entity_type"] = verdict.ontology_type
        row["entity_quality_version"] = QUALITY_GATE_VERSION
        out.append(row)
    return out


def annotate_entities(
    entities: list[dict],
    *,
    doc_frequency: dict[str, float] | None = None,
    min_confidence: float = 0.60,
    max_doc_frequency: float = 0.002,
    counters: dict[str, int] | None = None,
) -> list[dict]:
    """Mark entities with a graph-eligibility verdict instead of removing them.

    NON-DESTRUCTIVE by design. Deleting 2.8M stored mentions would be
    irreversible and would silently orphan the relations, facts and claims that
    reference them. Marking lets every consumer opt in, keeps the tagger's raw
    output auditable, and makes the whole gate revertible by version stamp.

    Adds to each entity, without removing anything:
      graph_eligible          bool   — may this become a graph node?
      graph_eligible_reason   str    — why not, when False
      ontology_entity_type    str    — type remapped onto ontology.yaml
      entity_quality_version  str    — which gate version ruled
    """
    out: list[dict] = []
    for e in entities or []:
        surface = e.get("surface_form") or e.get("text") or e.get("surface") or ""
        canon = (e.get("canonical_name") or e.get("canonical_label")
                 or surface).strip().lower()
        v = judge_entity(
            surface,
            e.get("entity_type") or "",
            confidence=float(e.get("confidence") or 0.0),
            min_confidence=min_confidence,
            corpus_doc_frequency=(doc_frequency or {}).get(canon),
            max_doc_frequency=max_doc_frequency,
            counters=counters,
        )
        row = dict(e)
        row["graph_eligible"] = v.keep
        row["graph_eligible_reason"] = "" if v.keep else v.reason
        row["ontology_entity_type"] = v.ontology_type or "other"
        row["entity_quality_version"] = QUALITY_GATE_VERSION
        out.append(row)
    return out


def eligible_only(entities: list[dict]) -> list[dict]:
    """Entities cleared for the graph. Un-annotated rows pass through.

    Pass-through is deliberate: a corpus that predates the gate must not
    silently lose every entity just because it was never annotated. Absence of
    a verdict is not a negative verdict.
    """
    return [
        e for e in (entities or [])
        if e.get("graph_eligible", True) is not False
    ]


# ---------------------------------------------------------------------------
# TWO TIERS — measured necessity, not theory.
#
# Applying the strict node gate as a PRECONDITION for relation extraction
# collapsed yield 34x (308 relations -> 9 over 1,200 chunks) because a relation
# needs BOTH endpoints: at 24% entity survival, pair survival is ~0.24^2 ~= 6%.
# Precision went to 1.000 and the lane went silent, which is the same failure
# as before wearing the opposite mask.
#
# HARD rules describe things that are not referents at all — pronouns,
# document furniture, machine ids, ontological-category labels. Those must
# never anchor anything.
#
# STRICT rules describe things that are real referents but poor NODES — bare
# generic nouns, high corpus frequency, unnamed places. A relation whose object
# is "strategy" is still informative when its subject is "Kotler"; a NODE
# called "strategy" is not.
# ---------------------------------------------------------------------------

_HARD_REJECT_KEYS = frozenset({
    "entity_rejected_pronoun",
    "entity_rejected_function_word",
    "entity_rejected_locator",
    "entity_rejected_machine_id",
    "entity_rejected_non_word",
    "entity_rejected_too_short",
    "entity_rejected_noise_label",
    "entity_rejected_deictic",
})


def judge_relation_anchor(
    surface: str,
    entity_type: str,
    *,
    confidence: float = 1.0,
    counters: dict[str, int] | None = None,
) -> EntityVerdict:
    """Relaxed tier: may this span ANCHOR a relation?

    Applies only the HARD rules. No genericness test, no corpus-frequency test,
    no confidence floor — those decide node-worthiness, not whether a span can
    participate in an asserted relation.
    """
    probe: dict[str, int] = {}
    verdict = judge_entity(
        surface, entity_type, confidence=1.0, min_confidence=0.0,
        corpus_doc_frequency=None, counters=probe,
    )
    hard_hit = next((k for k in probe if probe[k] and k in _HARD_REJECT_KEYS), None)
    if hard_hit:
        _inc(counters, hard_hit)
        return EntityVerdict(False, verdict.reason)
    mapped = LABEL_TO_ONTOLOGY.get((entity_type or "").upper(), "other")
    return EntityVerdict(True, "", ontology_type=mapped,
                         original_type=entity_type or "")


def annotate_entities_two_tier(
    entities: list[dict],
    *,
    doc_frequency: dict[str, float] | None = None,
    min_confidence: float = 0.60,
    max_doc_frequency: float = 0.002,
    counters: dict[str, int] | None = None,
) -> list[dict]:
    """Stamp BOTH verdicts: relation_eligible (relaxed) and graph_eligible."""
    out = annotate_entities(
        entities, doc_frequency=doc_frequency, min_confidence=min_confidence,
        max_doc_frequency=max_doc_frequency, counters=counters,
    )
    for row in out:
        surface = (row.get("surface_form") or row.get("text")
                   or row.get("surface") or "")
        v = judge_relation_anchor(surface, row.get("entity_type") or "",
                                  confidence=float(row.get("confidence") or 0.0))
        row["relation_eligible"] = v.keep
    return out


def relation_anchors(entities: list[dict]) -> list[dict]:
    """Entities cleared to anchor a relation. Un-annotated rows pass through."""
    return [
        e for e in (entities or [])
        if e.get("relation_eligible", True) is not False
    ]
