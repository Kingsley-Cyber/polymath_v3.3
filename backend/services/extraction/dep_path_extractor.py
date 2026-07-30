"""Core dependency-path relation extractor.

Derives relations from spaCy dependency paths between GLiNER entity heads.
Confidence sentinels (per 00_LOCKED_DECISIONS.md):
  - 1.0: DependencyMatcher high-precision patterns (deterministic grammar)
  - 0.9: Generic shortest-path inference (heuristic rule)

Predicate resolution is a 4-tier STRUCTURAL cascade (tiers, not ordered rules —
ordering within a tier is never load-bearing; ties broken alphabetically):
  T1: (signature, lemma, subj_type, obj_type) — most specific
  T2: (signature, lemma) — signature-aware (handles copular, passive)
  T3: (lemma) — flat synonym dict, last resort
  T4: DROP — default. NEVER lemma.upper().

Signature format (compact dash-joined, parameterized prepositions):
  "Microsoft acquired GitHub"        -> nsubj-VERB-dobj
  "Qdrant is a vector database"      -> nsubj-VERB-attr
  "Qdrant is in Rust"                -> nsubj-VERB-prep:in-pobj
  "GitHub was acquired by Microsoft" -> nsubjpass-VERB-agent-pobj
  "Qdrant, a vector database, ..."   -> appos (verbless)

Resolver returns (predicate, swap) | None:
  swap=True marks passive/agent constructions where the grammatical subject
  is the semantic object. Direction stays explicit.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import spacy
import yaml
from spacy.matcher import DependencyMatcher
from spacy.tokens import Doc, Token

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EntitySpan:
    """A typed entity span produced by GLiNER (or any NER upstream)."""

    surface: str
    start_char: int
    end_char: int
    entity_type: str = ""
    canonical_name: str = ""


@dataclass(slots=True)
class ExtractedTriple:
    """A single extracted relation triple with full provenance."""

    subject_surface: str
    subject_start: int
    subject_end: int
    predicate: str  # normalized
    predicate_lemma: str
    predicate_surface: str
    object_surface: str
    object_start: int
    object_end: int
    confidence: float  # 1.0 pattern (deterministic) / 0.9 parse-inferred (heuristic)
    dep_signature: str  # "ENTITY_A <-nsubj- VERB -dobj-> ENTITY_B"
    polarity: str = "POSITIVE"  # POSITIVE / NEGATIVE
    modality: str = "ASSERTED"  # ASSERTED / HYPOTHETICAL / CONDITIONAL
    assertion_mode: str = "direct"  # direct / attributed / conditional
    temporal_cue: str | None = None
    sentence_text: str = ""
    sentence_idx: int = 0
    chunk_id: str = ""
    doc_id: str = ""
    section_path: str = ""

    @property
    def is_graph_edge(self) -> bool:
        """True if this candidate qualifies as an asserted graph edge.

        The graph writer boundary uses this to filter: only direct,
        positive, asserted candidates become Neo4j edges. Qualified
        candidates (negated, modal, attributed, conditional) are preserved
        for the ClaimRecord path but never written as graph facts.
        """
        return (
            self.polarity == "POSITIVE"
            and self.modality == "ASSERTED"
            and self.assertion_mode == "direct"
        )


# ---------------------------------------------------------------------------
# Predicate resolution — 4-tier composite resolver
# ---------------------------------------------------------------------------

_SYNONYM_MAP: dict[str, str] | None = None
_SIGNATURE_RULES: list[dict] | None = None
_RULES_BY_LEMMA: dict[str, list[dict]] | None = None  # lemma → [rules] index
_ALLOWED_PAIRS: dict[str, frozenset[tuple[str, str]]] | None = None
_SUPPRESSION_CFG: dict[str, Any] | None = None
_CONFIG_LOADED: bool = False

# Suppression config accessors (populated at config load)
_ATTRIBUTION_VERBS: frozenset[str] = frozenset()
_CONDITIONAL_MARKERS: frozenset[str] = frozenset()
_CONTRAST_PREPS: frozenset[str] = frozenset()
_CONTRAST_CONJS: frozenset[str] = frozenset()
_LIGHT_VERBS: frozenset[str] = frozenset()
_LOW_PARSE_THRESHOLD: float = 0.02

# Emittable predicates: EXACTLY the ghost_b_schemas.py Predicate Literal (31 values).
# The ontology provides allowed_pairs constraints for a SUBSET of these.
# Predicates in ontology but NOT in the Literal (has_part, includes, quantizes,
# example_of, evaluates, deploys, creates, trains, runs) are NOT emittable —
# they would fail Pydantic validation downstream. Extending the Literal is an
# owner decision.
_VALID_PREDICATES = frozenset({
    "part_of", "member_of", "located_in", "works_for", "created_by",
    "owns", "affiliated_with", "synonym_of", "instance_of", "uses",
    "runs_on", "trained_on", "references", "implements", "depends_on",
    "produces", "stores", "detects", "supports", "defines", "represents",
    "maps_to", "preceded_by", "causes", "overlaps", "derived_from",
    "contradicts", "excepts", "overrides", "related_to",
})

# Rejection counters (extraction-layer allowed_pairs gate).
# Keyed by reason string. Never drop silently.
rejection_counters: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Canonical suppression/qualifier counter keys — SINGLE SOURCE OF TRUTH.
#
# R-pre (2026-07-30): every key below is incremented by _inc() somewhere in this
# module. Before R-pre, ghost_b_local.py hand-maintained a PARTIAL copy of this
# list (it omitted all three P2 structural guards) and emitted only 4 counters
# on ExtractionResult, so the rest were computed and garbage-collected at the
# emit boundary. The repo law "never drop silently — every suppression
# increments a named counter" was honored here and defeated downstream.
#
# Consumers MUST build their counter dicts from new_counters() rather than
# retyping the keys, so a new suppression rule can never go unpublished again.
# test_suppression_counter_coverage.py asserts this list == the keys actually
# incremented in this file.
# ---------------------------------------------------------------------------

# Suppressed = candidate dropped, never emitted.
SUPPRESSION_KEYS: tuple[str, ...] = (
    "skipped_low_parse_confidence",
    "skipped_verbless",
    "suppressed_expletive",
    "suppressed_contrast",
    "suppressed_agentless_passive",
    "suppressed_light_verb",
    "suppressed_multi_clause",        # P2 structural guard — was unpublished
    "suppressed_conjunct_crossing",   # P2 structural guard — was unpublished
    "suppressed_exception_boundary",  # P2 structural guard — was unpublished
)

# Qualified = candidate IS emitted, but carries a qualifier that keeps it off
# the graph (claims path only). Diagnostic, not suppression.
QUALIFIER_KEYS: tuple[str, ...] = (
    "qualified_negated",
    "qualified_modal",
    "qualified_attributed",
    "qualified_conditional",
)

# Contract-validation drops owned by ghost_b_local._validated_* helpers.
VALIDATION_DROP_KEYS: tuple[str, ...] = (
    "entity_drop",
    "relation_drop",
    "evidence_drop",
    "fact_drop",
)

# Adapter-boundary drops (services/extraction/spacy_relation_adapter.py). These
# were batch-level local variables logged in aggregate and never attributed to a
# chunk; R-pre makes them per-chunk so the loss is locatable.
ADAPTER_DROP_KEYS: tuple[str, ...] = (
    "adapter_allowed_pairs_rejected",
    "adapter_noise_or_t4_dropped",
    "adapter_non_graph_edge",   # qualified triples withheld from the graph
    "adapter_cap_truncated",    # edges discarded by max_related (R5 targets this)
)

ALL_COUNTER_KEYS: tuple[str, ...] = (
    VALIDATION_DROP_KEYS + QUALIFIER_KEYS + SUPPRESSION_KEYS + ADAPTER_DROP_KEYS
)


def new_counters() -> dict[str, int]:
    """Fresh zeroed counter dict with EVERY canonical key present.

    Zero-initialized (not defaultdict) so that "this guard never fired" is
    distinguishable from "this guard is not wired" in the persisted record.
    """
    return dict.fromkeys(ALL_COUNTER_KEYS, 0)


def _config_dir() -> Path:
    """Resolve the config/ directory. Raises if not found (fail LOUD)."""
    _this = Path(__file__).resolve()
    for parent_idx in (3, 2):
        candidate = _this.parents[parent_idx] / "config"
        if candidate.is_dir():
            return candidate
    raise RuntimeError(
        f"FATAL: config/ directory not found relative to {__file__}. "
        "Cannot load predicate_synonyms.yaml or ontology.yaml. "
        "Refusing to start with silent fallback."
    )


def _load_config() -> None:
    """Load and VALIDATE all config at first use. Fails LOUD on any error.

    Validation rules:
      - predicate_synonyms.yaml must exist and parse
      - every predicate value in synonyms must be in _VALID_PREDICATES (the Literal)
      - every predicate value in signature_rules must be valid or __DROP__
      - every T1 rule (subj_type + obj_type specified) must satisfy
        ontology.yaml allowed_pairs for its predicate
      - overlapping rules (same signature+lemma+types → different predicates) fail loud
      - ontology.yaml must exist and parse
    """
    global _SYNONYM_MAP, _SIGNATURE_RULES, _RULES_BY_LEMMA, _ALLOWED_PAIRS, _CONFIG_LOADED
    if _CONFIG_LOADED:
        return

    cfg = _config_dir()

    # --- Load ontology.yaml (allowed_pairs ONLY — not for extending predicates) ---
    ontology_path = cfg / "ontology.yaml"
    if not ontology_path.exists():
        raise RuntimeError(
            f"FATAL: {ontology_path} not found. "
            "Cannot enforce allowed_pairs gate."
        )
    ontology_data = yaml.safe_load(ontology_path.read_text())
    if not isinstance(ontology_data, dict):
        raise RuntimeError(
            f"FATAL: {ontology_path} is not a valid YAML mapping."
        )
    _ALLOWED_PAIRS = {}
    predicates_section = ontology_data.get("predicates") or {}
    for pred, spec in predicates_section.items():
        pairs = (spec or {}).get("allowed_pairs") or []
        _ALLOWED_PAIRS[pred] = frozenset(
            (str(a), str(b)) for a, b in pairs
        )
    # NOTE: We do NOT extend _VALID_PREDICATES from ontology. The emittable set
    # is the wire Literal only. Ontology predicates not in the Literal (has_part,
    # includes, quantizes, etc.) are constraints for OTHER consumers, not for
    # this extractor.

    # --- Load predicate_synonyms.yaml ---
    synonyms_path = cfg / "predicate_synonyms.yaml"
    if not synonyms_path.exists():
        raise RuntimeError(
            f"FATAL: {synonyms_path} not found. "
            "Cannot resolve predicates without synonym config."
        )
    data = yaml.safe_load(synonyms_path.read_text())
    if not isinstance(data, dict):
        raise RuntimeError(
            f"FATAL: {synonyms_path} is not a valid YAML mapping."
        )

    # --- Validate + load synonyms (T3) ---
    raw_syns = data.get("synonyms") or {}
    _SYNONYM_MAP = {}
    _invalid_syns: list[str] = []
    for k, v in raw_syns.items():
        if v not in _VALID_PREDICATES:
            _invalid_syns.append(f"  {k!r} -> {v!r}")
        else:
            _SYNONYM_MAP[k.lower()] = v
    if _invalid_syns:
        raise RuntimeError(
            f"FATAL: predicate_synonyms.yaml synonyms target non-emittable predicates "
            f"(not in ghost_b_schemas.Predicate Literal):\n"
            + "\n".join(_invalid_syns)
            + "\nEmittable predicates: " + ", ".join(sorted(_VALID_PREDICATES))
        )

    # --- Validate + load signature_rules (T1/T2) ---
    raw_rules = data.get("signature_rules") or []
    _SIGNATURE_RULES = []
    _invalid_rules: list[str] = []
    for i, rule in enumerate(raw_rules):
        pred = rule.get("predicate", "")
        if pred not in _VALID_PREDICATES and pred != "__DROP__":
            _invalid_rules.append(
                f"  rule[{i}]: predicate={pred!r} not in Predicate Literal"
            )
            continue
        # T1 validation: if subj_type AND obj_type specified, check allowed_pairs
        r_subj = rule.get("subj_type", "")
        r_obj = rule.get("obj_type", "")
        if r_subj and r_obj and pred in _VALID_PREDICATES and pred != "__DROP__":
            pair_set = _ALLOWED_PAIRS.get(pred)
            if pair_set is not None and (r_subj, r_obj) not in pair_set:
                _invalid_rules.append(
                    f"  rule[{i}]: ({r_subj}, {pred}, {r_obj}) "
                    f"violates ontology allowed_pairs"
                )
                continue
        _SIGNATURE_RULES.append(rule)
    if _invalid_rules:
        raise RuntimeError(
            f"FATAL: predicate_synonyms.yaml signature_rules validation failed:\n"
            + "\n".join(_invalid_rules)
        )

    # --- Detect overlapping rules (same match key → different predicates) ---
    _overlap_errors: list[str] = []
    _seen_keys: dict[tuple, dict] = {}  # (sig, lemma, subj, obj) → rule
    for i, rule in enumerate(_SIGNATURE_RULES):
        key = (
            rule.get("signature_contains", ""),
            rule.get("lemma", "").lower(),
            rule.get("subj_type", ""),
            rule.get("obj_type", ""),
        )
        if key in _seen_keys:
            prev = _seen_keys[key]
            if prev.get("predicate") != rule.get("predicate") or prev.get("swap") != rule.get("swap"):
                _overlap_errors.append(
                    f"  rules overlap on key={key}:\n"
                    f"    earlier: {prev}\n"
                    f"    later:   {rule}"
                )
        else:
            _seen_keys[key] = rule
    if _overlap_errors:
        raise RuntimeError(
            f"FATAL: ambiguous signature_rules (overlapping match keys with "
            f"different predicates). Fix config to remove ambiguity:\n"
            + "\n".join(_overlap_errors)
        )

    # --- Build lemma → [rules] index for O(1) lookup ---
    _RULES_BY_LEMMA = {}
    for rule in _SIGNATURE_RULES:
        lemma_key = rule.get("lemma", "").lower()
        _RULES_BY_LEMMA.setdefault(lemma_key, []).append(rule)

    # --- Load suppression config ---
    global _SUPPRESSION_CFG, _ATTRIBUTION_VERBS, _CONDITIONAL_MARKERS
    global _CONTRAST_PREPS, _CONTRAST_CONJS, _LIGHT_VERBS, _LOW_PARSE_THRESHOLD
    supp = data.get("suppression") or {}
    if not isinstance(supp, dict):
        raise RuntimeError(
            f"FATAL: 'suppression' section in predicate_synonyms.yaml must be a mapping."
        )
    _ATTRIBUTION_VERBS = frozenset(supp.get("attribution_verbs") or [])
    _CONDITIONAL_MARKERS = frozenset(supp.get("conditional_markers") or [])
    _CONTRAST_PREPS = frozenset(supp.get("contrast_prepositions") or [])
    _CONTRAST_CONJS = frozenset(supp.get("contrast_conjunctions") or [])
    _LIGHT_VERBS = frozenset(supp.get("light_verbs") or [])
    _LOW_PARSE_THRESHOLD = float(supp.get("low_parse_confidence_threshold", 0.02))
    _SUPPRESSION_CFG = supp

    _CONFIG_LOADED = True
    logger.info(
        "dep_path_extractor config loaded: %d synonyms, %d signature_rules "
        "(%d lemma buckets), %d ontology constraints, "
        "suppression(attrib=%d, cond=%d, light=%d)",
        len(_SYNONYM_MAP), len(_SIGNATURE_RULES), len(_RULES_BY_LEMMA),
        len(_ALLOWED_PAIRS),
        len(_ATTRIBUTION_VERBS), len(_CONDITIONAL_MARKERS), len(_LIGHT_VERBS),
    )


def _load_synonyms() -> dict[str, str]:
    """Load T3 flat synonym map. Triggers full config validation on first call."""
    _load_config()
    return _SYNONYM_MAP  # type: ignore[return-value]


def _load_signature_rules() -> list[dict]:
    """Load T1/T2 signature-aware rules. Triggers full config validation."""
    _load_config()
    return _SIGNATURE_RULES  # type: ignore[return-value]


def pair_allowed(predicate: str, subject_type: str, object_type: str) -> bool:
    """Check if (subject_type, predicate, object_type) satisfies ontology.

    Returns True if:
      - predicate has no allowed_pairs entry (unconstrained)
      - the pair is in the allowed set
      - either type is empty or 'other' (wildcard)
    """
    _load_config()
    assert _ALLOWED_PAIRS is not None
    pair_set = _ALLOWED_PAIRS.get(predicate)
    if pair_set is None:
        return True  # unconstrained predicate
    if not subject_type or not object_type:
        return True  # missing type info
    if subject_type == "other" or object_type == "other":
        return True  # wildcard
    return (subject_type, object_type) in pair_set


# Copular complement prepositions → (predicate, swap) (used by T2 copular logic)
_COPULAR_PREP_MAP: dict[str, tuple[str, bool]] = {
    "in": ("located_in", False),
    "at": ("located_in", False),
    "on": ("located_in", False),
    "from": ("derived_from", False),
    "by": ("created_by", True),   # swap: grammatical subject is semantic object
    "of": ("part_of", False),
    "for": ("supports", False),
    "with": ("overlaps", False),
}


def _resolve_copular(pred_tok: Token, object_tok: Token) -> tuple[str, bool] | None:
    """T2 copular resolution: branch on ClearNLP complement structure.

    spaCy English models use ClearNLP/OntoNotes labels:
      attr  = nominal predicate complement ("X is a Y")
      acomp = adjectival complement ("X is fast") — DROP
      prep  = prepositional complement ("X is in Y")

    Returns (predicate, swap) or None to signal DROP.
    """
    # Prepositional complement: "X is in Y", "X was built by Y"
    for child in pred_tok.children:
        if child.dep_ == "prep" and child.lemma_.lower() in _COPULAR_PREP_MAP:
            return _COPULAR_PREP_MAP[child.lemma_.lower()]

    # Adjectival/verbal complement — property, not a relation edge
    if object_tok.dep_ in ("acomp", "xcomp", "advcl"):
        return None
    if object_tok.pos_ in ("ADJ", "ADV"):
        return None

    # Nominal complement (attr, appos) → instance_of
    if object_tok.dep_ in ("attr", "appos", "nsubj", "ROOT"):
        return ("instance_of", False)
    if object_tok.pos_ in ("NOUN", "PROPN"):
        return ("instance_of", False)

    return None  # unknown complement structure — drop rather than pollute


def _has_quantity_object(object_tok: Token) -> bool:
    """Detect if the object token's subtree is a measurement/quantity.

    Used by the 'have' hazard: "X has 4 GB of RAM" → DROP (fact for
    enrich.py, not a graph edge). Checks for nummod, QUANTITY/NUMBER
    POS tags, or measurement-unit patterns in the object subtree.
    """
    for child in object_tok.subtree:
        if child.dep_ == "nummod":
            return True
        if child.pos_ in ("NUM", "QUANTITY"):
            return True
        # Measurement unit pattern: "4 GB", "10 ms", "2 GHz"
        if child.like_num and object_tok.pos_ in ("NOUN", "PROPN"):
            return True
    return False


def _resolve_possessive(
    path: list[tuple[str, str, int]],
    doc: Doc,
    tok_a: Token,
    tok_b: Token,
    ent_a: "EntitySpan",
    ent_b: "EntitySpan",
) -> "ExtractedTriple | None":
    """Noun-anchored possessive: "Google's TensorFlow" → (Google, owns, TensorFlow).

    Fires when the dep path between two entities contains a 'poss' label
    and no verb was found on the path. The possessor owns the possessed.
    """
    has_poss = any(dep == "poss" for dep, _, _ in path)
    if not has_poss:
        return None

    # Determine possessor: the token with dep_=="poss" is the possessor
    if tok_a.dep_ == "poss":
        possessor_ent, possessed_ent = ent_a, ent_b
        possessor_tok, possessed_tok = tok_a, tok_b
    elif tok_b.dep_ == "poss":
        possessor_ent, possessed_ent = ent_b, ent_a
        possessor_tok, possessed_tok = tok_b, tok_a
    else:
        # poss is on an intermediate token — check subtree ownership
        # e.g. "Google's" modifies an intermediate noun that IS tok_b
        for dep, direction, tidx in path:
            if dep == "poss":
                poss_tok = doc[tidx]
                # The poss token's head is the possessed noun
                if poss_tok.head.i == tok_b.i or poss_tok.head.i == tok_a.i:
                    if tok_a.i == poss_tok.head.i:
                        possessor_ent, possessed_ent = ent_b, ent_a
                        possessor_tok, possessed_tok = tok_b, tok_a
                    else:
                        possessor_ent, possessed_ent = ent_a, ent_b
                        possessor_tok, possessed_tok = tok_a, tok_b
                    break
        else:
            return None

    # Gate: check allowed_pairs for "owns"
    _load_config()
    if not pair_allowed("owns", possessor_ent.entity_type, possessed_ent.entity_type):
        return None

    sig = "poss"
    return ExtractedTriple(
        subject_surface=possessor_ent.surface,
        subject_start=possessor_ent.start_char,
        subject_end=possessor_ent.end_char,
        predicate="owns",
        predicate_lemma="own",
        predicate_surface="'s",
        object_surface=possessed_ent.surface,
        object_start=possessed_ent.start_char,
        object_end=possessed_ent.end_char,
        confidence=0.9,  # heuristic (noun-anchored, no verb)
        dep_signature=sig,
        polarity="POSITIVE",
        modality="ASSERTED",
        sentence_text=possessed_tok.sent.text if possessed_tok is not None else "",
        sentence_idx=0,
    )


def _sig_contains_segment(signature: str, pattern: str) -> bool:
    """Segment-exact signature matching.

    Signatures are dash-separated segments like "nsubj-ROOT-dobj".
    A pattern matches if it equals one or more complete segments.
    This prevents prep:in matching prep:into, prep:on matching prep:onto.
    """
    if not pattern:
        return True
    segments = signature.split("-")
    # Pattern may itself be multi-segment (e.g. "prep:by-agent")
    pat_segments = pattern.split("-")
    # Check if pat_segments appears as a contiguous subsequence of segments
    pat_len = len(pat_segments)
    for i in range(len(segments) - pat_len + 1):
        if segments[i:i + pat_len] == pat_segments:
            return True
    return False


def resolve_predicate(
    signature: str,
    lemma: str,
    subject_type: str,
    object_type: str,
    pred_tok: Token | None = None,
    object_tok: Token | None = None,
) -> tuple[str, bool] | None:
    """4-tier STRUCTURAL composite predicate resolver.

    Tiers are structural, NOT ordered rules. Overlapping rules are
    detected at config load and fail loud — at runtime, at most one
    rule matches per tier.

    T1: (signature, lemma, subj_type, obj_type) — exact context
    T2: (signature, lemma) — signature-aware (copular, passive)
    T3: (lemma) — flat synonym dict
    T4: DROP — default. NEVER lemma.upper().

    Returns (predicate, swap) where swap=True marks passive/agent
    constructions, or None (drop this edge).
    """
    _load_config()
    # Lemma-indexed lookup: only scan rules matching this lemma (or wildcard "")
    bucket = (_RULES_BY_LEMMA or {}).get(lemma, [])
    wildcard_bucket = (_RULES_BY_LEMMA or {}).get("", [])
    rules = bucket + wildcard_bucket

    # --- T1: rules with BOTH subj_type and obj_type constraints ---
    for rule in rules:
        r_subj = rule.get("subj_type", "")
        r_obj = rule.get("obj_type", "")
        if not (r_subj and r_obj):
            continue  # not a T1 rule
        sig_pat = rule.get("signature_contains", "")
        if not _sig_contains_segment(signature, sig_pat):
            continue
        if subject_type and r_subj != subject_type:
            continue
        if object_type and r_obj != object_type:
            continue
        pred = rule.get("predicate", "")
        if pred == "__DROP__":
            return None  # explicit drop wins immediately
        if pred in _VALID_PREDICATES:
            return (pred, bool(rule.get("swap", False)))

    # --- T2: rules with signature_contains but NOT both type constraints ---
    for rule in rules:
        r_subj = rule.get("subj_type", "")
        r_obj = rule.get("obj_type", "")
        if r_subj and r_obj:
            continue  # that's a T1 rule, already checked
        sig_pat = rule.get("signature_contains", "")
        if not _sig_contains_segment(signature, sig_pat):
            continue
        # Partial type constraint (one-sided)
        if r_subj and subject_type and r_subj != subject_type:
            continue
        if r_obj and object_type and r_obj != object_type:
            continue
        pred = rule.get("predicate", "")
        swap = bool(rule.get("swap", False))
        if pred == "__DROP__":
            return None  # explicit drop wins immediately
        if pred in _VALID_PREDICATES:
            return (pred, swap)

    # --- T2 special: copular branching (requires token access) ---
    if lemma == "be" and pred_tok is not None and object_tok is not None:
        return _resolve_copular(pred_tok, object_tok)

    # --- T2 special: "have" hazard (6.2% of corpus) ---
    if lemma == "have" and object_tok is not None:
        if _has_quantity_object(object_tok):
            return None  # "X has 4 GB of RAM" → fact, not edge
        # "X has Y" → Y is part_of X. Swap so Y becomes subject.
        return ("part_of", True)

    # --- T3: flat synonym dict ---
    synonyms = _load_synonyms()
    t3 = synonyms.get(lemma)
    if t3 and t3 in _VALID_PREDICATES:
        return (t3, False)

    # --- T4: DROP — do not manufacture predicates ---
    return None


# ---------------------------------------------------------------------------
# Dependency path utilities
# ---------------------------------------------------------------------------


def _find_head_token(doc: Doc, start_char: int, end_char: int) -> Token | None:
    """Find the syntactic head token for an entity span.

    Strategy: find all tokens within [start_char, end_char), then return
    the one highest in the dependency tree (closest to root).
    """
    span_tokens = [tok for tok in doc if tok.idx >= start_char and tok.idx + len(tok.text) <= end_char]
    if not span_tokens:
        # Fallback: token whose idx is within range
        span_tokens = [tok for tok in doc if start_char <= tok.idx < end_char]
    if not span_tokens:
        return None
    # Walk up to the highest token in the span (minimum depth)
    best = span_tokens[0]
    for tok in span_tokens[1:]:
        if tok.dep_ == "ROOT":
            return tok
        # Prefer the token that is ancestor of others
        if best.head == tok:
            best = tok
    # Walk up one level if the best token is a determiner or modifier
    if best.dep_ in ("det", "amod", "compound", "nummod") and best.head.i != best.i:
        best = best.head
    return best


def _shortest_dep_path(doc: Doc, tok_a: Token, tok_b: Token) -> list[tuple[str, str, int]]:
    """BFS shortest path on the undirected dependency tree.

    Returns list of (dep_label, direction, token_index) tuples representing
    the path from tok_a to tok_b. Direction is 'up' (toward root) or 'down'
    (away from root).
    """
    if tok_a.i == tok_b.i:
        return []

    # Build adjacency list (undirected)
    n = len(doc)
    adj: list[list[tuple[int, str, str]]] = [[] for _ in range(n)]
    for tok in doc:
        if tok.head.i != tok.i:
            adj[tok.i].append((tok.head.i, tok.dep_, "up"))
            adj[tok.head.i].append((tok.i, tok.dep_, "down"))

    # BFS
    visited = [False] * n
    parent: list[tuple[int, str, str] | None] = [None] * n
    queue = deque([tok_a.i])
    visited[tok_a.i] = True

    while queue:
        curr = queue.popleft()
        if curr == tok_b.i:
            break
        for neighbor, dep, direction in adj[curr]:
            if not visited[neighbor]:
                visited[neighbor] = True
                parent[neighbor] = (curr, dep, direction)
                queue.append(neighbor)

    # Reconstruct path
    if not visited[tok_b.i]:
        return []
    path: list[tuple[str, str, int]] = []
    node = tok_b.i
    while parent[node] is not None:
        prev, dep, direction = parent[node]  # type: ignore
        path.append((dep, direction, node))
        node = prev
    path.reverse()
    return path


def _build_signature(path: list[tuple[str, str, int]], doc: Doc, tok_a: Token, tok_b: Token) -> str:
    """Convert a dependency path into a compact pattern signature.

    Format: dash-joined dep labels from subject to object.
      - First element: subject's dep label (nsubj, nsubjpass, etc.)
      - Main verb tokens rendered as VERB (auxiliaries skipped)
      - Prepositions parameterized: prep:in, prep:by, prep:on
      - Other tokens use their ClearNLP dep label
      - Last element: object's dep label (dobj, pobj, attr, etc.)

    Examples:
      "Microsoft acquired GitHub"        -> nsubj-VERB-dobj
      "Qdrant is a vector database"      -> nsubj-VERB-attr
      "Qdrant is in Rust"                -> nsubj-VERB-prep:in-pobj
      "GitHub was acquired by Microsoft" -> nsubjpass-VERB-agent-pobj
      "Qdrant, a vector database, ..."   -> appos
    """
    if not path:
        return "appos"  # verbless (same token or direct appos)

    segments: list[str] = []

    # First element's dep label = subject's grammatical relation
    segments.append(path[0][0])

    # Render intermediate tokens (path elements whose token is not tok_b)
    for dep, direction, token_idx in path:
        tok = doc[token_idx]
        if tok.i == tok_b.i:
            continue  # object rendered separately
        if tok.i == tok_a.i:
            continue  # subject already rendered via first dep label
        # Skip auxiliary tokens — but NOT the root predicate.
        # In copular "X is Y", "is" has POS=AUX but dep=ROOT (it IS the predicate).
        # In passive "X was built", "was" has POS=AUX and dep=auxpass (skip it).
        is_aux = (tok.dep_ in ("aux", "auxpass", "cop") or
                  (tok.pos_ == "AUX" and tok.dep_ != "ROOT"))
        if is_aux:
            continue
        if tok.pos_ == "VERB" or tok.pos_ == "AUX":
            segments.append("VERB")
        elif tok.dep_ == "prep":
            # Prepositions parameterized with lemma: prep:in, prep:on, prep:by
            segments.append(f"prep:{tok.lemma_.lower()}")
        else:
            # Other tokens use their ClearNLP dep label (agent, dobj, etc.)
            segments.append(tok.dep_)

    # Last element: object's dep label (dobj, pobj, attr, etc.)
    segments.append(path[-1][0])

    return "-".join(segments)


# Auxiliary dep labels (ClearNLP/OntoNotes) — not the main predicate.
_AUX_DEPS = frozenset({"aux", "auxpass", "cop"})


def _extract_predicate_from_path(path: list[tuple[str, str, int]], doc: Doc) -> Token | None:
    """Find the main verb token on the dependency path (the predicate).

    Prefers non-auxiliary verbs (the semantic predicate). Falls back to
    the first auxiliary if no main verb exists on the path.
    """
    first_verb: Token | None = None
    for dep, direction, token_idx in path:
        tok = doc[token_idx]
        if tok.pos_ == "VERB" or tok.pos_ == "AUX":
            if first_verb is None:
                first_verb = tok
            # Prefer main verb (not aux/auxpass/cop)
            if tok.dep_ not in _AUX_DEPS:
                return tok
    if first_verb is not None:
        return first_verb
    # Fallback: first token in path
    if path:
        return doc[path[0][2]]
    return None


# ---------------------------------------------------------------------------
# DependencyMatcher high-precision patterns
# ---------------------------------------------------------------------------

_ACTIVE_TRANSITIVE_PATTERN = [
    {"RIGHT_ID": "verb", "RIGHT_ATTRS": {"POS": "VERB"}},
    {
        "LEFT_ID": "verb",
        "REL_OP": ">",
        "RIGHT_ID": "subject",
        "RIGHT_ATTRS": {"DEP": {"IN": ["nsubj", "nsubjpass"]}},
    },
    {
        "LEFT_ID": "verb",
        "REL_OP": ">",
        "RIGHT_ID": "object",
        "RIGHT_ATTRS": {"DEP": {"IN": ["dobj", "obj", "attr", "oprd"]}},
    },
]

_APPOS_PATTERN = [
    {"RIGHT_ID": "anchor", "RIGHT_ATTRS": {"POS": {"IN": ["PROPN", "NOUN"]}}},
    {
        "LEFT_ID": "anchor",
        "REL_OP": ">",
        "RIGHT_ID": "appositive",
        "RIGHT_ATTRS": {"DEP": "appos"},
    },
]

_POSS_PATTERN = [
    {"RIGHT_ID": "head", "RIGHT_ATTRS": {"POS": {"IN": ["PROPN", "NOUN"]}}},
    {
        "LEFT_ID": "head",
        "REL_OP": ">",
        "RIGHT_ID": "possessor",
        "RIGHT_ATTRS": {"DEP": "poss"},
    },
]


# ---------------------------------------------------------------------------
# Suppression helpers — false-edge prevention (precision guards)
# Every suppression increments a named counter. Nothing dropped silently.
# ---------------------------------------------------------------------------


def _is_in_attribution_context(pred_tok: Token) -> bool:
    """Detect if pred_tok is inside a ccomp/xcomp of an attribution verb.

    "Kahneman argues that X causes Y" → pred_tok='causes' is inside the
    ccomp of 'argues'. Relations inside attributed clauses are not asserted.
    """
    _load_config()
    # Walk up the dependency tree from pred_tok. If we hit a ccomp/xcomp
    # whose head is an attribution verb, suppress.
    current = pred_tok
    for _ in range(6):  # max depth guard
        if current.head.i == current.i:
            break  # reached root
        head = current.head
        # current is a ccomp/xcomp child of head
        if current.dep_ in ("ccomp", "xcomp"):
            if head.lemma_.lower() in _ATTRIBUTION_VERBS:
                return True
        current = head
    return False


def _is_in_conditional_clause(pred_tok: Token) -> bool:
    """Detect if pred_tok is inside a conditional/hypothetical clause.

    "If X fails, Y degrades" → 'fails' is inside advcl governed by mark='if'.
    Also catches: unless, when, should, were, suppose, assuming.
    """
    _load_config()
    current = pred_tok
    for _ in range(6):
        if current.head.i == current.i:
            break
        # Check if current is an advcl with a conditional marker
        if current.dep_ == "advcl":
            for child in current.children:
                if child.dep_ == "mark" and child.lemma_.lower() in _CONDITIONAL_MARKERS:
                    return True
        current = current.head
    # Also check pred_tok's own children for conditional mark
    for child in pred_tok.children:
        if child.dep_ == "mark" and child.lemma_.lower() in _CONDITIONAL_MARKERS:
            return True
    return False


def _is_contrast_subject(tok_a: Token, doc: Doc) -> bool:
    """Detect if tok_a is inside a contrast phrase.

    "Unlike Pinecone, Qdrant self-hosts" → Pinecone is inside prep:unlike.
    Entities in contrast phrases must NOT be bound as subject of main predicate.
    """
    _load_config()
    # Walk up from tok_a: if we're inside a prep whose lemma is a contrast prep
    current = tok_a
    for _ in range(5):
        if current.head.i == current.i:
            break
        head = current.head
        if head.dep_ == "prep" and head.lemma_.lower() in _CONTRAST_PREPS:
            return True
        # pobj of a contrast prep
        if current.dep_ == "pobj" and head.dep_ == "prep":
            if head.lemma_.lower() in _CONTRAST_PREPS:
                return True
        current = head
    # Check if tok_a is in a clause introduced by contrast conjunction
    # ("whereas X supports Y, Z does not")
    current = tok_a
    for _ in range(5):
        if current.head.i == current.i:
            break
        if current.dep_ == "advcl":
            for child in current.children:
                if child.dep_ == "mark" and child.lemma_.lower() in _CONTRAST_CONJS:
                    return True
        current = current.head
    return False


# Eventive noun suffixes: nominalizations derived from verbs.
# Light verb + eventive noun ("make use of", "give rise to") → suppress.
# Light verb + concrete noun ("have a engine") → let resolver handle.
_EVENTIVE_SUFFIXES = (
    "tion", "sion", "ment", "ance", "ence", "ing",
    "ure", "al", "ity", "ness", "sis",
)

# Known eventive nouns that don't match suffix patterns but are classic
# light-verb objects: "make use of", "take advantage of", "give way to".
_KNOWN_EVENTIVE_NOUNS = frozenset({
    "use", "advantage", "care", "note", "notice", "decision",
    "attempt", "contribution", "effort", "place", "room",
    "sure", "time", "way", "rise", "birth", "shape",
    "account", "charge", "effect", "part", "role",
})


def _is_eventive_noun(tok: Token) -> bool:
    """Heuristic: noun whose lemma ends in a nominalization suffix or is known."""
    lemma = tok.lemma_.lower()
    if lemma in _KNOWN_EVENTIVE_NOUNS:
        return True
    return any(lemma.endswith(s) for s in _EVENTIVE_SUFFIXES)


def _is_light_verb_construction(pred_tok: Token) -> bool:
    """Detect light verb constructions: "make use of", "give rise to".

    Light verb (make/take/give/do/have) + eventive noun dobj → the real
    predicate is the noun, not the verb. Suppress to avoid wrong mapping.
    Only fires when the dobj is an eventive noun (verbal nominalization),
    NOT for concrete entities ("have a recommendation engine" → has_part).
    """
    _load_config()
    if pred_tok.lemma_.lower() not in _LIGHT_VERBS:
        return False
    # Check if pred_tok has a dobj/obj child that is an eventive noun
    for child in pred_tok.children:
        if child.dep_ in ("dobj", "obj", "attr") and child.pos_ in ("NOUN", "PROPN"):
            if _is_eventive_noun(child):
                return True
    return False


def _is_verbless_sentence(sent) -> bool:
    """Sentence has no VERB/AUX token → heading/fragment, skip extraction.

    Checks ALL tokens (not just root) because en_core_web_sm occasionally
    mislabels the root POS (e.g. 'lowers' as NOUN). If any token is VERB/AUX,
    the sentence has verbal structure worth extracting from.
    """
    for tok in sent:
        if tok.pos_ in ("VERB", "AUX"):
            return False
    return True


def _is_expletive_subject(tok_a: Token) -> bool:
    """Sentence has expletive 'there' as subject → no real subject.

    Checks the sentence containing tok_a: if the sentence root has
    an 'expl' child with lemma 'there', the sentence is existential.
    """
    sent = tok_a.sent
    root = sent.root
    for child in root.children:
        if child.dep_ == "expl" and child.lemma_.lower() == "there":
            return True
    # Also check if tok_a itself is the expletive
    if tok_a.lemma_.lower() == "there" and tok_a.dep_ in ("nsubj", "expl"):
        return True
    return False


def _is_agentless_passive(pred_tok: Token, tok_a: Token) -> bool:
    """Passive without agent: nsubjpass present, no agent/pobj → partial edge."""
    if tok_a.dep_ != "nsubjpass":
        return False
    # Check if the predicate has an agent child
    for child in pred_tok.children:
        if child.dep_ == "agent":
            return False  # has agent, not agentless
    # Also check for prep:by attached to predicate
    for child in pred_tok.children:
        if child.dep_ == "prep" and child.lemma_.lower() == "by":
            return False  # has by-phrase
    return True  # nsubjpass with no agent → suppress


def _is_low_parse_confidence(doc: Doc) -> bool:
    """Ratio of sentence-final punctuation to token count below threshold.

    ASR transcripts lack reliable punctuation → sentence segmentation
    collapses → dep tree is unreliable. Skip relation extraction.
    """
    _load_config()
    n_tokens = len(doc)
    if n_tokens == 0:
        return True
    n_sent_final = sum(1 for tok in doc if tok.text in ".!?")
    ratio = n_sent_final / n_tokens
    return ratio < _LOW_PARSE_THRESHOLD


def _inc(counter: dict[str, int] | None, key: str) -> None:
    """Increment a named suppression counter (never drop silently)."""
    if counter is not None:
        counter[key] = counter.get(key, 0) + 1


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------


class DepPathExtractor:
    """Derives relations from spaCy dependency paths between entity heads.

    Usage:
        extractor = DepPathExtractor()
        triples = extractor.extract(
            text="Microsoft acquired GitHub in 2018.",
            entities=[
                EntitySpan("Microsoft", 0, 9, "ORG"),
                EntitySpan("GitHub", 19, 25, "ORG"),
            ],
            chunk_id="abc_001",
            doc_id="doc_xyz",
        )
    """

    def __init__(self, model_name: str = "en_core_web_sm"):
        # Use the shared nlp singleton to avoid duplicate model loads.
        # Falls back to direct load if the shared module isn't available.
        try:
            from services.extraction.appos_enrichment import get_shared_nlp
            self._nlp = get_shared_nlp()
        except (ImportError, Exception):
            self._nlp = spacy.load(model_name, disable=["ner", "textcat"])
        self._matcher = DependencyMatcher(self._nlp.vocab)
        self._matcher.add("ACTIVE_TRANSITIVE", [_ACTIVE_TRANSITIVE_PATTERN])
        self._matcher.add("APPOS", [_APPOS_PATTERN])
        self._matcher.add("POSS", [_POSS_PATTERN])
        self._pattern_matches: set[tuple[int, int]] = set()

    def extract(
        self,
        text: str,
        entities: list[EntitySpan],
        *,
        section_path: str = "",
        chunk_id: str = "",
        doc_id: str = "",
        doc: Doc | None = None,
        suppression_counters: dict[str, int] | None = None,
    ) -> list[ExtractedTriple]:
        """Extract all relation triples from text given entity spans.

        Args:
            doc: pre-parsed spaCy Doc (avoids re-parsing when shared with Stage B).
            suppression_counters: per-chunk dict for named suppression counters.
                Every suppression rule increments a key here. Never drop silently.
        """
        if not text.strip() or len(entities) < 2:
            return []

        if doc is None:
            doc = self._nlp(text)

        # --- SENTENCE-LEVEL SUPPRESSION (before entity-pair loop) ---

        # Low-parse-confidence guard: ASR transcripts, unpunctuated text
        if _is_low_parse_confidence(doc):
            _inc(suppression_counters, "skipped_low_parse_confidence")
            return []

        # Verbless fragment guard: headings, bullets, list items
        # If ALL sentences are verbless, skip the entire chunk.
        sents = list(doc.sents)
        has_verbal_sent = any(not _is_verbless_sentence(s) for s in sents)
        if not has_verbal_sent:
            _inc(suppression_counters, "skipped_verbless")
            return []

        triples: list[ExtractedTriple] = []

        # Run DependencyMatcher for high-precision patterns
        pattern_pairs = self._run_matcher(doc, entities)

        # For each entity pair, extract relation
        for i, ent_a in enumerate(entities):
            for j, ent_b in enumerate(entities):
                if i == j:
                    continue
                # Avoid duplicate pairs (only extract A->B, not B->A)
                if ent_a.start_char > ent_b.start_char:
                    continue

                tok_a = _find_head_token(doc, ent_a.start_char, ent_a.end_char)
                tok_b = _find_head_token(doc, ent_b.start_char, ent_b.end_char)
                if tok_a is None or tok_b is None:
                    continue
                if tok_a.i == tok_b.i:
                    continue

                # --- PAIR-LEVEL SUPPRESSION (before path extraction) ---

                # Expletive subject: "There is a tradeoff between X and Y"
                if _is_expletive_subject(tok_a):
                    _inc(suppression_counters, "suppressed_expletive")
                    continue

                # Contrast guard: "Unlike Pinecone, Qdrant self-hosts"
                # Entity inside contrast phrase must not be subject.
                if _is_contrast_subject(tok_a, doc):
                    _inc(suppression_counters, "suppressed_contrast")
                    continue

                # Determine confidence tier
                pair_key = (min(tok_a.i, tok_b.i), max(tok_a.i, tok_b.i))
                is_pattern_matched = pair_key in pattern_pairs

                # Extract dependency path
                path = _shortest_dep_path(doc, tok_a, tok_b)
                if not path:
                    continue

                # Noun-anchored possessive: "Google's TensorFlow" → owns
                # Check BEFORE verb extraction — poss paths have no verb.
                poss_result = _resolve_possessive(path, doc, tok_a, tok_b, ent_a, ent_b)
                if poss_result is not None:
                    triples.append(poss_result)
                    continue

                # Find predicate
                pred_tok = _extract_predicate_from_path(path, doc)
                if pred_tok is None:
                    continue

                # --- PREDICATE-LEVEL SUPPRESSION ---

                # Verbless sentence: skip pairs whose predicate sentence is a fragment
                if _is_verbless_sentence(pred_tok.sent):
                    _inc(suppression_counters, "skipped_verbless")
                    continue

                # Agentless passive: "GitHub was acquired" (no agent) → partial edge
                if _is_agentless_passive(pred_tok, tok_a):
                    _inc(suppression_counters, "suppressed_agentless_passive")
                    continue

                # Light verb construction: "make use of", "give rise to"
                if _is_light_verb_construction(pred_tok):
                    _inc(suppression_counters, "suppressed_light_verb")
                    continue

                # Detect attribution/conditional (carry as fields, don't suppress)
                _is_attributed = _is_in_attribution_context(pred_tok)
                _is_conditional = _is_in_conditional_clause(pred_tok)
                if _is_attributed:
                    _inc(suppression_counters, "qualified_attributed")
                if _is_conditional:
                    _inc(suppression_counters, "qualified_conditional")

                # Build signature
                signature = _build_signature(path, doc, tok_a, tok_b)

                # --- STRUCTURAL GUARDS (precision: single-clause constraint) ---

                # 1a. Single-clause constraint: reject candidates whose dep path
                # crosses more than one predicate node (VERB or ROOT rendered).
                # Validated: all 7 gate TPs have exactly 1 predicate node.
                _pred_nodes = sum(
                    1 for seg in signature.split("-")
                    if seg in ("VERB", "ROOT")
                )
                if _pred_nodes > 1:
                    _inc(suppression_counters, "suppressed_multi_clause")
                    continue

                # 1b. Conjunct-crossing guard: reject paths that traverse a
                # coordinating conjunction (cc) or land on a conjunct (conj).
                _sig_segs = signature.split("-")
                if "cc" in _sig_segs or "conj" in _sig_segs:
                    _inc(suppression_counters, "suppressed_conjunct_crossing")
                    continue

                # 1c. Exception boundary: any path traversing prep:except or
                # pcomp of an exception clause is not an asserted relation.
                if "prep:except" in signature or "pcomp" in signature:
                    _inc(suppression_counters, "suppressed_exception_boundary")
                    continue

                # Resolve predicate via 4-tier structural composite resolver
                lemma = pred_tok.lemma_.lower()
                resolved = resolve_predicate(
                    signature=signature,
                    lemma=lemma,
                    subject_type=ent_a.entity_type,
                    object_type=ent_b.entity_type,
                    pred_tok=pred_tok,
                    object_tok=tok_b,
                )
                if resolved is None:
                    continue  # T4: drop — cannot name the predicate

                normalized, swap = resolved

                # Apply swap: grammatical subject is semantic object
                # (passive/agent constructions: "X was built by Y")
                if swap:
                    subj_ent, obj_ent = ent_b, ent_a
                    subj_tok, obj_tok = tok_b, tok_a
                else:
                    subj_ent, obj_ent = ent_a, ent_b
                    subj_tok, obj_tok = tok_a, tok_b

                # allowed_pairs gate: reject invalid type combinations.
                # Count rejections by reason; never drop silently.
                if not pair_allowed(normalized, subj_ent.entity_type, obj_ent.entity_type):
                    reason = f"disallowed_pair:{normalized}:{subj_ent.entity_type}:{obj_ent.entity_type}"
                    rejection_counters[reason] = rejection_counters.get(reason, 0) + 1
                    continue

                # Determine sentence
                sent = pred_tok.sent
                sent_idx = 0
                for idx, s in enumerate(doc.sents):
                    if s.start <= pred_tok.i < s.end:
                        sent_idx = idx
                        break

                # Extract qualifiers (inline for performance)
                polarity, modality, temporal = _extract_qualifiers_inline(pred_tok)

                # Determine assertion_mode from structural context
                if _is_attributed:
                    assertion_mode = "attributed"
                elif _is_conditional:
                    assertion_mode = "conditional"
                else:
                    assertion_mode = "direct"

                # Diagnostic counters (not suppression — candidate is emitted)
                if polarity == "NEGATIVE":
                    _inc(suppression_counters, "qualified_negated")
                if modality != "ASSERTED":
                    _inc(suppression_counters, "qualified_modal")

                confidence = 1.0 if is_pattern_matched else 0.9

                triple = ExtractedTriple(
                    subject_surface=subj_ent.surface,
                    subject_start=subj_ent.start_char,
                    subject_end=subj_ent.end_char,
                    predicate=normalized,
                    predicate_lemma=lemma,
                    predicate_surface=pred_tok.text,
                    object_surface=obj_ent.surface,
                    object_start=obj_ent.start_char,
                    object_end=obj_ent.end_char,
                    confidence=confidence,
                    dep_signature=signature,
                    polarity=polarity,
                    modality=modality,
                    assertion_mode=assertion_mode,
                    temporal_cue=temporal,
                    sentence_text=sent.text.strip(),
                    sentence_idx=sent_idx,
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                    section_path=section_path,
                )
                triples.append(triple)

        return triples

    def _run_matcher(self, doc: Doc, entities: list[EntitySpan]) -> set[tuple[int, int]]:
        """Run DependencyMatcher and return matched token pairs."""
        pairs: set[tuple[int, int]] = set()
        try:
            matches = self._matcher(doc)
            for match_id, token_ids in matches:
                if len(token_ids) >= 2:
                    # Record all pairwise combinations
                    for a in range(len(token_ids)):
                        for b in range(a + 1, len(token_ids)):
                            pairs.add((min(token_ids[a], token_ids[b]), max(token_ids[a], token_ids[b])))
        except Exception as exc:
            logger.debug("DependencyMatcher error: %s", exc)
        return pairs


# ---------------------------------------------------------------------------
# Inline qualifier extraction (avoids circular import)
# ---------------------------------------------------------------------------

_MODAL_LEMMAS = {"can", "could", "may", "might", "shall", "should", "will", "would", "must"}
_CONDITIONAL_LEMMAS = {"if", "unless", "provided", "assuming"}
_TEMPORAL_PREPS = {"in", "on", "at", "during", "before", "after", "since", "until", "by"}


def _extract_qualifiers_inline(pred_tok: Token) -> tuple[str, str, str | None]:
    """Extract polarity, modality, and temporal cue from predicate subtree."""
    polarity = "POSITIVE"
    modality = "ASSERTED"
    temporal: str | None = None

    for child in pred_tok.children:
        # Negation
        if child.dep_ == "neg":
            polarity = "NEGATIVE"
        # Modality
        elif child.dep_ == "aux" and child.lemma_.lower() in _MODAL_LEMMAS:
            modality = "HYPOTHETICAL"
        elif child.dep_ == "mark" and child.lemma_.lower() in _CONDITIONAL_LEMMAS:
            modality = "CONDITIONAL"
        # Temporal
        elif child.dep_ == "prep" and child.text.lower() in _TEMPORAL_PREPS:
            # Grab the temporal noun phrase
            temporal_parts = [child.text]
            for grandchild in child.children:
                if grandchild.dep_ in ("pobj", "pcomp", "nummod"):
                    temporal_parts.append(grandchild.text)
            temporal = " ".join(temporal_parts)

    # Check for negation via "not" as a separate token
    if polarity == "POSITIVE":
        for child in pred_tok.children:
            if child.text.lower() == "not" or child.dep_ == "neg":
                polarity = "NEGATIVE"
                break

    return polarity, modality, temporal
