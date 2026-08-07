#!/usr/bin/env python3
"""Expanded dark canary — 50-query shadow suite across certified corpora.

Shadow-only: ranking_mutated must stay false. No user-visible CQ activation.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GSEM = "gsem-e2e-20260804a"
Q9 = "6a766597-29f3-4a3e-8918-5de10f0053b3"
OUT_DIR = Path(os.environ.get("CQ_OUT", "/tmp/expanded_dark_canary"))
AUTH_USERNAME = os.environ.get("CQ_AUTH_USERNAME", "Sambenja")
SYNTHESIS_POOL = os.environ.get(
    "CQ_SYNTHESIS_POOL_ENTRY", "provider-readiness-longcat-1"
)
API = os.environ.get("CQ_API", "http://127.0.0.1:8000")

# Target distribution (sums to 50)
DIST = {
    "direct_one_hop": 8,
    "two_hop_dependency": 8,
    "cross_domain_translation": 8,
    "contradiction": 6,
    "temporal_latest": 6,
    "ambiguous_identity": 4,
    "unsupported_transitivity": 4,
    "graph_fact_block": 3,
    "nonrelationship_negative_control": 3,
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _templates() -> dict[str, list[tuple[str, str]]]:
    """(corpus_id, query) variants per class."""

    g, q = GSEM, Q9
    return {
        "direct_one_hop": [
            (g, "What relationship does the C++ update loop have to combat state transitions?"),
            (g, "How does the combat update loop relate to state transitions in one hop?"),
            (g, "Cite the direct link between the C++ loop and combat transitions."),
            (g, "What does the update loop do immediately before combat state changes?"),
            (q, "What relationship does RAG have to IBM in this corpus? Cite children."),
            (q, "What is Information Retrieval according to the IR document?"),
            (q, "What does the Apple and Microsoft document say about Microsoft?"),
            (q, "What is Benesh Movement Notation per the explicit Benesh source?"),
        ],
        "two_hop_dependency": [
            (g, "Which intermediary connects RAG to Information Retrieval and then to generation?"),
            (g, "Trace a two-hop dependency from combat loop to preserved mechanics."),
            (g, "What depends on the C++ update loop before Luau translation can preserve mechanics?"),
            (g, "List the intermediate dependency between combat state and the translation target."),
            (q, "Which intermediary connects RAG to Information Retrieval concepts?"),
            (q, "Trace dependencies from data engineering fundamentals to retrieval concepts."),
            (q, "What connects Terraform exam topics to infrastructure identity concepts?"),
            (q, "Find a two-hop path from browser admission handoff to local scraper successor."),
        ],
        "cross_domain_translation": [
            (g, "How should a C++ combat update loop be translated into Roblox Luau while preserving the original mechanics?"),
            (g, "Translate combat loop semantics from C++ into Luau; cite source and target evidence."),
            (g, "Map C++ combat state transitions onto Roblox Luau equivalents with child citations."),
            (g, "Cross-domain: preserve mechanics when moving the update loop from C++ to Luau."),
            (g, "What must stay invariant when translating the combat update loop into Luau?"),
            (q, "How do RAG and Information Retrieval concepts translate across the IBM and IR docs?"),
            (q, "Translate Infrared sensing terminology without conflating it with Information Retrieval."),
            (q, "How should Benesh Movement Notation concepts be kept distinct when discussing movement writing?"),
        ],
        "contradiction": [
            (g, "Does the corpus contradict whether movement writing is identical to Benesh Movement Notation?"),
            (g, "Are there opposing claims about Benesh Movement Notation identity? Disclose both sides."),
            (g, "Contradiction check: movement writing versus Benesh — cite opposing children."),
            (q, "Does the corpus contradict Infrared versus Information Retrieval identity claims?"),
            (q, "Are Benesh sources consistent about movement notation identity? Disclose conflicts."),
            (q, "Contradiction: does any source claim Microsoft combat integration that others deny?"),
        ],
        "temporal_latest": [
            (g, "What is the latest state of combat transitions, excluding superseded deprecated APIs?"),
            (g, "Prefer the most recent assertion about the combat update loop and state."),
            (g, "What is the latest stated relationship between combat state and the update loop?"),
            (q, "What is the latest stated relationship between RAG and Information Retrieval?"),
            (q, "Prefer the most recent trail-signal directive over older handoff notes."),
            (q, "What is the latest Benesh-related claim, excluding superseded wording?"),
        ],
        "ambiguous_identity": [
            (g, "Explain IR in this corpus — Information Retrieval versus Infrared."),
            (g, "Do not merge IR senses: distinguish Information Retrieval from Infrared."),
            (q, "Explain IR in this corpus — Information Retrieval versus Infrared."),
            (q, "Keep Information Retrieval and Infrared as distinct identities; cite each."),
        ],
        "unsupported_transitivity": [
            (g, "Does Microsoft directly cause combat state transitions in this corpus?"),
            (g, "Is there an explicit Microsoft→combat causal path, or would that be invented?"),
            (q, "Does Microsoft directly cause combat state transitions in this corpus?"),
            (q, "Would linking Microsoft to combat via unsupported transitivity be valid here?"),
        ],
        "graph_fact_block": [
            (g, "Return only stored Neo4j graph facts establishing that Infrared equals Information Retrieval."),
            (q, "Return only stored Neo4j graph facts establishing that Infrared equals Information Retrieval."),
            (q, "Return only qualified Neo4j facts that Microsoft equals combat state — if none, say blocked."),
        ],
        "nonrelationship_negative_control": [
            (g, "What color is commonly associated with stop signs? Answer from general knowledge if absent."),
            (q, "What is 2+2? Answer briefly without using graph relationships."),
            (q, "Define the word 'serendipity' without citing corpus relationship paths."),
        ],
    }


def build_suite() -> list[dict[str, Any]]:
    templates = _templates()
    suite: list[dict[str, Any]] = []
    for klass, n in DIST.items():
        variants = templates[klass]
        for i in range(n):
            corpus_id, query = variants[i % len(variants)]
            # Prefer unique wording when enough variants exist
            if i < len(variants):
                corpus_id, query = variants[i]
            suite.append(
                {
                    "id": f"{klass}_{i+1:02d}",
                    "query_class": klass,
                    "corpus_id": corpus_id,
                    "query": query,
                }
            )
    assert len(suite) == 50, len(suite)
    return suite


async def _synthesis_preflight(db: Any, user_id: str) -> dict[str, Any]:
    """One LongCat chat ping — provider readiness, not retrieval."""

    import urllib.request

    from services.auth import AuthService

    token = AuthService().create_access_token(user_id, AUTH_USERNAME)
    body = {
        "message": "Reply with exactly: PONG",
        "corpus_ids": [GSEM],
        "retrieval_tier": "qdrant_mongo_graph",
        "web_search": False,
        "overrides": {"model": f"pool:{SYNTHESIS_POOL}"},
    }
    req = urllib.request.Request(
        f"{API}/api/chat",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
    )
    status = "failed"
    err = None
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read().decode("utf-8", "replace")
            if "PONG" in data or '"type": "done"' in data or '"type":"done"' in data:
                status = "ok"
            elif "402" in data or "401" in data or "Payment Required" in data:
                status = "provider_credit_error"
                err = data[:240]
            else:
                status = "degraded"
                err = data[:240]
    except Exception as exc:  # noqa: BLE001
        status = "transport_error"
        err = f"{type(exc).__name__}: {exc}"[:240]
    return {
        "status": status,
        "model": f"pool:{SYNTHESIS_POOL}",
        "error": err,
        "user_id": user_id,
    }


async def _run_one(
    *,
    item: dict[str, Any],
    user_id: str,
    settings: Any,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    from services.retriever import retriever_orchestrator
    from services.retriever.query_plan import build_query_plan_v2

    orch = retriever_orchestrator
    started = time.monotonic()
    result = await orch.retrieve_planned(
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
        dark_canary_synthesis_preflight_status=str(preflight.get("status") or "unknown"),
    )
    wall = round((time.monotonic() - started) * 1000.0, 2)
    diag = dict(getattr(result, "diagnostics", None) or {})
    dc = diag.get("dark_canary") or {}
    cq = diag.get("complex_query") or {}
    comparison = dc.get("comparison") if isinstance(dc, dict) else None
    return {
        "id": item["id"],
        "query_class": item["query_class"],
        "corpus_id": item["corpus_id"],
        "query": item["query"],
        "wall_ms": wall,
        "requested_tier": str(getattr(result, "requested_tier", "")),
        "effective_tier": str(getattr(result, "effective_tier", "")),
        "downgrade_reason": str(getattr(result, "downgrade_reason", "") or ""),
        "complex_query_ran": bool(cq.get("complex_query_executor_ran")),
        "ranking_mutated": bool(cq.get("ranking_mutated")),
        "dark_canary_gate": {
            "enabled": dc.get("enabled") if isinstance(dc, dict) else None,
            "gate_reason": dc.get("gate_reason") if isinstance(dc, dict) else None,
        },
        "finalist_count": len(getattr(result, "chunks", None) or []),
        "comparison": comparison,
        "error": dc.get("error") if isinstance(dc, dict) else None,
    }


def _production_gap_delta(acceptance: dict[str, Any], rows: list[dict]) -> dict[str, Any]:
    return {
        "schema_version": "expanded_dark_production_gap_delta.v1",
        "generated_at": _utcnow(),
        "shadow_expansion": {
            "acceptance_pass": bool(acceptance.get("pass")),
            "n": acceptance.get("n"),
            "ranking_mutations": acceptance.get("ranking_mutations"),
            "production_answer_mutations": acceptance.get("production_answer_mutations"),
        },
        "still_missing_for_user_visible_canary": {
            "CQ_winners_reach_finalists": False,
            "graph_added_chunks_reach_finalists": False,
            "ranking_mutated_inside_candidate_scope": False,
            "ranking_mutated_outside_candidate_scope": False,
            "canonical_context_packet_count": "not_1_on_synthesis_path",
            "context_packet_consumed_by_synthesis": False,
            "verification_controls_final_answer": False,
            "graph_downgrade_states_are_explicit": "partial_via_dark_ledger",
            "synthesis_provider_preflight": True,
            "duplicate_wave1_searches": "not_reproven_this_run",
        },
        "evidence": {
            "all_ranking_mutated_false": all(
                not bool((r.get("comparison") or {}).get("production_ranking_mutated"))
                for r in rows
                if r.get("comparison")
            ),
            "graphify_gaps_still_open": ["G02", "G03", "G04", "G10"],
        },
        "authority": {
            "user_visible_complex_query": "NOT_AUTHORIZED",
            "global_subquery_planner": "DISABLED",
            "production_migration": "NOT_AUTHORIZED",
        },
    }


async def main() -> int:
    from config import get_settings
    from services.conversation import conversation_service
    from services.ingestion.fixture_knowledge_pipeline import assert_fixture_safe
    from services.ingestion_service import ingestion_service
    from services.retriever.complex_query_dark_canary import (
        acceptance_snapshot,
        process_canary_enabled,
    )
    from services.retriever.graph_authority import inspect_graph_capabilities

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    # Clear prior ledger for this expansion run
    ledger = Path(
        str(
            getattr(
                settings,
                "COMPLEX_QUERY_DARK_CANARY_LEDGER_DIR",
                "/data/ingest-files/complex-query-dark-canary",
            )
        )
    )
    if ledger.is_dir():
        for name in ("comparisons.jsonl", "state.json"):
            p = ledger / name
            if p.exists():
                p.unlink()

    # Reset in-process rollback state
    from services.retriever import complex_query_dark_canary as dcmod

    dcmod._STATE.update(
        {
            "enabled": True,
            "disabled_reason": None,
            "measured_queries": 0,
            "rollback_events": [],
            "latency_breaches": [],
        }
    )

    # Attach the same service singletons retrieve_planned uses in /api/chat.
    await conversation_service.connect()
    await ingestion_service.connect(conversation_service._db)
    db = conversation_service._db
    assert db is not None
    assert getattr(ingestion_service, "qdrant_client", None) is not None

    # Certify corpora
    corpus_cert = {}
    for cid in (GSEM, Q9):
        c = await db["corpora"].find_one({"corpus_id": cid})
        assert c, f"missing corpus {cid}"
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
            c = await db["corpora"].find_one({"corpus_id": cid})
            assert_fixture_safe(c)
        caps = await inspect_graph_capabilities([cid])
        stored = (c or {}).get("graph_capabilities") or {}
        corpus_cert[cid] = {
            "name": c.get("name"),
            "advertised_mode": caps.get("advertised_mode") or stored.get("advertised_mode"),
            "entity_ready": stored.get("entity_ready") or caps.get("entity_ready"),
            "assertion_ready": stored.get("assertion_ready") or caps.get("assertion_ready"),
            "counts": caps.get("counts"),
            "production_visible": c.get("production_visible"),
            "excluded_from_user_search": c.get("excluded_from_user_search"),
        }
        assert corpus_cert[cid]["advertised_mode"] == "graph_assertion", corpus_cert[cid]
        assert corpus_cert[cid]["entity_ready"] is True
        assert corpus_cert[cid]["assertion_ready"] is True

    user = await db["users"].find_one({"username": AUTH_USERNAME})
    assert user, "missing allowlisted user"
    user_id = str(user["_id"])

    preflight = await _synthesis_preflight(db, user_id)
    print(f"[expanded-dark] synthesis_preflight={preflight['status']}", flush=True)

    suite = build_suite()
    counts = Counter(x["query_class"] for x in suite)
    assert dict(counts) == DIST, counts

    probes: list[dict[str, Any]] = []
    for i, item in enumerate(suite, 1):
        if not process_canary_enabled():
            print(
                f"[expanded-dark] STOP early at {i}: rolled back "
                f"{dcmod._STATE.get('disabled_reason')}",
                flush=True,
            )
            break
        print(
            f"[expanded-dark] {i}/50 {item['id']} corpus={item['corpus_id'][:12]}…",
            flush=True,
        )
        try:
            row = await _run_one(
                item=item,
                user_id=user_id,
                settings=settings,
                preflight=preflight,
            )
        except Exception as exc:  # noqa: BLE001
            row = {
                **item,
                "error": f"{type(exc).__name__}: {exc}"[:400],
                "comparison": None,
                "complex_query_ran": False,
                "ranking_mutated": False,
            }
        probes.append(row)
        await asyncio.sleep(0.05)

    comparisons = [
        p["comparison"] for p in probes if isinstance(p.get("comparison"), dict)
    ]
    acceptance = acceptance_snapshot(comparisons)
    gap = _production_gap_delta(acceptance, probes)

    report = {
        "schema_version": "expanded_dark_canary_run.v1",
        "generated_at": _utcnow(),
        "corpora": corpus_cert,
        "users": [{"user_id": user_id, "username": AUTH_USERNAME}],
        "synthesis_preflight": preflight,
        "distribution_requested": DIST,
        "distribution_executed": dict(
            Counter(p.get("query_class") for p in probes)
        ),
        "n_probes": len(probes),
        "n_comparisons": len(comparisons),
        "canary_still_enabled": process_canary_enabled(),
        "rollback_state": {
            "enabled": dcmod._STATE.get("enabled"),
            "disabled_reason": dcmod._STATE.get("disabled_reason"),
            "rollback_events": dcmod._STATE.get("rollback_events"),
        },
        "acceptance": acceptance,
        "production_gap_delta": gap,
        "contract": {
            "baseline_path_authoritative": True,
            "dark_path_shadow_only": True,
            "ranking_mutated": False,
            "user_visible_answer_mutated": False,
            "user_visible_complex_query": "NOT_AUTHORIZED",
            "global_subquery_planner": "DISABLED",
        },
        "hard_stop": {
            "next": "owner decision on user-visible canary — do not auto-promote",
            "visible_canary_prerequisites_met": False,
        },
        "probes": probes,
    }

    (OUT_DIR / "expanded_dark_canary_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    (OUT_DIR / "expanded_dark_canary_comparisons.jsonl").write_text(
        "\n".join(json.dumps(c, default=str) for c in comparisons)
        + ("\n" if comparisons else ""),
        encoding="utf-8",
    )
    (OUT_DIR / "expanded_dark_production_gap_delta.json").write_text(
        json.dumps(gap, indent=2, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "acceptance": acceptance,
                "preflight": preflight.get("status"),
                "n_comparisons": len(comparisons),
                "out": str(OUT_DIR),
            },
            indent=2,
            default=str,
        )
    )
    if len(comparisons) < 40:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
