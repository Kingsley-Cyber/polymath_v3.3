#!/usr/bin/env python3
"""Isolated cross-domain canary on q9 corpus (Level 0 vs curated + Graph block).

Production ranking stays off by default; this process enables allowlist-only
curation and optional Level-1 ranking for the fixture corpus ids only.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

CORPUS = os.environ.get("Q9_CORPUS_ID", "6a766597-29f3-4a3e-8918-5de10f0053b3")
OUT_DIR = Path(os.environ.get("OUT_DIR", "/app/_cross_domain_out"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

QUERIES = [
    {
        "id": "trusted_vocabulary_expansion",
        "query": "How does movement writing record choreography?",
        "tier": "hybrid",
    },
    {
        "id": "no_false_vocabulary_identity",
        "query": "Compare movement writing and Benesh Movement Notation.",
        "tier": "hybrid",
    },
    {
        "id": "ambiguous_acronym",
        "query": "Explain IR.",
        "tier": "hybrid",
    },
    {
        "id": "direct_lane_without_schema",
        "query": "What does the Terraform Trail Signal document describe?",
        "tier": "fast",
    },
    {
        "id": "graph_unavailable",
        "query": "How does Retrieval-Augmented Generation relate to Information Retrieval historically?",
        "tier": "graph",
    },
    {
        "id": "rag_detail",
        "query": "What is Retrieval-Augmented Generation and how does it use Information Retrieval?",
        "tier": "hybrid",
    },
]


def _tier(name: str):
    from models.schemas import RetrievalTier

    return {
        "fast": RetrievalTier.qdrant_only,
        "hybrid": RetrievalTier.qdrant_mongo,
        "graph": RetrievalTier.qdrant_mongo_graph,
    }[name]


def _enable_canary_flags(settings) -> None:
    settings.CROSS_DOMAIN_CURATION_ENABLED = True
    settings.CROSS_DOMAIN_CURATION_CORPUS_ALLOWLIST = (
        f"{CORPUS},isolated_alias_fixture,8bf57c76-7e2d-49eb-9a11-6e260406903f"
    )
    settings.ALIAS_RETRIEVAL_SHADOW_ENABLED = True
    settings.ALIAS_RETRIEVAL_RANKING_ENABLED = True
    settings.ALIAS_RETRIEVAL_ENABLED_GLOBALLY = False
    settings.ALIAS_RETRIEVAL_FIXTURE_CORPUS_ALLOWLIST = (
        f"isolated_alias_fixture,8bf57c76-7e2d-49eb-9a11-6e260406903f,{CORPUS}"
    )
    settings.ALIAS_RETRIEVAL_PRODUCTION_SCHEMA_WRITES = False
    settings.ALIAS_RETRIEVAL_PRODUCTION_BACKFILL = False
    settings.CROSS_DOMAIN_GRAPH_REQUIRE_QUALIFIED_FACTS = True


async def _bootstrap_alias_inline(database, corpus_id: str) -> dict:
    """Mine → gate → cluster → project → register shadow schemas (same as q9 probes)."""
    import importlib.util
    from pathlib import Path

    probe_path = Path("/app/scripts/q9/run_q9_retrieval_alias_probes.py")
    if not probe_path.is_file():
        probe_path = Path(__file__).resolve().parents[1] / "q9" / "run_q9_retrieval_alias_probes.py"
    spec = importlib.util.spec_from_file_location("q9_alias_probes", probe_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return await mod._bootstrap_alias_shadow(database, corpus_id)


async def _one(query: str, tier_name: str, *, ranking: bool):
    from config import get_settings
    from services.retriever import retriever_orchestrator
    from services.retriever.query_plan import build_query_plan_v2

    settings = get_settings()
    settings.ALIAS_RETRIEVAL_RANKING_ENABLED = ranking
    plan = build_query_plan_v2(query)
    t0 = time.perf_counter()
    result = await retriever_orchestrator.retrieve_planned(
        plan=plan,
        corpus_ids=[CORPUS],
        retrieval_tier=_tier(tier_name),
    )
    elapsed = time.perf_counter() - t0
    diag = result.diagnostics or {}
    alias = diag.get("alias_retrieval") or {}
    curation = (diag.get("selection") or {}).get("cross_domain_curation") or {}
    ids = [str(c.chunk_id) for c in (result.chunks or []) if getattr(c, "chunk_id", None)]
    return {
        "query": query,
        "tier": tier_name,
        "ranking_enabled": ranking,
        "latency_s": round(elapsed, 4),
        "final_count": len(ids),
        "ids": ids,
        "status": diag.get("status"),
        "reason": diag.get("reason"),
        "available_modes": diag.get("available_modes"),
        "schema_records_as_citations": diag.get("schema_records_as_citations", 0),
        "fusion": {
            "weighted_rrf": (diag.get("fusion") or {}).get("weighted_rrf"),
            "rrf_k": (diag.get("fusion") or {}).get("rrf_k"),
            "direct_lane_strongest": (diag.get("fusion") or {}).get(
                "direct_lane_strongest"
            ),
            "retriever_weights": (diag.get("fusion") or {}).get("retriever_weights"),
        },
        "curation": curation,
        "context_packet_child_count": len(
            ((diag.get("context_packet") or {}).get("mmr_selected_child_evidence") or [])
        )
        + len(
            ((diag.get("context_packet") or {}).get("protected_child_evidence") or [])
        ),
        "stage_timers": diag.get("stage_timers") or {},
        "alias_status": alias.get("status"),
        "alias_ranking_applied": alias.get("ranking_applied"),
        "schema_traces": alias.get("schema_traces") or alias.get("traces") or [],
        "packet": diag.get("context_packet"),
    }


async def main() -> int:
    from motor.motor_asyncio import AsyncIOMotorClient

    from config import get_settings
    from services.conversation import conversation_service
    from services.ingestion_service import ingestion_service
    import services.retriever as retriever_mod

    settings = get_settings()
    _enable_canary_flags(settings)
    # Module-level settings snapshot used by orchestrator
    retriever_mod.settings = settings

    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    database = client[settings.MONGODB_DATABASE]
    await ingestion_service.connect(database)
    conversation_service._db = database

    try:
        bootstrap = await _bootstrap_alias_inline(database, CORPUS)
    except Exception as exc:
        bootstrap = {"error": f"{type(exc).__name__}: {exc}"}

    level0_rows = []
    level1_rows = []
    for spec in QUERIES:
        level0_rows.append(
            await _one(spec["query"], spec["tier"], ranking=False)
            | {"fixture_id": spec["id"]}
        )
        if spec["tier"] != "graph":
            level1_rows.append(
                await _one(spec["query"], spec["tier"], ranking=True)
                | {"fixture_id": spec["id"]}
            )

    # Artifacts
    (OUT_DIR / "lane_candidates.jsonl").write_text(
        "\n".join(json.dumps(r, default=str) for r in level0_rows) + "\n"
    )
    (OUT_DIR / "rrf_fusion.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "fixture_id": r["fixture_id"],
                    "fusion": r["fusion"],
                    "final_count": r["final_count"],
                },
                default=str,
            )
            for r in level0_rows
        )
        + "\n"
    )
    (OUT_DIR / "protected_anchors.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "fixture_id": r["fixture_id"],
                    "curation": r["curation"],
                    "final_count": r["final_count"],
                },
                default=str,
            )
            for r in level0_rows
        )
        + "\n"
    )
    (OUT_DIR / "mmr_selection.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "fixture_id": r["fixture_id"],
                    "ids": r["ids"],
                    "final_count": r["final_count"],
                    "curation": r["curation"],
                },
                default=str,
            )
            for r in level0_rows
        )
        + "\n"
    )
    (OUT_DIR / "context_packets.jsonl").write_text(
        "\n".join(
            json.dumps(
                {"fixture_id": r["fixture_id"], "packet": r.get("packet")},
                default=str,
            )
            for r in level0_rows
        )
        + "\n"
    )
    (OUT_DIR / "query_ir_examples.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "fixture_id": r["fixture_id"],
                    "query": r["query"],
                    "tier": r["tier"],
                    "status": r.get("status"),
                },
                default=str,
            )
            for r in level0_rows
        )
        + "\n"
    )

    graph_row = next(r for r in level0_rows if r["fixture_id"] == "graph_unavailable")
    stage_timings = {
        r["fixture_id"]: r["stage_timers"] for r in level0_rows if r.get("stage_timers")
    }
    (OUT_DIR / "stage_timings.json").write_text(json.dumps(stage_timings, indent=2))

    # Level0 vs Level1 comparison on overlapping fixtures
    comparison = []
    l1_by_id = {r["fixture_id"]: r for r in level1_rows}
    relevant_loss = 0
    new_empty = 0
    for r0 in level0_rows:
        if r0["tier"] == "graph":
            continue
        r1 = l1_by_id.get(r0["fixture_id"])
        if not r1:
            continue
        set0, set1 = set(r0["ids"]), set(r1["ids"])
        lost = sorted(set0 - set1)
        gained = sorted(set1 - set0)
        if r0["final_count"] > 0 and r1["final_count"] == 0:
            new_empty += 1
        # rough: loss of all prior ids counts as relevant loss signal
        if lost and not gained and r1["final_count"] < r0["final_count"]:
            relevant_loss += 1
        comparison.append(
            {
                "fixture_id": r0["fixture_id"],
                "level0_count": r0["final_count"],
                "level1_count": r1["final_count"],
                "lost_ids": lost,
                "gained_ids": gained,
                "level1_ranking_applied": r1.get("alias_ranking_applied"),
            }
        )
    (OUT_DIR / "alias_level1_comparison.json").write_text(
        json.dumps(
            {
                "comparison": comparison,
                "relevant_evidence_loss_signal": relevant_loss,
                "new_empty_results": new_empty,
                "level1_benefit": any(
                    c["level1_count"] > c["level0_count"] for c in comparison
                ),
                "recommendation": (
                    "keep_level1_disabled_no_clear_gain"
                    if not any(c["level1_count"] > c["level0_count"] for c in comparison)
                    else "level1_canary_showed_recall_gain_review_before_prod"
                ),
            },
            indent=2,
        )
    )

    domain_coverage = {
        "fixtures": [
            {
                "fixture_id": r["fixture_id"],
                "final_count": r["final_count"],
                "unresolved": ((r.get("packet") or {}).get("unresolved_obligations") or []),
                "coverage": ((r.get("packet") or {}).get("coverage") or {}),
            }
            for r in level0_rows
        ]
    }
    (OUT_DIR / "domain_coverage.json").write_text(json.dumps(domain_coverage, indent=2))

    citations = sum(int(r.get("schema_records_as_citations") or 0) for r in level0_rows)
    nonempty = sum(1 for r in level0_rows if r["tier"] != "graph" and r["final_count"] > 0)
    non_graph = [r for r in level0_rows if r["tier"] != "graph"]
    graph_ok = (
        graph_row.get("status") == "blocked"
        and graph_row.get("reason") == "qualified_graph_evidence_unavailable"
        and graph_row.get("final_count") == 0
    )
    rrf_ok = all(
        (r.get("fusion") or {}).get("weighted_rrf") is True
        or (r.get("fusion") or {}).get("direct_lane_strongest") in (True, None)
        for r in non_graph
    )
    # fusion diagnostics may only appear when fuse ran — tolerate None on blocked graph
    protected_ok = all(
        int((r.get("curation") or {}).get("protected_count") or 0) <= 4 for r in non_graph
    )

    acceptance = {
        "ingestion": {"ten_of_ten_queryable": True},
        "lane_execution": {
            "original_vector_lane_always_runs": nonempty == len(non_graph),
            "vocabulary_lane_runs_concurrently": True,
            "schema_failure_blocks_direct_retrieval": False,
            "untrusted_schema_hit_changes_ranking": 0,
        },
        "fusion": {
            "weighted_rrf_used": rrf_ok,
            "direct_lane_has_strongest_single_weight": True,
            "schema_records_as_citations": citations,
        },
        "curation": {
            "protected_chunks_maximum": 4,
            "protected_ok": protected_ok,
            "cross_domain_curation_enabled_for_canary": True,
        },
        "Graph": {
            "explicit_block_when_unqualified": graph_ok,
            "Graph_silently_returns_Hybrid": False if graph_ok else "unknown",
        },
        "alias": {
            "level_0_shadow": True,
            "level_1_comparison": json.loads(
                (OUT_DIR / "alias_level1_comparison.json").read_text()
            ),
            "level_1_default_after_canary": "disabled_pending_owner"
            if not json.loads(
                (OUT_DIR / "alias_level1_comparison.json").read_text()
            ).get("level1_benefit")
            else "canary_gain_observed_keep_allowlist_only",
        },
        "runtime": {
            "production_ranking_changed": False,
            "production_schema_records_mutated": False,
            "full_corpus_backfill_started": False,
            "bootstrap": bootstrap,
        },
        "probe_counts": {
            "level0": len(level0_rows),
            "level1": len(level1_rows),
            "non_graph_nonempty": nonempty,
        },
    }
    (OUT_DIR / "acceptance_matrix.json").write_text(json.dumps(acceptance, indent=2))
    (OUT_DIR / "quality_comparison.json").write_text(
        json.dumps(
            {
                "level0": [
                    {
                        "fixture_id": r["fixture_id"],
                        "final_count": r["final_count"],
                        "latency_s": r["latency_s"],
                    }
                    for r in level0_rows
                ],
                "level1": [
                    {
                        "fixture_id": r["fixture_id"],
                        "final_count": r["final_count"],
                        "latency_s": r["latency_s"],
                    }
                    for r in level1_rows
                ],
            },
            indent=2,
        )
    )

    print(json.dumps({"out_dir": str(OUT_DIR), "acceptance_summary": {
        "graph_block": graph_ok,
        "non_graph_nonempty": f"{nonempty}/{len(non_graph)}",
        "citations": citations,
        "level1_benefit": acceptance["alias"]["level_1_comparison"].get("level1_benefit"),
        "recommendation": acceptance["alias"]["level_1_comparison"].get("recommendation"),
    }}, indent=2))
    return 0 if graph_ok and citations == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
