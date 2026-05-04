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
from datetime import datetime, timedelta
from typing import Awaitable, Callable, ClassVar, Literal

import httpx

from config import get_settings
from services.llm_lane_pool import (
    FatalLaneError,
    SOFT_FATAL_DISABLE_STRIKES,
    provider_error_tier,
    provider_error_summary,
)
from services.ontology import (
    entity_gloss_map,
    entity_type_names,
    relation_alias_tuple_map,
    relation_domain_range_map,
    relation_family_map,
    relation_gloss_map,
    relation_type_names,
    render_relation_decision_block,
)
from utils.tokens import count_tokens_messages, get_model_context_limit

# Phase 14.2 — pluggable schema retriever. Worker injects a closure over qdrant_client +
# corpus_id so this module stays independent of the Qdrant SDK.
#   args: (kind, query_vec, top_k)
#   returns: list of allowed terms ranked by similarity
SchemaResolver = Callable[[str, list[float], int], Awaitable[list[str]]]

logger = logging.getLogger(__name__)

PRIMARY_EXTRACTION_MODEL = "LFM2-1.2B-Extract"
REPAIR_EXTRACTION_MODEL = "Gemma-4-E4B"
TARGET_SCHEMA_VERSION = "polymath.extract.v2"
TRIPLE_REPAIR_CONFIDENCE_THRESHOLD = 0.70

_TARGET_RELATION_FAMILY_MAP = relation_family_map()
_RELATION_FAMILIES = sorted(set(_TARGET_RELATION_FAMILY_MAP.values()) | {"WeakAssociation"})
_EXTRACTION_CONTEXT_SAFETY_MARGIN = 64
_MIN_EXTRACTION_OUTPUT_TOKENS = 256
_DETERMINISTIC_ERROR_TYPES = {
    "token_budget",
    "bad_request",
    "unprocessable_entity",
}
_GEMMA_REPAIR_MODEL_HINTS = ("gemma4-e4b", "gemma-4-e4b")
_GEMMA_REPAIR_TEMPERATURE = 1.0
_GEMMA_REPAIR_TOP_P = 0.95
_GEMMA_REPAIR_TOP_K = 64
_TARGET_EXTRACTION_SCHEMA = (
    "Return data as a JSON object with this exact schema:\n"
    "{\n"
    '  "schema_version": "polymath.extract.v2",\n'
    '  "chunk_id": "string",\n'
    '  "doc_id": "string",\n'
    '  "corpus_id": "string",\n'
    '  "entities": [\n'
    "    {\n"
    '      "name": "verbatim entity name from the text",\n'
    '      "type": "approved entity type",\n'
    '      "aliases": ["verbatim aliases from the text"],\n'
    '      "description": "short description grounded only in the text"\n'
    "    }\n"
    "  ],\n"
    '  "relations": [\n'
    "    {\n"
    '      "subject": "entity name present in entities",\n'
    '      "predicate": "short relation predicate",\n'
    '      "predicate_family": "Structural | Spatial | Affiliation | Provenance | Operational | Referential | Analytical | Causal | Psychosocial | Interpretive | Strategic | Conflict | WeakAssociation",\n'
    '      "object": "entity name present in entities",\n'
    '      "qualifier": "optional qualifier from the text, or empty string",\n'
    '      "confidence": 0.0,\n'
    '      "source_sentence": "the shortest source sentence that explicitly states the relation"\n'
    "    }\n"
    "  ],\n"
    '  "objects": [\n'
    "    {\n"
    '      "name": "named non-entity object if useful",\n'
    '      "type": "object type",\n'
    '      "attributes": {"key": "value grounded in the text"}\n'
    "    }\n"
    "  ]\n"
    "}\n"
)

_SYSTEM = (
    "You are GHOST B, a schema-bound entity and relation extractor running in "
    "strict JSON contract mode for LFM2-1.2B-Extract. Output exactly one "
    "complete JSON object and nothing else. Do not use markdown, comments, "
    "apologies, explanations, or text outside the JSON object. Extract only "
    "facts explicitly stated in the text; do not infer beyond the text and do "
    "not hallucinate entities or relations. If evidence is sparse, return "
    "empty arrays. Prefer fewer high-confidence items over long output. "
    "Before finalizing, silently verify that every string, array, and object "
    "is closed and that the output can be parsed by json.loads.\n\n"
    + _TARGET_EXTRACTION_SCHEMA
)

_JSON_RECOVERY_SUFFIX = (
    "\n\nRECOVERY MODE: the previous extraction attempt for this chunk did not "
    "parse as JSON. Re-run the extraction from scratch using the same schema. "
    "Return a minimal valid JSON object, with fewer entities and relations if "
    "needed. Keep evidence phrases under 12 words, rejection_reasoning under "
    "8 words, and alternative_predicates_considered to at most 2 items. The "
    "response must be a single complete JSON object that parses cleanly."
)

PREDICATE_CONFIDENCE_DEMOTE_THRESHOLD = 0.60
HIGH_VALUE_EXTRACTION_CONFIDENCE_THRESHOLD = 0.75

# Default open-vocabulary enums when no schema is provided.
_DEFAULT_ENTITY_TYPES = ["person", "org", "concept", "other"]


def build_target_extraction_json_schema(
    *,
    chunk_id: str,
    doc_id: str,
    corpus_id: str,
    entity_vocab: list[str] | None = None,
    relation_vocab: list[str] | None = None,
    max_entities: int = 14,
    max_relations: int = 14,
) -> dict:
    """Strict JSON Schema enforced by vLLM through response_format."""

    entity_type_schema: dict = {"type": "string", "maxLength": 120}
    if entity_vocab:
        entity_type_schema["enum"] = list(dict.fromkeys(entity_vocab))
    predicate_schema: dict = {"type": "string", "maxLength": 200}
    if relation_vocab:
        predicate_schema["enum"] = list(dict.fromkeys(relation_vocab))
    short_string = {"type": "string", "maxLength": 200}
    medium_string = {"type": "string", "maxLength": 500}
    sentence_string = {"type": "string", "maxLength": 800}

    # Note: chunk_id / doc_id / corpus_id are NOT pinned to enum here. The
    # earlier version (`enum: [chunk_id]`) baked these unique values into the
    # JSON Schema, which forced vllm to compile a fresh structured-output FSM
    # per call. That defeated prefix caching at the schema level (run logs
    # showed `Prefix cache hit rate: 0.0%`). Validating these on the backend
    # after parse keeps the schema deterministic across calls so vllm can
    # reuse the FSM and prefill cache.
    _ = (chunk_id, doc_id, corpus_id)
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "enum": [TARGET_SCHEMA_VERSION]},
            "chunk_id": {"type": "string", "maxLength": 200},
            "doc_id": {"type": "string", "maxLength": 200},
            "corpus_id": {"type": "string", "maxLength": 200},
            "entities": {
                "type": "array",
                "maxItems": max(0, max_entities),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": short_string,
                        "type": entity_type_schema,
                        "aliases": {
                            "type": "array",
                            "maxItems": 8,
                            "items": short_string,
                        },
                        "description": medium_string,
                    },
                    "required": ["name", "type", "aliases", "description"],
                    "additionalProperties": False,
                },
            },
            "relations": {
                "type": "array",
                "maxItems": max(0, max_relations),
                "items": {
                    "type": "object",
                    "properties": {
                        "subject": short_string,
                        "predicate": predicate_schema,
                        "predicate_family": {
                            "type": "string",
                            "enum": _RELATION_FAMILIES,
                        },
                        "object": short_string,
                        "qualifier": medium_string,
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "source_sentence": sentence_string,
                    },
                    "required": [
                        "subject",
                        "predicate",
                        "predicate_family",
                        "object",
                        "qualifier",
                        "confidence",
                        "source_sentence",
                    ],
                    "additionalProperties": False,
                },
            },
            "objects": {
                "type": "array",
                "maxItems": 0,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": short_string,
                        "type": short_string,
                        "attributes": {
                            "type": "object",
                            "maxProperties": 20,
                            "additionalProperties": medium_string,
                        },
                    },
                    "required": ["name", "type", "attributes"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "schema_version",
            "chunk_id",
            "doc_id",
            "corpus_id",
            "entities",
            "relations",
            "objects",
        ],
        "additionalProperties": False,
    }


def _json_schema_response_format(schema: dict) -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "extraction",
            "schema": schema,
            "strict": True,
        },
    }


def _compact_system_prompt(*, recovery_mode: bool) -> str:
    base = (
        "You are GHOST B, an entity and relation extractor. Extract only facts "
        "explicitly stated in the text. Prefer fewer high-confidence entities "
        "and relations. Relation endpoints must be named entities in the output. "
        "Use related_to only for vague association or low predicate confidence. "
        "Emit compact minified JSON; do not indent or pad with whitespace."
    )
    if recovery_mode:
        return base + " Return the smallest valid extraction that preserves stated facts."
    return base


def _compact_user_prompt(
    *,
    task: "ExtractionTask",
    max_entities: int,
    max_relations: int,
    compact_mode: bool,
    schema_lens: "SchemaLens | dict | None",
) -> str:
    lens = _render_schema_lens_block(schema_lens)
    compact_note = (
        "Large-document compact mode: extract only central named entities and "
        "the strongest explicit relations."
        if compact_mode
        else "Extract central named entities and explicit relations."
    )
    return (
        f"chunk_id: {task.chunk_id}\n"
        f"doc_id: {task.doc_id}\n"
        f"corpus_id: {task.corpus_id}\n"
        f"Limits: at most {max_entities} entities and {max_relations} relations.\n"
        f"{compact_note}\n"
        "For each relation, source_sentence must be the shortest sentence that proves it.\n"
        "Set objects to an empty array.\n"
        f"{lens}\n\n"
        "TEXT:\n"
        f"{task.text}"
    )


def _safe_completion_budget(
    *,
    messages: list[dict[str, str]],
    model: str,
    requested_tokens: int,
    context_limit_override: int | None = None,
) -> tuple[int | None, dict[str, int]]:
    """Pre-flight token budget for an extraction call.

    `context_limit_override` lets callers pass the lane's authoritative context
    window (from ModelProfileRef.context_length). The static
    get_model_context_limit registry only knows public models — local
    fine-tunes like lfm2-extract @ 12288 would otherwise default to 4096 and
    starve the budget against their own legitimate context.
    """
    context_limit = context_limit_override or get_model_context_limit(model)
    prompt_tokens = count_tokens_messages(messages, model)
    available = context_limit - prompt_tokens - _EXTRACTION_CONTEXT_SAFETY_MARGIN
    budget = {
        "context_limit": context_limit,
        "prompt_tokens_estimate": prompt_tokens,
        "available_completion_tokens": available,
        "requested_completion_tokens": requested_tokens,
        "safety_margin_tokens": _EXTRACTION_CONTEXT_SAFETY_MARGIN,
    }
    if available < _MIN_EXTRACTION_OUTPUT_TOKENS:
        return None, budget
    return min(requested_tokens, available), budget


def _entry_is_local_vllm(entry: dict) -> bool:
    base_url = str(entry.get("base_url") or "")
    model = str(entry.get("model") or "")
    return "vllm-" in base_url or model.startswith("openai/lfm2-") or model.startswith("lfm2-")


