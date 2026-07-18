#!/usr/bin/env python3
"""Read-only bounded temporal candidate census for the 15-document E2E corpus."""

from __future__ import annotations

import json
from collections import Counter, defaultdict

from config import get_settings
from pymongo import MongoClient


CORPUS_ID = "2c894530-8d57-4432-a6d4-bc14505a698b"


def bounded(value: object, limit: int = 420) -> str:
    return " ".join(str(value or "").split())[:limit]


def main() -> None:
    settings = get_settings()
    mongo = MongoClient(settings.MONGODB_URI)
    try:
        database = mongo[settings.MONGODB_DATABASE]
        names = {
            str(row.get("doc_id") or ""): str(
                row.get("original_filename") or row.get("filename") or ""
            )
            for row in database["documents"].find(
                {"corpus_id": CORPUS_ID},
                {"_id": 0, "doc_id": 1, "original_filename": 1, "filename": 1},
            )
        }
        if len(names) != 15:
            raise RuntimeError(f"E2E document count drifted: {len(names)}")

        temporal_class_counts: Counter[str] = Counter()
        parents_by_file: defaultdict[str, list[dict]] = defaultdict(list)
        cursor = database["parent_chunks"].find(
            {
                "corpus_id": CORPUS_ID,
                "time_expressions.0": {"$exists": True},
            },
            {
                "_id": 0,
                "doc_id": 1,
                "parent_id": 1,
                "heading_path": 1,
                "temporal_class": 1,
                "time_expressions": 1,
                "summary": 1,
                "text": 1,
            },
        )
        for row in cursor:
            filename = names.get(str(row.get("doc_id") or ""), "<unknown>")
            temporal_class_counts[str(row.get("temporal_class") or "unknown")] += 1
            if len(parents_by_file[filename]) < 12:
                parents_by_file[filename].append(
                    {
                        "parent_id": str(row.get("parent_id") or ""),
                        "heading_path": row.get("heading_path") or [],
                        "temporal_class": str(row.get("temporal_class") or "unknown"),
                        "time_expressions": row.get("time_expressions") or [],
                        "context": bounded(row.get("summary") or row.get("text")),
                    }
                )

        capture_counts: Counter[str] = Counter()
        capture_samples: defaultdict[str, list[dict]] = defaultdict(list)
        ghost_cursor = database["ghost_b_extractions"].find(
            {
                "corpus_id": CORPUS_ID,
                "temporal_captures.0": {"$exists": True},
            },
            {"_id": 0, "doc_id": 1, "chunk_id": 1, "temporal_captures": 1},
        )
        for row in ghost_cursor:
            filename = names.get(str(row.get("doc_id") or ""), "<unknown>")
            capture_counts[filename] += 1
            if len(capture_samples[filename]) < 8:
                capture_samples[filename].append(
                    {
                        "chunk_id": str(row.get("chunk_id") or ""),
                        "temporal_captures": row.get("temporal_captures") or [],
                    }
                )

        result = {
            "schema_version": "e2e_temporal_candidate_census.v1",
            "corpus_id": CORPUS_ID,
            "parent_temporal_class_counts": dict(sorted(temporal_class_counts.items())),
            "parent_files_with_captures": len(parents_by_file),
            "parent_samples_by_filename": dict(sorted(parents_by_file.items())),
            "ghost_chunk_capture_counts_by_filename": dict(
                sorted(capture_counts.items())
            ),
            "ghost_samples_by_filename": dict(sorted(capture_samples.items())),
        }
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    finally:
        mongo.close()


if __name__ == "__main__":
    main()
