"""P3 — materialize the deterministic summary chain on a fixture corpus.

Runs the required summary lane WITHOUT any cost authority or provider pool:

    parent summaries (deterministic_parent.v1)
      -> summary_tree / doc_profile (deterministic tree build, use_llm=False)
      -> Qdrant projections (summary points + tier0 doc profiles)

This script proves the owner decision in live runtime:
``required_summary_provider: none`` — the baseline completes with zero
provider calls and zero summary-cost authority.

Usage:
    python backend/scripts/run_deterministic_summary_materialization.py \
        --corpus f842e3b5 --cycles 6

Writes: data_eval/deterministic_summary_materialization.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

# Host-side service endpoints (script runs outside the docker network).
os.environ.setdefault("EMBEDDER_URL", "http://localhost:8082")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from qdrant_client import AsyncQdrantClient  # noqa: E402

from config import get_settings  # noqa: E402

SUMMARY_TEXT_CLAUSE = {"summary": {"$exists": True, "$nin": [None, ""]}}


async def _corpus_state(db, corpus_id: str) -> dict:
    parents = await db["parent_chunks"].count_documents({"corpus_id": corpus_id})
    parents_summarized = await db["parent_chunks"].count_documents(
        {"corpus_id": corpus_id, **{"$and": [SUMMARY_TEXT_CLAUSE]}}
    )
    det_parents = await db["parent_chunks"].count_documents(
        {
            "corpus_id": corpus_id,
            "schema_version": "deterministic_summary.v1",
        }
    )
    tree_nodes = await db["summary_tree"].count_documents({"corpus_id": corpus_id})
    docs = await db["documents"].count_documents({"corpus_id": corpus_id})
    profiles = await db["documents"].count_documents(
        {
            "corpus_id": corpus_id,
            "doc_profile.summary": {"$exists": True, "$nin": [None, ""]},
        }
    )
    jobs = await db["summary_jobs"].aggregate(
        [
            {"$match": {"corpus_id": corpus_id}},
            {"$group": {"_id": "$status", "n": {"$sum": 1}}},
        ]
    ).to_list(32)
    readiness = await db["corpus_readiness"].find_one(
        {"corpus_id": corpus_id}, {"_id": 0, "status": 1, "updated_at": 1}
    )
    return {
        "parents": parents,
        "parents_summarized": parents_summarized,
        "deterministic_parents": det_parents,
        "summary_tree_nodes": tree_nodes,
        "documents": docs,
        "documents_with_profile": profiles,
        "summary_jobs": {row["_id"]: row["n"] for row in jobs},
        "readiness_status": (readiness or {}).get("status"),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, help="corpus_id prefix")
    parser.add_argument("--cycles", type=int, default=6)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "data_eval" / "deterministic_summary_materialization.json"),
    )
    args = parser.parse_args()

    settings = get_settings()
    mongo_uri = settings.MONGODB_URI.replace("mongodb:27017", "localhost:27017")
    client = AsyncIOMotorClient(mongo_uri)
    db = client[settings.MONGODB_DATABASE]

    corpus = await db["corpora"].find_one(
        {"corpus_id": {"$regex": f"^{args.corpus}"}},
        {"_id": 0, "corpus_id": 1, "name": 1, "user_id": 1, "default_ingestion_config": 1},
    )
    if not corpus:
        print(f"corpus prefix not found: {args.corpus}")
        return 1
    corpus_id = corpus["corpus_id"]

    from services.ingestion.summary_jobs import plan_summary_jobs
    from services.ingestion_service import IngestionService

    service = IngestionService()
    service._db = db
    service._qdrant = AsyncQdrantClient(url=os.environ["QDRANT_URL"], timeout=30)

    # The fixture corpus may predate Qdrant provisioning; the collection family
    # is a rebuildable projection and must exist before summary indexing.
    from services.storage.qdrant_writer import ensure_collections_for_corpus

    embedding_dim = int(
        ((corpus.get("default_ingestion_config") or {}).get("embedding_dimension")) or 1024
    )
    await ensure_collections_for_corpus(
        service._qdrant, corpus_id, dim=embedding_dim, corpus_name=corpus.get("name")
    )

    before = await _corpus_state(db, corpus_id)
    print(f"corpus {corpus_id[:8]} ({corpus.get('name')}) before: {json.dumps(before)}")

    cycles: list[dict] = []
    for cycle in range(1, args.cycles + 1):
        # Replan first: reclassifies blocked jobs whose preconditions are now
        # satisfied (mirrors the bounded repair cycle flow).
        plan = await plan_summary_jobs(
            db,
            corpus_id=corpus_id,
            user_id=str(corpus.get("user_id") or ""),
            apply=True,
            limit=max(args.limit * 10, 500),
        )
        # NO summary_cost_run_id / authority -> deterministic_summary.v1 lane.
        result = await service.run_summary_jobs(
            corpus_id=corpus_id,
            user_id=str(corpus.get("user_id") or ""),
            limit=args.limit,
        )
        receipt = result.get("summary_cost_receipt") or {}
        cycles.append(
            {
                "cycle": cycle,
                "plan": {
                    "planned": plan.get("planned"),
                    "blocked_reevaluation": plan.get("blocked_reevaluation"),
                    "artifact_reconciled": plan.get("artifact_reconciled"),
                },
                "status": result.get("status"),
                "claimed": result.get("claimed"),
                "parent_claimed": result.get("parent_claimed"),
                "document_claimed": result.get("document_claimed"),
                "runner_results": {
                    key: {
                        "status": (value or {}).get("status"),
                        "generated": (value or {}).get("generated"),
                        "built": (value or {}).get("built"),
                        "indexed": (value or {}).get("indexed"),
                        "index_error": (value or {}).get("index_error"),
                        "tier0_projection": (value or {}).get("tier0_projection"),
                    }
                    for key, value in (result.get("runner_results") or {}).items()
                },
                "receipt": receipt,
            }
        )
        print(
            f"cycle {cycle}: status={result.get('status')} "
            f"claimed={result.get('claimed')} "
            f"reevaluation={json.dumps(plan.get('blocked_reevaluation') or {})} "
            f"receipt={json.dumps(receipt)}"
        )
        if not result.get("claimed") and cycle > 1:
            break

    after = await _corpus_state(db, corpus_id)
    print(f"after: {json.dumps(after)}")

    # Re-project any deterministic parent summaries that were generated before
    # the collection family existed (idempotent UUID5 point ids).
    indexed = await service._index_deterministic_parent_summaries(
        corpus_id,
        effective_user_id=str(corpus.get("user_id") or ""),
        batch=32,
    )
    print(f"deterministic parent-summary points projected: {indexed}")

    record = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "corpus_id": corpus_id,
        "product": "deterministic_summary.v1",
        "provider_calls": 0,
        "authority_required": False,
        "final_parent_summary_projection": indexed,
        "before": before,
        "after": after,
        "cycles": cycles,
        "verdict": {
            "parents_summarized": after["parents_summarized"] >= before["parents"],
            "deterministic_parents": after["deterministic_parents"] > 0,
            "summary_tree_built": after["summary_tree_nodes"] > 0,
            "document_profiles_built": after["documents_with_profile"]
            >= max(after["documents"], 1),
            "provider_free": all(
                int((c.get("receipt") or {}).get("provider_calls") or 0) == 0
                for c in cycles
            ),
        },
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(record, indent=2, default=str))
    print(f"record written: {args.out}")
    print(f"verdict: {json.dumps(record['verdict'])}")
    return 0 if all(record["verdict"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
