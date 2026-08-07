"""Structured context packet for cross-domain synthesis (directive §context_packet)."""

from __future__ import annotations

from typing import Any, Sequence


def build_cross_domain_context_packet(
    *,
    plan: Any,
    chunks: Sequence[Any],
    diagnostics: dict[str, Any] | None = None,
    protected_chunk_ids: Sequence[str] | None = None,
    vocabulary_resolution: dict[str, Any] | None = None,
    summaries: Sequence[Any] | None = None,
) -> dict[str, Any]:
    diag = dict(diagnostics or {})
    protected = {str(x) for x in (protected_chunk_ids or []) if str(x)}
    curation = (diag.get("cross_domain_curation") or {}) if isinstance(
        diag.get("cross_domain_curation"), dict
    ) else {}
    if not protected and isinstance(curation.get("protected_chunk_ids"), list):
        protected = {str(x) for x in curation["protected_chunk_ids"] if str(x)}

    child_rows: list[dict[str, Any]] = []
    for chunk in chunks:
        cid = str(getattr(chunk, "chunk_id", "") or "")
        if not cid:
            continue
        meta = getattr(chunk, "metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        child_rows.append(
            {
                "evidence_id": f"{getattr(chunk, 'corpus_id', '')}:{getattr(chunk, 'doc_id', '')}:{cid}",
                "chunk_id": cid,
                "document_id": str(getattr(chunk, "doc_id", "") or ""),
                "parent_id": str(getattr(chunk, "parent_id", "") or ""),
                "corpus_id": str(getattr(chunk, "corpus_id", "") or ""),
                "heading_path": meta.get("heading_path"),
                "domain": meta.get("domain") or getattr(chunk, "domain", None),
                "selection_reason": (
                    "protected_anchor" if cid in protected else "mmr_selected"
                ),
                "retrieval_lanes": list(meta.get("planned_lanes") or []),
                "supported_obligations": list(meta.get("query_obligations") or []),
                "source_tier": str(getattr(chunk, "source_tier", "") or "child"),
                "schema_used_as_citation": False,
            }
        )

    summary_rows = []
    for s in summaries or []:
        summary_rows.append(
            {
                "summary_id": str(getattr(s, "chunk_id", "") or getattr(s, "id", "") or ""),
                "linked_child_ids": list(
                    (getattr(s, "metadata", None) or {}).get("source_chunk_ids") or []
                ),
                "answer_authority": False,
            }
        )

    required_domains = list(getattr(plan, "required_domains", ()) or ())
    domain_ids = []
    for d in required_domains:
        domain_ids.append(getattr(d, "domain_id", None) or str(d))

    covered_domains = sorted(
        {
            str(row.get("domain") or "")
            for row in child_rows
            if row.get("domain")
        }
    )
    unsupported = [d for d in domain_ids if d and d not in covered_domains]

    return {
        "conversation_context": {
            "original_query": getattr(plan, "original_query", ""),
            "standalone_query": getattr(plan, "standalone_query", ""),
        },
        "query_plan": {
            "complexity": getattr(plan, "complexity", ""),
            "concepts": list(getattr(plan, "concepts", ()) or ()),
            "required_domains": domain_ids,
        },
        "vocabulary_resolution": vocabulary_resolution or diag.get("vocabulary") or {},
        "routing_summaries": summary_rows,
        "protected_child_evidence": [
            row for row in child_rows if row["selection_reason"] == "protected_anchor"
        ],
        "mmr_selected_child_evidence": [
            row for row in child_rows if row["selection_reason"] == "mmr_selected"
        ],
        "cross_domain_bridges": [],
        "coverage": {
            "child_count": len(child_rows),
            "protected_count": len(protected),
            "domains_covered": covered_domains,
            "required_domains": domain_ids,
        },
        "unresolved_obligations": unsupported,
        "contradictions": [],
        "authority": {
            "child_chunks": True,
            "summaries": False,
            "vocabulary_records": False,
            "schema_records_as_citations": 0,
        },
    }