def _entry_concurrency_slots(entry: dict, *, extraction_mode: str) -> int:
    """Resolve the effective per-doc concurrency for one extraction lane.

    Local vLLM lanes used to be capped at 8 / 16 (compact / normal) to
    protect small GPUs from over-committing. On high-VRAM cards (RTX Pro
    6000 Blackwell with 97 GB, H100 NVL, etc.) those caps starve vllm —
    which schedules `max_num_seqs=256` natively — and Ghost B grinds at
    ~10% throughput. Two settings now override the legacy caps:

      LOCAL_VLLM_COMPACT_MAX_CONCURRENT  default 64  (was 8)
      LOCAL_VLLM_NORMAL_MAX_CONCURRENT   default 128 (was 16)

    The corpus's `max_concurrent` is the ceiling; this function clamps
    against the env-configured local-vllm safety cap. Cloud lanes are
    unaffected — they use the corpus value directly.
    """
    requested = max(1, int(entry.get("max_concurrent") or 1))
    if not _entry_is_local_vllm(entry):
        return requested
    settings = get_settings()
    if extraction_mode == "compact":
        cap = int(getattr(settings, "LOCAL_VLLM_COMPACT_MAX_CONCURRENT", 64))
    else:
        cap = int(getattr(settings, "LOCAL_VLLM_NORMAL_MAX_CONCURRENT", 128))
    return min(requested, max(1, cap))


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
    (re.compile(r"\b(placed|visible|appears?)\s+(inside|in|within)\b|\bshown\s+(inside|within)\b|\binside the\b"), "part_of", False),
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
    # Academic/statistical/book cues.
    (re.compile(r"\bmeasured\s+by\b"), "measures", True),
    (re.compile(r"\b(evaluates?|scores?|estimates?)\b.*\b(traits?|values?|scores?|quantit(?:y|ies)|metrics?|ratings?|measures?|estimates?|latent\s+traits?)\b"), "measures", False),
    (re.compile(r"\b(measures?|quantifies?)\b"), "measures", False),
    (re.compile(r"\b(evaluates?|checks?)\b.*\b(conditions?|assumptions?|hypotheses|hypothesis|constraints?|qualities|whether|model\s+fit|invariance)\b"), "tests", False),
    (re.compile(r"\b(tests?|validates?|falsifies?)\b"), "tests", False),
    (re.compile(r"\b(applied|applies|performed)\s+(to|on)\b"), "applied_to", False),
    (re.compile(r"\b(defined|specified|introduced|stated)\s+in\b"), "defined_in", False),
    (re.compile(r"\b(depicted|shown|illustrated|demonstrated)\s+(in|by)\b"), "illustrated_in", False),
    (re.compile(r"\b(follows?|is\s+drawn\s+from|distributed\s+as)\b.*\b(distribution|curve|law|pattern)\b"), "follows_distribution", False),
    (re.compile(r"\b(parameter|threshold|setting|variable)s?\s+(of|for)\b"), "parameter_of", False),
    (re.compile(r"\b(equivalent\s+to|same\s+as|also\s+called|referred\s+to\s+as)\b"), "equivalent_to", False),
    # Interpretive/self-growth/narrative cues.
    (re.compile(r"\b(activates?|activated|stimulates?|stimulated)\b"), "activates", False),
    (re.compile(r"\b(experiences?|experienced|undergoes?|feels?|felt)\b"), "experiences", False),
    (re.compile(r"\bexpress(?:es|ed|ing)?\s+(relief|freedom|fear|joy|shame|guilt|anger|sadness|grief|pain|anxiety|emotion|feeling|loss|love)\b"), "experiences", False),
    (re.compile(r"\b(imagines?|imagined|visuali[sz]es?|pictured?|envisions?|anticipates?)\b"), "imagines", False),
    (re.compile(r"\b(studies|studied|researches?|investigates?|examines?)\b(?!\s+in\b)"), "studies", False),
    (re.compile(r"\b(embodies?|personif(?:y|ies))\b"), "embodies", False),
    (re.compile(r"\b(symboli[sz]es?|stands?\s+for|signifies?)\b"), "symbolizes", False),
    (re.compile(r"\b(influences?|shapes?|affects?|pressures?)\b"), "influences", False),
    (re.compile(r"\b(driven\s+by|motivated\s+by)\b"), "motivates", True),
    (re.compile(r"\b(motivates?|drives?)\b"), "motivates", False),
    (re.compile(r"\b(struggles?\s+with|wrestles?\s+with|conflicted\s+by)\b"), "struggles_with", False),
    (re.compile(r"\b(reinforces?|strengthens?|normalizes?|intensifies?)\b"), "reinforces", False),
    (re.compile(r"\b(undermines?|weakens?|erodes?|destabilizes?|subverts?)\b"), "undermines", False),
    (re.compile(r"\b(frames?\s+as|presents?\s+as|casts?\s+as|positions?\s+as)\b"), "frames_as", False),
    (re.compile(r"\b(conceals?|hides?|masks?|disguises?|withholds?)\b"), "conceals", False),
    (re.compile(r"\b(leverages?|exploits?|uses?\s+strategically)\b"), "leverages", False),
]

# Runtime ontology contract. The legacy literals above are kept as readable
# fallback documentation, but Ghost B uses the shared ontology file below so
# prompt vocab, aliases, and validators cannot drift apart.
UNIVERSAL_ENTITY_SCHEMA = entity_type_names()
UNIVERSAL_RELATION_SCHEMA = relation_type_names()
UNIVERSAL_ENTITY_GLOSSES = entity_gloss_map()
UNIVERSAL_RELATION_GLOSSES = relation_gloss_map()
DOMAIN_RANGE_MAP = relation_domain_range_map()
RELATION_ALIAS_MAP = relation_alias_tuple_map()
_RELATION_TYPE_BY_KEY = {
    re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"): name
    for name in [*UNIVERSAL_RELATION_SCHEMA, "related_to"]
}


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
    if key in _RELATION_TYPE_BY_KEY:
        return _RELATION_TYPE_BY_KEY[key], False
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


def _render_compact_output_rules(*, recovery_mode: bool = False) -> str:
    """Render output-budget rules that preserve ontology guidance while limiting JSON size."""
    lines = [
        "- COMPACT OUTPUT BUDGET: valid JSON beats exhaustive extraction; prefer fewer high-confidence triples over a response that risks truncation",
        "- source_sentence <= 35 words; quote only the shortest full sentence that proves the relation; legacy evidence_phrase <= 25 words is accepted by the parser only",
        "- qualifier <= 12 words; use an empty string when the text has no explicit qualifier",
        "- Legacy parser note: atomic_fact <= 25 words, rejection_reasoning <= 10 words, and alternative_predicates_considered: max 2 predicates when old rows are encountered; do not output those fields in the target schema",
        "- relation descriptions and entity descriptions must be compact; no reasoning paragraphs",
        "- Do not repeat long evidence; source_sentence is the provenance field for relations",
    ]
    if recovery_mode:
        lines.extend(
            [
                "- RECOVERY MODE OUTPUT: return minimal valid JSON only; omit borderline facts and low-value examples",
                "- RECOVERY MODE OUTPUT: source_sentence <= 25 words and qualifier <= 8 words; legacy evidence_phrase <= 12 words accepted by parser only",
                "- RECOVERY MODE OUTPUT: relations must be fewer than or equal to entities; legacy candidate_facts must not exceed accepted relations",
            ]
        )
    return "\n".join(lines)


def _render_large_doc_compact_rules() -> str:
    """Rules for long-book first-pass extraction.

    This intentionally keeps the full ontology decision block elsewhere in the
    prompt. The compact mode only changes how much the model is allowed to
    return for a chunk; it does not loosen schema governance or hide
    related_to as a diagnostic fallback.
    """
    return "\n".join(
        [
            "- LARGE-DOC COMPACT MODE: this is a first-pass graph extraction for a long document",
            "- Extract only the strongest graph-useful entities and relations in this chunk",
            "- Prefer durable concepts, named systems, methods, documents, people, organizations, datasets, models, and explicit claims",
            "- Skip repeated headers/footers, citation boilerplate, publisher/copyright lines, long lists, glossary-only rows, and weak examples",
            "- Prefer one narrow predicate with short evidence over several speculative alternatives",
            "- Keep related_to only for genuinely unclear or low-confidence predicate choices",
        ]
    )


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
    compact_mode: bool = False,
    recovery_mode: bool = False,
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
    decision_block = render_relation_decision_block(relation_vocab_for_block)
    lens_block = _render_schema_lens_block(schema_lens)
    compact_rules = _render_compact_output_rules(recovery_mode=recovery_mode)
    large_doc_rules = _render_large_doc_compact_rules() if compact_mode else ""
    entity_cap = max_entities or get_settings().EXTRACTION_MAX_ENTITIES_PER_CHUNK
    relation_cap = max_relations or get_settings().EXTRACTION_MAX_RELATIONS_PER_CHUNK

    return (
        "Extract named entities and relations from the text below.\n"
        "Return a JSON object matching the LFM2-Extract schema exactly:\n"
        "\n"
        "{\n"
        '  "schema_version": "polymath.extract.v2",\n'
        f'  "chunk_id": "{chunk_id}",\n'
        f'  "doc_id": "{doc_id}",\n'
        f'  "corpus_id": "{corpus_id}",\n'
        '  "entities": [\n'
        "    {\n"
        '      "name": "verbatim entity name from the text",\n'
        f'      "type": "{entity_type_enum}",\n'
        '      "aliases": ["verbatim aliases from the text"],\n'
        '      "description": "short description grounded only in the text"\n'
        "    }\n"
        "  ],\n"
        '  "relations": [\n'
        "    {\n"
        '      "subject": "entity name present in entities",\n'
        f'      "predicate": "{predicate_desc}",\n'
        '      "predicate_family": "Structural | Spatial | Affiliation | Provenance | Operational | Referential | Analytical | Causal | Psychosocial | Interpretive | Strategic | Conflict | WeakAssociation",\n'
        '      "object": "entity name present in entities",\n'
        '      "qualifier": "optional qualifier from the text, or empty string",\n'
        '      "confidence": 0.0,\n'
        '      "source_sentence": "the shortest sentence that explicitly states the relation"\n'
        "    }\n"
        "  ],\n"
        '  "objects": [\n'
        "    {\n"
        '      "name": "named non-entity object if useful",\n'
        '      "type": "object type",\n'
        '      "attributes": {"key": "value grounded in the text"}\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "\n"
        "Rules:\n"
        f"- HARD LIMIT: output at most {entity_cap} entities and at most {relation_cap} relations for this chunk\n"
        f"{compact_rules}\n"
        f"{large_doc_rules}\n"
        "- Prefer high-confidence named entities and structurally useful relations; do not enumerate every proper noun, citation, example, list item, or generic noun\n"
        "- Keep JSON compact; no prose, markdown, comments, or duplicate entries\n"
        "- confidence: numeric float 0.0-1.0; do not quote it as a string; omit entries below 0.70\n"
        "- source_sentence is required whenever the relation is stated in a sentence; copy the shortest sentence that proves the triple\n"
        "- Do not output legacy candidate_facts; the compatibility parser accepts old candidate_facts/evidence_phrase fields, but LFM2-Extract should use only entities, relations, and objects.\n"
        "- relation subject and object MUST match entity names in entities after simple case/space normalization; do not invent endpoints.\n"
        "- name: preserve the entity's text form; aliases are only aliases explicitly present in the text\n"
        "- type is the entity's observed role in this chunk, not global identity\n"
        "- type stays broad: Product for software/apps/libraries/services, "
        "Document for reports/books/papers, Artifact for tangible/code artifacts, "
        "Method for procedures/algorithms, Concept for abstract ideas\n"
        "- predicate_family must be the best family for the predicate. Do not use WeakAssociation unless the source sentence only states a vague association.\n"
        "- For product specs / PRDs: use 'produces' when a module/API/model outputs an artifact; "
        "use 'uses' when a feature/module consumes a model/API/database/data object; "
        "use 'depends_on' for hard prerequisites, limits, or constraints; "
        "use 'implements' when a concrete module/screen realizes an abstract concept; "
        "use 'part_of' for feature/module/screen containment. Prefer a specific predicate "
        f"over '{SchemaContext.RELATION_SENTINEL}' when the text gives enough evidence.\n"
        "- Relation intent families: part_of/member_of are structural; "
        "uses/calls/implements/depends_on/produces/stores/extracts/detects/classifies/runs_on/trained_on/supports are operational; "
        "references/derived_from/represents/maps_to/defined_in/illustrated_in/equivalent_to are referential; "
        "measures/follows_distribution/tests/applied_to/studies are analytical; parameter_of is structural; "
        "causes/preceded_by/activates/influences/motivates/reinforces/undermines are causal; "
        "embodies/symbolizes/frames_as are interpretive; experiences/imagines/struggles_with are psychosocial; "
        "conceals/leverages are strategic; contradicts/excepts/overrides are conflict. Choose the narrowest predicate "
        f"inside the right family; use '{SchemaContext.RELATION_SENTINEL}' only when the family is genuinely unclear.\n"
        "- Governance scopes: measures/defined_in/follows_distribution/tests/applied_to/illustrated_in/parameter_of/equivalent_to/activates/experiences/imagines/studies are evidence-backed repair predicates for patterns seen in current related_to samples. "
        "embodies/symbolizes/influences/motivates/struggles_with/reinforces/undermines/frames_as/conceals/leverages are future expansion predicates for literature, self-growth, power, and social-dynamics corpora; use them only when the text explicitly states that meaning, force, motive, conflict, concealment, or strategy. "
        f"'{SchemaContext.RELATION_SENTINEL}' is a valid diagnostic fallback, not a failure.\n"
        "- Prefer 'runs_on' for model/app/device/platform execution, 'trained_on' for model-dataset training, "
        "'stores' for database/persistence relations, 'extracts' for entity/feature/data extraction, "
        "'detects' for finding objects/signals/events, 'classifies' for assigning categories, "
        "'calls' for API/function/service invocation, 'represents' for modeling/encoding, "
        "'maps_to' for transformations, 'supports' for explicit capabilities, "
        "'defined_in' for equation/figure/section/document/spec definitions, 'follows_distribution' for distributional claims, "
        "'activates' for explicit trigger/activation evidence, 'experiences' for felt/undergone states, "
        "'imagines' for mental visualization, 'studies' for systematic inquiry, "
        "'equivalent_to' for aliases, 'embodies'/'symbolizes' for literary meaning, and "
        "'motivates'/'struggles_with'/'reinforces'/'undermines' for self-growth or emotional dynamics.\n"
        "- Every relation subject/object with object_kind='entity' MUST also appear in entities, even if it is a generic endpoint like app, model, user, event, screen, or API.\n"
        f"- Keep '{SchemaContext.RELATION_SENTINEL}' for co-occurrence, see-also links, vague similarity/comparison, low predicate confidence, or interpretive claims without explicit evidence. Use it when no explicit verb, containment cue, dependency cue, reference cue, runtime cue, data-flow cue, or governed repair cue is present.\n"
        "- Legacy relation object_kind must only be 'entity' or 'literal' if encountered; do not output object_kind in the target schema.\n"
        "- do NOT output ontology facet fields such as domain_type, canonical_family, "
        "ontology_tags, or ontology_version; those are assigned deterministically after extraction\n"
        f"{vocab_block}\n"
        f"{decision_block}\n"
        f"{lens_block}\n"
        "\n"
        "TEXT:\n"
        f"{text}"
    )


