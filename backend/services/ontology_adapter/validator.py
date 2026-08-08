"""AdapterIR validator — the compiler's own gate.

Every IR is validated before activation: mappings land inside the frozen
canonical sets or are honestly OPEN; nothing nearest-vague; identity is
stable. Violations fail closed — an invalid adapter never activates.
"""
from __future__ import annotations

from services.ontology_adapter.adapter_ir import MAPPING_STATUSES, AdapterIR


def validate_ir(ir: AdapterIR) -> list[str]:
    """Return a list of violations; empty list = valid."""
    from services.extraction.canonical import canonical_entity_type
    from services.extraction.graphify_relations import predicate_compiler

    violations: list[str] = []
    allowed_predicates = predicate_compiler().allowed_predicates
    seen_labels = set()
    for entity in ir.entity_labels:
        if entity.label in seen_labels:
            violations.append(f"duplicate entity label {entity.label!r}")
        seen_labels.add(entity.label)
        core = canonical_entity_type(entity.canonical_core)
        if not core:
            violations.append(f"{entity.label!r}: empty canonical core")
        if not entity.description.strip():
            violations.append(f"{entity.label!r}: empty description")
    for predicate in ir.predicates:
        if predicate.status not in MAPPING_STATUSES:
            violations.append(f"{predicate.surface!r}: bad status {predicate.status!r}")
        if predicate.canonical is None and predicate.status != "OPEN":
            violations.append(f"{predicate.surface!r}: unmapped but status != OPEN")
        if predicate.canonical is not None and predicate.canonical not in allowed_predicates:
            violations.append(
                f"{predicate.surface!r}: canonical {predicate.canonical!r} outside frozen set"
            )
    if not ir.open_policy.get("preserve_unknown_entities", False):
        violations.append("open_policy must preserve unknown entities")
    if not ir.open_policy.get("preserve_unknown_predicates", False):
        violations.append("open_policy must preserve unknown predicates")
    if ir.schema_hash() != ir.schema_hash():
        violations.append("schema hash unstable")
    return violations
