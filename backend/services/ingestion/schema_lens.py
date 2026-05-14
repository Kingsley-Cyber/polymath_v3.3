"""Auto schema lens generation for Ghost B ingestion.

This module implements the bounded version of the TrustGraph/OntoRAG idea:
let the LLM profile corpus-local semantics, then clamp every suggestion back
to Polymath's approved universal entity and relation schema. The lens guides
extraction prompts; it never creates permanent schema labels by itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from config import get_settings
from services.ghost_b import (
    SchemaLens,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
)

logger = logging.getLogger(__name__)

SCHEMA_LENS_VERSION = "polymath.schema_lens.v1"
_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_+./'-]{2,}")

_RELATION_ALIAS_TO_APPROVED: dict[str, str] = {
    "built on": "depends_on",
    "based on": "derived_from",
    "powered by": "uses",
    "uses": "uses",
    "utilizes": "uses",
    "consumes": "uses",
    "used by": "uses",
    "used for": "uses",
    "reads": "uses",
    # `calls` was collapsed into `uses` in the universal schema; aliases
    # route to the surviving predicate.
    "calls": "uses",
    "invokes": "uses",
    "queries": "uses",
    "requires": "depends_on",
    "needs": "depends_on",
    "depends on": "depends_on",
    "contains": "part_of",
    "includes": "part_of",
    "composed of": "part_of",
    "implements": "implements",
    "realizes": "implements",
    "outputs": "produces",
    "generates": "produces",
    "creates": "produces",
    "stores": "stores",
    "stored in": "stores",
    "saved in": "stores",
    "persists": "stores",
    "persisted in": "stores",
    "saves to": "stores",
    # `extracts` was merged into `detects`; both verb classes route there.
    "extracts": "detects",
    "extracted from": "detects",
    "feature extraction": "detects",
    "entity extraction": "detects",
    "detects": "detects",
    "identifies": "detects",
    "classifies": "classifies",
    "predicts": "classifies",
    "runs on": "runs_on",
    "executes on": "runs_on",
    "on-device": "runs_on",
    "trained on": "trained_on",
    "training data": "trained_on",
    "training set": "trained_on",
    "supports": "supports",
    "enables": "supports",
    "provides": "supports",
    "facilitates": "supports",
    "represents": "represents",
    "models": "represents",
    "encodes": "represents",
    "maps to": "maps_to",
    "converts": "maps_to",
    "transforms": "maps_to",
    "contains": "part_of",
    "includes": "part_of",
    "belongs to": "member_of",
    "covers": "references",
    "teaches": "references",
    "defines": "references",
    "shows": "references",
    "demonstrates": "references",
    "cites": "references",
    "mentions": "references",
    "references": "references",
    "inspired by": "derived_from",
    "derived from": "derived_from",
    "conflicts with": "contradicts",
    "contradicts": "contradicts",
    "supersedes": "overrides",
    "overrides": "overrides",
    # Phase 5 — Roblox/Luau event vocabulary. Added globally because the
    # alias detector only activates when the alias text appears in the
    # sampled corpus text AND the target predicate is in the allowed
    # schema — so a book that says "the wire connects to the terminal"
    # picks up `connects→uses` (correct hint) without polluting Roblox
    # corpora in the other direction.
    "fires": "uses",
    "connects": "uses",
    "binds": "depends_on",
    "loads": "uses",
    "tweens": "uses",
}

_DOMAIN_RULES: list[dict[str, Any]] = [
    {
        "domain": "product_prd",
        "triggers": [
            "prd", "product", "feature", "requirement", "workflow", "user",
            "screen", "modal", "app", "api", "architecture", "feasibility",
            "constraint", "backend", "frontend",
        ],
        "entities": ["Product", "Method", "Concept", "Document", "Rule", "Artifact"],
        "relations": ["part_of", "uses", "implements", "depends_on", "produces", "stores", "supports", "references"],
        "object_kinds": ["App", "Service", "Report", "API", "Model", "Database"],
        "families": ["product_design", "identity_extraction", "workflow_automation"],
    },
    {
        "domain": "generative_ai",
        "triggers": [
            "llm", "genai", "generative ai", "rag", "embedding", "vector",
            "prompt", "agent", "mistral", "openai", "model", "synthesis",
        ],
        "entities": ["Concept", "Method", "Product", "Artifact", "Document"],
        "relations": ["uses", "implements", "depends_on", "produces", "detects", "trained_on", "supports", "derived_from", "references"],
        "object_kinds": ["Model", "API", "Service", "Dataset", "Report"],
        "families": ["generative_ai", "retrieval_augmented_generation", "agentic_ai"],
    },
    {
        "domain": "creative_coding",
        "triggers": [
            "processing", "pvector", "particle", "mover", "force", "box2d",
            "generative art", "creative coding", "sketch", "simulation",
            "algorithm", "neural network",
        ],
        "entities": ["Concept", "Method", "Artifact", "Product", "Document"],
        "relations": ["uses", "implements", "part_of", "represents", "maps_to", "derived_from", "produces", "references"],
        "object_kinds": ["Library", "Framework", "Snippet", "Book", "Tutorial"],
        "families": ["creative_coding", "physics_simulation", "generative_art"],
    },
    {
        "domain": "cymatics",
        "triggers": [
            "cymatics", "chladni", "oscillation", "fourier", "wave",
            "frequency", "vibration", "trigonometric", "periodic",
        ],
        "entities": ["Concept", "Method", "Document", "Event"],
        "relations": ["uses", "represents", "maps_to", "derived_from", "causes", "references", "implements"],
        "object_kinds": ["Report", "Paper", "Dataset"],
        "families": ["cymatics", "wave_physics", "generative_art"],
    },
    {
        "domain": "research_literature",
        "triggers": [
            "paper", "book", "report", "study", "university", "graduate",
            "chapter", "abstract", "methodology", "citation",
        ],
        "entities": ["Document", "Concept", "Method", "Person", "Organization"],
        "relations": ["references", "created_by", "derived_from", "part_of", "uses", "represents", "supports"],
        "object_kinds": ["Book", "Report", "Paper", "Whitepaper"],
        "families": ["research_literature"],
    },
    {
        # Phase 5 Gate 2 — Roblox/Luau prose corpora (e.g. YouTube transcripts,
        # creator-docs, devforum posts). Biases Ghost B's universal-schema
        # extraction toward Method/Product/Artifact so engine terms like
        # `Humanoid`, `TweenService`, `ParticleEmitter` extract as proper
        # entities — not generic Concept — and share identity with the
        # code-side entities produced by Phase 4 + roblox_ontology.
        "domain": "roblox",
        "triggers": [
            "roblox", "luau",
            "game:getservice", "instance.new",
            "remoteevent", "remotefunction", "bindableevent",
            "humanoid", "tweenservice", "runservice",
            "modulescript", "localscript", "replicatedstorage",
            "serverstorage", "serverscriptservice",
            "particleemitter", "animator", "animationtrack",
            "cframe", "vector3", "udim2", "color3",
            "datastore", "userinputservice", "httpservice",
            "datamodel", "workspace",
        ],
        "entities": ["Method", "Product", "Artifact", "Concept", "Document"],
        "relations": [
            "uses", "implements", "depends_on", "produces",
            "defines", "example_of", "during",
        ],
        "relation_aliases": {
            "fires": "uses",
            "connects": "uses",
            "binds": "depends_on",
            "loads": "uses",
            "requires": "depends_on",
            "exposes": "implements",
            "instances": "produces",
            "creates": "produces",
            "tweens": "uses",
        },
        "object_kinds": [
            "Service", "Class", "Event", "Signal",
            "Animation", "ParticleEffect", "DataModel",
        ],
        "families": ["roblox", "game_engine", "game_design"],
    },
]


def _dedupe(values: list[str], *, limit: int = 12) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= limit:
            break
    return out


def _snake(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "").strip().lower())
    return re.sub(r"_+", "_", value).strip("_")


def _sample_text(filename: str, parents: list[Any], children: list[Any]) -> str:
    settings = get_settings()
    max_chunks = int(getattr(settings, "SCHEMA_LENS_SAMPLE_CHUNKS", 8))
    max_chars = int(getattr(settings, "SCHEMA_LENS_SAMPLE_CHARS", 6000))
    parts = [f"filename: {filename}"]
    for parent in parents[:3]:
        heading = " / ".join(getattr(parent, "heading_path", []) or [])
        text = str(getattr(parent, "text", "") or "")
        if heading:
            parts.append(f"heading: {heading}")
        if text:
            parts.append(text[:900])
    for child in children[:max_chunks]:
        text = str(getattr(child, "text", "") or "")
        if text:
            parts.append(text[:700])
    return "\n\n".join(parts)[:max_chars]


def _relation_aliases_from_text(text: str, relation_schema: list[str]) -> dict[str, str]:
    allowed = set(relation_schema or UNIVERSAL_RELATION_SCHEMA)
    aliases: dict[str, str] = {}
    lower = text.lower()
    for alias, predicate in _RELATION_ALIAS_TO_APPROVED.items():
        if predicate in allowed and alias in lower:
            aliases[alias] = predicate
    return aliases


def build_deterministic_schema_lens(
    *,
    corpus_id: str,
    filename: str,
    parents: list[Any],
    children: list[Any],
    entity_schema: list[str] | None = None,
    relation_schema: list[str] | None = None,
) -> SchemaLens:
    """Build a cheap local lens from filename/headings/chunk text.

    This is the always-available fallback when the optional LLM profiling call
    fails. It also supplements a stored corpus lens as new document vocabulary
    appears during larger batch ingests.
    """
    text = _sample_text(filename, parents, children)
    lower = text.lower()
    allowed_entities = set(entity_schema or UNIVERSAL_ENTITY_SCHEMA)
    allowed_relations = set(relation_schema or UNIVERSAL_RELATION_SCHEMA)

    domains: list[str] = []
    entity_types: list[str] = []
    relations: list[str] = []
    object_kinds: list[str] = []
    families: list[str] = []

    for rule in _DOMAIN_RULES:
        if any(trigger in lower for trigger in rule["triggers"]):
            domains.append(rule["domain"])
            entity_types.extend([v for v in rule["entities"] if v in allowed_entities])
            relations.extend([v for v in rule["relations"] if v in allowed_relations])
            object_kinds.extend(rule["object_kinds"])
            families.extend(rule["families"])

    if not domains:
        domains.append("general_knowledge")
        entity_types.extend([v for v in ["Concept", "Document", "Method", "Product"] if v in allowed_entities])
        relations.extend([v for v in ["references", "part_of", "uses", "related_to"] if v in allowed_relations])

    if "Architecture_Feasibility_Report".lower() in lower or "feasibility report" in lower:
        object_kinds.append("Report")
        families.append("architecture_feasibility")
        if "Document" in allowed_entities:
            entity_types.insert(0, "Document")

    lens_hash = hashlib.sha1(f"{corpus_id}:{','.join(domains)}".encode("utf-8")).hexdigest()[:10]
    return SchemaLens(
        lens_id=f"lens:{lens_hash}",
        version=SCHEMA_LENS_VERSION,
        status="ready",
        source="deterministic",
        corpus_domains=_dedupe(domains, limit=8),
        preferred_entity_types=_dedupe(entity_types, limit=8),
        preferred_relations=_dedupe(relations, limit=10),
        relation_aliases=_relation_aliases_from_text(text, list(allowed_relations)),
        object_kinds=_dedupe(object_kinds, limit=10),
        canonical_families=_dedupe([_snake(v) for v in families], limit=10),
        confidence=0.55,
    )


def sanitize_schema_lens(
    payload: dict[str, Any] | SchemaLens | None,
    *,
    base: SchemaLens,
    entity_schema: list[str] | None = None,
    relation_schema: list[str] | None = None,
    source: str | None = None,
) -> SchemaLens:
    """Clamp an LLM/stored lens to approved output schema.

    Relation alias values must resolve to approved predicates. Entity type
    preferences must already exist in the configured schema. This is the guard
    that keeps auto-profiling useful instead of noisy.
    """
    if isinstance(payload, SchemaLens):
        data = payload.to_dict()
    elif isinstance(payload, dict):
        data = payload
    else:
        data = {}

    allowed_entities = set(entity_schema or UNIVERSAL_ENTITY_SCHEMA)
    allowed_relations = set(relation_schema or UNIVERSAL_RELATION_SCHEMA)

    aliases: dict[str, str] = dict(base.relation_aliases)
    for raw_alias, raw_predicate in (data.get("relation_aliases") or {}).items():
        alias = str(raw_alias or "").strip().lower()
        predicate = str(raw_predicate or "").strip()
        if not alias:
            continue
        predicate = _RELATION_ALIAS_TO_APPROVED.get(predicate.lower(), predicate)
        if predicate in allowed_relations:
            aliases[alias[:60]] = predicate

    entity_types = [
        v for v in [*base.preferred_entity_types, *(data.get("preferred_entity_types") or [])]
        if v in allowed_entities
    ]
    relations = [
        v for v in [*base.preferred_relations, *(data.get("preferred_relations") or [])]
        if v in allowed_relations
    ]
    domains = [_snake(v) for v in [*base.corpus_domains, *(data.get("corpus_domains") or [])]]
    families = [
        _snake(v)
        for v in [*base.canonical_families, *(data.get("canonical_families") or [])]
    ]

    return SchemaLens(
        lens_id=str(data.get("lens_id") or base.lens_id),
        version=SCHEMA_LENS_VERSION,
        status="ready",
        source=source or str(data.get("source") or base.source),
        corpus_domains=_dedupe(domains, limit=8),
        preferred_entity_types=_dedupe(entity_types, limit=8),
        preferred_relations=_dedupe(relations, limit=10),
        relation_aliases=dict(list(aliases.items())[:16]),
        object_kinds=_dedupe([*base.object_kinds, *(data.get("object_kinds") or [])], limit=10),
        canonical_families=_dedupe(families, limit=10),
        confidence=max(float(data.get("confidence") or 0.0), base.confidence),
    )


def merge_schema_lenses(
    stored: SchemaLens | dict | None,
    doc_lens: SchemaLens,
    *,
    entity_schema: list[str] | None = None,
    relation_schema: list[str] | None = None,
) -> SchemaLens:
    """Merge a stored corpus lens with cheap document-local hints."""
    base = SchemaLens.from_dict(stored if isinstance(stored, dict) else None) or doc_lens
    merged_payload = {
        "lens_id": base.lens_id,
        "source": "stored+deterministic",
        "corpus_domains": [*base.corpus_domains, *doc_lens.corpus_domains],
        "preferred_entity_types": [
            *base.preferred_entity_types,
            *doc_lens.preferred_entity_types,
        ],
        "preferred_relations": [*base.preferred_relations, *doc_lens.preferred_relations],
        "relation_aliases": {**base.relation_aliases, **doc_lens.relation_aliases},
        "object_kinds": [*base.object_kinds, *doc_lens.object_kinds],
        "canonical_families": [*base.canonical_families, *doc_lens.canonical_families],
        "confidence": max(base.confidence, doc_lens.confidence),
    }
    return sanitize_schema_lens(
        merged_payload,
        base=doc_lens,
        entity_schema=entity_schema,
        relation_schema=relation_schema,
        source="stored+deterministic",
    )


async def _profile_with_llm(
    *,
    sample: str,
    base: SchemaLens,
    entity_schema: list[str],
    relation_schema: list[str],
    pool: list[dict],
    model: str | None,
) -> dict[str, Any] | None:
    settings = get_settings()
    if not bool(getattr(settings, "SCHEMA_LENS_LLM_ENABLED", True)):
        return None

    entry = pool[0] if pool else {
        "model": model or settings.DEFAULT_COMPLETION_MODEL,
        "base_url": None,
        "api_key": None,
        "extra_params": {},
    }
    prompt = (
        "Profile this corpus sample and propose a bounded schema lens for extraction.\n"
        "You are NOT creating a new schema. Only choose from approved entity types "
        "and approved relation predicates. Return JSON only.\n\n"
        f"Approved entity types: {', '.join(entity_schema)}\n"
        f"Approved relations: {', '.join(relation_schema)}\n"
        "Required JSON keys:\n"
        "- corpus_domains: short snake_case labels\n"
        "- preferred_entity_types: subset of approved entity types\n"
        "- preferred_relations: subset of approved relations\n"
        "- relation_aliases: mapping from phrase in corpus to approved relation\n"
        "- object_kinds: short TitleCase kind labels to notice, not output fields\n"
        "- canonical_families: short snake_case family labels to notice\n"
        "- confidence: number 0-1\n\n"
        f"Deterministic starting lens:\n{json.dumps(base.to_dict(), ensure_ascii=True)}\n\n"
        f"Corpus sample:\n{sample}"
    )
    payload: dict[str, Any] = {
        "model": entry["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a conservative corpus profiler. Output only valid JSON. "
                    "Never invent relation predicates outside the approved list."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    if entry.get("base_url"):
        payload["api_base"] = entry["base_url"]
    if entry.get("api_key"):
        payload["api_key"] = entry["api_key"]
    for key, value in (entry.get("extra_params") or {}).items():
        if key not in ("model", "messages", "response_format"):
            payload[key] = value

    headers = {
        "Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}",
        "Content-Type": "application/json",
    }
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{settings.LITELLM_URL}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()
            raw = body["choices"][0]["message"]["content"]
            logger.info(
                "schema_lens_llm profile model=%s duration=%.2fs",
                entry["model"],
                time.perf_counter() - started,
            )
            return json.loads(raw)
    except Exception as exc:
        logger.warning("schema_lens_llm failed; using deterministic lens: %s", exc)
        return None


async def get_or_create_schema_lens(
    *,
    db: AsyncIOMotorDatabase,
    corpus_id: str,
    filename: str,
    parents: list[Any],
    children: list[Any],
    entity_schema: list[str] | None,
    relation_schema: list[str] | None,
    pool: list[dict],
    model: str | None,
) -> SchemaLens:
    """Return a corpus lens, creating/merging it without user prompting.

    First document in a corpus gets one optional LLM profiling call. Later
    documents reuse the stored lens and cheaply merge deterministic hints so a
    broad 550-document corpus can widen over time without paying one profiler
    call per file.
    """
    entity_vocab = entity_schema or UNIVERSAL_ENTITY_SCHEMA
    relation_vocab = relation_schema or UNIVERSAL_RELATION_SCHEMA
    doc_lens = build_deterministic_schema_lens(
        corpus_id=corpus_id,
        filename=filename,
        parents=parents,
        children=children,
        entity_schema=entity_vocab,
        relation_schema=relation_vocab,
    )

    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"schema_lens": 1},
    )
    stored = (corpus or {}).get("schema_lens")
    if stored and stored.get("version") == SCHEMA_LENS_VERSION:
        lens = merge_schema_lenses(
            stored,
            doc_lens,
            entity_schema=entity_vocab,
            relation_schema=relation_vocab,
        )
    else:
        llm_payload = await _profile_with_llm(
            sample=_sample_text(filename, parents, children),
            base=doc_lens,
            entity_schema=entity_vocab,
            relation_schema=relation_vocab,
            pool=pool,
            model=model,
        )
        lens = sanitize_schema_lens(
            llm_payload,
            base=doc_lens,
            entity_schema=entity_vocab,
            relation_schema=relation_vocab,
            source="llm+deterministic" if llm_payload else "deterministic",
        )

    await db["corpora"].update_one(
        {"corpus_id": corpus_id},
        {
            "$set": {
                "schema_lens": lens.to_dict(),
                "schema_lens_updated_at": datetime.utcnow(),
            }
        },
    )
    logger.info(
        "schema_lens ready corpus=%s source=%s domains=%s relations=%s",
        corpus_id[:8],
        lens.source,
        ",".join(lens.corpus_domains[:4]),
        ",".join(lens.preferred_relations[:6]),
    )
    return lens
