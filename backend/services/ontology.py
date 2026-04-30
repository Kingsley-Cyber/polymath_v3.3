"""Ontology contract for Ghost B extraction and graph write repair.

This module is the machine-readable source of truth for the lightweight
Polymath ontology. The data lives in `ontology.yaml`, formatted as strict JSON
so the backend can load it without adding a YAML parser dependency.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

ONTOLOGY_PATH = Path(__file__).with_name("ontology.yaml")


@lru_cache(maxsize=1)
def load_ontology() -> dict[str, Any]:
    """Load and validate the ontology contract once per process."""
    data = json.loads(ONTOLOGY_PATH.read_text(encoding="utf-8"))
    entity_names = [item["name"] for item in data.get("entity_types", [])]
    relation_names = [item["name"] for item in data.get("relations", [])]
    if not entity_names:
        raise ValueError("ontology has no entity_types")
    if not relation_names:
        raise ValueError("ontology has no relations")
    if len(entity_names) != len(set(entity_names)):
        raise ValueError("ontology entity_types contain duplicates")
    if len(relation_names) != len(set(relation_names)):
        raise ValueError("ontology relations contain duplicates")
    sentinel = str(data.get("relation_sentinel") or "related_to")
    if relation_names[-1] != sentinel:
        raise ValueError("ontology relation_sentinel must be the final relation")
    allowed_scopes = {"core", "related_to_repair", "ontology_expansion"}
    for item in data.get("relations", []):
        scope = item.get("governance_scope")
        if scope not in allowed_scopes:
            raise ValueError(
                f"ontology relation {item.get('name')!r} has invalid governance_scope {scope!r}"
            )
    return data


def ontology_version() -> str:
    return str(load_ontology().get("version") or "")


def entity_sentinel() -> str:
    return str(load_ontology().get("entity_sentinel") or "other")


def relation_sentinel() -> str:
    return str(load_ontology().get("relation_sentinel") or "related_to")


def entity_type_names() -> list[str]:
    return [str(item["name"]) for item in load_ontology()["entity_types"]]


def relation_type_names() -> list[str]:
    return [str(item["name"]) for item in load_ontology()["relations"]]


def entity_gloss_map() -> dict[str, str]:
    return {
        str(item["name"]): str(item.get("gloss") or "")
        for item in load_ontology()["entity_types"]
    } | {entity_sentinel(): "nothing above fits"}


def relation_gloss_map() -> dict[str, str]:
    return {
        str(item["name"]): str(item.get("gloss") or "")
        for item in load_ontology()["relations"]
    }


def relation_family_map() -> dict[str, str]:
    return {
        str(item["name"]): str(item.get("family") or "WeakAssociation")
        for item in load_ontology()["relations"]
    }


def relation_domain_range_map() -> dict[str, dict[str, list[str]]]:
    return {
        str(item["name"]): {
            "subject_types": [str(v) for v in item.get("subject_types", [])],
            "object_types": [str(v) for v in item.get("object_types", [])],
        }
        for item in load_ontology()["relations"]
        if item.get("subject_types") or item.get("object_types")
    }


def relation_alias_tuple_map() -> dict[str, tuple[str, bool]]:
    aliases = load_ontology().get("relation_aliases") or {}
    return {
        str(alias): (str(rule.get("predicate") or relation_sentinel()), bool(rule.get("reverse")))
        for alias, rule in aliases.items()
        if isinstance(rule, dict)
    }


def relation_definition(name: str) -> dict[str, Any]:
    for item in load_ontology()["relations"]:
        if item.get("name") == name:
            return item
    return {}


def render_relation_decision_block(vocab: list[str] | None) -> str:
    """Render compact predicate boundary rules for the Ghost B prompt."""
    if not vocab:
        return ""
    allowed = set(vocab)
    lines = ["\nPredicate decision rules:"]
    for item in load_ontology()["relations"]:
        name = str(item.get("name") or "")
        if name not in allowed or name == relation_sentinel():
            continue
        tests = [str(v) for v in item.get("discrimination_tests", []) if str(v).strip()]
        if not tests:
            continue
        direction = str(item.get("canonical_direction") or "").strip()
        definition = str(item.get("definition") or item.get("gloss") or "").strip()
        compact_tests = " ".join(tests[:3])
        direction_text = f" Direction: {direction}." if direction else ""
        lines.append(f"- {name}: {definition}.{direction_text} Test: {compact_tests}")
    if len(lines) == 1:
        return ""
    lines.append(
        "- When two predicates seem plausible, choose the one whose test is most specific. "
        f"Use {relation_sentinel()} only after these tests fail."
    )
    return "\n".join(lines)


def object_kind_compatible(
    predicate: str,
    subject_identity: dict | None,
    object_identity: dict | None,
) -> bool | None:
    """Return object-kind compatibility when the ontology has a specific rule.

    `True`/`False` means the rule is applicable and decisive. `None` means the
    ontology has no object-kind rule for the predicate, so callers should fall
    back to their broader entity/domain validation.
    """
    relation = relation_definition(predicate)
    subject_kinds = set(relation.get("subject_object_kinds") or [])
    object_kinds = set(relation.get("object_object_kinds") or [])
    if not subject_kinds and not object_kinds:
        return None
    subject_kind = str((subject_identity or {}).get("object_kind") or "")
    object_kind = str((object_identity or {}).get("object_kind") or "")
    if subject_kinds and not subject_kind:
        return None
    if object_kinds and not object_kind:
        return None
    subject_ok = not subject_kinds or subject_kind in subject_kinds
    object_ok = not object_kinds or object_kind in object_kinds
    return subject_ok and object_ok
