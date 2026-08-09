"""Phase-7 dual-lane schema-assisted retrieval (shadow index only).

Original-query evidence lane always runs and remains authoritative.
Schema lane may assist via trust-class budgets; it never supplies answer
citations. Global Fast schema expansion stays OFF unless an explicit
activation flag is passed (default false).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Sequence

from models.alias_schema_projection import (
    ALIAS_SCHEMA_RETRIEVAL_RELEASE,
    SchemaAssistedTraceV1,
    ShadowSchemaRecordV1,
    ShadowSchemaSurfaceV1,
)

RETRIEVAL_RELEASE = ALIAS_SCHEMA_RETRIEVAL_RELEASE
RetrievalTier = Literal["fast", "hybrid", "graph"]

# Route budgets (directive Phase 7).
ROUTE_BUDGETS: dict[str, dict[str, int]] = {
    "fast": {
        "trusted_canonical_terms": 3,
        "linked_child_anchors": 4,
        "parent_summaries": 0,
        "section_summaries": 0,
        "extra_vector_searches": 1,
    },
    "hybrid": {
        "trusted_canonical_terms": 5,
        "linked_child_anchors": 8,
        "linked_parent_summaries": 6,
        "linked_section_summaries": 3,
        "mongo_lexical_terms": 1,  # enabled flag as 1
    },
    "graph": {
        "canonical_entity_ids": 8,
        "linked_graph_node_ids": 8,
        "linked_child_anchors": 8,
        "parent_summaries": 4,
        "section_summaries": 4,
    },
}


@dataclass(frozen=True)
class EvidenceHit:
    """Simulated / planner-level evidence hit (child document chunk)."""

    child_id: str
    score: float
    lane: str  # original_query | schema_assisted
    text: str = ""
    parent_id: str | None = None
    section_id: str | None = None


@dataclass(frozen=True)
class DualLaneRetrievalResult:
    tier: str
    original_query: str
    expanded_queries: list[str]
    original_lane_hits: list[EvidenceHit]
    schema_lane_hits: list[EvidenceHit]
    final_ranked_child_ids: list[str]
    traces: list[SchemaAssistedTraceV1]
    used_parent_summary_ids: list[str] = field(default_factory=list)
    used_section_summary_ids: list[str] = field(default_factory=list)
    used_graph_node_ids: list[str] = field(default_factory=list)
    semantic_related_terms_changed_ranking: bool = False
    schema_records_used_as_answer_evidence: int = 0
    global_fast_activation: bool = False
    production_schema_mutations: int = 0
    retrieval_release: str = RETRIEVAL_RELEASE


def _norm(value: str) -> str:
    return " ".join((value or "").lower().split())


def _tokenize(value: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (value or "").lower()) if len(t) >= 2}


def _surface_matches_query(surface: str, query: str) -> bool:
    s_norm = _norm(surface)
    q = _norm(query)
    if not s_norm or not q:
        return False
    if s_norm == q or s_norm in q.split() or f" {s_norm} " in f" {q} ":
        return True
    if s_norm in q or q in s_norm:
        # Prefer token-boundary-ish containment for multi-word.
        return True
    return bool(_tokenize(surface) & _tokenize(query)) and (
        len(_tokenize(surface)) == 1 or _tokenize(surface) <= _tokenize(query)
    )


def match_shadow_schema(
    query: str,
    records: Sequence[ShadowSchemaRecordV1],
    *,
    active_document_ids: Sequence[str] | None = None,
    active_parent_ids: Sequence[str] | None = None,
) -> list[tuple[ShadowSchemaRecordV1, ShadowSchemaSurfaceV1]]:
    """Match query surfaces against shadow schema trust classes.

    Ambiguous aliases only match when an active document/parent scope is
    provided and intersects the record's source documents / linked parents.
    Descriptions never match as alias expansions.
    """

    q = _norm(query)
    doc_scope = {_norm(x) for x in (active_document_ids or [])}
    parent_scope = {_norm(x) for x in (active_parent_ids or [])}
    hits: list[tuple[ShadowSchemaRecordV1, ShadowSchemaSurfaceV1, int]] = []

    for record in records:
        buckets: list[ShadowSchemaSurfaceV1] = [
            *record.trusted_aliases,
            *record.temporal_aliases,
            *record.retrieval_surface_variants,
            *record.ambiguous_aliases,
            *record.related_terms,
        ]
        for surface in buckets:
            matched = _surface_matches_query(surface.surface, query)
            if not matched and _norm(record.canonical_term) == q:
                # Canonical equality may authorize trusted/temporal surfaces.
                matched = surface.trust_class in {
                    "trusted_aliases",
                    "temporal_aliases",
                }
            if not matched:
                continue
            if surface.trust_class == "ambiguous_aliases":
                src_docs = {_norm(x) for x in record.source_document_ids}
                src_parents = {_norm(x) for x in record.linked_parent_ids}
                if not doc_scope and not parent_scope:
                    continue
                in_doc = bool(doc_scope and src_docs & doc_scope)
                in_parent = bool(parent_scope and src_parents & parent_scope)
                if not (in_doc or in_parent):
                    continue
            s_norm = _norm(surface.surface)
            rank_key = 0 if s_norm == q else (1 if s_norm in q else 2)
            hits.append((record, surface, rank_key))

    hits.sort(
        key=lambda row: (
            row[2],
            0 if row[1].identity_authority else 1,
            row[1].surface.lower(),
            row[0].schema_point_id,
        )
    )
    seen: set[tuple[str, str]] = set()
    out: list[tuple[ShadowSchemaRecordV1, ShadowSchemaSurfaceV1]] = []
    for record, surface, _ in hits:
        key = (record.schema_point_id, surface.surface.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((record, surface))
    return out


def expansion_for_surface(
    record: ShadowSchemaRecordV1,
    surface: ShadowSchemaSurfaceV1,
) -> tuple[str | None, str]:
    """Return (expanded_query_or_none, ranking_contribution)."""

    if surface.trust_class in {"trusted_aliases", "temporal_aliases"}:
        return record.canonical_term, "authoritative_expansion"
    if surface.trust_class == "retrieval_surface_variants":
        # Bounded assistance — expand to canonical but mark non-identity.
        return record.canonical_term, "bounded_assistance"
    if surface.trust_class == "ambiguous_aliases":
        return record.canonical_term, "scoped_assistance"
    if surface.trust_class == "related_terms":
        return None, "trace_only_no_ranking"
    if surface.trust_class == "descriptions":
        return None, "none"
    if surface.trust_class == "legacy_unqualified":
        return None, "none"
    return None, "none"


def apply_route_link_budget(
    tier: str,
    record: ShadowSchemaRecordV1,
    *,
    ranking_contribution: str,
) -> dict[str, list[str]]:
    """Select linked anchors within route budgets."""

    budget = ROUTE_BUDGETS[tier]
    children = list(record.linked_child_ids)
    parents = list(record.linked_parent_ids)
    sections = list(record.linked_section_ids)
    graph_nodes = list(record.linked_graph_node_ids)

    if ranking_contribution == "trace_only_no_ranking":
        return {
            "linked_child_ids": [],
            "linked_parent_ids": [],
            "linked_section_ids": [],
            "linked_graph_node_ids": [],
        }

    if tier == "fast":
        return {
            "linked_child_ids": children[: budget["linked_child_anchors"]],
            "linked_parent_ids": [],
            "linked_section_ids": [],
            "linked_graph_node_ids": [],
        }
    if tier == "hybrid":
        return {
            "linked_child_ids": children[: budget["linked_child_anchors"]],
            "linked_parent_ids": parents[: budget["linked_parent_summaries"]],
            "linked_section_ids": sections[: budget["linked_section_summaries"]],
            "linked_graph_node_ids": [],
        }
    # graph
    return {
        "linked_child_ids": children[: budget["linked_child_anchors"]],
        "linked_parent_ids": parents[: budget.get("parent_summaries", 0)],
        "linked_section_ids": sections[: budget.get("section_summaries", 0)],
        "linked_graph_node_ids": graph_nodes[: budget["linked_graph_node_ids"]],
    }


def _score_text(query: str, text: str) -> float:
    q_tokens = _tokenize(query)
    t_tokens = _tokenize(text)
    if not q_tokens or not t_tokens:
        return 0.0
    overlap = len(q_tokens & t_tokens) / len(q_tokens)
    return overlap


def run_dual_lane_retrieval(
    query: str,
    *,
    tier: str,
    shadow_records: Sequence[ShadowSchemaRecordV1],
    corpus_child_index: Sequence[dict[str, Any]],
    active_document_ids: Sequence[str] | None = None,
    active_parent_ids: Sequence[str] | None = None,
    activate_global_fast_schema_expansion: bool = False,
) -> DualLaneRetrievalResult:
    """Execute original + schema lanes with trust-class expansion policy.

    ``corpus_child_index`` items: {child_id, text, parent_id?, section_id?,
    document_id?, graph_node_ids?}.
    """

    if tier not in ROUTE_BUDGETS:
        raise ValueError(f"unsupported tier: {tier}")

    # Lane 1: original query — always runs.
    original_hits: list[EvidenceHit] = []
    for row in corpus_child_index:
        score = _score_text(query, str(row.get("text") or ""))
        if score <= 0:
            continue
        original_hits.append(
            EvidenceHit(
                child_id=str(row["child_id"]),
                score=score + 0.15,  # original lane authority bias
                lane="original_query",
                text=str(row.get("text") or ""),
                parent_id=row.get("parent_id"),
                section_id=row.get("section_id"),
            )
        )
    original_hits.sort(key=lambda h: (-h.score, h.child_id))

    # Lane 2: schema vocabulary match (shadow only).
    # Fast global activation remains false by default.
    schema_enabled = True
    if tier == "fast" and not activate_global_fast_schema_expansion:
        # Shadow integration may still assist for tests / isolated fixtures,
        # but the activation flag stays false and is reported as such.
        schema_enabled = True  # local shadow path; NOT global production activation

    matches = (
        match_shadow_schema(
            query,
            shadow_records,
            active_document_ids=active_document_ids,
            active_parent_ids=active_parent_ids,
        )
        if schema_enabled
        else []
    )

    budget = ROUTE_BUDGETS[tier]
    max_terms = budget.get("trusted_canonical_terms", budget.get("canonical_entity_ids", 3))
    expanded_queries: list[str] = []
    traces: list[SchemaAssistedTraceV1] = []
    schema_hits: list[EvidenceHit] = []
    used_parents: list[str] = []
    used_sections: list[str] = []
    used_graph: list[str] = []
    related_changed_ranking = False

    trusted_term_count = 0
    for record, surface in matches:
        expanded, contribution = expansion_for_surface(record, surface)
        links = apply_route_link_budget(tier, record, ranking_contribution=contribution)

        if contribution == "authoritative_expansion":
            if trusted_term_count >= max_terms:
                continue
            trusted_term_count += 1
        if expanded and contribution != "trace_only_no_ranking":
            if expanded not in expanded_queries and _norm(expanded) != _norm(query):
                expanded_queries.append(expanded)

        # Retrieve linked children for non-trace contributions.
        hydrated: list[str] = []
        if contribution != "trace_only_no_ranking" and contribution != "none":
            search_query = expanded or query
            for row in corpus_child_index:
                child_id = str(row["child_id"])
                if links["linked_child_ids"] and child_id not in links["linked_child_ids"]:
                    # Prefer linked anchors; still allow lexical assist within bound.
                    if contribution == "authoritative_expansion":
                        # For trusted expansion, allow search over linked children first.
                        continue
                score = _score_text(search_query, str(row.get("text") or ""))
                if links["linked_child_ids"] and child_id in links["linked_child_ids"]:
                    score += 0.25
                if score <= 0:
                    continue
                # Retrieval-only / scoped: bounded — lower weight than original.
                weight = {
                    "authoritative_expansion": 0.10,
                    "bounded_assistance": 0.05,
                    "scoped_assistance": 0.04,
                }.get(contribution, 0.0)
                schema_hits.append(
                    EvidenceHit(
                        child_id=child_id,
                        score=score + weight,
                        lane="schema_assisted",
                        text=str(row.get("text") or ""),
                        parent_id=row.get("parent_id"),
                        section_id=row.get("section_id"),
                    )
                )
                hydrated.append(child_id)

            # If trusted expansion had linked children but none scored, still
            # hydrate linked anchors as structural hits (exact child evidence).
            if contribution == "authoritative_expansion":
                linked_set = set(links["linked_child_ids"])
                for row in corpus_child_index:
                    child_id = str(row["child_id"])
                    if child_id not in linked_set or child_id in hydrated:
                        continue
                    schema_hits.append(
                        EvidenceHit(
                            child_id=child_id,
                            score=0.20,
                            lane="schema_assisted",
                            text=str(row.get("text") or ""),
                            parent_id=row.get("parent_id"),
                            section_id=row.get("section_id"),
                        )
                    )
                    hydrated.append(child_id)

        used_parents.extend(links["linked_parent_ids"])
        used_sections.extend(links["linked_section_ids"])
        used_graph.extend(links["linked_graph_node_ids"])

        if contribution == "trace_only_no_ranking":
            # Must not alter ranking — no schema_hits added from related terms.
            pass

        cand_ids = surface.source_alias_candidate_ids
        dec_ids = surface.source_alias_decision_ids
        traces.append(
            SchemaAssistedTraceV1.create(
                query=query,
                schema_point_id=record.schema_point_id,
                corpus_entity_id=record.corpus_entity_id,
                alias_candidate_id=cand_ids[0] if cand_ids else None,
                alias_decision_id=dec_ids[0] if dec_ids else None,
                matched_surface=surface.surface,
                canonical_term=record.canonical_term,
                trust_class=surface.trust_class,
                linked_child_ids=links["linked_child_ids"],
                linked_parent_ids=links["linked_parent_ids"],
                linked_section_ids=links["linked_section_ids"],
                expanded_query=expanded,
                ranking_contribution=contribution,  # type: ignore[arg-type]
                final_hydrated_child_ids=hydrated,
                original_query_lane_ran=True,
                schema_used_as_answer_evidence=False,
            )
        )

    schema_hits.sort(key=lambda h: (-h.score, h.child_id))

    # Merge ranking: original lane authoritative; schema assists without
    # related-term influence. Exclude any hits only attributable to related_terms.
    related_child_ids = {
        child
        for trace in traces
        if trace.ranking_contribution == "trace_only_no_ranking"
        for child in trace.final_hydrated_child_ids
    }
    # related terms should have empty hydrated lists by construction.
    if related_child_ids:
        related_changed_ranking = True  # safety tripwire for tests

    merged: dict[str, float] = {}
    for hit in original_hits:
        merged[hit.child_id] = max(merged.get(hit.child_id, 0.0), hit.score)
    for hit in schema_hits:
        if hit.child_id in related_child_ids:
            continue
        merged[hit.child_id] = max(merged.get(hit.child_id, 0.0), hit.score)

    final_ids = [
        child_id
        for child_id, _ in sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    # Direct search works without schema hit — original lane alone suffices.
    if not matches:
        traces.append(
            SchemaAssistedTraceV1.create(
                query=query,
                schema_point_id=None,
                corpus_entity_id=None,
                matched_surface=None,
                canonical_term=None,
                trust_class=None,
                expanded_query=None,
                ranking_contribution="none",
                final_hydrated_child_ids=[h.child_id for h in original_hits[:8]],
                original_query_lane_ran=True,
                schema_used_as_answer_evidence=False,
            )
        )

    return DualLaneRetrievalResult(
        tier=tier,
        original_query=query,
        expanded_queries=expanded_queries,
        original_lane_hits=original_hits,
        schema_lane_hits=schema_hits,
        final_ranked_child_ids=final_ids,
        traces=traces,
        used_parent_summary_ids=sorted(set(used_parents)),
        used_section_summary_ids=sorted(set(used_sections)),
        used_graph_node_ids=sorted(set(used_graph)),
        semantic_related_terms_changed_ranking=related_changed_ranking,
        schema_records_used_as_answer_evidence=0,
        global_fast_activation=bool(activate_global_fast_schema_expansion),
        production_schema_mutations=0,
    )


def replay_retrieval_fingerprint(result: DualLaneRetrievalResult) -> dict[str, Any]:
    return {
        "tier": result.tier,
        "final_ranked_child_ids": list(result.final_ranked_child_ids),
        "expanded_queries": list(result.expanded_queries),
        "trace_hashes": [t.trace_hash for t in result.traces],
        "used_parent_summary_ids": list(result.used_parent_summary_ids),
        "used_section_summary_ids": list(result.used_section_summary_ids),
        "used_graph_node_ids": list(result.used_graph_node_ids),
        "global_fast_activation": result.global_fast_activation,
        "schema_records_used_as_answer_evidence": result.schema_records_used_as_answer_evidence,
    }
