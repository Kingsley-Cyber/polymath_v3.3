"""Relex provider shim: AdapterIR → what the Relex path expects.

The shim translates, it never interprets: entity labels register through
the SAME runtime-adapter seam config adapters use (schema_descriptions /
facets / schema_hash all flow), and native predicate surfaces extend the
relation label list plus the compiler's synonym tables — exact mappings
map, everything else stays OPEN with native semantics preserved.
"""
from __future__ import annotations

from services.ontology_adapter.adapter_ir import AdapterIR
from services.ontology_adapter.validator import validate_ir

_ACTIVE: AdapterIR | None = None
_BY_DOCUMENT: dict[str, str] = {}
_IR_BY_NAME: dict[str, AdapterIR] = {}


def active_adapter() -> AdapterIR | None:
    return _ACTIVE


def adapter_for_document(document_id: str) -> str | None:
    return _BY_DOCUMENT.get(document_id)


def extra_relation_labels() -> tuple[str, ...]:
    surfaces: list[str] = []
    if _ACTIVE is not None:
        surfaces.extend(p.surface for p in _ACTIVE.predicates)
    for ir in _IR_BY_NAME.values():
        surfaces.extend(p.surface for p in ir.predicates)
    return tuple(dict.fromkeys(surfaces))


def activate(ir: AdapterIR) -> str:
    """Register the compiled adapter for this process. Fail-closed on
    validation; returns the runtime adapter name for GRAPHIFY_FORCED_ADAPTERS."""
    global _ACTIVE
    violations = validate_ir(ir)
    if violations:
        raise ValueError(f"AdapterIR invalid: {violations}")

    from services.extraction.gliner2_cpu_provider import register_runtime_adapter
    from services.extraction.graphify_relations import predicate_compiler

    name = f"compiled:{ir.adapter_id}:{ir.schema_hash()[:12]}"
    register_runtime_adapter(
        name,
        labels={e.label: e.description for e in ir.entity_labels},
        facets={e.label: {"core": e.canonical_core, "facet": e.facet}
                for e in ir.entity_labels},
    )
    compiler = predicate_compiler()
    for predicate in ir.predicates:
        if predicate.canonical is not None and predicate.status == "exact":
            # exact native→canonical mappings only; narrower/broader/
            # ambiguous stay OPEN-side until a stricter policy exists.
            compiler.synonyms.setdefault(predicate.surface, predicate.canonical)
    _ACTIVE = ir
    return name


def activate_for_document(document_id: str, ir: AdapterIR) -> str:
    """Per-document compiled adapter (production wiring): registers the IR
    and binds it to this document; select_schema_adapters consults the
    binding. Gold-blind by construction — the IR came from the profiler."""
    name = activate(ir)
    _BY_DOCUMENT[document_id] = name
    _IR_BY_NAME[name] = ir
    return name


def deactivate() -> None:
    global _ACTIVE
    _ACTIVE = None
    _BY_DOCUMENT.clear()
    _IR_BY_NAME.clear()
