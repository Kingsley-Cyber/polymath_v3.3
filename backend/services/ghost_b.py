"""
GHOST B — Entity Extraction (Phase 4)

Runs when ingestion_config.use_neo4j is True.
Extracts entities and relations from child chunks via LiteLLM. temperature=0.
Bounded by EXTRACTION_MAX_CONCURRENT semaphore.

Called by the ingestion worker AFTER tier chunking and stable ID assignment.
Returns ExtractionResult list — worker passes to neo4j_writer.write_document_graph().

Extraction target: child text ONLY. Never parent body, never summary strings.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, ClassVar, Literal

import httpx
import tiktoken

from config import get_settings
from services.extraction_provider_cards import (
    provider_payload_defaults,
    resolve_extraction_provider_card,
)
from services.llm_lane_pool import (
    FatalLaneError,
    SOFT_FATAL_DISABLE_STRIKES,
    provider_error_tier,
    provider_error_summary,
)

# Phase 14.2 — pluggable schema retriever. Worker injects a closure over qdrant_client +
# corpus_id so this module stays independent of the Qdrant SDK.
#   args: (kind, query_vec, top_k)
#   returns: list of allowed terms ranked by similarity
SchemaResolver = Callable[[str, list[float], int], Awaitable[list[str]]]
GhostBAuditSink = Callable[[dict[str, Any]], Awaitable[None]]
ExtractionRoutingPolicy = Literal["work_stealing", "balanced", "primary_fallback"]
_BALANCED_ROUTE_OFFSETS: dict[tuple[str, ...], int] = {}

logger = logging.getLogger(__name__)
_TOKENIZER = tiktoken.get_encoding("cl100k_base")
_GLOBAL_EXTRACTION_SEMAPHORE: asyncio.Semaphore | None = None
_GLOBAL_EXTRACTION_SEMAPHORE_LIMIT: int | None = None
_GLOBAL_EXTRACTION_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None
_MODEL_LANE_SEMAPHORES: dict[tuple[int, str, str], asyncio.Semaphore] = {}
_MODEL_LANE_SEMAPHORE_LIMITS: dict[tuple[int, str, str], int] = {}
_MODEL_LANE_SEMAPHORE_LOOPS: dict[tuple[int, str, str], asyncio.AbstractEventLoop] = {}

_SYSTEM = (
    "You are a precise entity, relation, and fact extractor. "
    "Output EXACTLY one JSON object per line. "
    "Do NOT output any other text, code fences, explanations, preambles, or postambles. "
    "Extract only what is explicitly stated in the text. "
    "Do not hallucinate entities, relations, or facts."
)
_JSON_OBJECT_SYSTEM = (
    "You are a precise entity, relation, and fact extractor. "
    "Output EXACTLY one valid JSON object. "
    "Do NOT output JSONL, code fences, explanations, preambles, or postambles. "
    "Extract only what is explicitly stated in the text. "
    "Do not hallucinate entities, relations, or facts."
)

# Default open-vocabulary enums when no schema is provided.
_DEFAULT_ENTITY_TYPES = ["person", "org", "concept", "other"]
FACT_TYPES: tuple[str, ...] = (
    "property",
    "status",
    "timestamp",
    "quantity",        # Pt 8d — numeric with unit ($100, 5kg, 30%, 2.3M)
    "threshold",
    "category",
    "tag",
    "rule_condition",
    "rule_action",
)


# ── Universal schema (baked, replaces per-corpus tuning) ───────────────────
# Each vocab list stays under SCHEMA_INLINE_LIMIT (30) so the ghost_b prompt
# never needs per-chunk Qdrant retrieval — the full vocab fits inline every time.
# See GOTCHAS entry on the universal schema for the ruleset.

UNIVERSAL_ENTITY_SCHEMA: list[str] = [
    "Person",         # individuals: authors, employees, historical figures, characters
    "Organization",   # companies, institutions, teams, units, departments
    "Location",       # geographic places, facilities, addresses, regions
    "Event",          # conferences, incidents, meetings, milestones, operations
    "Concept",        # theories, ideas, principles, abstract topics
    "Method",         # techniques, algorithms, procedures, approaches, therapies
    "Product",        # commercial goods, hardware, non-software services
    "Software",       # libraries, frameworks, apps, APIs, languages, platforms, engines
    "Document",       # papers, books, reports, articles, messages
    "Standard",       # protocols, specifications, data formats, schemas (JSON, HTTP, OpenAPI)
    "Rule",           # SOPs, policies, guidelines, protocols (prescriptive, no legal force)
    "Law",            # statutes, regulations, case citations, treaties, UCMJ / HIPAA / GDPR
    "Artifact",       # tangible objects, equipment, tools, weapons, code snippets, datasets
    "TimeReference",  # dates, periods, deadlines, durations
]

UNIVERSAL_RELATION_SCHEMA: list[str] = [
    # Structural
    "part_of",
    "member_of",
    "located_in",
    "works_for",
    "created_by",
    "owns",
    "affiliated_with",
    # Canonicalization (entity-merging predicates — let the LLM emit synonyms
    # and instance-of edges so the graph can collapse surface variants and
    # build type hierarchies. Tracked separately from operational relations
    # in case future versions tier them out of EXTRACTION_MAX_RELATIONS_PER_CHUNK.)
    "synonym_of",
    "instance_of",
    # Operational
    "uses",          # absorbs the previous `calls` predicate
    "runs_on",       # runtime / deployment substrate
    "trained_on",    # training data or corpus used by model/method
    "references",
    "implements",
    "depends_on",
    "produces",
    "stores",
    "detects",       # absorbs the previous `extracts` + `classifies` predicates
    "supports",
    # Referential / definitional / transformation
    "defines",       # Pt 8d — "X defines Y" — definitions, glossaries, formal specs
    "represents",
    "maps_to",
    # Temporal
    "preceded_by",
    "causes",
    "overlaps",      # temporal co-occurrence / containment (Event/TimeReference overlap)
    # Provenance / conflict
    "derived_from",
    "contradicts",
    "excepts",
    "overrides",
    # Sentinel — MUST stay last; rendered with a [FALLBACK] tag in the prompt.
    "related_to",
]
# Total = 30 predicates including sentinel. Stays at SCHEMA_INLINE_LIMIT (30).
# resolve_chunk_vocab inlines the full list when len <= limit; bumping the
# count above 30 flips ghost_b into per-chunk Qdrant retrieval mode (see
# GOTCHAS §42), which degrades on fresh ingest where chunk vectors don't
# yet exist when ghost_b runs.

# Phase I — Claude-authored disambiguating glosses injected into the extraction
# prompt's vocabulary constraint block. Each ≤ 8 words, designed to separate
# sibling types/predicates that LLMs routinely confuse (Concept vs Method,
# Rule vs Law, uses vs depends_on, contradicts vs overrides vs excepts, etc.).
# Not consumed by any migration / comparison / storage code — purely an
# LLM-facing prompt augmentation.
UNIVERSAL_ENTITY_GLOSSES: dict[str, str] = {
    # Glosses are intentionally short and free of commas / nested parens.
    # The constraint block renders these as `Name=gloss` with `|` separators
    # (no spaces). LLMs occasionally treat embedded commas inside parens as
    # separator boundaries; ultra-tight glosses sidestep that failure mode
    # and shrink the per-chunk extraction prompt by ~75%.
    #
    # Pt9g — Every type in UNIVERSAL_ENTITY_SCHEMA MUST have a gloss here.
    # `_render_vocab_line` falls back to bare-name rendering for missing
    # entries, which leaves the new type undefined relative to its
    # neighbors and lets the LLM's training prior dominate. Pt9a added
    # `Software` and `Standard` to the schema/Literal but forgot to add
    # glosses here, so every software-flavored entity (TensorFlow, React,
    # ML Kit) routed to `Product=built offering` instead of `Software`
    # for three consecutive ingest cycles. Future additions: don't
    # repeat that. The schema/Literal/gloss triple is the contract.
    "Person":        "human individual",
    "Organization":  "formal group",
    "Location":      "physical place",
    "Event":         "bounded occurrence",
    "Concept":       "abstract idea not a procedure",
    "Method":        "executable procedure",
    "Product":       "built offering not Software",
    "Software":      "library framework runtime API or language",
    "Document":      "authored writing",
    "Standard":      "protocol specification format or schema",
    "Rule":          "non-legal guideline",
    "Law":           "binding statute",
    "Artifact":      "tangible object not a Product",
    "TimeReference": "specific date or duration",
    "other":         "fallback",
}

UNIVERSAL_RELATION_GLOSSES: dict[str, str] = {
    "part_of":         "X subcomponent of Y",
    "member_of":       "X in group Y",
    "located_in":      "X inside place Y",
    "works_for":       "X employed by Y",
    "created_by":      "Y authored X",
    "owns":            "X owns/holds title to Y",
    "affiliated_with": "X loosely tied to Y not member",
    "synonym_of":      "X same entity as Y",
    "instance_of":     "X is a Y subclass or kind",
    "uses":            "X consumes or invokes Y",
    "runs_on":         "X executes on Y",
    "trained_on":      "X learns from Y",
    "references":      "X cites Y",
    "implements":      "X concrete form of Y",
    "depends_on":      "X needs Y",
    "produces":        "X outputs Y",
    "stores":          "X persists Y",
    "detects":         "X identifies or pulls Y from data",
    "supports":        "X enables Y",
    "defines":         "X gives meaning of Y",
    "represents":      "X models Y",
    "maps_to":         "X transforms to Y",
    "preceded_by":     "X follows Y in time",
    "causes":          "X leads to Y",
    "overlaps":        "X co-occurs in time with Y",
    "derived_from":    "X evolved from Y",
    "contradicts":     "X conflicts with Y",
    "excepts":         "X carveout from Y",
    "overrides":       "X supersedes Y",
    "related_to":      "use only when no specific predicate fits",
}

DOMAIN_RANGE_MAP: dict[str, dict[str, list[str]]] = {
    "part_of": {
        "subject_types": ["Artifact", "Concept", "Document", "Organization", "Product", "Rule", "Law"],
        "object_types": ["Artifact", "Concept", "Document", "Organization", "Product", "Rule", "Law"],
    },
    "member_of": {
        "subject_types": ["Person", "Organization"],
        "object_types": ["Organization"],
    },
    "located_in": {
        "subject_types": ["Person", "Organization", "Event", "Artifact", "Product"],
        "object_types": ["Location"],
    },
    "works_for": {
        "subject_types": ["Person"],
        "object_types": ["Organization"],
    },
    "created_by": {
        "subject_types": [
            "Artifact",
            "Concept",
            "Document",
            "Method",
            "Organization",
            "Product",
            "Software",
            "Standard",
            "Rule",
            "Law",
        ],
        "object_types": ["Person", "Organization"],
    },
    "owns": {
        "subject_types": ["Person", "Organization"],
        "object_types": ["Artifact", "Document", "Organization", "Product", "Location"],
    },
    "affiliated_with": {
        "subject_types": ["Person", "Organization"],
        "object_types": ["Organization", "Event", "Concept", "Product"],
    },
    "synonym_of": {
        # Canonicalization edge — same surface variant of any entity. Allowed
        # over every entity type so the LLM can merge "OpenAI" / "openai inc"
        # or "PyTorch" / "torch" regardless of typing.
        "subject_types": [
            "Person", "Organization", "Location", "Event", "Concept", "Method",
            "Product", "Document", "Rule", "Law", "Artifact", "TimeReference",
        ],
        "object_types": [
            "Person", "Organization", "Location", "Event", "Concept", "Method",
            "Product", "Document", "Rule", "Law", "Artifact", "TimeReference",
        ],
    },
    "instance_of": {
        # X is a kind/subtype of Y. Restricted to the type-hierarchy axes
        # that come up in practice (concept/method/product taxonomies, etc.).
        "subject_types": [
            "Concept", "Method", "Product", "Artifact", "Document",
            "Rule", "Law", "Organization", "Event",
        ],
        "object_types": [
            "Concept", "Method", "Product", "Artifact", "Document",
            "Rule", "Law", "Organization", "Event",
        ],
    },
    "uses": {
        # Absorbs the previous `calls` predicate; subject set widened to
        # include the API-invocation actors that used to live under `calls`.
        "subject_types": ["Person", "Organization", "Method", "Product", "Artifact"],
        "object_types": ["Artifact", "Product", "Method", "Concept", "Document", "Organization"],
    },
    "references": {
        "subject_types": ["Concept", "Document", "Method", "Product", "Rule", "Law"],
        "object_types": ["Concept", "Document", "Method", "Person", "Organization", "Rule", "Law"],
    },
    "implements": {
        "subject_types": ["Method", "Product", "Artifact", "Organization", "Rule", "Law"],
        "object_types": ["Concept", "Method", "Rule", "Law"],
    },
    "depends_on": {
        "subject_types": ["Artifact", "Concept", "Document", "Method", "Product", "Rule", "Law"],
        "object_types": ["Artifact", "Concept", "Document", "Method", "Product", "Rule", "Law"],
    },
    "produces": {
        "subject_types": ["Person", "Organization", "Method", "Product", "Artifact", "Event"],
        "object_types": ["Artifact", "Concept", "Document", "Event", "Method", "Product"],
    },
    "stores": {
        "subject_types": ["Artifact", "Method", "Organization", "Product"],
        "object_types": ["Artifact", "Concept", "Document", "Product"],
    },
    "detects": {
        # Absorbs the previous `extracts` predicate — both have identical
        # subject/object ranges in practice (an algorithm/method/system pulling
        # or recognizing a target), and keeping them separate produced
        # inconsistent labels for the same edge across chunks.
        "subject_types": ["Artifact", "Method", "Organization", "Product"],
        "object_types": ["Artifact", "Concept", "Document", "Event", "Location", "Organization", "Person", "Product"],
    },
    "classifies": {
        "subject_types": ["Artifact", "Method", "Organization", "Product"],
        "object_types": ["Artifact", "Concept", "Document", "Event", "Organization", "Person", "Product"],
    },
    "runs_on": {
        "subject_types": ["Artifact", "Method", "Product"],
        "object_types": ["Artifact", "Concept", "Location", "Organization", "Product"],
    },
    "trained_on": {
        "subject_types": ["Artifact", "Method", "Product"],
        "object_types": ["Artifact", "Concept", "Document", "Product"],
    },
    "supports": {
        "subject_types": ["Artifact", "Method", "Organization", "Product"],
        "object_types": ["Artifact", "Concept", "Document", "Method", "Product", "Rule"],
    },
    "represents": {
        "subject_types": ["Artifact", "Concept", "Document", "Method", "Product"],
        "object_types": ["Artifact", "Concept", "Document", "Event", "Method", "Organization", "Person", "Product"],
    },
    "maps_to": {
        "subject_types": ["Artifact", "Concept", "Document", "Method", "Product"],
        "object_types": ["Artifact", "Concept", "Document", "Method", "Product"],
    },
    "preceded_by": {
        "subject_types": ["Event", "TimeReference", "Document", "Concept", "Method", "Person", "Organization"],
        "object_types": ["Event", "TimeReference", "Document", "Concept", "Method", "Person", "Organization"],
    },
    "causes": {
        # TimeReference added so dated triggers ("the 2020 ruling caused …")
        # can sit on either side; without it, every temporally-qualified
        # cause/effect was forced into the related_to sentinel.
        "subject_types": ["Event", "Concept", "Method", "Rule", "Law", "TimeReference"],
        "object_types": ["Event", "Concept", "Method", "Rule", "Law", "TimeReference"],
    },
    "overlaps": {
        # Temporal co-occurrence between events / dates / periods.
        "subject_types": ["Event", "TimeReference", "Concept"],
        "object_types": ["Event", "TimeReference", "Concept"],
    },
    "derived_from": {
        "subject_types": ["Artifact", "Concept", "Document", "Method", "Product", "Rule", "Law"],
        "object_types": ["Artifact", "Concept", "Document", "Method", "Product", "Rule", "Law"],
    },
    "contradicts": {
        "subject_types": ["Concept", "Document", "Method", "Rule", "Law"],
        "object_types": ["Concept", "Document", "Method", "Rule", "Law"],
    },
    "excepts": {
        "subject_types": ["Rule", "Law", "Document"],
        "object_types": ["Rule", "Law", "Document", "Concept"],
    },
    "overrides": {
        "subject_types": ["Rule", "Law", "Document"],
        "object_types": ["Rule", "Law", "Document"],
    },
}

RELATION_ALIAS_MAP: dict[str, tuple[str, bool]] = {
    # value = (approved_predicate, reverse_subject_object)
    "contains": ("part_of", True),
    "includes": ("part_of", True),
    "composed_of": ("part_of", True),
    "has_part": ("part_of", True),
    "belongs_to": ("member_of", False),
    "employed_by": ("works_for", False),
    "authored_by": ("created_by", False),
    "built_by": ("created_by", False),
    "developed_by": ("created_by", False),
    "created": ("created_by", True),
    "uses": ("uses", False),
    "using": ("uses", False),
    "utilizes": ("uses", False),
    "consumes": ("uses", False),
    "used_by": ("uses", True),
    "used_for": ("uses", False),
    "reads": ("uses", False),
    "read_by": ("uses", True),
    "writes": ("stores", False),
    "stored_in": ("stores", True),
    "saved_in": ("stores", True),
    "persisted_in": ("stores", True),
    # `calls` was collapsed into `uses` — these aliases route to the surviving
    # predicate so legacy LLM emissions still normalize cleanly.
    "calls": ("uses", False),
    "invokes": ("uses", False),
    "queries": ("uses", False),
    "called_by": ("uses", True),
    "requires": ("depends_on", False),
    "needs": ("depends_on", False),
    "depends": ("depends_on", False),
    "outputs": ("produces", False),
    "generates": ("produces", False),
    "creates": ("produces", False),
    "emits": ("produces", False),
    "returns": ("produces", False),
    "produced_by": ("produces", True),
    "generated_by": ("produces", True),
    "provides": ("supports", False),
    "enables": ("supports", False),
    "allows": ("supports", False),
    "facilitates": ("supports", False),
    "runs_on": ("runs_on", False),
    "deployed_on": ("runs_on", False),
    "executes_on": ("runs_on", False),
    "trained_on": ("trained_on", False),
    "trained_with": ("trained_on", False),
    # `extracts` was merged into `detects`; both legacy aliases route to the
    # survivor so old prompts and historical snapshots still normalize.
    "extract": ("detects", False),
    "extracts": ("detects", False),
    "extracted_from": ("detects", False),
    "pulls": ("detects", False),
    "identifies": ("detects", False),
    "recognizes": ("detects", False),
    # New canonicalization / typing predicates — common LLM verbs map in.
    "is_a": ("instance_of", False),
    "is_an": ("instance_of", False),
    "kind_of": ("instance_of", False),
    "type_of": ("instance_of", False),
    "subclass_of": ("instance_of", False),
    "subtype_of": ("instance_of", False),
    "same_as": ("synonym_of", False),
    "alias_of": ("synonym_of", False),
    "aka": ("synonym_of", False),
    "also_known_as": ("synonym_of", False),
    "owns": ("owns", False),
    "owned_by": ("owns", True),
    "holds": ("owns", False),
    "affiliated": ("affiliated_with", False),
    "associated_with": ("affiliated_with", False),
    "partner_of": ("affiliated_with", False),
    "sponsored_by": ("affiliated_with", False),
    "co_occurs_with": ("overlaps", False),
    "concurrent_with": ("overlaps", False),
    "during": ("overlaps", False),
    "within_timespan_of": ("overlaps", False),
    "throughout": ("overlaps", False),
    # Pt 8d — `classifies` dropped; route prediction-/labeling-style verbs
    # to `detects` which has identical semantic flavor (model produces
    # category).
    "predicts": ("detects", False),
    "labels": ("detects", False),
    "classifies": ("detects", False),
    "categorizes": ("detects", False),
    "models": ("represents", False),
    "encodes": ("represents", False),
    "converts": ("maps_to", False),
    "transforms": ("maps_to", False),
    "translates": ("maps_to", False),
    "maps_onto": ("maps_to", False),
    "discusses": ("references", False),
    "covers": ("references", False),
    "teaches": ("references", False),
    "defines": ("defines", False),
    "shows": ("references", False),
    "demonstrates": ("references", False),
    "based_on": ("derived_from", False),
    "inspired_by": ("derived_from", False),
    "built_on": ("derived_from", False),
    "prevents": ("contradicts", False),
    "replaces": ("overrides", False),
    "supersedes": ("overrides", False),
    # Pt 8a — audit-identified LLM-emitted predicates that previously got
    # demoted to `related_to` because they were outside the 30-slot schema.
    # Aliasing recovers the relational intent without expanding the schema
    # (expansion would break the inline-vocab path per GOTCHAS §42 since the
    # cap is exactly 30 with the sentinel). Mapping rationale:
    #   measures      → detects     (sensor measures = detects values)
    #   studies       → references  (a book studies/cites a topic)
    #   correlates_with → overlaps  (statistical / temporal co-occurrence)
    #   similar_to    → derived_from (X similar to Y ≈ X based on Y)
    #   influences    → causes      (partial / soft causation)
    #   affects       → causes      (induces change)
    #   reduces       → causes      (directional causation; sign loss is acceptable)
    #   published_by  → created_by  (work created by publisher)
    "measures": ("detects", False),
    "studies": ("references", False),
    "correlates_with": ("overlaps", False),
    "similar_to": ("derived_from", False),
    "influences": ("causes", False),
    "affects": ("causes", False),
    "reduces": ("causes", False),
    "published_by": ("created_by", False),
    # Definition/example aliases. Reverse-direction entries (rev=True) flip
    # subject/object because "Y defined_in X" reads as "X defines Y".
    "describes": ("defines", False),
    "specifies": ("defines", False),
    "denotes": ("defines", False),
    "means": ("defines", False),
    "defined_in": ("defines", True),
    "defined_by": ("defines", True),
    "explained_in": ("defines", True),
    "example_of": ("instance_of", False),
    "exemplifies": ("instance_of", False),
    "case_of": ("instance_of", False),
    "illustrates": ("instance_of", False),
    "demonstrates_case": ("instance_of", False),
    "illustrated_in": ("references", True),
    # Pt 8d — high-frequency off-schema predicates seen in the existing
    # graph (audit Pt 7c finding). Route to closest semantic-family
    # canonical so future ingestions canonicalize them instead of
    # demoting to the `related_to` sentinel.
    "tests": ("uses", False),
    "parameter_of": ("part_of", False),
    "equivalent_to": ("synonym_of", False),
    "activates": ("uses", False),
    "experiences": ("causes", True),
}

# Whitespace collapse for the Phase B evidence gate. Both the chunk text and
# the model-supplied evidence_phrase are normalized through this regex before
# the substring check so a paraphrase like "trained on  the corpus\n" still
# matches "trained on the corpus".
_WHITESPACE_RE = re.compile(r"\s+")

# Pt 8c — semantic-empty Unicode characters that break substring match
# but carry no meaning: soft hyphens (frontmatter hyphenation),
# middle-dots / hyphenation points (dictionary syllabification like
# `at·om·ic`), zero-width joiners (some PDF→markdown converters insert
# these between every glyph), BOM. Stripped entirely before comparison.
_UNICODE_NOISE_TRANSLATE = str.maketrans({
    "­": None,  # soft hyphen
    "·": None,  # middle dot
    "‧": None,  # hyphenation point
    "​": None,  # zero-width space
    "‌": None,  # zero-width non-joiner
    "‍": None,  # zero-width joiner
    "﻿": None,  # zero-width no-break space / BOM
})
# Pt 8c — Unicode dash variants normalized to ASCII hyphen so an en-dash
# in the chunk text and a plain hyphen in the LLM-emitted phrase still
# substring-match.
_DASH_NORMALIZE_RE = re.compile(r"[‐-―−⁃]")
# Pt 8c — markdown emphasis / code-fence markers that wrap words in the
# chunk (`**Atomic Habits**`) but rarely appear in LLM-emitted phrases.
# Stripped (not space-replaced) because they touch word boundaries.
_MD_EMPHASIS_RE = re.compile(r"[*_`]+")


def _normalize_evidence(text: str) -> str:
    """Lowercase + whitespace-collapse + Unicode/markdown noise strip.

    Pt 8c expansion: HTML entities decoded (`&amp;` → `&`), syllabification
    marks and zero-width characters removed, Unicode dashes normalized to
    `-`, markdown emphasis stripped. This lifts the Pt 7f false-drop
    class on dictionary-formatted text (atomic, habit, the book title)
    and the Phase B miss on cite-style entities with `&amp;`.
    """
    if not text:
        return ""
    s = text
    if "&" in s:
        # html.unescape handles &amp; &lt; &gt; &quot; &#39; and numeric refs.
        # Cheap when no entities are present (early exit on the substring).
        import html as _html  # local import keeps module load light
        s = _html.unescape(s)
    s = s.translate(_UNICODE_NOISE_TRANSLATE)
    s = _DASH_NORMALIZE_RE.sub("-", s)
    s = _MD_EMPHASIS_RE.sub("", s)
    return _WHITESPACE_RE.sub(" ", s).lower().strip()


# Pt 8c — paraphrase tolerance. English function-word stoplist — these
# are tokens the LLM commonly omits / swaps / inserts when paraphrasing,
# so they shouldn't count toward content overlap. Anything outside this
# list is treated as "content" and must be present in the chunk for the
# overlap fraction to credit it.
_EVIDENCE_STOPWORDS: frozenset[str] = frozenset({
    # articles / coordinators / determiners
    "a", "an", "the", "and", "or", "but", "nor", "so", "yet",
    # prepositions
    "of", "in", "on", "at", "to", "for", "with", "by", "from", "as",
    "into", "onto", "out", "off", "up", "down", "over", "under", "about",
    "through", "between", "across", "above", "below", "behind", "before",
    "after", "during", "since", "until", "near", "against", "along",
    "around", "among", "via", "per", "than", "like",
    # be / have / do / modals
    "is", "are", "was", "were", "be", "been", "being", "am",
    "have", "has", "had", "having",
    "do", "does", "did", "doing", "done",
    "will", "would", "shall", "should", "may", "might", "must",
    "can", "could", "ought",
    # demonstratives / pronouns / possessives
    "this", "that", "these", "those", "it", "its",
    "i", "me", "you", "we", "they", "us", "them",
    "his", "her", "hers", "their", "theirs", "our", "ours", "your", "yours",
    "my", "mine", "him", "she", "he", "who", "whom", "whose", "which",
    # wh-words / connectors
    "when", "where", "why", "how", "what",
    "if", "then", "because", "although", "though", "while", "whereas",
    # frequency / generic adverbs that rarely carry topical signal
    "also", "just", "only", "very", "really", "still", "even", "ever",
    "always", "never", "often", "sometimes", "usually",
    # negation / generic quantifiers
    "not", "no", "yes",
    "more", "most", "less", "least", "much", "many", "some", "any",
    "all", "each", "every", "few", "several",
})
_EVIDENCE_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _evidence_token_overlap(phrase: str, text: str, *, threshold: float = 0.6) -> bool:
    """Token-overlap fallback for paraphrased evidence.

    Returns True iff at least `threshold` fraction of `phrase`'s content
    tokens (non-stopword alphanumeric runs after lowercasing) also appear
    in `text`. Both inputs are assumed already normalized.

    Defaults to 0.6 — empirically permissive enough to recover paraphrases
    like `"Scott Adams, the cartoonist behind the Dilbert comic"`
    matching a chunk that says `"Scott Adams created Dilbert"` (4/6
    content tokens = 67%), while still rejecting bald hallucinations
    where the LLM invents a phrase wholesale.
    """
    phrase_tokens = {
        t for t in _EVIDENCE_TOKEN_RE.findall(phrase or "")
        if t not in _EVIDENCE_STOPWORDS
    }
    if not phrase_tokens:
        return False
    text_tokens = set(_EVIDENCE_TOKEN_RE.findall(text or ""))
    overlap = phrase_tokens & text_tokens
    effective_threshold = max(threshold, 0.8) if len(phrase_tokens) <= 3 else threshold
    return (len(overlap) / len(phrase_tokens)) >= effective_threshold


def _validate_evidence(evidence_phrase: str | None, chunk_text: str) -> bool:
    """Phase B evidence gate — return True iff `evidence_phrase` matches
    `chunk_text` after normalization.

    Two-stage check:
      1. Verbatim substring after `_normalize_evidence` (the original
         Phase B contract — Unicode-noise-cleaned, lowercase, whitespace-
         collapsed, HTML-decoded, markdown-stripped).
      2. Pt 8c paraphrase fallback — if substring fails, accept when ≥60%
         of the phrase's content tokens appear in the normalized chunk
         text. Rejects bald hallucinations but recovers legitimate
         paraphrases like `"Scott Adams, the cartoonist behind the
         Dilbert comic"` for a chunk saying `"Scott Adams created
         Dilbert"`.

    Empty / missing phrase fails. Side-effect-free so callers decide
    what to do with the result.
    """
    norm_phrase = _normalize_evidence(evidence_phrase or "")
    if not norm_phrase:
        return False
    norm_text = _normalize_evidence(chunk_text)
    if norm_phrase in norm_text:
        return True
    return _evidence_token_overlap(norm_phrase, norm_text, threshold=0.6)


EVIDENCE_CUE_RULES: list[tuple[re.Pattern[str], str, bool]] = [
    # Data/container direction repairs: "events stored in SQLite" means
    # SQLite stores events, even if the LLM emitted events -> stores -> SQLite.
    (re.compile(r"\b(stored|saved|persisted|kept)\s+(in|inside|to)\b"), "stores", True),
    (re.compile(r"\b(pre-?load(?:ing)?|loads?)\b.*\b(into|in)\b"), "stores", True),
    # UI/product containment is structural, not geographic.
    (re.compile(r"\b(placed|shown|visible|appears?)\s+(inside|in|within)\b|\binside the\b"), "part_of", False),
    # Explicit textual/citation cues.
    (re.compile(r"\b(quote|quotes|quoted|references?|mentions?|surfaces?)\b"), "references", False),
    # Runtime/deployment cues.
    (re.compile(r"\b(fine-?tune|deploy(?:ed)?|runs?|executes?)\s+(on|via)\b|\bon-device\b|\bnnapi\b|\bgpu\b"), "runs_on", False),
    # Training-data cues.
    (re.compile(r"\btrained\s+on\b|\btraining\s+data\b|\bdataset\b"), "trained_on", False),
    # Output/artifact creation cues.
    (re.compile(r"\b(export|outputs?|generates?|returns?|creates?)\b"), "produces", False),
    # Extraction / detection / dataflow cues — `extracts` was merged into
    # `detects`, so both surface verb classes route to the same predicate.
    (re.compile(r"\b(extracts?|captures?|pulls?|detects?|recognizes?|identifies?)\b"), "detects", False),
]


def normalize_relation_predicate_alias(predicate: str | None) -> tuple[str, bool]:
    """Map common LLM predicate variants to the approved relation vocabulary.

    Returns `(approved_predicate, reverse_subject_object)`. The reverse flag is
    used for passive predicates such as `stored_in` and `used_by`, where the
    correct graph edge points from the storage/consumer back to the object.
    """
    raw = str(predicate or "").strip()
    if not raw:
        return raw, False
    key = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    return RELATION_ALIAS_MAP.get(key, (raw, False))


def _render_vocab_line(
    name: str, glosses: dict[str, str], *, sentinel: str | None = None
) -> str:
    """Render a single vocab item as 'Name=gloss' when a gloss is known, else
    just 'Name'. Custom per-corpus entity/relation names keep working — they
    simply render without a gloss. The `=` separator avoids the nested-paren
    failure mode where some LLMs treat ',' inside `(…)` as a list boundary.

    When `sentinel` is supplied and matches `name`, the rendered line is
    suffixed with `[FALLBACK]` so the model sees an explicit "use only when
    nothing else fits" tag inline with the option itself. Sentinel placement
    stays at the end of the list (the LLM-bias mitigation), but the tag is
    what actually reduces lazy fallbacks.
    """
    g = glosses.get(name)
    base = f"{name}={g}" if g else name
    if sentinel is not None and name == sentinel:
        return f"{base} [FALLBACK]"
    return base


def _render_vocab_constraint(
    vocab: list[str], glosses: dict[str, str], sentinel: str
) -> str:
    """Render the vocab list as `Name=gloss|Name=gloss|…` — pipe with no
    surrounding whitespace. The dense form removes ~3 chars per separator
    (38 separators × 3 = ~120 chars saved per call) and removes the
    space-comma confusion that previously confused some smaller models.
    The sentinel item gets an explicit `[FALLBACK]` tag inline.
    """
    return "|".join(
        _render_vocab_line(v, glosses, sentinel=sentinel) for v in vocab
    )


def _render_schema_lens_block(schema_lens: SchemaLens | dict | None) -> str:
    """Render bounded corpus guidance for the extraction prompt.

    The block is intentionally framed as guidance, never as permission to
    invent schema labels. All final entity/relation labels are still enforced
    by SchemaContext after the LLM returns JSON.
    """
    lens = (
        schema_lens
        if isinstance(schema_lens, SchemaLens)
        else SchemaLens.from_dict(schema_lens if isinstance(schema_lens, dict) else None)
    )
    if lens is None or lens.status != "ready":
        return ""

    lines: list[str] = ["\nCorpus schema lens (guidance only; not new output fields):"]
    if lens.corpus_domains:
        lines.append("- likely corpus domains: " + ", ".join(lens.corpus_domains[:8]))
    # Pt9f — preferred_entity_types deliberately NOT rendered.
    #
    # The original design assumed corpora are topically coherent (one
    # domain). The lens averaged across matched domain rules and emitted
    # a "prefer these 8 entity types" guidance line. For single-domain
    # corpora this was a useful nudge. For heterogeneous libraries —
    # the actual user case here, 521 books spanning software /
    # psychology / business / game design / ML / writing / decision
    # theory — the averaging picks 8 winners, truncates the rest, and
    # then biases every chunk's extraction toward the corpus-wide
    # winners regardless of the individual chunk's domain.
    #
    # Concrete failure observed: Phase5_Luau_v4 corpus matched 6 domain
    # rules. The top 8 preferred became [Product, Method, Concept,
    # Document, Rule, Artifact, Person, Organization] — Software (the
    # leading entry of software_engineering's domain rule) lost the
    # cap=8 race to product_prd's Product. Every React / TensorFlow /
    # MLKit chunk in the corpus then got biased toward Product despite
    # the universal vocab line containing Software.
    #
    # The vocab line ("entity_type one of: Person|...|Software|...")
    # already enumerates all 14 universal types. The LLM has the full
    # menu. Dropping the "prefer" line lets the chunk text decide
    # entity_type without corpus-wide override. Object_kind hints
    # below STAY because object_kind is open-vocab and the domain-aware
    # cheat sheet from Pt9d genuinely helps the LLM pick the right
    # granularity once the bucket is decided.
    #
    # preferred_relations renders below — kept for now because relation
    # vocab is 30 entries with cap=10 (the bias is gentler than the
    # 14:8 entity_type ratio). Revisit if a relation gets visibly
    # truncated the same way.
    if lens.preferred_relations:
        lines.append(
            "- prefer these approved predicates when text supports them: "
            + ", ".join(lens.preferred_relations[:10])
        )
    if lens.relation_aliases:
        aliases = [
            f"{alias} -> {predicate}"
            for alias, predicate in list(lens.relation_aliases.items())[:10]
        ]
        lines.append("- relation phrase aliases to normalize: " + "; ".join(aliases))
    if lens.object_kinds:
        lines.append(
            "- object kinds to notice for entity naming only: "
            + ", ".join(lens.object_kinds[:10])
        )
    if lens.canonical_families:
        lines.append(
            "- concept families to notice for coverage only: "
            + ", ".join(lens.canonical_families[:10])
        )
    lines.append(
        "- If this lens conflicts with the chunk text, trust the chunk text. "
        "Never output the lens fields themselves."
    )
    return "\n".join(lines)


# Pt9d — fallback when no SchemaLens has been built yet (first ingest of
# a corpus, before the deterministic lens runs). Intentionally narrow —
# the scattershot kitchen sink the original Pt9b prompt shipped with
# pulled the LLM toward emitting empty object_kind because no single
# example felt domain-appropriate. A short generic list keeps the door
# open without confusing the model.
_OBJECT_KIND_FALLBACK_HINTS: list[str] = [
    "Library", "Framework", "Service", "Method",
    "Theory", "Document",
]


def _render_object_kind_hint(schema_lens: SchemaLens | dict | None) -> str:
    """Pt9d — render the prompt's object_kind cheat sheet from the
    SchemaLens's domain-specific kinds.

    The lens is built deterministically by `build_deterministic_schema_lens`
    from triggers in the corpus content. A software-heavy corpus produces
    a software lens with kinds like ["Library", "Framework", "Application",
    "Service", "API", "Language", "Platform"]. A psychology corpus
    produces ["Disorder", "Syndrome", "Trait", "Therapy", "Assessment",
    "Theory"]. Etc.

    Returning the lens's kinds as the prompt's allowed-values list is
    what makes Pt9b's object_kind extraction actually populate. Without
    domain-specific steering, the LLM sees a generic kitchen-sink list
    (library|framework|disorder|trait|therapy|protocol|format|...) where
    only 2-3 entries match the chunk's domain and emits empty rather
    than guess.

    When no lens is provided (first ingest of a corpus), fall back to
    a tight generic list that's better than the original kitchen sink
    but worse than a real domain-specific list. The lens forms on the
    second ingest at the latest, so this state is transient.
    """
    lens = (
        schema_lens
        if isinstance(schema_lens, SchemaLens)
        else SchemaLens.from_dict(schema_lens if isinstance(schema_lens, dict) else None)
    )
    kinds = (
        list(getattr(lens, "object_kinds", []) or [])
        if lens is not None
        else []
    )
    if not kinds:
        kinds = list(_OBJECT_KIND_FALLBACK_HINTS)
    return "|".join(kinds[:10])


def _bounded_extraction_text(text: str, max_tokens: int) -> tuple[str, int, bool]:
    """Bound the span sent to Ghost B independently of chunker correctness."""

    raw = str(text or "")
    if max_tokens <= 0:
        return raw, 0, False
    tokens = _TOKENIZER.encode(raw, disallowed_special=())
    if len(tokens) <= max_tokens:
        return raw, len(tokens), False
    bounded = _TOKENIZER.decode(tokens[:max_tokens]).strip()
    return bounded, len(tokens), True


def _global_extraction_semaphore(limit: int) -> asyncio.Semaphore:
    global _GLOBAL_EXTRACTION_SEMAPHORE
    global _GLOBAL_EXTRACTION_SEMAPHORE_LIMIT
    global _GLOBAL_EXTRACTION_SEMAPHORE_LOOP

    normalized_limit = max(1, int(limit or 1))
    loop = asyncio.get_running_loop()
    if (
        _GLOBAL_EXTRACTION_SEMAPHORE is None
        or _GLOBAL_EXTRACTION_SEMAPHORE_LIMIT != normalized_limit
        or _GLOBAL_EXTRACTION_SEMAPHORE_LOOP is not loop
    ):
        _GLOBAL_EXTRACTION_SEMAPHORE = asyncio.Semaphore(normalized_limit)
        _GLOBAL_EXTRACTION_SEMAPHORE_LIMIT = normalized_limit
        _GLOBAL_EXTRACTION_SEMAPHORE_LOOP = loop
    return _GLOBAL_EXTRACTION_SEMAPHORE


def _model_lane_key(entry: dict, pool_idx: int) -> tuple[int, str, str]:
    return (
        pool_idx,
        str(entry.get("model") or ""),
        str(entry.get("base_url") or ""),
    )


def _model_lane_semaphore(
    entry: dict,
    pool_idx: int,
    limit: int,
) -> asyncio.Semaphore:
    normalized_limit = max(1, int(limit or 1))
    loop = asyncio.get_running_loop()
    key = _model_lane_key(entry, pool_idx)
    if (
        key not in _MODEL_LANE_SEMAPHORES
        or _MODEL_LANE_SEMAPHORE_LIMITS.get(key) != normalized_limit
        or _MODEL_LANE_SEMAPHORE_LOOPS.get(key) is not loop
    ):
        _MODEL_LANE_SEMAPHORES[key] = asyncio.Semaphore(normalized_limit)
        _MODEL_LANE_SEMAPHORE_LIMITS[key] = normalized_limit
        _MODEL_LANE_SEMAPHORE_LOOPS[key] = loop
    return _MODEL_LANE_SEMAPHORES[key]


@dataclass
class SchemaContext:
    """Phase 14: Ontology-Lite schema for entity-type and relation-predicate vocabularies.

    `entity_schema` and `relation_schema` are user-defined categories. The LLM creates
    instances freely under those categories. When a category is provided but the LLM
    proposes an out-of-schema label, behavior is governed by `strict`:

      - 'off'  → no enforcement (schema is a hint).
      - 'soft' → remap unknowns to ENTITY_SENTINEL ('other') / RELATION_SENTINEL ('related_to').
                 Edge / node is preserved, vague but not lost.
      - 'hard' → drop unknowns entirely (precision-critical mode).

    Sentinels are always implicitly available — the user's schema cannot remove them.
    """

    entity_schema: list[str] | None = None
    relation_schema: list[str] | None = None
    strict: Literal["off", "soft", "hard"] = "soft"

    ENTITY_SENTINEL: ClassVar[str] = "other"
    RELATION_SENTINEL: ClassVar[str] = "related_to"

    @property
    def has_entity_schema(self) -> bool:
        return bool(self.entity_schema)

    @property
    def has_relation_schema(self) -> bool:
        return bool(self.relation_schema)

    @property
    def entity_vocab(self) -> list[str]:
        """User entity types + sentinel, deduplicated, preserving user order."""
        if not self.entity_schema:
            return []
        return list(dict.fromkeys([*self.entity_schema, self.ENTITY_SENTINEL]))

    @property
    def relation_vocab(self) -> list[str]:
        """User relation predicates + sentinel, deduplicated, preserving user order."""
        if not self.relation_schema:
            return []
        return list(dict.fromkeys([*self.relation_schema, self.RELATION_SENTINEL]))


@dataclass
class SchemaLens:
    """Bounded corpus guidance for Ghost B extraction.

    The lens is deliberately not a schema. It is a compact, auto-generated
    reading frame that helps the LLM notice corpus-local concepts while the
    normal SchemaContext still constrains entity_type and predicate output.
    """

    lens_id: str
    version: str = "polymath.schema_lens.v1"
    status: str = "ready"
    source: str = "deterministic"
    corpus_domains: list[str] = field(default_factory=list)
    preferred_entity_types: list[str] = field(default_factory=list)
    preferred_relations: list[str] = field(default_factory=list)
    relation_aliases: dict[str, str] = field(default_factory=dict)
    object_kinds: list[str] = field(default_factory=list)
    canonical_families: list[str] = field(default_factory=list)
    confidence: float = 0.0

    @staticmethod
    def from_dict(data: dict | None) -> "SchemaLens | None":
        if not isinstance(data, dict):
            return None
        return SchemaLens(
            lens_id=str(data.get("lens_id") or "schema-lens"),
            version=str(data.get("version") or "polymath.schema_lens.v1"),
            status=str(data.get("status") or "ready"),
            source=str(data.get("source") or "deterministic"),
            corpus_domains=[
                str(v) for v in (data.get("corpus_domains") or []) if str(v).strip()
            ],
            preferred_entity_types=[
                str(v)
                for v in (data.get("preferred_entity_types") or [])
                if str(v).strip()
            ],
            preferred_relations=[
                str(v) for v in (data.get("preferred_relations") or []) if str(v).strip()
            ],
            relation_aliases={
                str(k).strip(): str(v).strip()
                for k, v in (data.get("relation_aliases") or {}).items()
                if str(k).strip() and str(v).strip()
            },
            object_kinds=[
                str(v) for v in (data.get("object_kinds") or []) if str(v).strip()
            ],
            canonical_families=[
                str(v) for v in (data.get("canonical_families") or []) if str(v).strip()
            ],
            confidence=float(data.get("confidence") or 0.0),
        )

    def to_dict(self) -> dict:
        return {
            "lens_id": self.lens_id,
            "version": self.version,
            "status": self.status,
            "source": self.source,
            "corpus_domains": self.corpus_domains,
            "preferred_entity_types": self.preferred_entity_types,
            "preferred_relations": self.preferred_relations,
            "relation_aliases": self.relation_aliases,
            "object_kinds": self.object_kinds,
            "canonical_families": self.canonical_families,
            "confidence": self.confidence,
        }


async def resolve_chunk_vocab(
    schema: SchemaContext | None,
    chunk_vec: list[float] | None,
    resolver: SchemaResolver | None,
    inline_limit: int,
    top_k: int,
) -> tuple[list[str] | None, list[str] | None]:
    """Phase 14.2 — decide which entity / relation terms to inject for this chunk.

    Strategy:
      - No schema set                 → return (None, None); caller renders default prompt.
      - Vocab fits inline (≤ limit)   → return the full sentinel-augmented vocab.
      - Vocab > limit + retriever ok  → return top-K terms (always with sentinel appended).
      - Vocab > limit + no retriever  → degraded fallback: first `inline_limit` terms.

    The sentinel ('other' / 'related_to') is ALWAYS the last element so the LLM
    sees the safety-valve explicitly.
    """
    if schema is None:
        return (None, None)

    async def _resolve_one(
        kind: str, full_vocab: list[str], sentinel: str
    ) -> list[str] | None:
        if not full_vocab:
            return None
        if len(full_vocab) <= inline_limit:
            return full_vocab  # Already sentinel-augmented by SchemaContext property.
        if resolver is None or chunk_vec is None:
            logger.warning(
                "GHOST B schema retrieval skipped (no resolver or vector); "
                "falling back to first %d of %d %s terms.",
                inline_limit,
                len(full_vocab),
                kind,
            )
            head = list(full_vocab[:inline_limit])
            if sentinel not in head:
                head.append(sentinel)
            return head
        retrieved = await resolver(kind, chunk_vec, top_k)
        # Always append sentinel as a safety valve (deduplicate in case it was retrieved).
        out = list(dict.fromkeys([*retrieved, sentinel]))
        return out

    eff_entity = await _resolve_one(
        "entity_type", schema.entity_vocab, SchemaContext.ENTITY_SENTINEL
    )
    eff_relation = await _resolve_one(
        "relation", schema.relation_vocab, SchemaContext.RELATION_SENTINEL
    )
    return eff_entity, eff_relation


def _lane_supports_json_object(entry: dict) -> bool:
    """Return True for provider lanes known to honor OpenAI JSON object mode."""
    return resolve_extraction_provider_card(entry).supports_json_object


def _lane_supports_json_schema(entry: dict) -> bool:
    """Pt9c — return True for provider lanes known to honor json_schema mode.

    json_schema (a.k.a. "structured outputs") uses provider-level constrained
    decoding: the decoder masks any token that would violate the schema as
    the model generates. Contrast with json_object, which only guarantees
    valid JSON syntax (not shape).

    Production extraction should use this for known capable lanes by default:
    OpenAI, DeepSeek, and managed vLLM. Operators can still set
    extra_params.supports_json_schema=false to force JSONL for a broken or
    nonconformant provider. If a lane claims support but rejects the payload,
    _process_one records json_schema_unsupported and falls back to JSONL once.
    """
    return resolve_extraction_provider_card(entry).supports_json_schema


def _select_extraction_output_mode(
    configured_mode: str | None,
    entry: dict,
    *,
    profile_name: str,
) -> Literal["json_object", "json_object_prompt", "jsonl", "json_schema"]:
    """Resolve the actual Ghost B output contract for one lane attempt.

    JSONL is the compatibility fallback. json_schema (Pt9c) is the production
    default for known capable lanes; when enabled, the LLM is token-masked at
    generation time to match the ExtractionResponse Pydantic model exactly —
    eliminating off-vocab leaks at the source rather than catching them
    post-hoc in Pt8b validation. json_object (legacy) is retained for explicit
    fallback behavior.

    Rescue profiles stay on JSONL — the rescue prompt is JSONL-shaped and
    its merge logic with accepted_jsonl_items depends on the line format.
    Only the "normal" profile path opts into json_schema.
    """
    if profile_name != "normal":
        return "jsonl"
    card = resolve_extraction_provider_card(entry)
    if card.schema_mode == "json_schema":
        return "json_schema"
    if card.schema_mode == "json_object":
        return "json_object"
    if card.schema_mode == "json_object_prompt":
        return "json_object_prompt"
    configured = str(configured_mode or "").strip().lower()
    if configured in {"json_object", "json_object_prompt"}:
        return configured  # type: ignore[return-value]
    return "jsonl"


def _entry_extra_params(entry: dict | object) -> dict[str, Any]:
    if hasattr(entry, "model_dump"):
        data = entry.model_dump()  # type: ignore[attr-defined]
    elif isinstance(entry, dict):
        data = entry
    else:
        data = dict(entry or {})  # type: ignore[arg-type]
    extra = data.get("extra_params") or {}
    return extra if isinstance(extra, dict) else {}


def _normalize_extraction_routing_policy(value: Any) -> ExtractionRoutingPolicy | None:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"balanced", "balanced_fanout", "fanout", "parallel"}:
        return "balanced"
    if text in {"work_stealing", "workstealing", "spillover", "fastest", "auto"}:
        return "work_stealing"
    if text in {"primary_fallback", "primary_failover", "fallback", "failover"}:
        return "primary_fallback"
    return None


def _resolve_extraction_routing_policy(pool: list[dict]) -> ExtractionRoutingPolicy:
    """Resolve how a provider pool should consume the chunk queue.

    Work-stealing remains the fastest default for a single provider family.
    Mixed private-vLLM plus cloud pools default to balanced fanout so "RTX +
    cloud" visibly means both lanes participate, unless the operator asks for
    explicit primary/fallback behavior.
    """

    for entry in pool:
        explicit = _normalize_extraction_routing_policy(entry.get("routing_policy"))
        if explicit:
            return explicit
        extra = _entry_extra_params(entry)
        explicit = _normalize_extraction_routing_policy(
            extra.get("routing_policy") or extra.get("route_policy")
        )
        if explicit:
            return explicit

    if len(pool) < 2:
        return "work_stealing"
    cards = [resolve_extraction_provider_card(entry) for entry in pool]
    has_private = any(card.local_private for card in cards)
    has_cloud = any(not card.local_private for card in cards)
    if has_private and has_cloud:
        return "balanced"
    return "work_stealing"


def _pool_route_key(pool: list[dict]) -> tuple[str, ...]:
    return tuple(
        ":".join(
            str(entry.get(field) or "")
            for field in ("provider_preset", "base_url", "model")
        )
        for entry in pool
    )


def _next_balanced_route_offset(
    pool: list[dict],
    routing_policy: ExtractionRoutingPolicy,
) -> int:
    if routing_policy != "balanced" or len(pool) < 2:
        return 0
    key = _pool_route_key(pool)
    current = _BALANCED_ROUTE_OFFSETS.get(key, 0)
    _BALANCED_ROUTE_OFFSETS[key] = current + 1
    return current


def _worker_lane_spawn_order(
    lane_limits: list[int],
    disabled_lanes: set[int],
    routing_policy: ExtractionRoutingPolicy,
    *,
    start_offset: int = 0,
) -> list[int]:
    enabled = [
        idx
        for idx, limit in enumerate(lane_limits)
        if idx not in disabled_lanes and int(limit or 0) > 0
    ]
    if routing_policy == "primary_fallback":
        return [enabled[0]] * int(lane_limits[enabled[0]]) if enabled else []
    if routing_policy != "balanced":
        return [
            idx
            for idx in enabled
            for _ in range(int(lane_limits[idx]))
        ]

    if enabled and start_offset:
        offset = int(start_offset) % len(enabled)
        enabled = enabled[offset:] + enabled[:offset]

    order: list[int] = []
    max_slots = max((int(lane_limits[idx]) for idx in enabled), default=0)
    for slot in range(max_slots):
        for idx in enabled:
            if slot < int(lane_limits[idx]):
                order.append(idx)
    return order


def _json_object_response_format() -> dict[str, str]:
    return {"type": "json_object"}


def _pin_all_required(schema: dict) -> dict:
    """Pt9c — force `required` to include every key in `properties` and set
    `additionalProperties: False`, recursively.

    Pydantic's .model_json_schema() emits `required` containing only fields
    without defaults — fields with `Field(default=...)` are optional from
    Pydantic's view. But OpenAI's strict json_schema mode rejects schemas
    where `properties` contains keys not in `required`. The mitigation is to
    pin every property as required AND set additionalProperties=False at
    every object level. The model still emits defaults (empty string, empty
    list) so the runtime behavior matches Pydantic — strict mode only
    enforces structural completeness.

    Mutates a shallow copy by walking; the input schema dict is not
    modified.  Returns the modified copy for chaining.

    Coverage:
      - properties: dict[str, schema] → recurse into each value
      - $defs / definitions: nested model registry → recurse into each value
      - items: array element schema → recurse
      - additionalProperties: nested object schema → recurse (only if dict;
        we replace booleans on object-typed nodes below)
      - anyOf / allOf / oneOf: schema lists → recurse into each element
    """
    import copy

    def _walk(node):
        if not isinstance(node, dict):
            return node
        # Object schemas: pin required + additionalProperties.
        if node.get("type") == "object" and isinstance(node.get("properties"), dict):
            node["required"] = list(node["properties"].keys())
            node["additionalProperties"] = False
        # Recurse into all places a nested schema can appear.
        for key in ("properties", "$defs", "definitions"):
            children = node.get(key)
            if isinstance(children, dict):
                for sub in children.values():
                    _walk(sub)
        items = node.get("items")
        if isinstance(items, dict):
            _walk(items)
        addl = node.get("additionalProperties")
        if isinstance(addl, dict):
            _walk(addl)
        for key in ("anyOf", "allOf", "oneOf"):
            lst = node.get(key)
            if isinstance(lst, list):
                for sub in lst:
                    _walk(sub)
        return node

    cloned = copy.deepcopy(schema)
    return _walk(cloned)


def _json_schema_response_format() -> dict:
    """Pt9c — build the json_schema response_format payload from the
    ExtractionResponse Pydantic model.

    Pydantic is the single source of truth — .model_json_schema() emits a
    schema that matches what _parse() expects. The _pin_all_required pass
    adapts it for OpenAI strict mode (and is a no-op for permissive
    providers like DeepSeek). One source of truth means the schema can
    never drift from the parser; adding a field to LLMEntity automatically
    flows into the generated schema with no hand-edits.
    """
    from services.ghost_b_schemas import ExtractionResponse

    raw = ExtractionResponse.model_json_schema()
    pinned = _pin_all_required(raw)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "ghost_b_extraction",
            "schema": pinned,
            "strict": True,
        },
    }


def _render_table_extraction_rules(
    chunk_kind: str | None,
    metadata: dict | None,
) -> str:
    if str(chunk_kind or "").lower() != "table":
        return ""
    meta = metadata or {}
    columns = [
        str(col).strip()
        for col in (meta.get("columns") or [])
        if str(col).strip()
    ][:12]
    column_hint = f" Columns: {', '.join(columns)}." if columns else ""
    return (
        "Table chunk rules:\n"
        f"- Treat each Row N line as structured evidence.{column_hint}\n"
        "- Use row labels or named row values as subjects when possible.\n"
        "- Use column headers as property_name, predicate context, or fact value labels.\n"
        "- Do not extract table numbers, captions, or column headers as standalone entities.\n"
        "- Prefer facts for numeric, categorical, size, status, score, threshold, and comparison cells.\n"
    )


def build_user_prompt(
    *,
    chunk_id: str,
    doc_id: str,
    corpus_id: str,
    text: str,
    max_entities: int | None = None,
    max_relations: int | None = None,
    schema: SchemaContext | None = None,
    effective_entity_vocab: list[str] | None = None,
    effective_relation_vocab: list[str] | None = None,
    schema_lens: SchemaLens | dict | None = None,
    enable_facts: bool | None = None,
    max_facts: int | None = None,
    max_total_lines: int | None = None,
    chunk_kind: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Render the per-chunk extraction user prompt, with optional schema constraints.

    When `schema` is None or has no vocabularies, behavior matches pre-14.1 verbatim
    (default 4-bucket entity_type enum, free-form predicates).

    Phase 14.2: callers may pass `effective_*_vocab` to override the vocabulary
    rendered for this specific chunk (e.g. retrieved top-K instead of full schema).
    When None, the full sentinel-augmented vocab from `schema` is used.
    """
    # Decide entity_type enum for the JSON schema example. Use `|` (no
    # surrounding whitespace) for symmetry with the predicate enum and
    # vocab block — every separator saves two characters per call.
    if schema and schema.has_entity_schema:
        vocab = effective_entity_vocab or schema.entity_vocab
        entity_type_enum = "|".join(vocab)
        entity_vocab_for_block = vocab
    else:
        entity_type_enum = "|".join(_DEFAULT_ENTITY_TYPES)
        entity_vocab_for_block = None

    # Decide predicate description
    if schema and schema.has_relation_schema:
        vocab = effective_relation_vocab or schema.relation_vocab
        predicate_desc = "|".join(vocab)
        relation_vocab_for_block = vocab
    else:
        predicate_desc = "short verb phrase label"
        relation_vocab_for_block = None

    # Build the vocabulary constraint block. Glosses render as `Name=gloss`
    # joined by `|` (no surrounding whitespace). Each block has exactly one
    # short fallback line — the verbose paragraphs were redundant with the
    # glosses themselves and inflated every per-chunk extraction prompt.
    vocab_block_lines: list[str] = []
    if entity_vocab_for_block or relation_vocab_for_block:
        vocab_block_lines.append("\nVocab:")
        if entity_vocab_for_block:
            rendered = _render_vocab_constraint(
                entity_vocab_for_block,
                UNIVERSAL_ENTITY_GLOSSES,
                SchemaContext.ENTITY_SENTINEL,
            )
            vocab_block_lines.append(f"entity_type one of: {rendered}")
            vocab_block_lines.append(
                f"If none fit use '{SchemaContext.ENTITY_SENTINEL}'. Never invent."
            )
        if relation_vocab_for_block:
            rendered = _render_vocab_constraint(
                relation_vocab_for_block,
                UNIVERSAL_RELATION_GLOSSES,
                SchemaContext.RELATION_SENTINEL,
            )
            vocab_block_lines.append(f"predicate one of: {rendered}")
            vocab_block_lines.append(
                f"If none fit use '{SchemaContext.RELATION_SENTINEL}'. Never invent."
            )
    vocab_block = "\n".join(vocab_block_lines)
    lens_block = _render_schema_lens_block(schema_lens)
    # Pt9d — render the object_kind cheat sheet from the SchemaLens's
    # domain-specific kinds. The original Pt9b prompt hardcoded a
    # kitchen-sink list (library|framework|disorder|trait|therapy|...)
    # that the LLM ignored because no single hint matched a chunk's
    # domain cleanly. The lens narrows this to the domain that actually
    # fired.
    object_kind_hint = _render_object_kind_hint(schema_lens)
    settings = get_settings()
    entity_cap = max_entities or settings.EXTRACTION_MAX_ENTITIES_PER_CHUNK
    relation_cap = max_relations or settings.EXTRACTION_MAX_RELATIONS_PER_CHUNK
    facts_enabled = settings.EXTRACTION_ENABLE_FACTS if enable_facts is None else enable_facts
    fact_cap = (
        settings.EXTRACTION_MAX_FACTS_PER_CHUNK
        if max_facts is None
        else max(0, int(max_facts))
    )
    line_cap = (
        settings.EXTRACTION_MAX_TOTAL_LINES
        if max_total_lines is None
        else max(1, int(max_total_lines))
    )
    fact_rules = ""
    fact_protocol = ""
    target = "entities and relations"
    if facts_enabled and fact_cap > 0:
        fact_types = "|".join(FACT_TYPES)
        target = "entities, relations, and facts"
        fact_protocol = (
            f'- Fact line: {{"t":"f","sub":"canonical_name from entities",'
            f'"ft":"{fact_types}","pn":"snake_case","val":"verbatim or normalized",'
            '"unit":"optional unit","cond":"optional condition","cf":0.0,'
            '"ev":"short source phrase"}}\n'
        )
        fact_rules = (
            f"- max {fact_cap} facts; facts are optional\n"
            "- facts capture high-value status, timestamps, thresholds, categories, tags, rule conditions/actions, or property values\n"
            "- fact subject must match an entity canonical_name already listed in entities\n"
            "- fact evidence_phrase must be a short exact phrase from text\n"
            "- drop vague or low-value facts\n"
        )
    table_rules = _render_table_extraction_rules(chunk_kind, metadata)

    return (
        f"Extract {target}. Output JSONL only: one self-contained JSON object per line.\n"
        "Do NOT output arrays, an outer object, prose, markdown, or code fences.\n"
        'End the complete extraction with exactly: {"t":"x"}\n'
        "Abbreviations:\n"
        "- t=e entity, t=r relation, t=f fact, t=x finished\n"
        "- Entity: cn=canonical_name, sf=surface_form, et=entity_type, ek=object_kind (optional free-form refinement: library/framework/disorder/therapy/protocol/etc), cf=confidence, qa=query_aliases (optional list of 2-4 search variants), def=definitional_phrase (optional 1-sentence definition pulled from this chunk)\n"
        "- Relation: sub=subject, pred=predicate, obj=object, ok=object_kind (\"entity\"|\"literal\" — distinct from entity ek), cf=confidence, ev=evidence_phrase, cue=optional relation trigger\n"
        "- Fact: sub=subject, ft=fact_type, pn=property_name, val=value, unit=unit, cond=condition, cf=confidence, ev=evidence_phrase\n"
        "Line shapes:\n"
        f'- Entity line: {{"t":"e","cn":"lowercase no-punct","sf":"verbatim","et":"{entity_type_enum}","ek":"{object_kind_hint} (choose one when applicable)","cf":0.0,"qa":["abbreviation","alt name"],"def":"one-sentence definition if explicitly stated in this chunk"}}\n'
        f'- Relation line: {{"t":"r","sub":"canonical_name","pred":"{predicate_desc}","obj":"canonical_name or literal","ok":"entity|literal","cf":0.0,"ev":"short source phrase","cue":"trigger"}}\n'
        f"{fact_protocol}"
        '- Finished line: {"t":"x"}\n'
        "Rules:\n"
        f"- max {line_cap} total extraction item lines including entities, relations, and facts\n"
        f"- max {entity_cap} entities, max {relation_cap} relations\n"
        f"{fact_rules}"
        f"{table_rules}"
        "- compact JSONL, no prose/markdown/duplicates; omit null or empty fields\n"
        "- canonical_name: lowercase, strip punctuation\n"
        "- confidence in [0,1]; drop low-confidence entries\n"
        "- evidence_phrase: short exact phrase from text\n"
        "- every relation must include evidence_phrase copied from TEXT; "
        "if no exact supporting phrase exists, do not emit that relation\n"
        "- if evidence_phrase contains a newline, escape it as \\n or rewrite it as one sentence\n"
        "- object_kind=entity only if object is a named entity in text and also listed in entities\n"
        f"- pick the narrowest predicate; use '{SchemaContext.RELATION_SENTINEL}' only when no specific predicate fits. Never invent a new predicate\n"
        "- omit ontology fields (domain_type, canonical_family, ontology_tags, ontology_version)\n"
        # Pt 7e — entity-quality gate. Without this, the LLM dutifully extracts
        # everything `explicitly stated` including generic role words and
        # structural references, which then form spurious cross-book bridges
        # in the Brain View. This rule is the upstream version of the
        # entity_stoplist.json gate applied at query time.
        "- SKIP generic mentions: pronouns (I, you, we, they, it); single letters and "
        "isolated math variables (k, i, n, x, v, w; subscripted symbols like S_k or v'); "
        "theorem / lemma / equation / proposition / corollary references (Theorem N, "
        "Lemma N, Equation N) and chapter/section/figure/table references (chapter N, "
        "table N, figure N, section N, index, references, introduction, appendix, "
        "this book, this chapter, the book); type names used as entities "
        "(person, organization, concept, document, entity); generic role/kinship words "
        "(user, users, patient, parent, children, family, the author, the reader, "
        "teacher, student, human, people, individual) unless the text is defining or "
        "specifying THIS one; generic abstract nouns standing alone (state, system, "
        "time, language, subject, data, object, action, rule) unless the text is treating "
        "them as a specific named concept; relation/predicate vocabulary words "
        "(uses, runs_on, produces, defines, stores, maps_to, references, owns, "
        "supports, implements, related_to, etc.) when they are merely listed as "
        "allowed labels rather than discussed as domain concepts; "
        # Pt10a — bibliographic citations are not entities. Drop them
        # at the source so they don't pollute the graph as Person /
        # Document / Concept.
        "bibliographic citations and reference entries (anything matching "
        "'Author. Title. Publisher, Year' or 'Author1, Author2 and Author3 ... 1998' "
        "or appearing under a References / Bibliography heading). "
        "Author names from citations are also out — only extract a person "
        "when the chunk discusses them as a subject, not as a cited author. "
        "Prefer specific named concepts.\n"
        f"{vocab_block}\n"
        f"{lens_block}\n"
        "\n"
        "TEXT:\n"
        f"{text}"
    )


