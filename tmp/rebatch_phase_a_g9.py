#!/usr/bin/env python3
"""Real /api/chat SSE assertions for REBATCH_RUNBOOK Phase A g9."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.request
from typing import Any

from bson import ObjectId
from config import get_settings
from pymongo import MongoClient
from services.auth import auth_service


CASES = (
    {
        "id": "direct",
        "query": (
            "Who authored Field Notes on Community Garden Stewardship, "
            "and when was it published?"
        ),
        "answer_anchors": (("maria okafor",), ("2019",)),
        "source_anchors": ("garden", "stewardship"),
    },
    {
        "id": "plain_language",
        "query": (
            "What happened after the winter 1911 supply delivery to Skerry "
            "Point was missed?"
        ),
        "answer_anchors": (
            ("lamp oil", "oil"),
            ("alternating nights", "alternate nights", "every other night"),
            ("two groundings", "2 groundings"),
        ),
        "source_anchors": ("lighthouse", "skerry"),
    },
    {
        "id": "verified_absent",
        "query": (
            "According to this corpus, what did the Velocitron Cryo-Orchid "
            "protocol require at Neptune Vault?"
        ),
        "absent_terms": ("velocitron", "cryo orchid", "neptune vault"),
    },
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold())).strip()


def mint_probe_token(db, corpus_id: str) -> str:
    corpus = db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "user_id": 1},
    )
    require(bool(corpus and corpus.get("user_id")), "corpus owner is absent")
    user_id = str(corpus["user_id"])
    require(ObjectId.is_valid(user_id), "corpus owner id is invalid")
    user = db["users"].find_one(
        {"_id": ObjectId(user_id)},
        {"_id": 1, "username": 1},
    )
    require(bool(user and user.get("username")), "corpus owner user is absent")
    return auth_service.create_access_token(
        user_id=str(user["_id"]),
        username=str(user["username"]),
    )


def corpus_text(db, corpus_id: str) -> str:
    parts: list[str] = []
    for collection in ("chunks", "parent_chunks"):
        for row in db[collection].find(
            {"corpus_id": corpus_id},
            {"_id": 0, "text": 1, "summary": 1},
        ):
            parts.extend((str(row.get("text") or ""), str(row.get("summary") or "")))
    return norm("\n".join(parts))


def source_label(source: dict[str, Any], document_names: dict[str, str]) -> str:
    discovered_name = document_names.get(str(source.get("doc_id") or ""), "")
    heading_path = source.get("heading_path") or []
    return str(
        source.get("filename")
        or source.get("doc_name")
        or source.get("document_name")
        or source.get("title")
        or discovered_name
        or (" > ".join(str(part) for part in heading_path) if heading_path else "")
        or source.get("doc_id")
        or ""
    )


def run_sse(
    *,
    base: str,
    token: str,
    corpus_id: str,
    tier: str,
    query: str,
) -> dict[str, Any]:
    body = json.dumps(
        {
            "message": query,
            "corpus_ids": [corpus_id],
            "retrieval_tier": tier,
            "overrides": {"hyde_enabled": False},
        }
    ).encode()
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/chat",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    started = time.perf_counter()
    current_event = None
    answer: list[str] = []
    sources: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    errors: list[str] = []
    done: dict[str, Any] = {}
    with urllib.request.urlopen(request, timeout=600) as response:
        require(response.status == 200, f"chat HTTP status {response.status}")
        require(
            "text/event-stream" in str(response.headers.get("Content-Type") or ""),
            "chat did not return SSE",
        )
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
                sources = list(obj.get("sources") or obj.get("data") or [])
            elif event_type == "trace_event" or obj.get("trace_event"):
                traces.append(dict(obj.get("trace_event") or obj))
            elif event_type == "error":
                errors.append(str(obj.get("content") or obj.get("error") or "unknown"))
            elif event_type == "done":
                done = obj
    return {
        "answer": "".join(answer),
        "sources": sources,
        "traces": traces,
        "errors": errors,
        "done": done,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def effective_tier(result: dict[str, Any]) -> str:
    for trace in reversed(result["traces"]):
        if trace.get("title") == "Local RAG retrieval":
            value = str((trace.get("metadata") or {}).get("effective_tier") or "")
            if value:
                return value
    return ""


def model_skipped(result: dict[str, Any]) -> bool:
    return any(
        trace.get("title") == "Assistant final answer"
        and (trace.get("metadata") or {}).get("model_skipped") is True
        for trace in result["traces"]
    )


def validate_answerable(
    case: dict[str, Any],
    result: dict[str, Any],
    *,
    corpus_id: str,
    tier: str,
    document_names: dict[str, str],
) -> dict[str, Any]:
    answer_norm = norm(result["answer"])
    missing_answer_anchors = [
        list(group)
        for group in case["answer_anchors"]
        if not any(norm(term) in answer_norm for term in group)
    ]
    sources = result["sources"]
    citation_docs = sorted(
        {
            source_label(source, document_names)
            for source in sources
            if source_label(source, document_names)
        }
    )
    corpus_citations = [
        source for source in sources if str(source.get("corpus_id") or "") == corpus_id
    ]
    desired_source_hits = [
        label
        for label in citation_docs
        if any(anchor in norm(label) for anchor in case["source_anchors"])
    ]
    require(not result["errors"], f"{case['id']} SSE error events: {result['errors']}")
    require(bool(result["done"]), f"{case['id']} missing done event")
    require(effective_tier(result) == tier, f"{case['id']} effective tier mismatch")
    require(not missing_answer_anchors, f"{case['id']} missing answer anchors: {missing_answer_anchors}")
    require(bool(corpus_citations), f"{case['id']} has no smoke-corpus citation sources")
    require(bool(desired_source_hits), f"{case['id']} lacks expected document citation")
    return {
        "case": case["id"],
        "answer_chars": len(result["answer"]),
        "answer_sha256": hashlib.sha256(result["answer"].encode()).hexdigest(),
        "missing_answer_anchors": missing_answer_anchors,
        "citation_count": len(sources),
        "smoke_corpus_citation_count": len(corpus_citations),
        "citation_documents": citation_docs,
        "expected_document_citation_hits": desired_source_hits,
        "effective_tier": effective_tier(result),
        "elapsed_seconds": result["elapsed_seconds"],
    }


def validate_absent(
    case: dict[str, Any],
    result: dict[str, Any],
    *,
    corpus_id: str,
    tier: str,
    all_corpus_text: str,
) -> dict[str, Any]:
    present_terms = [term for term in case["absent_terms"] if norm(term) in all_corpus_text]
    answer_norm = norm(result["answer"])
    refusal_markers = (
        "cannot answer",
        "did not find source evidence",
        "did not establish",
        "not contain the answer",
        "not in the selected corpus",
    )
    fail_closed = any(norm(marker) in answer_norm for marker in refusal_markers)
    require(not present_terms, f"absent-topic terms unexpectedly present: {present_terms}")
    require(not result["errors"], f"absent SSE error events: {result['errors']}")
    require(bool(result["done"]), "absent case missing done event")
    require(effective_tier(result) == tier, "absent case effective tier mismatch")
    require(fail_closed, "absent-topic answer did not refuse as corpus-unsupported")
    require(model_skipped(result), "absent-topic did not trigger model-skipped fail-close")
    return {
        "case": case["id"],
        "corpus_absence_verified": True,
        "verified_absent_terms": list(case["absent_terms"]),
        "answer_chars": len(result["answer"]),
        "answer_sha256": hashlib.sha256(result["answer"].encode()).hexdigest(),
        "fail_closed": fail_closed,
        "model_skipped": model_skipped(result),
        "citation_count": len(result["sources"]),
        "effective_tier": effective_tier(result),
        "elapsed_seconds": result["elapsed_seconds"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument(
        "--tier",
        required=True,
        choices=("qdrant_only", "qdrant_mongo", "qdrant_mongo_graph"),
    )
    parser.add_argument("--base", default="http://localhost:8000")
    args = parser.parse_args()
    settings = get_settings()
    mongo = MongoClient(settings.MONGODB_URI)
    db = mongo[settings.MONGODB_DATABASE]
    try:
        token = mint_probe_token(db, args.corpus_id)
        all_text = corpus_text(db, args.corpus_id)
        document_names = {
            str(row.get("doc_id") or ""): str(
                row.get("filename") or row.get("original_filename") or ""
            )
            for row in db["documents"].find(
                {"corpus_id": args.corpus_id},
                {"_id": 0, "doc_id": 1, "filename": 1, "original_filename": 1},
            )
        }
        reports: list[dict[str, Any]] = []
        for case in CASES:
            print(f"CASE_START tier={args.tier} case={case['id']}", flush=True)
            result = run_sse(
                base=args.base,
                token=token,
                corpus_id=args.corpus_id,
                tier=args.tier,
                query=case["query"],
            )
            if case["id"] == "verified_absent":
                report = validate_absent(
                    case,
                    result,
                    corpus_id=args.corpus_id,
                    tier=args.tier,
                    all_corpus_text=all_text,
                )
            else:
                report = validate_answerable(
                    case,
                    result,
                    corpus_id=args.corpus_id,
                    tier=args.tier,
                    document_names=document_names,
                )
            reports.append(report)
            print(json.dumps(report, sort_keys=True), flush=True)
        print(
            json.dumps(
                {
                    "gate": "g9",
                    "tier": args.tier,
                    "real_sse_cases": len(reports),
                    "probe_token_used": True,
                    "reports": reports,
                },
                indent=2,
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    finally:
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
