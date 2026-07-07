#!/usr/bin/env python
"""Run and audit the tested Polymath extraction model roster.

This is a standalone implementation scaffold for the cloud extraction lanes
documented in docs/extraction-router-implementation-handoff-2026-07-07.md.
It intentionally does not mutate the production ingestion path. The goal is
to make provider behavior, prompt shape, schema validation, deterministic
repairs, and audit artifacts explicit before wiring the router into Ghost B.

Example:
    python scripts/run_extraction_model_router.py ^
        --input "C:\\Users\\Sammb\\Downloads\\On March 14, 2026, Dr. Mira Chen ar.txt" ^
        --profiles longcat_2_direct hy3_preview_direct hy3_direct mistral_nemo_schema ^
        --audit-dir .codex-logs\\extraction-router

Required environment variables by profile:
    LONGCAT_API_KEY
    SILICONFLOW_API_KEY
    OPENROUTER_API_KEY
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


ProviderName = Literal["longcat", "siliconflow", "openrouter"]
ExtractionMode = Literal["prompt_json", "json_schema", "compact_ir"]


ENTITY_TYPES = {
    "Person",
    "Organization",
    "Location",
    "Event",
    "Concept",
    "Method",
    "Product",
    "Software",
    "Document",
    "Standard",
    "Rule",
    "Law",
    "Artifact",
    "TimeReference",
    "other",
}

PREDICATES = {
    "part_of",
    "member_of",
    "located_in",
    "works_for",
    "created_by",
    "owns",
    "affiliated_with",
    "synonym_of",
    "instance_of",
    "example_of",
    "uses",
    "references",
    "implements",
    "depends_on",
    "produces",
    "stores",
    "detects",
    "supports",
    "defines",
    "represents",
    "maps_to",
    "preceded_by",
    "causes",
    "overlaps",
    "during",
    "derived_from",
    "contradicts",
    "excepts",
    "overrides",
    "related_to",
}

FACT_TYPES = {
    "property",
    "status",
    "timestamp",
    "quantity",
    "threshold",
    "category",
    "tag",
    "rule_condition",
    "rule_action",
}

PREDICATE_REMAP: dict[str, tuple[str, bool]] = {
    "about": ("related_to", False),
    "approved": ("supports", False),
    "approved_by": ("supports", False),
    "authored": ("created_by", True),
    "authored_by": ("created_by", False),
    "belongs_to": ("member_of", False),
    "blamed": ("related_to", False),
    "built": ("created_by", True),
    "built_by": ("created_by", False),
    "contains": ("part_of", True),
    "contract_with": ("affiliated_with", False),
    "created": ("created_by", True),
    "creates": ("produces", False),
    "designed": ("created_by", True),
    "designed_by": ("created_by", False),
    "detected": ("detects", False),
    "detects": ("detects", False),
    "developed": ("created_by", True),
    "developed_by": ("created_by", False),
    "founded": ("created_by", True),
    "founded_by": ("created_by", False),
    "includes": ("part_of", True),
    "located_at": ("located_in", False),
    "opened": ("related_to", False),
    "opens": ("related_to", False),
    "paid": ("supports", False),
    "ran": ("affiliated_with", False),
    "run_by": ("affiliated_with", False),
    "signed_contract_with": ("affiliated_with", False),
    "stored": ("stores", False),
    "stored_in": ("stores", True),
    "supervised": ("supports", False),
    "supervised_by": ("supports", False),
    "used": ("uses", False),
    "used_by": ("uses", True),
    "uses": ("uses", False),
}

PERSON_TITLES = ("dr ", "captain ", "capt ", "prof ", "mr ", "mrs ", "ms ")


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: ProviderName
    model_id: str
    mode: ExtractionMode
    base_url: str
    api_key_env: str
    priority: int
    max_chunk_chars: int
    max_output_tokens: int
    supports_json_schema: bool
    requires_thinking_disabled: bool = False
    notes: str = ""


@dataclass
class RepairMetrics:
    raw_json_parse: bool = False
    balanced_json_salvage: bool = False
    candidate_pydantic: bool = False
    compiled_pydantic: bool = False
    predicate_remap_count: int = 0
    predicate_drop_count: int = 0
    object_kind_repair_count: int = 0
    endpoint_completion_count: int = 0
    semantic_repair_count: int = 0
    confidence_repair_count: int = 0
    ir_compile_count: int = 0
    error: str = ""


@dataclass
class RunResult:
    profile: str
    provider: str
    model: str
    chunk_id: str
    ok: bool
    accepted: bool
    acceptance_score: int
    elapsed_s: float
    finish_reason: str | None
    usage: dict[str, Any]
    counts: dict[str, int]
    repairs: RepairMetrics
    audit_prefix: str
    error: str = ""


MODEL_PROFILES: dict[str, ModelProfile] = {
    "longcat_2_direct": ModelProfile(
        name="longcat_2_direct",
        provider="longcat",
        model_id="LongCat-2.0",
        mode="prompt_json",
        base_url="https://api.longcat.chat/openai/v1",
        api_key_env="LONGCAT_API_KEY",
        priority=10,
        max_chunk_chars=4500,
        max_output_tokens=4500,
        supports_json_schema=False,
        requires_thinking_disabled=True,
        notes="Primary direct lane; must disable thinking.",
    ),
    "hy3_preview_direct": ModelProfile(
        name="hy3_preview_direct",
        provider="siliconflow",
        model_id="tencent/Hy3-preview",
        mode="prompt_json",
        base_url="https://api.siliconflow.com/v1",
        api_key_env="SILICONFLOW_API_KEY",
        priority=20,
        max_chunk_chars=4000,
        max_output_tokens=3500,
        supports_json_schema=False,
        notes="Prompt-only lane; SiliconFlow JSON mode is unsupported.",
    ),
    "hy3_direct": ModelProfile(
        name="hy3_direct",
        provider="siliconflow",
        model_id="tencent/Hy3",
        mode="prompt_json",
        base_url="https://api.siliconflow.com/v1",
        api_key_env="SILICONFLOW_API_KEY",
        priority=30,
        max_chunk_chars=4000,
        max_output_tokens=3500,
        supports_json_schema=False,
        notes="Prompt-only fallback to Hy3-preview.",
    ),
    "mistral_nemo_schema": ModelProfile(
        name="mistral_nemo_schema",
        provider="openrouter",
        model_id="mistralai/mistral-nemo",
        mode="json_schema",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        priority=40,
        max_chunk_chars=4000,
        max_output_tokens=3600,
        supports_json_schema=True,
        notes="OpenRouter strict structured-output lane.",
    ),
    "ling_flash_ir": ModelProfile(
        name="ling_flash_ir",
        provider="openrouter",
        model_id="inclusionai/ling-2.6-flash",
        mode="compact_ir",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        priority=90,
        max_chunk_chars=1800,
        max_output_tokens=1600,
        supports_json_schema=False,
        notes="Auxiliary short-chunk/table IR lane; not one of the four primary direct lanes.",
    ),
}


def utc_stamp() -> str:
    return dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_extraction_response_model():
    from services.ghost_b_schemas import ExtractionResponse

    return ExtractionResponse


def pin_all_required(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt Pydantic's schema for strict provider-side json_schema modes."""

    def walk(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        if node.get("type") == "object" and isinstance(node.get("properties"), dict):
            node["required"] = list(node["properties"].keys())
            node["additionalProperties"] = False
        for key in ("properties", "$defs", "definitions"):
            children = node.get(key)
            if isinstance(children, dict):
                for sub in children.values():
                    walk(sub)
        if isinstance(node.get("items"), dict):
            walk(node["items"])
        if isinstance(node.get("additionalProperties"), dict):
            walk(node["additionalProperties"])
        for key in ("anyOf", "allOf", "oneOf"):
            values = node.get(key)
            if isinstance(values, list):
                for sub in values:
                    walk(sub)
        return node

    return walk(copy.deepcopy(schema))


def extraction_response_json_schema() -> dict[str, Any]:
    model = load_extraction_response_model()
    return pin_all_required(model.model_json_schema())


def strip_title(name: str) -> str:
    lowered = name.strip().lower()
    for title in PERSON_TITLES:
        if lowered.startswith(title):
            return name.strip()[len(title) :].strip()
    return name.strip()


def canonicalize(value: Any) -> str:
    text = strip_title(str(value or "")).strip()
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" \t\r\n\"'`.,;:()[]{}")
    return text.lower()