def _split_extraction_contract(prompt: str) -> tuple[str, str]:
    """Move the JSON/schema contract into the system message for LFM2-Extract.

    Liquid's Extract checkpoints are tuned for a ChatML shape where the schema
    lives in the system role and the source document lives in the user role.
    """
    marker = "\n\nTEXT:\n"
    contract, found, source_text = prompt.partition(marker)
    if not found:
        return prompt, ""
    return contract, f"TEXT:\n{source_text}"


@dataclass
class ExtractionTask:
    chunk_id: str
    doc_id: str
    corpus_id: str
    text: str  # child chunk text only
    document_title: str | None = None
    heading_path: list[str] | None = None
    chunk_kind: str | None = None


@dataclass
class EntityItem:
    canonical_name: str
    surface_form: str
    entity_type: str  # person | org | concept | other
    confidence: float
    aliases: list[str] = field(default_factory=list)
    description: str = ""


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
    predicate_confidence: float | None = None
    extraction_confidence: float | None = None
    alternative_predicates_considered: list[str] = field(default_factory=list)
    rejection_reasoning: str = ""
    atomic_fact: str = ""
    candidate_subject: str | None = None
    candidate_predicate: str | None = None
    candidate_object: str | None = None
    review_status: str | None = None
    predicate_family: str | None = None
    qualifier: str = ""
    source_sentence: str = ""
    extraction_model: str = PRIMARY_EXTRACTION_MODEL
    repaired: bool = False
    repair_reasons: list[str] = field(default_factory=list)


@dataclass
class ObjectItem:
    name: str
    type: str
    attributes: dict = field(default_factory=dict)


@dataclass
class CandidateFactItem:
    atomic_fact: str
    candidate_subject: str
    candidate_predicate: str
    candidate_object: str
    predicate_confidence: float
    extraction_confidence: float
    alternative_predicates_considered: list[str] = field(default_factory=list)
    rejection_reasoning: str = ""
    evidence_phrase: str = ""
    object_kind: str = "entity"
    relation_cue: str = ""


@dataclass
class ExtractionResult:
    schema_version: str
    chunk_id: str
    doc_id: str
    corpus_id: str
    entities: list[EntityItem] = field(default_factory=list)
    candidate_facts: list[CandidateFactItem] = field(default_factory=list)
    relations: list[RelationItem] = field(default_factory=list)
    objects: list[ObjectItem] = field(default_factory=list)

    # Phase 14 observability counters (per-chunk; aggregated at corpus level later).
    entity_remap_count: int = 0   # soft mode: entity_type → 'other'
    entity_drop_count: int = 0    # hard mode: entity dropped
    relation_remap_count: int = 0  # soft mode: predicate → 'related_to'
    relation_drop_count: int = 0   # hard mode: relation dropped
    domain_range_remap_count: int = 0  # soft domain/range mismatch → related_to
    domain_range_warn_count: int = 0  # soft domain/range mismatch kept with warning
    endpoint_completion_count: int = 0  # missing relation endpoints added as entities
    evidence_cue_repair_count: int = 0  # evidence phrase repaired predicate/direction
    direction_repair_count: int = 0  # alias/evidence repair reversed relation direction
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
    retryable: bool = True
    retry_after: datetime | None = None
    backfill_attempt_count: int = 0
    lane_state: str | None = None


@dataclass
class ExtractionBatchReport:
    results: list[ExtractionResult]
    failures: list[ExtractionFailureItem]
    metrics: dict
    relation_repairs: list["RelationRepairCandidate"] = field(default_factory=list)


@dataclass
class RelationRepairItem:
    relation: RelationItem
    reasons: list[str]


@dataclass
class RelationRepairCandidate:
    chunk_id: str
    doc_id: str
    corpus_id: str
    schema_version: str
    relation: RelationItem
    reasons: list[str]
    source_sentence: str
    entity_names: list[str]
    entity_snapshot: list[EntityItem] = field(default_factory=list)
    schema_snapshot: dict = field(default_factory=dict)
    schema_lens_id: str | None = None


