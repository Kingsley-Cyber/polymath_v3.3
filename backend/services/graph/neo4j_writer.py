"""
Neo4j writer — persists graph data after GHOST B extraction (Phase 4).

All upserts use MERGE (idempotent).
All Document/Chunk MATCH clauses scope by corpus_id to prevent cross-corpus bleed.

Entity nodes are global by design — cross-corpus dedup via deterministic ID.

Phase 14.4: entity_id format is now `entity:{name_slug}`. Ghost B's
entity_type is preserved as extraction evidence (`observed_entity_types` on
the node, `extracted_type` on MENTIONS) instead of fragmenting identity.

Entry point: write_document_graph() — call after GHOST B returns ExtractionResult list.
"""

import logging
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from neo4j import AsyncDriver

from services.ghost_b import (
    EntityItem,
    ExtractionResult,
    RelationItem,
    SchemaContext,
    UNIVERSAL_RELATION_SCHEMA,
    normalize_relation_predicate_alias,
)
from services.graph.entity_quality import quality_payload
from services.ontology import (
    object_kind_compatible,
    ontology_version,
    relation_family_map,
)

logger = logging.getLogger(__name__)
ALIAS_MAP_PATH = Path(__file__).with_name("entity_aliases.json")
FACET_TAXONOMY_PATH = Path(__file__).with_name("facet_taxonomy.json")
DOMAIN_TAXONOMY_PATH = Path(__file__).with_name("domain_taxonomy.json")
CANONICAL_FAMILIES_PATH = Path(__file__).with_name("canonical_families.json")
ENTITY_TYPE_OVERRIDES_PATH = Path(__file__).with_name("entity_type_overrides.json")
ONTOLOGY_VERSION = ontology_version()
ENTITY_ID_PREFIX = "entity"
ENTITY_TYPE_PRIORITY = [
    "Person",
    "Organization",
    "Location",
    "Event",
    "Document",
    "Rule",
    "Law",
    "Product",
    "Artifact",
    "Method",
    "Concept",
    "TimeReference",
    SchemaContext.ENTITY_SENTINEL,
]
RELATION_FAMILY_MAP = relation_family_map()
_APPROVED_SPECIFIC_RELATIONS = {
    value for value in UNIVERSAL_RELATION_SCHEMA if value != SchemaContext.RELATION_SENTINEL
}
_OPERATIONAL_SUBJECT_DOMAINS = {
    "Feature",
    "Module",
    "Screen",
    "Product",
    "ArchitectureDecision",
    "AIModel",
    "CloudService",
    "Database",
    "MobileApp",
}
_OPERATIONAL_OBJECT_DOMAINS = {
    "AIModel",
    "CloudService",
    "DataObject",
    "Database",
    "Dataset",
    "Device",
    "Platform",
}
_OPERATIONAL_OBJECT_KINDS = {
    "App",
    "DataObject",
    "Dataset",
    "Framework",
    "Library",
    "Model",
    "Service",
    "Tool",
    "API",
    "Database",
}
_OUTPUT_OBJECT_DOMAINS = {"OutputArtifact"}
_OUTPUT_OBJECT_KINDS = {"Book", "Report", "Spec", "Whitepaper"}
_CONSTRAINT_OBJECT_DOMAINS = {"Constraint", "Risk", "PricingRule"}
_PRODUCTION_HINTS = (
    "generation",
    "generator",
    "synthesis",
    "pipeline",
    "engine",
    "export",
    "output",
    "producer",
)
_RELATION_CUE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("trained_on", ("trained on", "trained with", "training data", "training set", "learns from")),
    ("runs_on", ("runs on", "run on", "executes on", "deployed on", "on-device", "on device")),
    ("stores", ("stores", "stored in", "persists", "persisted in", "saves to", "saved to")),
    ("extracts", ("extracts", "extract ", "extracted from", "feature extraction", "entity extraction", "pulls from")),
    ("detects", ("detects", "identifies", "recognizes", "finds", "object detection")),
    ("classifies", ("classifies", "classification", "predicts", "assigns category", "labels as")),
    ("calls", ("calls", "invokes", "requests", "queries", "api call", "endpoint")),
    ("maps_to", ("maps to", "maps onto", "converts", "transforms", "translates")),
    ("represents", ("represents", "models", "modeled as", "encodes")),
    ("supports", ("supports", "enables", "allows", "provides", "facilitates")),
    ("produces", ("produces", "generates", "outputs", "emits", "returns", "creates")),
    ("depends_on", ("depends on", "requires", "prerequisite", "constraint", "needs")),
    ("uses", ("uses", "using", "utilizes", "consumes", "powered by")),
    ("implements", ("implements", "realizes", "concrete form")),
    ("references", ("references", "cites", "mentions", "according to", "described in")),
    ("derived_from", ("derived from", "based on", "adapted from", "inspired by", "built on")),
    ("measures", ("measures", "measured by", "quantifies")),
    ("tests", ("tests", "validates", "falsifies")),
    ("applied_to", ("applied to", "applies to", "performed on")),
    ("defined_in", ("defined in", "specified in", "introduced in")),
    ("illustrated_in", ("illustrated in", "depicted in", "shown in", "demonstrated in")),
    ("follows_distribution", ("follows a", "drawn from", "distributed as", "distributed according to")),
    ("parameter_of", ("parameter of", "parameters of", "threshold of", "setting of")),
    ("equivalent_to", ("equivalent to", "same as", "also called", "referred to as")),
    ("activates", ("activates", "activated", "stimulates", "stimulated")),
    ("experiences", ("experiences", "experienced", "undergoes", "feels", "felt")),
    ("imagines", ("imagines", "imagined", "visualizes", "visualises", "pictures", "envisions", "anticipates")),
    ("studies", ("studies", "researches", "investigates", "examines")),
    ("embodies", ("embodies", "personifies")),
    ("symbolizes", ("symbolizes", "symbolises", "stands for", "signifies")),
    ("influences", ("influences", "shapes", "affects", "pressures")),
    ("motivates", ("motivates", "motivated by", "drives", "driven by")),
    ("struggles_with", ("struggles with", "wrestles with", "conflicted by")),
    ("reinforces", ("reinforces", "strengthens", "normalizes", "intensifies")),
    ("undermines", ("undermines", "weakens", "erodes", "destabilizes", "subverts")),
    ("frames_as", ("frames as", "presents as", "casts as", "positions as")),
    ("conceals", ("conceals", "hides", "masks", "disguises", "withholds")),
    ("leverages", ("leverages", "exploits", "uses strategically")),
    ("causes", ("causes", "leads to", "results in", "because of")),
    ("preceded_by", ("preceded by", "after", "followed by")),
    ("contradicts", ("contradicts", "conflicts with", "inconsistent with", "opposes")),
    ("excepts", ("except", "unless", "excluding", "exception")),
    ("overrides", ("overrides", "replaces", "supersedes", "deprecated by")),
)
_RECOVERABLE_SOURCE_PREDICATES = {
    "part_of",
    "member_of",
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
    "measures",
    "defined_in",
    "follows_distribution",
    "tests",
    "applied_to",
    "illustrated_in",
    "parameter_of",
    "equivalent_to",
    "activates",
    "experiences",
    "imagines",
    "studies",
    "embodies",
    "symbolizes",
    "influences",
    "motivates",
    "struggles_with",
    "reinforces",
    "undermines",
    "frames_as",
    "conceals",
    "leverages",
    "preceded_by",
    "causes",
    "derived_from",
    "contradicts",
    "excepts",
    "overrides",
}

