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
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, ClassVar, Literal

import httpx

from config import get_settings
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

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a precise entity and relation extractor. "
    "Output ONLY valid JSON. Extract only what is explicitly stated in the text. "
    "Do not hallucinate entities or relations."
)

# Default open-vocabulary enums when no schema is provided.
_DEFAULT_ENTITY_TYPES = ["person", "org", "concept", "other"]


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
    "Method",         # techniques, algorithms, procedures, approaches
    "Product",        # software systems, hardware, commercial goods, services
    "Document",       # papers, books, reports, specs, messages
    "Rule",           # SOPs, policies, guidelines, protocols (prescriptive, no legal force)
    "Law",            # statutes, regulations, case citations, treaties, UCMJ / HIPAA / GDPR
    "Artifact",       # tangible objects, equipment, tools, weapons, code snippets
    "TimeReference",  # dates, periods, deadlines, durations
]

UNIVERSAL_RELATION_SCHEMA: list[str] = [
    "part_of",
    "member_of",
    "located_in",
    "works_for",
    "created_by",
    "uses",
    "calls",
    "references",
    "implements",
    "depends_on",
    "produces",
    "stores",
    "extracts",
    "detects",
    "classifies",
    "runs_on",
    "trained_on",
    "supports",
    "represents",
    "maps_to",
    "preceded_by",
    "causes",
    "derived_from",
    "contradicts",
    "excepts",
    "overrides",
    "related_to",  # sentinel — MUST stay last
]

# Phase I — Claude-authored disambiguating glosses injected into the extraction
# prompt's vocabulary constraint block. Each ≤ 8 words, designed to separate
# sibling types/predicates that LLMs routinely confuse (Concept vs Method,
# Rule vs Law, uses vs depends_on, contradicts vs overrides vs excepts, etc.).
# Not consumed by any migration / comparison / storage code — purely an
# LLM-facing prompt augmentation.
UNIVERSAL_ENTITY_GLOSSES: dict[str, str] = {
    "Person":        "named human individual",
    "Organization":  "named formal group (company, team, unit)",
    "Location":      "named physical place",
    "Event":         "named bounded occurrence",
    "Concept":       "abstract idea, not a procedure",
    "Method":        "executable procedure or named technique",
    "Product":       "built offering with users",
    "Document":      "authored written artifact",
    "Rule":          "non-legal prescriptive guideline",
    "Law":           "legally binding statute or regulation",
    "Artifact":      "tangible object, not a Product",
    "TimeReference": "specific date, period, or duration",
    "other":         "nothing above fits",
}

