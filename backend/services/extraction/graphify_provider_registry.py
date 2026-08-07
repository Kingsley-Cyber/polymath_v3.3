"""Fail-closed registry for the canonical Graphify entity provider."""

from __future__ import annotations

from services.extraction.gliner2_cpu_provider import GLiNER2CPUProvider, get_gliner2_cpu_provider

CANONICAL_PROVIDER = "fastino/gliner2-base-v1"


def canonical_entity_provider() -> GLiNER2CPUProvider:
    return get_gliner2_cpu_provider()