_VAGUE_RELATION_EVIDENCE_RE = re.compile(
    r"\b("
    r"co-?occurs?|co-?occurrence|see\s+also|related\s+to|associated\s+with|"
    r"similar(?:ity)?|comparable|comparison|compared\s+to|like|resembles?|"
    r"analog(?:y|ous)|parallel(?:s)?"
    r")\b"
)
_MEASURE_EVALUATION_RE = re.compile(
    r"\b(evaluates?|scores?|estimates?)\b.*\b("
    r"traits?|values?|scores?|quantit(?:y|ies)|metrics?|ratings?|measures?|"
    r"estimates?|parameters?|coefficients?|probabilit(?:y|ies)|latent\s+traits?"
    r")\b"
)
_TEST_EVALUATION_RE = re.compile(
    r"\b(evaluates?|checks?)\b.*\b("
    r"conditions?|assumptions?|hypotheses|hypothesis|constraints?|qualities|"
    r"whether|model\s+fit|invariance"
    r")\b"
)
_DEFINED_IN_EVIDENCE_RE = re.compile(
    r"\b(defined|specified|introduced|stated)\s+in\b"
)
_EXPERIENCE_EVIDENCE_RE = re.compile(
    r"\bexpress(?:es|ed|ing)?\s+("
    r"relief|freedom|fear|joy|shame|guilt|anger|sadness|grief|pain|"
    r"anxiety|emotion|feeling|loss|love"
    r")\b"
)


def relation_family_for_predicate(predicate: str | None) -> str:
    """Return the stable relation family for a raw extraction predicate.

    The raw predicate is still stored on the edge. The family is a deterministic
    grouping used by graph retrieval and Mission Control to distinguish strong
    operational/structural/referential edges from weak catch-all associations.
    """
    normalized = str(predicate or "").strip()
    return RELATION_FAMILY_MAP.get(normalized, "WeakAssociation")


def _identity_value(identity: dict | None, key: str) -> str:
    if not identity:
        return ""
    return str(identity.get(key) or "").strip()


def _identity_text(identity: dict | None) -> str:
    if not identity:
        return ""
    parts = [
        identity.get("canonical_name"),
        identity.get("display_name"),
        identity.get("primary_entity_type"),
        identity.get("object_kind"),
        identity.get("domain_type"),
        identity.get("canonical_family"),
    ]
    return " ".join(str(p).lower() for p in parts if p)


def _predicate_from_evidence(*parts: str | None) -> str | None:
    text = " ".join(str(part or "").lower() for part in parts if part)
    if not text:
        return None
    if _evidence_is_vague_association(text):
        return None
    if _TEST_EVALUATION_RE.search(text):
        return "tests"
    if _MEASURE_EVALUATION_RE.search(text):
        return "measures"
    if _DEFINED_IN_EVIDENCE_RE.search(text):
        return "defined_in"
    if _EXPERIENCE_EVIDENCE_RE.search(text):
        return "experiences"
    for predicate, cues in _RELATION_CUE_PATTERNS:
        if any(cue in text for cue in cues):
            return predicate
    return None


def _evidence_is_vague_association(evidence: str | None) -> bool:
    return bool(_VAGUE_RELATION_EVIDENCE_RE.search(str(evidence or "").lower()))


def _is_low_predicate_confidence(
    validation_status: str | None,
    predicate_confidence: float | None,
) -> bool:
    if "low_predicate_confidence" in str(validation_status or ""):
        return True
    if predicate_confidence is None:
        return False
    try:
        return float(predicate_confidence) < 0.60
    except (TypeError, ValueError):
        return False


def _identity_domain_kind_type(identity: dict | None) -> tuple[str, str, str]:
    return (
        _identity_value(identity, "domain_type"),
        _identity_value(identity, "object_kind"),
        _identity_value(identity, "primary_entity_type"),
    )


def _has_any(value: str, candidates: set[str]) -> bool:
    return bool(value and value in candidates)


def _relation_compatible_with_facets(
    predicate: str,
    subject_identity: dict | None,
    object_identity: dict | None,
) -> bool:
    """Cheap ontology-aware compatibility for repairing soft-remapped edges.

    This is deliberately looser than Ghost B's broad entity-type domain/range
    map because the writer has richer facets. It is still conservative enough
    to avoid turning arbitrary `related_to` edges into confident facts.
    """
    if predicate not in _APPROVED_SPECIFIC_RELATIONS:
        return False
    if not subject_identity or not object_identity:
        return False
    object_kind_rule = object_kind_compatible(
        predicate, subject_identity, object_identity
    )
    if object_kind_rule is False:
        return False

    subject_domain, subject_kind, subject_type = _identity_domain_kind_type(subject_identity)
    object_domain, object_kind, object_type = _identity_domain_kind_type(object_identity)
    operational_subject = (
        _has_any(subject_domain, _OPERATIONAL_SUBJECT_DOMAINS)
        or subject_type in {"Artifact", "Method", "Organization", "Product"}
        or subject_kind in {"App", "Library", "Service", "Tool", "API", "Framework"}
    )
    operational_object = (
        _has_any(object_domain, _OPERATIONAL_OBJECT_DOMAINS)
        or _has_any(object_kind, _OPERATIONAL_OBJECT_KINDS)
        or object_type in {"Artifact", "Method", "Product"}
    )

    if predicate in {"uses", "calls", "supports"}:
        return operational_subject and operational_object
    if predicate == "runs_on":
        return operational_subject and (
            object_domain in {"Device", "Platform", "CloudService"}
            or object_kind in {"Device", "Platform", "Service", "Framework"}
            or object_type in {"Artifact", "Product", "Organization", "Location"}
        )
    if predicate == "trained_on":
        return operational_subject and (
            object_domain in {"Dataset", "DataObject"}
            or object_kind in {"Dataset", "DataObject"}
            or object_type in {"Artifact", "Concept", "Document", "Product"}
        )
    if predicate == "stores":
        return operational_subject and (
            object_domain in {"DataObject", "Dataset", "OutputArtifact"}
            or object_kind in {"DataObject", "Dataset", "Document", "Report", "Spec"}
            or object_type in {"Artifact", "Concept", "Document", "Product"}
        )
    if predicate in {"extracts", "detects", "classifies"}:
        return operational_subject and object_type in {
            "Artifact", "Concept", "Document", "Event", "Location",
            "Organization", "Person", "Product",
        }
    if predicate == "produces":
        return operational_subject and (
            object_domain in _OUTPUT_OBJECT_DOMAINS | {"DataObject", "Dataset"}
            or object_kind in _OUTPUT_OBJECT_KINDS | {"Dataset", "DataObject"}
            or object_type in {"Artifact", "Concept", "Document", "Event", "Method", "Product"}
        )
    if predicate == "depends_on":
        return object_domain in _CONSTRAINT_OBJECT_DOMAINS or object_type in {
            "Artifact", "Concept", "Document", "Law", "Method", "Product", "Rule"
        }
    if predicate == "implements":
        return operational_subject and object_type in {"Concept", "Method", "Rule", "Law"}
    if predicate in {"references", "derived_from", "represents", "maps_to"}:
        return object_type in {
            "Artifact", "Concept", "Document", "Event", "Method",
            "Organization", "Person", "Product", "Rule", "Law",
        }
    if predicate in {"measures", "tests", "applied_to", "follows_distribution", "studies"}:
        return subject_type in {
            "Artifact", "Concept", "Document", "Method", "Organization", "Person", "Product",
        } and object_type in {
            "Artifact", "Concept", "Document", "Event", "Law", "Method",
            "Organization", "Person", "Product", "Rule",
        }
    if predicate in {"defined_in", "illustrated_in", "equivalent_to"}:
        return object_type in {
            "Artifact", "Concept", "Document", "Event", "Law", "Method",
            "Organization", "Person", "Product", "Rule",
        }
    if predicate == "parameter_of":
        return subject_type in {"Artifact", "Concept", "Rule"} and object_type in {
            "Artifact", "Concept", "Method", "Product",
        }
    if predicate in {
        "activates", "embodies", "experiences", "imagines", "symbolizes",
        "influences", "motivates", "struggles_with", "reinforces",
        "undermines", "frames_as", "conceals", "leverages",
    }:
        return subject_type in {
            "Artifact", "Concept", "Document", "Event", "Law", "Method",
            "Organization", "Person", "Product", "Rule",
        } and object_type in {
            "Artifact", "Concept", "Document", "Event", "Law", "Method",
            "Organization", "Person", "Product", "Rule",
        }
    if predicate in {"part_of", "member_of", "created_by", "works_for", "located_in"}:
        return True
    if predicate in {"causes", "preceded_by", "contradicts", "excepts", "overrides"}:
        return object_type in {"Concept", "Document", "Event", "Law", "Method", "Rule", "TimeReference"}
    return False


