#!/usr/bin/env python3
"""Build deterministic librarian_card.v0 cards for one or more corpora.

Runs INSIDE the backend container (motor + config get_settings — same
connection pattern as polymath_summary_backfill_scoped.py; never a host
.env).

Defaults are conservative:
  - dry-run unless --apply is passed: builds cards in memory, prints
    per-field coverage plus sample cards, writes NOTHING
  - --apply upserts cards into the ``librarian_cards`` Mongo collection
    (authoritative write; no Qdrant writes ever happen here)

Usage (inside the backend container):
  python scripts/build_librarian_cards.py --corpus UGO_CORPUS
  python scripts/build_librarian_cards.py --corpus <corpus_id> --apply
  python scripts/build_librarian_cards.py --all-active --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
BACKEND_ROOT = HERE.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.librarian.card_builder import (
    _field_coverage,
    build_corpus_cards,
    build_librarian_card,
    slim_card_payload,
)
from services.storage.record_status import with_active_records

log = logging.getLogger("build_librarian_cards")

_SAMPLE_ENTRIES_PER_FIELD = 3
_SAMPLE_VALUE_CHARS = 120
_SAMPLE_SOURCE_IDS = 3


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _mongo_db() -> tuple[AsyncIOMotorClient, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        db = client.get_default_database()
    except Exception:
        db = client[settings.MONGODB_DATABASE]
    return client, db


async def _resolve_corpora(
    db: Any, *, corpus: str | None, all_active: bool
) -> list[dict[str, Any]]:
    projection = {"_id": 0, "corpus_id": 1, "name": 1}
    if all_active:
        rows = await db["corpora"].find(
            with_active_records({}), projection
        ).to_list(length=None)
        return sorted(rows, key=lambda row: str(row.get("corpus_id") or ""))
    row = await db["corpora"].find_one(
        with_active_records({"corpus_id": corpus}), projection
    )
    if row is None:
        row = await db["corpora"].find_one(
            with_active_records({"name": corpus}), projection
        )
    return [row] if row else []


def _truncate_card(card: dict[str, Any]) -> dict[str, Any]:
    """Shrink a card for terminal display without changing its shape."""

    out: dict[str, Any] = {}
    for key, value in card.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            trimmed = []
            for entry in value[:_SAMPLE_ENTRIES_PER_FIELD]:
                entry = dict(entry)
                entry["value"] = str(entry.get("value") or "")[:_SAMPLE_VALUE_CHARS]
                ids = entry.get("source_ids") or []
                entry["source_ids"] = ids[:_SAMPLE_SOURCE_IDS] + (
                    [f"... +{len(ids) - _SAMPLE_SOURCE_IDS} more"]
                    if len(ids) > _SAMPLE_SOURCE_IDS
                    else []
                )
                trimmed.append(entry)
            if len(value) > _SAMPLE_ENTRIES_PER_FIELD:
                trimmed.append(f"... +{len(value) - _SAMPLE_ENTRIES_PER_FIELD} more")
            out[key] = trimmed
        elif key == "evidence_spans" and isinstance(value, dict):
            out[key] = {
                span_key: f"{len(ids)} ids (first: {ids[0] if ids else '-'})"
                for span_key, ids in value.items()
            }
        else:
            out[key] = value
    return out


async def _dry_run_corpus(
    db: Any, *, corpus_id: str, limit: int | None, samples: int
) -> dict[str, Any]:
    doc_rows = await db["documents"].find(
        with_active_records({"corpus_id": corpus_id}),
        {"_id": 0, "doc_id": 1},
    ).to_list(length=None)
    doc_ids = sorted(
        {str(row.get("doc_id") or "") for row in doc_rows if row.get("doc_id")}
    )
    if limit is not None:
        doc_ids = doc_ids[: max(0, int(limit))]

    cards: list[dict[str, Any]] = []
    skipped = 0
    rejected = 0
    for doc_id in doc_ids:
        card = await build_librarian_card(db, corpus_id=corpus_id, doc_id=doc_id)
        if card is None:
            skipped += 1
            continue
        rejected += int(card.get("rejected_value_count") or 0)
        cards.append(card)

    report = {
        "mode": "dry-run (no writes)",
        "corpus_id": corpus_id,
        "documents_scanned": len(doc_ids),
        "cards_buildable": len(cards),
        "cards_skipped_zero_seed": skipped,
        "values_rejected_missing_source_ids": rejected,
        "field_coverage": _field_coverage(cards),
    }
    print(json.dumps(report, indent=2, default=_json_default))
    for card in cards[: max(0, samples)]:
        print(f"\n--- sample card ({card['doc_id'][:16]}…) ---")
        print(json.dumps(_truncate_card(card), indent=2, default=_json_default))
        print("\n--- slim routing payload (returned, never written) ---")
        print(json.dumps(slim_card_payload(card), indent=2, default=_json_default))
    return report


async def _run(args: argparse.Namespace) -> int:
    client, db = _mongo_db()
    try:
        corpora = await _resolve_corpora(
            db, corpus=args.corpus, all_active=args.all_active
        )
        if not corpora:
            print(f"No active corpus matched {args.corpus!r}", file=sys.stderr)
            return 2
        for corpus in corpora:
            corpus_id = str(corpus["corpus_id"])
            print(
                f"\n=== corpus {corpus.get('name') or '?'} ({corpus_id}) ===",
            )
            if args.apply:
                result = await build_corpus_cards(
                    db, corpus_id=corpus_id, limit=args.limit
                )
                print(json.dumps(result, indent=2, default=_json_default))
            else:
                await _dry_run_corpus(
                    db,
                    corpus_id=corpus_id,
                    limit=args.limit,
                    samples=args.samples,
                )
        return 0
    finally:
        client.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--corpus", help="corpus_id or corpus name")
    target.add_argument(
        "--all-active",
        action="store_true",
        help="build for every active corpus",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="max documents per corpus"
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=2,
        help="sample cards printed in dry-run (default 2)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="upsert cards into Mongo librarian_cards (default: dry-run)",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
