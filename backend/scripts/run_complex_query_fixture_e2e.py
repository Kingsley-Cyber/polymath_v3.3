#!/usr/bin/env python3
"""Complex-query Phases 4–12 fixture E2E (gsem-e2e-20260804a only) → STOP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

CORPUS_ID = os.environ.get("GSEM_FIXTURE_CORPUS_ID", "gsem-e2e-20260804a")
OUT_DIR = Path(os.environ.get("CQ_OUT", "/tmp/complex_query_e2e"))
QUERY = os.environ.get(
    "CQ_PROBE_QUERY",
    "How should a C++ combat update loop be translated into Roblox Luau "
    "while preserving the original mechanics?",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(obj: object) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


async def main() -> int:
    from motor.motor_asyncio import AsyncIOMotorClient
    from neo4j import AsyncGraphDatabase
    from qdrant_client import AsyncQdrantClient

    from config import get_settings
    from services.ingestion.fixture_knowledge_pipeline import assert_fixture_safe
    from services.retriever.complex_query_runtime import run_complex_query_fixture

    settings = get_settings()
    # Force dark global planner; fixture allowlist only.
    object.__setattr__(settings, "COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED", False)
    object.__setattr__(settings, "COMPLEX_QUERY_CORPUS_ALLOWLIST", CORPUS_ID)

    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DATABASE]
    corpus = await db["corpora"].find_one({"corpus_id": CORPUS_ID})
    assert corpus, f"missing fixture corpus {CORPUS_ID}"
    assert_fixture_safe(corpus)

    qdrant = AsyncQdrantClient(url=settings.QDRANT_URL, timeout=120)
    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        run1 = await run_complex_query_fixture(
            db=db,
            qdrant=qdrant,
            neo4j_driver=driver,
            query=QUERY,
            corpus_id=CORPUS_ID,
            settings=settings,
        )
        # Restart replay: second plan+execute, compare hashes
        run2 = await run_complex_query_fixture(
            db=db,
            qdrant=qdrant,
            neo4j_driver=driver,
            query=QUERY,
            corpus_id=CORPUS_ID,
            settings=settings,
        )
    finally:
        await driver.close()
        await qdrant.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    def dump_run(run, prefix: str) -> dict:
        root = run.bundle.root.model_dump()
        plans = [s.model_dump() for s in run.bundle.subqueries]
        results = [r.model_dump() for r in run.subquery_results]
        (OUT_DIR / f"{prefix}_root_query_ir.json").write_text(json.dumps(root, indent=2, default=str))
        (OUT_DIR / "subquery_plans.jsonl").write_text(
            "\n".join(json.dumps(p, default=str) for p in plans) + "\n"
        )
        (OUT_DIR / "subquery_results.jsonl").write_text(
            "\n".join(json.dumps(r, default=str) for r in results) + "\n"
        )
        (OUT_DIR / "traversal_plans.jsonl").write_text(
            json.dumps(run.traversal_diagnostics, indent=2, default=str)
        )
        (OUT_DIR / "graph_paths.jsonl").write_text(
            "\n".join(
                json.dumps(p, default=str)
                for p in (run.context_packet.get("graph_paths") or [])
            )
            + ("\n" if run.context_packet.get("graph_paths") else "")
        )
        (OUT_DIR / "bridges.jsonl").write_text(
            "\n".join(json.dumps(b, default=str) for b in run.bridges) + "\n"
        )
        (OUT_DIR / "contradictions.jsonl").write_text(
            "\n".join(json.dumps(c, default=str) for c in run.contradictions) + "\n"
        )
        (OUT_DIR / "obligation_fusion.jsonl").write_text(
            "\n".join(json.dumps(f, default=str) for f in run.obligation_fusions) + "\n"
        )
        (OUT_DIR / "mmr_selection.jsonl").write_text(
            json.dumps(
                {
                    "protected": run.protected_child_ids,
                    "selected": run.mmr_selected_child_ids,
                },
                indent=2,
            )
        )
        (OUT_DIR / "context_packets.jsonl").write_text(
            json.dumps(run.context_packet, default=str) + "\n"
        )
        (OUT_DIR / "answer_verification.jsonl").write_text(
            json.dumps(run.verification, default=str) + "\n"
        )
        return {
            "root_plan_hash": run.bundle.root.plan_hash,
            "subquery_hashes": [s.plan_hash for s in run.bundle.subqueries],
            "path_ids": [
                p.get("path_id")
                for p in (run.context_packet.get("graph_paths") or [])
            ],
            "selected_evidence": run.mmr_selected_child_ids,
            "context_hash": run.context_packet.get("context_hash"),
        }

    h1 = dump_run(run1, "run1")
    h2 = {
        "root_plan_hash": run2.bundle.root.plan_hash,
        "subquery_hashes": [s.plan_hash for s in run2.bundle.subqueries],
        "path_ids": [
            p.get("path_id") for p in (run2.context_packet.get("graph_paths") or [])
        ],
        "selected_evidence": run2.mmr_selected_child_ids,
        "context_hash": run2.context_packet.get("context_hash"),
    }
    replay = {
        "force_recreate_services": False,
        "same_process_replay": True,
        "plan_hash_stable": h1["root_plan_hash"] == h2["root_plan_hash"],
        "subquery_hashes_stable": h1["subquery_hashes"] == h2["subquery_hashes"],
        "path_ids_stable": h1["path_ids"] == h2["path_ids"],
        "selected_evidence_stable": h1["selected_evidence"] == h2["selected_evidence"],
        "context_hash_stable": h1["context_hash"] == h2["context_hash"],
        "run1": h1,
        "run2": h2,
    }
    (OUT_DIR / "restart_replay.json").write_text(json.dumps(replay, indent=2))
    (OUT_DIR / "stage_timings.json").write_text(
        json.dumps(
            {
                "run1_ms": run1.execution_ms,
                "run2_ms": run2.execution_ms,
                "wave1_ms": run1.wave1.execution_ms,
                "traversal_ms": run1.traversal_diagnostics.get("execution_ms"),
            },
            indent=2,
        )
    )

    acceptance = {
        **run1.acceptance,
        "replay_ok": all(
            [
                replay["plan_hash_stable"],
                replay["subquery_hashes_stable"],
                replay["context_hash_stable"],
            ]
        ),
        "fixture_corpus": CORPUS_ID,
        "finished_at": _utcnow(),
        "hard_stop": True,
    }
    acceptance["all_ok"] = bool(acceptance.get("phase_4_5_ok")) and bool(
        acceptance.get("replay_ok")
    )
    (OUT_DIR / "acceptance_matrix.json").write_text(json.dumps(acceptance, indent=2, default=str))

    # Also append root_query_ir.jsonl
    (OUT_DIR / "root_query_ir.jsonl").write_text(
        json.dumps(run1.bundle.root.model_dump(), default=str) + "\n"
    )

    report = {
        "corpus_id": CORPUS_ID,
        "query": QUERY,
        "acceptance": acceptance,
        "intent_class": run1.bundle.root.intent_class,
        "graph_level": run1.bundle.root.graph_level,
        "subquery_count": len(run1.bundle.subqueries),
        "direct_children": len(run1.wave1.direct_child_ids),
        "paths": len(run1.context_packet.get("graph_paths") or []),
        "final_children": len(run1.mmr_selected_child_ids),
        "execution_ms": run1.execution_ms,
    }
    print(json.dumps(report, indent=2, default=str))
    client.close()
    return 0 if acceptance["all_ok"] else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