def _recover_source_predicate_with_evidence(
    predicate: str,
    subject_identity: dict | None,
    object_identity: dict | None,
    evidence_phrase: str | None,
) -> bool:
    """Allow high-confidence LLM source intent to survive broad type mismatch.

    Ghost B's broad entity labels are intentionally coarse. A Product/Concept
    mismatch often fails the initial domain/range map even when the evidence
    phrase is clear. We recover only non-identity predicates here; strict
    identity predicates such as `works_for`/`created_by` still need proper
    facets or direction repair.
    """
    if predicate not in _RECOVERABLE_SOURCE_PREDICATES:
        return False
    if not subject_identity or not object_identity:
        return False
    evidence = str(evidence_phrase or "").strip()
    if not evidence:
        return False
    if _evidence_is_vague_association(evidence):
        return False
    if _predicate_from_evidence(evidence) != predicate:
        return False
    return True


def refine_related_to_predicate(
    predicate: str,
    subject_identity: dict | None,
    object_identity: dict | None,
    *,
    source_predicate: str | None = None,
    evidence_phrase: str | None = None,
    relation_cue: str | None = None,
    validation_status: str | None = None,
    predicate_confidence: float | None = None,
) -> str:
    """Conservatively refine a weak `related_to` edge using ontology facets.

    Category B justification: Ghost B is intentionally recall-friendly and
    domain/range validation remaps uncertain relations to `related_to`. The
    writer may recover a narrower predicate only when the evidence phrase or
    relation cue explicitly supports it and deterministic facets do not
    contradict it. Facets alone are not enough to promote a weak association.
    """
    if predicate != SchemaContext.RELATION_SENTINEL:
        return predicate
    if not subject_identity or not object_identity:
        return predicate
    if _is_low_predicate_confidence(validation_status, predicate_confidence):
        return predicate

    evidence = " ".join(
        str(part or "").strip()
        for part in (evidence_phrase, relation_cue)
        if str(part or "").strip()
    )
    if not evidence or _evidence_is_vague_association(evidence):
        return predicate

    evidence_predicate = _predicate_from_evidence(evidence)
    if evidence_predicate and _relation_compatible_with_facets(
        evidence_predicate, subject_identity, object_identity
    ):
        return evidence_predicate

    original_predicate = str(source_predicate or "").strip()
    original_predicate, _ = normalize_relation_predicate_alias(original_predicate)
    if (
        original_predicate in _APPROVED_SPECIFIC_RELATIONS
        and _relation_compatible_with_facets(original_predicate, subject_identity, object_identity)
        and _predicate_from_evidence(evidence) == original_predicate
    ):
        return original_predicate
    if _recover_source_predicate_with_evidence(
        original_predicate,
        subject_identity,
        object_identity,
        evidence,
    ):
        return original_predicate

    return predicate


def relation_edge_strength(
    predicate: str,
    confidence: float,
    validation_status: str | None = None,
    *,
    predicate_refined: bool = False,
) -> str:
    """Classify relation reliability for Mission Control and future filters."""
    status = str(validation_status or "")
    if predicate == SchemaContext.RELATION_SENTINEL:
        return "weak"
    if "review_required" in status:
        return "thin"
    if "domain_range_warn" in status:
        return "thin"
    if predicate_refined:
        return "repaired"
    if "evidence_cue_repair" in status:
        return "repaired"
    if validation_status in {"domain_range_mismatch", "schema_predicate_remap"}:
        return "thin"
    if confidence < 0.6:
        return "thin"
    return "strong"


def _append_status(existing: str | None, status: str) -> str:
    if not existing:
        return status
    parts = [part for part in str(existing).split("+") if part]
    if status not in parts:
        parts.append(status)
    return "+".join(parts)


def relation_eligible_for_synthesis(
    predicate: str,
    confidence: float,
    validation_status: str | None = None,
) -> bool:
    """Keep weak catchall edges available in the graph but out of strong synthesis."""
    if predicate == SchemaContext.RELATION_SENTINEL or confidence < 0.55:
        return False
    if "domain_range_warn" in str(validation_status or ""):
        return confidence >= 0.75
    return True


def normalize_entity_name(name: str) -> str:
    """Canonical form for dedup: lowercase, NFKD, strip punctuation, collapse spaces."""
    name = name.lower().strip()
    name = unicodedata.normalize("NFKD", name)
    name = re.sub(r"[^\w\s]", "", name)
    return re.sub(r"\s+", " ", name).strip()