def _provider_error_kind(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return "rate_limited"
        text = provider_error_summary(exc).lower()
        if "context length" in text or "input_tokens" in text or "maximum context" in text:
            return "token_budget"
        if status == 400:
            return "bad_request"
        if status == 422:
            return "unprocessable_entity"
        return "provider_error"
    if isinstance(exc, httpx.RequestError):
        return "provider_error"
    return exc.__class__.__name__


def _is_deterministic_provider_error(exc: Exception) -> bool:
    return _provider_error_kind(exc) in _DETERMINISTIC_ERROR_TYPES


def _retryable_infrastructure_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        if _is_deterministic_provider_error(exc):
            return False
        return exc.response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
    return isinstance(exc, httpx.RequestError)


def summarize_extraction_batch(
    *,
    total_chunks: int,
    results: list[ExtractionResult],
    failures: list[ExtractionFailureItem],
    call_metrics: list[dict],
    models: list[str],
    metrics_context: dict | None = None,
) -> dict:
    """Return compact extraction metrics suitable for Mongo/audit surfaces."""
    total_tokens = sum(int(m.get("total_tokens") or 0) for m in call_metrics)
    prompt_tokens = sum(int(m.get("prompt_tokens") or 0) for m in call_metrics)
    completion_tokens = sum(int(m.get("completion_tokens") or 0) for m in call_metrics)
    total_duration = sum(float(m.get("duration_seconds") or 0.0) for m in call_metrics)
    attempt_count = len(call_metrics)
    json_recovery_count = sum(1 for m in call_metrics if m.get("recovery_mode"))
    truncated_call_count = sum(1 for m in call_metrics if m.get("truncated"))
    repair_model_count = sum(1 for m in call_metrics if m.get("repair_model"))
    relation_count = sum(len(r.relations) for r in results)
    related_to_count = sum(
        1
        for r in results
        for rel in r.relations
        if rel.predicate == "related_to"
    )
    predicate_confidences = [
        float(rel.predicate_confidence)
        for r in results
        for rel in r.relations
        if rel.predicate_confidence is not None
    ]
    success_rate = round(len(results) / total_chunks, 4) if total_chunks else 1.0
    lens_ids = sorted({r.schema_lens_id for r in results if r.schema_lens_id})
    error_counts: dict[str, int] = {}
    for failure in failures:
        error_counts[failure.error_type] = error_counts.get(failure.error_type, 0) + 1
    retryable_failures = sum(1 for failure in failures if failure.retryable)
    retry_after_values = [
        failure.retry_after
        for failure in failures
        if failure.retry_after is not None
    ]
    retry_after = min(retry_after_values).isoformat() if retry_after_values else None
    skipped_low_value = int((metrics_context or {}).get("skipped_low_value_chunks") or 0)
    metrics = {
        "requested_chunks": total_chunks,
        "extracted_chunks": len(results),
        "failed_chunks": len(failures),
        "failed_chunk_count": len(failures),
        "success_rate": success_rate,
        "ghost_b_success_rate": success_rate,
        "attempt_count": attempt_count,
        "json_recovery_count": json_recovery_count,
        "json_recovery_rate": (
            round(json_recovery_count / total_chunks, 4) if total_chunks else 0.0
        ),
        "json_recovery_attempt_rate": (
            round(json_recovery_count / attempt_count, 4) if attempt_count else 0.0
        ),
        "ghost_b_total_chunks": total_chunks + skipped_low_value,
        "ghost_b_skipped_chunks": skipped_low_value,
        "ghost_b_truncated_count": truncated_call_count,
        "ghost_b_truncated_rate": (
            round(truncated_call_count / attempt_count, 4) if attempt_count else 0.0
        ),
        "ghost_b_recovered_count": json_recovery_count,
        "ghost_b_failed_count": len(failures),
        "repair_model_count": repair_model_count,
        "repair_model_rate": (
            round(repair_model_count / attempt_count, 4) if attempt_count else 0.0
        ),
        "models": models,
        "total_tokens": total_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost_tokens": total_tokens,
        "avg_prompt_tokens_per_chunk": (
            round(prompt_tokens / total_chunks, 2) if total_chunks else 0.0
        ),
        "total_duration_seconds": round(total_duration, 3),
        "entity_count": sum(len(r.entities) for r in results),
        "relation_count": relation_count,
        "candidate_fact_count": sum(len(r.candidate_facts) for r in results),
        "related_to_count": related_to_count,
        "related_to_ratio": round(related_to_count / relation_count, 4) if relation_count else 0.0,
        "predicate_confidence_avg": (
            round(sum(predicate_confidences) / len(predicate_confidences), 4)
            if predicate_confidences
            else 0.0
        ),
        "entity_remap_count": sum(r.entity_remap_count for r in results),
        "relation_remap_count": sum(r.relation_remap_count for r in results),
        "domain_range_remap_count": sum(r.domain_range_remap_count for r in results),
        "domain_range_warn_count": sum(r.domain_range_warn_count for r in results),
        "endpoint_completion_count": sum(r.endpoint_completion_count for r in results),
        "evidence_cue_repair_count": sum(r.evidence_cue_repair_count for r in results),
        "direction_repair_count": sum(r.direction_repair_count for r in results),
        "review_relation_count": sum(
            1
            for r in results
            for rel in r.relations
            if rel.review_status
            or "review_required" in str(rel.validation_status or "")
        ),
        "entity_drop_count": sum(r.entity_drop_count for r in results),
        "relation_drop_count": sum(r.relation_drop_count for r in results),
        "schema_lens_ids": lens_ids,
        "error_counts": error_counts,
        "retryable_failed_chunks": retryable_failures,
        "retry_budget_exhausted_count": int(error_counts.get("retry_budget_exhausted") or 0),
        "all_lanes_exhausted_count": int(error_counts.get("all_lanes_exhausted") or 0),
        "lane_cooling_down_count": int(error_counts.get("lane_cooling_down") or 0),
        "provider_error_count": int(error_counts.get("provider_error") or 0),
        "rate_limited_count": int(error_counts.get("rate_limited") or 0),
        "timeout_count": int(error_counts.get("timeout") or 0),
        "graph_retry_after": retry_after,
    }
    if metrics_context:
        metrics.update(metrics_context)
    return metrics


def _entity_key(name: str) -> str:
    return " ".join(str(name or "").lower().split())


def _append_validation_status(existing: str | None, status: str) -> str:
    if not existing:
        return status
    parts = [part for part in str(existing).split("+") if part]
    if status not in parts:
        parts.append(status)
    return "+".join(parts)


def _relation_confidence_kwargs(relation: RelationItem) -> dict:
    return {
        "predicate_confidence": relation.predicate_confidence,
        "extraction_confidence": relation.extraction_confidence,
        "alternative_predicates_considered": list(
            relation.alternative_predicates_considered or []
        ),
        "rejection_reasoning": relation.rejection_reasoning,
        "atomic_fact": relation.atomic_fact,
        "candidate_subject": relation.candidate_subject,
        "candidate_predicate": relation.candidate_predicate,
        "candidate_object": relation.candidate_object,
        "review_status": relation.review_status,
        "predicate_family": relation.predicate_family,
        "qualifier": relation.qualifier,
        "source_sentence": relation.source_sentence,
        "extraction_model": relation.extraction_model,
        "repaired": relation.repaired,
        "repair_reasons": list(relation.repair_reasons or []),
    }


def _normalize_string_list(value) -> list[str]:
    """Normalize optional LLM list fields without splitting strings into chars."""
    if value is None:
        raw_values = []
    elif isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = [value]
    return [str(item).strip() for item in raw_values if str(item).strip()]


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_numeric_confidence(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _coerce_confidence(value, default: float = 0.0) -> float:
    """Parse LLM confidence values defensively and clamp them to [0, 1]."""
    try:
        if isinstance(value, str):
            text = value.strip()
            is_percent = text.endswith("%")
            if is_percent:
                text = text[:-1].strip()
            parsed = float(text)
            if is_percent or parsed > 1.0:
                parsed = parsed / 100.0
        else:
            parsed = float(value)
            if parsed > 1.0:
                parsed = parsed / 100.0
    except (TypeError, ValueError):
        parsed = default
    if parsed != parsed:  # NaN
        parsed = default
    return min(max(float(parsed), 0.0), 1.0)


def _canonical_vocab_value(value: str | None, vocab: list[str]) -> str:
    raw = str(value or "").strip()
    if not raw:
        return raw
    by_key = {
        re.sub(r"[^a-z0-9]+", "_", str(item).lower()).strip("_"): str(item)
        for item in vocab
    }
    key = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    return by_key.get(key, raw)


def _normalize_relation_object_kind(value, *, default_object_kind: str = "entity") -> str:
    raw = str(value or "").strip()
    if not raw:
        return default_object_kind
    key = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    if key in {"literal", "value", "scalar", "string", "number", "date", "literal_value"}:
        return "literal"
    if key in {"entity", "node", "named_entity", "canonical_entity"}:
        return "entity"
    # If the model leaked an ontology facet such as Model/API/Dataset into
    # relation.object_kind, the object is still an entity endpoint.
    return "entity"


def _candidate_fact_from_mapping(
    row: dict,
    *,
    default_object_kind: str = "entity",
) -> CandidateFactItem | None:
    """Parse the explicit candidate-fact layer, accepting legacy relation keys."""
    if not isinstance(row, dict):
        return None
    subject = str(row.get("candidate_subject") or row.get("subject") or "").strip()
    predicate = str(row.get("candidate_predicate") or row.get("predicate") or "").strip()
    obj = str(row.get("candidate_object") or row.get("object") or "").strip()
    if not subject or not predicate or not obj:
        return None
    confidence = _coerce_confidence(
        row.get("confidence", row.get("extraction_confidence")),
        0.0,
    )
    predicate_confidence = _coerce_confidence(row.get("predicate_confidence"), confidence)
    extraction_confidence = _coerce_confidence(row.get("extraction_confidence"), confidence)
    atomic_fact = str(row.get("atomic_fact") or "").strip()
    if not atomic_fact:
        atomic_fact = f"{subject} {predicate} {obj}"
    return CandidateFactItem(
        atomic_fact=atomic_fact[:500],
        candidate_subject=subject,
        candidate_predicate=predicate,
        candidate_object=obj,
        predicate_confidence=predicate_confidence,
        extraction_confidence=extraction_confidence,
        alternative_predicates_considered=_normalize_string_list(
            row.get("alternative_predicates_considered")
        )[:5],
        rejection_reasoning=str(row.get("rejection_reasoning") or "")[:300],
        evidence_phrase=str(row.get("evidence_phrase") or row.get("evidence") or "")[:500],
        object_kind=_normalize_relation_object_kind(
            row.get("object_kind"), default_object_kind=default_object_kind
        ),
        relation_cue=str(row.get("relation_cue") or "")[:120],
    )


def _relation_key(
    subject: str,
    predicate: str,
    obj: str,
    *,
    source_predicate: str | None = None,
) -> tuple[str, str, str]:
    return (
        _entity_key(subject),
        str(source_predicate or predicate or "").strip().lower(),
        _entity_key(obj),
    )


def _relation_from_candidate_fact(
    candidate: CandidateFactItem,
    *,
    threshold: float,
    row_confidence,
) -> RelationItem | None:
    confidence = _coerce_confidence(row_confidence, candidate.extraction_confidence)
    extraction_confidence = candidate.extraction_confidence
    if confidence < threshold or extraction_confidence < threshold:
        return None

    predicate = candidate.candidate_predicate
    source_predicate = None
    validation_status = None
    review_status = None
    if (
        candidate.predicate_confidence < PREDICATE_CONFIDENCE_DEMOTE_THRESHOLD
        and predicate != SchemaContext.RELATION_SENTINEL
    ):
        source_predicate = predicate
        predicate = SchemaContext.RELATION_SENTINEL
        validation_status = "low_predicate_confidence"
        if extraction_confidence >= HIGH_VALUE_EXTRACTION_CONFIDENCE_THRESHOLD:
            validation_status = _append_validation_status(
                validation_status, "review_required"
            )
            review_status = "needs_backfill"

    return RelationItem(
        subject=candidate.candidate_subject,
        predicate=predicate,
        object=candidate.candidate_object,
        object_kind=candidate.object_kind,
        confidence=confidence,
        evidence_phrase=candidate.evidence_phrase,
        relation_cue=candidate.relation_cue,
        source_predicate=source_predicate,
        validation_status=validation_status,
        predicate_confidence=candidate.predicate_confidence,
        extraction_confidence=extraction_confidence,
        alternative_predicates_considered=list(
            candidate.alternative_predicates_considered or []
        ),
        rejection_reasoning=candidate.rejection_reasoning,
        atomic_fact=candidate.atomic_fact,
        candidate_subject=candidate.candidate_subject,
        candidate_predicate=candidate.candidate_predicate,
        candidate_object=candidate.candidate_object,
        review_status=review_status,
    )


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
    if any(term in text for term in (
        "report", "book", "document", "whitepaper", "guide", "equation",
        "figure", "table", "section", "appendix",
    )):
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
        "test", "estimation", "modeling", "scoring",
    )):
        return "Method"
    if any(term in text for term in (
        "card", "button", "screen", "drawer", "interface", "ui", "view",
        "controller", "prompt", "json", "snapshot", "profile", "artifact",
        "file", "chunk", "module", "stack", "dataset", "data", "gguf",
    )):
        return "Artifact"
    if any(term in text for term in (
        "distribution", "parameter", "trait", "ability", "attitude", "identity",
        "motivation", "fear", "agency", "authority", "status", "power",
        "masculinity", "vulnerability", "trust", "shame", "theme", "symbol",
        "archetype", "habit", "belief",
    )):
        return "Concept"

    if role == "subject" and predicate in {
        "uses", "calls", "stores", "extracts", "detects", "classifies", "supports",
        "measures", "tests", "applied_to",
    }:
        return "Method"
    if role == "object" and predicate in {"uses", "calls", "runs_on", "stores"}:
        return "Product"
    if role == "object" and predicate in {"defined_in", "illustrated_in"}:
        return "Document"
    if predicate in {
        "part_of", "references", "depends_on", "represents", "maps_to",
        "follows_distribution", "parameter_of", "equivalent_to", "embodies",
        "symbolizes", "influences", "motivates", "struggles_with",
        "reinforces", "undermines", "frames_as", "conceals", "leverages",
    }:
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
                description="endpoint completed from relation",
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
        validation_status=_append_validation_status(
            relation.validation_status, validation_status
        ),
        **_relation_confidence_kwargs(relation),
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
        validation_status=(
            _append_validation_status(relation.validation_status, validation_status)
            if validation_status
            else relation.validation_status
        ),
        **_relation_confidence_kwargs(relation),
    )


def _relation_with_object_kind(
    relation: RelationItem,
    object_kind: str,
) -> RelationItem:
    if relation.object_kind == object_kind:
        return relation
    return RelationItem(
        subject=relation.subject,
        predicate=relation.predicate,
        object=relation.object,
        object_kind=object_kind,
        confidence=relation.confidence,
        evidence_phrase=relation.evidence_phrase,
        relation_cue=relation.relation_cue,
        source_predicate=relation.source_predicate,
        validation_status=relation.validation_status,
        **_relation_confidence_kwargs(relation),
    )


_VAGUE_RELATION_EVIDENCE_RE = re.compile(
    r"\b("
    r"co-?occurs?|co-?occurrence|see\s+also|related\s+to|associated\s+with|"
    r"similar(?:ity)?|comparable|comparison|compared\s+to|like|resembles?|"
    r"analog(?:y|ous)|parallel(?:s)?"
    r")\b"
)