UNIVERSAL_RELATION_GLOSSES: dict[str, str] = {
    "part_of":      "X is a structural subcomponent of Y",
    "member_of":    "X is in group Y",
    "located_in":   "X is inside place Y",
    "works_for":    "Person X employed by Org Y",
    "created_by":   "Y authored or built X",
    "uses":         "X operationally consumes Y",
    "calls":        "X invokes API/function/service Y",
    "references":   "X cites or mentions Y",
    "implements":   "X is concrete form of abstract Y",
    "depends_on":   "X requires Y to function",
    "produces":     "X outputs Y",
    "stores":       "X persists Y in storage",
    "extracts":     "X pulls Y from source data",
    "detects":      "X identifies Y in input",
    "classifies":   "X assigns Y as a category",
    "runs_on":      "X executes on platform Y",
    "trained_on":   "X learns from dataset Y",
    "supports":     "X enables capability Y",
    "represents":   "X models or encodes Y",
    "maps_to":      "X transforms into Y",
    "preceded_by":  "X happened after Y",
    "causes":       "X leads to effect Y",
    "derived_from": "X evolved out of Y",
    "contradicts":  "X conflicts with Y",
    "excepts":      "X is carveout from Y",
    "overrides":    "X supersedes Y on conflict",
    "related_to":   "fallback catchall",
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
        "subject_types": ["Artifact", "Concept", "Document", "Method", "Product", "Rule", "Law"],
        "object_types": ["Person", "Organization"],
    },
    "uses": {
        "subject_types": ["Person", "Organization", "Method", "Product", "Artifact"],
        "object_types": ["Artifact", "Product", "Method", "Concept", "Document"],
    },
    "calls": {
        "subject_types": ["Artifact", "Method", "Product", "Organization"],
        "object_types": ["Artifact", "Method", "Product", "Organization"],
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
    "extracts": {
        "subject_types": ["Artifact", "Method", "Organization", "Product"],
        "object_types": ["Artifact", "Concept", "Document", "Event", "Location", "Organization", "Person", "Product"],
    },
    "detects": {
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
        "subject_types": ["Event", "TimeReference", "Document", "Concept", "Method"],
        "object_types": ["Event", "TimeReference", "Document", "Concept", "Method"],
    },
    "causes": {
        "subject_types": ["Event", "Concept", "Method", "Rule", "Law"],
        "object_types": ["Event", "Concept", "Method", "Rule", "Law"],
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
    "calls": ("calls", False),
    "invokes": ("calls", False),
    "queries": ("calls", False),
    "called_by": ("calls", True),
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
    "trained_with": ("trained_on", False),
    "extract": ("extracts", False),
    "identifies": ("detects", False),
    "recognizes": ("detects", False),
    "predicts": ("classifies", False),
    "labels": ("classifies", False),
    "models": ("represents", False),
    "encodes": ("represents", False),
    "converts": ("maps_to", False),
    "transforms": ("maps_to", False),
    "translates": ("maps_to", False),
    "maps_onto": ("maps_to", False),
    "discusses": ("references", False),
    "covers": ("references", False),
    "teaches": ("references", False),
    "defines": ("references", False),
    "shows": ("references", False),
    "demonstrates": ("references", False),
    "based_on": ("derived_from", False),
    "inspired_by": ("derived_from", False),
    "built_on": ("derived_from", False),
    "prevents": ("contradicts", False),
    "replaces": ("overrides", False),
    "supersedes": ("overrides", False),
}

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
    # Extraction/detection/dataflow cues.
    (re.compile(r"\b(extracts?|captures?|pulls?)\b"), "extracts", False),
    (re.compile(r"\b(detects?|recognizes?|identifies?)\b"), "detects", False),
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


def _render_vocab_line(name: str, glosses: dict[str, str]) -> str:
    """Render a single vocab item as 'Name (gloss)' when a gloss is known, else
    just 'Name'. Custom per-corpus entity/relation names keep working — they
    simply render without a gloss.
    """
    g = glosses.get(name)
    return f"{name} ({g})" if g else name


def _render_vocab_constraint(
    vocab: list[str], glosses: dict[str, str], sentinel: str
) -> str:
    """Render the full vocab list with glosses, followed by the sentinel
    fallback instruction. Used by the extraction prompt builder.
    """
    return " | ".join(_render_vocab_line(v, glosses) for v in vocab)


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
    if lens.preferred_entity_types:
        lines.append(
            "- prefer these approved entity_type values when text supports them: "
            + ", ".join(lens.preferred_entity_types[:8])
        )
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
) -> str:
    """Render the per-chunk extraction user prompt, with optional schema constraints.

    When `schema` is None or has no vocabularies, behavior matches pre-14.1 verbatim
    (default 4-bucket entity_type enum, free-form predicates).

    Phase 14.2: callers may pass `effective_*_vocab` to override the vocabulary
    rendered for this specific chunk (e.g. retrieved top-K instead of full schema).
    When None, the full sentinel-augmented vocab from `schema` is used.
    """
    # Decide entity_type enum for the JSON schema example
    if schema and schema.has_entity_schema:
        vocab = effective_entity_vocab or schema.entity_vocab
        entity_type_enum = " | ".join(vocab)
        entity_vocab_for_block = vocab
    else:
        entity_type_enum = " | ".join(_DEFAULT_ENTITY_TYPES)
        entity_vocab_for_block = None

    # Decide predicate description
    if schema and schema.has_relation_schema:
        vocab = effective_relation_vocab or schema.relation_vocab
        predicate_desc = " | ".join(vocab)
        relation_vocab_for_block = vocab
    else:
        predicate_desc = "short verb phrase label"
        relation_vocab_for_block = None

    # Build optional vocabulary constraint block. When a vocab name has a
    # known gloss in UNIVERSAL_*_GLOSSES it renders as `Name (gloss)`, which
    # gives the LLM the disambiguating signal it otherwise lacks. Names not
    # in the gloss dict (custom per-corpus schemas) render bare.
    vocab_block_lines: list[str] = []
    if entity_vocab_for_block or relation_vocab_for_block:
        vocab_block_lines.append("\nVocabulary constraints:")
        if entity_vocab_for_block:
            rendered = _render_vocab_constraint(
                entity_vocab_for_block,
                UNIVERSAL_ENTITY_GLOSSES,
                SchemaContext.ENTITY_SENTINEL,
            )
            vocab_block_lines.append(
                f"- entity_type MUST be one of: {rendered}"
            )
            vocab_block_lines.append(
                f"  Use the parenthetical gloss to disambiguate sibling types. "
                f"If no listed type fits, use '{SchemaContext.ENTITY_SENTINEL}'. "
                "Never invent a new type."
            )
        if relation_vocab_for_block:
            rendered = _render_vocab_constraint(
                relation_vocab_for_block,
                UNIVERSAL_RELATION_GLOSSES,
                SchemaContext.RELATION_SENTINEL,
            )
            vocab_block_lines.append(
                f"- predicate MUST be one of: {rendered}"
            )
            vocab_block_lines.append(
                f"  Use the parenthetical gloss to pick the narrowest fitting predicate. "
                f"If no listed predicate fits, use '{SchemaContext.RELATION_SENTINEL}'. "
                "Never invent a new predicate."
            )
    vocab_block = "\n".join(vocab_block_lines)
    lens_block = _render_schema_lens_block(schema_lens)
    entity_cap = max_entities or get_settings().EXTRACTION_MAX_ENTITIES_PER_CHUNK
    relation_cap = max_relations or get_settings().EXTRACTION_MAX_RELATIONS_PER_CHUNK

    return (
        "Extract named entities and relations from the text below.\n"
        "Return a JSON object matching this schema exactly:\n"
        "\n"
        "{\n"
        '  "schema_version": "polymath.extract.v1",\n'
        f'  "chunk_id": "{chunk_id}",\n'
        f'  "doc_id": "{doc_id}",\n'
        f'  "corpus_id": "{corpus_id}",\n'
        '  "entities": [\n'
        "    {\n"
        '      "canonical_name": "lowercase normalized key (no punctuation, spaces collapsed)",\n'
        '      "surface_form": "verbatim text as it appears",\n'
        f'      "entity_type": "{entity_type_enum}",\n'
        '      "confidence": 0.0\n'
        "    }\n"
        "  ],\n"
        '  "relations": [\n'
        "    {\n"
        '      "subject": "canonical_name of subject entity",\n'
        f'      "predicate": "{predicate_desc}",\n'
        '      "object": "canonical_name or literal string",\n'
        '      "object_kind": "entity | literal",\n'
        '      "confidence": 0.0,\n'
        '      "evidence_phrase": "short source phrase that proves the predicate"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "\n"
        "Rules:\n"
        f"- HARD LIMIT: output at most {entity_cap} entities and at most {relation_cap} relations for this chunk\n"
        "- Prefer high-confidence named entities and structurally useful relations; do not enumerate every proper noun, citation, example, list item, or generic noun\n"
        "- Keep JSON compact; no prose, markdown, comments, or duplicate entries\n"
        "- confidence: float 0.0–1.0; omit entries below threshold\n"
        "- canonical_name: lowercase, strip punctuation, collapse whitespace\n"
        "- entity_type is the entity's observed role in this chunk, not global identity\n"
        "- entity_type stays broad: Product for software/apps/libraries/services, "
        "Document for reports/books/papers, Artifact for tangible/code artifacts, "
        "Method for procedures/algorithms, Concept for abstract ideas\n"
        "- evidence_phrase must be a short exact or near-exact phrase from the text that explains the relation; "
        "leave it empty only when the relation is obvious from nearby syntax\n"
        '- relation object_kind "entity" only when the object is itself a named entity in the text\n'
        "- For product specs / PRDs: use 'produces' when a module/API/model outputs an artifact; "
        "use 'uses' when a feature/module consumes a model/API/database/data object; "
        "use 'depends_on' for hard prerequisites, limits, or constraints; "
        "use 'implements' when a concrete module/screen realizes an abstract concept; "
        "use 'part_of' for feature/module/screen containment. Prefer a specific predicate "
        f"over '{SchemaContext.RELATION_SENTINEL}' when the text gives enough evidence.\n"
        "- Relation intent families: part_of/member_of are structural; "
        "uses/calls/implements/depends_on/produces/stores/extracts/detects/classifies/runs_on/trained_on/supports are operational; "
        "references/derived_from/represents/maps_to are referential; causes/preceded_by are causal; "
        "contradicts/excepts/overrides are conflict. Choose the narrowest predicate "
        f"inside the right family; use '{SchemaContext.RELATION_SENTINEL}' only when the family is genuinely unclear.\n"
        "- Prefer 'runs_on' for model/app/device/platform execution, 'trained_on' for model-dataset training, "
        "'stores' for database/persistence relations, 'extracts' for entity/feature/data extraction, "
        "'detects' for finding objects/signals/events, 'classifies' for assigning categories, "
        "'calls' for API/function/service invocation, 'represents' for modeling/encoding, "
        "'maps_to' for transformations, and 'supports' for explicit capabilities.\n"
        "- Every relation subject/object with object_kind='entity' MUST also appear in entities, even if it is a generic endpoint like app, model, user, event, screen, or API.\n"
        f"- Use '{SchemaContext.RELATION_SENTINEL}' only when no explicit verb, containment cue, dependency cue, reference cue, runtime cue, or data-flow cue is present.\n"
        "- do NOT output ontology facet fields such as object_kind, domain_type, "
        "canonical_family, ontology_tags, or ontology_version; those are assigned deterministically after extraction\n"
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


@dataclass
class EntityItem:
    canonical_name: str
    surface_form: str
    entity_type: str  # person | org | concept | other
    confidence: float


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


@dataclass
class ExtractionResult:
    schema_version: str
    chunk_id: str
    doc_id: str
    corpus_id: str
    entities: list[EntityItem] = field(default_factory=list)
    relations: list[RelationItem] = field(default_factory=list)

    # Phase 14 observability counters (per-chunk; aggregated at corpus level later).
    entity_remap_count: int = 0   # soft mode: entity_type → 'other'
    entity_drop_count: int = 0    # hard mode: entity dropped
    relation_remap_count: int = 0  # soft mode: predicate → 'related_to'
    relation_drop_count: int = 0   # hard mode: relation dropped
    domain_range_remap_count: int = 0  # soft domain/range mismatch → related_to
    domain_range_warn_count: int = 0  # soft domain/range mismatch kept with warning
    endpoint_completion_count: int = 0  # missing relation endpoints added as entities
    evidence_cue_repair_count: int = 0  # evidence phrase repaired predicate/direction
    schema_lens_id: str | None = None


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
    return {
        "requested_chunks": total_chunks,
        "extracted_chunks": len(results),
        "failed_chunks": len(failures),
        "success_rate": round(len(results) / total_chunks, 4) if total_chunks else 1.0,
        "attempt_count": len(call_metrics),
        "models": models,
        "total_tokens": total_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_duration_seconds": round(total_duration, 3),
        "entity_count": sum(len(r.entities) for r in results),
        "relation_count": relation_count,
        "related_to_count": related_to_count,
        "related_to_ratio": round(related_to_count / relation_count, 4) if relation_count else 0.0,
        "entity_remap_count": sum(r.entity_remap_count for r in results),
        "relation_remap_count": sum(r.relation_remap_count for r in results),
        "domain_range_remap_count": sum(r.domain_range_remap_count for r in results),
        "domain_range_warn_count": sum(r.domain_range_warn_count for r in results),
        "endpoint_completion_count": sum(r.endpoint_completion_count for r in results),
        "evidence_cue_repair_count": sum(r.evidence_cue_repair_count for r in results),
        "entity_drop_count": sum(r.entity_drop_count for r in results),
        "relation_drop_count": sum(r.relation_drop_count for r in results),
        "schema_lens_ids": lens_ids,
        "error_counts": error_counts,
    }


def _entity_key(name: str) -> str:
    return " ".join(str(name or "").lower().split())


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
        "uses", "calls", "stores", "extracts", "detects", "classifies", "supports",
    }:
        return "Method"
    if role == "object" and predicate in {"uses", "calls", "runs_on", "stores"}:
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
            if e.entity_type in allowed:
                out_entities.append(e)
            elif schema.strict == "soft":
                out_entities.append(
                    EntityItem(
                        canonical_name=e.canonical_name,
                        surface_form=e.surface_form,
                        entity_type=SchemaContext.ENTITY_SENTINEL,
                        confidence=e.confidence,
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
                out_relations.append(_relation_with_remap(r, validation_status="schema_predicate_remap"))
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
    out_entities = _complete_relation_endpoint_entities(
        out_entities, out_relations, counters
    )
    out_relations = _apply_domain_range(out_entities, out_relations, counters)
    return out_entities, out_relations, counters


def _parse(
    raw: str,
    task: ExtractionTask,
    threshold: float,
    schema: SchemaContext | None = None,
    schema_lens: SchemaLens | dict | None = None,
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
            entities.append(
                EntityItem(
                    canonical_name=e["canonical_name"],
                    surface_form=e.get("surface_form", e["canonical_name"]),
                    entity_type=e.get("entity_type", "other"),
                    confidence=float(e["confidence"]),
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
        entities=entities,
        relations=relations,
        entity_remap_count=counters["entity_remap_count"],
        entity_drop_count=counters["entity_drop_count"],
        relation_remap_count=counters["relation_remap_count"],
        relation_drop_count=counters["relation_drop_count"],
        domain_range_remap_count=counters["domain_range_remap_count"],
        domain_range_warn_count=counters["domain_range_warn_count"],
        endpoint_completion_count=counters["endpoint_completion_count"],
        evidence_cue_repair_count=counters["evidence_cue_repair_count"],
        schema_lens_id=lens.lens_id if lens else None,
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
    max_entities = settings.EXTRACTION_MAX_ENTITIES_PER_CHUNK
    max_relations = settings.EXTRACTION_MAX_RELATIONS_PER_CHUNK
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
        chunk_vec = chunk_vectors.get(task.chunk_id) if chunk_vectors else None
        eff_entity, eff_relation = await resolve_chunk_vocab(
            schema=schema,
            chunk_vec=chunk_vec,
            resolver=schema_resolver,
            inline_limit=inline_limit,
            top_k=top_k,
        )
        payload: dict = {
            "model": entry["model"],
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": build_user_prompt(
                        chunk_id=task.chunk_id,
                        doc_id=task.doc_id,
                        corpus_id=task.corpus_id,
                        text=task.text,
                        max_entities=max_entities,
                        max_relations=max_relations,
                        schema=schema,
                        effective_entity_vocab=eff_entity,
                        effective_relation_vocab=eff_relation,
                        schema_lens=schema_lens,
                    ),
                },
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "max_tokens": max_completion_tokens,
        }
        if entry.get("base_url"):
            payload["api_base"] = entry["base_url"]
        if entry.get("api_key"):
            payload["api_key"] = entry["api_key"]
        for _k, _v in (entry.get("extra_params") or {}).items():
            if _k not in ("model", "messages", "response_format"):
                payload[_k] = _v

        # 2-attempt retry on the same lane. Work-stealing handles
        # cross-lane rebalancing naturally, so there's no need to jump
        # lanes on retry — most transient failures (rate-limit, 5xx,
        # JSON parse) recover on a fresh connection.
        last_exc: Exception | None = None
        last_error_type = "unknown"
        for attempt in range(2):
            started = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(
                        f"{settings.LITELLM_URL}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                    duration = time.perf_counter() - started
                    resp.raise_for_status()
                    body = resp.json()
                    usage = body.get("usage") or {}
                    logger.info(
                        "GHOST B extraction call: chunk_id=%s model=%s "
                        "duration=%.2fs total_tokens=%s prompt_tokens=%s "
                        "completion_tokens=%s lane=%d attempt=%d",
                        task.chunk_id,
                        entry["model"],
                        duration,
                        usage.get("total_tokens"),
                        usage.get("prompt_tokens"),
                        usage.get("completion_tokens"),
                        pool_idx,
                        attempt + 1,
                    )
                    raw = body["choices"][0]["message"]["content"]
                    result = _parse(raw, task, threshold, schema=schema, schema_lens=schema_lens)
                    call_metrics.append(
                        {
                            "chunk_id": task.chunk_id,
                            "model": entry["model"],
                            "lane": pool_idx,
                            "attempt": attempt + 1,
                            "duration_seconds": round(duration, 3),
                            "total_tokens": usage.get("total_tokens"),
                            "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens"),
                            "success": bool(result),
                            "error_type": None if result else "parse_error",
                        }
                    )
                    if result:
                        logger.debug(
                            "GHOST B: chunk_id=%s entities=%d relations=%d "
                            "(attempt=%d lane=%d)",
                            task.chunk_id,
                            len(result.entities),
                            len(result.relations),
                            attempt + 1,
                            pool_idx,
                        )
                        return result
                    last_error_type = "parse_error"
                    last_exc = RuntimeError("parse returned None")
            except Exception as exc:
                last_exc = exc
                last_error_type = exc.__class__.__name__
                fatal_tier = provider_error_tier(exc)
                fatal_lane = fatal_tier is not None
                call_metrics.append(
                    {
                        "chunk_id": task.chunk_id,
                        "model": entry["model"],
                        "lane": pool_idx,
                        "attempt": attempt + 1,
                        "duration_seconds": round(time.perf_counter() - started, 3),
                        "total_tokens": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "success": False,
                        "error_type": (
                            f"{fatal_tier}_fatal_lane_error"
                            if fatal_lane
                            else last_error_type
                        ),
                    }
                )
                if fatal_lane:
                    raise FatalLaneError(exc) from exc
                if attempt == 0:
                    logger.warning(
                        "GHOST B lane %d failed chunk_id=%s attempt=%d: %s — retrying",
                        pool_idx, task.chunk_id, attempt + 1, exc,
                    )
                continue
        logger.error(
            "GHOST B failed chunk_id=%s lane=%d after 2 attempts: %s",
            task.chunk_id, pool_idx, last_exc,
        )
        failures_list.append(
            ExtractionFailureItem(
                chunk_id=task.chunk_id,
                doc_id=task.doc_id,
                corpus_id=task.corpus_id,
                model=str(entry["model"]),
                lane=pool_idx,
                attempts=2,
                error_type=last_error_type,
                error_message=str(last_exc)[:1000],
            )
        )
        return None

    async def _lane_worker(pool_idx: int) -> None:
        """One coroutine per lane slot. Drains the shared queue until empty."""
        nonlocal failed_count
        while True:
            if pool_idx in disabled_lanes:
                return
            try:
                task = task_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if pool_idx in disabled_lanes:
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
            finally:
                task_queue.task_done()

    async def _run_enabled_workers() -> None:
        # Spawn total_concurrency workers = sum of enabled per-lane max_concurrent.
        workers: list[asyncio.Task] = []
        for pool_idx, entry in enumerate(pool):
            if pool_idx in disabled_lanes:
                continue
            slots = int(entry.get("max_concurrent") or 1) or 1
            for _ in range(slots):
                workers.append(asyncio.create_task(_lane_worker(pool_idx)))
        if workers:
            await asyncio.gather(*workers, return_exceptions=False)

    await _run_enabled_workers()
    while not task_queue.empty():
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