@lru_cache(maxsize=1)
def _load_alias_lookup() -> dict[str, str]:
    try:
        data = json.loads(ALIAS_MAP_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Entity alias map failed to load: %s", exc)
        return {}

    lookup: dict[str, str] = {}
    for canonical, aliases in data.items():
        canonical_norm = normalize_entity_name(canonical)
        if not canonical_norm:
            continue
        lookup[canonical_norm] = canonical_norm
        for alias in aliases or []:
            alias_norm = normalize_entity_name(str(alias))
            if alias_norm:
                lookup[alias_norm] = canonical_norm
    return lookup


def resolve_entity_alias(normalized_name: str) -> str:
    """Return the configured canonical alias for an already-normalized name."""
    return _load_alias_lookup().get(normalized_name, normalized_name)


def canonicalize_entity_name(name: str) -> str:
    return resolve_entity_alias(normalize_entity_name(name))


@lru_cache(maxsize=1)
def _load_entity_type_overrides() -> dict[str, str]:
    try:
        data = json.loads(ENTITY_TYPE_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Entity type overrides failed to load: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}

    overrides: dict[str, str] = {}
    for name, value in data.items():
        canonical = canonicalize_entity_name(str(name))
        if not canonical:
            continue
        if isinstance(value, str):
            overrides[canonical] = value
        elif isinstance(value, dict) and value.get("primary_entity_type"):
            overrides[canonical] = str(value["primary_entity_type"])
    return overrides


def resolve_primary_entity_type(
    canonical_name: str,
    observed_types: list[str],
) -> str:
    """Pick the stable node type for one canonical entity.

    Ghost B's type is evidence from a chunk, not global identity. This resolver
    collapses split nodes such as Product:pvector + Method:pvector into one
    entity:pvector node while preserving all observed types on the node and
    MENTIONS edge.
    """
    canonical = canonicalize_entity_name(canonical_name)
    override = _load_entity_type_overrides().get(canonical)
    if override:
        return override
    observed = {t for t in observed_types if t}
    for candidate in ENTITY_TYPE_PRIORITY:
        if candidate in observed:
            return candidate
    return "Concept"


@lru_cache(maxsize=1)
def _load_facet_taxonomy() -> dict:
    try:
        data = json.loads(FACET_TAXONOMY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Facet taxonomy failed to load: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=1)
def _load_domain_taxonomy() -> dict:
    try:
        data = json.loads(DOMAIN_TAXONOMY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Domain taxonomy failed to load: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=1)
def _load_canonical_families() -> dict:
    try:
        data = json.loads(CANONICAL_FAMILIES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Canonical family map failed to load: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _norm_contains(haystack: str, needle: str) -> bool:
    needle_norm = normalize_entity_name(needle)
    if not needle_norm:
        return False
    return re.search(rf"\b{re.escape(needle_norm)}\b", haystack) is not None


def resolve_canonical_family(
    entity_name: str,
    text_context: str = "",
) -> str | None:
    """Resolve a stable cross-type family label from curated aliases.

    Families are deliberately small and auditable. They group entities such as
    Box2D/PBox2D/JBox2D under `physics_simulation` without asking Ghost B to
    produce additional ontology fields.
    """
    families = _load_canonical_families()
    if not families:
        return None
    raw_name = str(entity_name or "")
    name_norm = canonicalize_entity_name(raw_name).replace("_", " ")
    context_norm = normalize_entity_name(text_context)
    haystack = " ".join(part for part in (name_norm, context_norm) if part)
    if not haystack:
        return None

    for family, spec in families.items():
        if not isinstance(spec, dict):
            continue
        for term in [*(spec.get("members") or []), *(spec.get("synonyms") or [])]:
            term_norm = canonicalize_entity_name(str(term)).replace("_", " ")
            if term_norm and (name_norm == term_norm or _norm_contains(haystack, term_norm)):
                return str(family)
    return None


def resolve_facets(
    entity_name: str,
    entity_type: str,
    text_context: str = "",
) -> dict[str, str]:
    """Infer lightweight ontology facets for an extracted entity.

    This is the first soft-ontology layer: keep Ghost B's broad entity_type,
    then add an optional object_kind hierarchy for concrete objects such as
    libraries, apps, reports, datasets, and books. It is deterministic and
    ingestion-time only, so query latency stays bounded.
    """
    taxonomy = _load_facet_taxonomy()
    type_taxonomy = taxonomy.get(entity_type)
    if not isinstance(type_taxonomy, dict):
        return {}

    raw_name = str(entity_name or "")
    raw_lower = raw_name.lower().strip()
    name_norm = canonicalize_entity_name(raw_name)
    name_match = name_norm.replace("_", " ")
    context_norm = normalize_entity_name(text_context)
    haystack = " ".join(part for part in (name_match, context_norm) if part)

    def _facet(kind: str, spec: dict) -> dict[str, str]:
        parent = str(spec.get("parent") or entity_type)
        return {
            "object_kind": kind,
            "object_kind_parent": parent,
            "object_kind_root": entity_type,
        }

    # Exact known names first; these are intentionally tiny and auditable.
    for kind, spec in type_taxonomy.items():
        for known in spec.get("known") or []:
            known_norm = canonicalize_entity_name(str(known))
            if known_norm and (
                name_match == known_norm or _norm_contains(name_match, known_norm)
            ):
                return _facet(str(kind), spec)

    # Extension and filename checks catch common document/code artifacts before
    # general synonym matching.
    if entity_type == "Document":
        if raw_lower.endswith((".pdf", ".doc", ".docx")) or _norm_contains(haystack, "report"):
            spec = type_taxonomy.get("Report", {})
            return _facet("Report", spec)
        if raw_lower.endswith((".md", ".txt")) and _norm_contains(haystack, "tutorial"):
            spec = type_taxonomy.get("Tutorial", {})
            return _facet("Tutorial", spec)
    if entity_type == "Artifact":
        if raw_lower.endswith((".dll", ".so", ".jar")):
            spec = type_taxonomy.get("Library", {})
            return _facet("Library", spec)
        if raw_lower.endswith((".exe", ".app")):
            spec = type_taxonomy.get("Tool", {})
            return _facet("Tool", spec)

    # Synonym match over normalized name + optional context.
    for kind, spec in type_taxonomy.items():
        for term in spec.get("synonyms") or []:
            if _norm_contains(haystack, str(term)):
                return _facet(str(kind), spec)

    return {}


def resolve_domain_type(
    entity_name: str,
    entity_type: str = "",
    text_context: str = "",
) -> dict[str, str]:
    """Infer PRD/app-design role facets for product-spec corpora.

    Universal entity types stay intentionally broad. PRD documents need a
    second lens that says whether an entity behaves like a Feature, Module,
    DataObject, AIModel, Constraint, Risk, Milestone, etc. This deterministic
    facet gives Mission Control product-design semantics without asking Ghost B
    to emit extra fields or changing the global schema.
    """
    taxonomy = _load_domain_taxonomy()
    if not taxonomy:
        return {}

    raw_name = str(entity_name or "")
    name_norm = canonicalize_entity_name(raw_name).replace("_", " ")
    context_norm = normalize_entity_name(text_context)
    haystack = " ".join(part for part in (name_norm, context_norm) if part)
    if not haystack:
        return {}

    def _domain(kind: str, spec: dict) -> dict[str, str]:
        return {
            "domain_type": kind,
            "domain_type_parent": str(spec.get("parent") or "AppDesign"),
            "domain_type_root": str(spec.get("root") or "PRD"),
        }

    # Known names are higher precision than generic synonym words.
    for kind, spec in taxonomy.items():
        if not isinstance(spec, dict):
            continue
        for known in spec.get("known") or []:
            known_norm = canonicalize_entity_name(str(known)).replace("_", " ")
            if known_norm and (
                name_norm == known_norm or _norm_contains(name_norm, known_norm)
            ):
                return _domain(str(kind), spec)

    for kind, spec in taxonomy.items():
        if not isinstance(spec, dict):
            continue
        for term in spec.get("synonyms") or []:
            if _norm_contains(haystack, str(term)):
                return _domain(str(kind), spec)

    return {}


def resolve_ontology_metadata(
    entity_name: str,
    entity_type: str,
    text_context: str = "",
) -> dict[str, str]:
    """Resolve all deterministic ontology metadata stored on Entity nodes."""
    metadata = resolve_facets(entity_name, entity_type, text_context)
    metadata.update(resolve_domain_type(entity_name, entity_type, text_context))
    family = resolve_canonical_family(entity_name, text_context)
    if family:
        metadata["canonical_family"] = family
    metadata["ontology_version"] = ONTOLOGY_VERSION
    return metadata


def _slugify_type(entity_type: str) -> str:
    """Lowercase, alphanumerics + hyphens only. Empty → 'other'."""
    slug = re.sub(r"[^a-z0-9]+", "-", (entity_type or "").lower()).strip("-")
    return slug or SchemaContext.ENTITY_SENTINEL


def _slugify_name(canonical_name: str) -> str:
    """Normalized canonical name → URL-safe slug (spaces → hyphens, no punctuation)."""
    return canonicalize_entity_name(canonical_name).replace(" ", "-")


def entity_id_from_name(canonical_name: str, entity_type: str | None = None) -> str:
    """Deterministic canonical entity ID.

    Format: `entity:{name_slug}`.

    The `entity_type` argument is intentionally ignored and retained only for
    call-site compatibility. Type is now extraction evidence, stored as
    primary_entity_type / observed_entity_types on Entity and extracted_type on
    MENTIONS. This prevents Product:pvector, Method:pvector, and Concept:pvector
    from becoming separate graph nodes.
    """
    return f"{ENTITY_ID_PREFIX}:{_slugify_name(canonical_name)}"


async def _upsert_document(
    driver: AsyncDriver,
    doc_id: str,
    corpus_id: str,
    user_id: str | None,
    file_id: str | None,
) -> None:
    async with driver.session() as session:
        await session.run(
            """
            MERGE (d:Document {doc_id: $doc_id})
            SET d.corpus_id = $corpus_id,
                d.user_id = $user_id,
                d.file_id = $file_id
            """,
            doc_id=doc_id,
            corpus_id=corpus_id,
            user_id=user_id,
            file_id=file_id,
        )


async def _upsert_chunk(
    driver: AsyncDriver,
    chunk_id: str,
    doc_id: str,
    corpus_id: str,
) -> None:
    async with driver.session() as session:
        await session.run(
            """
            MERGE (c:Chunk {chunk_id: $chunk_id})
            SET c.doc_id = $doc_id, c.corpus_id = $corpus_id
            WITH c
            MATCH (d:Document {doc_id: $doc_id, corpus_id: $corpus_id})
            MERGE (d)-[:HAS_CHUNK]->(c)
            """,
            chunk_id=chunk_id,
            doc_id=doc_id,
            corpus_id=corpus_id,
        )


async def _upsert_entity_and_mention(
    driver: AsyncDriver,
    entity: EntityItem,
    chunk_id: str,
    corpus_id: str,
) -> None:
    canonical = canonicalize_entity_name(entity.canonical_name)
    primary_type = resolve_primary_entity_type(canonical, [entity.entity_type])
    eid = entity_id_from_name(canonical, primary_type)
    ontology = resolve_ontology_metadata(canonical, primary_type)
    quality = quality_payload(
        entity.surface_form or entity.canonical_name or canonical,
        primary_type,
        observed_entity_types=[entity.entity_type],
    )
    async with driver.session() as session:
        await session.run(
            """
            MERGE (e:Entity {entity_id: $entity_id})
            ON CREATE SET e.first_seen = timestamp()
            SET e.normalized_name = $canonical_name,
                e.canonical_name = $canonical_name,
                e.display_name = $display_name,
                e.primary_entity_type = $primary_entity_type,
                e.entity_type = $primary_entity_type,
                e.confidence = CASE
                    WHEN e.confidence IS NULL OR $confidence > e.confidence THEN $confidence
                    ELSE e.confidence
                END,
                e.object_kind = $object_kind,
                e.object_kind_parent = $object_kind_parent,
                e.object_kind_root = $object_kind_root,
                e.domain_type = $domain_type,
                e.domain_type_parent = $domain_type_parent,
                e.domain_type_root = $domain_type_root,
                e.canonical_family = $canonical_family,
                e.ontology_version = $ontology_version,
                e.label_quality = $label_quality,
                e.eligible_for_topic_label = $eligible_for_topic_label,
                e.eligible_for_synthesis = $eligible_for_synthesis,
                e.quality_reasons = $quality_reasons,
                e.entity_quality_version = $entity_quality_version
            WITH e
            SET e.observed_entity_types = CASE
                WHEN e.observed_entity_types IS NULL THEN [$extracted_type]
                WHEN $extracted_type IN e.observed_entity_types THEN e.observed_entity_types
                ELSE e.observed_entity_types + [$extracted_type]
            END
            WITH e
            MATCH (c:Chunk {chunk_id: $chunk_id, corpus_id: $corpus_id})
            MERGE (c)-[m:MENTIONS]->(e)
            SET m.confidence = CASE
                    WHEN m.confidence IS NULL OR $confidence > m.confidence THEN $confidence
                    ELSE m.confidence
                END,
                m.extracted_type = $extracted_type,
                m.surface_form = $surface_form,
                m.extractor = 'ghost_b',
                m.ontology_version = $ontology_version
            SET m.extracted_types = CASE
                WHEN m.extracted_types IS NULL THEN [$extracted_type]
                WHEN $extracted_type IN m.extracted_types THEN m.extracted_types
                ELSE m.extracted_types + [$extracted_type]
            END
            """,
            entity_id=eid,
            canonical_name=canonical,
            display_name=entity.surface_form or entity.canonical_name,
            surface_form=entity.surface_form or entity.canonical_name,
            primary_entity_type=primary_type,
            extracted_type=entity.entity_type,
            confidence=entity.confidence,
            object_kind=ontology.get("object_kind"),
            object_kind_parent=ontology.get("object_kind_parent"),
            object_kind_root=ontology.get("object_kind_root"),
            domain_type=ontology.get("domain_type"),
            domain_type_parent=ontology.get("domain_type_parent"),
            domain_type_root=ontology.get("domain_type_root"),
            canonical_family=ontology.get("canonical_family"),
            ontology_version=ontology.get("ontology_version"),
            label_quality=quality["label_quality"],
            eligible_for_topic_label=quality["eligible_for_topic_label"],
            eligible_for_synthesis=quality["eligible_for_synthesis"],
            quality_reasons=quality["quality_reasons"],
            entity_quality_version=quality["entity_quality_version"],
            chunk_id=chunk_id,
            corpus_id=corpus_id,
        )


async def _upsert_relation(
    driver: AsyncDriver,
    relation: RelationItem,
    name_to_type: dict[str, str],
) -> None:
    """Upsert RELATES_TO edge between two entities.

    Phase 14.3: looks up subject/object entity_type from `name_to_type` (built
    from this document's extracted entities). When a relation references a name
    the LLM didn't extract as an entity in any chunk of this document, we fall
    back to the ENTITY_SENTINEL ('other') namespace so the edge is preserved.
    """
    if relation.object_kind != "entity":
        return
    subject_type = name_to_type.get(
        canonicalize_entity_name(relation.subject), SchemaContext.ENTITY_SENTINEL
    )
    object_type = name_to_type.get(
        canonicalize_entity_name(relation.object), SchemaContext.ENTITY_SENTINEL
    )
    subject_id = entity_id_from_name(relation.subject, subject_type)
    object_id = entity_id_from_name(relation.object, object_type)
    relation_family = relation_family_for_predicate(relation.predicate)
    edge_strength = relation_edge_strength(
        relation.predicate,
        relation.confidence,
        relation.validation_status,
        predicate_refined=False,
    )
    async with driver.session() as session:
        await session.run(
            """
            MATCH (s:Entity {entity_id: $subject_id})
            MATCH (o:Entity {entity_id: $object_id})
            MERGE (s)-[r:RELATES_TO {predicate: $predicate}]->(o)
            SET r.confidence = $confidence,
                r.relation_family = $relation_family,
                r.edge_strength = $edge_strength,
                r.eligible_for_synthesis = $eligible_for_synthesis
            """,
            subject_id=subject_id,
            object_id=object_id,
            predicate=relation.predicate,
            relation_family=relation_family,
            edge_strength=edge_strength,
            eligible_for_synthesis=relation_eligible_for_synthesis(
                relation.predicate, relation.confidence, relation.validation_status
            ),
            confidence=relation.confidence,
        )


async def write_document_graph(
    driver: AsyncDriver,
    doc_id: str,
    corpus_id: str,
    extraction_results: list[ExtractionResult],
    user_id: str | None = None,
    file_id: str | None = None,
    all_chunk_ids: list[str] | None = None,
) -> None:
    """
    Write the full graph for one document after GHOST B completes.

    Phase K — BATCHED via UNWIND. Previously this fired one MERGE per
    chunk / entity / relation, producing 500-3000 serial round-trips per
    document. The new version executes 4 queries total per document
    (Document, Chunks, Entities+Mentions, Relations) regardless of size,
    reducing Neo4j round-trips by ~1000x on busy docs.

    Creates/updates: Document node, Chunk nodes, HAS_CHUNK edges,
    Entity nodes, MENTIONS edges, RELATES_TO edges (entity→entity only).
    all_chunk_ids lets ingestion preserve complete document/chunk coverage in
    Neo4j even when Ghost B only returned partial entity/relation extraction.
    """
    # 1. Document node — single MERGE.
    await _upsert_document(driver, doc_id, corpus_id, user_id, file_id)

    # 2. Build batched payloads from the extraction results.
    chunk_ids = list(dict.fromkeys([*(all_chunk_ids or []), *[r.chunk_id for r in extraction_results]]))
    chunk_rows: list[dict] = [{"chunk_id": chunk_id} for chunk_id in chunk_ids]

    entity_groups: dict[str, dict] = {}
    for result in extraction_results:
        for entity in result.entities:
            canonical = canonicalize_entity_name(entity.canonical_name)
            if not canonical:
                continue
            group = entity_groups.setdefault(
                canonical,
                {
                    "canonical_name": canonical,
                    "display_name": entity.surface_form or entity.canonical_name,
                    "observed_entity_types": [],
                    "confidence": 0.0,
                },
            )
            if entity.entity_type not in group["observed_entity_types"]:
                group["observed_entity_types"].append(entity.entity_type)
            if entity.confidence > group["confidence"]:
                group["confidence"] = entity.confidence
                group["display_name"] = entity.surface_form or entity.canonical_name

    entity_identity: dict[str, dict] = {}
    for canonical, group in entity_groups.items():
        observed_types = list(group["observed_entity_types"])
        primary_type = resolve_primary_entity_type(canonical, observed_types)
        ontology = resolve_ontology_metadata(canonical, primary_type)
        quality = quality_payload(
            group["display_name"] or canonical,
            primary_type,
            observed_entity_types=observed_types,
        )
        entity_identity[canonical] = {
            "entity_id": entity_id_from_name(canonical, primary_type),
            "canonical_name": canonical,
            "display_name": group["display_name"],
            "primary_entity_type": primary_type,
            "observed_entity_types": observed_types,
            "confidence": group["confidence"],
            "object_kind": ontology.get("object_kind"),
            "object_kind_parent": ontology.get("object_kind_parent"),
            "object_kind_root": ontology.get("object_kind_root"),
            "domain_type": ontology.get("domain_type"),
            "domain_type_parent": ontology.get("domain_type_parent"),
            "domain_type_root": ontology.get("domain_type_root"),
            "canonical_family": ontology.get("canonical_family"),
            "ontology_version": ontology.get("ontology_version"),
            "label_quality": quality["label_quality"],
            "eligible_for_topic_label": quality["eligible_for_topic_label"],
            "eligible_for_synthesis": quality["eligible_for_synthesis"],
            "quality_reasons": quality["quality_reasons"],
            "entity_quality_version": quality["entity_quality_version"],
        }

    mention_rows: list[dict] = []
    alias_resolution_count = 0
    facet_resolution_count = 0
    domain_resolution_count = 0
    family_resolution_count = 0
    for r in extraction_results:
        for entity in r.entities:
            normalized = normalize_entity_name(entity.canonical_name)
            canonical_normalized = resolve_entity_alias(normalized)
            identity = entity_identity.get(canonical_normalized)
            if not identity:
                continue
            if canonical_normalized != normalized:
                alias_resolution_count += 1
            if identity.get("object_kind"):
                facet_resolution_count += 1
            if identity.get("domain_type"):
                domain_resolution_count += 1
            if identity.get("canonical_family"):
                family_resolution_count += 1
            mention_rows.append({
                "chunk_id": r.chunk_id,
                "entity_id": identity["entity_id"],
                "normalized_name": canonical_normalized,
                "canonical_name": identity["canonical_name"],
                "display_name": identity["display_name"],
                "surface_form": entity.surface_form or entity.canonical_name,
                "primary_entity_type": identity["primary_entity_type"],
                "entity_type": identity["primary_entity_type"],
                "extracted_type": entity.entity_type,
                "observed_entity_types": identity["observed_entity_types"],
                "confidence": entity.confidence,
                "object_kind": identity.get("object_kind"),
                "object_kind_parent": identity.get("object_kind_parent"),
                "object_kind_root": identity.get("object_kind_root"),
                "domain_type": identity.get("domain_type"),
                "domain_type_parent": identity.get("domain_type_parent"),
                "domain_type_root": identity.get("domain_type_root"),
                "canonical_family": identity.get("canonical_family"),
                "ontology_version": identity.get("ontology_version"),
                "label_quality": identity.get("label_quality"),
                "eligible_for_topic_label": identity.get("eligible_for_topic_label"),
                "eligible_for_synthesis": identity.get("eligible_for_synthesis"),
                "quality_reasons": identity.get("quality_reasons"),
                "entity_quality_version": identity.get("entity_quality_version"),
            })

    relation_rows: list[dict] = []
    related_to_refinement_count = 0
    for r in extraction_results:
        for relation in r.relations:
            if relation.object_kind != "entity":
                continue
            source_predicate_raw = relation.source_predicate or relation.predicate
            normalized_source_predicate, reverse_relation = normalize_relation_predicate_alias(
                source_predicate_raw
            )
            subject_name = relation.object if reverse_relation else relation.subject
            object_name = relation.subject if reverse_relation else relation.object
            subject_canonical = canonicalize_entity_name(subject_name)
            object_canonical = canonicalize_entity_name(object_name)
            subject_identity = entity_identity.get(subject_canonical)
            object_identity = entity_identity.get(object_canonical)
            refined_predicate = refine_related_to_predicate(
                relation.predicate,
                subject_identity,
                object_identity,
                source_predicate=normalized_source_predicate,
                evidence_phrase=relation.evidence_phrase,
                relation_cue=relation.relation_cue,
                validation_status=relation.validation_status,
                predicate_confidence=relation.predicate_confidence,
            )
            if refined_predicate != relation.predicate:
                related_to_refinement_count += 1
            predicate_refined = refined_predicate != relation.predicate
            validation_status = relation.validation_status or (
                "repaired_from_related_to" if predicate_refined else None
            )
            if reverse_relation:
                validation_status = _append_status(validation_status, "direction_repair")
            facet_evidence_available = bool(
                subject_identity
                and object_identity
                and (
                    subject_identity.get("object_kind")
                    or object_identity.get("object_kind")
                    or subject_identity.get("domain_type")
                    or object_identity.get("domain_type")
                    or subject_identity.get("canonical_family")
                    or object_identity.get("canonical_family")
                )
            )
            if (
                refined_predicate != SchemaContext.RELATION_SENTINEL
                and facet_evidence_available
                and object_kind_compatible(
                    refined_predicate, subject_identity, object_identity
                )
                is False
            ):
                validation_status = _append_status(
                    validation_status, "object_kind_mismatch"
                )
                normalized_source_predicate = refined_predicate
                refined_predicate = SchemaContext.RELATION_SENTINEL
                predicate_refined = True
            edge_strength = relation_edge_strength(
                refined_predicate,
                relation.confidence,
                validation_status,
                predicate_refined=predicate_refined,
            )
            relation_rows.append({
                "subject_id": (subject_identity or {}).get(
                    "entity_id", entity_id_from_name(subject_canonical)
                ),
                "object_id": (object_identity or {}).get(
                    "entity_id", entity_id_from_name(object_canonical)
                ),
                "predicate": refined_predicate,
                "source_predicate": source_predicate_raw,
                "relation_family": relation_family_for_predicate(refined_predicate),
                "predicate_refined": predicate_refined,
                "direction_repaired": reverse_relation,
                "edge_strength": edge_strength,
                "eligible_for_synthesis": relation_eligible_for_synthesis(
                    refined_predicate, relation.confidence, validation_status
                ),
                "validation_status": validation_status,
                "evidence_phrase": relation.evidence_phrase,
                "relation_cue": relation.relation_cue,
                "atomic_fact": relation.atomic_fact,
                "candidate_subject": relation.candidate_subject or relation.subject,
                "candidate_predicate": relation.candidate_predicate or source_predicate_raw,
                "candidate_object": relation.candidate_object or relation.object,
                "review_status": relation.review_status,
                "confidence": relation.confidence,
                "predicate_confidence": relation.predicate_confidence,
                "extraction_confidence": relation.extraction_confidence,
                "alternative_predicates_considered": relation.alternative_predicates_considered or [],
                "rejection_reasoning": relation.rejection_reasoning,
                "chunk_id": r.chunk_id,
            })

    if alias_resolution_count:
        logger.info(
            "Neo4j alias resolution: doc=%s corpus=%s aliases=%d",
            doc_id[:12], corpus_id[:8], alias_resolution_count,
        )
    if facet_resolution_count or domain_resolution_count or family_resolution_count:
        logger.info(
            "Neo4j ontology resolution: doc=%s corpus=%s object_kind=%d domain_type=%d family=%d version=%s",
            doc_id[:12], corpus_id[:8], facet_resolution_count,
            domain_resolution_count, family_resolution_count, ONTOLOGY_VERSION,
        )
    if related_to_refinement_count:
        logger.info(
            "Neo4j relation refinement: doc=%s corpus=%s related_to_refined=%d",
            doc_id[:12], corpus_id[:8], related_to_refinement_count,
        )

    # 3. Single session for all remaining writes. Each query uses UNWIND to
    # fan out over its list.
    async with driver.session() as session:
        # Chunks + HAS_CHUNK edges.
        if chunk_rows:
            await session.run(
                """
                UNWIND $rows AS row
                MERGE (c:Chunk {chunk_id: row.chunk_id})
                SET c.doc_id = $doc_id, c.corpus_id = $corpus_id
                WITH c
                MATCH (d:Document {doc_id: $doc_id, corpus_id: $corpus_id})
                MERGE (d)-[:HAS_CHUNK]->(c)
                """,
                rows=chunk_rows,
                doc_id=doc_id,
                corpus_id=corpus_id,
            )

        # Entities + MENTIONS edges.
        if mention_rows:
            await session.run(
                """
                UNWIND $rows AS row
                MERGE (e:Entity {entity_id: row.entity_id})
                ON CREATE SET e.first_seen = timestamp()
                SET e.normalized_name = row.normalized_name,
                    e.canonical_name = row.canonical_name,
                    e.display_name = row.display_name,
                    e.primary_entity_type = row.primary_entity_type,
                    e.entity_type = row.primary_entity_type,
                    e.confidence = CASE
                        WHEN e.confidence IS NULL OR row.confidence > e.confidence THEN row.confidence
                        ELSE e.confidence
                    END,
                    e.object_kind = row.object_kind,
                    e.object_kind_parent = row.object_kind_parent,
                    e.object_kind_root = row.object_kind_root,
                    e.domain_type = row.domain_type,
                    e.domain_type_parent = row.domain_type_parent,
                    e.domain_type_root = row.domain_type_root,
                    e.canonical_family = row.canonical_family,
                    e.ontology_version = row.ontology_version,
                    e.label_quality = row.label_quality,
                    e.eligible_for_topic_label = row.eligible_for_topic_label,
                    e.eligible_for_synthesis = row.eligible_for_synthesis,
                    e.quality_reasons = row.quality_reasons,
                    e.entity_quality_version = row.entity_quality_version
                WITH e, row
                SET e.observed_entity_types = reduce(
                    types = coalesce(e.observed_entity_types, []),
                    t IN row.observed_entity_types |
                    CASE WHEN t IN types THEN types ELSE types + [t] END
                )
                WITH e, row
                MATCH (c:Chunk {chunk_id: row.chunk_id, corpus_id: $corpus_id})
                MERGE (c)-[m:MENTIONS]->(e)
                SET m.confidence = CASE
                        WHEN m.confidence IS NULL OR row.confidence > m.confidence THEN row.confidence
                        ELSE m.confidence
                    END,
                    m.extracted_type = row.extracted_type,
                    m.surface_form = row.surface_form,
                    m.extractor = 'ghost_b',
                    m.ontology_version = row.ontology_version
                SET m.extracted_types = CASE
                    WHEN m.extracted_types IS NULL THEN [row.extracted_type]
                    WHEN row.extracted_type IN m.extracted_types THEN m.extracted_types
                    ELSE m.extracted_types + [row.extracted_type]
                END
                """,
                rows=mention_rows,
                corpus_id=corpus_id,
            )

        # RELATES_TO edges between entities (entity_id lookup).
        if relation_rows:
            await session.run(
                """
                UNWIND $rows AS row
                MATCH (s:Entity {entity_id: row.subject_id})
                MATCH (o:Entity {entity_id: row.object_id})
                MERGE (s)-[r:RELATES_TO {predicate: row.predicate}]->(o)
                SET r.confidence = CASE
                        WHEN r.confidence IS NULL OR row.confidence > r.confidence THEN row.confidence
                        ELSE r.confidence
                    END,
                    r.relation_family = row.relation_family,
                    r.predicate_refined = coalesce(r.predicate_refined, false) OR row.predicate_refined,
                    r.direction_repaired = coalesce(r.direction_repaired, false) OR row.direction_repaired,
                    r.edge_strength = CASE
                        WHEN row.edge_strength = 'strong' THEN 'strong'
                        WHEN row.edge_strength = 'repaired' AND coalesce(r.edge_strength, '') <> 'strong' THEN 'repaired'
                        WHEN row.edge_strength = 'thin' AND coalesce(r.edge_strength, '') IN ['', 'weak'] THEN 'thin'
                        ELSE coalesce(r.edge_strength, row.edge_strength)
                    END,
                    r.eligible_for_synthesis = coalesce(r.eligible_for_synthesis, false) OR row.eligible_for_synthesis
                SET r.predicate_confidence = CASE
                    WHEN row.predicate_confidence IS NULL THEN r.predicate_confidence
                    WHEN r.predicate_confidence IS NULL OR row.predicate_confidence > r.predicate_confidence THEN row.predicate_confidence
                    ELSE r.predicate_confidence
                END,
                    r.extraction_confidence = CASE
                    WHEN row.extraction_confidence IS NULL THEN r.extraction_confidence
                    WHEN r.extraction_confidence IS NULL OR row.extraction_confidence > r.extraction_confidence THEN row.extraction_confidence
                    ELSE r.extraction_confidence
                END
                SET r.evidence_chunk_ids = CASE
                    WHEN r.evidence_chunk_ids IS NULL THEN [row.chunk_id]
                    WHEN row.chunk_id IN r.evidence_chunk_ids THEN r.evidence_chunk_ids
                    ELSE r.evidence_chunk_ids + [row.chunk_id]
                END
                SET r.evidence_phrases = CASE
                    WHEN row.evidence_phrase IS NULL OR row.evidence_phrase = '' THEN coalesce(r.evidence_phrases, [])
                    WHEN r.evidence_phrases IS NULL THEN [row.evidence_phrase]
                    WHEN row.evidence_phrase IN r.evidence_phrases THEN r.evidence_phrases
                    ELSE r.evidence_phrases + [row.evidence_phrase]
                END
                SET r.atomic_facts = CASE
                    WHEN row.atomic_fact IS NULL OR row.atomic_fact = '' THEN coalesce(r.atomic_facts, [])
                    WHEN r.atomic_facts IS NULL THEN [row.atomic_fact]
                    WHEN row.atomic_fact IN r.atomic_facts THEN r.atomic_facts
                    ELSE r.atomic_facts + [row.atomic_fact]
                END
                SET r.relation_cues = CASE
                    WHEN row.relation_cue IS NULL OR row.relation_cue = '' THEN coalesce(r.relation_cues, [])
                    WHEN r.relation_cues IS NULL THEN [row.relation_cue]
                    WHEN row.relation_cue IN r.relation_cues THEN r.relation_cues
                    ELSE r.relation_cues + [row.relation_cue]
                END
                SET r.source_predicates = CASE
                    WHEN r.source_predicates IS NULL THEN [row.source_predicate]
                    WHEN row.source_predicate IN r.source_predicates THEN r.source_predicates
                    ELSE r.source_predicates + [row.source_predicate]
                END
                SET r.validation_statuses = CASE
                    WHEN row.validation_status IS NULL OR row.validation_status = '' THEN coalesce(r.validation_statuses, [])
                    WHEN r.validation_statuses IS NULL THEN [row.validation_status]
                    WHEN row.validation_status IN r.validation_statuses THEN r.validation_statuses
                    ELSE r.validation_statuses + [row.validation_status]
                END
                SET r.review_statuses = CASE
                    WHEN row.review_status IS NULL OR row.review_status = '' THEN coalesce(r.review_statuses, [])
                    WHEN r.review_statuses IS NULL THEN [row.review_status]
                    WHEN row.review_status IN r.review_statuses THEN r.review_statuses
                    ELSE r.review_statuses + [row.review_status]
                END
                SET r.candidate_subjects = CASE
                    WHEN row.candidate_subject IS NULL OR row.candidate_subject = '' THEN coalesce(r.candidate_subjects, [])
                    WHEN r.candidate_subjects IS NULL THEN [row.candidate_subject]
                    WHEN row.candidate_subject IN r.candidate_subjects THEN r.candidate_subjects
                    ELSE r.candidate_subjects + [row.candidate_subject]
                END
                SET r.candidate_predicates = CASE
                    WHEN row.candidate_predicate IS NULL OR row.candidate_predicate = '' THEN coalesce(r.candidate_predicates, [])
                    WHEN r.candidate_predicates IS NULL THEN [row.candidate_predicate]
                    WHEN row.candidate_predicate IN r.candidate_predicates THEN r.candidate_predicates
                    ELSE r.candidate_predicates + [row.candidate_predicate]
                END
                SET r.candidate_objects = CASE
                    WHEN row.candidate_object IS NULL OR row.candidate_object = '' THEN coalesce(r.candidate_objects, [])
                    WHEN r.candidate_objects IS NULL THEN [row.candidate_object]
                    WHEN row.candidate_object IN r.candidate_objects THEN r.candidate_objects
                    ELSE r.candidate_objects + [row.candidate_object]
                END
                SET r.alternative_predicates_considered = reduce(
                    preds = coalesce(r.alternative_predicates_considered, []),
                    p IN coalesce(row.alternative_predicates_considered, []) |
                    CASE WHEN p IN preds THEN preds ELSE preds + [p] END
                )
                SET r.rejection_reasonings = CASE
                    WHEN row.rejection_reasoning IS NULL OR row.rejection_reasoning = '' THEN coalesce(r.rejection_reasonings, [])
                    WHEN r.rejection_reasonings IS NULL THEN [row.rejection_reasoning]
                    WHEN row.rejection_reasoning IN r.rejection_reasonings THEN r.rejection_reasonings
                    ELSE r.rejection_reasonings + [row.rejection_reasoning]
                END
                SET r.corpus_ids = CASE
                    WHEN r.corpus_ids IS NULL THEN [$corpus_id]
                    WHEN $corpus_id IN r.corpus_ids THEN r.corpus_ids
                    ELSE r.corpus_ids + [$corpus_id]
                END
                """,
                rows=relation_rows,
                corpus_id=corpus_id,
            )
            await session.run(
                """
                UNWIND $rows AS row
                WITH row
                WHERE row.predicate <> 'related_to'
                MATCH (s:Entity {entity_id: row.subject_id})
                MATCH (o:Entity {entity_id: row.object_id})
                MATCH (s)-[weak:RELATES_TO {predicate: 'related_to'}]-(o)
                DELETE weak
                """,
                rows=relation_rows,
            )

    entity_count = sum(len(r.entities) for r in extraction_results)
    relation_count = sum(len(r.relations) for r in extraction_results)
    logger.info(
        "Neo4j write complete (batched): doc=%s corpus=%s chunks=%d entities=%d relations=%d",
        doc_id,
        corpus_id,
        len(chunk_rows),
        entity_count,
        relation_count,
    )