def surface_from_canonical(name: str) -> str:
    clean = str(name or "").strip()
    if not clean:
        return ""
    if any(ch.isupper() for ch in clean) or any(ch.isdigit() for ch in clean):
        return clean
    return " ".join(part.capitalize() for part in clean.split())


def clamp_confidence(value: Any, metrics: RepairMetrics, default: float = 0.82) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        metrics.confidence_repair_count += 1
        return default
    if conf < 0.0 or conf > 1.0:
        metrics.confidence_repair_count += 1
    return max(0.0, min(1.0, conf))


def normalize_predicate(raw: Any, metrics: RepairMetrics) -> tuple[str, bool, str]:
    original = str(raw or "").strip()
    key = re.sub(r"[^a-z0-9_]+", "_", original.lower()).strip("_")
    if key in PREDICATES:
        return key, False, original
    if key in PREDICATE_REMAP:
        mapped, reverse = PREDICATE_REMAP[key]
        metrics.predicate_remap_count += 1
        return mapped, reverse, original
    metrics.predicate_remap_count += 1
    return "related_to", False, original


def infer_relation_object_kind(value: Any, object_name: str, entity_names: set[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"entity", "literal"}:
        return normalized
    if canonicalize(object_name) in entity_names:
        return "entity"
    if normalized in {
        "person",
        "organization",
        "location",
        "event",
        "concept",
        "method",
        "product",
        "software",
        "document",
        "standard",
        "rule",
        "law",
        "artifact",
        "timereference",
        "other",
    }:
        return "entity"
    if re.search(r"\b[A-Z][A-Za-z0-9-]+(?:\s+[A-Z][A-Za-z0-9-]+)*\b", str(object_name or "")):
        return "entity"
    return "literal"


def first_value(obj: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return default


def strip_to_contract(candidate: dict[str, Any], metrics: RepairMetrics) -> dict[str, Any]:
    entities_in = candidate.get("entities") or []
    relations_in = candidate.get("relations") or []
    facts_in = candidate.get("facts") or []

    if not isinstance(entities_in, list):
        entities_in = []
    if not isinstance(relations_in, list):
        relations_in = []
    if not isinstance(facts_in, list):
        facts_in = []

    entities: list[dict[str, Any]] = []
    seen_entities: set[str] = set()
    for raw in entities_in:
        if isinstance(raw, str):
            raw = {"canonical_name": raw, "surface_form": raw, "entity_type": "Concept", "confidence": 0.82}
        if not isinstance(raw, dict):
            continue
        canonical_name = canonicalize(first_value(raw, "canonical_name", "cn", "name"))
        if not canonical_name:
            continue
        if canonical_name in seen_entities:
            continue
        entity_type = str(first_value(raw, "entity_type", "et", "type", default="other")).strip()
        if entity_type not in ENTITY_TYPES:
            entity_type = "other"
        seen_entities.add(canonical_name)
        entities.append(
            {
                "canonical_name": canonical_name,
                "surface_form": str(first_value(raw, "surface_form", "sf", "name", default=surface_from_canonical(canonical_name))).strip()[:300],
                "entity_type": entity_type,
                "confidence": clamp_confidence(first_value(raw, "confidence", "cf", default=0.82), metrics),
                "query_aliases": [str(v).strip() for v in (raw.get("query_aliases") or []) if str(v).strip()][:5],
                "definitional_phrase": str(raw.get("definitional_phrase") or "").strip()[:200],
                "object_kind": str(first_value(raw, "object_kind", "e_kind", default="")).strip()[:100],
            }
        )

    relations: list[dict[str, Any]] = []
    for raw in relations_in:
        if isinstance(raw, str):
            metrics.predicate_drop_count += 1
            continue
        if not isinstance(raw, dict):
            continue
        subject = canonicalize(first_value(raw, "subject", "sub"))
        obj = canonicalize(first_value(raw, "object", "obj"))
        if not subject or not obj:
            continue
        predicate, reverse, original_predicate = normalize_predicate(first_value(raw, "predicate", "pred"), metrics)
        if reverse:
            subject, obj = obj, subject
        object_kind_raw = first_value(raw, "object_kind", "ok", default="literal")
        object_kind = infer_relation_object_kind(object_kind_raw, obj, seen_entities)
        if str(object_kind_raw or "").strip().lower() != object_kind:
            metrics.object_kind_repair_count += 1
        relation_cue = str(first_value(raw, "relation_cue", "cue", default="")).strip()[:120]
        if not relation_cue and original_predicate and original_predicate != predicate:
            relation_cue = original_predicate[:120]
        relations.append(
            {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "object_kind": object_kind,
                "confidence": clamp_confidence(first_value(raw, "confidence", "cf", default=0.82), metrics),
                "evidence_phrase": str(first_value(raw, "evidence_phrase", "evidence", "ev", default="")).strip()[:500],
                "relation_cue": relation_cue,
            }
        )

    facts: list[dict[str, Any]] = []
    for raw in facts_in:
        if not isinstance(raw, dict):
            continue
        subject = canonicalize(first_value(raw, "subject", "sub"))
        value = str(first_value(raw, "value", "val", default="")).strip()
        if not subject or not value:
            continue
        fact_type = str(first_value(raw, "fact_type", "ft", default="property")).strip()
        if fact_type not in FACT_TYPES:
            fact_type = "property"
        property_name = str(first_value(raw, "property_name", "pn", "predicate", default="value")).strip()
        property_name = re.sub(r"[^a-zA-Z0-9_]+", "_", property_name.lower()).strip("_") or "value"
        facts.append(
            {
                "subject": subject,
                "fact_type": fact_type,
                "property_name": property_name[:80],
                "value": value[:500],
                "unit": str(raw.get("unit") or "").strip()[:40],
                "condition": str(first_value(raw, "condition", "cond", default="")).strip()[:300],
                "confidence": clamp_confidence(first_value(raw, "confidence", "cf", default=0.82), metrics),
                "evidence_phrase": str(first_value(raw, "evidence_phrase", "evidence", "ev", default="")).strip()[:500],
            }
        )

    return {"entities": entities, "relations": relations, "facts": facts}


def entity_type_lookup(compiled: dict[str, Any]) -> dict[str, str]:
    return {
        canonicalize(e.get("canonical_name")): str(e.get("entity_type") or "other")
        for e in compiled.get("entities", [])
        if isinstance(e, dict)
    }


def reverse_relation(rel: dict[str, Any]) -> None:
    rel["subject"], rel["object"] = rel["object"], rel["subject"]


def semantic_repairs(compiled: dict[str, Any], metrics: RepairMetrics) -> None:
    types = entity_type_lookup(compiled)
    for rel in compiled.get("relations", []):
        if not isinstance(rel, dict):
            continue
        sub_type = types.get(canonicalize(rel.get("subject")), "other")
        obj_type = types.get(canonicalize(rel.get("object")), "other")
        predicate = rel.get("predicate")
        cue = str(rel.get("relation_cue") or rel.get("evidence_phrase") or "").lower()
        if predicate == "created_by" and sub_type == "Person" and obj_type in {
            "Organization",
            "Product",
            "Software",
            "Document",
            "Standard",
            "Rule",
            "Law",
            "Artifact",
            "Concept",
            "Method",
        }:
            reverse_relation(rel)
            metrics.semantic_repair_count += 1
        elif predicate == "works_for" and sub_type == "Organization" and obj_type == "Person":
            reverse_relation(rel)
            metrics.semantic_repair_count += 1
        elif predicate == "located_in" and obj_type not in {"Location", "Organization"} and sub_type == "Location":
            reverse_relation(rel)
            metrics.semantic_repair_count += 1
        elif predicate == "created_by" and "founded by" in cue and obj_type not in {"Person", "Organization"}:
            reverse_relation(rel)
            metrics.semantic_repair_count += 1


def complete_endpoints(compiled: dict[str, Any], metrics: RepairMetrics) -> None:
    entities = compiled.setdefault("entities", [])
    seen = {canonicalize(e.get("canonical_name")) for e in entities if isinstance(e, dict)}

    def add_entity(name: str) -> None:
        canonical_name = canonicalize(name)
        if not canonical_name or canonical_name in seen:
            return
        entities.append(
            {
                "canonical_name": canonical_name,
                "surface_form": surface_from_canonical(canonical_name),
                "entity_type": "other",
                "confidence": 0.72,
                "query_aliases": [],
                "definitional_phrase": "",
                "object_kind": "",
            }
        )
        seen.add(canonical_name)
        metrics.endpoint_completion_count += 1

    for rel in compiled.get("relations", []):
        if not isinstance(rel, dict):
            continue
        add_entity(rel.get("subject", ""))
        if rel.get("object_kind") == "entity":
            add_entity(rel.get("object", ""))


def compile_ling_ir(candidate: dict[str, Any], metrics: RepairMetrics) -> dict[str, Any]:
    """Compile Ling's compact IR contract into the Polymath contract."""
    entities = candidate.get("entities") or []
    frames = candidate.get("frames") or candidate.get("relations") or []
    facts = candidate.get("facts") or []
    relations: list[dict[str, Any]] = []

    for frame in frames:
        if not isinstance(frame, dict):
            continue
        subject = first_value(frame, "subject", "row", "entity", "source")
        target = first_value(frame, "object", "target", "slot", "label", "concept")
        cue = first_value(frame, "relation_cue", "cue", "verb", "frame_type", default="defines")
        if not subject or not target:
            continue
        relations.append(
            {
                "subject": subject,
                "predicate": "defines" if str(cue).lower() in {"defines", "concepts to extract"} else "related_to",
                "object": target,
                "object_kind": "entity",
                "confidence": first_value(frame, "confidence", "cf", default=0.82),
                "evidence_phrase": first_value(frame, "evidence_phrase", "evidence", "ev", default=""),
                "relation_cue": cue,
            }
        )
        metrics.ir_compile_count += 1

    return {"entities": entities, "relations": relations, "facts": facts}


def validate_contract(payload: dict[str, Any]) -> tuple[bool, str]:
    try:
        model = load_extraction_response_model()
        model.model_validate(payload)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def extract_balanced_json(text: str) -> tuple[str, bool]:
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("empty model content")

    wrapper = re.search(r"<json_payload>\s*(.*?)\s*</json_payload>", stripped, flags=re.IGNORECASE | re.DOTALL)
    if wrapper:
        return wrapper.group(1).strip(), False

    if stripped.startswith("{"):
        try:
            json.loads(stripped)
            return stripped, False
        except json.JSONDecodeError:
            pass

    start = stripped.find("{")
    if start < 0:
        raise ValueError("no JSON object start found")

    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(stripped)):
        ch = stripped[idx]
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
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : idx + 1], True
    raise ValueError("no balanced JSON object found")


