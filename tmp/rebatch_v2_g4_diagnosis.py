#!/usr/bin/env python3
"""Read-only diagnosis for the two required Phase A temporal phrases."""

from __future__ import annotations

import json

from config import get_settings
from pymongo import MongoClient


CORPUS_ID = "62193743-4175-40da-b861-ba1e1e567b9a"
PHRASES = ("winter 1911", "2018 drought summer")


def bounded_snippet(text: str, phrase: str, radius: int = 180) -> str:
    normalized = text.casefold()
    start = normalized.find(phrase.casefold())
    if start < 0:
        return ""
    left = max(0, start - radius)
    right = min(len(text), start + len(phrase) + radius)
    return text[left:right]


def main() -> int:
    settings = get_settings()
    client = MongoClient(settings.MONGODB_URI)
    try:
        db = client[settings.MONGODB_DATABASE]
        documents = {
            row.get("doc_id"): row
            for row in db.documents.find(
                {"corpus_id": CORPUS_ID},
                {"_id": 0, "doc_id": 1, "filename": 1, "original_filename": 1},
            )
        }
        chunks = list(
            db.chunks.find(
                {"corpus_id": CORPUS_ID},
                {
                    "_id": 0,
                    "chunk_id": 1,
                    "doc_id": 1,
                    "chunk_kind": 1,
                    "text": 1,
                    "heading_path": 1,
                },
            )
        )
        extraction_by_chunk = {
            row.get("chunk_id"): row
            for row in db.ghost_b_extractions.find(
                {"corpus_id": CORPUS_ID},
                {
                    "_id": 0,
                    "chunk_id": 1,
                    "status": 1,
                    "provider": 1,
                    "model": 1,
                    "temporal_capture_version": 1,
                    "temporal_captures": 1,
                },
            )
        }

        findings = []
        for phrase in PHRASES:
            matches = []
            for chunk in chunks:
                text = str(chunk.get("text") or "")
                if phrase.casefold() not in text.casefold():
                    continue
                document = documents.get(chunk.get("doc_id"), {})
                extraction = extraction_by_chunk.get(chunk.get("chunk_id"), {})
                captures = extraction.get("temporal_captures") or []
                matches.append(
                    {
                        "chunk_id": chunk.get("chunk_id"),
                        "doc_id": chunk.get("doc_id"),
                        "filename": document.get("filename")
                        or document.get("original_filename"),
                        "chunk_kind": chunk.get("chunk_kind"),
                        "heading_path": chunk.get("heading_path"),
                        "snippet": bounded_snippet(text, phrase),
                        "extraction": {
                            "status": extraction.get("status"),
                            "provider": extraction.get("provider"),
                            "model": extraction.get("model"),
                            "temporal_capture_version": extraction.get(
                                "temporal_capture_version"
                            ),
                            "temporal_captures": captures,
                        },
                        "capture_texts_are_exact_substrings": [
                            {
                                "text": capture.get("text"),
                                "exact_substring": str(capture.get("text") or "") in text,
                                "offset_slice": text[
                                    int(capture.get("char_start") or 0) : int(
                                        capture.get("char_end") or 0
                                    )
                                ],
                            }
                            for capture in captures
                            if isinstance(capture, dict)
                        ],
                    }
                )
            findings.append({"required_phrase": phrase, "matches": matches})

        print(json.dumps({"corpus_id": CORPUS_ID, "findings": findings}, indent=2))
        assert all(item["matches"] for item in findings), "required phrase absent from chunks"
        assert all(
            len(item["matches"]) == 1 for item in findings
        ), "required phrase unexpectedly spans multiple chunks"
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
