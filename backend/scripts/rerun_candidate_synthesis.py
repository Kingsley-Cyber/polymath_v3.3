#!/usr/bin/env python3
"""Re-run dark candidate synthesis for quota classes using DeepSeek Flash."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

OUT = Path("/tmp/candidate_adoption")
SYNTHESIS_QUOTA = {
    "direct_one_hop": 5,
    "two_hop_dependency": 5,
    "cross_domain_translation": 5,
    "contradiction": 3,
    "temporal_latest": 3,
    "ambiguous_identity": 2,
    "unsupported_transitivity": 2,
}
# DeepSeek platform API (api.deepseek.com) via encrypted settings key.
MODEL = os.environ.get(
    "CQ_SYNTHESIS_POOL_ENTRY",
    "pool:deepseek-api__deepseek-v4-flash",
)
if not MODEL.startswith("pool:") and not MODEL.startswith("profile:"):
    MODEL = f"pool:{MODEL}"
UID = "6a132beafef900c17f87848e"


async def main() -> int:
    from services.conversation import conversation_service
    from services.ingestion_service import ingestion_service
    from services.retriever import retriever_orchestrator
    from services.retriever.complex_query_candidate_adoption import acceptance_snapshot
    from services.retriever.complex_query_candidate_synthesis import (
        run_dark_candidate_synthesis,
    )
    from services.retriever.query_plan import build_query_plan_v2
    from services.settings import settings_service

    await conversation_service.connect()
    settings_service.attach(conversation_service._db)
    await ingestion_service.connect(conversation_service._db)

    report = json.loads((OUT / "candidate_adoption_report.json").read_text())
    budget = dict(SYNTHESIS_QUOTA)
    verifications: list[dict] = []
    synth_ok = 0

    for p in report["probes"]:
        klass = p["query_class"]
        if budget.get(klass, 0) <= 0:
            continue
        print(f"[resynth] {p['id']}", flush=True)
        result = await retriever_orchestrator.retrieve_planned(
            plan=build_query_plan_v2(
                p["query"],
                corpus_ids=[p["corpus_id"]],
                standalone_query=p["query"],
            ),
            corpus_ids=[p["corpus_id"]],
            retrieval_tier="qdrant_mongo_graph",
            request_user_id=UID,
            dark_canary_query_class=klass,
        )
        ca = (getattr(result, "diagnostics", None) or {}).get("candidate_adoption") or {}
        chunks = list(ca.get("candidate_chunks") or [])
        packet = dict(ca.get("context_packet") or {})
        cmp_row = dict(ca.get("comparison") or p.get("comparison") or {})
        dark = await run_dark_candidate_synthesis(
            query=p["query"],
            candidate_chunks=chunks,
            context_packet=packet,
            model=MODEL,
            user_id=UID,
        )
        budget[klass] -= 1
        ver = dict((dark or {}).get("verification") or {})
        if dark and dark.get("answer") and not dark.get("error"):
            synth_ok += 1
        cmp_row["candidate_synthesis_ms"] = dark.get("synthesis_ms")
        cmp_row["candidate_answer_chars"] = dark.get("answer_chars")
        cmp_row["verification_status"] = ver.get("verification_status")
        cmp_row["claims_total"] = ver.get("claims_total")
        cmp_row["claims_supported"] = ver.get("claims_supported")
        cmp_row["claims_unsupported"] = ver.get("claims_unsupported")
        p["comparison"] = cmp_row
        p["dark_synthesis"] = {
            "ran": True,
            "chars": dark.get("answer_chars"),
            "error": dark.get("error"),
            "verification_status": ver.get("verification_status"),
            "returned_to_user": False,
            "model": MODEL,
        }
        verifications.append(
            {
                "id": p["id"],
                "query_class": klass,
                "verification": ver,
                "answer_preview": (dark.get("answer") or "")[:400],
                "error": dark.get("error"),
            }
        )

    comparisons = [p["comparison"] for p in report["probes"] if p.get("comparison")]
    acceptance = acceptance_snapshot(comparisons)
    report["acceptance"] = acceptance
    report["n_synthesis"] = synth_ok
    report["synthesis_model"] = MODEL
    qd = dict(report.get("quality_delta") or {})
    qd["dark_synthesis_success_count"] = synth_ok
    tot = sum(int(v.get("verification", {}).get("claims_total") or 0) for v in verifications)
    sup = sum(int(v.get("verification", {}).get("claims_supported") or 0) for v in verifications)
    qd["candidate_supported_claim_rate"] = round(sup / max(1, tot), 4)
    report["quality_delta"] = qd

    (OUT / "candidate_adoption_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    (OUT / "candidate_adoption_comparisons.jsonl").write_text(
        "\n".join(json.dumps(c, default=str) for c in comparisons) + "\n",
        encoding="utf-8",
    )
    (OUT / "candidate_answer_verification.jsonl").write_text(
        "\n".join(json.dumps(v, default=str) for v in verifications) + "\n",
        encoding="utf-8",
    )
    (OUT / "candidate_acceptance_matrix.json").write_text(
        json.dumps(
            {"acceptance": acceptance, "quality_delta": qd},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "acceptance_pass": acceptance.get("pass"),
                "synth_ok": synth_ok,
                "final_generated_answer_verified": acceptance.get(
                    "final_generated_answer_verified"
                ),
                "unsupported_final_claims": acceptance.get("unsupported_final_claims"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
