"""Measure the 19 not-yet-passed held-out-v2 probes on the two-flag stack."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import e2e_instrumented_eval_baseline as instrumented
import e2e_retrieval_eval as core
from config import get_settings
from pymongo import MongoClient


SPEC_SHA256 = "3b35c14c165f6be89202b809ea01a1cd6ad0f5c0217e4167b86e4b5dc0b09960"
PRIOR_LOG_SHA256 = "6345380ccb36331776515348c1e62adbee902a069910c1741516cdb5f2043832"
SELECTION_SHA256 = "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00"
CORPUS_ID = "2c894530-8d57-4432-a6d4-bc14505a698b"
TIER = "qdrant_mongo_graph"
MAX_ENVELOPE_USD = 1.10


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prior_results(path: Path) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("EXECUTION_DONE "):
            continue
        row = json.loads(line.removeprefix("EXECUTION_DONE "))
        query_id = str(row["execution_id"]).split("::", 1)[0]
        results[query_id] = bool(row.get("fail_closed"))
    return results


def answerability_telemetry(traces: list[dict[str, Any]]) -> dict[str, Any]:
    for trace in reversed(traces):
        if trace.get("title") != "Answerability gate":
            continue
        metadata = trace.get("metadata") or {}
        guard = metadata.get("corpus_scope_guard") or {}
        return {
            "status": metadata.get("status"),
            "answerable": metadata.get("answerable"),
            "raw_answerable": metadata.get("raw_answerable"),
            "required_coverage": metadata.get("required_coverage"),
            "missing_critical_atoms": metadata.get("missing_critical_atoms") or [],
            "policy_version": metadata.get("answerability_policy_version"),
            "guard": {
                "enabled": guard.get("enabled"),
                "eligible": guard.get("eligible"),
                "terms": guard.get("terms") or [],
                "matched_terms": guard.get("matched_terms") or [],
                "missing_terms": guard.get("missing_terms") or [],
                "coverage": guard.get("coverage"),
                "min_terms": guard.get("min_terms"),
                "min_coverage": guard.get("min_coverage"),
                "supported": guard.get("supported"),
                "applied": guard.get("applied"),
                "reason": guard.get("reason"),
            },
        }
    return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", type=Path, required=True)
    parser.add_argument("--prior-log", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    require(sha256(args.prereg) == SPEC_SHA256, "held-out-v2 spec hash drifted")
    require(sha256(args.prior_log) == PRIOR_LOG_SHA256, "prior RED log hash drifted")
    require(sha256(args.selection) == SELECTION_SHA256, "selection hash drifted")

    spec = json.loads(args.prereg.read_text(encoding="utf-8"))
    queries = list(spec.get("queries") or [])
    require(len(queries) == 28, "held-out-v2 query count drifted")
    require(spec.get("used_for_tuning") is False, "held-out set lost gate-only status")
    require(all(row.get("must_refuse") is True for row in queries), "probe contract drifted")

    prior = prior_results(args.prior_log)
    passed = {query_id for query_id, refused in prior.items() if refused}
    answered = {query_id for query_id, refused in prior.items() if not refused}
    require(len(prior) == 12, "prior partial pass did not close at 12 probes")
    require(len(passed) == 9 and len(answered) == 3, "prior RED split drifted")
    pending = [row for row in queries if str(row["id"]) not in passed]
    require(len(pending) == 19, "attribution set did not close at 19 probes")
    require(answered <= {str(row["id"]) for row in pending}, "answered probes omitted")

    settings = get_settings()
    require(
        settings.RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED is True,
        "relationship allocation must remain ON",
    )
    require(
        settings.ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED is True,
        "corpus-scope v2 must remain ON",
    )
    require(
        settings.TEMPORAL_QUERY_ROUTING_ENABLED is False,
        "temporal must remain OFF",
    )
    require(
        settings.FOUR_LANE_TIER0_ROUTER_ENABLED is False,
        "router must remain OFF for two-flag attribution",
    )

    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    selected_filenames = {str(row["filename"]) for row in selection["selected"]}
    require(len(selected_filenames) == 15, "selection did not close at 15 documents")

    mongo = MongoClient(settings.MONGODB_URI)
    database = mongo[settings.MONGODB_DATABASE]
    try:
        corpus = database["corpora"].find_one(
            {"corpus_id": CORPUS_ID},
            {"_id": 0, "name": 1, "user_id": 1},
        )
        require(bool(corpus), "E2E corpus is absent")
        route = instrumented.query_route_preflight(database, corpus)
        envelope = instrumented.cost_envelope(len(pending))
        require(
            float(envelope["total_usd"]) <= MAX_ENVELOPE_USD,
            "attribution envelope exceeds senior cap",
        )
        document_names = {
            str(row.get("doc_id") or ""): str(
                row.get("original_filename") or row.get("filename") or ""
            )
            for row in database["documents"].find(
                {"corpus_id": CORPUS_ID},
                {"_id": 0, "doc_id": 1, "filename": 1, "original_filename": 1},
            )
        }
        require(len(document_names) == 15, "E2E document count drifted")
        token = core._mint_token(database, CORPUS_ID)

        state: dict[str, Any] = {
            "schema_version": "polymath.e2e_heldout_negative_attribution.v1",
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "completed_at_utc": None,
            "measurement_only": True,
            "used_for_tuning": False,
            "corpus_id": CORPUS_ID,
            "tier": TIER,
            "spec_sha256": SPEC_SHA256,
            "prior_red_log_sha256": PRIOR_LOG_SHA256,
            "selection_sha256": SELECTION_SHA256,
            "prior_passed_probe_ids": sorted(passed),
            "prior_answered_probe_ids": sorted(answered),
            "query_route_preflight": route,
            "cost_envelope": envelope,
            "runtime_flags": {
                "relationship_evidence_allocation_enabled": True,
                "answerability_corpus_scope_v2_enabled": True,
                "temporal_query_routing_enabled": False,
                "four_lane_tier0_router_enabled": False,
            },
            "results": [],
            "summary": None,
        }
        core._atomic_write(args.output, state)
        print("COST_ENVELOPE=" + json.dumps(envelope, sort_keys=True), flush=True)

        for case in pending:
            query_id = str(case["id"])
            print(f"ATTRIBUTION_START {query_id}", flush=True)
            raw = core._run_sse(
                base=args.base,
                token=token,
                corpus_id=CORPUS_ID,
                tier=TIER,
                question=str(case["question"]),
                top_k=8,
            )
            adapted = {
                "id": query_id,
                "shape": "negative_control",
                "question": case["question"],
                "must_refuse": True,
                "expected_any": [],
                "expected_min_distinct": 0,
                "evidence_anchors": {},
            }
            scored = core._score_execution(
                case=adapted,
                tier=TIER,
                raw=raw,
                corpus_id=CORPUS_ID,
                document_names=document_names,
                selected_filenames=selected_filenames,
            )
            telemetry = answerability_telemetry(raw["traces"])
            technical_ok = bool(
                not scored["errors"]
                and scored["done_received"]
                and scored["effective_tier"] == TIER
                and telemetry
            )
            result = {
                "query_id": query_id,
                "family": case.get("family"),
                "question_sha256": hashlib.sha256(
                    str(case["question"]).encode("utf-8")
                ).hexdigest(),
                "refused": scored["fail_closed"],
                "model_skipped": scored["model_skipped"],
                "source_count": scored["source_count"],
                "source_filenames": scored["source_filenames"],
                "technical_ok": technical_ok,
                "errors": scored["errors"],
                "elapsed_seconds": scored["elapsed_seconds"],
                "answer_sha256": scored["answer_sha256"],
                "answerability": telemetry,
            }
            state["results"].append(result)
            core._atomic_write(args.output, state)
            print(
                "ATTRIBUTION_DONE "
                + json.dumps(
                    {
                        "query_id": query_id,
                        "refused": result["refused"],
                        "technical_ok": technical_ok,
                        "coverage": (telemetry.get("guard") or {}).get("coverage"),
                        "matched_terms": (telemetry.get("guard") or {}).get(
                            "matched_terms"
                        ),
                        "missing_terms": (telemetry.get("guard") or {}).get(
                            "missing_terms"
                        ),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

        results = state["results"]
        state["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        state["summary"] = {
            "execution_count": len(results),
            "refused_count": sum(row["refused"] is True for row in results),
            "answered_count": sum(row["refused"] is False for row in results),
            "technical_success_count": sum(row["technical_ok"] for row in results),
            "technical_success": all(row["technical_ok"] for row in results),
        }
        core._atomic_write(args.output, state)
        print("SUMMARY=" + json.dumps(state["summary"], sort_keys=True), flush=True)
        return 0 if state["summary"]["technical_success"] else 1
    finally:
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
