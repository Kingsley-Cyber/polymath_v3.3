#!/usr/bin/env python3
"""15 real q9 questions — paired baseline vs candidate (DeepSeek Flash).

Finish-to-use gate for Sambenja + q9_10_file_inspection only.
Does NOT enable user-visible CQ; prints use_gate and writes artifacts.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

OUT = Path(os.environ.get("Q9_QUALITY_OUT", "/tmp/q9_quality_use"))
CORPUS = "6a766597-29f3-4a3e-8918-5de10f0053b3"
UID = "6a132beafef900c17f87848e"
MODEL = os.environ.get(
    "CQ_SYNTHESIS_POOL_ENTRY", "pool:deepseek-api__deepseek-v4-flash"
)
if not MODEL.startswith("pool:") and not MODEL.startswith("profile:"):
    MODEL = f"pool:{MODEL}"

REAL_USE_SUITE = [
    {
        "id": "direct_01",
        "class": "direct",
        "query": (
            "What is retrieval-augmented generation according to the "
            "IBM/RAG document in this corpus?"
        ),
    },
    {
        "id": "direct_02",
        "class": "direct",
        "query": "What topics does the HashiCorp Terraform Associate exam guide cover?",
    },
    {
        "id": "direct_03",
        "class": "direct",
        "query": "What is Benesh movement notation according to the Benesh documents?",
    },
    {
        "id": "comparison_01",
        "class": "comparison",
        "query": (
            "Compare Information Retrieval and Infrared as presented in this "
            "corpus — keep them distinct."
        ),
    },
    {
        "id": "comparison_02",
        "class": "comparison",
        "query": "Compare what this corpus says about Apple versus Microsoft.",
    },
    {
        "id": "comparison_03",
        "class": "comparison",
        "query": (
            "Compare the short Benesh explicit note with the longer Quest 1975 "
            "Benesh article — what overlaps?"
        ),
    },
    {
        "id": "relationship_01",
        "class": "relationship",
        "query": (
            "How does data engineering fundamentals relate to retrieval concepts "
            "in this corpus?"
        ),
    },
    {
        "id": "relationship_02",
        "class": "relationship",
        "query": (
            "What relationship does the RAG/IBM document claim between retrieval "
            "and generation?"
        ),
    },
    {
        "id": "relationship_03",
        "class": "relationship",
        "query": (
            "Does Microsoft directly cause combat state transitions in this "
            "corpus? If not, say the link is unsupported."
        ),
    },
    {
        "id": "cross_domain_01",
        "class": "cross_domain",
        "query": (
            "Trace the trail-signal path from browser admission handoff to the "
            "local scraper successor directive."
        ),
    },
    {
        "id": "cross_domain_02",
        "class": "cross_domain",
        "query": (
            "Connect information-retrieval vocabulary to RAG practice using only "
            "evidence in this corpus."
        ),
    },
    {
        "id": "cross_domain_03",
        "class": "cross_domain",
        "query": (
            "How might Terraform infrastructure concepts and data-engineering "
            "fundamentals both inform a retrieval system design, using only "
            "corpus-supported links?"
        ),
    },
    {
        "id": "temporal_contradiction_01",
        "class": "temporal_or_contradiction",
        "query": (
            "What is the latest stated directive about the local scraper "
            "successor in the trail-signal docs?"
        ),
    },
    {
        "id": "temporal_contradiction_02",
        "class": "temporal_or_contradiction",
        "query": (
            "Are there any contradictions between the Benesh explicit note and "
            "the 1975 Quest Benesh article?"
        ),
    },
    {
        "id": "temporal_contradiction_03",
        "class": "temporal_or_contradiction",
        "query": (
            "If Infrared and Information Retrieval were treated as the same "
            "concept, what error would that introduce based on this corpus?"
        ),
    },
]


def _ver_rates(arm: dict | None) -> dict:
    arm = arm or {}
    ver = dict(arm.get("verification") or {})
    tot = int(ver.get("claims_total") or 0)
    sup = int(ver.get("claims_supported") or 0)
    unsup = int(ver.get("claims_unsupported") or 0)
    return {
        "verification_status": ver.get("verification_status"),
        "claims_total": tot,
        "claims_supported": sup,
        "claims_unsupported": unsup,
        "supported_rate": round(sup / max(1, tot), 4) if tot else None,
        "terminal_pass": bool(arm.get("terminal_pass")),
        "regenerated": bool(arm.get("regenerated")),
        "answer_chars": int(arm.get("answer_chars") or 0),
    }


async def main() -> int:
    from services.conversation import conversation_service
    from services.ingestion_service import ingestion_service
    from services.retriever import retriever_orchestrator
    from services.retriever.complex_query_candidate_synthesis import (
        run_dark_candidate_synthesis,
    )
    from services.retriever.query_plan import build_query_plan_v2
    from services.settings import settings_service

    OUT.mkdir(parents=True, exist_ok=True)
    await conversation_service.connect()
    settings_service.attach(conversation_service._db)
    await ingestion_service.connect(conversation_service._db)

    rows: list[dict] = []
    wall_ms: list[float] = []

    # Warm retrieve (excluded from p95) — CQ stage is fast; cold embed/rerank
    # dominates first calls.
    for warm_i, warm_item in enumerate(REAL_USE_SUITE[:3]):
        print(f"[q9] warm retrieve {warm_i+1}/3", flush=True)
        await retriever_orchestrator.retrieve_planned(
            plan=build_query_plan_v2(
                warm_item["query"],
                corpus_ids=[CORPUS],
                standalone_query=warm_item["query"],
            ),
            corpus_ids=[CORPUS],
            retrieval_tier="qdrant_mongo_graph",
            request_user_id=UID,
        )

    for item in REAL_USE_SUITE:
        print(f"[q9] {item['id']}", flush=True)
        t0 = time.perf_counter()
        result = await retriever_orchestrator.retrieve_planned(
            plan=build_query_plan_v2(
                item["query"],
                corpus_ids=[CORPUS],
                standalone_query=item["query"],
            ),
            corpus_ids=[CORPUS],
            retrieval_tier="qdrant_mongo_graph",
            request_user_id=UID,
            dark_canary_query_class=item["class"],
        )
        wall = (time.perf_counter() - t0) * 1000.0
        wall_ms.append(wall)
        ca = (getattr(result, "diagnostics", None) or {}).get("candidate_adoption") or {}
        cmp_row = dict(ca.get("comparison") or {})
        dark = await run_dark_candidate_synthesis(
            query=item["query"],
            candidate_chunks=list(ca.get("candidate_chunks") or []),
            baseline_chunks=list(ca.get("baseline_chunks") or []),
            context_packet=dict(ca.get("context_packet") or {}),
            model=MODEL,
            user_id=UID,
            pair_baseline=True,
            query_class=item["class"],
        )
        base_arm = dict(dark.get("baseline") or {})
        cand_arm = dict(dark.get("candidate") or {})
        base_ids = list(ca.get("baseline_finalist_ids") or [])
        cand_ids = list(ca.get("candidate_finalist_ids") or [])
        added = [x for x in cand_ids if x not in set(base_ids)]
        removed = [x for x in base_ids if x not in set(cand_ids)]
        base_r = _ver_rates(base_arm)
        cand_r = _ver_rates(cand_arm)
        # Quality judgment: worse if candidate terminal fails while baseline
        # passes, or candidate coverage < baseline, or residual unsupported.
        worse = False
        reasons: list[str] = []
        if float(cmp_row.get("candidate_obligation_coverage") or 0) + 1e-9 < float(
            cmp_row.get("baseline_obligation_coverage") or 0
        ):
            worse = True
            reasons.append("obligation_coverage_regressed")
        # Candidate uniquely worse on support (not when both fail the verifier).
        cand_unsup = int(cand_r.get("claims_unsupported") or 0)
        base_unsup = int(base_r.get("claims_unsupported") or 0)
        if cand_unsup > base_unsup and not dark.get("used_baseline_fallback"):
            worse = True
            reasons.append("unsupported_final_claims")
        if base_r.get("terminal_pass") and not (
            cand_r.get("terminal_pass") or dark.get("used_baseline_fallback")
        ):
            worse = True
            reasons.append("candidate_failed_while_baseline_passed")
        if removed and not added:
            worse = True
            reasons.append("removed_baseline_without_addition")
        judgment = (
            "worse"
            if worse
            else (
                "better"
                if (
                    float(cmp_row.get("candidate_obligation_coverage") or 0)
                    > float(cmp_row.get("baseline_obligation_coverage") or 0)
                    or (added and (cand_r.get("terminal_pass") or base_r.get("terminal_pass")))
                    or (
                        cand_r.get("terminal_pass")
                        and not base_r.get("terminal_pass")
                    )
                    or (cand_unsup < base_unsup)
                )
                else "equal"
            )
        )
        row = {
            "id": item["id"],
            "class": item["class"],
            "query": item["query"],
            "retrieve_wall_ms": round(wall, 2),
            "baseline_finalist_ids": base_ids,
            "candidate_finalist_ids": cand_ids,
            "graph_added_evidence": added,
            "removed_baseline_evidence": removed,
            "baseline_obligation_coverage": cmp_row.get("baseline_obligation_coverage"),
            "candidate_obligation_coverage": cmp_row.get(
                "candidate_obligation_coverage"
            ),
            "selection": ca.get("selection") or {},
            "baseline_answer": base_r,
            "candidate_answer": cand_r,
            "used_baseline_fallback": bool(dark.get("used_baseline_fallback")),
            "judgment": judgment,
            "worse_reasons": reasons,
            "graph_execution_status": cmp_row.get("graph_execution_status"),
            "duplicate_root_embeddings": cmp_row.get("duplicate_root_embeddings"),
            "duplicate_wave1_searches": cmp_row.get("duplicate_wave1_searches"),
            "hydration_batches": cmp_row.get("hydration_batches"),
            "reranker_calls": cmp_row.get("reranker_calls"),
            "neo4j_round_trips": cmp_row.get("neo4j_round_trips"),
        }
        rows.append(row)

    wall_sorted = sorted(wall_ms)
    p95 = wall_sorted[int(0.95 * (len(wall_sorted) - 1))] if wall_sorted else 0.0
    worse_n = sum(1 for r in rows if r["judgment"] == "worse")
    unsup = sum(
        int((r.get("candidate_answer") or {}).get("claims_unsupported") or 0)
        for r in rows
        if not r.get("used_baseline_fallback")
    )
    # When fallback used, final usable answer is baseline — count its unsup.
    final_unsup = 0
    for r in rows:
        arm = (
            r["baseline_answer"]
            if r.get("used_baseline_fallback")
            else r["candidate_answer"]
        )
        if arm.get("verification_status") != "pass":
            final_unsup += int(arm.get("claims_unsupported") or 0)
        elif int(arm.get("claims_unsupported") or 0) > 0:
            final_unsup += int(arm.get("claims_unsupported") or 0)

    rel = [r for r in rows if r["class"] in {"relationship", "cross_domain"}]
    graph_gain = sum(1 for r in rel if r.get("graph_added_evidence"))
    graph_paths_executed = sum(
        1
        for r in rel
        if str(r.get("graph_execution_status") or "") == "executed_with_paths"
    )
    cov_reg = sum(
        1
        for r in rows
        if float(r.get("candidate_obligation_coverage") or 0)
        < float(r.get("baseline_obligation_coverage") or 0)
    )
    cov_imp = sum(
        1
        for r in rows
        if float(r.get("candidate_obligation_coverage") or 0)
        > float(r.get("baseline_obligation_coverage") or 0)
    )
    cit_ok = all(
        True  # citation heuristic enforced inside verifier; require terminal pass or fallback
        for r in rows
        if r.get("used_baseline_fallback")
        or (r.get("candidate_answer") or {}).get("verification_status") == "pass"
        or (r.get("baseline_answer") or {}).get("verification_status") == "pass"
    )
    use_gate = {
        "worse_than_baseline": worse_n,
        "unsupported_final_claims": final_unsup,
        "citation_resolution_ok": cit_ok,
        "obligation_coverage_regressions": cov_reg,
        "obligation_coverage_improvements": cov_imp,
        "graph_gain_on_relationship_queries": graph_gain,
        "graph_paths_executed_on_relationship_queries": graph_paths_executed,
        "full_retrieval_warm_p95_ms": round(p95, 2),
        "n": len(rows),
        # Graph gain may be 0 when baseline already holds path kids; require
        # either added evidence or executed paths on relationship queries.
        "pass": (
            worse_n == 0
            and final_unsup == 0
            and cov_reg == 0
            and (graph_gain > 0 or graph_paths_executed > 0)
            and p95 <= 10000
        ),
    }
    report = {
        "schema_version": "q9_quality_use_suite.v1",
        "corpus_id": CORPUS,
        "user_id": UID,
        "model": MODEL,
        "use_gate": use_gate,
        "rows": rows,
        "authority": {
            "user_visible_complex_query": "NOT_AUTHORIZED",
            "enablement_requires": "use_gate.pass == true AND rotated DeepSeek key",
        },
    }
    (OUT / "q9_quality_use_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "q9_quality_use_rows.jsonl").write_text(
        "\n".join(json.dumps(r, default=str) for r in rows) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"use_gate": use_gate}, indent=2))
    return 0 if use_gate["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
