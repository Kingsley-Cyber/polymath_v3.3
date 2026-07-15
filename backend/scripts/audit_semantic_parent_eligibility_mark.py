#!/usr/bin/env python3
"""Read-only B1 census for mark semantic-parent eligibility v2."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ingestion.semantic_parent_eligibility import (
    classify_parent_text_v2,
    parent_eligibility_recipe_hash,
)
from services.settings import settings_service

SCHEMA_VERSION = "polymath.semantic_parent_eligibility_census.v2"
COMPILATION_COLLECTION = "semantic_digest_claim_compilations"


class CensusError(RuntimeError):
    """The live structural or eligibility population drifted."""


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = client.get_default_database()
        except Exception:
            db = client[settings.MONGODB_DATABASE]
        settings_service.attach(db)
        corpora = (
            await db["corpora"]
            .find(
                {"name": args.corpus_name, "status": {"$ne": "deleted"}},
                {"_id": 0, "corpus_id": 1},
            )
            .to_list(length=3)
        )
        if len(corpora) != 1:
            raise CensusError(
                f"expected one active corpus named {args.corpus_name!r}; "
                f"found {len(corpora)}"
            )
        corpus_id = str(corpora[0].get("corpus_id") or "")
        parents = (
            await db["parent_chunks"]
            .find(
                {
                    "corpus_id": corpus_id,
                    "validation_status": "valid",
                    "text": {"$exists": True, "$nin": [None, ""]},
                    "child_ids.0": {"$exists": True},
                },
                {"_id": 0, "text": 1},
            )
            .to_list(length=None)
        )
        reasons: Counter[str] = Counter()
        for parent in parents:
            reasons[classify_parent_text_v2(parent["text"]).reason] += 1

        base_count = len(parents)
        eligible_count = reasons["eligible"]
        if base_count != args.expected_base_count:
            raise CensusError(
                f"base census drifted: expected {args.expected_base_count}, "
                f"found {base_count}"
            )
        if eligible_count != args.expected_eligible_count:
            raise CensusError(
                f"eligible census drifted: expected {args.expected_eligible_count}, "
                f"found {eligible_count}"
            )
        if sum(reasons.values()) != base_count:
            raise CensusError("eligibility reason accounting does not close")

        compilation_rows = await db[COMPILATION_COLLECTION].count_documents(
            {"corpus_id": corpus_id}
        )
        compilation_noncanonical = await db[COMPILATION_COLLECTION].count_documents(
            {"corpus_id": corpus_id, "canonical_write": False}
        )
        compilation_canonical_or_missing = await db[
            COMPILATION_COLLECTION
        ].count_documents(
            {
                "corpus_id": corpus_id,
                "$or": [
                    {"canonical_write": {"$ne": False}},
                    {"canonical_write": {"$exists": False}},
                ],
            }
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "corpus": {
                "name": args.corpus_name,
                "corpus_id": corpus_id,
            },
            "recipe": {
                "schema_version": "semantic_parent_eligibility.v2",
                "recipe_hash": parent_eligibility_recipe_hash(),
                "substantive_byte_min": 256,
                "comparison": "greater_than_or_equal",
            },
            "census": {
                "structural_base_count": base_count,
                "heading_only_count": reasons["heading_only"],
                "below_substantive_byte_min_count": reasons[
                    "below_substantive_byte_min"
                ],
                "eligible_count": eligible_count,
                "accounting_closed": sum(reasons.values()) == base_count,
            },
            "disclosed_noncanonical_stores": {
                COMPILATION_COLLECTION: {
                    "row_count": compilation_rows,
                    "canonical_write_false_count": compilation_noncanonical,
                    "canonical_or_missing_flag_count": compilation_canonical_or_missing,
                }
            },
            "writes": 0,
            "provider_calls": 0,
        }
    finally:
        client.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--corpus-name",
        default="markbuildsbrands_transcripts",
    )
    parser.add_argument("--expected-base-count", type=int, default=989)
    parser.add_argument("--expected-eligible-count", type=int, default=795)
    return parser.parse_args()


def main() -> int:
    print(json.dumps(asyncio.run(run(_parse_args())), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
