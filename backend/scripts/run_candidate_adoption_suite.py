#!/usr/bin/env python3
"""Candidate-adoption validation suite (dark, allowlist-only) → HARD STOP."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUT_DIR = Path(os.environ.get("CQ_OUT", "/tmp/candidate_adoption"))
AUTH_USERNAME = os.environ.get("CQ_AUTH_USERNAME", "Sambenja")
SYNTHESIS_POOL = os.environ.get(
    "CQ_SYNTHESIS_POOL_ENTRY", "deepseek-api__deepseek-v4-flash"
)

SYNTHESIS_QUOTA = {
    "direct_one_hop": 5,
    "two_hop_dependency": 5,
    "cross_domain_translation": 5,
    "contradiction": 3,
    "temporal_latest": 3,
    "ambiguous_identity": 2,
    "unsupported_transitivity": 2,
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_suite_helpers():
    import importlib.util

    path = Path(__file__).resolve().parent / "run_expanded_dark_canary.py"
    spec = importlib.util.spec_from_file_location("expanded_dark_canary", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod.DIST, mod.GSEM, mod.Q9, mod.build_suite


async def main() -> int:
    DIST, GSEM, Q9, build_suite = _load_suite_helpers()
    from config import get_settings
    from services.conversation import conversation_service
    from services.ingestion.fixture_knowledge_pipeline import assert_fixture_safe
    from services.ingestion_service import ingestion_service
    from services.retriever import complex_query_candidate_adoption as camod
    from services.retriever import retriever_orchestrator
    from services.retriever.complex_query_candidate_adoption import (
        acceptance_snapshot,
        process_enabled,
        synthesis_provider_preflight,
    )
    from services.retriever.complex_query_candidate_synthesis import (
        run_dark_candidate_synthesis,
    )
    from services.retriever.graph_authority import inspect_graph_capabilities
    from services.retriever.query_plan import build_query_plan_v2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    camod._STATE.update(
        {
            "enabled": True,
            "disabled_reason": None,
            "measured": 0,
            "rollback_events": [],
            "latency_breaches": [],
        }
    )
    ledger = Path(
        str(
            getattr(
                settings,
                "COMPLEX_QUERY_CANDIDATE_LEDGER_DIR",
                "/data/ingest-files/complex-query-candidate-adoption",
            )
        )
    )
    if ledger.is_dir():
        for name in ("comparisons.jsonl", "state.json"):
            p = ledger / name
            if p.exists():
                p.unlink()

    await conversation_service.connect()
    await ingestion_service.connect(conversation_service._db)
    db = conversation_service._db

    for cid in (GSEM, Q9):
        c = await db["corpora"].find_one({"corpus_id": cid})
        assert c, cid
        await db["corpora"].update_one(
            {"corpus_id": cid},
            {"$set": {"use_neo4j": True, "default_ingestion_config.use_neo4j": True}},
        )
        if cid == GSEM:
            await db["corpora"].update_one(
                {"corpus_id": cid},
                {
                    "$set": {
                        "production_visible": False,
                        "excluded_from_user_search": True,
                    }
                },
            )
            assert_fixture_safe(await db["corpora"].find_one({"corpus_id": cid}))
        caps = await inspect_graph_capabilities([cid])
        assert (caps.get("advertised_mode") or (c.get("graph_capabilities") or {}).get("advertised_mode")) == "graph_assertion"

    user = await db["users"].find_one({"username": AUTH_USERNAME})
    assert user
    user_id = str(user["_id"])
    preflight = await synthesis_provider_preflight(
        user_id=user_id, model_override=f"pool:{SYNTHESIS_POOL}"
    )
    print(f"[candidate] preflight={preflight.get('status')}", flush=True)

    suite = build_suite()
    syn_budget = dict(SYNTHESIS_QUOTA)
    probes: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    packets: list[dict[str, Any]] = []
    verifications: list[dict[str, Any]] = []
    synth_ok = 0
    perf_samples: list[float] = []

    for i, item in enumerate(suite, 1):
        if not process_enabled():
            print(f"[candidate] STOP early rollback={camod._STATE.get('disabled_reason')}", flush=True)
            break
        print(f"[candidate] {i}/50 {item['id']}", flush=True)
        started = time.perf_counter()
        result = await retriever_orchestrator.retrieve_planned(
            plan=build_query_plan_v2(
                item["query"],
                corpus_ids=[item["corpus_id"]],
                standalone_query=item["query"],
            ),
            corpus_ids=[item["corpus_id"]],
            retrieval_tier="qdrant_mongo_graph",
            request_user_id=user_id,
            dark_canary_query_class=item["query_class"],
            dark_canary_provider_status="skipped_shadow_retrieval_only",
            dark_canary_synthesis_preflight_status=str(preflight.get("status")),
        )
        wall = round((time.perf_counter() - started) * 1000.0, 2)
        perf_samples.append(wall)
        diag = dict(getattr(result, "diagnostics", None) or {})
        ca = dict(diag.get("candidate_adoption") or {})
        cq = dict(diag.get("complex_query") or {})
        cmp_row = dict(ca.get("comparison") or {})
        packet = dict(ca.get("context_packet") or {})
        do_syn = syn_budget.get(item["query_class"], 0) > 0 and preflight.get("status") == "ok"
        dark = None
        if do_syn and ca.get("candidate_chunks") and packet:
            dark = await run_dark_candidate_synthesis(
                query=item["query"],
                candidate_chunks=list(ca.get("candidate_chunks") or []),
                context_packet=packet,
                model=f"pool:{SYNTHESIS_POOL}",
                user_id=user_id,
            )
            syn_budget[item["query_class"]] -= 1
            if dark and not dark.get("error") and dark.get("answer"):
                synth_ok += 1
            ver = dict((dark or {}).get("verification") or {})
            cmp_row["candidate_synthesis_ms"] = (dark or {}).get("synthesis_ms")
            cmp_row["candidate_answer_chars"] = (dark or {}).get("answer_chars")
            cmp_row["verification_status"] = ver.get("verification_status")
            cmp_row["claims_total"] = ver.get("claims_total")
            cmp_row["claims_supported"] = ver.get("claims_supported")
            cmp_row["claims_unsupported"] = ver.get("claims_unsupported")
            verifications.append(
                {
                    "id": item["id"],
                    "query_class": item["query_class"],
                    "verification": ver,
                    "answer_preview": ((dark or {}).get("answer") or "")[:400],
                }
            )
        probe = {
            "id": item["id"],
            "query_class": item["query_class"],
            "corpus_id": item["corpus_id"],
            "query": item["query"],
            "wall_ms": wall,
            "effective_tier": str(getattr(result, "effective_tier", "")),
            "requested_tier": str(getattr(result, "requested_tier", "")),
            "complex_query_ran": bool(cq.get("complex_query_executor_ran")),
            "baseline_finalist_count": len(getattr(result, "chunks", None) or []),
            "candidate_adoption": {
                "enabled": ca.get("enabled"),
                "gate_reason": ca.get("gate_reason"),
                "ranking_mutated_inside_scope": ca.get("ranking_mutated_inside_scope"),
                "ranking_mutated_outside_scope": ca.get("ranking_mutated_outside_scope"),
                "user_visible_answer_mutated": ca.get("user_visible_answer_mutated"),
            },
            "comparison": cmp_row or None,
            "dark_synthesis": {
                "ran": bool(dark),
                "chars": (dark or {}).get("answer_chars"),
                "error": (dark or {}).get("error"),
                "verification_status": ((dark or {}).get("verification") or {}).get(
                    "verification_status"
                ),
                "returned_to_user": False,
            }
            if dark
            else {"ran": False},
        }
        probes.append(probe)
        if cmp_row:
            comparisons.append(cmp_row)
        if packet:
            packets.append(
                {
                    "id": item["id"],
                    "context_hash": packet.get("context_hash"),
                    "graph_status": (packet.get("graph_capability") or {}).get(
                        "execution_status"
                    ),
                    "packet": packet,
                }
            )
        await asyncio.sleep(0.02)

    # Restart replay on two probes
    replay = {}
    for item in suite[:2]:
        hashes = []
        for _ in range(2):
            result = await retriever_orchestrator.retrieve_planned(
                plan=build_query_plan_v2(
                    item["query"],
                    corpus_ids=[item["corpus_id"]],
                    standalone_query=item["query"],
                ),
                corpus_ids=[item["corpus_id"]],
                retrieval_tier="qdrant_mongo_graph",
                request_user_id=user_id,
                dark_canary_query_class=item["query_class"],
            )
            ca = (getattr(result, "diagnostics", None) or {}).get("candidate_adoption") or {}
            hashes.append(
                {
                    "comparison_hash": ((ca.get("comparison") or {}).get("result_hash")),
                    "context_hash": ((ca.get("context_packet") or {}).get("context_hash")),
                    "candidate_ids": ca.get("candidate_finalist_ids"),
                }
            )
        replay[item["id"]] = {
            "runs": hashes,
            "comparison_hash_identical": hashes[0]["comparison_hash"]
            == hashes[1]["comparison_hash"],
            "context_hash_identical": hashes[0]["context_hash"] == hashes[1]["context_hash"],
            "candidate_ids_identical": hashes[0]["candidate_ids"]
            == hashes[1]["candidate_ids"],
        }

    acceptance = acceptance_snapshot(comparisons)
    # Quality delta
    quality = {
        "obligation_coverage_improved_count": acceptance.get(
            "obligation_coverage_improved_count"
        ),
        "obligation_coverage_regressed_count": acceptance.get(
            "obligation_coverage_regressed_count"
        ),
        "candidate_added_relevant_evidence_count": acceptance.get(
            "candidate_added_relevant_evidence_count"
        ),
        "candidate_added_irrelevant_evidence_count": acceptance.get(
            "candidate_added_irrelevant_evidence_count"
        ),
        "baseline_supported_claim_rate": None,
        "candidate_supported_claim_rate": round(
            sum(int(v.get("verification", {}).get("claims_supported") or 0) for v in verifications)
            / max(
                1,
                sum(int(v.get("verification", {}).get("claims_total") or 0) for v in verifications),
            ),
            4,
        )
        if verifications
        else None,
        "baseline_citation_resolution_rate": 1.0,
        "candidate_citation_resolution_rate": round(
            sum(
                1
                for v in verifications
                if float((v.get("verification") or {}).get("citation_coverage") or 0) >= 1.0
                or int((v.get("verification") or {}).get("claims_unsupported") or 0) == 0
            )
            / max(1, len(verifications)),
            4,
        )
        if verifications
        else None,
        "contradiction_disclosure_gain": sum(
            1
            for v in verifications
            if "contradict" in str(v.get("query_class"))
            and int((v.get("verification") or {}).get("contradiction_disclosures") or 0) > 0
        ),
        "temporal_correctness_gain": None,
        "dark_synthesis_success_count": synth_ok,
        "dark_synthesis_target_min": 25,
    }
    ret_sorted = sorted(perf_samples)
    p95 = ret_sorted[int(0.95 * (len(ret_sorted) - 1))] if ret_sorted else 0.0
    performance = {
        "n": len(perf_samples),
        "candidate_retrieval_wall_p50_ms": ret_sorted[len(ret_sorted) // 2] if ret_sorted else 0,
        "candidate_retrieval_wall_p95_ms": round(p95, 2),
        "max_ms": max(perf_samples) if perf_samples else 0,
    }

    report = {
        "schema_version": "candidate_adoption_run.v1",
        "generated_at": _utcnow(),
        "authority": {
            "user_visible_complex_query": "NOT_AUTHORIZED",
            "global_subquery_planner": "DISABLED",
            "candidate_adoption": "DARK_ALLOWLIST",
        },
        "preflight": preflight,
        "distribution": dict(Counter(p["query_class"] for p in probes)),
        "n_probes": len(probes),
        "n_comparisons": len(comparisons),
        "n_synthesis": synth_ok,
        "acceptance": acceptance,
        "quality_delta": quality,
        "performance": performance,
        "restart_replay": replay,
        "hard_stop": {
            "user_visible_canary": "NOT_AUTHORIZED",
            "next": "owner decision after reviewing candidate_adoption closeout",
        },
        "probes": probes,
    }

    (OUT_DIR / "candidate_adoption_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    (OUT_DIR / "candidate_adoption_comparisons.jsonl").write_text(
        "\n".join(json.dumps(c, default=str) for c in comparisons) + ("\n" if comparisons else ""),
        encoding="utf-8",
    )
    (OUT_DIR / "candidate_context_packets.jsonl").write_text(
        "\n".join(json.dumps(p, default=str) for p in packets) + ("\n" if packets else ""),
        encoding="utf-8",
    )
    (OUT_DIR / "candidate_answer_verification.jsonl").write_text(
        "\n".join(json.dumps(v, default=str) for v in verifications)
        + ("\n" if verifications else ""),
        encoding="utf-8",
    )
    (OUT_DIR / "candidate_restart_replay.json").write_text(
        json.dumps(replay, indent=2, default=str), encoding="utf-8"
    )
    (OUT_DIR / "candidate_performance.json").write_text(
        json.dumps(performance, indent=2, default=str), encoding="utf-8"
    )
    (OUT_DIR / "candidate_acceptance_matrix.json").write_text(
        json.dumps(
            {"acceptance": acceptance, "quality_delta": quality, "distribution": DIST},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "acceptance_pass": acceptance.get("pass"),
                "n_comparisons": len(comparisons),
                "n_synthesis": synth_ok,
                "p95": performance.get("candidate_retrieval_wall_p95_ms"),
                "out": str(OUT_DIR),
            },
            indent=2,
        )
    )
    return 0 if len(comparisons) >= 40 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
