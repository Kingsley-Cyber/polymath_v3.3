"""Canonical contract tests — fail-closed invariants for the canonical layer.

These tests are the GUARDRAILS for `services/extraction/canonical.py`. They
enforce:

  1. Predicate values are snake_case (no spaces).
  2. Canonical entity types are lowercase enum values.
  3. Empty types do not silently bypass validation.
  4. Every accepted relation carries canonical endpoint IDs.
  5. Alias resolution is deterministic.
  6. Alias cycles fail closed.
  7. Same-name / different-type entities do not silently collapse (when
     type-qualified IDs are used).
  8. Repeated mentions keep separate spans but resolve to the same canonical
     entity when appropriate.
  9. Serialized canonical output is deterministic.
 10. Canonicalization functions are NOT redefined outside `canonical.py`
     (repository-scan guard).

Run: cd backend && pytest tests/extraction/test_canonical_contract.py -q
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from services.extraction.canonical import (
    CANONICAL_ENTITY_TYPES,
    CANONICAL_SCHEMA_VERSION,
    ENTITY_ID_POLICY,
    ENTITY_TYPE_ALIASES,
    RELATION_LABEL_TO_PREDICATE,
    RELATION_LABELS,
    AliasMapError,
    canonical_entity_type,
    canonicalize_entity_name,
    canonicalize_predicate_label,
    detect_entity_id_collision,
    entity_id_from_name,
    entity_id_with_type,
    is_valid_entity_type,
    normalize_entity_name,
    normalize_entity_type,
    validate_alias_map,
)
from services.extraction.relation_evidence import (
    PredicateScore,
    RelationEvidence,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "backend"
CANONICAL_MODULE_PATH = BACKEND_DIR / "services" / "extraction" / "canonical.py"


# ---------------------------------------------------------------------------
# 1. Predicate values are snake_case (no spaces)
# ---------------------------------------------------------------------------


class TestCanonicalPredicates:
    def test_no_predicate_contains_spaces(self):
        """Every value in RELATION_LABEL_TO_PREDICATE is snake_case."""
        offenders = [
            (label, pred)
            for label, pred in RELATION_LABEL_TO_PREDICATE.items()
            if " " in pred
        ]
        assert not offenders, (
            f"Canonical predicates must not contain spaces: {offenders}"
        )

    def test_predicate_canonicalization_no_spaces(self):
        """canonicalize_predicate_label never returns a value with spaces."""
        for label in RELATION_LABELS:
            canon = canonicalize_predicate_label(label)
            assert " " not in canon, (
                f"canonicalize_predicate_label({label!r}) = {canon!r} "
                f"contains a space"
            )

    def test_unknown_label_falls_back_to_snake_case(self):
        """Unknown labels are visible but snake_cased, not silently dropped."""
        assert canonicalize_predicate_label("not in mapping") == "not_in_mapping"
        assert canonicalize_predicate_label("Improves") == "improves"

    def test_predicate_label_mapping_loaded(self):
        """The mapping is non-empty and the version is declared."""
        assert RELATION_LABEL_TO_PREDICATE
        assert "uses" in RELATION_LABEL_TO_PREDICATE.values()
        assert "instance_of" in RELATION_LABEL_TO_PREDICATE.values()
        assert isinstance(CANONICAL_SCHEMA_VERSION, str) and CANONICAL_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 2. Canonical entity types are lowercase enum values
# ---------------------------------------------------------------------------


class TestCanonicalEntityTypes:
    def test_canonical_types_all_lowercase(self):
        """Every value in CANONICAL_ENTITY_TYPES is lowercase."""
        for t in CANONICAL_ENTITY_TYPES:
            assert t == t.lower(), f"Canonical entity type {t!r} is not lowercase"

    def test_canonical_entity_type_returns_lowercase(self):
        assert canonical_entity_type("Concept") == "concept"
        assert canonical_entity_type("CONCEPT") == "concept"
        assert canonical_entity_type("Software") == "software"
        assert canonical_entity_type("PERSON") == "person"
        assert canonical_entity_type("Agent") == "person"  # synonym folding

    def test_legacy_normalize_returns_title_case(self):
        """Backwards-compat: normalize_entity_type returns Title Case."""
        assert normalize_entity_type("concept") == "Concept"
        assert normalize_entity_type("SOFTWARE") == "Software"

    def test_no_alias_value_uses_mixed_case(self):
        """ENTITY_TYPE_ALIASES values are ontology Title-Case tokens.

        The ontology uses Title Case for declared types (Concept, Software,
        TimeReference). These are NOT lowercase — the lowercase form is the
        CANONICAL serialization form obtained via `canonical_entity_type()`.
        This test just verifies the values match the ontology tuple.
        """
        from services.extraction.canonical import _ONTOLOGY_ENTITY_TYPES
        valid = set(_ONTOLOGY_ENTITY_TYPES)
        for upper, title in ENTITY_TYPE_ALIASES.items():
            assert title in valid, (
                f"ENTITY_TYPE_ALIASES[{upper!r}] = {title!r} is not in the "
                f"ontology entity type set"
            )


# ---------------------------------------------------------------------------
# 3. Empty types do not silently bypass validation
# ---------------------------------------------------------------------------


class TestEmptyTypeValidation:
    def test_empty_type_is_not_valid(self):
        assert not is_valid_entity_type("")
        assert not is_valid_entity_type("   ")
        assert not is_valid_entity_type(None)  # type: ignore[arg-type]

    def test_empty_type_canonicalizes_to_unknown(self):
        """canonical_entity_type never returns empty — unknown is explicit."""
        assert canonical_entity_type("") == "unknown"
        assert canonical_entity_type("   ") == "unknown"
        assert canonical_entity_type(None) == "unknown"  # type: ignore[arg-type]

    def test_unknown_type_canonicalizes_to_unknown(self):
        """Unrecognized types do not get coerced into the ontology."""
        # Returns the raw lowercase string per normalize_entity_type's
        # "unknown stays visible" rule, BUT canonical_entity_type treats
        # anything outside the ontology as "unknown" for serialization.
        assert canonical_entity_type("WibblyWoo") == "unknown"


# ---------------------------------------------------------------------------
# 4. Every accepted relation has canonical endpoint IDs
# ---------------------------------------------------------------------------


class TestCanonicalEndpointIDs:
    def _sample_evidence(self) -> RelationEvidence:
        return RelationEvidence(
            chunk_id="c1",
            subject_id="span:0:5",
            subject_text="Apple",
            subject_type="organization",
            subject_start=0,
            subject_end=5,
            object_id="span:10:14",
            object_text="iPhone",
            object_type="product",
            object_start=10,
            object_end=14,
            predicate_scores=(PredicateScore("produces", 0.5),),
            reverse_scores={},
        )

    def test_evidence_exposes_canonical_subject_id(self):
        ev = self._sample_evidence()
        assert ev.canonical_subject_id == entity_id_from_name("Apple")

    def test_evidence_exposes_canonical_object_id(self):
        ev = self._sample_evidence()
        assert ev.canonical_object_id == entity_id_from_name("iPhone")

    def test_canonical_ids_are_prefixed_and_nonempty(self):
        ev = self._sample_evidence()
        for cid in (ev.canonical_subject_id, ev.canonical_object_id):
            assert cid.startswith("entity:")
            assert len(cid) > len("entity:")


# ---------------------------------------------------------------------------
# 5. Alias resolution is deterministic
# ---------------------------------------------------------------------------


class TestAliasDeterminism:
    def test_same_input_same_output(self):
        assert canonicalize_entity_name("OpenAI") == canonicalize_entity_name("OpenAI")
        assert canonicalize_entity_name("P-Vector") == canonicalize_entity_name("P-Vector")

    def test_alias_folds_known_variants(self):
        """Configured aliases collapse onto the canonical key."""
        # From config/canonical/entity_aliases.json
        assert canonicalize_entity_name("open ai") == canonicalize_entity_name("openai")
        assert canonicalize_entity_name("p-vector") == canonicalize_entity_name("pvector")
        assert canonicalize_entity_name("react.js") == canonicalize_entity_name("react")

    def test_alias_resolution_idempotent(self):
        """Resolving twice produces the same value as resolving once."""
        once = canonicalize_entity_name("OpenAI")
        twice = canonicalize_entity_name(once)
        assert once == twice


# ---------------------------------------------------------------------------
# 6. Alias cycles fail closed
# ---------------------------------------------------------------------------


class TestAliasCycleDetection:
    def test_cycle_detected(self):
        """A → B and B → A is rejected."""
        bad_map = {
            "alpha": ["beta"],
            "beta": ["alpha"],
        }
        errors = validate_alias_map(bad_map)
        assert errors, f"Cycle not detected: {errors}"
        assert any("CYCLE" in e for e in errors)

    def test_cycle_raises_when_requested(self):
        bad_map = {"alpha": ["beta"], "beta": ["alpha"]}
        with pytest.raises(AliasMapError):
            validate_alias_map(bad_map, raise_on_error=True)

    def test_convergence_is_not_a_cycle(self):
        """A → C and B → C is legitimate convergence, not a cycle."""
        ok_map = {
            "alpha": ["a1", "a2"],
            "beta": ["b1", "b2"],
            "common": [],
        }
        errors = validate_alias_map(ok_map)
        # No cycles in this map — common is a key, not an alias of either.
        assert not any("CYCLE" in e for e in errors)

    def test_alias_to_self_target_detected(self):
        """An alias cannot BE another canonical key (would create a cycle)."""
        # 'beta' is BOTH a canonical key AND an alias for 'alpha'.
        bad_map = {"alpha": ["beta"], "beta": []}
        errors = validate_alias_map(bad_map)
        assert any("CYCLE" in e for e in errors), (
            f"Alias-to-canonical-key cycle not detected: {errors}"
        )

    def test_loaded_alias_map_is_valid(self):
        """The production alias map (config/canonical/) has no cycles."""
        errors = validate_alias_map()
        cycle_errors = [e for e in errors if "CYCLE" in e]
        assert not cycle_errors, (
            f"Production alias map has cycles: {cycle_errors}"
        )


# ---------------------------------------------------------------------------
# 7. Same-name / different-type entities do not silently collapse
# ---------------------------------------------------------------------------


class TestEntityTypeQualification:
    def test_name_only_id_is_documented_policy(self):
        """The current policy is name-only — make it explicit."""
        assert ENTITY_ID_POLICY == "name_only"

    def test_name_only_ids_collide_by_design(self):
        """Legacy: Apple/org and apple/food share an ID (graph continuity)."""
        # This is the documented behavior. detect_entity_id_collision flags it.
        assert detect_entity_id_collision("Apple", "Organization", "apple", "Food")
        assert entity_id_from_name("Apple") == entity_id_from_name("apple")

    def test_type_qualified_ids_disambiguate(self):
        """Type-qualified IDs distinguish same-name different-type entities."""
        id_org = entity_id_with_type("Apple", "Organization")
        id_food = entity_id_with_type("apple", "Food")
        assert id_org != id_food, (
            f"Type-qualified IDs should differ: {id_org!r} vs {id_food!r}"
        )
        assert "organization" in id_org
        # "Food" is not in the ontology → falls back to name-only.
        assert id_food == entity_id_from_name("apple")

    def test_type_qualified_ids_for_known_types(self):
        """Ontology-known types produce type-qualified IDs."""
        id_sw = entity_id_with_type("Java", "Software")
        id_loc = entity_id_with_type("Java", "Location")
        assert id_sw != id_loc
        assert "software" in id_sw
        assert "location" in id_loc

    def test_type_qualified_falls_back_when_type_missing(self):
        """Missing type → name-only ID (backwards compat)."""
        assert entity_id_with_type("Apple", None) == entity_id_from_name("Apple")
        assert entity_id_with_type("Apple", "") == entity_id_from_name("Apple")

    def test_type_qualified_falls_back_for_unknown_type(self):
        """Unknown (non-ontology) type → name-only ID."""
        assert entity_id_with_type("Apple", "WibblyWoo") == entity_id_from_name("Apple")


# ---------------------------------------------------------------------------
# 8. Repeated mentions retain separate spans but resolve to same entity
# ---------------------------------------------------------------------------


class TestMentionIdentity:
    def test_repeated_mentions_same_canonical_id_different_span(self):
        ev_a = RelationEvidence(
            chunk_id="c1",
            subject_id="span:0:6", subject_text="CAPTCHA", subject_type="software",
            subject_start=0, subject_end=6,
            object_id="span:10:14", object_text="spam", object_type="concept",
            object_start=10, object_end=14,
            predicate_scores=(PredicateScore("detects", 0.5),),
            reverse_scores={},
        )
        ev_b = RelationEvidence(
            chunk_id="c1",
            subject_id="span:100:106", subject_text="CAPTCHA", subject_type="software",
            subject_start=100, subject_end=106,
            object_id="span:110:114", object_text="spam", object_type="concept",
            object_start=110, object_end=114,
            predicate_scores=(PredicateScore("detects", 0.3),),
            reverse_scores={},
        )
        # Spans differ — separate mention records.
        assert ev_a.relation_key != ev_b.relation_key
        # Canonical IDs agree — same entity.
        assert ev_a.canonical_subject_id == ev_b.canonical_subject_id
        assert ev_a.canonical_object_id == ev_b.canonical_object_id


# ---------------------------------------------------------------------------
# 9. Serialized output is deterministic
# ---------------------------------------------------------------------------


class TestSerializedDeterminism:
    def _serialize(self, name: str, etype: str, label: str) -> str:
        return json.dumps({
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "canonical_name": canonicalize_entity_name(name),
            "canonical_type": canonical_entity_type(etype),
            "entity_id": entity_id_from_name(name),
            "type_qualified_id": entity_id_with_type(name, etype),
            "predicate": canonicalize_predicate_label(label),
        }, sort_keys=True)

    def test_same_input_same_serialization(self):
        s1 = self._serialize("Apple", "Organization", "produces")
        s2 = self._serialize("Apple", "Organization", "produces")
        assert s1 == s2
        h1 = hashlib.sha256(s1.encode()).hexdigest()
        h2 = hashlib.sha256(s2.encode()).hexdigest()
        assert h1 == h2

    def test_different_input_different_serialization(self):
        s1 = self._serialize("Apple", "Organization", "produces")
        s2 = self._serialize("Google", "Organization", "uses")
        assert s1 != s2


# ---------------------------------------------------------------------------
# 10. Repository-scan: canonicalization functions are NOT redefined elsewhere
# ---------------------------------------------------------------------------


# These are the functions that MUST be defined ONLY in canonical.py.
# Other modules may IMPORT and re-export them, but they must NOT contain
# their own `def normalize_entity_name(...)` etc.
_CANONICAL_OWNED_FUNCTIONS = frozenset({
    "normalize_entity_name",
    "normalize_entity_type",
    "canonicalize_predicate_label",
    "canonicalize_entity_name",
    "entity_id_from_name",
    "entity_id_with_type",
    "resolve_entity_alias",
    "canonical_entity_type",
    "is_valid_entity_type",
    "validate_alias_map",
    "_slugify_name",
    "_load_alias_lookup",
    "_load_entity_type_overrides",
    "_load_predicate_labels",
})


def _collect_function_defs(node: ast.AST) -> set[str]:
    """Top-level FunctionDef names in a module / class body."""
    names: set[str] = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(child.name)
    return names


class TestCanonicalOwnership:
    def test_no_redefinitions_outside_canonical_py(self):
        """Repository scan: canonical functions are only DEFINED in canonical.py.

        Modules that re-export (alias via import) are fine. We only fail when
        a module contains its own `def normalize_entity_name(...)` etc.
        """
        violations: list[tuple[Path, str]] = []
        # Scan backend/services/ for python files (recursive).
        services_dir = BACKEND_DIR / "services"
        py_files = sorted(services_dir.rglob("*.py"))
        for py_file in py_files:
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            # Top-level FunctionDefs only (methods are inside ClassDef).
            top_level_defs = _collect_function_defs(tree)
            owned_here = top_level_defs & _CANONICAL_OWNED_FUNCTIONS
            # canonical.py itself is allowed to define all of them.
            if py_file.resolve() == CANONICAL_MODULE_PATH.resolve():
                continue
            for fn_name in sorted(owned_here):
                violations.append((py_file, fn_name))

        assert not violations, (
            "Canonical functions redefined outside canonical.py: "
            + "; ".join(f"{p.name}:{fn}" for p, fn in violations)
        )

    def test_no_redefinitions_in_tests_or_scripts(self):
        """Tests/scripts must not redefine canonical functions either."""
        violations: list[tuple[Path, str]] = []
        scan_dirs = [BACKEND_DIR / "tests", BACKEND_DIR / "scripts"]
        py_files: list[Path] = []
        for d in scan_dirs:
            if d.exists():
                py_files.extend(sorted(d.rglob("*.py")))
        for py_file in py_files:
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            top_level_defs = _collect_function_defs(tree)
            owned_here = top_level_defs & _CANONICAL_OWNED_FUNCTIONS
            for fn_name in sorted(owned_here):
                violations.append((py_file, fn_name))
        assert not violations, (
            "Canonical functions redefined in tests/scripts: "
            + "; ".join(f"{p.name}:{fn}" for p, fn in violations)
        )

    def test_entity_quality_uses_public_name(self):
        """entity_quality.py imports ENTITY_TYPE_ALIASES, not _UPPER_TO_ONTOLOGY."""
        src = (BACKEND_DIR / "services" / "extraction" / "entity_quality.py").read_text()
        assert "ENTITY_TYPE_ALIASES as LABEL_TO_ONTOLOGY" in src, (
            "entity_quality.py must import the PUBLIC ENTITY_TYPE_ALIASES name, "
            "not the legacy private _UPPER_TO_ONTOLOGY alias."
        )
