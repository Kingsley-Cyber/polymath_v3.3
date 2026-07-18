"""Run and score the immutable 15-document, three-tier retrieval preregistration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from config import get_settings
from pymongo import MongoClient
from services.auth import auth_service


PREREG_SHA = "8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110"
SELECTION_SHA = "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00"
TIERS = ("qdrant_only", "qdrant_mongo", "qdrant_mongo_graph")
REFUSAL_MARKERS = (
    "cannot answer",
    "did not find source evidence",
    "did not establish",
    "not contain the answer",
    "not in the selected corpus",
    "not supported by the selected corpus",
)


def _norm(value: Any) -> str:
    return re.sub(
        r"\s+",
        " ",
        re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()),
    ).strip()


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        + b"\n"
    )
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _mint_token(database: Any, corpus_id: str) -> str:
    corpus = database["corpora"].find_one(
        {"corpus_id": corpus_id}, {"_id": 0, "user_id": 1}
    )
    if not corpus or not corpus.get("user_id"):
        raise RuntimeError("E2E corpus owner is absent")
    user_id = str(corpus["user_id"])
    if not ObjectId.is_valid(user_id):
        raise RuntimeError("E2E corpus owner identity is invalid")
    user = database["users"].find_one(
        {"_id": ObjectId(user_id)}, {"_id": 1, "username": 1}
    )
    if not user or not user.get("username"):
        raise RuntimeError("E2E corpus owner user is absent")
    return auth_service.create_access_token(
        user_id=str(user["_id"]), username=str(user["username"])
    )


def _source_text(source: dict[str, Any]) -> str:
    return str(
        source.get("text")
        or source.get("content")
        or source.get("chunk_text")
        or source.get("summary")
        or ""
    )


def _source_filename(source: dict[str, Any], names: dict[str, str]) -> str:
    mapped = names.get(str(source.get("doc_id") or ""), "")
    return str(
        mapped
        or source.get("filename")
        or source.get("doc_name")
        or source.get("document_name")
        or source.get("title")
        or ""
    )


def _effective_tier(traces: list[dict[str, Any]]) -> str:
    for trace in reversed(traces):
        if trace.get("title") == "Local RAG retrieval":
            value = str((trace.get("metadata") or {}).get("effective_tier") or "")
            if value:
                return value
    return ""


def _model_skipped(traces: list[dict[str, Any]]) -> bool:
    return any(
        trace.get("title") == "Assistant final answer"
        and (trace.get("metadata") or {}).get("model_skipped") is True
        for trace in traces
    )


def _run_sse(
    *,
    base: str,
    token: str,
    corpus_id: str,
    tier: str,
    question: str,
    top_k: int,
) -> dict[str, Any]:
    payload = {
        "message": question,
        "corpus_ids": [corpus_id],
        "retrieval_tier": tier,
        "overrides": {"final_top_k": top_k, "temperature": 0},
    }
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/chat",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    started = time.perf_counter()
    current_event: str | None = None
    answer: list[str] = []
    sources: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    errors: list[str] = []
    done: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            if response.status != 200:
                raise RuntimeError(f"chat HTTP status {response.status}")
            if "text/event-stream" not in str(
                response.headers.get("Content-Type") or ""
            ):
                raise RuntimeError("chat did not return SSE")
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event:"):
                    current_event = line.split(":", 1)[1].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                try:
                    obj = json.loads(line[5:].strip())
                except Exception:
                    continue
                event_type = obj.get("type") or current_event
                if event_type == "token":
                    answer.append(str(obj.get("content") or obj.get("token") or ""))
                elif event_type == "sources":
                    raw_sources = obj.get("sources") or obj.get("data") or []
                    sources = raw_sources if isinstance(raw_sources, list) else []
                elif event_type == "trace_event" or obj.get("trace_event"):
                    traces.append(dict(obj.get("trace_event") or obj))
                elif event_type == "error":
                    errors.append(
                        str(obj.get("content") or obj.get("error") or "")[:500]
                    )
                elif event_type == "done":
                    done = obj
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        errors.append(f"HTTP {exc.code}: {detail}")
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return {
        "answer": "".join(answer),
        "sources": sources,
        "traces": traces,
        "errors": errors,
        "done": done,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def _score_execution(
    *,
    case: dict[str, Any],
    tier: str,
    raw: dict[str, Any],
    corpus_id: str,
    document_names: dict[str, str],
    selected_filenames: set[str],
) -> dict[str, Any]:
    sources = raw["sources"]
    source_filenames = [_source_filename(source, document_names) for source in sources]
    source_filenames = [name for name in source_filenames if name]
    distinct_filenames = sorted(set(source_filenames))
    memberships = [
        str(source.get("corpus_id") or "") == corpus_id
        and str(source.get("doc_id") or "") in document_names
        and _source_filename(source, document_names) in selected_filenames
        for source in sources
    ]
    expected = list(case.get("expected_any") or [])
    matched = sorted(set(expected) & set(distinct_filenames))
    expected_min = int(case.get("expected_min_distinct") or 0)
    evidence_diagnostics: dict[str, Any] = {}
    for filename, anchors in (case.get("evidence_anchors") or {}).items():
        texts = [
            _source_text(source)
            for source in sources
            if _source_filename(source, document_names) == filename
        ]
        haystack = _norm("\n".join(texts))
        hits = [anchor for anchor in anchors if _norm(anchor) in haystack]
        evidence_diagnostics[filename] = {
            "anchor_count": len(anchors),
            "anchor_hits": hits,
            "all_anchors_hit": len(hits) == len(anchors),
        }
    answer_norm = _norm(raw["answer"])
    refusal_marker_hits = [
        marker for marker in REFUSAL_MARKERS if _norm(marker) in answer_norm
    ]
    model_skipped = _model_skipped(raw["traces"])
    must_refuse = bool(case.get("must_refuse"))
    fail_closed = bool(refusal_marker_hits and model_skipped) if must_refuse else None
    return {
        "execution_id": f"{case['id']}::{tier}",
        "query_id": case["id"],
        "shape": case["shape"],
        "tier": tier,
        "question": case["question"],
        "elapsed_seconds": raw["elapsed_seconds"],
        "effective_tier": _effective_tier(raw["traces"]),
        "done_received": bool(raw["done"]),
        "errors": raw["errors"],
        "source_count": len(sources),
        "source_filenames": distinct_filenames,
        "source_membership_count": sum(1 for value in memberships if value),
        "all_sources_in_selected_corpus": all(memberships),
        "expected_filenames": expected,
        "matched_expected_filenames": matched,
        "doc_hit": bool(matched) if expected else None,
        "distinct_target_count": len(matched),
        "expected_min_distinct": expected_min,
        "min_distinct_met": len(matched) >= expected_min,
        "evidence_anchor_diagnostics": evidence_diagnostics,
        "must_refuse": must_refuse,
        "refusal_marker_hits": refusal_marker_hits,
        "model_skipped": model_skipped,
        "fail_closed": fail_closed,
        "answer_chars": len(raw["answer"]),
        "answer_sha256": hashlib.sha256(raw["answer"].encode("utf-8")).hexdigest(),
        "answer_excerpt": raw["answer"][:500],
    }


def _rate(rows: list[dict[str, Any]], field: str) -> float:
    if not rows:
        return 0.0
    return sum(1 for row in rows if row.get(field) is True) / len(rows)


def _finalize(prereg: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    direct = [row for row in results if row["shape"].startswith("direct_")]
    lay = [row for row in results if row["shape"] == "lay_language"]
    relationship = [
        row for row in results if row["shape"] == "relationship_multi_document"
    ]
    negatives = [row for row in results if row["shape"] == "negative_control"]
    total_sources = sum(int(row["source_count"]) for row in results)
    member_sources = sum(int(row["source_membership_count"]) for row in results)
    metrics = {
        "execution_count": len(results),
        "all_tiers_present": sorted({row["tier"] for row in results}) == sorted(TIERS),
        "effective_tier_match_rate": _rate(
            [{**row, "match": row["effective_tier"] == row["tier"]} for row in results],
            "match",
        ),
        "corpus_boundary_precision": (
            member_sources / total_sources if total_sources else 1.0
        ),
        "citation_source_membership_rate": (
            member_sources / total_sources if total_sources else 1.0
        ),
        "direct_doc_hit_rate": _rate(direct, "doc_hit"),
        "lay_language_doc_hit_rate": _rate(lay, "doc_hit"),
        "relationship_query_min_distinct_target_rate": _rate(
            relationship, "min_distinct_met"
        ),
        "negative_refusal_rate": _rate(negatives, "fail_closed"),
        "technical_success_rate": _rate(
            [
                {
                    **row,
                    "technical_success": not row["errors"]
                    and row["done_received"]
                    and row["effective_tier"] == row["tier"],
                }
                for row in results
            ],
            "technical_success",
        ),
        "total_citation_sources": total_sources,
    }
    targets = prereg["gates"]
    gates = {
        "execution_closure": len(results) == len(prereg["queries"]) * len(TIERS),
        "all_tiers_required": metrics["all_tiers_present"],
        "effective_tier_match": metrics["effective_tier_match_rate"] == 1.0,
        "corpus_boundary_precision": metrics["corpus_boundary_precision"]
        >= float(targets["corpus_boundary_precision"]),
        "direct_doc_hit_rate": metrics["direct_doc_hit_rate"]
        >= float(targets["direct_doc_hit_rate_min"]),
        "lay_language_doc_hit_rate": metrics["lay_language_doc_hit_rate"]
        >= float(targets["lay_language_doc_hit_rate_min"]),
        "relationship_query_min_distinct_target_rate": metrics[
            "relationship_query_min_distinct_target_rate"
        ]
        >= float(targets["relationship_query_min_distinct_target_rate_min"]),
        "negative_refusal_rate": metrics["negative_refusal_rate"]
        >= float(targets["negative_refusal_rate_min"]),
        "citation_source_membership_rate": metrics["citation_source_membership_rate"]
        >= float(targets["citation_source_membership_rate_min"]),
        "technical_success": metrics["technical_success_rate"] == 1.0,
    }
    return {"metrics": metrics, "gates": gates, "passed": all(gates.values())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--prereg", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 6:
        raise RuntimeError("retrieval eval concurrency must be between 1 and 6")
    prereg_bytes = args.prereg.read_bytes()
    if hashlib.sha256(prereg_bytes).hexdigest() != PREREG_SHA:
        raise RuntimeError("retrieval preregistration hash drifted")
    prereg = json.loads(prereg_bytes)
    if tuple(prereg.get("tiers") or ()) != TIERS:
        raise RuntimeError("retrieval preregistration tiers drifted")
    selection_bytes = args.selection.read_bytes()
    if hashlib.sha256(selection_bytes).hexdigest() != SELECTION_SHA:
        raise RuntimeError("document selection hash drifted")
    selection = json.loads(selection_bytes)
    selected_filenames = {str(row["filename"]) for row in selection["selected"]}
    if len(selected_filenames) != 15:
        raise RuntimeError("document selection did not close at 15 filenames")
    settings = get_settings()
    mongo = MongoClient(settings.MONGODB_URI)
    database = mongo[settings.MONGODB_DATABASE]
    try:
        corpus = database["corpora"].find_one(
            {"corpus_id": args.corpus_id}, {"_id": 0, "name": 1}
        )
        if not corpus or corpus.get("name") != "runpod_e2e_15doc_20260715":
            raise RuntimeError(
                "only the response-discovered fresh E2E corpus is admissible"
            )
        document_names = {
            str(row.get("doc_id") or ""): str(
                row.get("original_filename") or row.get("filename") or ""
            )
            for row in database["documents"].find(
                {"corpus_id": args.corpus_id},
                {"_id": 0, "doc_id": 1, "filename": 1, "original_filename": 1},
            )
        }
        if len(document_names) != 15:
            raise RuntimeError(
                f"retrieval corpus is not 15-document complete: {len(document_names)}"
            )
        token = _mint_token(database, args.corpus_id)
        if args.output.exists():
            state = json.loads(args.output.read_text(encoding="utf-8"))
            if (
                state.get("preregistration_sha256") != PREREG_SHA
                or state.get("corpus_id") != args.corpus_id
            ):
                raise RuntimeError("existing eval journal identity drifted")
        else:
            state = {
                "schema_version": "runpod_e2e_retrieval_results.v1",
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "completed_at_utc": None,
                "preregistration_sha256": PREREG_SHA,
                "corpus_id": args.corpus_id,
                "top_k": int(prereg["top_k"]),
                "targets": prereg["gates"],
                "results": [],
                "summary": None,
            }
            _atomic_write(args.output, state)
        completed = {row["execution_id"] for row in state["results"]}
        pending = [
            (case, tier)
            for case in prereg["queries"]
            for tier in TIERS
            if f"{case['id']}::{tier}" not in completed
        ]

        def run_execution(case: dict[str, Any], tier: str) -> dict[str, Any]:
            execution_id = f"{case['id']}::{tier}"
            print(f"EXECUTION_START {execution_id}", flush=True)
            raw = _run_sse(
                base=args.base,
                token=token,
                corpus_id=args.corpus_id,
                tier=tier,
                question=case["question"],
                top_k=int(prereg["top_k"]),
            )
            return _score_execution(
                case=case,
                tier=tier,
                raw=raw,
                corpus_id=args.corpus_id,
                document_names=document_names,
                selected_filenames=selected_filenames,
            )

        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = {
                executor.submit(run_execution, case, tier): (case, tier)
                for case, tier in pending
            }
            for future in as_completed(futures):
                case, tier = futures[future]
                execution_id = f"{case['id']}::{tier}"
                scored = future.result()
                state["results"].append(scored)
                state["summary"] = _finalize(prereg, state["results"])
                _atomic_write(args.output, state)
                print(
                    "EXECUTION_DONE "
                    + json.dumps(
                        {
                            "execution_id": execution_id,
                            "elapsed_seconds": scored["elapsed_seconds"],
                            "source_count": scored["source_count"],
                            "matched_expected_filenames": scored[
                                "matched_expected_filenames"
                            ],
                            "min_distinct_met": scored["min_distinct_met"],
                            "fail_closed": scored["fail_closed"],
                            "technical_ok": not scored["errors"]
                            and scored["done_received"]
                            and scored["effective_tier"] == tier,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
        query_order = {
            str(case["id"]): index for index, case in enumerate(prereg["queries"])
        }
        tier_order = {tier: index for index, tier in enumerate(TIERS)}
        state["results"].sort(
            key=lambda row: (
                query_order[str(row["query_id"])],
                tier_order[str(row["tier"])],
            )
        )
        state["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        state["summary"] = _finalize(prereg, state["results"])
        _atomic_write(args.output, state)
        print(json.dumps(state["summary"], indent=2, sort_keys=True), flush=True)
        return 0 if state["summary"]["passed"] else 1
    finally:
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