def parse_candidate(content: str, metrics: RepairMetrics) -> dict[str, Any]:
    json_text, salvaged = extract_balanced_json(content)
    metrics.balanced_json_salvage = salvaged
    candidate = json.loads(json_text)
    metrics.raw_json_parse = True
    if not isinstance(candidate, dict):
        raise ValueError("top-level JSON is not an object")
    return candidate


def direct_system_prompt() -> str:
    return (
        "You are a deterministic Polymath extraction engine. "
        "Extract only claims explicitly supported by the document. "
        "Return exactly one JSON object matching the Polymath contract. "
        "Do not output markdown, prose, or code fences."
    )


def direct_user_prompt(chunk: str, *, chunk_id: str, doc_id: str, corpus_id: str, use_wrapper: bool) -> str:
    output_shape = {
        "entities": [
            {
                "canonical_name": "lowercase canonical entity name",
                "surface_form": "verbatim text span",
                "entity_type": "Person|Organization|Location|Event|Concept|Method|Product|Software|Document|Standard|Rule|Law|Artifact|TimeReference|other",
                "confidence": 0.91,
                "query_aliases": [],
                "definitional_phrase": "",
                "object_kind": "",
            }
        ],
        "relations": [
            {
                "subject": "canonical source entity",
                "predicate": "one allowed predicate only",
                "object": "canonical target entity or literal",
                "object_kind": "entity",
                "confidence": 0.88,
                "evidence_phrase": "short exact source phrase",
                "relation_cue": "raw text verb or phrase",
            }
        ],
        "facts": [
            {
                "subject": "canonical entity already listed",
                "fact_type": "property|status|timestamp|quantity|threshold|category|tag|rule_condition|rule_action",
                "property_name": "snake_case",
                "value": "verbatim or normalized value",
                "unit": "",
                "condition": "",
                "confidence": 0.86,
                "evidence_phrase": "short exact source phrase",
            }
        ],
    }
    relation_list = "|".join(sorted(PREDICATES))
    entity_list = "|".join(sorted(ENTITY_TYPES))
    fact_list = "|".join(sorted(FACT_TYPES))
    body = f"""<task>
Extract entities, relations, and facts for Polymath graph ingestion.
XML tags in this prompt are delimiters only. They are not ontology.
The only ontology is the listed Polymath schema.

chunk_id={chunk_id}
doc_id={doc_id}
corpus_id={corpus_id}

Entity types: {entity_list}
Predicates: {relation_list}
Fact types: {fact_list}

Rules:
- Output exactly one RFC8259 JSON object.
- Do not output arrays as the top level.
- Do not output markdown or explanations.
- created_by direction is created thing -> creator.
- works_for direction is person -> organization.
- located_in direction is contained thing/location -> containing location.
- Put raw verbs such as founded, designed, run by, supervised, paid, detected in relation_cue.
- The predicate field must be one allowed predicate only.
- relation.object_kind is only "entity" or "literal".
- relation.object_kind is "entity" only if the relation object is an entity endpoint.
- Every entity relation endpoint must appear in entities, unless the object_kind is literal.
- Evidence phrases must be short exact substrings from the document.
- Confidence must be 0.72 to 0.99 for extracted items.
- Prefer facts for dates, measurements, thresholds, dollar values, status, conditions, and percentages.

Required shape:
{json.dumps(output_shape, ensure_ascii=False)}
</task>

<document>
{chunk}
</document>
"""
    if use_wrapper:
        body += "\nReturn only:\n<json_payload>{...}</json_payload>\n"
    else:
        body += "\nReturn only the JSON object.\n"
    return body


