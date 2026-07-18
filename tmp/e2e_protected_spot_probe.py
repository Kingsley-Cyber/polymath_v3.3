#!/usr/bin/env python3
"""Three-query, read-only retrieval spot probe for the protected ecom corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from bson import ObjectId
from config import get_settings
from e2e_retrieval_eval import _effective_tier, _run_sse, _source_filename
from pymongo import MongoClient
from services.auth import auth_service


PROTECTED = "fd460347-61cc-4358-87fc-4b2a80533f0a"
TIER = "qdrant_mongo_graph"
CASES = (
    {
        "id": "protected_direct_facs",
        "question": "What is the Facial Action Coding System used to code?",
        "expected": "Paul Ekman - Facial Action Coding System Manual (0).md",
    },
    {
        "id": "protected_direct_rule_of_six",
        "question": "What factors make up Walter Murch's Rule of Six for deciding a cut?",
        "expected": "Walter Murch - In the Blink of an Eye (2001).md",
    },
    {
        "id": "protected_direct_laban_machine",
        "question": (
            "How is Bayesian reasoning applied to Laban Movement Analysis "
            "in human-machine interaction?"
        ),
        "expected": (
            "[International Journal of Reasoning-based Intelligent Systems vol. 2 iss. 1] "
            "Bayesian reasoning for Laban Movement Analysis used in human-machine interaction"
            "{Rett, Jorg_ Dias, Jorge_ Ahuactzin, Juan Manuel}(2010)[1.md"
        ),
    },
)


def _atomic_write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _mint_token(database: Any) -> str:
    corpus = database["corpora"].find_one(
        {"corpus_id": PROTECTED}, {"_id": 0, "user_id": 1}
    )
    if not corpus or not corpus.get("user_id"):
        raise RuntimeError("protected corpus owner is absent")
    user_id = str(corpus["user_id"])
    if not ObjectId.is_valid(user_id):
        raise RuntimeError("protected corpus owner identity is invalid")
    user = database["users"].find_one(
        {"_id": ObjectId(user_id)}, {"_id": 1, "username": 1}
    )
    if not user or not user.get("username"):
        raise RuntimeError("protected corpus owner user is absent")
    return auth_service.create_access_token(
        user_id=str(user["_id"]), username=str(user["username"])
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", default="http://localhost:8000")
    args = parser.parse_args()

    settings = get_settings()
    mongo = MongoClient(settings.MONGODB_URI)
    try:
        database = mongo[settings.MONGODB_DATABASE]
        document_names = {
            str(row.get("doc_id") or ""): str(
                row.get("original_filename") or row.get("filename") or ""
            )
            for row in database["documents"].find(
                {"corpus_id": PROTECTED},
                {"_id": 0, "doc_id": 1, "filename": 1, "original_filename": 1},
            )
        }
        expected = {str(case["expected"]) for case in CASES}
        if not expected.issubset(set(document_names.values())):
            raise RuntimeError("protected probe target documents are absent")
        token = _mint_token(database)
        results = []
        for case in CASES:
            raw = _run_sse(
                base=args.base,
                token=token,
                corpus_id=PROTECTED,
                tier=TIER,
                question=str(case["question"]),
                top_k=10,
            )
            source_names = sorted(
                {
                    _source_filename(source, document_names)
                    for source in raw["sources"]
                    if _source_filename(source, document_names)
                }
            )
            memberships = [
                str(source.get("corpus_id") or "") == PROTECTED
                and str(source.get("doc_id") or "") in document_names
                for source in raw["sources"]
            ]
            passed = (
                not raw["errors"]
                and bool(raw["done"])
                and _effective_tier(raw["traces"]) == TIER
                and bool(raw["sources"])
                and all(memberships)
                and str(case["expected"]) in source_names
            )
            results.append(
                {
                    "id": case["id"],
                    "expected_filename": case["expected"],
                    "source_filenames": source_names,
                    "source_count": len(raw["sources"]),
                    "all_sources_in_protected_corpus": all(memberships),
                    "effective_tier": _effective_tier(raw["traces"]),
                    "done_received": bool(raw["done"]),
                    "errors": raw["errors"],
                    "elapsed_seconds": raw["elapsed_seconds"],
                    "answer_chars": len(raw["answer"]),
                    "answer_sha256": hashlib.sha256(
                        raw["answer"].encode("utf-8")
                    ).hexdigest(),
                    "passed": passed,
                }
            )
        output = {
            "schema_version": "e2e_protected_spot_probe.v1",
            "corpus_id": PROTECTED,
            "tier": TIER,
            "query_count": len(results),
            "results": results,
            "passed": all(row["passed"] for row in results),
            "writes_performed": 0,
        }
        _atomic_write(args.output, output)
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if output["passed"] else 1
    finally:
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
