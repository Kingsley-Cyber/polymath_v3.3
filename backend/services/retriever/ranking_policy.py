"""Candidate weighting and final-source diversity policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from models.schemas import RetrievalTier, SourceChunk
from services.retriever.intent_policy import QueryNeed, RetrievalIntent
from services.retriever.query_grounding import (
    chunk_concept_hits,
    concept_groups,
)

# ── Phase 1: candidate-collapse hygiene ───────────────────────────────────
# Intent-adaptive distinct-document breadth — a focused (SPECIFIC) query may
# concentrate on a few authoritative sources; a broad/thematic one should fan
# out. The per-document chunk ceiling is derived from this
# (ceil(final_top_k / breadth), floored at 1), so no single document can
# collapse the context window (the "6 of 9 chunks from one book" pathology).
_DISTINCT_DOC_BREADTH: dict[QueryNeed, int] = {
    QueryNeed.SPECIFIC: 4,
    QueryNeed.BALANCED: 6,
    QueryNeed.BROAD: 8,
}
# Relative (pool-derived) noise floor on the MAIN selection for hydrated tiers:
# a non-graph chunk scoring below this fraction of the top score is the kind of
# lexical-rescued junk (a ~0.02 cross-encoder chunk lifted to ~0.18 by the
# query-grounding per-word bonus) the LLM should not see. Graph-provenance
# chunks are exempt (their value is relational); MIN_KEEP never strands a pool.
_MAIN_FLOOR_RATIO: float = 0.25
_MAIN_ABS_FLOOR: float = 0.10
_MAIN_MIN_KEEP: int = 3
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_'\-]*", re.IGNORECASE)
_TOKEN_STOPWORDS = frozenset(
    {
        "a", "about", "above", "after", "again", "against", "also", "an",
        "and", "are", "as", "at", "because",
        "before", "being", "between", "could", "does", "doing", "during",
        "each", "from", "further", "have", "having", "here", "hers",
        "itself", "just", "more", "most", "only", "other", "ours", "over",
        "same", "should", "some", "such", "than", "that", "their", "there",
        "these", "they", "this", "those", "through", "under", "until",
        "very", "what", "when", "where", "which", "while", "with", "would",
        "your",
    }
)
_REQUIRED_CONCEPT_STOPWORDS = frozenset(
    {
        "associate",
        "association",
        "connect",
        "connected",
        "define",
        "definition",
        "actually",
        "basically",
        "explain",
        "essentially",
        "link",
        "linked",
        "relate",
        "related",
        "relationship",
    }
)


@dataclass(frozen=True)
class DiversityResult:
    candidates: list[SourceChunk]
    added: int
    diagnostics: dict[str, Any] | None = None


def _provenance_retrievers(chunk: SourceChunk) -> set[str]:
    values: set[str] = set()
    for item in chunk.provenance or []:
        retriever = item.get("retriever")
        if retriever:
            values.add(str(retriever))
    return values


def _document_anchor_confidence(chunk: SourceChunk) -> float:
    best = 0.0
    source_tier = (chunk.source_tier or "").lower()
    if "document_anchor" in source_tier:
        best = 0.75
    for item in chunk.provenance or []:
        if item.get("retriever") != "document_anchor":
            continue
        try:
            best = max(best, float(item.get("document_score") or 0.0))
        except (TypeError, ValueError):
            continue
    return best


def _is_confident_document_anchor(chunk: SourceChunk) -> bool:
    return _document_anchor_confidence(chunk) >= 0.75


def _is_graph_expansion(chunk: SourceChunk) -> bool:
    """A chunk surfaced by Neo4j Mode A/B expansion (graph_mode_a/_bridge/_b).

    These are demoted by the text-similarity cross-encoder because their value
    is relational, not lexical, so the diversity pass reserves slots for them.
    """
    return (chunk.source_tier or "").lower().startswith("graph_mode")


def _is_vector_child_candidate(chunk: SourceChunk) -> bool:
    source_tier = (chunk.source_tier or "").lower()
    if "document_anchor" in source_tier or "summary" in source_tier:
        return False
    return candidate_kind(chunk) == "child" and (
        "qdrant_child" in source_tier
        or "vector" in source_tier
        or source_tier in {"child", "tier_a"}
    )


def candidate_kind(chunk: SourceChunk) -> str:
    """Classify a candidate by retrieval lane."""
    source_tier = (chunk.source_tier or "").lower()
    retrievers = _provenance_retrievers(chunk)

    if source_tier == "graph_fact_seed" or "neo4j_fact" in retrievers:
        return "fact"
    if "+lexical" in source_tier or "lexical" in retrievers or "qdrant_sparse" in retrievers:
        return "lexical"
    if "summary" in source_tier or (
        chunk.summary and (chunk.text == chunk.summary or chunk.chunk_id.endswith("_summary"))
    ):
        return "summary"
    return "child"


def _weight_for(kind: str, intent: RetrievalIntent, tier: RetrievalTier) -> float:
    if tier == RetrievalTier.qdrant_only:
        weights = {
            QueryNeed.SPECIFIC: {"child": 1.08, "summary": 0.94},
            QueryNeed.BALANCED: {"child": 1.00, "summary": 1.02},
            QueryNeed.BROAD: {"child": 0.96, "summary": 1.10},
        }
    else:
        weights = {
            QueryNeed.SPECIFIC: {
                "child": 1.06,
                "summary": 0.95,
                "lexical": 1.12,
                "fact": 1.04,
            },
            QueryNeed.BALANCED: {
                "child": 1.00,
                "summary": 1.04,
                "lexical": 1.06,
                "fact": 1.04,
            },
            QueryNeed.BROAD: {
                "child": 0.96,
                "summary": 1.12,
                "lexical": 1.02,
                "fact": 1.06,
            },
        }
    return weights.get(intent.need, {}).get(kind, 1.0)


def apply_candidate_weights(
    chunks: list[SourceChunk],
    *,
    intent: RetrievalIntent,
    tier: RetrievalTier,
) -> list[SourceChunk]:
    """Return score-adjusted copies using conservative lane weights."""
    weighted: list[SourceChunk] = []
    for chunk in chunks:
        copied = chunk.model_copy()
        copied.score = float(copied.score or 0.0) * _weight_for(
            candidate_kind(copied),
            intent,
            tier,
        )
        weighted.append(copied)
    weighted.sort(
        key=lambda c: (
            -float(c.score or 0.0),
            c.parent_id or "",
            c.doc_id or "",
            c.chunk_id or "",
        )
    )
    return weighted


def _grounded_score(
    score: float,
    *,
    hits: int,
    total: int,
    score_scale: str | None,
) -> float:
    """Conservative score adjustment for query-concept coverage.

    Bounded rerankers can emit 0..1 scores that look authoritative even when
    the sidecar failed and the original lexical score was preserved. Complete
    query-concept coverage gets a small lift; partial/no coverage gets demoted.
    """
    if total <= 0:
        return score

    scale = (score_scale or "").lower()
    bounded = scale in {"probability", "cosine"} or 0.0 <= score <= 1.0
    coverage = hits / total

    if bounded:
        if hits <= 0:
            return round(max(0.0, score * 0.30), 4)
        if hits < total:
            multiplier = 0.62 + (0.18 * coverage)
            return round(min(1.0, max(0.0, score * multiplier + 0.04 * hits)), 4)
        return round(min(1.0, max(0.0, score * 1.04 + 0.12)), 4)

    if hits <= 0:
        return round(score - 1.25, 4)
    if hits < total:
        return round(score - (0.65 * (1.0 - coverage)) + (0.10 * hits), 4)
    return round(score + 0.75 + (0.10 * min(hits, 3)), 4)


def apply_query_grounding(
    chunks: list[SourceChunk],
    *,
    query: str,
    tier: RetrievalTier,
    score_scale: str | None = None,
) -> list[SourceChunk]:
    """Prefer final evidence that covers the user's core query concepts.

    This does not add any new store to a retrieval tier. It only reorders and
    lightly rescales the candidates already retrieved by that tier, using a
    deterministic concept coverage pass. If no candidate covers any extracted
    query concept, the original ordering is preserved.
    """
    if len(chunks) <= 1:
        return chunks

    groups = concept_groups(query)
    if not groups:
        return chunks

    scored: list[tuple[SourceChunk, int, tuple[str, ...]]] = []
    group_counts: dict[str, int] = {group.key: 0 for group in groups}
    for chunk in chunks:
        hits, matched = chunk_concept_hits(chunk, groups)
        for key in matched:
            group_counts[key] = group_counts.get(key, 0) + 1
        scored.append((chunk, hits, matched))

    max_hits = max((hits for _, hits, _ in scored), default=0)
    if max_hits <= 0:
        return chunks

    total = len(groups)
    grounded: list[tuple[SourceChunk, int, float]] = []
    for chunk, hits, matched in scored:
        copied = chunk.model_copy()
        original_score = float(copied.score or 0.0)
        rarity = 0.0
        for key in matched:
            count = max(group_counts.get(key, 0), 1)
            rarity += 1.0 / count
        if total > 1:
            rarity = min(1.0, rarity)
        else:
            rarity = 0.0
        copied.score = _grounded_score(
            original_score,
            hits=hits,
            total=total,
            score_scale=score_scale,
        )
        copied.metadata = dict(copied.metadata or {})
        copied.metadata["query_grounding"] = {
            "concept_count": total,
            "matched_count": hits,
            "matched": list(matched),
            "original_score": original_score,
            "adjusted_score": copied.score,
            "tier": tier.value if hasattr(tier, "value") else str(tier),
        }
        grounded.append((copied, hits, rarity))

    grounded.sort(
        key=lambda item: (
            -item[1],
            -item[2],
            -float(item[0].score or 0.0),
            item[0].parent_id or "",
            item[0].doc_id or "",
            item[0].chunk_id or "",
        )
    )
    return [chunk for chunk, _, _ in grounded]


def _candidate_identity(chunk: SourceChunk) -> tuple[str, str, str]:
    return (
        chunk.parent_id or chunk.chunk_id or "",
        chunk.doc_id or "",
        " / ".join(chunk.heading_path or []),
    )


def _passes_diversity_threshold(candidate: SourceChunk, top_score: float) -> bool:
    score = float(candidate.score or 0.0)
    if 0.0 <= top_score <= 1.0:
        return score >= max(0.35, top_score * 0.80)
    return score >= top_score - 1.25


def _per_doc_cap_for(intent: RetrievalIntent, final_top_k: int) -> int:
    """Max chunks a single document may contribute to the final set.

    Derived from intent-adaptive distinct-document breadth:
    cap = ceil(final_top_k / breadth), floored at 1. final_top_k=8 →
    SPECIFIC:2, BALANCED:2, BROAD:1.
    """
    breadth = max(1, int(_DISTINCT_DOC_BREADTH.get(intent.need, 6)))
    return max(1, -(-int(final_top_k) // breadth))


@dataclass(frozen=True)
class _MMRPolicy:
    lambda_: float
    relevance_floor: float
    min_docs: int
    target_docs: int
    soft_doc_cap: int
    hard_doc_cap: int
    max_per_parent: int
    near_duplicate_similarity: float
    graph_reserve: int = 0
    max_same_predicate: int = 999


def _mmr_policy_for(
    *,
    tier: RetrievalTier,
    intent: RetrievalIntent,
    final_top_k: int,
    multi_corpus: bool,
) -> _MMRPolicy:
    """Tier-aware MMR policy.

    Fast Search fights vector-neighborhood collapse, Hybrid Search fights
    document/section/text/atom collapse, and Graph Augmentation also fights
    entity/fact/predicate/path collapse.
    """
    if intent.need == QueryNeed.SPECIFIC:
        min_docs, target_docs = 1, 2
        soft_doc_cap, hard_doc_cap = 2, 2
        intent_lambda_boost = 0.08
    elif intent.need == QueryNeed.BROAD:
        min_docs, target_docs = 3, 4
        soft_doc_cap, hard_doc_cap = 3, 4
        intent_lambda_boost = 0.0
    else:
        min_docs, target_docs = 2, 3
        soft_doc_cap, hard_doc_cap = 3, 4
        intent_lambda_boost = 0.03

    if multi_corpus:
        min_docs = max(min_docs, min(3, final_top_k))
        target_docs = max(target_docs, min(4, final_top_k))

    if tier == RetrievalTier.qdrant_only:
        base_lambda = 0.75
        relevance_floor = 0.90
        graph_reserve = 0
        max_same_predicate = 999
    elif tier == RetrievalTier.qdrant_mongo:
        base_lambda = 0.65
        relevance_floor = 0.85
        graph_reserve = 0
        max_same_predicate = 999
    else:
        base_lambda = 0.55
        relevance_floor = 0.80
        graph_reserve = 2 if intent.need == QueryNeed.BROAD else 1
        max_same_predicate = 3

    return _MMRPolicy(
        lambda_=min(0.88, base_lambda + intent_lambda_boost),
        relevance_floor=relevance_floor,
        min_docs=min(min_docs, final_top_k),
        target_docs=min(target_docs, final_top_k),
        soft_doc_cap=soft_doc_cap,
        hard_doc_cap=hard_doc_cap,
        max_per_parent=2,
        near_duplicate_similarity=0.88,
        graph_reserve=graph_reserve,
        max_same_predicate=max_same_predicate,
    )


def _chunk_text(chunk: SourceChunk) -> str:
    return " ".join(
        part
        for part in [
            chunk.text or "",
            chunk.summary or "",
            " / ".join(chunk.heading_path or []),
            chunk.doc_name or "",
        ]
        if part
    )


def _token_set(text: str) -> set[str]:
    out: set[str] = set()
    for token in _TOKEN_RE.findall(text or ""):
        low = token.lower().strip("-_'")
        if low in _TOKEN_STOPWORDS:
            continue
        out.add(low)
    return out


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(len(left | right), 1)


def _metadata_list(metadata: dict[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        raw = metadata.get(key)
        if raw is None:
            continue
        if isinstance(raw, (list, tuple, set)):
            values.extend(str(item) for item in raw if item is not None)
        else:
            values.append(str(raw))
    return [value.strip() for value in values if value and value.strip()]


def _candidate_retrievers(chunk: SourceChunk) -> set[str]:
    retrievers = _provenance_retrievers(chunk)
    source_tier = (chunk.source_tier or "").lower()
    if "lexical" in source_tier:
        retrievers.add("lexical")
    if "document_anchor" in source_tier:
        retrievers.add("document_anchor")
    if "graph" in source_tier or "neo4j" in source_tier:
        retrievers.add("neo4j_graph")
    if "summary" in source_tier or chunk.summary:
        retrievers.add("qdrant_summary")
    if (
        "vector" in source_tier
        or "qdrant_child" in source_tier
        or source_tier in {"child", "tier_a"}
    ):
        retrievers.add("qdrant_vector")
    return retrievers or {source_tier or "unknown"}


def _provenance_values(chunk: SourceChunk, *keys: str) -> set[str]:
    values: set[str] = set()
    for item in chunk.provenance or []:
        for key in keys:
            raw = item.get(key)
            if raw is None:
                continue
            if isinstance(raw, (list, tuple, set)):
                values.update(str(v).strip().lower() for v in raw if v)
            else:
                value = str(raw).strip().lower()
                if value:
                    values.add(value)
    return values


def _candidate_predicates(chunk: SourceChunk) -> set[str]:
    metadata = chunk.metadata or {}
    predicates = set(_metadata_list(metadata, "predicates", "predicate"))
    predicates.update(_provenance_values(chunk, "predicate", "property_name"))
    return {p.lower() for p in predicates if p}


def _candidate_entities(chunk: SourceChunk) -> set[str]:
    metadata = chunk.metadata or {}
    entities = set(
        _metadata_list(
            metadata,
            "entities",
            "entity",
            "matched_entities",
            "related_entity",
            "seed_entity",
            "neighbor_entity",
        )
    )
    entities.update(
        _provenance_values(
            chunk,
            "entity",
            "subject",
            "surface_form",
            "seed_entity",
            "neighbor_entity",
            "related_entity",
        )
    )
    return {e.lower() for e in entities if e}


def _candidate_fact_ids(chunk: SourceChunk) -> set[str]:
    metadata = chunk.metadata or {}
    facts = set(_metadata_list(metadata, "facts", "fact_ids", "fact_id"))
    facts.update(_provenance_values(chunk, "fact_id"))
    return {f.lower() for f in facts if f}


def _candidate_atoms(chunk: SourceChunk) -> set[str]:
    """Deterministic, lightweight evidence-atom tags for final selection."""
    metadata = chunk.metadata or {}
    text = _chunk_text(chunk).lower()
    atoms: set[str] = set()

    grounding = metadata.get("query_grounding")
    if isinstance(grounding, dict):
        for item in grounding.get("matched") or []:
            key = str(item).strip().lower()
            if key:
                atoms.add(f"concept:{key}")

    atoms.update(f"predicate:{p}" for p in _candidate_predicates(chunk))
    atoms.update(f"fact:{f}" for f in _candidate_fact_ids(chunk))

    if (
        " stands for " in text
        or " refers to " in text
        or " defined as " in text
        or " definition " in text
        or re.search(r"\bis\s+(?:a|an|the)\b", text)
        or re.search(
            r"\b[a-z0-9][a-z0-9_+\-]*(?:\s+[a-z0-9][a-z0-9_+\-]*){0,4}"
            r"\s+is\s+"
            r"(?!associated\b|related\b|used\b|part\b|connected\b|linked\b|"
            r"based\b|involved\b|not\b|often\b|commonly\b|typically\b|also\b)",
            text,
        )
    ):
        atoms.add("definition")
    if any(
        phrase in text
        for phrase in (
            " field of ",
            " branch of ",
            " subfield",
            " type of ",
            " kind of ",
            " category of ",
        )
    ):
        atoms.add("classification")
    if any(
        phrase in text
        for phrase in (
            "human language",
            "natural language",
            "language",
            "text",
            "speech",
            "linguistic",
        )
    ):
        atoms.add("language_focus")
    if any(
        phrase in text
        for phrase in (
            " related to ",
            " relates to ",
            " associated with ",
            " association",
            " part of ",
            " uses ",
            " used for ",
            " enables ",
            " supports ",
            " requires ",
            " causes ",
            " connects ",
        )
    ):
        atoms.add("relationship")
    if any(
        phrase in text
        for phrase in (
            " task",
            " translation",
            " classification",
            " extraction",
            " summarization",
            " question answering",
            " tokenization",
            " parsing",
            " generation",
        )
    ):
        atoms.add("methods_tasks")
    if any(
        phrase in text
        for phrase in (
            " application",
            " used in ",
            " chatbot",
            " search",
            " assistant",
            " document analysis",
            " analytics",
        )
    ):
        atoms.add("applications")
    if any(
        phrase in text
        for phrase in (
            " step",
            " process",
            " pipeline",
            " workflow",
            " retrieve",
            " hydrate",
            " rerank",
        )
    ):
        atoms.add("procedure")
    if any(
        phrase in text
        for phrase in (
            " however",
            " limitation",
            " caveat",
            " not ",
            " rather than ",
            " differs from ",
            " contrast",
        )
    ):
        atoms.add("distinction_caveat")
    if _is_graph_expansion(chunk) or (chunk.source_tier or "").lower() == "graph_fact_seed":
        atoms.add("graph_evidence")
    if not atoms:
        atoms.add(candidate_kind(chunk))
    return atoms


def _fingerprint(chunk: SourceChunk) -> dict[str, Any]:
    parent_key = chunk.parent_id or chunk.chunk_id or ""
    return {
        "doc": str(chunk.doc_id or ""),
        "parent": str(parent_key),
        "identity": _candidate_identity(chunk),
        "tokens": _token_set(_chunk_text(chunk)),
        "atoms": _candidate_atoms(chunk),
        "entities": _candidate_entities(chunk),
        "facts": _candidate_fact_ids(chunk),
        "predicates": _candidate_predicates(chunk),
        "retrievers": _candidate_retrievers(chunk),
        "graph_supported": (
            _is_graph_expansion(chunk)
            or (chunk.source_tier or "").lower() == "graph_fact_seed"
            or any("neo4j" in r or "graph" in r for r in _candidate_retrievers(chunk))
        ),
    }


def _score_normalizer(ranked: list[SourceChunk]) -> dict[int, float]:
    if not ranked:
        return {}
    raw_scores = [float(chunk.score or 0.0) for chunk in ranked]
    low = min(raw_scores)
    high = max(raw_scores)
    denom = high - low
    last = max(len(ranked) - 1, 1)
    bounded = 0.0 <= low <= high <= 1.0 and high > 0.0
    normalized: dict[int, float] = {}
    for idx, score in enumerate(raw_scores):
        rank_component = 1.0 - (idx / last)
        if bounded:
            score_component = max(0.0, min(1.0, score / high))
            normalized[idx] = (0.90 * score_component) + (0.10 * rank_component)
        elif denom > 1e-9:
            score_component = (score - low) / denom
            normalized[idx] = (0.82 * score_component) + (0.18 * rank_component)
        else:
            normalized[idx] = rank_component
    return normalized


def _passes_relevance_floor(
    *,
    idx: int,
    candidate: SourceChunk,
    fp: dict[str, Any],
    relevance_by_idx: dict[int, float],
    policy: _MMRPolicy,
    top_score: float,
) -> bool:
    """Reject fake diversity while preserving trusted non-text evidence lanes."""
    if idx == 0:
        return True
    if _is_confident_document_anchor(candidate):
        return True
    # Graph expansion can be reranker-demoted because its value is relational.
    # Keep it eligible only when it carries graph provenance/atoms, not merely
    # because it is from a different document.
    if fp["graph_supported"] and (
        fp["facts"]
        or fp["predicates"]
        or "graph_evidence" in fp["atoms"]
    ):
        return True
    raw_score = float(candidate.score or 0.0)
    if 0.0 <= raw_score <= top_score <= 1.0 and top_score > 0.0:
        return (raw_score / top_score) >= policy.relevance_floor
    return relevance_by_idx.get(idx, 0.0) >= policy.relevance_floor


def _similarity(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    tier: RetrievalTier,
) -> float:
    parent_same = 0.92 if left["parent"] and left["parent"] == right["parent"] else 0.0
    doc_same = 0.24 if left["doc"] and left["doc"] == right["doc"] else 0.0
    text_sim = _jaccard(left["tokens"], right["tokens"])
    atom_sim = _jaccard(left["atoms"], right["atoms"])

    if tier == RetrievalTier.qdrant_only:
        return max(parent_same, doc_same, text_sim)

    entity_sim = _jaccard(left["entities"], right["entities"])
    fact_sim = _jaccard(left["facts"], right["facts"])
    predicate_sim = _jaccard(left["predicates"], right["predicates"])

    if tier == RetrievalTier.qdrant_mongo_graph:
        return max(
            parent_same,
            text_sim,
            doc_same,
            0.62 * atom_sim,
            0.66 * entity_sim,
            0.78 * fact_sim,
            0.58 * predicate_sim,
        )

    return max(parent_same, text_sim, doc_same, 0.68 * atom_sim)


def _selected_counts(
    selected_fps: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    doc_counts: dict[str, int] = {}
    parent_counts: dict[str, int] = {}
    predicate_counts: dict[str, int] = {}
    for fp in selected_fps:
        doc = fp["doc"]
        parent = fp["parent"]
        if doc:
            doc_counts[doc] = doc_counts.get(doc, 0) + 1
        if parent:
            parent_counts[parent] = parent_counts.get(parent, 0) + 1
        for predicate in fp["predicates"]:
            predicate_counts[predicate] = predicate_counts.get(predicate, 0) + 1
    return doc_counts, parent_counts, predicate_counts


def _source_agreement_bonus(fp: dict[str, Any]) -> float:
    retriever_count = len(fp["retrievers"])
    if retriever_count <= 1:
        return 0.0
    return min(0.08, 0.025 * (retriever_count - 1))


def _annotated_copy(
    chunk: SourceChunk,
    *,
    fp: dict[str, Any],
    order: int,
    original_rank: int,
    score: float,
    policy: _MMRPolicy,
    selected_by: str,
    tier: RetrievalTier,
) -> SourceChunk:
    copied = chunk.model_copy()
    metadata = dict(copied.metadata or {})
    metadata["evidence_atoms"] = sorted(fp["atoms"])
    metadata["retrieval_sources"] = sorted(fp["retrievers"])
    metadata["diversity_rerank"] = {
        "order": order,
        "original_rank": original_rank,
        "selected_by": selected_by,
        "mmr_score": round(score, 4),
        "mmr_lambda": policy.lambda_,
        "tier": tier.value if hasattr(tier, "value") else str(tier),
    }
    copied.metadata = metadata
    return copied


def _required_atoms_for_query(query: str | None) -> set[str]:
    """Cheap deterministic answerability target for the selected context."""
    q = (query or "").lower()
    required: set[str] = set()

    for group in concept_groups(query or "", max_groups=4):
        key = str(getattr(group, "key", "") or "").strip().lower()
        if key and key not in _REQUIRED_CONCEPT_STOPWORDS:
            required.add(f"concept:{key}")

    if any(
        phrase in q
        for phrase in (
            "what is",
            "what are",
            "define",
            "definition",
            "meaning of",
            "stands for",
        )
    ):
        required.add("definition")
    if any(
        phrase in q
        for phrase in (
            "associate",
            "association",
            "relate",
            "related",
            "relationship",
            "connect",
            "linked",
            "between",
        )
    ):
        required.add("relationship")
    if any(
        phrase in q
        for phrase in (
            "task",
            "tasks",
            "example",
            "examples",
            "application",
            "applications",
            "include",
        )
    ):
        required.add("methods_tasks")
    if any(
        phrase in q
        for phrase in (
            "steps",
            "process",
            "procedure",
            "workflow",
            "how to",
        )
    ):
        required.add("procedure")

    return required


def _atom_counts(selected_indices: list[int], fingerprints: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for idx in selected_indices:
        for atom in fingerprints[idx]["atoms"]:
            counts[atom] = counts.get(atom, 0) + 1
    return counts


def _near_duplicate_pairs(
    selected_indices: list[int],
    fingerprints: list[dict[str, Any]],
    *,
    threshold: float,
) -> int:
    pairs = 0
    for pos, left_idx in enumerate(selected_indices):
        left = fingerprints[left_idx]
        for right_idx in selected_indices[pos + 1:]:
            right = fingerprints[right_idx]
            if _jaccard(left["tokens"], right["tokens"]) >= threshold:
                pairs += 1
    return pairs


def _evaluate_sufficiency(
    *,
    query: str | None,
    selected_indices: list[int],
    fingerprints: list[dict[str, Any]],
) -> dict[str, Any]:
    required = _required_atoms_for_query(query)
    atom_counts = _atom_counts(selected_indices, fingerprints)
    covered = {atom for atom in required if atom_counts.get(atom, 0) > 0}
    critical = required & {"definition", "relationship", "procedure"}
    missing_critical = critical - covered
    coverage = (len(covered) / len(required)) if required else 1.0
    return {
        "required_atoms": sorted(required),
        "covered_required_atoms": sorted(covered),
        "missing_atoms": sorted(required - covered),
        "missing_critical_atoms": sorted(missing_critical),
        "required_coverage": round(coverage, 4),
        "answerable": coverage >= 0.80 and not missing_critical,
        "atom_counts": atom_counts,
    }


def _repair_sufficiency(
    *,
    query: str | None,
    ranked: list[SourceChunk],
    fingerprints: list[dict[str, Any]],
    relevance_by_idx: dict[int, float],
    selected_indices: list[int],
    selected_scores: dict[int, float],
    selected_by: dict[int, str],
    policy: _MMRPolicy,
    final_top_k: int,
) -> tuple[list[int], dict[int, float], dict[int, str], dict[str, Any], int]:
    """Bounded metadata/text-atom repair using the existing ranked pool only."""
    sufficiency = _evaluate_sufficiency(
        query=query,
        selected_indices=selected_indices,
        fingerprints=fingerprints,
    )
    if sufficiency["answerable"]:
        return selected_indices, selected_scores, selected_by, sufficiency, 0

    selected = list(selected_indices)
    selected_set = set(selected)
    scores = dict(selected_scores)
    reasons = dict(selected_by)
    repair_rounds = 0

    for _round in range(2):
        missing = set(sufficiency.get("missing_atoms") or [])
        if not missing:
            break
        current_fps = [fingerprints[idx] for idx in selected]
        best_idx: int | None = None
        best_score = float("-inf")

        for idx, candidate in enumerate(ranked[: max(len(ranked), 1)]):
            if idx in selected_set:
                continue
            fp = fingerprints[idx]
            if not (fp["atoms"] & missing):
                continue
            if not _passes_relevance_floor(
                idx=idx,
                candidate=candidate,
                fp=fp,
                relevance_by_idx=relevance_by_idx,
                policy=policy,
                top_score=float(ranked[0].score or 0.0) if ranked else 0.0,
            ):
                continue
            if any(fp["parent"] and fp["parent"] == current["parent"] for current in current_fps):
                continue
            if any(
                _jaccard(fp["tokens"], current["tokens"]) >= policy.near_duplicate_similarity
                for current in current_fps
            ):
                continue

            atom_gain = len(fp["atoms"] & missing)
            score = (
                relevance_by_idx.get(idx, 0.0)
                + 0.14 * atom_gain
                + _source_agreement_bonus(fp)
                + (0.08 if fp["graph_supported"] else 0.0)
                + (0.08 if _is_confident_document_anchor(candidate) else 0.0)
            )
            if score > best_score:
                best_score = score
                best_idx = idx

        if best_idx is None:
            break

        if len(selected) < final_top_k:
            selected.append(best_idx)
            selected_set.add(best_idx)
        else:
            atom_counts = _atom_counts(selected, fingerprints)
            replace_pos = 0
            replace_score = float("inf")
            for pos, idx in enumerate(selected):
                fp = fingerprints[idx]
                unique_required = [
                    atom
                    for atom in fp["atoms"]
                    if atom in sufficiency["required_atoms"] and atom_counts.get(atom, 0) == 1
                ]
                protected_penalty = 0.45 * len(unique_required)
                score = selected_scores.get(idx, relevance_by_idx.get(idx, 0.0)) + protected_penalty
                if score < replace_score:
                    replace_score = score
                    replace_pos = pos
            removed = selected[replace_pos]
            selected_set.discard(removed)
            selected[replace_pos] = best_idx
            selected_set.add(best_idx)
            scores.pop(removed, None)
            reasons.pop(removed, None)

        scores[best_idx] = best_score
        reasons[best_idx] = "sufficiency_repair"
        repair_rounds += 1
        sufficiency = _evaluate_sufficiency(
            query=query,
            selected_indices=selected,
            fingerprints=fingerprints,
        )
        if sufficiency["answerable"]:
            break

    return selected, scores, reasons, sufficiency, repair_rounds


def select_with_diversity(
    ranked: list[SourceChunk],
    *,
    final_top_k: int,
    intent: RetrievalIntent,
    tier: RetrievalTier,
    multi_corpus: bool = False,
    query: str | None = None,
) -> DiversityResult:
    """Select final context with tier-aware MMR/atomic diversity.

    Every UI route now does the same second-pass shape:
    retrieve wide -> normalize -> dedupe upstream -> MMR/diversity here.
    The diversity target changes by tier: Fast Search spreads vector
    neighborhoods; Hybrid Search spreads documents/parents/text/atoms; Graph
    Augmentation also spreads entity/fact/predicate/path evidence.
    """
    final_top_k = max(1, int(final_top_k))
    if not ranked:
        return DiversityResult(candidates=[], added=0)

    policy = _mmr_policy_for(
        tier=tier,
        intent=intent,
        final_top_k=final_top_k,
        multi_corpus=multi_corpus,
    )
    relevance_by_idx = _score_normalizer(ranked)
    fingerprints = [_fingerprint(chunk) for chunk in ranked]
    top_score = float(ranked[0].score or 0.0)
    bounded = 0.0 <= top_score <= 1.0
    rel_floor = (
        max(_MAIN_ABS_FLOOR, top_score * _MAIN_FLOOR_RATIO) if bounded else 0.0
    )

    chosen_idx: set[int] = set()
    selected_indices: list[int] = []
    selected_scores: dict[int, float] = {}
    selected_by: dict[int, str] = {}
    covered_atoms: set[str] = set()

    def _take(idx: int, score: float, reason: str) -> None:
        selected_indices.append(idx)
        chosen_idx.add(idx)
        selected_scores[idx] = score
        selected_by[idx] = reason
        covered_atoms.update(fingerprints[idx]["atoms"])

    def _candidate_score(idx: int, *, relaxed: bool) -> float | None:
        candidate = ranked[idx]
        fp = fingerprints[idx]
        selected_fps = [fingerprints[i] for i in selected_indices]
        doc_counts, parent_counts, predicate_counts = _selected_counts(selected_fps)

        passes_relevance_floor = _passes_relevance_floor(
            idx=idx,
            candidate=candidate,
            fp=fp,
            relevance_by_idx=relevance_by_idx,
            policy=policy,
            top_score=top_score,
        )
        if not passes_relevance_floor:
            raw_score = float(candidate.score or 0.0)
            if 0.0 <= raw_score <= top_score <= 1.0 and top_score > 0.0:
                relaxed_floor_ok = (raw_score / top_score) >= 0.25
            else:
                relaxed_floor_ok = relevance_by_idx.get(idx, 0.0) >= 0.25
            can_relax_relevance_floor = (
                relaxed
                and tier != RetrievalTier.qdrant_only
                and len(selected_indices) < min(final_top_k, _MAIN_MIN_KEEP)
                and relaxed_floor_ok
            )
            if not can_relax_relevance_floor:
                return None

        if not relaxed:
            if (
                tier != RetrievalTier.qdrant_only
                and not fp["graph_supported"]
                and bounded
                and float(candidate.score or 0.0) < rel_floor
            ):
                return None
            if parent_counts.get(fp["parent"], 0) >= policy.max_per_parent:
                return None
            if doc_counts.get(fp["doc"], 0) >= policy.hard_doc_cap:
                return None
            if any(
                _jaccard(fp["tokens"], selected_fp["tokens"])
                >= policy.near_duplicate_similarity
                for selected_fp in selected_fps
            ):
                return None
            if (
                tier == RetrievalTier.qdrant_mongo_graph
                and fp["predicates"]
                and any(
                    predicate_counts.get(predicate, 0) >= policy.max_same_predicate
                    for predicate in fp["predicates"]
                )
            ):
                return None
        elif (
            tier != RetrievalTier.qdrant_only
            and not fp["graph_supported"]
            and bounded
            and len(selected_indices) >= min(final_top_k, _MAIN_MIN_KEEP)
            and float(candidate.score or 0.0) < rel_floor
        ):
            return None
        elif (
            fp["doc"]
            and doc_counts.get(fp["doc"], 0) >= policy.hard_doc_cap
            and len(selected_indices) >= min(final_top_k, _MAIN_MIN_KEEP)
        ):
            return None
        elif (
            fp["parent"]
            and parent_counts.get(fp["parent"], 0) >= policy.max_per_parent
            and len(selected_indices) >= min(final_top_k, _MAIN_MIN_KEEP)
        ):
            return None

        relevance = relevance_by_idx.get(idx, 0.0)
        redundancy = (
            max(
                _similarity(fp, selected_fp, tier=tier)
                for selected_fp in selected_fps
            )
            if selected_fps
            else 0.0
        )
        distinct_docs = len([doc for doc in doc_counts if doc])
        doc_count = doc_counts.get(fp["doc"], 0)
        parent_count = parent_counts.get(fp["parent"], 0)
        new_atoms = fp["atoms"] - covered_atoms

        new_doc_bonus = 0.0
        if fp["doc"] and doc_count == 0:
            new_doc_bonus = 0.10 if distinct_docs < policy.target_docs else 0.04
        atom_bonus = min(0.14, 0.035 * len(new_atoms))
        source_bonus = _source_agreement_bonus(fp)
        graph_bonus = 0.0
        if tier == RetrievalTier.qdrant_mongo_graph and fp["graph_supported"]:
            graph_bonus = 0.08
            if fp["facts"]:
                graph_bonus += 0.04
            if fp["predicates"]:
                graph_bonus += 0.03
        anchor_bonus = 0.32 if _is_confident_document_anchor(candidate) else 0.0

        doc_penalty = 0.0
        if fp["doc"] and doc_count > 0 and distinct_docs < policy.min_docs:
            doc_penalty += 0.08
        if doc_count >= policy.soft_doc_cap:
            doc_penalty += 0.14 * (doc_count - policy.soft_doc_cap + 1)
        parent_penalty = 0.10 * parent_count
        predicate_penalty = 0.0
        if tier == RetrievalTier.qdrant_mongo_graph and fp["predicates"]:
            predicate_penalty = 0.04 * sum(
                predicate_counts.get(predicate, 0) for predicate in fp["predicates"]
            )

        return (
            policy.lambda_ * relevance
            - (1.0 - policy.lambda_) * redundancy
            + atom_bonus
            + new_doc_bonus
            + source_bonus
            + graph_bonus
            + anchor_bonus
            - doc_penalty
            - parent_penalty
            - predicate_penalty
        )

    while len(selected_indices) < min(final_top_k, len(ranked)):
        best_idx: int | None = None
        best_score = float("-inf")
        best_relaxed = False

        for relaxed in (False, True):
            for idx in range(len(ranked)):
                if idx in chosen_idx:
                    continue
                score = _candidate_score(idx, relaxed=relaxed)
                if score is None:
                    continue
                if score > best_score:
                    best_score = score
                    best_idx = idx
                    best_relaxed = relaxed
            if best_idx is not None:
                break

        if best_idx is None:
            break
        _take(best_idx, best_score, "mmr_relaxed" if best_relaxed else "mmr")

    if tier != RetrievalTier.qdrant_only and not any(
        _is_confident_document_anchor(ranked[idx]) for idx in selected_indices
    ):
        for idx, candidate in enumerate(ranked):
            if idx in chosen_idx or not _is_confident_document_anchor(candidate):
                continue
            anchor_score = relevance_by_idx.get(idx, 0.0) + 0.18
            if len(selected_indices) < final_top_k:
                _take(idx, anchor_score, "document_anchor_reserve")
            else:
                replace_pos: int | None = None
                replace_score = float("inf")
                for pos, selected_idx in enumerate(selected_indices):
                    if _is_confident_document_anchor(ranked[selected_idx]):
                        continue
                    score = selected_scores.get(
                        selected_idx,
                        relevance_by_idx.get(selected_idx, 0.0),
                    )
                    if score < replace_score:
                        replace_score = score
                        replace_pos = pos
                if replace_pos is None:
                    break
                removed_idx = selected_indices[replace_pos]
                chosen_idx.discard(removed_idx)
                selected_indices[replace_pos] = idx
                chosen_idx.add(idx)
                selected_scores.pop(removed_idx, None)
                selected_by.pop(removed_idx, None)
                selected_scores[idx] = anchor_score
                selected_by[idx] = "document_anchor_reserve"
                covered_atoms.clear()
                for selected_idx in selected_indices:
                    covered_atoms.update(fingerprints[selected_idx]["atoms"])
            break

    if (
        tier == RetrievalTier.qdrant_mongo
        and len(selected_indices) < final_top_k
        and not any(_is_vector_child_candidate(ranked[idx]) for idx in selected_indices)
    ):
        for idx, candidate in enumerate(ranked[:final_top_k]):
            if idx in chosen_idx or not _is_vector_child_candidate(candidate):
                continue
            fp = fingerprints[idx]
            selected_fps = [fingerprints[i] for i in selected_indices]
            if any(
                fp["parent"] and fp["parent"] == selected_fp["parent"]
                for selected_fp in selected_fps
            ):
                continue
            if any(
                _jaccard(fp["tokens"], selected_fp["tokens"])
                >= policy.near_duplicate_similarity
                for selected_fp in selected_fps
            ):
                continue
            reserve_score = relevance_by_idx.get(idx, 0.0) + 0.08
            _take(idx, reserve_score, "vector_child_reserve")
            break

    if (
        tier == RetrievalTier.qdrant_only
        and len(selected_indices) < final_top_k
        and not any(candidate_kind(ranked[idx]) == "summary" for idx in selected_indices)
    ):
        for idx, candidate in enumerate(ranked[:final_top_k]):
            if idx in chosen_idx or candidate_kind(candidate) != "summary":
                continue
            raw_score = float(candidate.score or 0.0)
            if not (top_score > 0.0 and (raw_score / top_score) >= 0.75):
                continue
            fp = fingerprints[idx]
            selected_fps = [fingerprints[i] for i in selected_indices]
            if any(
                fp["parent"] and fp["parent"] == selected_fp["parent"]
                for selected_fp in selected_fps
            ):
                continue
            reserve_score = relevance_by_idx.get(idx, 0.0) + 0.06
            _take(idx, reserve_score, "vector_summary_reserve")
            break

    if tier == RetrievalTier.qdrant_mongo_graph and policy.graph_reserve > 0:
        graph_selected = sum(
            1 for idx in selected_indices if fingerprints[idx]["graph_supported"]
        )
        graph_need = max(0, policy.graph_reserve - graph_selected)
        for idx, fp in enumerate(fingerprints):
            if graph_need <= 0:
                break
            if idx in chosen_idx or not fp["graph_supported"]:
                continue
            selected_fps = [fingerprints[i] for i in selected_indices]
            if any(
                fp["parent"] and fp["parent"] == selected_fp["parent"]
                for selected_fp in selected_fps
            ):
                continue
            reserve_score = (
                relevance_by_idx.get(idx, 0.0)
                + 0.12
                + min(0.10, 0.03 * len(fp["atoms"] - covered_atoms))
            )
            if len(selected_indices) < final_top_k:
                _take(idx, reserve_score, "graph_reserve")
            else:
                replace_idx: int | None = None
                replace_score = float("inf")
                for pos, selected_idx in enumerate(selected_indices):
                    selected_fp = fingerprints[selected_idx]
                    if selected_fp["graph_supported"]:
                        continue
                    score = selected_scores.get(selected_idx, 0.0)
                    if score < replace_score:
                        replace_score = score
                        replace_idx = pos
                if replace_idx is None:
                    continue
                removed_idx = selected_indices[replace_idx]
                chosen_idx.discard(removed_idx)
                selected_indices[replace_idx] = idx
                chosen_idx.add(idx)
                selected_scores.pop(removed_idx, None)
                selected_by.pop(removed_idx, None)
                selected_scores[idx] = reserve_score
                selected_by[idx] = "graph_reserve"
                covered_atoms.clear()
                for selected_idx in selected_indices:
                    covered_atoms.update(fingerprints[selected_idx]["atoms"])
            graph_need -= 1

    selected_indices, selected_scores, selected_by, sufficiency, repair_rounds = (
        _repair_sufficiency(
            query=query,
            ranked=ranked,
            fingerprints=fingerprints,
            relevance_by_idx=relevance_by_idx,
            selected_indices=selected_indices,
            selected_scores=selected_scores,
            selected_by=selected_by,
            policy=policy,
            final_top_k=final_top_k,
        )
    )

    annotated = [
        _annotated_copy(
            ranked[idx],
            fp=fingerprints[idx],
            order=order + 1,
            original_rank=idx + 1,
            score=selected_scores.get(idx, relevance_by_idx.get(idx, 0.0)),
            policy=policy,
            selected_by=selected_by.get(idx, "mmr"),
            tier=tier,
        )
        for order, idx in enumerate(selected_indices[:final_top_k])
    ]
    diagnostics = {
        "final_k": final_top_k,
        "actual_output_count": len(annotated),
        "selected_outside_raw_top_k": sum(
            1 for idx in selected_indices[:final_top_k] if idx >= final_top_k
        ),
        "min_selected_relevance": round(
            min((relevance_by_idx.get(idx, 0.0) for idx in selected_indices[:final_top_k]), default=0.0),
            4,
        ),
        "relevance_floor": policy.relevance_floor,
        "near_duplicate_pairs": _near_duplicate_pairs(
            selected_indices[:final_top_k],
            fingerprints,
            threshold=policy.near_duplicate_similarity,
        ),
        "repair_rounds": repair_rounds,
        "sufficiency": sufficiency,
    }
    for chunk in annotated:
        metadata = dict(chunk.metadata or {})
        metadata["answer_sufficiency"] = {
            "answerable": sufficiency.get("answerable"),
            "required_coverage": sufficiency.get("required_coverage"),
            "missing_atoms": sufficiency.get("missing_atoms"),
            "repair_rounds": repair_rounds,
        }
        chunk.metadata = metadata
    added = sum(1 for idx in selected_indices[:final_top_k] if idx >= final_top_k)
    return DiversityResult(candidates=annotated, added=added, diagnostics=diagnostics)