def ling_system_prompt() -> str:
    return (
        "You are a deterministic information extraction transducer. "
        "Output exactly one RFC8259 JSON object. No markdown. No XML in output."
    )


def ling_user_prompt(chunk: str, *, chunk_id: str, doc_id: str, corpus_id: str) -> str:
    return f"""<task>
Extract a compact intermediate representation for Polymath.
Use one contract only:
{{
  "entities": [],
  "frames": [],
  "facts": []
}}

chunk_id={chunk_id}
doc_id={doc_id}
corpus_id={corpus_id}

Rules:
- XML tags are delimiters only.
- For tables, each row is one local extraction unit.
- Do not enumerate outside the selected rows.
- Confidence must be 0.72-0.99.
- Max entities <= 14.
- Max frames <= 12.
- Max facts <= 4.
- Use facts for table cells, prompt-use notes, quantities, statuses, and constraints.
</task>

<document>
{chunk}
</document>
"""


def build_messages(profile: ModelProfile, chunk: str, *, chunk_id: str, doc_id: str, corpus_id: str) -> list[dict[str, str]]:
    if profile.mode == "compact_ir":
        return [
            {"role": "system", "content": ling_system_prompt()},
            {"role": "user", "content": ling_user_prompt(chunk, chunk_id=chunk_id, doc_id=doc_id, corpus_id=corpus_id)},
        ]
    return [
        {"role": "system", "content": direct_system_prompt()},
        {
            "role": "user",
            "content": direct_user_prompt(
                chunk,
                chunk_id=chunk_id,
                doc_id=doc_id,
                corpus_id=corpus_id,
                use_wrapper=profile.mode != "json_schema",
            ),
        },
    ]


