"""Configurable weighted RRF weights for cross-domain fusion.

Maps directive lane names onto existing planned-pool retriever keys and
legacy retrieve() lane keys. Direct original evidence remains the strongest
single weight.
"""

from __future__ import annotations

from typing import Any


def planned_retriever_rrf_weights(settings: Any | None = None) -> dict[str, float]:
    if settings is None:
        from config import get_settings

        settings = get_settings()
    direct = float(getattr(settings, "CROSS_DOMAIN_RRF_WEIGHT_DIRECT", 1.0))
    weights = {
        "dense": direct,
        "summary": float(getattr(settings, "CROSS_DOMAIN_RRF_WEIGHT_SUMMARY_GUIDED", 0.65)),
        "lexical": float(getattr(settings, "CROSS_DOMAIN_RRF_WEIGHT_MONGO_LEXICAL", 0.80)),
        "graph": float(getattr(settings, "CROSS_DOMAIN_RRF_WEIGHT_GRAPH_CHILD", 0.85)),
        # Vocabulary-linked / expansion pools reuse dense or these aliases:
        "trusted_canonical": float(
            getattr(settings, "CROSS_DOMAIN_RRF_WEIGHT_TRUSTED_CANONICAL", 0.80)
        ),
        "linked_child": float(
            getattr(settings, "CROSS_DOMAIN_RRF_WEIGHT_LINKED_CHILD", 0.65)
        ),
    }
    # Invariant: direct lane is the strongest single weight.
    other_max = max(v for k, v in weights.items() if k != "dense")
    if direct + 1e-9 < other_max:
        weights["dense"] = other_max + 0.05
    return weights


def legacy_lane_rrf_weights(settings: Any | None = None) -> dict[str, float]:
    if settings is None:
        from config import get_settings

        settings = get_settings()
    planned = planned_retriever_rrf_weights(settings)
    return {
        "b": planned["dense"],
        "anchor": planned["linked_child"],
        "lex": planned["lexical"],
        "graph": planned["graph"],
        "a": planned["summary"],
        "fact": planned["graph"],
    }


def rrf_k(settings: Any | None = None) -> float:
    if settings is None:
        from config import get_settings

        settings = get_settings()
    return float(getattr(settings, "CROSS_DOMAIN_RRF_K", 60.0))
