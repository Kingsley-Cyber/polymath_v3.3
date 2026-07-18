"""Gate-only runner for the frozen 28-probe held-out negative v2 suite."""

from __future__ import annotations

import json

import e2e_instrumented_eval_baseline as instrumented
import e2e_retrieval_eval as core
from config import get_settings
from pymongo import MongoClient


SPEC_SHA256 = "3b35c14c165f6be89202b809ea01a1cd6ad0f5c0217e4167b86e4b5dc0b09960"
CORPUS_ID = "2c894530-8d57-4432-a6d4-bc14505a698b"
TIER = "qdrant_mongo_graph"
_real_loads = core.json.loads


def _adapt_spec(payload, *args, **kwargs):
    value = _real_loads(payload, *args, **kwargs)
    if (
        isinstance(value, dict)
        and value.get("schema_version") == "polymath.e2e_heldout_negative.v2"
    ):
        if value.get("used_for_tuning") is not False:
            raise RuntimeError("held-out v2 suite lost its gate-only contract")
        queries = list(value.get("queries") or [])
        if len(queries) != 28 or not all(row.get("must_refuse") for row in queries):
            raise RuntimeError("held-out v2 suite did not close at 28 refusal probes")
        return {
            "schema_version": "polymath.runpod_e2e_retrieval_preregister.v1",
            "tiers": [TIER],
            "top_k": 8,
            "gates": {
                "corpus_boundary_precision": 1.0,
                "direct_doc_hit_rate_min": 0.0,
                "lay_language_doc_hit_rate_min": 0.0,
                "relationship_query_min_distinct_target_rate_min": 0.0,
                "negative_refusal_rate_min": 1.0,
                "citation_source_membership_rate_min": 1.0,
            },
            "queries": [
                {
                    "id": row["id"],
                    "shape": "negative_control",
                    "question": row["question"],
                    "must_refuse": True,
                    "expected_any": [],
                    "expected_min_distinct": 0,
                    "evidence_anchors": {},
                }
                for row in queries
            ],
        }
    return value


def _route_and_cost_preflight() -> None:
    settings = get_settings()
    mongo = MongoClient(settings.MONGODB_URI)
    try:
        database = mongo[settings.MONGODB_DATABASE]
        corpus = database["corpora"].find_one(
            {"corpus_id": CORPUS_ID},
            {"_id": 0, "name": 1, "user_id": 1},
        )
        if not corpus:
            raise RuntimeError("held-out v2 corpus is absent")
        route = instrumented.query_route_preflight(database, corpus)
        envelope = instrumented.cost_envelope(28)
        print("QUERY_ROUTE_PREFLIGHT=" + json.dumps(route, sort_keys=True), flush=True)
        print("COST_ENVELOPE=" + json.dumps(envelope, sort_keys=True), flush=True)
    finally:
        mongo.close()


core.PREREG_SHA = SPEC_SHA256
core.TIERS = (TIER,)
core.json.loads = _adapt_spec
_route_and_cost_preflight()
raise SystemExit(core.main())
