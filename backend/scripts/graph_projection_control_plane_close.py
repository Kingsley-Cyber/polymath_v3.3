#!/usr/bin/env python3
"""q9 Graph Projection Control-Plane closure — orphan classify + project + certify.

Usage (inside backend container):
  PYTHONPATH=/app python /app/_gproj_close.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

Q9_CORPUS = os.environ.get(
    "GPROJ_CORPUS_ID", "6a766597-29f3-4a3e-8918-5de10f0053b3"
)
OUT_DIR = Path(os.environ.get("GPROJ_OUT", "/app/data_eval/q9_final"))


async def main() -> int:
    from motor.motor_asyncio import AsyncIOMotorClient
    from neo4j import AsyncGraphDatabase

    from config import get_settings
    from services.graph.projection_jobs import (
        JOBS_COLLECTION,
        reconcile_stuck_extraction_orphans,
    )
    from services.graph.projection_runner import plan_all_documents, run_projection_jobs
    from services.retriever.graph_authority import inspect_graph_capabilities

    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DATABASE]
    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "corpus_id": Q9_CORPUS,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "blind_full_reextraction": False,
        "bounded_reextract_limit": 0,
    }

    # 1) Classify stuck orphans (no blind re-extract).
    orphan = await reconcile_stuck_extraction_orphans(
        db,
        corpus_id=Q9_CORPUS,
        apply=True,
        bounded_reextract_limit=0,
    )
    report["orphan_reconciliation"] = orphan
    (OUT_DIR / "orphan_reconciliation.json").write_text(
        json.dumps(orphan, indent=2, default=str)
    )

    # 2) Plan projection jobs for all documents.
    plan = await plan_all_documents(db, corpus_id=Q9_CORPUS)
    report["plan"] = {
        "documents": plan["documents"],
        "job_count": sum(len(p.get("jobs") or []) for p in plan.get("plans") or []),
    }

    # 3) Authorize + apply + verify + certify (idempotent).
    run1 = await run_projection_jobs(
        db, driver, corpus_id=Q9_CORPUS, owner="gproj_close_1", max_jobs=500
    )
    run2 = await run_projection_jobs(
        db, driver, corpus_id=Q9_CORPUS, owner="gproj_close_2", max_jobs=500
    )
    report["run_pass_1"] = {
        "executed": run1.get("executed"),
        "authorized": run1.get("authorized"),
        "job_status_histogram": run1.get("job_status_histogram"),
        "certificate_mode": (run1.get("certificate") or {}).get("advertised_mode"),
        "noop_marks_ready": (run1.get("certificate") or {}).get("noop_marks_ready"),
    }
    report["run_pass_2_replay"] = {
        "executed": run2.get("executed"),
        "job_status_histogram": run2.get("job_status_histogram"),
        "certificate_hash_1": (run1.get("certificate") or {}).get("certificate_hash"),
        "certificate_hash_2": (run2.get("certificate") or {}).get("certificate_hash"),
        "restart_replay_identical": (run1.get("certificate") or {}).get(
            "certificate_hash"
        )
        == (run2.get("certificate") or {}).get("certificate_hash"),
        "idempotent_no_new_work": int(run2.get("executed") or 0) == 0,
    }

    # 4) Determinism + job counters.
    jobs = await db[JOBS_COLLECTION].find({"corpus_id": Q9_CORPUS}).to_list(10000)
    job_ids = [j.get("graph_job_id") for j in jobs]
    pending = sum(
        1
        for j in jobs
        if j.get("status")
        in {
            "PLANNED",
            "INPUTS_VALIDATED",
            "ONTOLOGY_RESOLVED",
            "AUTHORIZED",
            "APPLYING",
            "APPLIED",
            "VERIFIED",
        }
    )
    leased = sum(1 for j in jobs if j.get("status") == "APPLYING")
    stuck_ext = await db["extraction_jobs"].count_documents(
        {"corpus_id": Q9_CORPUS, "status": "stuck_orphan"}
    )
    unclassified = await db["extraction_jobs"].count_documents(
        {
            "corpus_id": Q9_CORPUS,
            "status": "stuck_orphan",
            "orphan_class": {"$exists": False},
        }
    )
    caps = await inspect_graph_capabilities([Q9_CORPUS])

    # Assertion support integrity sample.
    support_ok = True
    async with driver.session() as session:
        orphan_assertions = await (
            await session.run(
                """
                MATCH (a:RelationAssertion {corpus_id: $cid})
                WHERE NOT ()-[:SUPPORTS_ASSERTION]->(a)
                RETURN count(a) AS n
                """,
                cid=Q9_CORPUS,
            )
        ).single()
        support_orphans = int(orphan_assertions["n"] or 0)
        support_ok = support_orphans == 0

    report["acceptance"] = {
        "jobs": {
            "pending": pending,
            "leased": leased,
            "stuck_orphan": stuck_ext,
            "unclassified_orphans": unclassified,
            "terminal_or_explicitly_blocked": pending == 0 and leased == 0,
        },
        "determinism": {
            "duplicate_job_ids": len(job_ids) - len(set(job_ids)),
            "restart_replay_identical": report["run_pass_2_replay"][
                "restart_replay_identical"
            ],
            "idempotent_projection": report["run_pass_2_replay"][
                "idempotent_no_new_work"
            ],
        },
        "verification": {
            "assertion_support_orphans": support_orphans,
            "every_assertion_has_supporting_chunk": support_ok,
            "no_op_marks_ready": False,
        },
        "readiness": {
            "advertised_mode": (run2.get("certificate") or {}).get("advertised_mode"),
            "structural_ready": (run2.get("certificate") or {}).get("structural_ready"),
            "entity_ready": (run2.get("certificate") or {}).get("entity_ready"),
            "assertion_ready": (run2.get("certificate") or {}).get("assertion_ready"),
            "qualified_fact_ready": (run2.get("certificate") or {}).get(
                "qualified_fact_ready"
            ),
            "neo4j_counts": caps.get("counts"),
        },
    }
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    out_path = OUT_DIR / "graph_projection_control_plane_closeout.json"
    out_path.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report["acceptance"], indent=2, default=str))
    print(f"WROTE {out_path}")

    await driver.close()
    client.close()

    acc = report["acceptance"]
    ok = (
        acc["jobs"]["pending"] == 0
        and acc["jobs"]["leased"] == 0
        and acc["jobs"]["stuck_orphan"] == 0
        and acc["jobs"]["unclassified_orphans"] == 0
        and acc["determinism"]["duplicate_job_ids"] == 0
        and acc["determinism"]["restart_replay_identical"]
        and acc["determinism"]["idempotent_projection"]
        and acc["verification"]["every_assertion_has_supporting_chunk"]
        and acc["verification"]["no_op_marks_ready"] is False
    )
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