def build_payload(profile: ModelProfile, messages: list[dict[str, str]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": profile.model_id,
        "messages": messages,
        "temperature": 0,
        "top_p": 0.1,
        "max_tokens": profile.max_output_tokens,
        "stream": False,
    }
    if profile.requires_thinking_disabled:
        payload["thinking"] = {"type": "disabled"}
    if profile.mode == "json_schema":
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "polymath_extraction",
                "strict": True,
                "schema": extraction_response_json_schema(),
            },
        }
        payload["provider"] = {"require_parameters": True}
    return payload


def chat_url(profile: ModelProfile) -> str:
    return f"{profile.base_url.rstrip('/')}/chat/completions"


def message_content(response_json: dict[str, Any]) -> str:
    message = ((response_json.get("choices") or [{}])[0].get("message") or {})
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content or "")


def finish_reason(response_json: dict[str, Any]) -> str | None:
    choices = response_json.get("choices") or []
    if not choices:
        return None
    return choices[0].get("finish_reason")


def score_result(metrics: RepairMetrics, finish: str | None) -> int:
    score = 100
    if not metrics.raw_json_parse:
        score -= 50
    if metrics.balanced_json_salvage:
        score -= 20
    if not metrics.candidate_pydantic:
        score -= 15
    if not metrics.compiled_pydantic:
        score -= 60
    score -= min(30, 10 * metrics.predicate_remap_count)
    score -= min(24, 8 * metrics.object_kind_repair_count)
    score -= min(24, 8 * metrics.endpoint_completion_count)
    score -= min(50, 25 * metrics.semantic_repair_count)
    if finish == "length":
        score -= 50
    return max(0, min(110, score))