def build_json_object_prompt(
    *,
    chunk_id: str,
    doc_id: str,
    corpus_id: str,
    text: str,
    max_entities: int | None = None,
    max_relations: int | None = None,
    schema: SchemaContext | None = None,
    effective_entity_vocab: list[str] | None = None,
    effective_relation_vocab: list[str] | None = None,
    schema_lens: SchemaLens | dict | None = None,
    enable_facts: bool | None = None,
    max_facts: int | None = None,
    evidence_max_chars: int | None = None,
    fact_value_max_chars: int | None = None,
    chunk_kind: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Render the strict JSON-object primary extraction prompt.

    This path is used only when the provider lane can enforce JSON object mode
    at the transport layer. Rescue mode deliberately stays JSONL because JSONL
    lets a partially generated answer be salvaged line-by-line.
    """
    if schema and schema.has_entity_schema:
        entity_vocab = effective_entity_vocab or schema.entity_vocab
        entity_type_desc = "|".join(entity_vocab)
        entity_vocab_for_block = entity_vocab
    else:
        entity_type_desc = "|".join(_DEFAULT_ENTITY_TYPES)
        entity_vocab_for_block = None

    if schema and schema.has_relation_schema:
        relation_vocab = effective_relation_vocab or schema.relation_vocab
        predicate_desc = "|".join(relation_vocab)
        relation_vocab_for_block = relation_vocab
    else:
        predicate_desc = "short verb phrase label"
        relation_vocab_for_block = None

    vocab_block_lines: list[str] = []
    if entity_vocab_for_block or relation_vocab_for_block:
        vocab_block_lines.append("\nVocab:")
        if entity_vocab_for_block:
            rendered = _render_vocab_constraint(
                entity_vocab_for_block,
                UNIVERSAL_ENTITY_GLOSSES,
                SchemaContext.ENTITY_SENTINEL,
            )
            vocab_block_lines.append(f"entity_type one of: {rendered}")
            vocab_block_lines.append(
                f"If none fit use '{SchemaContext.ENTITY_SENTINEL}'. Never invent."
            )
        if relation_vocab_for_block:
            rendered = _render_vocab_constraint(
                relation_vocab_for_block,
                UNIVERSAL_RELATION_GLOSSES,
                SchemaContext.RELATION_SENTINEL,
            )
            vocab_block_lines.append(f"predicate one of: {rendered}")
            vocab_block_lines.append(
                f"If none fit use '{SchemaContext.RELATION_SENTINEL}'. Never invent."
            )
    vocab_block = "\n".join(vocab_block_lines)
    lens_block = _render_schema_lens_block(schema_lens)
    # Pt9d — lens-driven object_kind cheat sheet, same logic as in
    # build_user_prompt. Pre-Pt9d this was a hardcoded kitchen sink
    # ("library | framework | disorder | therapy | protocol | format | ...")
    # that the LLM ignored because no single hint matched a chunk's
    # domain cleanly.
    object_kind_hint = _render_object_kind_hint(schema_lens)
    settings = get_settings()
    entity_cap = max_entities or settings.EXTRACTION_JSON_OBJECT_MAX_ENTITIES_PER_CHUNK
    relation_cap = (
        max_relations
        if max_relations is not None
        else settings.EXTRACTION_JSON_OBJECT_MAX_RELATIONS_PER_CHUNK
    )
    facts_enabled = settings.EXTRACTION_ENABLE_FACTS if enable_facts is None else enable_facts
    fact_cap = (
        settings.EXTRACTION_JSON_OBJECT_MAX_FACTS_PER_CHUNK
        if max_facts is None
        else max(0, int(max_facts))
    )
    evidence_cap = evidence_max_chars or settings.EXTRACTION_EVIDENCE_MAX_CHARS
    value_cap = fact_value_max_chars or settings.EXTRACTION_FACT_VALUE_MAX_CHARS
    fact_types = "|".join(FACT_TYPES)
    fact_rule = (
        f"- facts: max {fact_cap}; fact_type one of {fact_types}; "
        "subject must match an entity canonical_name; "
        f"value <= {value_cap} chars\n"
        if facts_enabled and fact_cap > 0
        else "- facts must be []\n"
    )
    table_rules = _render_table_extraction_rules(chunk_kind, metadata)
    shape = {
        "schema_version": "polymath.extract.v2",
        "entities": [
            {
                "canonical_name": "lowercase no-punct",
                "surface_form": "verbatim phrase",
                "entity_type": entity_type_desc,
                # Pt9b — object_kind: free-form second-axis refinement of
                # entity_type. Pt9d — the allowed values are domain-tuned
                # via the SchemaLens (software corpora see Library/
                # Framework/Service/etc.; psychology corpora see Disorder/
                # Syndrome/Trait/Therapy/etc.). Choose one when the chunk
                # text supports it; omit only when no kind genuinely fits.
                "object_kind": f"{object_kind_hint} (choose one when applicable)",
                "confidence": 0.0,
                # Pt 10c — query-facing fields. Both optional. Emit only when
                # naturally present in the chunk; omit entirely (or set to
                # empty) otherwise.
                "query_aliases": ["abbreviation or synonym (optional)"],
                "definitional_phrase": "one-sentence definition if explicitly stated (optional)",
            }
        ],
        "relations": [
            {
                "subject": "entity canonical_name",
                "predicate": predicate_desc,
                "object": "entity canonical_name or literal",
                "object_kind": "entity|literal",
                "confidence": 0.0,
                "evidence_phrase": "short exact phrase",
            }
        ],
        "facts": [
            {
                "subject": "entity canonical_name",
                "fact_type": fact_types,
                "property_name": "snake_case",
                "value": "verbatim or normalized",
                "unit": "optional unit",
                "condition": "optional condition",
                "confidence": 0.0,
                "evidence_phrase": "short exact phrase",
            }
        ],
    }
    shape_json = json.dumps(shape, ensure_ascii=False, separators=(",", ":"))
    return (
        "Return exactly one valid JSON object and nothing else. "
        "Do not use markdown, code fences, JSONL, prose, or comments.\n"
        "The response must be parseable by json.loads. The word json is part of this contract.\n"
        "Object shape:\n"
        f"{shape_json}\n"
        "Rules:\n"
        f"- entities: max {entity_cap}; canonical_name lowercase, stripped punctuation; "
        "do not invent entity_type\n"
        f"- relations: max {relation_cap}; predicate from allowed vocabulary; "
        "subject must match an entity canonical_name\n"
        f"{fact_rule}"
        f"{table_rules}"
        f"- evidence_phrase <= {evidence_cap} chars and must be an exact phrase from TEXT\n"
        "- every relation must include evidence_phrase copied from TEXT; "
        "if no exact supporting phrase exists, do not emit that relation\n"
        "- if evidence_phrase contains a newline, escape it as \\n or rewrite it as one sentence\n"
        "- confidence in [0,1]; drop low-confidence or redundant items\n"
        "- prefer fewer correct items over broad coverage; "
        "do not fill caps just because they exist\n"
        f"- use '{SchemaContext.RELATION_SENTINEL}' only when no specific predicate fits\n"
        "- omit ontology fields (domain_type, canonical_family, ontology_tags, ontology_version)\n"
        # Pt 7e — entity-quality gate (mirrors the JSONL prompt). Without
        # this the LLM extracts everything stated in the text, including
        # generic words that spawn spurious cross-book bridges.
        "- SKIP generic mentions: pronouns (I, you, we, they, it); single letters and "
        "isolated math variables (k, i, n, x, v, w; subscripted symbols like S_k or v'); "
        "theorem / lemma / equation / proposition / corollary references (Theorem N, "
        "Lemma N, Equation N) and chapter/section/figure/table references (chapter N, "
        "table N, figure N, section N, index, references, introduction, appendix, "
        "this book, this chapter, the book); type names used as entities "
        "(person, organization, concept, document, entity); generic role/kinship words "
        "(user, users, patient, parent, children, family, the author, the reader, "
        "teacher, student, human, people, individual) unless the text is defining or "
        "specifying THIS one; generic abstract nouns standing alone (state, system, "
        "time, language, subject, data, object, action, rule) unless the text is treating "
        "them as a specific named concept; relation/predicate vocabulary words "
        "(uses, runs_on, produces, defines, stores, maps_to, references, owns, "
        "supports, implements, related_to, etc.) when they are merely listed as "
        "allowed labels rather than discussed as domain concepts; "
        # Pt10a — bibliographic citations are not entities. Drop them
        # at the source so they don't pollute the graph as Person /
        # Document / Concept.
        "bibliographic citations and reference entries (anything matching "
        "'Author. Title. Publisher, Year' or 'Author1, Author2 and Author3 ... 1998' "
        "or appearing under a References / Bibliography heading). "
        "Author names from citations are also out — only extract a person "
        "when the chunk discusses them as a subject, not as a cited author. "
        "Prefer specific named concepts.\n"
        f"{vocab_block}\n"
        f"{lens_block}\n"
        "\n"
        "TEXT:\n"
        f"{text}"
    )


@dataclass
class ExtractionTask:
    chunk_id: str
    doc_id: str
    corpus_id: str
    text: str  # child chunk text only
    chunk_kind: str = "body"
    metadata: dict = field(default_factory=dict)


@dataclass
class EntityItem:
    canonical_name: str
    surface_form: str
    entity_type: str  # person | org | concept | other
    confidence: float
    # Pt 10c — query-facing fields. Default-empty so all existing
    # EntityItem(**dict) call sites (including the worker rehydrator that
    # reads pre-Pt-10c Mongo staging) keep working without code changes.
    query_aliases: list[str] = field(default_factory=list)
    definitional_phrase: str = ""
    # Pt9b — second-axis typed facet. The LLM emits a free-form refinement
    # (e.g. "library", "framework", "disorder", "therapy", "protocol") that
    # narrows the entity_type bucket. Default-empty so pre-Pt9b call sites
    # (worker rehydration from staging, _apply_schema soft-remap fallback,
    # endpoint completion) all keep working. The graph layer
    # (neo4j_writer.py:1556+, orchestrator.py:189+) has been expecting this
    # property; pre-Pt9b only ~1.2% of Entity nodes had it populated via
    # resolve_facets heuristic name-matching. LLM emission lights up the
    # remaining 98.8%.
    object_kind: str = ""


@dataclass
class RelationItem:
    subject: str
    predicate: str
    object: str
    object_kind: str  # entity | literal
    confidence: float
    evidence_phrase: str = ""
    relation_cue: str = ""
    source_predicate: str | None = None
    validation_status: str | None = None


FactType = Literal[
    "property",
    "status",
    "timestamp",
    "threshold",
    "category",
    "tag",
    "rule_condition",
    "rule_action",
]


@dataclass
class FactItem:
    subject: str
    fact_type: FactType
    property_name: str
    value: str
    unit: str | None
    condition: str | None
    confidence: float
    evidence_phrase: str


@dataclass
class ExtractionResult:
    schema_version: str
    chunk_id: str
    doc_id: str
    corpus_id: str
    entities: list[EntityItem] = field(default_factory=list)
    relations: list[RelationItem] = field(default_factory=list)
    facts: list[FactItem] = field(default_factory=list)

    # Pt 10b — original chunk text. Carried alongside the structured
    # extraction output so that neo4j_writer.resolve_ontology_metadata can
    # use it as `text_context` for taxonomy synonym matching. Without this
    # field, the three call sites in neo4j_writer.py defaulted `text_context`
    # to "" and the haystack-based synonym matching in resolve_facets /
    # resolve_domain_type / resolve_canonical_family fell through for ~99%
    # of entities (only exact-known-name hits + filename-extension shortcuts
    # ever populated object_kind / domain_type / canonical_family).
    text: str = ""

    # Phase 14 observability counters (per-chunk; aggregated at corpus level later).
    entity_remap_count: int = 0   # soft mode: entity_type → 'other'
    entity_drop_count: int = 0    # hard mode: entity dropped
    relation_remap_count: int = 0  # soft mode: predicate → 'related_to'
    relation_drop_count: int = 0   # hard mode: relation dropped
    domain_range_remap_count: int = 0  # soft domain/range mismatch → related_to
    domain_range_warn_count: int = 0  # soft domain/range mismatch kept with warning
    endpoint_completion_count: int = 0  # missing relation endpoints added as entities
    evidence_cue_repair_count: int = 0  # evidence phrase repaired predicate/direction
    semantic_direction_repair_count: int = 0
    semantic_direction_drop_count: int = 0
    entity_evidence_drop_count: int = 0
    citation_drop_count: int = 0
    strict_entity_drop_count: int = 0
    strict_relation_drop_count: int = 0
    # Phase B evidence gate — relations whose evidence_phrase is empty,
    # missing, or not a substring of the chunk text get dropped before
    # they reach Mongo / Neo4j. The counter exposes the rejection rate at
    # `ghost_b_metrics.evidence_drop_count` (corpus-level sum) so we can
    # see how often the model invents a phrase for an otherwise-valid
    # relation.
    evidence_drop_count: int = 0
    fact_drop_count: int = 0
    schema_lens_id: str | None = None

    @property
    def validation_rejection_count(self) -> int:
        """Total emitted items rejected by promotion gates.

        This intentionally excludes deterministic repairs/remaps that still
        produce a schema-valid promoted item. It counts only items dropped by
        evidence, citation, schema, strict Pydantic, or fact gates.
        """
        return (
            self.entity_drop_count
            + self.relation_drop_count
            + self.entity_evidence_drop_count
            + self.citation_drop_count
            + self.strict_entity_drop_count
            + self.strict_relation_drop_count
            + self.semantic_direction_drop_count
            + self.evidence_drop_count
            + self.fact_drop_count
        )


@dataclass
class ExtractionFailureItem:
    chunk_id: str
    doc_id: str
    corpus_id: str
    model: str
    lane: int
    attempts: int
    error_type: str
    error_message: str


@dataclass
class ExtractionBatchReport:
    results: list[ExtractionResult]
    failures: list[ExtractionFailureItem]
    metrics: dict


@dataclass(frozen=True)
class ExtractionAttemptProfile:
    name: str
    max_tokens: int
    max_entities: int
    max_relations: int
    max_total_lines: int
    enable_facts: bool
    max_facts: int


def _raw_output_fingerprint(
    raw: str,
    *,
    first_chars: int = 200,
    last_chars: int = 400,
) -> dict[str, Any]:
    text = str(raw or "")
    encoded = text.encode("utf-8", errors="replace")
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "chars": len(text),
        "first": text[: max(0, first_chars)],
        "last": text[-max(0, last_chars):] if last_chars > 0 else "",
    }


def _jsonl_item_counts(items: list[dict[str, Any]] | None) -> dict[str, int]:
    counts = {"entities": 0, "relations": 0, "facts": 0}
    for item in items or []:
        item_type = _jsonl_type(item)
        if item_type == "e":
            counts["entities"] += 1
        elif item_type == "r":
            counts["relations"] += 1
        elif item_type == "f":
            counts["facts"] += 1
    return counts


def _result_counts(result: ExtractionResult | None) -> dict[str, int]:
    if result is None:
        return {"entities": 0, "relations": 0, "facts": 0}
    return {
        "entities": len(result.entities),
        "relations": len(result.relations),
        "facts": len(getattr(result, "facts", []) or []),
    }


def _validation_rejection_count(result: ExtractionResult | None) -> int:
    if result is None:
        return 0
    return int(getattr(result, "validation_rejection_count", 0) or 0)


async def _emit_ghost_b_audit_event(
    sink: GhostBAuditSink | None,
    event: dict[str, Any],
) -> None:
    if sink is None:
        return
    try:
        await sink(event)
    except Exception as exc:
        logger.warning("Ghost B audit event sink failed: %s", exc)


def _cap_jsonl_items(items: list[dict], max_items: int) -> tuple[list[dict], bool]:
    if max_items <= 0 or len(items) <= max_items:
        return items, False
    return items[:max_items], True


def _cap_result(
    result: ExtractionResult | None,
    profile: ExtractionAttemptProfile,
) -> ExtractionResult | None:
    if result is None:
        return None
    result.entities = result.entities[: profile.max_entities]
    result.relations = result.relations[: profile.max_relations]
    result.facts = result.facts[: profile.max_facts] if profile.enable_facts else []
    return result


def _result_has_items(result: ExtractionResult | None) -> bool:
    if result is None:
        return False
    return bool(result.entities or result.relations or result.facts)


def _json_object_claims_items(raw: str) -> bool:
    """Return True when a JSON-object response claimed graph items pre-validation."""
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    return any(bool(data.get(key)) for key in ("entities", "relations", "facts"))


def summarize_extraction_batch(
    *,
    total_chunks: int,
    results: list[ExtractionResult],
    failures: list[ExtractionFailureItem],
    call_metrics: list[dict],
    models: list[str],
) -> dict:
    """Return compact extraction metrics suitable for Mongo/audit surfaces."""
    total_tokens = sum(int(m.get("total_tokens") or 0) for m in call_metrics)
    prompt_tokens = sum(int(m.get("prompt_tokens") or 0) for m in call_metrics)
    completion_tokens = sum(int(m.get("completion_tokens") or 0) for m in call_metrics)
    total_duration = sum(float(m.get("duration_seconds") or 0.0) for m in call_metrics)
    relation_count = sum(len(r.relations) for r in results)
    related_to_count = sum(
        1
        for r in results
        for rel in r.relations
        if rel.predicate == "related_to"
    )
    lens_ids = sorted({r.schema_lens_id for r in results if r.schema_lens_id})
    error_counts: dict[str, int] = {}
    for failure in failures:
        error_counts[failure.error_type] = error_counts.get(failure.error_type, 0) + 1
    lane_call_counts: dict[str, int] = {}
    provider_call_counts: dict[str, int] = {}
    model_call_counts: dict[str, int] = {}
    routing_policies: set[str] = set()
    for metric in call_metrics:
        lane_key = str(metric.get("lane", "unknown"))
        lane_call_counts[lane_key] = lane_call_counts.get(lane_key, 0) + 1
        provider = str(metric.get("provider") or "unknown")
        provider_call_counts[provider] = provider_call_counts.get(provider, 0) + 1
        model = str(metric.get("model") or "unknown")
        model_call_counts[model] = model_call_counts.get(model, 0) + 1
        policy = str(metric.get("routing_policy") or "").strip()
        if policy:
            routing_policies.add(policy)
    ordered_policies = sorted(routing_policies)
    return {
        "requested_chunks": total_chunks,
        "extracted_chunks": len(results),
        "failed_chunks": len(failures),
        "success_rate": round(len(results) / total_chunks, 4) if total_chunks else 1.0,
        "attempt_count": len(call_metrics),
        "models": models,
        "routing_policy": (
            ordered_policies[0]
            if len(ordered_policies) == 1
            else ordered_policies
        ),
        "lane_call_counts": lane_call_counts,
        "provider_call_counts": provider_call_counts,
        "model_call_counts": model_call_counts,
        "total_tokens": total_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_duration_seconds": round(total_duration, 3),
        "entity_count": sum(len(r.entities) for r in results),
        "relation_count": relation_count,
        "fact_count": sum(len(getattr(r, "facts", []) or []) for r in results),
        "related_to_count": related_to_count,
        "related_to_ratio": round(related_to_count / relation_count, 4) if relation_count else 0.0,
        "entity_remap_count": sum(r.entity_remap_count for r in results),
        "relation_remap_count": sum(r.relation_remap_count for r in results),
        "domain_range_remap_count": sum(r.domain_range_remap_count for r in results),
        "domain_range_warn_count": sum(r.domain_range_warn_count for r in results),
        "endpoint_completion_count": sum(r.endpoint_completion_count for r in results),
        "evidence_cue_repair_count": sum(r.evidence_cue_repair_count for r in results),
        "evidence_drop_count": sum(r.evidence_drop_count for r in results),
        "fact_drop_count": sum(getattr(r, "fact_drop_count", 0) for r in results),
        "entity_evidence_drop_count": sum(
            getattr(r, "entity_evidence_drop_count", 0) for r in results
        ),
        "citation_drop_count": sum(getattr(r, "citation_drop_count", 0) for r in results),
        "strict_entity_drop_count": sum(
            getattr(r, "strict_entity_drop_count", 0) for r in results
        ),
        "strict_relation_drop_count": sum(
            getattr(r, "strict_relation_drop_count", 0) for r in results
        ),
        "validation_rejection_count": sum(
            _validation_rejection_count(r) for r in results
        ),
        "entity_drop_count": sum(r.entity_drop_count for r in results),
        "relation_drop_count": sum(r.relation_drop_count for r in results),
        "schema_lens_ids": lens_ids,
        "error_counts": error_counts,
    }


def _entity_key(name: str) -> str:
    return " ".join(str(name or "").lower().split())


_OBJECT_KIND_PAREN_RE = re.compile(r"\s*\([^)]*\)")


# Pt10a — bibliographic citation detector. Drops entities that look like
# "Author1, Author2 and Author3. Title. Publisher, Year." which Ghost B
# was extracting as Person (or worse: Document) when an LLM hit a
# bibliography page or footnote. Verified failure on Phase5_Luau_v4:
# the Fowler book's references section produced entities like
# "Alpert, Brown and Woolf. Design Patterns Smalltalk Companion.
# Addison-Wesley, 1998." typed as Person.
#
# Heuristic (only fires when ALL three conditions hit, so legitimate
# entities like a Person named "Foo Bar (1998)" don't get caught):
#   1. Contains a 4-digit year in 1900-2099 range
#   2. Either matches a publisher keyword OR is >= 8 words long
#   3. Has at least one period or comma (citation punctuation)
#
# The publisher list is intentionally tiny and high-confidence — adding
# more requires a per-publisher false-positive audit. Generic terms like
# "Press" or "Books" deliberately excluded.
_CITATION_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_CITATION_PUBLISHERS = frozenset([
    "addison-wesley", "addison wesley",
    "o'reilly", "oreilly", "o reilly",
    "manning publications", "manning",
    "wiley", "springer", "packt", "apress",
    "mit press", "cambridge university press",
    "oxford university press", "no starch press",
    "morgan kaufmann", "prentice hall",
    "mcgraw hill", "mcgraw-hill",
    "wrox", "pragmatic bookshelf",
])


def _looks_like_citation(canonical_name: str, surface_form: str = "") -> bool:
    """Pt10a — return True if the entity name pattern-matches a
    bibliographic citation. Used to drop these before they pollute
    the graph as Person / Document / Concept entities.

    Defensive: returns False (do NOT drop) on any uncertainty. False
    negatives are recoverable (a few citation entities slip through);
    false positives lose real entities.
    """
    name = (canonical_name or "").strip()
    if not name:
        return False
    text = f"{name} {surface_form or ''}".lower()
    if not _CITATION_YEAR_RE.search(text):
        return False
    has_punct = "." in name or "," in name
    if not has_punct:
        return False
    long_enough = len(name.split()) >= 8
    has_publisher = any(pub in text for pub in _CITATION_PUBLISHERS)
    return has_publisher or long_enough


def _normalize_object_kind(raw: str, canon: list[str] | None) -> str:
    """Pt9b — canonicalize the LLM-emitted entity object_kind facet.

    LLMs are not consistent: "library", "Library", "lib", "library (python)",
    "Library (Python)", "JS library" all describe the same thing. Without a
    normalizer, downstream queries like
    `MATCH (e:Entity) WHERE e.object_kind = "library"` miss most of these.

    Strategy:
      1. Lowercase + strip parenthetical qualifiers.
      2. Exact match against the corpus's canonical object_kinds list
         (populated by SchemaLens from `_DOMAIN_RULES`). Return the canonical
         spelling so all variants converge.
      3. Prefix match — "javascript library" → "library" if "library" is in
         the canonical list.
      4. Substring match — "open-source library" → "library" if "library"
         occurs anywhere in the cleaned string AND in the canonical list.
      5. Fallback: return the cleaned string, bounded to 100 chars. This
         lets rare-but-valid kinds pass through; the schema_lens widens
         over time via deterministic merge.

    `canon` is None or empty → cleaned pass-through (no canonical list yet,
    e.g., first ingest before SchemaLens is built).
    """
    cleaned = _OBJECT_KIND_PAREN_RE.sub("", str(raw or "")).strip().lower()
    if not cleaned:
        return ""
    if not canon:
        return cleaned[:100]
    canon_lower = {str(c).lower(): str(c) for c in canon if str(c).strip()}
    # Exact match first.
    if cleaned in canon_lower:
        return canon_lower[cleaned]
    # Prefix match — "javascript library" starts with "library"?
    # We want the REVERSE — "library" is a prefix of "library (python)".
    # So check whether cleaned STARTS WITH any canon term.
    for term_lc, term in canon_lower.items():
        if cleaned.startswith(term_lc + " ") or cleaned.startswith(term_lc + "-"):
            return term
    # Substring match — "open-source library" contains "library".
    for term_lc, term in canon_lower.items():
        if f" {term_lc} " in f" {cleaned} ":
            return term
    # Pass-through bounded.
    return cleaned[:100]


def _append_validation_status(existing: str | None, status: str) -> str:
    if not existing:
        return status
    parts = [part for part in str(existing).split("+") if part]
    if status not in parts:
        parts.append(status)
    return "+".join(parts)


def _infer_endpoint_entity_type(name: str, relation: RelationItem, *, role: str) -> str:
    """Infer a broad universal type for a relation endpoint the LLM referenced
    but forgot to include in `entities`.

    This is intentionally conservative: it only uses the approved universal
    entity types and aims to keep domain/range validation from erasing a good
    predicate simply because an endpoint was omitted from the entity array.
    """
    text = _entity_key(name)
    predicate = relation.predicate

    if not text:
        return SchemaContext.ENTITY_SENTINEL

    if text in {"user", "reader", "customer", "developer", "admin"}:
        return "Person"
    if any(term in text for term in ("openai", "google", "apple", "firebase", "council")):
        return "Organization"
    if any(term in text for term in ("limit", "rule", "constraint", "gate", "policy", "requirement")):
        return "Rule"
    if any(term in text for term in ("report", "book", "document", "whitepaper", "guide")):
        return "Document"
    if any(term in text for term in ("sprint", "milestone", "phase", "session", "event")):
        return "Event"
    if any(term in text for term in (
        "api", "model", "llm", "gemma", "llama", "qwen", "tensorflow", "ml kit",
        "sqlite", "database", "firebase", "android", "ios", "device", "gpu",
        "rtx", "nnapi", "platform", "service", "plugin", "sdk", "library",
        "framework", "app",
    )):
        return "Product"
    if any(term in text for term in (
        "pipeline", "flow", "process", "extraction", "generation", "training",
        "classification", "detection", "inference", "router", "architecture",
    )):
        return "Method"
    if any(term in text for term in (
        "card", "button", "screen", "drawer", "interface", "ui", "view",
        "controller", "prompt", "json", "snapshot", "profile", "artifact",
        "file", "chunk", "module", "stack", "dataset", "data", "gguf",
    )):
        return "Artifact"

    if role == "subject" and predicate in {
        "uses", "stores", "detects", "classifies", "supports",
    }:
        return "Method"
    if role == "object" and predicate in {"uses", "runs_on", "stores"}:
        return "Product"
    if predicate in {"part_of", "references", "depends_on", "represents", "maps_to"}:
        return "Concept"
    return "Concept"


def _complete_relation_endpoint_entities(
    entities: list[EntityItem],
    relations: list[RelationItem],
    counters: dict[str, int],
) -> list[EntityItem]:
    """Add entity endpoints that relations reference but the LLM omitted.

    Ghost B sometimes emits a good relation (`model uses Unsloth`) while
    forgetting to include the generic endpoint (`model`) in the entity list.
    Adding a broad, low-confidence entity here preserves the relation's
    semantic predicate without asking for another LLM call.
    """
    out = list(entities)
    seen = {_entity_key(e.canonical_name) for e in out if e.canonical_name}

    def add_endpoint(name: str, relation: RelationItem, role: str) -> None:
        key = _entity_key(name)
        if not key or key in seen:
            return
        inferred = _infer_endpoint_entity_type(name, relation, role=role)
        out.append(
            EntityItem(
                canonical_name=key,
                surface_form=str(name),
                entity_type=inferred,
                confidence=min(max(float(relation.confidence), 0.0), 0.75),
            )
        )
        seen.add(key)
        counters["endpoint_completion_count"] += 1

    for relation in relations:
        if relation.object_kind != "entity":
            continue
        add_endpoint(relation.subject, relation, "subject")
        add_endpoint(relation.object, relation, "object")
    return out


def _canonical_entity_type(entity_type: str | None, allowed: set[str]) -> str | None:
    """Return schema-canonical entity type casing when the value is a case variant."""
    raw = str(entity_type or "").strip()
    if not raw:
        return None
    if raw in allowed:
        return raw
    by_lower = {value.lower(): value for value in allowed}
    return by_lower.get(raw.lower())


def _looks_like_storage_target(name: str) -> bool:
    text = _entity_key(name)
    return any(term in text for term in (
        "sqlite", "database", "db", "cache", "kv cache", "kv-cache",
        "storage", "store", "filesystem", "file system",
    ))


def _looks_like_runtime_target(name: str) -> bool:
    text = _entity_key(name)
    return any(term in text for term in (
        "android", "ios", "iphone", "samsung", "device", "gpu", "rtx",
        "nnapi", "npu", "cpu", "tpu", "executorch", "llama cpp",
        "llama_cpp", "flutter", "browser", "server", "runtime",
        "platform",
    ))


def _relation_domain_range_ok(
    relation: RelationItem,
    name_to_type: dict[str, str],
    dr_map: dict[str, dict[str, list[str]]] = DOMAIN_RANGE_MAP,
) -> bool:
    constraints = dr_map.get(relation.predicate)
    if not constraints or relation.object_kind != "entity":
        return True
    subject_type = name_to_type.get(_entity_key(relation.subject), SchemaContext.ENTITY_SENTINEL)
    object_type = name_to_type.get(_entity_key(relation.object), SchemaContext.ENTITY_SENTINEL)
    subject_ok = subject_type in constraints.get("subject_types", [])
    object_ok = object_type in constraints.get("object_types", [])
    return subject_ok and object_ok


def _relation_with_remap(
    relation: RelationItem,
    *,
    validation_status: str,
    predicate: str = SchemaContext.RELATION_SENTINEL,
) -> RelationItem:
    """Return a soft-remapped relation while preserving the LLM's original intent.

    The graph writer can later repair the edge using ontology facets or the
    extracted evidence phrase. Without this field, a domain/range mismatch
    permanently erases whether the model originally meant `uses`, `runs_on`,
    `trained_on`, etc.
    """
    return RelationItem(
        subject=relation.subject,
        predicate=predicate,
        object=relation.object,
        object_kind=relation.object_kind,
        confidence=relation.confidence,
        evidence_phrase=relation.evidence_phrase,
        relation_cue=relation.relation_cue,
        source_predicate=relation.source_predicate or relation.predicate,
        validation_status=validation_status,
    )


def _relation_with_predicate(
    relation: RelationItem,
    predicate: str,
    *,
    reverse: bool = False,
    validation_status: str | None = None,
) -> RelationItem:
    subject = relation.object if reverse and relation.object_kind == "entity" else relation.subject
    obj = relation.subject if reverse and relation.object_kind == "entity" else relation.object
    return RelationItem(
        subject=subject,
        predicate=predicate,
        object=obj,
        object_kind=relation.object_kind,
        confidence=relation.confidence,
        evidence_phrase=relation.evidence_phrase,
        relation_cue=relation.relation_cue,
        source_predicate=relation.source_predicate or relation.predicate,
        validation_status=validation_status or relation.validation_status,
    )


def _repair_relation_from_evidence(
    relation: RelationItem,
    counters: dict[str, int],
) -> RelationItem:
    if relation.object_kind != "entity":
        return relation
    evidence = f"{relation.evidence_phrase} {relation.relation_cue}".lower()
    if not evidence.strip():
        return relation
    for pattern, predicate, reverse in EVIDENCE_CUE_RULES:
        if not pattern.search(evidence):
            continue
        if predicate == "stores" and reverse and not _looks_like_storage_target(relation.object):
            continue
        if predicate == "runs_on" and not _looks_like_runtime_target(relation.object):
            continue
        if predicate == relation.predicate and not reverse:
            return relation
        counters["evidence_cue_repair_count"] += 1
        return _relation_with_predicate(
            relation,
            predicate,
            reverse=reverse,
            validation_status=_append_validation_status(
                relation.validation_status, "evidence_cue_repair"
            ),
        )
    return relation


def _relation_with_domain_warning(relation: RelationItem) -> RelationItem:
    return RelationItem(
        subject=relation.subject,
        predicate=relation.predicate,
        object=relation.object,
        object_kind=relation.object_kind,
        confidence=relation.confidence,
        evidence_phrase=relation.evidence_phrase,
        relation_cue=relation.relation_cue,
        source_predicate=relation.source_predicate or relation.predicate,
        validation_status=_append_validation_status(
            relation.validation_status, "domain_range_warn"
        ),
    )


def _should_warn_domain_range(relation: RelationItem) -> bool:
    """Keep an evidence-backed predicate and mark it thin instead of erasing it.

    `related_to` should represent genuinely unclear semantics. A clear
    evidence phrase plus an approved predicate is more useful as a warned
    `uses`/`stores`/`part_of` edge than as a strong-looking catchall.
    """
    if relation.predicate == SchemaContext.RELATION_SENTINEL:
        return False
    if relation.object_kind != "entity":
        return False
    if not str(relation.evidence_phrase or "").strip():
        return False
    return relation.confidence >= 0.55


def _apply_domain_range(
    entities: list[EntityItem],
    relations: list[RelationItem],
    counters: dict[str, int],
) -> list[RelationItem]:
    name_to_type = {
        _entity_key(entity.canonical_name): entity.entity_type
        for entity in entities
    }
    out: list[RelationItem] = []
    for relation in relations:
        if _relation_domain_range_ok(relation, name_to_type):
            out.append(relation)
            continue
        if _should_warn_domain_range(relation):
            out.append(_relation_with_domain_warning(relation))
            counters["domain_range_warn_count"] += 1
            logger.debug(
                "GHOST B domain/range warn %r kept (subject=%r object=%r)",
                relation.predicate,
                relation.subject,
                relation.object,
            )
            continue
        out.append(_relation_with_remap(relation, validation_status="domain_range_mismatch"))
        counters["domain_range_remap_count"] += 1
        logger.debug(
            "GHOST B domain/range remap %r -> %r (subject=%r object=%r)",
            relation.predicate,
            SchemaContext.RELATION_SENTINEL,
            relation.subject,
            relation.object,
        )
    return out


_CREATOR_ENTITY_TYPES = {"Person", "Organization"}
_CREATED_ENTITY_TYPES = {
    "Product",
    "Software",
    "Document",
    "Standard",
    "Rule",
    "Artifact",
    "Method",
    "Concept",
    "Event",
    "Organization",
}
_EMPLOYER_ENTITY_TYPES = {"Organization"}
_OWNED_ENTITY_TYPES = {
    "Product",
    "Software",
    "Document",
    "Artifact",
    "Standard",
    "Location",
}


def _apply_semantic_direction_checks(
    entities: list[EntityItem],
    relations: list[RelationItem],
    counters: dict[str, int],
) -> list[RelationItem]:
    """Repair obvious subject/object reversals before graph promotion.

    The goal is not ontology perfection. It is a deterministic gate for the
    common LLM mistake where the predicate is correct but the arrow is flipped:
    "Alice created Widget" emitted as Alice -created_by-> Widget. Ambiguous
    cases fall through to the existing domain/range gate instead of guessing.
    """

    name_to_type = {
        _entity_key(entity.canonical_name): entity.entity_type
        for entity in entities
    }
    out: list[RelationItem] = []
    for relation in relations:
        if relation.object_kind != "entity":
            out.append(relation)
            continue
        subject_type = name_to_type.get(_entity_key(relation.subject))
        object_type = name_to_type.get(_entity_key(relation.object))

        should_reverse = False
        if relation.predicate == "created_by":
            should_reverse = (
                subject_type in _CREATOR_ENTITY_TYPES
                and object_type in _CREATED_ENTITY_TYPES
            )
        elif relation.predicate == "works_for":
            should_reverse = (
                subject_type in _EMPLOYER_ENTITY_TYPES
                and object_type == "Person"
            )
        elif relation.predicate == "owns":
            should_reverse = (
                subject_type in _OWNED_ENTITY_TYPES
                and object_type in _CREATOR_ENTITY_TYPES
            )

        if should_reverse:
            counters["semantic_direction_repair_count"] += 1
            repaired = _relation_with_predicate(
                relation,
                relation.predicate,
                reverse=True,
                validation_status=_append_validation_status(
                    relation.validation_status, "semantic_direction_repair"
                ),
            )
            logger.debug(
                "GHOST B semantic direction repair %r subject=%r object=%r -> subject=%r object=%r",
                relation.predicate,
                relation.subject,
                relation.object,
                repaired.subject,
                repaired.object,
            )
            out.append(repaired)
            continue
        out.append(relation)
    return out


def _apply_schema(
    entities: list[EntityItem],
    relations: list[RelationItem],
    schema: SchemaContext | None,
) -> tuple[list[EntityItem], list[RelationItem], dict[str, int]]:
    """Apply Phase 14 schema_strict semantics. Returns (entities, relations, counters).

    Counters: entity_remap_count, entity_drop_count, relation_remap_count, relation_drop_count.
    """
    counters = {
        "entity_remap_count": 0,
        "entity_drop_count": 0,
        "relation_remap_count": 0,
        "relation_drop_count": 0,
        "domain_range_remap_count": 0,
        "domain_range_warn_count": 0,
        "endpoint_completion_count": 0,
        "evidence_cue_repair_count": 0,
        "semantic_direction_repair_count": 0,
        "semantic_direction_drop_count": 0,
    }

    # No schema or strict='off' → pass-through
    if schema is None or schema.strict == "off":
        return entities, relations, counters
    if not (schema.has_entity_schema or schema.has_relation_schema):
        return entities, relations, counters

    # Entities
    out_entities: list[EntityItem] = []
    if schema.has_entity_schema:
        allowed = set(schema.entity_vocab)
        for e in entities:
            canonical_type = _canonical_entity_type(e.entity_type, allowed)
            if canonical_type:
                out_entities.append(
                    replace(e, entity_type=canonical_type)
                    if canonical_type != e.entity_type
                    else e
                )
            elif schema.strict == "soft":
                out_entities.append(
                    EntityItem(
                        canonical_name=e.canonical_name,
                        surface_form=e.surface_form,
                        entity_type=SchemaContext.ENTITY_SENTINEL,
                        confidence=e.confidence,
                        # Pt 10c — preserve query-facing fields across the
                        # soft-remap (only entity_type is being rewritten).
                        query_aliases=list(getattr(e, "query_aliases", []) or []),
                        definitional_phrase=getattr(e, "definitional_phrase", "") or "",
                        # Pt9b — preserve object_kind across soft-remap.
                        # entity_type went to sentinel; object_kind still
                        # holds usable specificity (e.g. "library").
                        object_kind=getattr(e, "object_kind", "") or "",
                    )
                )
                counters["entity_remap_count"] += 1
                logger.debug(
                    "GHOST B remap entity_type %r -> %r (canonical=%r)",
                    e.entity_type,
                    SchemaContext.ENTITY_SENTINEL,
                    e.canonical_name,
                )
            else:  # hard
                counters["entity_drop_count"] += 1
                logger.debug(
                    "GHOST B drop entity_type %r (canonical=%r) — hard mode",
                    e.entity_type,
                    e.canonical_name,
                )
    else:
        out_entities = entities

    # Relations
    out_relations: list[RelationItem] = []
    if schema.has_relation_schema:
        allowed = set(schema.relation_vocab)
        drop_unknown_relations = bool(
            getattr(get_settings(), "EXTRACTION_DROP_UNKNOWN_RELATIONS", True)
        )
        for r in relations:
            normalized_predicate, reverse = normalize_relation_predicate_alias(r.predicate)
            candidate = (
                _relation_with_predicate(
                    r,
                    normalized_predicate,
                    reverse=reverse,
                    validation_status="schema_predicate_alias",
                )
                if normalized_predicate != r.predicate or reverse
                else r
            )
            if candidate.predicate in allowed:
                out_relations.append(candidate)
            elif schema.strict == "soft":
                if drop_unknown_relations:
                    counters["relation_drop_count"] += 1
                    logger.debug(
                        "GHOST B drop unknown predicate %r "
                        "(subject=%r object=%r) — production gate",
                        r.predicate,
                        r.subject,
                        r.object,
                    )
                else:
                    out_relations.append(
                        _relation_with_remap(
                            r, validation_status="schema_predicate_remap"
                        )
                    )
                    counters["relation_remap_count"] += 1
                    logger.debug(
                        "GHOST B remap predicate %r -> %r (subject=%r object=%r)",
                        r.predicate,
                        SchemaContext.RELATION_SENTINEL,
                        r.subject,
                        r.object,
                    )
            else:  # hard
                counters["relation_drop_count"] += 1
                logger.debug(
                    "GHOST B drop predicate %r (subject=%r object=%r) — hard mode",
                    r.predicate,
                    r.subject,
                    r.object,
                )
    else:
        out_relations = relations

    out_relations = [
        _repair_relation_from_evidence(relation, counters)
        for relation in out_relations
    ]
    out_relations = _apply_semantic_direction_checks(
        out_entities, out_relations, counters
    )
    out_entities = _complete_relation_endpoint_entities(
        out_entities, out_relations, counters
    )
    out_relations = _apply_domain_range(out_entities, out_relations, counters)
    return out_entities, out_relations, counters


def _parse_facts(
    raw_facts: Any,
    *,
    task: ExtractionTask,
    threshold: float,
    entity_names: set[str],
    max_facts: int,
) -> tuple[list[FactItem], int]:
    """Validate optional structured facts without risking the chunk result."""
    if max_facts <= 0:
        return [], 0
    if raw_facts in (None, ""):
        return [], 0
    if not isinstance(raw_facts, list):
        return [], 1

    facts: list[FactItem] = []
    dropped = 0
    allowed_types = set(FACT_TYPES)
    for item in raw_facts:
        if len(facts) >= max_facts:
            break
        if not isinstance(item, dict):
            dropped += 1
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            dropped += 1
            continue
        if confidence < threshold:
            dropped += 1
            continue

        subject = _entity_key(item.get("subject") or "")
        fact_type = str(item.get("fact_type") or "").strip()
        property_name = _entity_key(item.get("property_name") or "").replace(" ", "_")
        value = str(item.get("value") or "").strip()
        evidence_phrase = str(
            item.get("evidence_phrase") or item.get("evidence") or ""
        ).strip()[:500]

        if (
            not subject
            or subject not in entity_names
            or fact_type not in allowed_types
            or not property_name
            or not value
            or not _validate_evidence(evidence_phrase, task.text)
        ):
            dropped += 1
            continue

        unit_raw = item.get("unit")
        condition_raw = item.get("condition")
        facts.append(
            FactItem(
                subject=subject,
                fact_type=fact_type,  # type: ignore[arg-type]
                property_name=property_name[:120],
                value=value[:500],
                unit=str(unit_raw).strip()[:80] if unit_raw not in (None, "") else None,
                condition=(
                    str(condition_raw).strip()[:300]
                    if condition_raw not in (None, "")
                    else None
                ),
                confidence=confidence,
                evidence_phrase=evidence_phrase,
            )
        )
    return facts, dropped


def _parse(
    raw: str,
    task: ExtractionTask,
    threshold: float,
    schema: SchemaContext | None = None,
    schema_lens: SchemaLens | dict | None = None,
    *,
    enable_facts: bool | None = None,
    max_facts: int | None = None,
) -> ExtractionResult | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("GHOST B JSON parse failed chunk_id=%s: %s", task.chunk_id, exc)
        return None

    entities: list[EntityItem] = []
    for e in data.get("entities", []):
        if e.get("confidence", 0.0) < threshold:
            continue
        try:
            # Pt 10c — query_aliases + definitional_phrase. Defensive coercion
            # for the legacy JSON parser (the JSONL parser at line ~2477
            # already normalizes these into the dict, so values arrive
            # well-shaped here; legacy JSON output may carry them raw).
            qa_raw = e.get("query_aliases") or []
            if isinstance(qa_raw, str):
                qa_list = [qa_raw.strip()] if qa_raw.strip() else []
            elif isinstance(qa_raw, list):
                qa_list = [str(a).strip() for a in qa_raw if str(a).strip()][:5]
            else:
                qa_list = []
            # Pt9b — object_kind read tolerant of both wire-format keys.
            # JSON_OBJECT prompts emit "object_kind"; JSONL prompts emit "ek"
            # (entity-kind) to disambiguate from RelationItem.object_kind
            # which carries different semantics ("entity"|"literal").
            # _jsonl_items_to_object already translates "ek" → "object_kind"
            # in the dict it builds, so reading "object_kind" here is enough.
            lens_kinds = None
            if schema_lens is not None:
                lens_kinds = (
                    schema_lens.object_kinds
                    if isinstance(schema_lens, SchemaLens)
                    else (schema_lens.get("object_kinds") if isinstance(schema_lens, dict) else None)
                )
            entities.append(
                EntityItem(
                    canonical_name=e["canonical_name"],
                    surface_form=e.get("surface_form", e["canonical_name"]),
                    entity_type=e.get("entity_type", "other"),
                    confidence=float(e["confidence"]),
                    query_aliases=qa_list,
                    definitional_phrase=str(e.get("definitional_phrase", "") or "").strip()[:200],
                    object_kind=_normalize_object_kind(
                        e.get("object_kind") or e.get("e_kind", ""),
                        lens_kinds,
                    ),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    relations: list[RelationItem] = []
    for r in data.get("relations", []):
        if r.get("confidence", 0.0) < threshold:
            continue
        try:
            relations.append(
                RelationItem(
                    subject=r["subject"],
                    predicate=r["predicate"],
                    object=r["object"],
                    object_kind=r.get("object_kind", "literal"),
                    confidence=float(r["confidence"]),
                    evidence_phrase=str(r.get("evidence_phrase") or r.get("evidence") or "")[:500],
                    relation_cue=str(r.get("relation_cue") or "")[:120],
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    entities, relations, counters = _apply_schema(entities, relations, schema)

    # Pt 7f — Entity evidence gate. Mirrors the Phase B relation gate just
    # below: drop any entity whose `surface_form` (the LLM's claim about
    # what literal phrase it saw) doesn't actually appear in `task.text`
    # after lowercase + whitespace-collapsed comparison. This catches the
    # dominant specific-name hallucination class — e.g. DeepSeek inventing
    # "Netflix" / "Yelp" / "eBay" at confidence 0.95 in a chunk that only
    # discusses Amazon. Falls back to `canonical_name` when `surface_form`
    # is empty (some legacy paths leave it blank). Dropped entities
    # cascade through `neo4j_writer.write_document_graph`, which already
    # skips MENTIONS / RELATES_TO rows whose endpoint isn't in
    # `entity_identity`, so no downstream change is required.
    kept_entities: list[EntityItem] = []
    entity_evidence_drop_count = 0
    citation_drop_count = 0
    for e in entities:
        surface = e.surface_form or e.canonical_name
        # Pt10a — bibliographic citation gate. Drops entities that look
        # like "Author. Title. Publisher, Year." regardless of evidence
        # validation. The citation MAY pass evidence (the LLM copied
        # verbatim from a references page) but is still pollution.
        if _looks_like_citation(e.canonical_name, surface):
            citation_drop_count += 1
            logger.warning(
                "GHOST B citation gate dropped entity chunk_id=%s name=%r",
                task.chunk_id,
                (e.canonical_name or "")[:60],
            )
            continue
        if _validate_evidence(surface, task.text):
            kept_entities.append(e)
            continue
        entity_evidence_drop_count += 1
        logger.warning(
            "GHOST B evidence gate dropped entity chunk_id=%s name=%r surface=%r",
            task.chunk_id,
            (e.canonical_name or "")[:40],
            (surface or "")[:40],
        )
    entities = kept_entities

    # Phase B evidence-validation gate. Drop any relation whose evidence_phrase
    # is empty / missing OR is not a substring of the chunk text after a
    # case-insensitive whitespace-collapsed comparison. The check is cheap
    # (one regex sub + `in`) and immediately filters the most common failure
    # mode of frontier extractors: hallucinating a paraphrase that sounds
    # plausible but doesn't appear in the source. synonym_of / instance_of
    # self-edges are exempt because canonicalization edges don't always have
    # a textual cue (e.g. "OpenAI" / "openai inc" can be merged on
    # surface-form similarity alone).
    kept_relations: list[RelationItem] = []
    evidence_drop_count = 0
    for r in relations:
        if r.predicate in {"synonym_of", "instance_of"}:
            kept_relations.append(r)
            continue
        if not _validate_evidence(r.evidence_phrase, task.text):
            evidence_drop_count += 1
            logger.warning(
                "GHOST B evidence gate dropped relation chunk_id=%s "
                "predicate=%s subject=%r object=%r evidence=%r",
                task.chunk_id,
                r.predicate,
                r.subject[:40],
                r.object[:40],
                (r.evidence_phrase or "")[:60],
            )
            continue
        kept_relations.append(r)
    relations = kept_relations

    # Pt 8b — strict Pydantic-Literal validation. CORE pipeline step
    # (intrinsic, not flag-gated). Runs AFTER _apply_schema soft-remap,
    # Pt 7f entity evidence gate, and Phase B relation evidence gate.
    # Each entity / relation is validated through
    # services.ghost_b_schemas (Pydantic + Literal types) and DROPPED
    # if it fails — closing the off-schema escape hatch that previously
    # silently demoted ~21% of edges to the `related_to` sentinel. Same
    # tier of guarantee as the Pt 7f and Phase B gates above.
    from services.ghost_b_schemas import ExtractionResponse, LLMEntity, LLMRelation
    from pydantic import ValidationError

    strict_entities: list[EntityItem] = []
    strict_entity_drops = 0
    for e in entities:
        canonical_type = _canonical_entity_type(
            e.entity_type,
            set(UNIVERSAL_ENTITY_SCHEMA) | {SchemaContext.ENTITY_SENTINEL},
        )
        candidate = (
            replace(e, entity_type=canonical_type)
            if canonical_type and canonical_type != e.entity_type
            else e
        )
        try:
            LLMEntity(
                canonical_name=candidate.canonical_name,
                surface_form=candidate.surface_form or "",
                entity_type=candidate.entity_type,
                confidence=float(candidate.confidence),
                # Pt9b — include object_kind in Pt8b validation. Free-form
                # str field (no Literal), so this validates length only and
                # is bit-for-bit identical to pre-Pt9b validation when the
                # LLM didn't emit one.
                object_kind=getattr(candidate, "object_kind", "") or "",
            )
            strict_entities.append(candidate)
        except ValidationError as ve:
            strict_entity_drops += 1
            logger.warning(
                "GHOST B Pt8b strict-validation dropped entity chunk_id=%s "
                "name=%r type=%r reason=%s",
                task.chunk_id,
                (e.canonical_name or "")[:40],
                candidate.entity_type,
                str(ve)[:200],
            )
    entities = strict_entities

    strict_relations: list[RelationItem] = []
    strict_relation_drops = 0
    for r in relations:
        try:
            LLMRelation(
                subject=r.subject,
                predicate=r.predicate,
                object=r.object,
                object_kind=r.object_kind if r.object_kind in {"entity", "literal"} else "literal",
                confidence=float(r.confidence),
                evidence_phrase=r.evidence_phrase or "",
                relation_cue=r.relation_cue or "",
            )
            strict_relations.append(r)
        except ValidationError as ve:
            strict_relation_drops += 1
            logger.warning(
                "GHOST B Pt8b strict-validation dropped relation chunk_id=%s "
                "predicate=%r subject=%r object=%r reason=%s",
                task.chunk_id,
                r.predicate,
                (r.subject or "")[:30],
                (r.object or "")[:30],
                str(ve)[:200],
            )
    relations = strict_relations
    if strict_entity_drops or strict_relation_drops:
        logger.info(
            "GHOST B Pt8b strict-validation chunk_id=%s entity_drops=%d relation_drops=%d",
            task.chunk_id,
            strict_entity_drops,
            strict_relation_drops,
        )

    settings = get_settings()
    facts_enabled = settings.EXTRACTION_ENABLE_FACTS if enable_facts is None else enable_facts
    fact_cap = (
        settings.EXTRACTION_MAX_FACTS_PER_CHUNK
        if max_facts is None
        else max(0, int(max_facts))
    )
    facts: list[FactItem] = []
    fact_drop_count = 0
    if facts_enabled:
        facts, fact_drop_count = _parse_facts(
            data.get("facts", []),
            task=task,
            threshold=threshold,
            entity_names={_entity_key(e.canonical_name) for e in entities},
            max_facts=fact_cap,
        )

    try:
        ExtractionResponse(
            entities=[
                {
                    "canonical_name": e.canonical_name,
                    "surface_form": e.surface_form or "",
                    "entity_type": e.entity_type,
                    "confidence": float(e.confidence),
                    "query_aliases": list(getattr(e, "query_aliases", []) or []),
                    "definitional_phrase": getattr(e, "definitional_phrase", "") or "",
                    "object_kind": getattr(e, "object_kind", "") or "",
                }
                for e in entities
            ],
            relations=[
                {
                    "subject": r.subject,
                    "predicate": r.predicate,
                    "object": r.object,
                    "object_kind": r.object_kind,
                    "confidence": float(r.confidence),
                    "evidence_phrase": r.evidence_phrase,
                    "relation_cue": r.relation_cue or "",
                }
                for r in relations
            ],
            facts=[
                {
                    "subject": f.subject,
                    "fact_type": f.fact_type,
                    "property_name": f.property_name,
                    "value": f.value,
                    "unit": f.unit or "",
                    "condition": f.condition or "",
                    "confidence": float(f.confidence),
                    "evidence_phrase": f.evidence_phrase,
                }
                for f in facts
            ],
        )
    except ValidationError as ve:
        logger.warning(
            "GHOST B promoted ExtractionResponse validation failed chunk_id=%s reason=%s",
            task.chunk_id,
            str(ve)[:300],
        )
        return None

    lens = (
        schema_lens
        if isinstance(schema_lens, SchemaLens)
        else SchemaLens.from_dict(schema_lens if isinstance(schema_lens, dict) else None)
    )

    return ExtractionResult(
        schema_version=data.get("schema_version", "polymath.extract.v1"),
        chunk_id=task.chunk_id,
        doc_id=task.doc_id,
        corpus_id=task.corpus_id,
        # Pt 10b — preserve the chunk text so the downstream Neo4j writer can
        # feed it to resolve_ontology_metadata for taxonomy synonym matching.
        text=task.text,
        entities=entities,
        relations=relations,
        facts=facts,
        entity_remap_count=counters["entity_remap_count"],
        entity_drop_count=counters["entity_drop_count"],
        relation_remap_count=counters["relation_remap_count"],
        relation_drop_count=counters["relation_drop_count"],
        domain_range_remap_count=counters["domain_range_remap_count"],
        domain_range_warn_count=counters["domain_range_warn_count"],
        endpoint_completion_count=counters["endpoint_completion_count"],
        evidence_cue_repair_count=counters["evidence_cue_repair_count"],
        semantic_direction_repair_count=counters["semantic_direction_repair_count"],
        semantic_direction_drop_count=counters["semantic_direction_drop_count"],
        entity_evidence_drop_count=entity_evidence_drop_count,
        citation_drop_count=citation_drop_count,
        strict_entity_drop_count=strict_entity_drops,
        strict_relation_drop_count=strict_relation_drops,
        evidence_drop_count=evidence_drop_count,
        fact_drop_count=fact_drop_count,
        schema_lens_id=lens.lens_id if lens else None,
    )


@dataclass
class JsonlParseChunk:
    items: list[dict]
    finished: bool = False
    valid_lines: int = 0
    invalid_line: str | None = None


def _jsonl_type(item: dict) -> str:
    return str(item.get("t") or item.get("type") or "").strip().lower()


def _jsonl_confidence(item: dict) -> float:
    try:
        return float(item.get("cf", item.get("confidence", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _compact_jsonl_line(item: dict) -> str:
    return json.dumps(item, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _debug_log_raw_jsonl_lines(
    raw: str,
    *,
    chunk_id: str,
    lane: int,
    attempt: int,
    enabled: bool,
) -> None:
    """Emit exact provider JSONL lines behind an explicit debug flag."""

    if not enabled:
        return
    lines = str(raw or "").splitlines()
    if not lines:
        logger.debug(
            "GHOST B raw JSONL chunk_id=%s lane=%d attempt=%d line=0 raw=''",
            chunk_id,
            lane,
            attempt,
        )
        return
    for line_no, line in enumerate(lines, start=1):
        logger.debug(
            "GHOST B raw JSONL chunk_id=%s lane=%d attempt=%d line=%d raw=%r",
            chunk_id,
            lane,
            attempt,
            line_no,
            line,
        )


def _parse_jsonl_lines(raw: str) -> JsonlParseChunk:
    """Parse complete JSONL records until the first invalid line.

    A provider that stops at the output cap usually leaves one partial final
    line. Everything before that line is usable, so the caller can continue
    from the last valid items instead of discarding the whole chunk.
    """

    parsed = JsonlParseChunk(items=[])
    for line in str(raw or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("`"):
            continue
        first_brace = stripped.find("{")
        if first_brace < 0:
            continue
        if first_brace > 0:
            stripped = stripped[first_brace:]
        try:
            item = json.loads(stripped)
        except json.JSONDecodeError:
            parsed.invalid_line = stripped[:500]
            break
        if not isinstance(item, dict):
            parsed.invalid_line = stripped[:500]
            break
        parsed.valid_lines += 1
        item_type = _jsonl_type(item)
        if item_type == "x":
            parsed.finished = True
            break
        if item_type in {"e", "r", "f"}:
            parsed.items.append(item)
        else:
            parsed.invalid_line = stripped[:500]
            break
    return parsed


def _jsonl_item_identity(item: dict) -> tuple:
    item_type = _jsonl_type(item)
    if item_type == "e":
        return (
            "e",
            _entity_key(item.get("cn") or item.get("canonical_name") or ""),
        )
    if item_type == "r":
        return (
            "r",
            _entity_key(item.get("sub") or item.get("subject") or ""),
            str(item.get("pred") or item.get("predicate") or "").strip(),
            _entity_key(item.get("obj") or item.get("object") or ""),
            str(item.get("ok") or item.get("object_kind") or "literal").strip(),
        )
    if item_type == "f":
        return (
            "f",
            _entity_key(item.get("sub") or item.get("subject") or ""),
            str(item.get("ft") or item.get("fact_type") or "").strip(),
            _entity_key(item.get("pn") or item.get("property_name") or "").replace(" ", "_"),
            str(item.get("val") or item.get("value") or "").strip(),
        )
    return (item_type, _compact_jsonl_line(item))


def _merge_jsonl_items(
    existing: list[dict],
    incoming: list[dict],
    *,
    prefer_incoming: bool = False,
) -> list[dict]:
    out = list(existing)
    seen = {_jsonl_item_identity(item): idx for idx, item in enumerate(out)}
    for item in incoming:
        identity = _jsonl_item_identity(item)
        if identity in seen:
            if prefer_incoming:
                out[seen[identity]] = item
            continue
        seen[identity] = len(out)
        out.append(item)
    return out


def _jsonl_items_to_object(items: list[dict], task: ExtractionTask) -> dict:
    data = {
        "schema_version": "polymath.extract.v1",
        "chunk_id": task.chunk_id,
        "doc_id": task.doc_id,
        "corpus_id": task.corpus_id,
        "entities": [],
        "relations": [],
        "facts": [],
    }
    for item in items:
        item_type = _jsonl_type(item)
        if item_type == "e":
            canonical_name = str(
                item.get("cn") or item.get("canonical_name") or ""
            ).strip()
            if not canonical_name:
                continue
            # Pt 10c — query_aliases + definitional_phrase. Coerce defensively:
            # the LLM may emit a single string instead of a list for qa, or
            # omit either field entirely. Both default to safe empty values.
            qa_raw = item.get("qa") or item.get("query_aliases") or []
            if isinstance(qa_raw, str):
                qa_list = [qa_raw.strip()] if qa_raw.strip() else []
            elif isinstance(qa_raw, list):
                qa_list = [str(a).strip() for a in qa_raw if str(a).strip()][:5]
            else:
                qa_list = []
            def_raw = item.get("def") or item.get("definitional_phrase") or ""
            def_str = str(def_raw).strip()[:200]
            # Pt9b — `ek` (entity-kind) is the JSONL abbreviation for
            # object_kind. Distinct from `ok` (which is the relation's
            # object_kind with "entity"|"literal" semantics — see the
            # `elif item_type == "r"` branch below). Two different wire
            # keys for two different semantics avoids prompt confusion.
            data["entities"].append(
                {
                    "canonical_name": canonical_name,
                    "surface_form": str(
                        item.get("sf") or item.get("surface_form") or canonical_name
                    ).strip(),
                    "entity_type": str(
                        item.get("et") or item.get("entity_type") or "other"
                    ).strip(),
                    "confidence": _jsonl_confidence(item),
                    "query_aliases": qa_list,
                    "definitional_phrase": def_str,
                    "object_kind": str(
                        item.get("ek") or item.get("object_kind") or item.get("e_kind") or ""
                    ).strip(),
                }
            )
        elif item_type == "r":
            subject = str(item.get("sub") or item.get("subject") or "").strip()
            predicate = str(item.get("pred") or item.get("predicate") or "").strip()
            obj = str(item.get("obj") or item.get("object") or "").strip()
            if not subject or not predicate or not obj:
                continue
            data["relations"].append(
                {
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "object_kind": str(
                        item.get("ok") or item.get("object_kind") or "literal"
                    ).strip(),
                    "confidence": _jsonl_confidence(item),
                    "evidence_phrase": str(
                        item.get("ev") or item.get("evidence_phrase") or ""
                    ).strip(),
                    "relation_cue": str(
                        item.get("rc")
                        or item.get("relation_cue")
                        or item.get("cue")
                        or ""
                    ).strip(),
                }
            )
        elif item_type == "f":
            subject = str(item.get("sub") or item.get("subject") or "").strip()
            fact_type = str(item.get("ft") or item.get("fact_type") or "").strip()
            property_name = str(
                item.get("pn") or item.get("property_name") or ""
            ).strip()
            value = str(item.get("val") or item.get("value") or "").strip()
            if not subject or not fact_type or not property_name or not value:
                continue
            fact = {
                "subject": subject,
                "fact_type": fact_type,
                "property_name": property_name,
                "value": value,
                "confidence": _jsonl_confidence(item),
                "evidence_phrase": str(
                    item.get("ev") or item.get("evidence_phrase") or ""
                ).strip(),
            }
            if item.get("unit") not in (None, ""):
                fact["unit"] = item.get("unit")
            if item.get("cond", item.get("condition")) not in (None, ""):
                fact["condition"] = item.get("cond", item.get("condition"))
            data["facts"].append(fact)
    return data


def _parse_jsonl_items(
    items: list[dict],
    task: ExtractionTask,
    threshold: float,
    schema: SchemaContext | None = None,
    schema_lens: SchemaLens | dict | None = None,
    *,
    enable_facts: bool | None = None,
    max_facts: int | None = None,
) -> ExtractionResult | None:
    data = _jsonl_items_to_object(items, task)
    return _parse(
        json.dumps(data, ensure_ascii=True, separators=(",", ":")),
        task,
        threshold,
        schema=schema,
        schema_lens=schema_lens,
        enable_facts=enable_facts,
        max_facts=max_facts,
    )


def _repair_truncated_json_object(raw: str) -> str | None:
    """Best-effort close for legacy one-shot JSON object responses."""

    text = str(raw or "").strip()
    if not text.startswith("{"):
        return None
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]") and stack and stack[-1] == ch:
            stack.pop()
    repaired = text
    if escape:
        repaired += "\\"
    if in_string:
        repaired += '"'
    while stack:
        repaired += stack.pop()
    return repaired if repaired != text else None


def _parse_object_with_repair(
    raw: str,
    task: ExtractionTask,
    threshold: float,
    schema: SchemaContext | None = None,
    schema_lens: SchemaLens | dict | None = None,
    *,
    enable_facts: bool | None = None,
    max_facts: int | None = None,
) -> ExtractionResult | None:
    result = _parse(
        raw,
        task,
        threshold,
        schema=schema,
        schema_lens=schema_lens,
        enable_facts=enable_facts,
        max_facts=max_facts,
    )
    if result is not None:
        return result
    repaired = _repair_truncated_json_object(raw)
    if not repaired:
        return None
    return _parse(
        repaired,
        task,
        threshold,
        schema=schema,
        schema_lens=schema_lens,
        enable_facts=enable_facts,
        max_facts=max_facts,
    )


def build_rescue_prompt(
    *,
    chunk_id: str,
    doc_id: str,
    corpus_id: str,
    text: str,
    max_entities: int,
    max_relations: int,
    max_total_lines: int,
    schema: SchemaContext | None = None,
    effective_entity_vocab: list[str] | None = None,
    effective_relation_vocab: list[str] | None = None,
    accepted_items: list[dict] | None = None,
    failure_reason: str | None = None,
    enable_facts: bool | None = None,
    max_facts: int | None = None,
    chunk_kind: str | None = None,
    metadata: dict | None = None,
    **_: Any,
) -> str:
    """Single bounded repair/resume prompt after a foreground contract violation."""

    if schema and schema.has_entity_schema:
        entity_vocab = effective_entity_vocab or schema.entity_vocab
    else:
        entity_vocab = _DEFAULT_ENTITY_TYPES
    if schema and schema.has_relation_schema:
        relation_vocab = effective_relation_vocab or schema.relation_vocab
    else:
        relation_vocab = ["short verb phrase label"]
    entity_vocab_text = "|".join(entity_vocab)
    relation_vocab_text = "|".join(relation_vocab)
    accepted_lines = "\n".join(
        _compact_jsonl_line(item) for item in (accepted_items or [])
    )
    if not accepted_lines:
        accepted_lines = "(none)"
    facts_enabled = bool(enable_facts) and int(max_facts or 0) > 0
    fact_limit = max(0, int(max_facts or 0))
    fact_protocol = ""
    fact_rule = "- Facts are disabled for this repair.\n"
    target = "entities and relations"
    if facts_enabled:
        fact_types = "|".join(FACT_TYPES)
        target = "entities, relations, and facts"
        fact_protocol = (
            f'Fact: {{"t":"f","sub":"canonical_name","ft":"{fact_types}",'
            '"pn":"snake_case","val":"verbatim or normalized","unit":"optional unit",'
            '"cond":"optional condition","cf":0.0,"ev":"exact short phrase"}}\n'
        )
        fact_rule = (
            f"- Max {fact_limit} facts; include facts only when they are high-value and evidence-backed.\n"
        )
    table_rules = _render_table_extraction_rules(chunk_kind, metadata)
    return (
        "REPAIR MODE: the previous JSONL extraction was incomplete, malformed, capped, or failed validation.\n"
        f"Failure reason: {failure_reason or 'contract_violation'}.\n"
        "You have one repair attempt.\n"
        "Accepted valid JSONL lines already kept:\n"
        f"{accepted_lines}\n"
        "Do not repeat accepted lines. Return only the missing highest-value remaining lines.\n"
        'If the accepted lines already cover the complete extraction, return only {"t":"x"}.\n'
        f"Focus on {target}.\n"
        "Output compact JSONL only, one object per line; no prose, markdown, or arrays.\n"
        'End with exactly: {"t":"x"}\n'
        f"Limits: max {max_total_lines} item lines, max {max_entities} entities, "
        f"max {max_relations} relations.\n"
        f"{fact_rule}"
        f"{table_rules}"
        f'Entity: {{"t":"e","cn":"lowercase no-punct","sf":"verbatim","et":"{entity_vocab_text}","cf":0.0}}\n'
        f'Relation: {{"t":"r","sub":"canonical_name","pred":"{relation_vocab_text}","obj":"canonical_name or literal","ok":"entity|literal","cf":0.0,"ev":"exact short phrase","cue":"trigger"}}\n'
        f"{fact_protocol}"
        "- Use only labels listed above; use related_to only if no specific predicate fits.\n"
        "- Relation ev must be an exact short phrase from TEXT.\n"
        "- Prefer fewer correct lines over broad coverage.\n"
        "TEXT:\n"
        f"{text}"
    )


async def extract_entities(
    tasks: list[ExtractionTask],
    model: str | None = None,
    schema: SchemaContext | None = None,
    schema_lens: SchemaLens | dict | None = None,
    chunk_vectors: dict[str, list[float]] | None = None,
    schema_resolver: SchemaResolver | None = None,
    *,
    pool: list[dict] | None = None,
    return_report: bool = False,
    enable_facts: bool | None = None,
    audit_event_sink: GhostBAuditSink | None = None,
    audit_run_id: str | None = None,
) -> list[ExtractionResult] | ExtractionBatchReport:
    """
    Extract entities from child chunks in parallel, bounded by EXTRACTION_MAX_CONCURRENT.

    Args:
        tasks: Child chunks to process. Must have stable chunk_id, doc_id, corpus_id.
        model: LiteLLM model string (falls back to settings.DEFAULT_COMPLETION_MODEL).
        schema: Optional Phase 14 SchemaContext. None = open extraction (current 4-bucket enum).
        schema_lens: Optional bounded corpus/domain guidance. It can bias attention
                     and relation alias normalization, but SchemaContext still
                     constrains the final labels.
        chunk_vectors: Phase 14.2 — map of chunk_id → embedding for per-chunk schema retrieval.
                       Required only when schema vocab > SCHEMA_INLINE_LIMIT and resolver is set.
        schema_resolver: Phase 14.2 — async callable (kind, vec, top_k) → list[str].
                         Worker injects this as a closure over qdrant_client + corpus_id so
                         this module stays free of Qdrant SDK imports.
        api_base: Phase 19.3 — optional per-call api_base (OpenAI-passthrough override).
        api_key: Phase 19.3 — optional per-call api_key (plaintext; caller decrypts).
        extra_params: Phase 19.3 — extra LiteLLM body params merged in. Keys `model`,
            `messages`, and `response_format` are reserved.
        max_concurrent: Phase 19.3 — override for EXTRACTION_MAX_CONCURRENT.

    Returns:
        Successful extraction results. Failures are logged and skipped. When
        return_report=True, returns ExtractionBatchReport with final per-chunk
        failures and aggregate metrics for audit/backfill.

    Toggle gate: caller must verify ingestion_config.use_neo4j before calling.
    """
    if not tasks:
        return []

    settings = get_settings()
    threshold = settings.ENTITY_CONFIDENCE_THRESHOLD
    inline_limit = settings.SCHEMA_INLINE_LIMIT
    top_k = settings.SCHEMA_RETRIEVAL_TOP_K
    max_completion_tokens = settings.EXTRACTION_MAX_TOKENS
    output_mode_setting = settings.EXTRACTION_OUTPUT_MODE
    facts_enabled = settings.EXTRACTION_ENABLE_FACTS if enable_facts is None else enable_facts
    max_facts = settings.EXTRACTION_MAX_FACTS_PER_CHUNK
    json_object_max_entities = settings.EXTRACTION_JSON_OBJECT_MAX_ENTITIES_PER_CHUNK
    json_object_max_relations = settings.EXTRACTION_JSON_OBJECT_MAX_RELATIONS_PER_CHUNK
    json_object_max_facts = settings.EXTRACTION_JSON_OBJECT_MAX_FACTS_PER_CHUNK
    evidence_max_chars = settings.EXTRACTION_EVIDENCE_MAX_CHARS
    fact_value_max_chars = settings.EXTRACTION_FACT_VALUE_MAX_CHARS
    rescue_max_tokens = settings.EXTRACTION_RESCUE_MAX_TOKENS
    max_jsonl_calls = min(
        settings.EXTRACTION_JSONL_MAX_CALLS,
        settings.EXTRACTION_FOREGROUND_MAX_CALLS,
        2,
    )
    raw_jsonl_debug = settings.EXTRACTION_JSONL_DEBUG_RAW
    audit_raw_first_chars = int(
        getattr(settings, "EXTRACTION_ERROR_AUDIT_RAW_FIRST_CHARS", 200) or 0
    )
    audit_raw_last_chars = int(
        getattr(settings, "EXTRACTION_ERROR_AUDIT_RAW_LAST_CHARS", 400) or 0
    )
    max_input_tokens = settings.EXTRACTION_MAX_INPUT_TOKENS
    max_total_lines = settings.EXTRACTION_MAX_TOTAL_LINES
    max_entities = settings.EXTRACTION_MAX_ENTITIES_PER_CHUNK
    max_relations = settings.EXTRACTION_MAX_RELATIONS_PER_CHUNK
    rescue_max_entities = settings.EXTRACTION_RESCUE_MAX_ENTITIES_PER_CHUNK
    rescue_max_relations = settings.EXTRACTION_RESCUE_MAX_RELATIONS_PER_CHUNK
    rescue_max_total_lines = settings.EXTRACTION_RESCUE_MAX_TOTAL_LINES
    failure_pause_percent = settings.EXTRACTION_FAILURE_PAUSE_PERCENT
    failure_pause_min_chunks = settings.EXTRACTION_FAILURE_PAUSE_MIN_CHUNKS
    headers = {
        "Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}",
        "Content-Type": "application/json",
    }

    if not pool:
        pool = [
            {
                "model": model or settings.DEFAULT_COMPLETION_MODEL,
                "base_url": None,
                "api_key": None,
                "max_concurrent": settings.EXTRACTION_MAX_CONCURRENT,
                "extra_params": {},
            }
        ]

    from services.ingestion.model_lifecycle import (
        ensure_model_lifecycle_ready,
        shutdown_model_lifecycle,
    )

    await ensure_model_lifecycle_ready(pool, purpose="ghost_b")

    lane_limits = [max(1, int(entry.get("max_concurrent") or 1) or 1) for entry in pool]
    try:
        from services.private_vllm_capacity import (
            fetch_private_vllm_capacity,
            plan_private_vllm_concurrency,
        )

        for idx, entry in enumerate(pool):
            card = resolve_extraction_provider_card(entry)
            if card.concurrency_policy != "adaptive_vram_85" or not card.lifecycle_base_url:
                continue
            extra = entry.get("extra_params") or {}
            if not isinstance(extra, dict):
                extra = {}
            safety_ratio = float(extra.get("vram_safety_ratio") or 0.85)
            per_request_vram_gb = extra.get("per_request_vram_gb")
            try:
                per_request = (
                    float(per_request_vram_gb)
                    if per_request_vram_gb not in (None, "")
                    else None
                )
            except (TypeError, ValueError):
                per_request = None
            capacity = await fetch_private_vllm_capacity(
                card.lifecycle_base_url,
                api_key=entry.get("lifecycle_api_key"),
                status_path=str(entry.get("lifecycle_status_path") or "/status"),
                timeout_s=5.0,
            )
            effective, meta = plan_private_vllm_concurrency(
                lane_limits[idx],
                capacity,
                safety_ratio=safety_ratio,
                per_request_vram_gb=per_request,
            )
            if effective < lane_limits[idx]:
                logger.warning(
                    "GHOST B private vLLM concurrency reduced: lane=%d requested=%d effective=%d reason=%s free_vram_gb=%s recommended=%s",
                    idx,
                    lane_limits[idx],
                    effective,
                    meta.get("reason"),
                    capacity.gpu_vram_free_gb,
                    capacity.recommended_concurrency,
                )
                lane_limits[idx] = effective
            else:
                logger.info(
                    "GHOST B private vLLM capacity accepted: lane=%d concurrency=%d free_vram_gb=%s recommended=%s",
                    idx,
                    lane_limits[idx],
                    capacity.gpu_vram_free_gb,
                    capacity.recommended_concurrency,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("GHOST B private vLLM capacity check unavailable: %s", exc)
    configured_lane_concurrency = sum(lane_limits) or max(1, settings.EXTRACTION_MAX_CONCURRENT)
    global_max_concurrent = min(
        settings.EXTRACTION_GLOBAL_MAX_CONCURRENT,
        configured_lane_concurrency,
    )
    try:
        from services.ingestion.resource_planner import throttle_concurrency_for_rss

        throttled, throttle_meta = throttle_concurrency_for_rss(
            global_max_concurrent,
            settings=settings,
        )
        if throttled < global_max_concurrent:
            logger.warning(
                "GHOST B concurrency throttled for memory: requested=%d effective=%d rss=%sMB soft_limit=%sMB cap=%sMB",
                global_max_concurrent,
                throttled,
                throttle_meta.get("rss_mb"),
                throttle_meta.get("rss_soft_limit_mb"),
                throttle_meta.get("ram_cap_mb"),
            )
            global_max_concurrent = throttled
    except Exception as exc:  # noqa: BLE001
        logger.debug("GHOST B memory throttle unavailable: %s", exc)
    global_sem = _global_extraction_semaphore(global_max_concurrent)
    lane_sems = [
        _model_lane_semaphore(entry, idx, lane_limits[idx])
        for idx, entry in enumerate(pool)
    ]
    routing_policy = _resolve_extraction_routing_policy(pool)
    balanced_start_offset = _next_balanced_route_offset(pool, routing_policy)
    logger.info(
        "GHOST B routing selected: policy=%s lane_limits=%s start_offset=%d models=%s",
        routing_policy,
        lane_limits,
        balanced_start_offset,
        [str(entry.get("model") or "") for entry in pool],
    )

    # Phase K — WORK-STEALING POOL. Instead of round-robin assignment
    # (task i → pool[i%N]), we spawn one worker coroutine PER lane slot
    # (total = sum of per-lane max_concurrent) and let every worker pull
    # from a shared queue. Fast lanes naturally pull more work; slow
    # lanes don't bottleneck the throughput of their share. At 4 lanes
    # with a 30 tok/s : 600 tok/s imbalance this typically reclaims
    # 30-40% of theoretical aggregate.
    task_queue: "asyncio.Queue[ExtractionTask]" = asyncio.Queue()
    for t in tasks:
        task_queue.put_nowait(t)

    results_list: list[ExtractionResult] = []
    failures_list: list[ExtractionFailureItem] = []
    call_metrics: list[dict] = []
    failed_count = 0
    _list_lock = asyncio.Lock()
    disabled_lanes: set[int] = set()
    lane_fatal_strikes: dict[int, int] = {}
    _disabled_lock = asyncio.Lock()
    failure_budget_open = False
    failure_budget_reason = "failure_budget_exceeded"

    def _failure_budget_should_open() -> bool:
        if failure_pause_percent >= 100.0:
            return False
        processed = len(results_list) + failed_count
        if processed < failure_pause_min_chunks:
            return False
        if processed <= 0:
            return False
        return (100.0 * failed_count / processed) >= failure_pause_percent

    async def _lane_disable_ready(pool_idx: int, exc: Exception) -> bool:
        tier = provider_error_tier(exc)
        if tier == "hard":
            return True
        if tier != "soft":
            return False
        async with _disabled_lock:
            strikes = lane_fatal_strikes.get(pool_idx, 0) + 1
            lane_fatal_strikes[pool_idx] = strikes
        entry = pool[pool_idx]
        if strikes >= SOFT_FATAL_DISABLE_STRIKES:
            return True
        logger.warning(
            "GHOST B saw soft fatal provider signal for lane=%d model=%s "
            "strike=%d/%d; keeping lane active until repeated: %s",
            pool_idx,
            entry["model"],
            strikes,
            SOFT_FATAL_DISABLE_STRIKES,
            provider_error_summary(exc),
        )
        return False

    async def _clear_lane_strikes(pool_idx: int) -> None:
        async with _disabled_lock:
            lane_fatal_strikes.pop(pool_idx, None)

    async def _disable_lane(pool_idx: int, exc: Exception) -> None:
        async with _disabled_lock:
            if pool_idx in disabled_lanes:
                return
            disabled_lanes.add(pool_idx)
        entry = pool[pool_idx]
        logger.error(
            "GHOST B disabled extraction lane=%d model=%s after fatal provider error: %s",
            pool_idx,
            entry["model"],
            provider_error_summary(exc),
        )

    async def _process_one(task: ExtractionTask, pool_idx: int) -> ExtractionResult | None:
        entry = pool[pool_idx]
        bounded_text, input_token_count, input_truncated = _bounded_extraction_text(
            task.text,
            max_input_tokens,
        )
        prompt_task = ExtractionTask(
            chunk_id=task.chunk_id,
            doc_id=task.doc_id,
            corpus_id=task.corpus_id,
            text=bounded_text,
            chunk_kind=task.chunk_kind,
            metadata=dict(task.metadata or {}),
        )
        if input_truncated:
            logger.warning(
                "GHOST B input span truncated chunk_id=%s tokens=%d max_input_tokens=%d",
                task.chunk_id,
                input_token_count,
                max_input_tokens,
            )
        chunk_vec = chunk_vectors.get(task.chunk_id) if chunk_vectors else None
        eff_entity, eff_relation = await resolve_chunk_vocab(
            schema=schema,
            chunk_vec=chunk_vec,
            resolver=schema_resolver,
            inline_limit=inline_limit,
            top_k=top_k,
        )
        prompt_kwargs = {
            "chunk_id": task.chunk_id,
            "doc_id": task.doc_id,
            "corpus_id": task.corpus_id,
            "text": bounded_text,
            "max_entities": max_entities,
            "max_relations": max_relations,
            "schema": schema,
            "effective_entity_vocab": eff_entity,
            "effective_relation_vocab": eff_relation,
            "schema_lens": schema_lens,
            "enable_facts": facts_enabled,
            "max_facts": max_facts,
            "max_total_lines": max_total_lines,
            "chunk_kind": prompt_task.chunk_kind,
            "metadata": prompt_task.metadata,
        }
        normal_output_mode = _select_extraction_output_mode(
            output_mode_setting,
            entry,
            profile_name="normal",
        )
        normal_max_entities = (
            min(max_entities, json_object_max_entities)
            if normal_output_mode in ("json_object", "json_object_prompt")
            else max_entities
        )
        normal_max_relations = (
            min(max_relations, json_object_max_relations)
            if normal_output_mode in ("json_object", "json_object_prompt")
            else max_relations
        )
        normal_max_facts = (
            min(max_facts, json_object_max_facts)
            if normal_output_mode in ("json_object", "json_object_prompt")
            else max_facts
        )
        normal_profile = ExtractionAttemptProfile(
            name="normal",
            max_tokens=max_completion_tokens,
            max_entities=normal_max_entities,
            max_relations=normal_max_relations,
            max_total_lines=max_total_lines,
            enable_facts=facts_enabled,
            max_facts=normal_max_facts if facts_enabled else 0,
        )
        rescue_profile = ExtractionAttemptProfile(
            name="rescue",
            max_tokens=rescue_max_tokens,
            max_entities=rescue_max_entities,
            max_relations=rescue_max_relations,
            max_total_lines=rescue_max_total_lines,
            enable_facts=facts_enabled,
            max_facts=max_facts if facts_enabled else 0,
        )
        profiles = [normal_profile, rescue_profile]
        payload_base: dict = {
            "model": entry["model"],
            "temperature": 0,
        }
        if entry.get("base_url"):
            payload_base["api_base"] = entry["base_url"]
        if entry.get("api_key"):
            payload_base["api_key"] = entry["api_key"]
        # Internal flags (supports_json_schema, managed_vllm, …) stay OUT of
        # provider bodies — Groq 400s on unknown keys (2026-07-05).
        from services.ingestion.extraction_contract import provider_payload_extras

        payload_base.update(provider_payload_extras(entry.get("extra_params")))
        provider_card = resolve_extraction_provider_card(entry)
        for key, value in provider_payload_defaults(provider_card).items():
            payload_base.setdefault(key, value)
        model_name = str(entry["model"])
        base_url = str(entry.get("base_url") or "")
        model_key = model_name.lower()
        base_url_key = base_url.lower()
        # DeepSeek v4-flash/v4-pro and MiMo reasoning variants can default
        # thinking-mode ON: reasoning tokens consume the output budget before
        # JSONL content emits. Force thinking off for extraction; explicit
        # operator overrides via corpus extra_params take precedence.
        if (
            (
                model_key.startswith("deepseek/")
                or "mimo" in model_key
                or "xiaomimimo" in base_url_key
            )
            and "thinking" not in payload_base
        ):
            payload_base["thinking"] = {"type": "disabled"}

        # Bounded foreground state machine:
        #   attempt 1 = normal compact graph extraction
        #   attempt 2 = smaller rescue prompt, no facts, lower caps
        # Never repeat the exact same broad prompt after a capped/malformed reply.
        last_exc: Exception | None = None
        last_error_type = "unknown"
        parse_failures = 0
        exception_failures = 0
        call_attempts = 0
        accepted_jsonl_items: list[dict[str, Any]] = []
        max_attempts = max(1, max_jsonl_calls)

        async def _audit_attempt(
            *,
            event: str,
            attempt: int,
            profile: ExtractionAttemptProfile,
            output_mode: str,
            prompt_hash: str | None = None,
            prompt_chars: int | None = None,
            raw_fingerprint: dict[str, Any] | None = None,
            result: ExtractionResult | None = None,
            jsonl_chunk: JsonlParseChunk | None = None,
            jsonl_items: list[dict[str, Any]] | None = None,
            merged_jsonl_items: list[dict[str, Any]] | None = None,
            line_cap_exceeded: bool = False,
            empty_after_validation: bool = False,
            hit_output_cap: bool = False,
            finish_reason: Any = None,
            usage: dict[str, Any] | None = None,
            max_tokens: int | None = None,
            error_type: str | None = None,
            error_message: str | None = None,
        ) -> None:
            pre_counts = _jsonl_item_counts(merged_jsonl_items or jsonl_items)
            post_counts = _result_counts(result)
            body: dict[str, Any] = {
                "event": event,
                "run_id": audit_run_id,
                "corpus_id": task.corpus_id,
                "doc_id": task.doc_id,
                "chunk_id": task.chunk_id,
                "attempt": attempt,
                "profile": profile.name,
                "model": str(entry["model"]),
                "lane": pool_idx,
                "input_tokens": input_token_count,
                "input_truncated": input_truncated,
                "prompt_hash": prompt_hash,
                "prompt_chars": prompt_chars,
                "output_mode": output_mode,
                "max_tokens": max_tokens,
                "completion_tokens": (usage or {}).get("completion_tokens"),
                "prompt_tokens": (usage or {}).get("prompt_tokens"),
                "total_tokens": (usage or {}).get("total_tokens"),
                "finish_reason": finish_reason,
                "hit_output_cap": hit_output_cap,
                "facts_enabled": profile.enable_facts,
                "provider_card": provider_card.to_safe_dict(),
                "caps": {
                    "entities": profile.max_entities,
                    "relations": profile.max_relations,
                    "facts": profile.max_facts if profile.enable_facts else 0,
                    "lines": profile.max_total_lines,
                },
                "jsonl": {
                    "valid_lines": jsonl_chunk.valid_lines if jsonl_chunk else 0,
                    "finished": jsonl_chunk.finished if jsonl_chunk else False,
                    "invalid_tail": bool(jsonl_chunk.invalid_line) if jsonl_chunk else False,
                    "items": len(jsonl_items or []),
                    "accepted_items": len(accepted_jsonl_items),
                    "merged_items": len(merged_jsonl_items or []),
                    "line_cap_exceeded": line_cap_exceeded,
                },
                "validation": {
                    "pre_entities": pre_counts["entities"],
                    "pre_relations": pre_counts["relations"],
                    "pre_facts": pre_counts["facts"],
                    "post_entities": post_counts["entities"],
                    "post_relations": post_counts["relations"],
                    "post_facts": post_counts["facts"],
                    "entity_schema_drops": getattr(result, "entity_drop_count", 0) if result else 0,
                    "relation_schema_drops": getattr(result, "relation_drop_count", 0) if result else 0,
                    "entity_evidence_drops": getattr(result, "entity_evidence_drop_count", 0) if result else 0,
                    "citation_drops": getattr(result, "citation_drop_count", 0) if result else 0,
                    "strict_entity_drops": getattr(result, "strict_entity_drop_count", 0) if result else 0,
                    "strict_relation_drops": getattr(result, "strict_relation_drop_count", 0) if result else 0,
                    "semantic_direction_drops": getattr(result, "semantic_direction_drop_count", 0) if result else 0,
                    "evidence_drops": getattr(result, "evidence_drop_count", 0) if result else 0,
                    "fact_drops": getattr(result, "fact_drop_count", 0) if result else 0,
                    "validation_rejections": _validation_rejection_count(result),
                    "empty_after_validation": empty_after_validation,
                },
                "error_type": error_type,
                "error_message": (error_message or "")[:1000],
            }
            if event == "ghost_b_attempt_failed" or raw_fingerprint is not None:
                body["raw"] = raw_fingerprint or _raw_output_fingerprint(
                    "",
                    first_chars=audit_raw_first_chars,
                    last_chars=audit_raw_last_chars,
                )
            await _emit_ghost_b_audit_event(audit_event_sink, body)

        for attempt in range(max_attempts):
            started = time.perf_counter()
            profile = profiles[min(attempt, len(profiles) - 1)]
            profile_kwargs = {
                **prompt_kwargs,
                "max_entities": profile.max_entities,
                "max_relations": profile.max_relations,
                "enable_facts": profile.enable_facts,
                "max_facts": profile.max_facts,
                "max_total_lines": profile.max_total_lines,
            }
            profile_output_mode = (
                normal_output_mode if profile.name == "normal" else "jsonl"
            )
            # Pt9c/provider-card — json_schema, json_object, and compiler-
            # gated json_object_prompt all use the single-object prompt. Only
            # the first two send provider-native response_format payloads.
            if profile_output_mode in ("json_object", "json_schema", "json_object_prompt"):
                prompt = build_json_object_prompt(
                    **{k: v for k, v in profile_kwargs.items() if k != "max_total_lines"},
                    evidence_max_chars=evidence_max_chars,
                    fact_value_max_chars=fact_value_max_chars,
                )
            else:
                prompt = (
                    build_user_prompt(**profile_kwargs)
                    if profile.name == "normal"
                    else build_rescue_prompt(
                        **profile_kwargs,
                        accepted_items=accepted_jsonl_items,
                        failure_reason=last_error_type,
                    )
                )
            prompt_chars = len(prompt)
            prompt_hash = hashlib.sha256(
                prompt.encode("utf-8", errors="replace")
            ).hexdigest()
            attempt_payload = dict(payload_base)
            attempt_payload["messages"] = [
                {
                    "role": "system",
                    "content": (
                        _JSON_OBJECT_SYSTEM
                        if profile_output_mode in ("json_object", "json_schema", "json_object_prompt")
                        else _SYSTEM
                    ),
                },
                {"role": "user", "content": prompt},
            ]
            # Pt9c — branch on exact mode for response_format because the
            # payload differs: json_schema sends a full schema spec, while
            # json_object just sends {"type": "json_object"}.
            if profile_output_mode == "json_schema":
                attempt_payload["response_format"] = _json_schema_response_format()
            elif profile_output_mode == "json_object":
                attempt_payload["response_format"] = _json_object_response_format()
            attempt_max_tokens = profile.max_tokens
            attempt_payload["max_tokens"] = attempt_max_tokens
            try:
                async with lane_sems[pool_idx]:
                    async with global_sem:
                        async with httpx.AsyncClient(timeout=120.0) as client:
                            try:
                                resp = await client.post(
                                    f"{settings.LITELLM_URL}/chat/completions",
                                    json=attempt_payload,
                                    headers=headers,
                                )
                            finally:
                                attempt_payload["messages"] = []
                                prompt = ""
                    duration = time.perf_counter() - started
                    resp.raise_for_status()
                    call_attempts += 1
                    body = resp.json()
                    usage = body.get("usage") or {}
                    choices = body.get("choices") or []
                    choice = choices[0] if choices else {}
                    finish_reason = choice.get("finish_reason")
                    logger.info(
                        "GHOST B extraction call: chunk_id=%s model=%s "
                        "duration=%.2fs total_tokens=%s prompt_tokens=%s "
                        "completion_tokens=%s finish_reason=%s max_tokens=%s "
                        "lane=%d attempt=%d output_mode=%s",
                        task.chunk_id,
                        entry["model"],
                        duration,
                        usage.get("total_tokens"),
                        usage.get("prompt_tokens"),
                        usage.get("completion_tokens"),
                        finish_reason,
                        attempt_max_tokens,
                        pool_idx,
                        attempt + 1,
                        profile_output_mode,
                    )
                    raw = str(choice.get("message", {}).get("content") or "")
                    body = {}
                    choices = []
                    choice = {}
                    resp = None
                    _debug_log_raw_jsonl_lines(
                        raw,
                        chunk_id=task.chunk_id,
                        lane=pool_idx,
                        attempt=attempt + 1,
                        enabled=raw_jsonl_debug,
                    )
                    completion_tokens_raw = usage.get("completion_tokens")
                    try:
                        completion_tokens = int(completion_tokens_raw)
                    except (TypeError, ValueError):
                        completion_tokens = None
                    hit_output_cap = (
                        finish_reason == "length"
                        or (
                            completion_tokens is not None
                            and completion_tokens >= attempt_max_tokens
                        )
                    )
                    jsonl_chunk = JsonlParseChunk(items=[])
                    jsonl_items: list[dict[str, Any]] = []
                    merged_jsonl_items: list[dict[str, Any]] = []
                    line_cap_exceeded = False
                    empty_after_claimed_items = False
                    object_mode = profile_output_mode in (
                        "json_object",
                        "json_schema",
                        "json_object_prompt",
                    )
                    object_claimed_items = False
                    used_jsonl_fallback = False
                    # Pt9c — json_schema produces the same single-JSON-object
                    # shape as json_object (same _parse_object_with_repair
                    # path). The provider's constrained decoder guarantees
                    # validity, but the repair fallback still runs as
                    # defense-in-depth if something exotic comes back.
                    if object_mode:
                        object_claimed_items = _json_object_claims_items(raw)
                        result = _parse_object_with_repair(
                            raw,
                            prompt_task,
                            threshold,
                            schema=schema,
                            schema_lens=schema_lens,
                            enable_facts=profile.enable_facts,
                            max_facts=profile.max_facts,
                        )
                        if result is None:
                            used_jsonl_fallback = True
                            jsonl_chunk = _parse_jsonl_lines(raw)
                            jsonl_items, line_cap_exceeded = _cap_jsonl_items(
                                jsonl_chunk.items,
                                profile.max_total_lines,
                            )
                            merged_jsonl_items = _merge_jsonl_items(
                                accepted_jsonl_items,
                                jsonl_items,
                                prefer_incoming=True,
                            )
                            if merged_jsonl_items or jsonl_chunk.finished:
                                result = _parse_jsonl_items(
                                    merged_jsonl_items,
                                    prompt_task,
                                    threshold,
                                    schema=schema,
                                    schema_lens=schema_lens,
                                    enable_facts=profile.enable_facts,
                                    max_facts=profile.max_facts,
                                )
                    else:
                        jsonl_chunk = _parse_jsonl_lines(raw)
                        jsonl_items, line_cap_exceeded = _cap_jsonl_items(
                            jsonl_chunk.items,
                            profile.max_total_lines,
                        )
                        merged_jsonl_items = (
                            _merge_jsonl_items(
                                accepted_jsonl_items,
                                jsonl_items,
                                prefer_incoming=True,
                            )
                            if profile.name != "normal"
                            else list(jsonl_items)
                        )
                        if merged_jsonl_items or jsonl_chunk.finished:
                            result = _parse_jsonl_items(
                                merged_jsonl_items,
                                prompt_task,
                                threshold,
                                schema=schema,
                                schema_lens=schema_lens,
                                enable_facts=profile.enable_facts,
                                max_facts=profile.max_facts,
                            )
                        else:
                            result = None
                    result = _cap_result(result, profile)
                    empty_after_claimed_items = (
                        result is not None
                        and (
                            bool(merged_jsonl_items)
                            or (object_mode and object_claimed_items)
                        )
                        and not _result_has_items(result)
                    )
                    if object_mode and not used_jsonl_fallback:
                        complete = (
                            result is not None
                            and not empty_after_claimed_items
                            and not line_cap_exceeded
                        )
                    else:
                        complete = (
                            jsonl_chunk.finished
                            and result is not None
                            and not empty_after_claimed_items
                            and not line_cap_exceeded
                        )
                    attempt_error_type = None
                    if not complete:
                        if line_cap_exceeded:
                            attempt_error_type = "line_cap_exceeded"
                        elif hit_output_cap:
                            attempt_error_type = "truncated_json"
                        elif empty_after_claimed_items:
                            attempt_error_type = "empty_after_validation"
                        elif merged_jsonl_items:
                            attempt_error_type = "jsonl_incomplete"
                        else:
                            attempt_error_type = "parse_error"
                        last_error_type = attempt_error_type
                        if merged_jsonl_items:
                            last_exc = RuntimeError(
                                "JSONL extraction did not emit finished sentinel"
                            )
                    call_metrics.append(
                        {
                            "chunk_id": task.chunk_id,
                            "model": entry["model"],
                            "provider": provider_card.provider,
                            "schema_mode": provider_card.schema_mode,
                            "json_repair_mode": provider_card.json_repair_mode,
                            "semantic_verifier_mode": provider_card.semantic_verifier_mode,
                            "lane": pool_idx,
                            "profile": profile.name,
                            "output_mode": profile_output_mode,
                            "attempt": attempt + 1,
                            "duration_seconds": round(duration, 3),
                            "total_tokens": usage.get("total_tokens"),
                            "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens"),
                            "finish_reason": finish_reason,
                            "max_tokens": attempt_max_tokens,
                            "max_total_lines": profile.max_total_lines,
                            "prompt_hash": prompt_hash,
                            "prompt_chars": prompt_chars,
                            "global_max_concurrent": global_max_concurrent,
                            "model_lane_max_concurrent": lane_limits[pool_idx],
                            "input_tokens": input_token_count,
                            "input_truncated": input_truncated,
                            "success": bool(complete),
                            "error_type": attempt_error_type,
                            "routing_policy": routing_policy,
                            "jsonl_valid_lines": jsonl_chunk.valid_lines,
                            "jsonl_finished": jsonl_chunk.finished,
                            "jsonl_accepted_items": len(accepted_jsonl_items),
                            "line_cap_exceeded": line_cap_exceeded,
                            "empty_after_validation": empty_after_claimed_items,
                        }
                    )
                    if complete and result:
                        validation_rejections = _validation_rejection_count(result)
                        await _audit_attempt(
                            event=(
                                "ghost_b_attempt_succeeded_with_validation_rejections"
                                if validation_rejections
                                else "ghost_b_attempt_succeeded"
                            ),
                            attempt=attempt + 1,
                            profile=profile,
                            output_mode=profile_output_mode,
                            prompt_hash=prompt_hash,
                            prompt_chars=prompt_chars,
                            raw_fingerprint=(
                                _raw_output_fingerprint(
                                    raw,
                                    first_chars=audit_raw_first_chars,
                                    last_chars=audit_raw_last_chars,
                                )
                                if validation_rejections
                                else None
                            ),
                            result=result,
                            jsonl_chunk=jsonl_chunk,
                            jsonl_items=jsonl_items,
                            merged_jsonl_items=merged_jsonl_items,
                            line_cap_exceeded=line_cap_exceeded,
                            empty_after_validation=empty_after_claimed_items,
                            hit_output_cap=hit_output_cap,
                            finish_reason=finish_reason,
                            usage=usage,
                            max_tokens=attempt_max_tokens,
                            error_type=(
                                "validation_gate_rejected_items"
                                if validation_rejections
                                else None
                            ),
                            error_message=(
                                f"{validation_rejections} emitted extraction item(s) "
                                "were rejected by promotion gates"
                                if validation_rejections
                                else None
                            ),
                        )
                        raw = ""
                        logger.debug(
                            "GHOST B: chunk_id=%s entities=%d relations=%d "
                            "(attempt=%d lane=%d profile=%s jsonl_items=%d)",
                            task.chunk_id,
                            len(result.entities),
                            len(result.relations),
                            attempt + 1,
                            pool_idx,
                            profile.name,
                            len(jsonl_items),
                        )
                        return result
                    await _audit_attempt(
                        event="ghost_b_attempt_failed",
                        attempt=attempt + 1,
                        profile=profile,
                        output_mode=profile_output_mode,
                        prompt_hash=prompt_hash,
                        prompt_chars=prompt_chars,
                        raw_fingerprint=_raw_output_fingerprint(
                            raw,
                            first_chars=audit_raw_first_chars,
                            last_chars=audit_raw_last_chars,
                        ),
                        result=result,
                        jsonl_chunk=jsonl_chunk,
                        jsonl_items=jsonl_items,
                        merged_jsonl_items=merged_jsonl_items,
                        line_cap_exceeded=line_cap_exceeded,
                        empty_after_validation=empty_after_claimed_items,
                        hit_output_cap=hit_output_cap,
                        finish_reason=finish_reason,
                        usage=usage,
                        max_tokens=attempt_max_tokens,
                        error_type=attempt_error_type,
                        error_message=str(attempt_error_type or last_exc or ""),
                    )
                    raw = ""
                    if (
                        jsonl_items
                        or empty_after_claimed_items
                        or (merged_jsonl_items and not jsonl_chunk.finished)
                    ):
                        if jsonl_items:
                            accepted_jsonl_items = _merge_jsonl_items(
                                accepted_jsonl_items,
                                jsonl_items,
                                prefer_incoming=True,
                            )
                        if line_cap_exceeded:
                            last_error_type = "line_cap_exceeded"
                        elif hit_output_cap:
                            last_error_type = "truncated_json"
                        elif empty_after_claimed_items:
                            last_error_type = "empty_after_validation"
                        else:
                            last_error_type = "jsonl_incomplete"
                        last_exc = RuntimeError(
                            "JSONL extraction did not complete with a valid normalized object"
                        )
                        logger.warning(
                            "GHOST B JSONL contract violation chunk_id=%s lane=%d "
                            "profile=%s items=%d accepted_items=%d valid_lines=%d "
                            "finish_reason=%s invalid_tail=%s line_cap_exceeded=%s "
                            "empty_after_validation=%s",
                            task.chunk_id,
                            pool_idx,
                            profile.name,
                            len(jsonl_items),
                            len(accepted_jsonl_items),
                            jsonl_chunk.valid_lines,
                            finish_reason,
                            bool(jsonl_chunk.invalid_line),
                            line_cap_exceeded,
                            empty_after_claimed_items,
                        )
                        continue
                    parse_failures += 1
                    if hit_output_cap and attempt + 1 < max_attempts:
                        last_error_type = "truncated_json"
                        last_exc = RuntimeError(
                            f"parse returned None after hitting max_tokens={attempt_max_tokens}"
                        )
                        logger.warning(
                            "GHOST B JSON parse failed after output cap "
                            "chunk_id=%s lane=%d completion_tokens=%s "
                            "max_tokens=%s; switching_to_rescue=%s",
                            task.chunk_id,
                            pool_idx,
                            completion_tokens_raw,
                            attempt_max_tokens,
                            attempt == 0,
                        )
                        continue
                    last_error_type = "parse_error"
                    last_exc = RuntimeError("parse returned None")
                    if parse_failures >= 2:
                        break
            except Exception as exc:
                last_exc = exc
                last_error_type = exc.__class__.__name__
                exception_failures += 1
                # Pt9c — json_schema mode shares the 400/422 retry path
                # with json_object. Some providers reject malformed
                # response_format payloads (or unsupported schema features)
                # with these status codes; we fall through to the rescue
                # path the same way, accepting one degraded attempt rather
                # than failing the whole chunk.
                if (
                    profile_output_mode in ("json_object", "json_schema", "json_object_prompt")
                    and isinstance(exc, httpx.HTTPStatusError)
                    and exc.response is not None
                    and exc.response.status_code in {400, 422}
                    and attempt + 1 < max_attempts
                ):
                    last_error_type = (
                        "json_schema_unsupported"
                        if profile_output_mode == "json_schema"
                        else "json_object_unsupported"
                    )
                    call_metrics.append(
                        {
                            "chunk_id": task.chunk_id,
                            "model": entry["model"],
                            "provider": provider_card.provider,
                            "schema_mode": provider_card.schema_mode,
                            "json_repair_mode": provider_card.json_repair_mode,
                            "semantic_verifier_mode": provider_card.semantic_verifier_mode,
                            "lane": pool_idx,
                            "profile": profile.name,
                            "output_mode": profile_output_mode,
                            "attempt": attempt + 1,
                            "duration_seconds": round(
                                time.perf_counter() - started,
                                3,
                            ),
                            "total_tokens": 0,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "finish_reason": None,
                            "max_tokens": attempt_payload.get("max_tokens"),
                            "prompt_hash": prompt_hash,
                            "prompt_chars": prompt_chars,
                            "success": False,
                            "error_type": last_error_type,
                            "routing_policy": routing_policy,
                        }
                    )
                    await _audit_attempt(
                        event="ghost_b_attempt_failed",
                        attempt=attempt + 1,
                        profile=profile,
                        output_mode=profile_output_mode,
                        prompt_hash=prompt_hash,
                        prompt_chars=prompt_chars,
                        hit_output_cap=False,
                        finish_reason=None,
                        usage={},
                        max_tokens=attempt_payload.get("max_tokens"),
                        error_type=last_error_type,
                        error_message=str(exc),
                    )
                    logger.warning(
                        "GHOST B %s mode was rejected by provider "
                        "chunk_id=%s lane=%d status=%s; switching_to_rescue=True",
                        profile_output_mode,
                        task.chunk_id,
                        pool_idx,
                        exc.response.status_code,
                    )
                    continue
                fatal_tier = provider_error_tier(exc)
                fatal_lane = fatal_tier is not None
                call_metrics.append(
                    {
                        "chunk_id": task.chunk_id,
                        "model": entry["model"],
                        "provider": provider_card.provider,
                        "schema_mode": provider_card.schema_mode,
                        "json_repair_mode": provider_card.json_repair_mode,
                        "semantic_verifier_mode": provider_card.semantic_verifier_mode,
                        "lane": pool_idx,
                        "profile": profile.name,
                        "output_mode": profile_output_mode,
                        "attempt": attempt + 1,
                        "duration_seconds": round(time.perf_counter() - started, 3),
                        "total_tokens": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "finish_reason": None,
                        "max_tokens": attempt_payload.get("max_tokens"),
                        "prompt_hash": prompt_hash,
                        "prompt_chars": prompt_chars,
                        "success": False,
                        "error_type": (
                            f"{fatal_tier}_fatal_lane_error"
                            if fatal_lane
                            else last_error_type
                        ),
                        "routing_policy": routing_policy,
                    }
                )
                await _audit_attempt(
                    event="ghost_b_attempt_failed",
                    attempt=attempt + 1,
                    profile=profile,
                    output_mode=profile_output_mode,
                    prompt_hash=prompt_hash,
                    prompt_chars=prompt_chars,
                    hit_output_cap=False,
                    finish_reason=None,
                    usage={},
                    max_tokens=attempt_payload.get("max_tokens"),
                    error_type=(
                        f"{fatal_tier}_fatal_lane_error"
                        if fatal_lane
                        else last_error_type
                    ),
                    error_message=str(exc),
                )
                if fatal_lane:
                    raise FatalLaneError(exc) from exc
                if attempt == 0:
                    logger.warning(
                        "GHOST B lane %d failed chunk_id=%s attempt=%d: %s — retrying",
                        pool_idx, task.chunk_id, attempt + 1, exc,
                    )
                if exception_failures >= 2:
                    break
                continue
        logger.error(
            "GHOST B failed chunk_id=%s lane=%d after %d attempts: %s",
            task.chunk_id,
            pool_idx,
            call_attempts or (parse_failures + exception_failures),
            last_exc,
        )
        failures_list.append(
            ExtractionFailureItem(
                chunk_id=task.chunk_id,
                doc_id=task.doc_id,
                corpus_id=task.corpus_id,
                model=str(entry["model"]),
                lane=pool_idx,
                attempts=call_attempts or (parse_failures + exception_failures),
                error_type=last_error_type,
                error_message=str(last_exc)[:1000],
            )
        )
        return None

    async def _lane_worker(pool_idx: int) -> None:
        """One coroutine per lane slot. Drains the shared queue until empty."""
        nonlocal failed_count, failure_budget_open
        while True:
            if pool_idx in disabled_lanes or failure_budget_open:
                return
            try:
                task = task_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if pool_idx in disabled_lanes or failure_budget_open:
                    task_queue.put_nowait(task)
                    return
                try:
                    result = await _process_one(task, pool_idx)
                except FatalLaneError as exc:
                    task_queue.put_nowait(task)
                    if await _lane_disable_ready(pool_idx, exc.original):
                        await _disable_lane(pool_idx, exc.original)
                        logger.warning(
                            "GHOST B requeued chunk_id=%s after disabling lane=%d",
                            task.chunk_id,
                            pool_idx,
                        )
                    else:
                        logger.warning(
                            "GHOST B requeued chunk_id=%s after soft fatal strike on lane=%d",
                            task.chunk_id,
                            pool_idx,
                        )
                    return
                async with _list_lock:
                    if result is not None:
                        await _clear_lane_strikes(pool_idx)
                        results_list.append(result)
                    else:
                        failed_count += 1
                    if not failure_budget_open and _failure_budget_should_open():
                        failure_budget_open = True
                        processed = len(results_list) + failed_count
                        logger.error(
                            "GHOST B failure budget tripped: failed=%d processed=%d "
                            "threshold=%.1f%% min_chunks=%d queued=%d",
                            failed_count,
                            processed,
                            failure_pause_percent,
                            failure_pause_min_chunks,
                            task_queue.qsize(),
                        )
                        await _emit_ghost_b_audit_event(
                            audit_event_sink,
                            {
                                "event": "ghost_b_failure_budget_tripped",
                                "run_id": audit_run_id,
                                "corpus_id": task.corpus_id,
                                "doc_id": task.doc_id,
                                "chunk_id": task.chunk_id,
                                "failed": failed_count,
                                "processed": processed,
                                "successes": len(results_list),
                                "threshold_percent": failure_pause_percent,
                                "min_chunks": failure_pause_min_chunks,
                                "queued_remaining": task_queue.qsize(),
                                "lane_limits": lane_limits,
                                "global_max_concurrent": global_max_concurrent,
                            },
                        )
            finally:
                task_queue.task_done()

    async def _run_enabled_workers() -> None:
        # Spawn total_concurrency workers = sum of enabled per-lane max_concurrent.
        workers: list[asyncio.Task] = []
        for pool_idx in _worker_lane_spawn_order(
            lane_limits,
            disabled_lanes,
            routing_policy,
            start_offset=balanced_start_offset,
        ):
            workers.append(asyncio.create_task(_lane_worker(pool_idx)))
        if workers:
            await asyncio.gather(*workers, return_exceptions=False)

    try:
        await _run_enabled_workers()
        while not task_queue.empty():
            if failure_budget_open:
                logger.error(
                    "GHOST B stopped with %d chunks still queued after failure budget tripped",
                    task_queue.qsize(),
                )
                break
            enabled_count = sum(
                1 for pool_idx in range(len(pool)) if pool_idx not in disabled_lanes
            )
            if enabled_count <= 0:
                logger.error(
                    "GHOST B stopped with %d chunks still queued because all extraction lanes were disabled",
                    task_queue.qsize(),
                )
                break
            pending_before = task_queue.qsize()
            disabled_before = len(disabled_lanes)
            await _run_enabled_workers()
            if task_queue.qsize() >= pending_before and len(disabled_lanes) == disabled_before:
                logger.warning(
                    "GHOST B stopped with %d chunks still queued after retry drain made no progress",
                    task_queue.qsize(),
                )
                break
    finally:
        await shutdown_model_lifecycle(pool, purpose="ghost_b")

    results = results_list  # alias for the unchanged stats block below
    if disabled_lanes:
        logger.warning(
            "GHOST B completed with disabled lanes: %s",
            ", ".join(
                f"{idx}:{pool[idx]['model']}" for idx in sorted(disabled_lanes)
            ),
        )

    accounted_chunk_ids = {r.chunk_id for r in results_list} | {
        f.chunk_id for f in failures_list
    }
    unprocessed_tasks = [t for t in tasks if t.chunk_id not in accounted_chunk_ids]
    if unprocessed_tasks:
        if failure_budget_open:
            reason = failure_budget_reason
            message = (
                "Foreground Ghost B paused this document after the failed-chunk "
                "percentage exceeded the configured budget."
            )
        else:
            reason = (
                "all_enabled_lanes_exhausted_after_circuit_breaker"
                if disabled_lanes
                else "not_processed"
            )
            message = (
                "No healthy extraction lane remained to process this chunk."
                if disabled_lanes
                else "Extraction worker exited before processing this chunk."
            )
        for task in unprocessed_tasks:
            failures_list.append(
                ExtractionFailureItem(
                    chunk_id=task.chunk_id,
                    doc_id=task.doc_id,
                    corpus_id=task.corpus_id,
                    model="pool",
                    lane=-1,
                    attempts=0,
                    error_type=reason,
                    error_message=message,
                )
            )
        failed_count += len(unprocessed_tasks)

    # Failure reporting
    if failed_count > 0:
        fail_pct = 100.0 * failed_count / len(tasks)
        log_fn = logger.error if fail_pct > 5.0 else logger.warning
        log_fn(
            "GHOST B failures: %d/%d chunks (%.1f%%) — see earlier errors for lane/reason",
            failed_count, len(tasks), fail_pct,
        )

    # Aggregate stats for the whole batch (helps tune schema_strict mode).
    total_entity_remaps = sum(r.entity_remap_count for r in results)
    total_relation_remaps = sum(r.relation_remap_count for r in results)
    total_entity_drops = sum(r.entity_drop_count for r in results)
    total_relation_drops = sum(r.relation_drop_count for r in results)
    total_domain_range_remaps = sum(r.domain_range_remap_count for r in results)
    schema_mode = schema.strict if schema else "off"
    logger.info(
        "GHOST B complete: %d/%d chunks extracted across %d model(s) [%s] "
        "(schema_strict=%s, entity_remaps=%d relation_remaps=%d "
        "domain_range_remaps=%d entity_drops=%d relation_drops=%d)",
        len(results),
        len(tasks),
        len(pool),
        ", ".join(e["model"] for e in pool),
        schema_mode,
        total_entity_remaps,
        total_relation_remaps,
        total_domain_range_remaps,
        total_entity_drops,
        total_relation_drops,
    )
    if return_report:
        return ExtractionBatchReport(
            results=results,
            failures=failures_list,
            metrics=summarize_extraction_batch(
                total_chunks=len(tasks),
                results=results,
                failures=failures_list,
                call_metrics=call_metrics,
                models=[str(e["model"]) for e in pool],
            ),
        )
    return results