def _relation_has_low_predicate_confidence(relation: RelationItem) -> bool:
    if relation.predicate_confidence is not None:
        try:
            if float(relation.predicate_confidence) < PREDICATE_CONFIDENCE_DEMOTE_THRESHOLD:
                return True
        except (TypeError, ValueError):
            pass
    return "low_predicate_confidence" in str(relation.validation_status or "")


def _evidence_is_vague_association(evidence: str) -> bool:
    return bool(_VAGUE_RELATION_EVIDENCE_RE.search(evidence.lower()))


def _repair_relation_from_evidence(
    relation: RelationItem,
    counters: dict[str, int],
) -> RelationItem:
    if relation.object_kind != "entity":
        return relation
    if _relation_has_low_predicate_confidence(relation):
        return relation
    evidence = f"{relation.evidence_phrase} {relation.relation_cue}".lower()
    if not evidence.strip():
        return relation
    if _evidence_is_vague_association(evidence):
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
        if reverse:
            counters["direction_repair_count"] += 1
        return _relation_with_predicate(
            relation,
            predicate,
            reverse=reverse,
            validation_status="evidence_cue_repair",
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
        **_relation_confidence_kwargs(relation),
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
    if _relation_has_low_predicate_confidence(relation):
        return False
    if _evidence_is_vague_association(relation.evidence_phrase):
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
    *,
    complete_missing_endpoints: bool = True,
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
        "direction_repair_count": 0,
    }

    relations = [
        _relation_with_object_kind(
            relation,
            _normalize_relation_object_kind(
                relation.object_kind, default_object_kind="literal"
            ),
        )
        for relation in relations
    ]

    # No schema or strict='off' → pass-through
    if schema is None or schema.strict == "off":
        if complete_missing_endpoints:
            entities = _complete_relation_endpoint_entities(
                entities, relations, counters
            )
        return entities, relations, counters
    if not (schema.has_entity_schema or schema.has_relation_schema):
        if complete_missing_endpoints:
            entities = _complete_relation_endpoint_entities(
                entities, relations, counters
            )
        return entities, relations, counters

    # Entities
    out_entities: list[EntityItem] = []
    if schema.has_entity_schema:
        allowed = set(schema.entity_vocab)
        for e in entities:
            entity_type = _canonical_vocab_value(e.entity_type, schema.entity_vocab)
            if entity_type in allowed:
                out_entities.append(
                    EntityItem(
                        canonical_name=e.canonical_name,
                        surface_form=e.surface_form,
                        entity_type=entity_type,
                        confidence=e.confidence,
                        aliases=list(e.aliases or []),
                        description=e.description,
                    )
                )
            elif schema.strict == "soft":
                out_entities.append(
                    EntityItem(
                        canonical_name=e.canonical_name,
                        surface_form=e.surface_form,
                        entity_type=SchemaContext.ENTITY_SENTINEL,
                        confidence=e.confidence,
                        aliases=list(e.aliases or []),
                        description=e.description,
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
            normalized_predicate = _canonical_vocab_value(
                normalized_predicate, schema.relation_vocab
            )
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
            if reverse:
                counters["direction_repair_count"] += 1
            if candidate.predicate in allowed:
                out_relations.append(candidate)
            elif schema.strict == "soft":
                out_relations.append(
                    _relation_with_remap(
                        candidate,
                        validation_status="schema_predicate_remap",
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
    if complete_missing_endpoints:
        out_entities = _complete_relation_endpoint_entities(
            out_entities, out_relations, counters
        )
    out_relations = _apply_domain_range(out_entities, out_relations, counters)
    return out_entities, out_relations, counters


def compile_extraction_candidates(
    entities: list[EntityItem],
    relations: list[RelationItem],
    schema: SchemaContext | None,
    *,
    complete_missing_endpoints: bool = True,
) -> tuple[list[EntityItem], list[RelationItem], dict[str, int]]:
    """Deterministic ontology compiler for Ghost B's proposed graph facts.

    The LLM proposes candidate facts and backward-compatible relation rows;
    this compiler owns final predicate aliases, direction repairs, domain/range
    compatibility, confidence demotion, and validation flags before Neo4j sees
    the output.
    """
    return _apply_schema(
        entities,
        relations,
        schema,
        complete_missing_endpoints=complete_missing_endpoints,
    )


def _looks_like_target_schema(data: dict) -> bool:
    if str(data.get("schema_version") or "").strip() == TARGET_SCHEMA_VERSION:
        return True
    entities = data.get("entities") or []
    relations = data.get("relations") or []
    if any(isinstance(e, dict) and "name" in e and "type" in e for e in entities):
        return True
    return any(
        isinstance(r, dict)
        and ("predicate_family" in r or "source_sentence" in r or "qualifier" in r)
        for r in relations
    )


def _target_relation_family_for_predicate(predicate: str | None) -> str:
    normalized = str(predicate or "").strip()
    return _TARGET_RELATION_FAMILY_MAP.get(normalized, "WeakAssociation")


def _target_entity_from_mapping(row: dict) -> EntityItem | None:
    if not isinstance(row, dict):
        return None
    name = str(row.get("name") or row.get("canonical_name") or "").strip()
    if not name:
        return None
    entity_type = str(row.get("type") or row.get("entity_type") or "other").strip() or "other"
    aliases = _normalize_string_list(row.get("aliases"))[:12]
    description = str(row.get("description") or "").strip()[:500]
    confidence = _coerce_confidence(row.get("confidence"), 1.0)
    return EntityItem(
        canonical_name=_entity_key(name),
        surface_form=name,
        entity_type=entity_type,
        confidence=confidence,
        aliases=aliases,
        description=description,
    )


def _target_object_from_mapping(row: dict) -> ObjectItem | None:
    if not isinstance(row, dict):
        return None
    name = str(row.get("name") or "").strip()
    object_type = str(row.get("type") or "").strip()
    if not name or not object_type:
        return None
    attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    return ObjectItem(name=name, type=object_type, attributes=dict(attributes))


def _target_relation_repair_reasons_from_values(
    *,
    subject: str,
    obj: str,
    confidence_value,
    predicate_family: str | None,
    entity_names: set[str],
) -> list[str]:
    reasons: list[str] = []
    if not _is_numeric_confidence(confidence_value):
        reasons.append("confidence_not_numeric")
    else:
        confidence = _coerce_confidence(confidence_value, 0.0)
        if confidence < TRIPLE_REPAIR_CONFIDENCE_THRESHOLD:
            reasons.append("confidence_below_0.70")
    family = str(predicate_family or "").strip()
    if not family:
        reasons.append("predicate_family_missing")
    elif family == "WeakAssociation":
        reasons.append("predicate_family_WeakAssociation")
    if _entity_key(subject) not in entity_names:
        reasons.append("subject_missing_from_entities")
    if _entity_key(obj) not in entity_names:
        reasons.append("object_missing_from_entities")
    return reasons


def _target_relation_repair_reasons(
    relation: RelationItem,
    entity_names: set[str],
) -> list[str]:
    return _target_relation_repair_reasons_from_values(
        subject=relation.subject,
        obj=relation.object,
        confidence_value=relation.confidence,
        predicate_family=relation.predicate_family,
        entity_names=entity_names,
    )


def _target_relation_from_mapping(
    row: dict,
    *,
    entity_names: set[str],
    extraction_model: str,
    repaired: bool,
) -> tuple[RelationItem | None, list[str]]:
    if not isinstance(row, dict):
        return None, ["relation_not_object"]
    subject_raw = str(row.get("subject") or "").strip()
    predicate = str(row.get("predicate") or "").strip()
    object_raw = str(row.get("object") or "").strip()
    if not subject_raw or not predicate or not object_raw:
        return None, ["relation_missing_required_field"]

    confidence_value = row.get("confidence")
    confidence = (
        _coerce_confidence(confidence_value, 0.0)
        if _is_numeric_confidence(confidence_value)
        else 0.0
    )
    predicate_family = str(row.get("predicate_family") or "").strip() or None
    qualifier = str(row.get("qualifier") or "").strip()[:300]
    source_sentence = str(
        row.get("source_sentence") or row.get("evidence_phrase") or ""
    ).strip()[:1000]
    reasons = _target_relation_repair_reasons_from_values(
        subject=subject_raw,
        obj=object_raw,
        confidence_value=confidence_value,
        predicate_family=predicate_family,
        entity_names=entity_names,
    )
    validation_status = None
    for reason in reasons:
        validation_status = _append_validation_status(validation_status, reason)
    if not source_sentence:
        validation_status = _append_validation_status(
            validation_status, "missing_source_sentence"
        )
    return (
        RelationItem(
            subject=_entity_key(subject_raw),
            predicate=predicate,
            object=_entity_key(object_raw),
            object_kind="entity",
            confidence=confidence,
            evidence_phrase=source_sentence,
            source_sentence=source_sentence,
            predicate_family=predicate_family,
            qualifier=qualifier,
            validation_status=validation_status,
            extraction_model=extraction_model,
            repaired=repaired,
            repair_reasons=list(reasons),
            predicate_confidence=confidence,
            extraction_confidence=confidence,
            atomic_fact=source_sentence,
            candidate_subject=_entity_key(subject_raw),
            candidate_predicate=predicate,
            candidate_object=_entity_key(object_raw),
        ),
        reasons,
    )


def _parse_target_data(
    data: dict,
    task: ExtractionTask,
    threshold: float,
    schema: SchemaContext | None,
    schema_lens: SchemaLens | dict | None,
    *,
    extraction_model: str,
    repaired: bool,
) -> tuple[ExtractionResult, list[RelationRepairItem]]:
    entities = [
        item
        for item in (
            _target_entity_from_mapping(row) for row in data.get("entities", [])
        )
        if item is not None
    ]
    objects = [
        item
        for item in (
            _target_object_from_mapping(row) for row in data.get("objects", [])
        )
        if item is not None
    ]
    entity_names = {_entity_key(entity.canonical_name) for entity in entities}
    relations: list[RelationItem] = []
    repair_items: list[RelationRepairItem] = []
    relation_keys: set[tuple[str, str, str]] = set()
    for row in data.get("relations", []):
        relation, reasons = _target_relation_from_mapping(
            row,
            entity_names=entity_names,
            extraction_model=extraction_model,
            repaired=repaired,
        )
        if relation is None:
            continue
        relation_key = _relation_key(relation.subject, relation.predicate, relation.object)
        if relation_key in relation_keys:
            continue
        relation_keys.add(relation_key)
        relations.append(relation)
        if reasons:
            repair_items.append(RelationRepairItem(relation=relation, reasons=reasons))

    entities, relations, counters = compile_extraction_candidates(
        entities,
        relations,
        schema,
        complete_missing_endpoints=False,
    )
    lens = (
        schema_lens
        if isinstance(schema_lens, SchemaLens)
        else SchemaLens.from_dict(schema_lens if isinstance(schema_lens, dict) else None)
    )
    return (
        ExtractionResult(
            schema_version=data.get("schema_version", TARGET_SCHEMA_VERSION),
            chunk_id=task.chunk_id,
            doc_id=task.doc_id,
            corpus_id=task.corpus_id,
            entities=entities,
            candidate_facts=[],
            relations=relations,
            objects=objects,
            entity_remap_count=counters["entity_remap_count"],
            entity_drop_count=counters["entity_drop_count"],
            relation_remap_count=counters["relation_remap_count"],
            relation_drop_count=counters["relation_drop_count"],
            domain_range_remap_count=counters["domain_range_remap_count"],
            domain_range_warn_count=counters["domain_range_warn_count"],
            endpoint_completion_count=counters["endpoint_completion_count"],
            evidence_cue_repair_count=counters["evidence_cue_repair_count"],
            direction_repair_count=counters["direction_repair_count"],
            schema_lens_id=lens.lens_id if lens else None,
        ),
        repair_items,
    )


def _parse(
    raw: str,
    task: ExtractionTask,
    threshold: float,
    schema: SchemaContext | None = None,
    schema_lens: SchemaLens | dict | None = None,
    *,
    extraction_model: str = PRIMARY_EXTRACTION_MODEL,
    repaired: bool = False,
) -> ExtractionResult | None:
    result, _repair_items = _parse_with_repair_items(
        raw,
        task,
        threshold,
        schema=schema,
        schema_lens=schema_lens,
        extraction_model=extraction_model,
        repaired=repaired,
    )
    return result


def _parse_with_repair_items(
    raw: str,
    task: ExtractionTask,
    threshold: float,
    schema: SchemaContext | None = None,
    schema_lens: SchemaLens | dict | None = None,
    *,
    extraction_model: str = PRIMARY_EXTRACTION_MODEL,
    repaired: bool = False,
) -> tuple[ExtractionResult | None, list[RelationRepairItem]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        repaired_raw = _close_truncated_json_prefix(raw)
        if repaired_raw:
            try:
                data = json.loads(repaired_raw)
                logger.warning(
                    "GHOST B JSON parse recovered truncated prefix chunk_id=%s: %s",
                    task.chunk_id,
                    exc,
                )
            except json.JSONDecodeError:
                logger.error("GHOST B JSON parse failed chunk_id=%s: %s", task.chunk_id, exc)
                return None, []
        else:
            logger.error("GHOST B JSON parse failed chunk_id=%s: %s", task.chunk_id, exc)
            return None, []

    if not isinstance(data, dict):
        logger.error("GHOST B JSON root is not object chunk_id=%s", task.chunk_id)
        return None, []

    if _looks_like_target_schema(data):
        return _parse_target_data(
            data,
            task,
            threshold,
            schema,
            schema_lens,
            extraction_model=extraction_model,
            repaired=repaired,
        )

    entities: list[EntityItem] = []
    for e in data.get("entities", []):
        if not isinstance(e, dict):
            continue
        confidence = _coerce_confidence(e.get("confidence"), 0.0)
        if confidence < threshold:
            continue
        try:
            entities.append(
                EntityItem(
                    canonical_name=e["canonical_name"],
                    surface_form=e.get("surface_form", e["canonical_name"]),
                    entity_type=e.get("entity_type", "other"),
                    confidence=confidence,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    candidate_facts: list[CandidateFactItem] = []
    seen_candidate_keys: set[tuple[str, str, str]] = set()
    for row in data.get("candidate_facts", []):
        candidate = _candidate_fact_from_mapping(row)
        if candidate is None:
            continue
        key = _relation_key(
            candidate.candidate_subject,
            candidate.candidate_predicate,
            candidate.candidate_object,
        )
        if key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(key)
        candidate_facts.append(candidate)

    relations: list[RelationItem] = []
    relation_keys: set[tuple[str, str, str]] = set()
    relation_rows = data.get("relations", [])
    for r in relation_rows:
        candidate = _candidate_fact_from_mapping(r, default_object_kind="literal")
        if candidate is None:
            continue
        key = _relation_key(
            candidate.candidate_subject,
            candidate.candidate_predicate,
            candidate.candidate_object,
        )
        if key not in seen_candidate_keys:
            seen_candidate_keys.add(key)
            candidate_facts.append(candidate)
        relation = _relation_from_candidate_fact(
            candidate,
            threshold=threshold,
            row_confidence=r.get("confidence"),
        )
        if relation is None:
            continue
        relation_key = _relation_key(
            relation.subject,
            relation.predicate,
            relation.object,
            source_predicate=relation.source_predicate,
        )
        if relation_key in relation_keys:
            continue
        relation_keys.add(relation_key)
        relations.append(relation)

    for candidate in candidate_facts:
        relation = _relation_from_candidate_fact(
            candidate,
            threshold=threshold,
            row_confidence=candidate.extraction_confidence,
        )
        if relation is None:
            continue
        relation_key = _relation_key(
            relation.subject,
            relation.predicate,
            relation.object,
            source_predicate=relation.source_predicate,
        )
        if relation_key in relation_keys:
            continue
        relation_keys.add(relation_key)
        relations.append(relation)

    entities, relations, counters = compile_extraction_candidates(
        entities, relations, schema
    )
    for relation in relations:
        relation.extraction_model = extraction_model
        relation.repaired = repaired
        if not relation.source_sentence and relation.evidence_phrase:
            relation.source_sentence = relation.evidence_phrase
        if not relation.predicate_family:
            relation.predicate_family = _target_relation_family_for_predicate(
                relation.predicate
            )
    lens = (
        schema_lens
        if isinstance(schema_lens, SchemaLens)
        else SchemaLens.from_dict(schema_lens if isinstance(schema_lens, dict) else None)
    )

    return (
        ExtractionResult(
            schema_version=data.get("schema_version", "polymath.extract.v1"),
            chunk_id=task.chunk_id,
            doc_id=task.doc_id,
            corpus_id=task.corpus_id,
            entities=entities,
            candidate_facts=candidate_facts,
            relations=relations,
            objects=[],
            entity_remap_count=counters["entity_remap_count"],
            entity_drop_count=counters["entity_drop_count"],
            relation_remap_count=counters["relation_remap_count"],
            relation_drop_count=counters["relation_drop_count"],
            domain_range_remap_count=counters["domain_range_remap_count"],
            domain_range_warn_count=counters["domain_range_warn_count"],
            endpoint_completion_count=counters["endpoint_completion_count"],
            evidence_cue_repair_count=counters["evidence_cue_repair_count"],
            direction_repair_count=counters["direction_repair_count"],
            schema_lens_id=lens.lens_id if lens else None,
        ),
        [],
    )


def _close_truncated_json_prefix(raw: str) -> str | None:
    """Close a valid JSON prefix that was cut after whitespace.

    vLLM structured decoding can legally emit whitespace forever before a
    closing delimiter. If max_tokens cuts the response there, json.loads sees an
    invalid document even though the emitted prefix is usable. This repair only
    appends missing closing delimiters when the prefix is outside a string.
    """

    text = raw.rstrip()
    if not text:
        return None
    if len(text) < 64 or TARGET_SCHEMA_VERSION not in text:
        return None
    stack: list[str] = []
    in_string = False
    escape = False
    for char in text:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            stack.append("}")
        elif char == "[":
            stack.append("]")
        elif char in "}]":
            if not stack or stack[-1] != char:
                return None
            stack.pop()
    if in_string or not stack:
        return None
    while text.endswith(","):
        text = text[:-1].rstrip()
    return text + "".join(reversed(stack))


def _relation_repair_key(relation: RelationItem) -> tuple[str, str, str]:
    return _relation_key(relation.subject, relation.predicate, relation.object)


def _target_schema_repair_contract(
    schema: SchemaContext | None,
    *,
    enable_thinking: bool = False,
) -> str:
    lines = [
        (
            "<|think|>\n"
            if enable_thinking
            else ""
        )
        + (
            "You are a relation repair specialist. Given entity names, a "
            "candidate relation, and the source sentence, determine whether "
            "the relation is correct. Return a JSON object with the repaired "
            "relation or a rejection."
        ),
        _TARGET_EXTRACTION_SCHEMA,
        "Rules:",
        "- Use only the supplied source_sentence; do not infer beyond it.",
        "- If the sentence does not explicitly support a valid triple, return {\"relation\": null}.",
        "- relation.subject and relation.object must match supplied entity names after case/space normalization.",
        "- confidence must be a numeric float >= 0.70.",
        "- predicate_family is required and must not be WeakAssociation for a repaired triple.",
    ]
    if schema and schema.has_entity_schema:
        lines.append("- approved entity types: " + ", ".join(schema.entity_vocab))
    if schema and schema.has_relation_schema:
        lines.append("- approved predicates: " + ", ".join(schema.relation_vocab))
    return "\n".join(lines)


def _is_gemma_repair_entry(repair_entry: dict) -> bool:
    model = str(repair_entry.get("model") or "").lower()
    base_url = str(repair_entry.get("base_url") or "").lower()
    return any(hint in model or hint in base_url for hint in _GEMMA_REPAIR_MODEL_HINTS)


def _repair_extra_params(repair_entry: dict) -> dict:
    extra = dict(repair_entry.get("extra_params") or {})
    if not _is_gemma_repair_entry(repair_entry):
        return extra
    try:
        temperature = float(extra.get("temperature", 0))
    except (TypeError, ValueError):
        temperature = 0
    if temperature <= 0:
        extra["temperature"] = _GEMMA_REPAIR_TEMPERATURE
    extra.setdefault("top_p", _GEMMA_REPAIR_TOP_P)
    extra.setdefault("top_k", _GEMMA_REPAIR_TOP_K)
    return extra


def _repair_json_payload(raw: str) -> str:
    text = str(raw or "").strip()
    if "<channel|>" in text:
        text = text.rsplit("<channel|>", 1)[-1].strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    if text.startswith("{"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _relation_repair_payload(relation: RelationItem, reasons: list[str]) -> dict:
    return {
        "subject": relation.subject,
        "predicate": relation.predicate,
        "predicate_family": relation.predicate_family,
        "object": relation.object,
        "qualifier": relation.qualifier,
        "confidence": relation.confidence,
        "source_sentence": relation.source_sentence or relation.evidence_phrase,
        "repair_reasons": reasons,
    }


def _schema_snapshot(schema: SchemaContext | None) -> dict:
    if schema is None:
        return {}
    return {
        "entity_schema": list(schema.entity_schema or []),
        "relation_schema": list(schema.relation_schema or []),
        "strict": schema.strict,
    }


def _relation_repair_candidate(
    *,
    result: ExtractionResult,
    item: RelationRepairItem,
    schema: SchemaContext | None,
) -> RelationRepairCandidate:
    relation = item.relation
    source_sentence = relation.source_sentence or relation.evidence_phrase
    return RelationRepairCandidate(
        chunk_id=result.chunk_id,
        doc_id=result.doc_id,
        corpus_id=result.corpus_id,
        schema_version=result.schema_version,
        relation=relation,
        reasons=list(item.reasons),
        source_sentence=source_sentence,
        entity_names=sorted({_entity_key(entity.canonical_name) for entity in result.entities}),
        entity_snapshot=list(result.entities),
        schema_snapshot=_schema_snapshot(schema),
        schema_lens_id=result.schema_lens_id,
    )


def _drop_deferred_repair_relations(
    result: ExtractionResult,
    repair_items: list[RelationRepairItem],
) -> ExtractionResult:
    repair_keys = {_relation_repair_key(item.relation) for item in repair_items}
    if not repair_keys:
        return result
    result.relations = [
        relation
        for relation in result.relations
        if _relation_repair_key(relation) not in repair_keys
    ]
    return result


def _extract_repaired_relation_row(data: dict) -> dict | None:
    if not isinstance(data, dict):
        return None
    if "relation" in data:
        relation = data.get("relation")
        return relation if isinstance(relation, dict) else None
    relations = data.get("relations")
    if isinstance(relations, list) and relations and isinstance(relations[0], dict):
        return relations[0]
    if {"subject", "predicate", "object"}.issubset(data.keys()):
        return data
    return None


def _parse_repaired_relation(
    raw: str,
    *,
    original: RelationItem,
    entity_names: set[str],
    schema: SchemaContext | None,
) -> RelationItem | None:
    try:
        data = json.loads(_repair_json_payload(raw))
    except json.JSONDecodeError:
        return None
    row = _extract_repaired_relation_row(data)
    if row is None:
        return None
    if not str(row.get("source_sentence") or "").strip():
        row = {**row, "source_sentence": original.source_sentence or original.evidence_phrase}
    relation, reasons = _target_relation_from_mapping(
        row,
        entity_names=entity_names,
        extraction_model=REPAIR_EXTRACTION_MODEL,
        repaired=True,
    )
    if relation is None or reasons:
        return None
    if schema and schema.has_relation_schema:
        predicate, reverse = normalize_relation_predicate_alias(relation.predicate)
        predicate = _canonical_vocab_value(predicate, schema.relation_vocab)
        if predicate not in set(schema.relation_vocab):
            return None
        if reverse:
            relation = _relation_with_predicate(
                relation,
                predicate,
                reverse=True,
                validation_status="schema_predicate_alias",
            )
        else:
            relation.predicate = predicate
    repaired_relation = relation
    repaired_relation.repaired = True
    repaired_relation.extraction_model = REPAIR_EXTRACTION_MODEL
    repaired_relation.repair_reasons = list(original.repair_reasons or [])
    repaired_relation.source_sentence = (
        repaired_relation.source_sentence
        or repaired_relation.evidence_phrase
        or original.source_sentence
        or original.evidence_phrase
    )
    repaired_relation.evidence_phrase = repaired_relation.source_sentence
    if not repaired_relation.predicate_family:
        repaired_relation.predicate_family = _target_relation_family_for_predicate(
            repaired_relation.predicate
        )
    if _target_relation_repair_reasons(repaired_relation, entity_names):
        return None
    return repaired_relation


async def _repair_target_relation_with_gemma(
    *,
    repair_entry: dict,
    task: ExtractionTask,
    relation: RelationItem,
    reasons: list[str],
    entity_names: set[str],
    schema: SchemaContext | None,
    settings,
    headers: dict[str, str],
) -> RelationItem | None:
    source_sentence = relation.source_sentence or relation.evidence_phrase
    extra_params = _repair_extra_params(repair_entry)
    system_content = _target_schema_repair_contract(
        schema,
        enable_thinking=_is_gemma_repair_entry(repair_entry),
    )
    user_content = json.dumps(
        {
            "source_sentence": source_sentence,
            "entity_names": sorted(entity_names),
            "failed_triple": _relation_repair_payload(relation, reasons),
        },
        ensure_ascii=True,
    )
    payload: dict = {
        "model": repair_entry["model"],
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ],
        "temperature": extra_params.get("temperature", 0),
        "response_format": {"type": "json_object"},
        "max_tokens": min(
            int(extra_params.get("max_tokens") or 1024),
            2048,
        ),
    }
    if repair_entry.get("base_url"):
        payload["api_base"] = repair_entry["base_url"]
    if repair_entry.get("api_key"):
        payload["api_key"] = repair_entry["api_key"]
    for key, value in extra_params.items():
        if key not in {"model", "messages", "response_format", "max_tokens"}:
            payload[key] = value

    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(
            f"{settings.LITELLM_URL}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
    return _parse_repaired_relation(
        raw,
        original=relation,
        entity_names=entity_names,
        schema=schema,
    )


async def _repair_target_relations(
    *,
    result: ExtractionResult,
    repair_items: list[RelationRepairItem],
    task: ExtractionTask,
    repair_pool: list[dict],
    schema: SchemaContext | None,
    settings,
    headers: dict[str, str],
    call_metrics: list[dict],
) -> ExtractionResult:
    if not repair_items:
        return result
    if not repair_pool:
        return result

    repair_by_key = {
        _relation_repair_key(item.relation): item for item in repair_items
    }
    repaired_by_key: dict[tuple[str, str, str], RelationItem] = {}
    entity_names = {_entity_key(entity.canonical_name) for entity in result.entities}
    for idx, item in enumerate(repair_items):
        repair_entry = repair_pool[idx % len(repair_pool)]
        started = time.perf_counter()
        success = False
        error_type = None
        try:
            repaired_relation = await _repair_target_relation_with_gemma(
                repair_entry=repair_entry,
                task=task,
                relation=item.relation,
                reasons=item.reasons,
                entity_names=entity_names,
                schema=schema,
                settings=settings,
                headers=headers,
            )
            if repaired_relation is not None:
                repaired_by_key[_relation_repair_key(item.relation)] = repaired_relation
                success = True
        except Exception as exc:
            error_type = _provider_error_kind(exc)
            logger.warning(
                "GHOST B triple repair failed chunk_id=%s triple=%s reasons=%s error=%s",
                task.chunk_id,
                _relation_repair_key(item.relation),
                item.reasons,
                exc,
            )
        finally:
            call_metrics.append(
                {
                    "chunk_id": task.chunk_id,
                    "model": repair_entry["model"],
                    "lane": -1,
                    "attempt": 0,
                    "duration_seconds": round(time.perf_counter() - started, 3),
                    "total_tokens": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "success": success,
                    "error_type": error_type,
                    "recovery_mode": False,
                    "repair_model": True,
                    "triple_repair": True,
                    "repair_reasons": item.reasons,
                }
            )

    repaired_relations: list[RelationItem] = []
    for relation in result.relations:
        key = _relation_repair_key(relation)
        if key in repaired_by_key:
            repaired_relations.append(repaired_by_key[key])
        elif key not in repair_by_key:
            repaired_relations.append(relation)
        else:
            logger.warning(
                "GHOST B dropped unrepaired target triple chunk_id=%s triple=%s reasons=%s",
                task.chunk_id,
                key,
                repair_by_key[key].reasons,
            )
    result.relations = repaired_relations
    return result


async def extract_entities(
    tasks: list[ExtractionTask],
    model: str | None = None,
    schema: SchemaContext | None = None,
    schema_lens: SchemaLens | dict | None = None,
    chunk_vectors: dict[str, list[float]] | None = None,
    schema_resolver: SchemaResolver | None = None,
    *,
    pool: list[dict] | None = None,
    repair_pool: list[dict] | None = None,
    return_report: bool = False,
    extraction_mode: Literal["full", "compact"] = "full",
    max_entities_per_chunk: int | None = None,
    max_relations_per_chunk: int | None = None,
    max_completion_tokens_override: int | None = None,
    per_chunk_max_attempts: int = 2,
    per_doc_max_failed_chunks_before_pause: int = 50,
    per_lane_max_consecutive_failures: int = SOFT_FATAL_DISABLE_STRIKES,
    per_lane_cooldown_seconds: int = 300,
    defer_triple_repair: bool = False,
    metrics_context: dict | None = None,
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
    max_completion_tokens = (
        int(max_completion_tokens_override)
        if max_completion_tokens_override is not None
        else settings.EXTRACTION_MAX_TOKENS
    )
    max_entities = (
        int(max_entities_per_chunk)
        if max_entities_per_chunk is not None
        else settings.EXTRACTION_MAX_ENTITIES_PER_CHUNK
    )
    max_relations = (
        int(max_relations_per_chunk)
        if max_relations_per_chunk is not None
        else settings.EXTRACTION_MAX_RELATIONS_PER_CHUNK
    )
    max_completion_tokens = max(256, min(max_completion_tokens, settings.EXTRACTION_MAX_TOKENS))
    max_entities = max(1, min(max_entities, settings.EXTRACTION_MAX_ENTITIES_PER_CHUNK))
    max_relations = max(0, min(max_relations, settings.EXTRACTION_MAX_RELATIONS_PER_CHUNK))
    per_chunk_max_attempts = max(1, min(int(per_chunk_max_attempts or 2), 5))
    per_doc_max_failed_chunks_before_pause = max(
        1, int(per_doc_max_failed_chunks_before_pause or 50)
    )
    per_lane_max_consecutive_failures = max(
        1, int(per_lane_max_consecutive_failures or SOFT_FATAL_DISABLE_STRIKES)
    )
    per_lane_cooldown_seconds = max(0, int(per_lane_cooldown_seconds or 0))
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
    repair_pool = repair_pool or []

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
    relation_repair_candidates: list[RelationRepairCandidate] = []
    call_metrics: list[dict] = []
    failed_count = 0
    paused_due_doc_budget = False
    deterministic_doc_error: str | None = None
    _list_lock = asyncio.Lock()
    disabled_lanes: set[int] = set()
    lane_fatal_strikes: dict[int, int] = {}
    lane_states: dict[int, str] = {idx: "healthy" for idx in range(len(pool))}
    chunk_attempts: dict[str, int] = {}
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
        if strikes >= per_lane_max_consecutive_failures:
            return True
        logger.warning(
            "GHOST B saw soft fatal provider signal for lane=%d model=%s "
            "strike=%d/%d; keeping lane active until repeated: %s",
            pool_idx,
            entry["model"],
            strikes,
            per_lane_max_consecutive_failures,
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
            tier = provider_error_tier(exc)
            lane_states[pool_idx] = (
                "exhausted_for_document" if tier == "hard" else "cooling_down"
            )
        entry = pool[pool_idx]
        logger.error(
            "GHOST B disabled extraction lane=%d model=%s after fatal provider error: %s",
            pool_idx,
            entry["model"],
            provider_error_summary(exc),
        )

    async def _reserve_chunk_attempt(chunk_id: str) -> int | None:
        async with _list_lock:
            current = int(chunk_attempts.get(chunk_id) or 0)
            if current >= per_chunk_max_attempts:
                return None
            current += 1
            chunk_attempts[chunk_id] = current
            return current

    async def _open_document_circuit(reason: str) -> None:
        nonlocal paused_due_doc_budget, deterministic_doc_error
        async with _list_lock:
            paused_due_doc_budget = True
            deterministic_doc_error = reason

    async def _process_one(task: ExtractionTask, pool_idx: int) -> ExtractionResult | None:
        if paused_due_doc_budget:
            return None
        entry = pool[pool_idx]
        chunk_vec = chunk_vectors.get(task.chunk_id) if chunk_vectors else None
        eff_entity, eff_relation = await resolve_chunk_vocab(
            schema=schema,
            chunk_vec=chunk_vec,
            resolver=schema_resolver,
            inline_limit=inline_limit,
            top_k=top_k,
        )
        # Attempt budget is explicit and infrastructure-only. A valid JSON
        # response never retries for weak semantics, low predicate confidence,
        # or related_to. Parse errors get compact recovery; provider/transport
        # errors only retry inside the bounded infrastructure budget.
        last_exc: Exception | None = None
        last_error_type = "unknown"
        attempts_used = 0
        parse_recovery_used = False
        while True:
            reserved_attempt = await _reserve_chunk_attempt(task.chunk_id)
            if reserved_attempt is None:
                last_error_type = "retry_budget_exhausted"
                last_exc = RuntimeError("Ghost B per-chunk retry budget exhausted")
                break
            attempts_used = reserved_attempt
            recovery_mode = (
                last_error_type == "parse_error" and not parse_recovery_used
            )
            if recovery_mode:
                parse_recovery_used = True
            attempt_entry = entry
            repair_model_used = False
            if recovery_mode and repair_pool:
                attempt_entry = repair_pool[(reserved_attempt - 1) % len(repair_pool)]
                repair_model_used = True
            attempt_max_entities = min(max_entities, 5) if recovery_mode else max_entities
            attempt_max_relations = min(max_relations, 5) if recovery_mode else max_relations
            attempt_max_tokens = (
                min(max_completion_tokens, 2048) if recovery_mode else max_completion_tokens
            )
            target_schema = build_target_extraction_json_schema(
                chunk_id=task.chunk_id,
                doc_id=task.doc_id,
                corpus_id=task.corpus_id,
                entity_vocab=eff_entity
                or (schema.entity_vocab if schema and schema.has_entity_schema else None),
                relation_vocab=eff_relation
                or (schema.relation_vocab if schema and schema.has_relation_schema else None),
                max_entities=attempt_max_entities,
                max_relations=attempt_max_relations,
            )
            messages = [
                {
                    "role": "system",
                    "content": _compact_system_prompt(recovery_mode=recovery_mode),
                },
                {
                    "role": "user",
                    "content": _compact_user_prompt(
                        task=task,
                        max_entities=attempt_max_entities,
                        max_relations=attempt_max_relations,
                        compact_mode=extraction_mode == "compact",
                        schema_lens=schema_lens,
                    ),
                },
            ]
            safe_max_tokens, token_budget = _safe_completion_budget(
                messages=messages,
                model=str(attempt_entry["model"]),
                requested_tokens=attempt_max_tokens,
                context_limit_override=attempt_entry.get("context_length"),
            )
            if safe_max_tokens is None:
                last_error_type = "token_budget"
                last_exc = RuntimeError(
                    "Ghost B token budget infeasible before provider call: "
                    f"{token_budget}"
                )
                call_metrics.append(
                    {
                        "chunk_id": task.chunk_id,
                        "model": attempt_entry["model"],
                        "lane": pool_idx,
                        "attempt": reserved_attempt,
                        "duration_seconds": 0,
                        "total_tokens": 0,
                        "prompt_tokens": token_budget["prompt_tokens_estimate"],
                        "completion_tokens": 0,
                        "success": False,
                        "error_type": last_error_type,
                        "recovery_mode": recovery_mode,
                        "repair_model": repair_model_used,
                        "max_tokens": 0,
                        "token_budget": token_budget,
                    }
                )
                await _open_document_circuit(last_error_type)
                break
            payload: dict = {
                "model": attempt_entry["model"],
                "messages": messages,
                "temperature": 0,
                "response_format": _json_schema_response_format(target_schema),
                "max_tokens": safe_max_tokens,
            }
            if attempt_entry.get("base_url"):
                payload["api_base"] = attempt_entry["base_url"]
            if attempt_entry.get("api_key"):
                payload["api_key"] = attempt_entry["api_key"]
            for _k, _v in (attempt_entry.get("extra_params") or {}).items():
                if _k not in ("model", "messages", "response_format", "max_tokens"):
                    payload[_k] = _v
            started = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=300.0) as client:
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
                        attempt_entry["model"],
                        duration,
                        usage.get("total_tokens"),
                        usage.get("prompt_tokens"),
                        usage.get("completion_tokens"),
                        pool_idx,
                        reserved_attempt,
                    )
                    raw = body["choices"][0]["message"]["content"]
                    parse_model = (
                        REPAIR_EXTRACTION_MODEL
                        if repair_model_used
                        else PRIMARY_EXTRACTION_MODEL
                    )
                    result, repair_items = _parse_with_repair_items(
                        raw,
                        task,
                        threshold,
                        schema=schema,
                        schema_lens=schema_lens,
                        extraction_model=parse_model,
                        repaired=repair_model_used,
                    )
                    if result and repair_items and not repair_model_used:
                        if defer_triple_repair:
                            relation_repair_candidates.extend(
                                _relation_repair_candidate(
                                    result=result,
                                    item=item,
                                    schema=schema,
                                )
                                for item in repair_items
                            )
                            result = _drop_deferred_repair_relations(
                                result,
                                repair_items,
                            )
                        else:
                            result = await _repair_target_relations(
                                result=result,
                                repair_items=repair_items,
                                task=task,
                                repair_pool=repair_pool,
                                schema=schema,
                                settings=settings,
                                headers=headers,
                                call_metrics=call_metrics,
                            )
                    completion_used = int(usage.get("completion_tokens") or 0)
                    # Truncation flag: provider hit (or sat at) the requested
                    # output cap. Use a small margin because some providers
                    # report completion_tokens slightly under the cap when the
                    # final token is a stop sequence rather than a hard cut.
                    truncated_call = bool(
                        safe_max_tokens
                        and completion_used >= max(1, int(safe_max_tokens * 0.98))
                    )
                    call_metrics.append(
                        {
                            "chunk_id": task.chunk_id,
                            "model": attempt_entry["model"],
                            "lane": pool_idx,
                            "attempt": reserved_attempt,
                            "duration_seconds": round(duration, 3),
                            "total_tokens": usage.get("total_tokens"),
                            "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens"),
                            "success": bool(result),
                            "error_type": None if result else "parse_error",
                            "recovery_mode": recovery_mode,
                            "repair_model": repair_model_used,
                            "triple_repair_requested": len(repair_items),
                            "max_tokens": safe_max_tokens,
                            "token_budget": token_budget,
                            "truncated": truncated_call,
                        }
                    )
                    if result:
                        logger.debug(
                            "GHOST B: chunk_id=%s entities=%d relations=%d "
                            "(attempt=%d lane=%d)",
                            task.chunk_id,
                            len(result.entities),
                            len(result.relations),
                            reserved_attempt,
                            pool_idx,
                        )
                        return result
                    last_error_type = "parse_error"
                    last_exc = RuntimeError("parse returned None")
                    if attempts_used < per_chunk_max_attempts and not parse_recovery_used:
                        continue
                    break
            except Exception as exc:
                last_exc = exc
                last_error_type = _provider_error_kind(exc)
                fatal_tier = provider_error_tier(exc)
                fatal_lane = fatal_tier is not None
                call_metrics.append(
                    {
                        "chunk_id": task.chunk_id,
                        "model": attempt_entry["model"],
                        "lane": pool_idx,
                        "attempt": reserved_attempt,
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
                        "recovery_mode": recovery_mode,
                        "repair_model": repair_model_used,
                        "max_tokens": safe_max_tokens,
                        "token_budget": token_budget,
                    }
                )
                if fatal_lane:
                    raise FatalLaneError(exc) from exc
                if _is_deterministic_provider_error(exc):
                    await _open_document_circuit(last_error_type)
                if (
                    _retryable_infrastructure_error(exc)
                    and attempts_used < per_chunk_max_attempts
                ):
                    logger.warning(
                        "GHOST B lane %d infrastructure retry chunk_id=%s attempt=%d/%d error_type=%s: %s",
                        pool_idx,
                        task.chunk_id,
                        attempts_used,
                        per_chunk_max_attempts,
                        last_error_type,
                        provider_error_summary(exc),
                    )
                    await asyncio.sleep(min(2.0, 0.25 * attempts_used))
                    continue
                logger.warning(
                    "GHOST B lane %d failed chunk_id=%s attempt=%d error_type=%s: %s",
                    pool_idx, task.chunk_id, attempts_used, last_error_type, exc,
                )
                break
        logger.error(
            "GHOST B failed chunk_id=%s lane=%d after %d/%d attempts: %s",
            task.chunk_id, pool_idx, attempts_used, per_chunk_max_attempts, last_exc,
        )
        retryable = last_error_type in {
            "retry_budget_exhausted",
            "provider_error",
            "rate_limited",
            "timeout",
        }
        retry_after = (
            datetime.utcnow() + timedelta(seconds=per_lane_cooldown_seconds)
            if retryable and per_lane_cooldown_seconds
            else None
        )
        failures_list.append(
            ExtractionFailureItem(
                chunk_id=task.chunk_id,
                doc_id=task.doc_id,
                corpus_id=task.corpus_id,
                model=str(entry["model"]),
                lane=pool_idx,
                attempts=attempts_used,
                error_type=last_error_type,
                error_message=provider_error_summary(last_exc, max_chars=1000),
                retryable=retryable,
                retry_after=retry_after,
                lane_state=lane_states.get(pool_idx, "healthy"),
            )
        )
        return None

    async def _lane_worker(pool_idx: int) -> None:
        """One coroutine per lane slot. Drains the shared queue until empty."""
        nonlocal failed_count, paused_due_doc_budget
        while True:
            if paused_due_doc_budget or pool_idx in disabled_lanes:
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
                        if failed_count >= per_doc_max_failed_chunks_before_pause:
                            paused_due_doc_budget = True
                            logger.warning(
                                "GHOST B pausing document after %d failed chunks reached budget=%d",
                                failed_count,
                                per_doc_max_failed_chunks_before_pause,
                            )
                            return
            finally:
                task_queue.task_done()

    async def _run_enabled_workers() -> None:
        # Spawn total_concurrency workers = sum of enabled per-lane max_concurrent.
        workers: list[asyncio.Task] = []
        for pool_idx, entry in enumerate(pool):
            if pool_idx in disabled_lanes:
                continue
            slots = _entry_concurrency_slots(entry, extraction_mode=extraction_mode)
            for _ in range(slots):
                workers.append(asyncio.create_task(_lane_worker(pool_idx)))
        if workers:
            await asyncio.gather(*workers, return_exceptions=False)

    await _run_enabled_workers()
    while not task_queue.empty():
        if paused_due_doc_budget:
            logger.warning(
                "GHOST B stopped with %d chunks queued because document failure budget was reached",
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
        if deterministic_doc_error:
            reason = deterministic_doc_error
            message = "Document graph extraction circuit opened after a deterministic request error."
        elif paused_due_doc_budget:
            reason = "retry_budget_exhausted"
            message = "Document graph extraction paused after failure retry budget was reached."
        elif disabled_lanes:
            reason = "all_lanes_exhausted"
            message = "No healthy extraction lane remained to process this chunk."
        else:
            reason = "not_processed"
            message = "Extraction worker exited before processing this chunk."
        retry_after = (
            datetime.utcnow() + timedelta(seconds=per_lane_cooldown_seconds)
            if reason in {"retry_budget_exhausted", "all_lanes_exhausted"}
            and per_lane_cooldown_seconds
            else None
        )
        lane_state = (
            "exhausted_for_document"
            if reason == "retry_budget_exhausted"
            else "cooling_down"
            if reason == "all_lanes_exhausted"
            else None
        )
        for task in unprocessed_tasks:
            failures_list.append(
                ExtractionFailureItem(
                    chunk_id=task.chunk_id,
                    doc_id=task.doc_id,
                    corpus_id=task.corpus_id,
                    model="pool",
                    lane=-1,
                    attempts=int(chunk_attempts.get(task.chunk_id) or 0),
                    error_type=reason,
                    error_message=message,
                    retryable=reason in {"retry_budget_exhausted", "all_lanes_exhausted"},
                    retry_after=retry_after,
                    lane_state=lane_state,
                )
            )
        failed_count += len(unprocessed_tasks)

    lane_state_counts: dict[str, int] = {}
    for state in lane_states.values():
        lane_state_counts[state] = lane_state_counts.get(state, 0) + 1
    metrics_context = {
        **(metrics_context or {}),
        "lane_state_counts": lane_state_counts,
        "disabled_lane_count": len(disabled_lanes),
        "repair_models": [str(e["model"]) for e in repair_pool],
        "defer_triple_repair": bool(defer_triple_repair),
        "retry_policy": {
            "per_chunk_max_attempts": per_chunk_max_attempts,
            "per_doc_max_failed_chunks_before_pause": per_doc_max_failed_chunks_before_pause,
            "per_lane_max_consecutive_failures": per_lane_max_consecutive_failures,
            "per_lane_cooldown_seconds": per_lane_cooldown_seconds,
        },
    }

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
                metrics_context={
                    **metrics_context,
                    "relation_repair_queued_count": len(relation_repair_candidates),
                },
            ),
            relation_repairs=relation_repair_candidates,
        )
    return results
