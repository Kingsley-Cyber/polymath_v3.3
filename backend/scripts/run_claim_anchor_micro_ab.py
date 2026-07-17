#!/usr/bin/env python3
"""Run one read-only arm of the preregistered claim-anchor micro A/B."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import BSON, ObjectId
from config import get_settings
from pymongo import MongoClient
from services.auth import auth_service


BACKEND = Path(__file__).resolve().parents[1]
QUESTIONS = BACKEND / "evals" / "heldout_questions.jsonl"
DEFAULT_SPEC = BACKEND / "evals" / "claim_anchor_join_micro_ab_v1.json"
CORPUS_COLLECTIONS = (
    "documents",
    "chunks",
    "parent_chunks",
    "summary_tree",
    "corpus_lexicon",
    "ghost_b_extractions",
    "semantic_digest_claim_compilations",
)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
        + b"\n"
    )
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _mint_token(db: Any, corpus_id: str) -> str:
    corpus = db.corpora.find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "user_id": 1},
    )
    user_id = str((corpus or {}).get("user_id") or "")
    if not ObjectId.is_valid(user_id):
        raise RuntimeError("corpus owner identity is absent or invalid")
    user = db.users.find_one(
        {"_id": ObjectId(user_id)},
        {"_id": 1, "username": 1},
    )
    if not user or not user.get("username"):
        raise RuntimeError("corpus owner user is absent")
    return auth_service.create_access_token(
        user_id=str(user["_id"]),
        username=str(user["username"]),
    )


def _run_sse(
    *,
    base: str,
    token: str,
    corpus_id: str,
    tier: str,
    question: str,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message": question,
        "corpus_ids": [corpus_id],
        "retrieval_tier": tier,
    }
    if conversation_id:
        body["conversation_id"] = conversation_id
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/chat",
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
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
    resolved_conversation_id = conversation_id
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            if response.status != 200:
                raise RuntimeError(f"chat HTTP status {response.status}")
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event:"):
                    current_event = line.split(":", 1)[1].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except Exception:
                    continue
                resolved_conversation_id = str(
                    event.get("conversation_id") or resolved_conversation_id or ""
                )
                event_type = event.get("type") or current_event
                if event_type == "token":
                    answer.append(str(event.get("content") or event.get("token") or ""))
                elif event_type == "sources":
                    raw_sources = event.get("sources") or event.get("data") or []
                    sources = raw_sources if isinstance(raw_sources, list) else []
                elif event_type == "trace_event" or event.get("trace_event"):
                    traces.append(dict(event.get("trace_event") or event))
                elif event_type == "error":
                    errors.append(
                        str(event.get("content") or event.get("error") or "")[:500]
                    )
                elif event_type == "done":
                    done = event
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return {
        "answer": "".join(answer),
        "sources": sources,
        "traces": traces,
        "errors": errors,
        "done": done,
        "conversation_id": resolved_conversation_id,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def _fingerprint(db: Any, corpus_id: str) -> dict[str, Any]:
    collections: dict[str, Any] = {}
    combined = hashlib.sha256()
    for name in CORPUS_COLLECTIONS:
        query_key = (
            "corpus_id" if name != "semantic_digest_claim_compilations" else "corpus_id"
        )
        digest = hashlib.sha256()
        count = 0
        for row in db[name].find({query_key: corpus_id}).sort("_id", 1):
            digest.update(BSON.encode(row))
            count += 1
        value = {"count": count, "sha256": digest.hexdigest()}
        collections[name] = value
        combined.update(name.encode("utf-8"))
        combined.update(str(count).encode("ascii"))
        combined.update(value["sha256"].encode("ascii"))
    return {
        "collections": collections,
        "combined_sha256": combined.hexdigest(),
    }


def _mapped_child_ids(parent: dict[str, Any]) -> list[str]:
    child_ids = [str(value) for value in (parent.get("child_ids") or []) if value]
    source_child_ids = [
        str(value) for value in (parent.get("source_child_ids") or []) if value
    ]
    if any(len(values) != len(set(values)) for values in (child_ids, source_child_ids)):
        return []
    if child_ids and source_child_ids and set(child_ids) != set(source_child_ids):
        return []
    return source_child_ids or child_ids


def _validate_anchor(
    db: Any,
    *,
    source: dict[str, Any],
    anchor: dict[str, Any],
) -> dict[str, bool]:
    corpus_id = str(source.get("corpus_id") or "")
    doc_id = str(source.get("doc_id") or "")
    child_id = str(anchor.get("child_id") or "")
    row = db.semantic_digest_claim_compilations.find_one(
        {
            "corpus_id": corpus_id,
            "document_id": doc_id,
            "child_id": child_id,
        }
    )
    child = db.chunks.find_one(
        {"corpus_id": corpus_id, "doc_id": doc_id, "chunk_id": child_id}
    )
    document = db.documents.find_one({"corpus_id": corpus_id, "doc_id": doc_id})
    if not row or not child or not document:
        return {
            "selected_source_ownership": False,
            "exact_span": False,
            "claim_identity": False,
            "provenance_closure": False,
            "valid": False,
        }

    selected_chunk_id = str(source.get("chunk_id") or "")
    direct = selected_chunk_id == child_id
    mapped = False
    if not direct and selected_chunk_id.endswith("_summary"):
        parent_id = str(
            anchor.get("mapped_parent_id")
            or source.get("parent_id")
            or selected_chunk_id.removesuffix("_summary")
        )
        parents = list(
            db.parent_chunks.find(
                {
                    "corpus_id": corpus_id,
                    "doc_id": doc_id,
                    "parent_id": parent_id,
                    "$or": [
                        {"status": {"$exists": False}},
                        {"status": "active"},
                    ],
                }
            ).limit(2)
        )
        mapped = bool(
            len(parents) == 1
            and parent_id == selected_chunk_id.removesuffix("_summary")
            and str(anchor.get("selected_chunk_id") or "") == selected_chunk_id
            and child_id in _mapped_child_ids(parents[0])
        )

    evidence = {
        str(item.get("evidence_ref_id") or ""): item
        for item in row.get("evidence_refs") or []
    }.get(str(anchor.get("evidence_ref_id") or ""))
    claims = {
        str(item.get("claim_id") or ""): item
        for item in (
            ((row.get("envelope") or {}).get("body") or {}).get("claims") or []
        )
    }
    claim = claims.get(str(anchor.get("claim_id") or ""))
    start = int(anchor.get("start") or 0)
    end = int(anchor.get("end") or 0)
    sentence = str(anchor.get("exact_sentence") or "")
    selected_source_ownership = bool(
        (direct or mapped)
        and corpus_id
        and doc_id
        and str(row.get("corpus_id") or "") == corpus_id
        and str(row.get("document_id") or "") == doc_id
        and str(row.get("child_id") or "") == child_id
    )
    exact_span = bool(
        evidence
        and str(child.get("text") or "")[start:end] == sentence
        and str(evidence.get("quote") or "") == sentence
        and int(evidence.get("start") or 0) == start
        and int(evidence.get("end") or 0) == end
    )
    claim_identity = bool(
        claim
        and str(claim.get("canonical_proposition") or "")
        == str(anchor.get("claim_text") or "")
        and str(anchor.get("evidence_ref_id") or "")
        in (claim.get("evidence_sentence_ids") or [])
    )
    provenance_closure = bool(
        str(row.get("source_version_id") or "")
        == str(anchor.get("source_version_id") or "")
        and str((row.get("envelope") or {}).get("artifact_revision_id") or "")
        == str(anchor.get("compilation_revision_id") or "")
    )
    return {
        "selected_source_ownership": selected_source_ownership,
        "exact_span": exact_span,
        "claim_identity": claim_identity,
        "provenance_closure": provenance_closure,
        "valid": (
            selected_source_ownership
            and exact_span
            and claim_identity
            and provenance_closure
        ),
    }


def _anchor_trace(traces: list[dict[str, Any]]) -> dict[str, Any]:
    for trace in reversed(traces):
        if trace.get("title") == "Atomic claim anchors":
            metadata = trace.get("metadata")
            return dict(metadata) if isinstance(metadata, dict) else {}
    for trace in reversed(traces):
        if trace.get("title") == "Local RAG retrieval":
            metadata = trace.get("metadata") or {}
            anchors = metadata.get("atomic_claim_anchors")
            return dict(anchors) if isinstance(anchors, dict) else {}
    return {}


def _assistant_model(db: Any, conversation_id: str) -> str:
    identity: Any = (
        ObjectId(conversation_id)
        if ObjectId.is_valid(conversation_id)
        else conversation_id
    )
    row = db.messages.find_one(
        {"conversation_id": identity, "role": "assistant"},
        sort=[("created_at", -1)],
    )
    return str((row or {}).get("model_used") or "")


def _source_without_claim_anchors(source: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(source)
    metadata = dict(sanitized.get("metadata") or {})
    metadata.pop("atomic_claim_anchors", None)
    sanitized["metadata"] = metadata
    return sanitized


def _source_fingerprint(sources: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        [_source_without_claim_anchors(source) for source in sources],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_contract(spec_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    questions_bytes = QUESTIONS.read_bytes()
    if hashlib.sha256(questions_bytes).hexdigest() != str(
        spec["heldout_questions_sha256"]
    ):
        raise RuntimeError("frozen held-out question hash drifted")
    selected = set(spec["query_ids"])
    rows = [
        json.loads(line)
        for line in questions_bytes.decode("utf-8").splitlines()
        if line.strip()
    ]
    by_id = {str(row["id"]): row for row in rows}
    if set(by_id) & selected != selected:
        raise RuntimeError("micro A/B query ID is absent from frozen questions")
    ordered = [by_id[str(query_id)] for query_id in spec["query_ids"]]
    if any(row.get("corpora") != [spec["corpus_name"]] for row in ordered):
        raise RuntimeError("micro A/B corpus contract drifted")
    return spec, ordered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-flag", choices=("off", "on"), required=True)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    spec, questions = _load_contract(args.spec)
    expected_enabled = args.expected_flag == "on"
    settings = get_settings()
    if bool(settings.ATOMIC_CLAIM_ANCHORS_ENABLED) != expected_enabled:
        raise RuntimeError("runtime claim-anchor flag does not match requested arm")

    mongo = MongoClient(settings.MONGODB_URI)
    db = mongo[settings.MONGODB_DATABASE]
    try:
        corpus = db.corpora.find_one(
            {"name": spec["corpus_name"], "status": "active"},
            {"_id": 0, "corpus_id": 1},
        )
        corpus_id = str((corpus or {}).get("corpus_id") or "")
        if not corpus_id:
            raise RuntimeError("micro A/B corpus is absent")
        token = _mint_token(db, corpus_id)
        before = _fingerprint(db, corpus_id)
        results: list[dict[str, Any]] = []
        for question in questions:
            conversation_id: str | None = None
            for history_turn in question.get("history") or []:
                prior = _run_sse(
                    base=args.base,
                    token=token,
                    corpus_id=corpus_id,
                    tier=spec["tier"],
                    question=history_turn,
                )
                if prior["errors"]:
                    raise RuntimeError(
                        f"{question['id']} history failed: {prior['errors']}"
                    )
                conversation_id = str(prior["conversation_id"] or "")
            raw = _run_sse(
                base=args.base,
                token=token,
                corpus_id=corpus_id,
                tier=spec["tier"],
                question=question["question"],
                conversation_id=conversation_id,
            )
            anchors: list[tuple[dict[str, Any], dict[str, Any]]] = []
            source_keys: list[dict[str, str]] = []
            for source in raw["sources"]:
                source_keys.append(
                    {
                        "corpus_id": str(source.get("corpus_id") or ""),
                        "doc_id": str(source.get("doc_id") or ""),
                        "chunk_id": str(source.get("chunk_id") or ""),
                        "parent_id": str(source.get("parent_id") or ""),
                    }
                )
                for anchor in (source.get("metadata") or {}).get(
                    "atomic_claim_anchors"
                ) or []:
                    if isinstance(anchor, dict):
                        anchors.append((source, anchor))
            checks = [
                _validate_anchor(db, source=source, anchor=anchor)
                for source, anchor in anchors
            ]
            valid_count = sum(int(check["valid"]) for check in checks)
            trace = _anchor_trace(raw["traces"])
            model_used = str(
                raw["done"].get("model_used")
                or _assistant_model(
                    db,
                    str(raw["conversation_id"] or ""),
                )
            )
            results.append(
                {
                    "query_id": question["id"],
                    "shape": question["shape"],
                    "source_keys": source_keys,
                    "source_count": len(source_keys),
                    "selected_evidence_sha256_without_anchors": _source_fingerprint(
                        raw["sources"]
                    ),
                    "anchor_count": len(anchors),
                    "valid_anchor_count": valid_count,
                    "citation_precision": (
                        valid_count / len(anchors) if anchors else None
                    ),
                    "all_citations_valid": (
                        all(check["valid"] for check in checks) if checks else None
                    ),
                    "anchor_trace": trace,
                    "prompt_render_count": int(trace.get("prompt_render_count") or 0),
                    "model_used": model_used,
                    "elapsed_seconds": raw["elapsed_seconds"],
                    "done_received": bool(raw["done"]),
                    "errors": raw["errors"],
                    "answer_sha256": hashlib.sha256(
                        raw["answer"].encode("utf-8")
                    ).hexdigest(),
                }
            )
            print(
                json.dumps(
                    {
                        "query_id": question["id"],
                        "sources": len(source_keys),
                        "anchors": len(anchors),
                        "valid": valid_count,
                        "rendered": int(trace.get("prompt_render_count") or 0),
                        "model": model_used,
                        "errors": raw["errors"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        after = _fingerprint(db, corpus_id)
        fingerprint_equal = before == after
        required_positive = set(spec["required_positive_anchor_ids_when_on"])
        failures: list[str] = []
        for row in results:
            if row["errors"] or not row["done_received"]:
                failures.append(f"{row['query_id']}:technical")
            if row["model_used"] != spec["model_contract"]:
                failures.append(f"{row['query_id']}:model_contract")
            if row["anchor_count"] and not row["all_citations_valid"]:
                failures.append(f"{row['query_id']}:citation_invalid")
            if not expected_enabled and (
                row["anchor_count"] or row["prompt_render_count"]
            ):
                failures.append(f"{row['query_id']}:off_exposure")
            if (
                expected_enabled
                and row["query_id"] in required_positive
                and (
                    not row["anchor_count"]
                    or not row["prompt_render_count"]
                    or row["anchor_count"] != row["prompt_render_count"]
                )
            ):
                failures.append(f"{row['query_id']}:required_anchor_missing")
        if not fingerprint_equal:
            failures.append("corpus_fingerprint_changed")
        output = {
            "schema_version": "claim_anchor_join_micro_ab_arm.v1",
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "arm": args.expected_flag,
            "runtime_flag_enabled": expected_enabled,
            "spec": spec,
            "corpus_id": corpus_id,
            "model_contract": spec["model_contract"],
            "corpus_fingerprint_before": before,
            "corpus_fingerprint_after": after,
            "corpus_fingerprint_equal": fingerprint_equal,
            "results": results,
            "failures": failures,
            "passed": not failures,
        }
        _atomic_write(args.output, output)
        print(
            json.dumps(
                {
                    "arm": args.expected_flag,
                    "passed": not failures,
                    "failures": failures,
                    "corpus_fingerprint_equal": fingerprint_equal,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0 if not failures else 1
    finally:
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
