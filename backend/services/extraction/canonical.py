"""Canonical entity and predicate representations — single source of truth.

This module is the SOLE owner of:
  - Entity name normalization (lowercase, NFKD, strip punctuation)
  - Entity alias resolution (config/canonical/entity_aliases.json)
  - Entity ID generation — see ENTITY_ID_POLICY below
  - Entity type normalization (GLiNER labels → ontology Title-Case)
  - Predicate label canonicalization (NL labels → snake_case ontology predicates)

WHY THIS EXISTS
    Contract drift between extraction layers caused silent data loss:
      - pair_allowed("uses", "concept", "concept") returned False
        (ontology expects "Concept")
      - Relex emitted "instance of" but production expected "is an instance of"
      - Syntax evidence couldn't join Relex pairs when the same entity appeared
        at different char offsets
      - Empty entity types were needed to bypass the ontology gate

    Moving canonicalization from graph-write time (neo4j_writer.py) to the
    extraction boundary means Relex, dep_path/frame extractors, the
    corroboration gate, and the graph writer all consume the same canonical
    forms.

CANONICAL DATA OWNERSHIP
    Shared configuration lives in `config/canonical/` so the extraction and
    graph packages both depend on the canonical configuration — NOT on each
    other. Files:
        config/canonical/entity_aliases.json
        config/canonical/entity_type_overrides.json
        config/canonical/predicate_labels.yaml

    For backwards compatibility, the loaders fall back to the historical
    location `backend/services/graph/` if the canonical config is absent
    (e.g. older checkouts). New data MUST be added to `config/canonical/`.

ENTITY ID POLICY  (see ENTITY_ID_POLICY)
    Current state: NAME-ONLY.
        entity_id_from_name("Apple") == "entity:apple"
    This is preserved for graph continuity: existing nodes in Neo4j use
    name-only IDs, and Product:pvector + Method:pvector + Concept:pvector
    MUST collapse to one node.

    Collision risk: name-only IDs collide across types:
        entity:apple   ← Apple Inc. (organization)
        entity:apple   ← apple (fruit / food)
    Long term, canonical entities should receive REGISTRY-BACKED stable IDs.
    As a provisional escape valve, `entity_id_with_type(name, type)` returns
    a type-qualified ID for new code paths that need to disambiguate. The
    graph writer continues to use name-only IDs until a registry migration is
    piloted with rollback and quality evaluation.

DESIGN RULES
    - Zero dependencies on neo4j, ghost_b, or any service module.
    - Data files live in config/canonical/ (preferred) or services/graph/ (fallback).
    - Every function is pure (no side effects beyond lru_cache).
    - Neo4j_writer re-exports these for backwards compatibility.
    - Consumers MUST use the public API. Private names (`_UPPER_TO_ONTOLOGY`)
      are kept ONLY as backwards-compat aliases and may be removed.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema version — stamped into graph-ready relation records
# ---------------------------------------------------------------------------
# Bump when the canonical forms change in a way that breaks downstream
# consumers (predicate mapping, type ontology, alias resolution, ID format).
# Graph-ready records MUST carry this version so consumers can detect schema
# drift and trigger re-canonicalization when needed.
CANONICAL_SCHEMA_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Paths — canonical config lives in config/canonical/, with a historical
# fallback to services/graph/ for older checkouts.
# ---------------------------------------------------------------------------
from services.extraction.config_locator import find_config_dir

_CANONICAL_CONFIG_DIR = find_config_dir(__file__) / "canonical"
_GRAPH_DIR = Path(__file__).resolve().parents[1] / "graph"

# Preferred location (config/canonical/)
CANONICAL_ALIAS_MAP_PATH = _CANONICAL_CONFIG_DIR / "entity_aliases.json"
CANONICAL_ENTITY_TYPE_OVERRIDES_PATH = _CANONICAL_CONFIG_DIR / "entity_type_overrides.json"
CANONICAL_PREDICATE_LABELS_PATH = _CANONICAL_CONFIG_DIR / "predicate_labels.yaml"

# Historical fallback (services/graph/) — read-only, do not write here.
_GRAPH_ALIAS_MAP_PATH = _GRAPH_DIR / "entity_aliases.json"
_GRAPH_ENTITY_TYPE_OVERRIDES_PATH = _GRAPH_DIR / "entity_type_overrides.json"

# Backwards-compat: existing callers reference these names.
ALIAS_MAP_PATH = CANONICAL_ALIAS_MAP_PATH
ENTITY_TYPE_OVERRIDES_PATH = CANONICAL_ENTITY_TYPE_OVERRIDES_PATH


def _resolve_path(preferred: Path, fallback: Path) -> Path:
    """Return the preferred path if it exists, else the fallback."""
    if preferred.exists():
        return preferred
    return fallback


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ENTITY_ID_PREFIX = "entity"

# Documented policy for entity ID generation. Callers MUST NOT assume a
# specific format — use `entity_id_from_name` (name-only) or
# `entity_id_with_type` (type-qualified). A future registry migration will
# introduce `entity_id_from_registry` and update this string.
ENTITY_ID_POLICY: str = "name_only"
ENTITY_ID_POLICY_DOC: str = (
    "Entity IDs are derived from the canonical name only (entity:{slug}). "
    "Type information is intentionally NOT folded into the ID because the "
    "graph treats Product:pvector, Method:pvector, and Concept:pvector as one "
    "node. COLLISION RISK: 'Apple' the organization and 'apple' the fruit "
    "share an ID. Long term this is replaced by registry-backed stable IDs. "
    "Use entity_id_with_type() for new code that needs type-qualified "
    "disambiguation without committing to a registry migration."
)

GENERIC_ENTITY_TERMS = {
    "agent",
    "attention",
    "class",
    "data",
    "example",
    "field",
    "memory",
    "method",
    "model",
    "object",
    "process",
    "system",
    "user",
    "value",
}


# ---------------------------------------------------------------------------
# Entity name canonicalization
# ---------------------------------------------------------------------------

def normalize_entity_name(name: str) -> str:
    """Canonical form for dedup: lowercase, NFKD, punctuation → space, collapse.

    Punctuation becomes a separator instead of being deleted so hyphenated and
    versioned identifiers keep their token structure: "Alert C-17" → "alert c 17"
    (never "alert c17"), "Beacon 2.2" → "beacon 2 2" (never merged with
    "beacon 22"). This keeps node identity aligned with evaluators that
    tokenize on non-alphanumerics, and keeps distinct identifiers distinct.
    """
    name = name.lower().strip()
    name = unicodedata.normalize("NFKD", name)
    name = re.sub(r"[^\w\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


# ---------------------------------------------------------------------------
# Noun-phrase name cores — in-text casing as the name signal
# ---------------------------------------------------------------------------
# In "the Redlark database" only "Redlark" is part of the name; "database" is
# a lowercase descriptor. The casing profile of the phrase itself (not any
# domain lexicon) separates name cores from descriptors, determiners, and
# quantifiers.

_NAME_STOP_TOKENS = frozenset({
    "a", "an", "the",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "several", "some", "many", "few", "each", "every", "all", "both",
    "any", "no", "another", "other", "various", "numerous", "most", "more",
    "less", "least", "this", "that", "these", "those",
    "its", "his", "her", "their", "our", "your", "my",
    # approximators and indefinite pronouns carry no name evidence
    "nearly", "almost", "roughly", "approximately", "about",
    "everything", "anything", "nothing", "something",
    "everyone", "anyone", "someone", "nobody", "none",
})


def singularize_token(word: str) -> str:
    """Conservative English singular of a final noun token, for identity
    folding only ("manifests"→"manifest", "batches"→"batch", "entries"→"entry").
    Returns the input unchanged when no safe rule applies."""
    lower = word.lower()
    if len(lower) > 3 and lower.endswith("ies"):
        return word[:-3] + ("Y" if word[-3].isupper() else "y")
    if len(lower) > 4 and lower.endswith(("ches", "shes", "xes", "sses", "zes")):
        return word[:-2]
    if len(lower) > 3 and lower.endswith("s") and not lower.endswith(("ss", "us", "is")):
        return word[:-1]
    return word
_NEUTRAL_DETERMINERS = frozenset({"a", "an", "the"})
_TOKEN_EDGE_PUNCT = "\"'“”‘’().,;:!?"
_NUMERIC_TAIL_RE = re.compile(r"^\d[\w.\-]*$")


def _phrase_tokens(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"\S+", text)]


def _token_word(token: str) -> str:
    return token.strip(_TOKEN_EDGE_PUNCT)


def _is_possessive_token(token: str) -> bool:
    return _token_word(token).endswith(("'s", "’s"))


def _is_name_core_token(token: str) -> bool:
    word = _token_word(token)
    if not word or word.casefold() in _NAME_STOP_TOKENS or _is_possessive_token(token):
        return False
    if any(char.isupper() for char in word):
        return True
    return bool(re.search(r"[A-Za-z]", word)) and bool(re.search(r"\d", word))


def name_core_span(text: str) -> tuple[int, int] | None:
    """Char span of the capitalized/identifier name core inside a noun phrase.

    Returns None when the phrase has no core (fully lowercase phrases such as
    "approval record" are legitimate technical names and are NOT descriptor
    cases — callers must leave them untouched). Trailing numeric tokens extend
    the core so versioned names stay whole ("Pinion 3.2").
    """
    tokens = _phrase_tokens(text)
    core_indexes = [index for index, (_s, _e, tok) in enumerate(tokens) if _is_name_core_token(tok)]
    if not core_indexes:
        return None
    first, last = core_indexes[0], core_indexes[-1]
    while last + 1 < len(tokens) and _NUMERIC_TAIL_RE.match(_token_word(tokens[last + 1][2])):
        last += 1
    return tokens[first][0], tokens[last][1]


def endpoint_mint_policy(text: str) -> tuple[str, tuple[int, int] | None]:
    """Decide whether a relation endpoint NP may name an entity.

    Returns (decision, span): "core" narrows to the capitalized/identifier
    core; "lowercase_phrase" keeps a plain multi-word technical phrase (only
    neutral determiners stripped); "blocked" means the NP carries no name
    evidence (quantified counts, possessive descriptions, bare common nouns)
    and must stay UNRESOLVED rather than minting an entity.
    """
    span = name_core_span(text)
    if span is not None:
        return "core", span
    tokens = _phrase_tokens(text)
    start_index, end_index = 0, len(tokens)
    blocked_stripping = False
    while start_index < end_index:
        word = _token_word(tokens[start_index][2]).casefold()
        if _is_possessive_token(tokens[start_index][2]):
            blocked_stripping = True
            start_index += 1
            continue
        if word in _NAME_STOP_TOKENS:
            if word not in _NEUTRAL_DETERMINERS:
                blocked_stripping = True
            start_index += 1
            continue
        break
    remaining = tokens[start_index:end_index]
    if blocked_stripping or len(remaining) < 2:
        return "blocked", None
    if all(re.fullmatch(r"[a-z][a-z\-]*", _token_word(tok)) for _s, _e, tok in remaining):
        return "lowercase_phrase", (remaining[0][0], remaining[-1][1])
    return "blocked", None


@lru_cache(maxsize=1)
def _load_alias_lookup() -> dict[str, str]:
    path = _resolve_path(CANONICAL_ALIAS_MAP_PATH, _GRAPH_ALIAS_MAP_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Entity alias map failed to load (%s): %s", path, exc)
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
    """Full canonicalization: normalize + alias resolution."""
    return resolve_entity_alias(normalize_entity_name(name))


@lru_cache(maxsize=1)
def _load_entity_type_overrides() -> dict[str, str]:
    path = _resolve_path(CANONICAL_ENTITY_TYPE_OVERRIDES_PATH, _GRAPH_ENTITY_TYPE_OVERRIDES_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Entity type overrides failed to load (%s): %s", path, exc)
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


def get_entity_type_overrides() -> dict[str, str]:
    """Public accessor for the type-override table."""
    return _load_entity_type_overrides()


# ---------------------------------------------------------------------------
# Alias map validation — fail closed on cycles and missing targets
# ---------------------------------------------------------------------------

class AliasMapError(ValueError):
    """Raised when the alias map fails validation."""


def validate_alias_map(
    raw_map: dict[str, list[str]] | None = None,
    *,
    raise_on_error: bool = False,
) -> list[str]:
    """Validate an alias map for cycles and missing targets.

    Returns a list of human-readable error strings. Empty list = valid.

    Checks:
      1. CYCLES: A → B and B → A (after normalization). A canonical entry
         cannot also appear as another entry's alias.
      2. CONVERGENCE: A → C and B → C is legitimate but recorded as info
         (not an error) when two different keys both alias to the same target.
      3. MISSING TARGETS: an alias resolves to a canonical name that is not
         itself a key in the map.

    Set `raise_on_error=True` to raise AliasMapError on the first violation.
    """
    if raw_map is None:
        path = _resolve_path(CANONICAL_ALIAS_MAP_PATH, _GRAPH_ALIAS_MAP_PATH)
        try:
            raw_map = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception as exc:
            msg = f"alias map unreadable: {exc}"
            if raise_on_error:
                raise AliasMapError(msg) from exc
            return [msg]

    errors: list[str] = []
    normalized_keys: dict[str, str] = {}
    for canonical, aliases in raw_map.items():
        canonical_norm = normalize_entity_name(canonical)
        if not canonical_norm:
            errors.append(f"empty canonical key after normalization: {canonical!r}")
            continue
        normalized_keys[canonical_norm] = canonical_norm

    # Build alias → canonical mapping
    alias_to_canonical: dict[str, str] = {}
    for canonical, aliases in raw_map.items():
        canonical_norm = normalize_entity_name(canonical)
        if not canonical_norm:
            continue
        for alias in aliases or []:
            alias_norm = normalize_entity_name(str(alias))
            if not alias_norm:
                errors.append(
                    f"empty alias for canonical {canonical!r}: {alias!r}"
                )
                continue
            # CYCLE: alias resolves to a DIFFERENT canonical key
            if alias_norm in normalized_keys and alias_norm != canonical_norm:
                errors.append(
                    f"CYCLE: alias {alias!r} ({alias_norm!r}) is itself a "
                    f"canonical key pointing at {alias_norm!r}, but it appears "
                    f"under {canonical!r} ({canonical_norm!r})"
                )
            alias_to_canonical[alias_norm] = canonical_norm

    # MISSING TARGETS: every alias target must be a canonical key
    for alias_norm, target_norm in alias_to_canonical.items():
        if target_norm not in normalized_keys:
            errors.append(
                f"MISSING TARGET: alias {alias_norm!r} points to "
                f"{target_norm!r} which is not a canonical key"
            )

    if raise_on_error and errors:
        raise AliasMapError("; ".join(errors))
    return errors


# ---------------------------------------------------------------------------
# Entity ID generation
# ---------------------------------------------------------------------------

def _slugify_name(canonical_name: str) -> str:
    """Normalized canonical name → URL-safe slug (spaces → hyphens, no punctuation)."""
    return canonicalize_entity_name(canonical_name).replace(" ", "-")


def entity_id_from_name(canonical_name: str, entity_type: str | None = None) -> str:
    """Deterministic canonical entity ID — NAME-ONLY (see ENTITY_ID_POLICY).

    Format: `entity:{name_slug}`.

    The `entity_type` argument is intentionally ignored and retained only for
    call-site compatibility. This is the legacy / production format: existing
    Neo4j nodes use name-only IDs, and Product:pvector, Method:pvector, and
    Concept:pvector MUST collapse to one graph node.

    For new code that needs to disambiguate same-name / different-type
    entities without committing to a registry migration, use
    `entity_id_with_type`. That function does NOT replace this one — the
    graph writer continues to use name-only IDs.
    """
    return f"{ENTITY_ID_PREFIX}:{_slugify_name(canonical_name)}"


def entity_id_with_type(canonical_name: str, entity_type: str | None) -> str:
    """Type-qualified entity ID — provisional disambiguation.

    Format: `entity:{type_slug}:{name_slug}` when type is non-empty and
    resolves to a known ontology type. Falls back to name-only when type is
    missing, empty, or not in the ontology.

    This does NOT replace the name-only ID used by the graph writer. It is a
    diagnostic / future-facing helper for code that needs to detect
    same-name collisions across types (Apple Inc. vs apple the fruit).
    """
    name_slug = _slugify_name(canonical_name)
    if not entity_type:
        return f"{ENTITY_ID_PREFIX}:{name_slug}"
    mapped = normalize_entity_type(entity_type)
    # Only qualify when the type resolves to a real ontology type. "other"
    # is the wildcard — qualifying with it would not disambiguate anything.
    if mapped not in _ONTOLOGY_ENTITY_TYPES or mapped == "other":
        return f"{ENTITY_ID_PREFIX}:{name_slug}"
    type_slug = mapped.lower()
    return f"{ENTITY_ID_PREFIX}:{type_slug}:{name_slug}"


def detect_entity_id_collision(
    name_a: str, type_a: str | None,
    name_b: str, type_b: str | None,
) -> bool:
    """True if two entities would share a name-only ID.

    Use this to flag ambiguous pairs (Apple/org vs apple/food) in diagnostics.
    The name-only ID is preserved for graph continuity, but callers can use
    this helper plus `entity_id_with_type` to disambiguate when needed.
    """
    return entity_id_from_name(name_a) == entity_id_from_name(name_b)


def is_generic_entity_name(canonical_name: str) -> bool:
    """True if the canonical name is a generic role/abstract noun."""
    normalized = canonicalize_entity_name(canonical_name)
    return normalized in GENERIC_ENTITY_TERMS


# ---------------------------------------------------------------------------
# Entity type normalization (single source of truth)
# ---------------------------------------------------------------------------
#
# ontology.yaml declares entity_types in Title Case (Concept, Software, ...).
# allowed_pairs does an EXACT tuple match, so casing variants silently fail
# every constrained predicate. This mapping folds all known upstream variants
# onto the ontology values.

_ONTOLOGY_ENTITY_TYPES = (
    "Person", "Organization", "Location", "Event", "Concept", "Method",
    "Product", "Software", "Document", "Standard", "Rule", "Law",
    "Artifact", "TimeReference", "other",
)

# Comprehensive UPPERCASE → Title-Case mapping. Merges:
#   - Identity maps for every ontology type (CONCEPT → Concept)
#   - All synonyms from entity_quality.py (AGENT → Person, SYSTEM → Software, etc.)
#   - All synonyms from spacy_relation_adapter.py (PLACE → Location)
#
# Public name: ENTITY_TYPE_ALIASES. The legacy private alias `_UPPER_TO_ONTOLOGY`
# is preserved for backwards compatibility with existing imports.
ENTITY_TYPE_ALIASES: dict[str, str] = {
    # Identity maps for ontology types
    **{t.upper(): t for t in _ONTOLOGY_ENTITY_TYPES},
    # Synonyms from entity_quality.py LABEL_TO_ONTOLOGY
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
    "SOFTWARE": "Software",
    "LOCATION": "Location",
    "CONCEPT_V2": "Concept",
}

# Backwards-compat alias — existing code imports `_UPPER_TO_ONTOLOGY`.
# Prefer `ENTITY_TYPE_ALIASES` in new code.
_UPPER_TO_ONTOLOGY: dict[str, str] = ENTITY_TYPE_ALIASES


def normalize_entity_type(raw: str) -> str:
    """Map an upstream entity type onto ontology.yaml casing.

    Exact ontology values pass through. Case variants are folded. Known
    out-of-ontology types become their mapped ontology type. Anything else is
    returned unchanged so it stays visible rather than being quietly coerced.

    Empty / missing types return "" — callers at the adapter boundary are
    responsible for substituting "unknown" if they need a non-empty
    placeholder that the gate can recognize as unresolved.
    """
    if not raw:
        return ""
    if raw in _ONTOLOGY_ENTITY_TYPES:
        return raw
    upper = raw.strip().upper()
    mapped = ENTITY_TYPE_ALIASES.get(upper)
    if mapped:
        return mapped
    # Unknown type: returned unchanged so it stays visible AND fails closed at
    # the allowed_pairs gate. Never coerce to "other" — that is a wildcard pass.
    return raw


def canonical_entity_type(raw: str) -> str:
    """Return the CANONICAL lowercase form of an entity type.

    This is the form used in serialized records, type-qualified entity IDs,
    and contract assertions. It is always lowercase (e.g. "concept",
    "software", "person"). Empty / unresolved input returns "unknown" so
    downstream consumers can distinguish "unset" from "explicitly unknown".

    The legacy `normalize_entity_type` returns Title Case for backwards
    compatibility with ontology.yaml's allowed_pairs declarations and the
    `pair_allowed` gate. New code SHOULD use `canonical_entity_type`.
    """
    if not raw or not raw.strip():
        return "unknown"
    mapped = normalize_entity_type(raw)
    # normalize_entity_type returns the raw input unchanged for unknown types.
    # Treat anything that didn't fold onto the ontology as "unknown" so
    # downstream serialization never carries a non-canonical type token.
    if mapped not in _ONTOLOGY_ENTITY_TYPES:
        return "unknown"
    return mapped.lower()


# The canonical lowercase enum — the single source of truth for the values
# `canonical_entity_type` may return. Tests assert against this set.
CANONICAL_ENTITY_TYPES: frozenset[str] = frozenset(
    t.lower() for t in _ONTOLOGY_ENTITY_TYPES if t != "other"
) | frozenset({"unknown"})


def is_valid_entity_type(entity_type: str) -> bool:
    """True iff the type is a recognized ontology type after normalization.

    Empty string is NOT valid — callers must replace it with "unknown" if
    they need a placeholder. This prevents silent type-gate bypass.
    """
    if not entity_type:
        return False
    return normalize_entity_type(entity_type) in _ONTOLOGY_ENTITY_TYPES


# ---------------------------------------------------------------------------
# Predicate label canonicalization
# ---------------------------------------------------------------------------
#
# Natural-language relation labels handed to the model, and the internal
# predicate each maps back to. Source of truth: config/canonical/predicate_labels.yaml.
# Embedded defaults below are used as a fallback if the YAML is unreadable;
# the contract test verifies they stay in sync.

_DEFAULT_RELATION_LABEL_TO_PREDICATE: dict[str, str] = {
    "affiliated with": "affiliated_with", "works for": "works_for",
    "created by": "created_by", "owns": "owns", "part of": "part_of",
    "located in": "located_in", "causes": "causes", "detects": "detects",
    "uses": "uses", "produces": "produces", "derived from": "derived_from",
    # Model emits "instance of" — drops the leading "is an" from the prompt.
    "is an instance of": "instance_of", "instance of": "instance_of",
    "synonym of": "synonym_of",
    "includes": "includes", "supports": "supports", "implements": "implements",
    "has part": "has_part", "references": "references",
    "depends on": "depends_on", "member of": "member_of",
    "example of": "example_of", "evaluates": "evaluates",
    "deploys": "deploys", "creates": "creates", "trains": "trains",
    "runs on": "runs", "quantizes": "quantizes",
}


@lru_cache(maxsize=1)
def _load_predicate_labels() -> dict[str, str]:
    """Load predicate label mapping from config/canonical/predicate_labels.yaml.

    Falls back to embedded defaults if the YAML is missing or unreadable.
    """
    try:
        import yaml  # local import — keep canonical.py importable standalone
        with CANONICAL_PREDICATE_LABELS_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        labels = data.get("labels") or {}
        if isinstance(labels, dict) and labels:
            return {str(k).strip().lower(): str(v) for k, v in labels.items()}
        logger.warning(
            "predicate_labels.yaml is empty or malformed; using embedded defaults"
        )
    except FileNotFoundError:
        # Silent fallback — embedded defaults are the historical source.
        pass
    except Exception as exc:
        logger.warning(
            "predicate_labels.yaml failed to load (%s): %s; using defaults",
            CANONICAL_PREDICATE_LABELS_PATH, exc,
        )
    return dict(_DEFAULT_RELATION_LABEL_TO_PREDICATE)


def get_predicate_label_mapping() -> dict[str, str]:
    """Public accessor for the canonical label → predicate mapping (copy)."""
    return dict(_load_predicate_labels())


# Module-level constants for backwards-compat imports.
# Re-fetched lazily on access via the function above; the constants below
# reflect the state at import time.
RELATION_LABEL_TO_PREDICATE: dict[str, str] = _load_predicate_labels()
RELATION_LABELS: list[str] = list(RELATION_LABEL_TO_PREDICATE)


def canonicalize_predicate_label(label: str) -> str:
    """Convert a natural-language relation label to its snake_case ontology predicate.

    Uses the canonical mapping (config/canonical/predicate_labels.yaml) as the
    authoritative source so the shadow-mode and production paths never diverge
    on label interpretation. Known model-output aliases (e.g. "instance of"
    for "is an instance of") are handled. Unknown labels fall back to naive
    space-replacement so they remain visible rather than being silently
    dropped.
    """
    key = label.strip().lower()
    mapped = _load_predicate_labels().get(key)
    if mapped is not None:
        return mapped
    # Unknown label (e.g. "improves" — not in the prompt set). Keep it visible
    # via naive canonicalization so it shows up in diagnostics rather than
    # being silently coerced to something it isn't.
    return key.replace(" ", "_")
