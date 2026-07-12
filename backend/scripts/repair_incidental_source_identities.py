"""Repair colliding source identities derived from incidental YouTube links.

Historical ingestion accepted the first YouTube URL in the first 32 KiB of a
document as its source identity. Ebook publisher/channel links consequently
made unrelated files share one ``source_key``. This migration changes only
collision members whose URL is not a concrete YouTube video and whose durable
content hash is already present. It is dry-run by default and idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ingestion.source_identity import extract_youtube_video_id
from services.storage.record_status import with_active_records


_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}


def repair_patch(document: dict[str, Any]) -> dict[str, Any] | None:
    identity = document.get("source_identity") or {}
    source_key = str(identity.get("source_key") or document.get("source_key") or "")
    canonical_url = str(identity.get("source_url_canonical") or "")
    if not canonical_url and source_key.startswith("url:"):
        canonical_url = source_key[4:]
    try:
        host = (urlparse(canonical_url).hostname or "").lower()
    except Exception:
        return None
    content_hash = str(identity.get("content_sha256") or "").lower()
    if (
        str(identity.get("source_kind") or "") != "url"
        or host not in _YOUTUBE_HOSTS
        or extract_youtube_video_id(canonical_url)
        or not _SHA256_RE.fullmatch(content_hash)
    ):
        return None
    replacement = f"sha256:{content_hash}"
    if replacement == source_key:
        return None
    now = datetime.now(timezone.utc)
    return {
        "$set": {
            "source_key": replacement,
            "source_identity.source_kind": "content_hash",
            "source_identity.source_key": replacement,
            "source_identity.identity_repair": {
                "repair_version": "incidental_youtube_identity.v1",
                "previous_source_key": source_key,
                "repaired_at": now,
            },
            "updated_at": now,
        },
        "$unset": {
            "source_identity.declared_source_url": "",
            "source_identity.source_url_canonical": "",
            "source_identity.youtube_video_id": "",
        },
    }


async def run(*, corpus_id: str, apply: bool, limit: int) -> dict[str, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DATABASE]
    collision_rows = await db.documents.aggregate(
        [
            {
                "$match": with_active_records(
                    {
                        "corpus_id": corpus_id,
                        "source_key": {"$exists": True, "$nin": [None, ""]},
                    }
                )
            },
            {
                "$group": {
                    "_id": "$source_key",
                    "doc_ids": {"$push": "$doc_id"},
                    "hashes": {"$addToSet": "$source_identity.content_sha256"},
                    "count": {"$sum": 1},
                }
            },
            {
                "$match": {
                    "count": {"$gt": 1},
                    "$expr": {"$gt": [{"$size": "$hashes"}, 1]},
                }
            },
            {"$limit": max(1, int(limit))},
        ]
    ).to_list(length=None)
    doc_ids = list(
        dict.fromkeys(
            str(doc_id)
            for row in collision_rows
            for doc_id in row.get("doc_ids") or []
            if str(doc_id)
        )
    )
    documents = await db.documents.find(
        {"corpus_id": corpus_id, "doc_id": {"$in": doc_ids}},
        {
            "_id": 0,
            "doc_id": 1,
            "source_key": 1,
            "source_identity": 1,
        },
    ).to_list(length=None)
    repairable = [(row, repair_patch(row)) for row in documents]
    repairable = [(row, patch) for row, patch in repairable if patch]
    modified = 0
    if apply:
        for row, patch in repairable:
            result = await db.documents.update_one(
                {"corpus_id": corpus_id, "doc_id": row["doc_id"]},
                patch,
            )
            modified += int(result.modified_count)
    client.close()
    return {
        "corpus_id": corpus_id,
        "collision_groups_scanned": len(collision_rows),
        "collision_documents_scanned": len(documents),
        "repairable_documents": len(repairable),
        "modified_documents": modified,
        "dry_run": not apply,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(
        asyncio.run(
            run(
                corpus_id=args.corpus_id,
                apply=bool(args.apply),
                limit=max(1, args.limit),
            )
        )
    )


if __name__ == "__main__":
    main()