def process_content(profile: ModelProfile, content: str, metrics: RepairMetrics) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate = parse_candidate(content, metrics)
    candidate_contract = candidate
    if profile.mode == "compact_ir":
        candidate_contract = compile_ling_ir(candidate, metrics)
    metrics.candidate_pydantic, _ = validate_contract(strip_to_contract(candidate_contract, RepairMetrics()))
    compiled = strip_to_contract(candidate_contract, metrics)
    semantic_repairs(compiled, metrics)
    complete_endpoints(compiled, metrics)
    metrics.compiled_pydantic, metrics.error = validate_contract(compiled)
    return candidate, compiled


def run_profile(
    profile: ModelProfile,
    chunk: str,
    *,
    chunk_id: str,
    doc_id: str,
    corpus_id: str,
    audit_dir: Path,
    dry_run: bool,
    timeout_s: float,
) -> RunResult:
    stamp = utc_stamp()
    audit_prefix = f"{stamp}_{profile.name}_{chunk_id}"
    messages = build_messages(profile, chunk, chunk_id=chunk_id, doc_id=doc_id, corpus_id=corpus_id)
    payload = build_payload(profile, messages)
    safe_request = {
        "url": chat_url(profile),
        "profile": asdict(profile),
        "payload": payload,
    }
    write_json(audit_dir / f"{audit_prefix}_request.json", safe_request)

    if dry_run:
        return RunResult(
            profile=profile.name,
            provider=profile.provider,
            model=profile.model_id,
            chunk_id=chunk_id,
            ok=True,
            accepted=False,
            acceptance_score=0,
            elapsed_s=0.0,
            finish_reason=None,
            usage={},
            counts={"entities": 0, "relations": 0, "facts": 0},
            repairs=RepairMetrics(),
            audit_prefix=audit_prefix,
            error="dry_run",
        )

    api_key = os.getenv(profile.api_key_env, "").strip()
    if not api_key:
        return RunResult(
            profile=profile.name,
            provider=profile.provider,
            model=profile.model_id,
            chunk_id=chunk_id,
            ok=False,
            accepted=False,
            acceptance_score=0,
            elapsed_s=0.0,
            finish_reason=None,
            usage={},
            counts={"entities": 0, "relations": 0, "facts": 0},
            repairs=RepairMetrics(error=f"missing environment variable {profile.api_key_env}"),
            audit_prefix=audit_prefix,
            error=f"missing environment variable {profile.api_key_env}",
        )

    started = time.perf_counter()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    metrics = RepairMetrics()
    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.post(chat_url(profile), json=payload, headers=headers)
        elapsed = round(time.perf_counter() - started, 3)
        response_json = response.json()
        write_json(audit_dir / f"{audit_prefix}_provider_response.json", response_json)
        response.raise_for_status()
        content = message_content(response_json)
        write_text(audit_dir / f"{audit_prefix}_raw_content.txt", content)
        candidate, compiled = process_content(profile, content, metrics)
        write_json(audit_dir / f"{audit_prefix}_candidate.json", candidate)
        write_json(audit_dir / f"{audit_prefix}_compiled.json", compiled)
        finish = finish_reason(response_json)
        score = score_result(metrics, finish)
        counts = {
            "entities": len(compiled.get("entities", [])),
            "relations": len(compiled.get("relations", [])),
            "facts": len(compiled.get("facts", [])),
        }
        accepted = metrics.compiled_pydantic and finish != "length" and score >= 70
        result = RunResult(
            profile=profile.name,
            provider=profile.provider,
            model=profile.model_id,
            chunk_id=chunk_id,
            ok=metrics.compiled_pydantic,
            accepted=accepted,
            acceptance_score=score,
            elapsed_s=elapsed,
            finish_reason=finish,
            usage=response_json.get("usage") or {},
            counts=counts,
            repairs=metrics,
            audit_prefix=audit_prefix,
            error=metrics.error,
        )
        write_json(audit_dir / f"{audit_prefix}_metrics.json", asdict(result))
        return result
    except Exception as exc:
        elapsed = round(time.perf_counter() - started, 3)
        metrics.error = str(exc)
        result = RunResult(
            profile=profile.name,
            provider=profile.provider,
            model=profile.model_id,
            chunk_id=chunk_id,
            ok=False,
            accepted=False,
            acceptance_score=0,
            elapsed_s=elapsed,
            finish_reason=None,
            usage={},
            counts={"entities": 0, "relations": 0, "facts": 0},
            repairs=metrics,
            audit_prefix=audit_prefix,
            error=str(exc),
        )
        write_json(audit_dir / f"{audit_prefix}_metrics.json", asdict(result))
        return result


