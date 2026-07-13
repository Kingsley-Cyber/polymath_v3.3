#!/usr/bin/env python3
"""READ-ONLY dry-run probe for the P1.5 shelf-role engine.

Loads the live ``librarian_cards`` for the requested corpora, runs the pure
:func:`services.librarian.shelf_engine.assign_shelf_roles`, and prints
``shelf_counts`` plus the top 3 documents per shelf with their matched fields
(and the full chain for bridge seats). This script performs Mongo ``find``
reads ONLY — no writes anywhere, no Qdrant, no LLM.

Usage (inside the backend container):

    python scripts/probe_shelf_roles.py \
        --corpus markbuildsbrands_transcripts \
        --corpus ecommerce_AI_FILM_SCHOOL \
        --query-concepts attention,persuasion,visual_storytelling
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
BACKEND_ROOT = HERE.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.librarian.card_builder import CARD_COLLECTION
from services.librarian.shelf_engine import ALL_ROLES, assign_shelf_roles

TOP_PER_SHELF = 3


def _mongo_db() -> tuple[AsyncIOMotorClient, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = client.get_default_database()
    except Exception:
        db = client[settings.MONGODB_DATABASE]
    return client, db


async def _resolve_corpus(db: Any, ref: str) -> dict[str, str] | None:
    row = await db["corpora"].find_one(
        {"$or": [{"corpus_id": ref}, {"name": ref}]},
        {"_id": 0, "corpus_id": 1, "name": 1},
    )
    if not row or not row.get("corpus_id"):
        return None
    return {"corpus_id": str(row["corpus_id"]), "name": str(row.get("name") or "")}


def _print_role_row(assignment: dict, role: dict, corpus_names: dict[str, str]) -> None:
    corpus = corpus_names.get(assignment["corpus_id"], assignment["corpus_id"])
    print(
        f"    doc={assignment['doc_id'][:16]}… corpus={corpus} "
        f"score={role['score']}"
    )
    print(f"      matched_fields={json.dumps(role['matched_fields'])}")
    print(f"      evidence_ids={len(role['evidence_ids'])} reasons={role['reasons']}")
    for chain in role.get("chains") or []:
        print(
            "      chain: document={document} -> concept={concept} -> "
            "transferable_principle={transferable_principle} -> "
            "user_goal={user_goal} (via {via_field})".format(**chain)
        )


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY shelf-role dry run over live librarian_cards."
    )
    parser.add_argument(
        "--corpus",
        action="append",
        required=True,
        help="Corpus name or corpus_id (repeatable).",
    )
    parser.add_argument(
        "--query-concepts",
        required=True,
        help="Comma-separated normalized snake_case concept/capability ids.",
    )
    args = parser.parse_args()

    query_concepts = [c.strip() for c in args.query_concepts.split(",") if c.strip()]
    if not query_concepts:
        print("ERROR: --query-concepts resolved to an empty list", file=sys.stderr)
        return 2

    client, db = _mongo_db()
    try:
        corpora: list[dict[str, str]] = []
        for ref in args.corpus:
            resolved = await _resolve_corpus(db, ref)
            if resolved is None:
                print(f"ERROR: corpus not found: {ref!r}", file=sys.stderr)
                return 2
            corpora.append(resolved)
        corpus_ids = sorted({c["corpus_id"] for c in corpora})
        corpus_names = {c["corpus_id"]: c["name"] for c in corpora}

        cards = (
            await db[CARD_COLLECTION]
            .find({"corpus_id": {"$in": corpus_ids}}, {"_id": 0})
            .to_list(length=None)
        )

        result = assign_shelf_roles(query_concepts, cards)

        print(f"policy_version: {result['policy_version']}")
        print(f"query_concepts: {query_concepts}")
        for corpus in corpora:
            n = sum(1 for card in cards if card.get("corpus_id") == corpus["corpus_id"])
            print(f"corpus: {corpus['name']} ({corpus['corpus_id']}) cards={n}")
        print(f"documents_with_roles: {len(result['assignments'])}")
        print(f"shelf_counts: {json.dumps(result['shelf_counts'])}")
        if result["skipped_roles"]:
            print(f"skipped_roles: {json.dumps(result['skipped_roles'], indent=2)}")

        for shelf in ALL_ROLES:
            rows = [
                (assignment, role)
                for assignment in result["assignments"]
                for role in assignment["roles"]
                if role["role"] == shelf
            ]
            rows.sort(
                key=lambda item: (
                    -item[1]["score"],
                    item[0]["doc_id"],
                    item[0]["corpus_id"],
                )
            )
            print(f"\nshelf={shelf} ({len(rows)} docs, top {TOP_PER_SHELF}):")
            if not rows:
                print("    (none)")
            for assignment, role in rows[:TOP_PER_SHELF]:
                _print_role_row(assignment, role, corpus_names)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