def split_text(text: str, max_chars: int) -> list[str]:
    clean = text.strip()
    if len(clean) <= max_chars:
        return [clean]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in re.split(r"\n\s*\n", clean):
        para = para.strip()
        if not para:
            continue
        if current and current_len + len(para) + 2 > max_chars:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = len(para)
        elif len(para) > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            for i in range(0, len(para), max_chars):
                chunks.append(para[i : i + max_chars])
        else:
            current.append(para)
            current_len += len(para) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Polymath extraction model router scaffold.")
    parser.add_argument("--input", required=True, help="Path to a text/markdown document chunk or full document.")
    parser.add_argument("--profiles", nargs="+", default=["longcat_2_direct", "hy3_preview_direct", "hy3_direct", "mistral_nemo_schema"])
    parser.add_argument("--audit-dir", default=".codex-logs/extraction-router", help="Directory for request/response/compiled artifacts.")
    parser.add_argument("--doc-id", default="manual-doc")
    parser.add_argument("--corpus-id", default="manual-corpus")
    parser.add_argument("--chunk-prefix", default="chunk")
    parser.add_argument("--max-chunks", type=int, default=1, help="Limit chunks processed from the input document.")
    parser.add_argument("--max-chars", type=int, default=0, help="Override chunk size. Default uses the smallest selected profile limit.")
    parser.add_argument("--timeout-s", type=float, default=90.0)
    parser.add_argument("--dry-run", action="store_true", help="Write request artifacts but do not call providers.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected: list[ModelProfile] = []
    for name in args.profiles:
        if name not in MODEL_PROFILES:
            print(f"Unknown profile: {name}", file=sys.stderr)
            print("Known profiles: " + ", ".join(sorted(MODEL_PROFILES)), file=sys.stderr)
            return 2
        selected.append(MODEL_PROFILES[name])
    selected.sort(key=lambda p: p.priority)

    text = Path(args.input).read_text(encoding="utf-8")
    max_chars = args.max_chars or min(profile.max_chunk_chars for profile in selected)
    chunks = split_text(text, max_chars)[: max(1, args.max_chunks)]
    audit_dir = Path(args.audit_dir)

    all_results: list[RunResult] = []
    for chunk_index, chunk in enumerate(chunks, start=1):
        chunk_id = f"{args.chunk_prefix}-{chunk_index:03d}"
        for profile in selected:
            result = run_profile(
                profile,
                chunk,
                chunk_id=chunk_id,
                doc_id=args.doc_id,
                corpus_id=args.corpus_id,
                audit_dir=audit_dir,
                dry_run=args.dry_run,
                timeout_s=args.timeout_s,
            )
            all_results.append(result)
            status = "ACCEPT" if result.accepted else "REJECT"
            print(
                f"{status} {result.profile} chunk={chunk_id} "
                f"score={result.acceptance_score} ok={result.ok} "
                f"counts={result.counts} error={result.error[:120]}"
            )

    summary = {
        "created_at": utc_stamp(),
        "input": str(Path(args.input).resolve()),
        "profiles": [profile.name for profile in selected],
        "chunk_count": len(chunks),
        "dry_run": bool(args.dry_run),
        "results": [asdict(result) for result in all_results],
    }
    write_json(audit_dir / f"{utc_stamp()}_router_summary.json", summary)
    return 0 if any(result.accepted for result in all_results) or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
