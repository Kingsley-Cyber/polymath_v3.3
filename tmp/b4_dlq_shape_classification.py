#!/usr/bin/env python3
"""Read-only B4 DLQ raw-output shape classifier; never emits raw text."""

from __future__ import annotations

import asyncio
from collections import Counter
import json
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.settings import settings_service


PHASE = "b4_atomic"
JOB_COLLECTION = "semantic_digest_jobs"
DEAD_LETTER_COLLECTION = "semantic_digest_dead_letters"


async def _database() -> tuple[AsyncIOMotorClient, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI, tz_aware=True)
    try:
        db = client.get_default_database()
    except Exception:
        db = client[settings.MONGODB_DATABASE]
    settings_service.attach(db)
    return client, db


def _shape_class(value: Any) -> str:
    if not isinstance(value, str):
        return "non_string_tool_arguments"
    if value == "":
        return "empty_tool_arguments"
    stripped = value.strip()
    if stripped == "":
        return "whitespace_only_tool_arguments"
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        lowered = stripped.casefold()
        refusal_markers = ("cannot", "can't", "unable", "sorry", "refuse")
        if any(marker in lowered for marker in refusal_markers):
            return "refusal_form_non_json"
        if stripped.startswith("```"):
            return "fenced_or_prefixed_non_json"
        if stripped[0] in "[{":
            return "malformed_or_truncated_json_prefix"
        return "non_json_text_prefix"
    if parsed == {}:
        return "empty_json_object_tool_arguments"
    if parsed == []:
        return "empty_json_array_tool_arguments"
    return "valid_json_wrong_schema"


async def main() -> int:
    client, db = await _database()
    try:
        jobs = await db[JOB_COLLECTION].find(
            {"phase": PHASE, "status": "dead_letter"},
            {"_id": 0, "ordinal": 1, "dead_letter_id": 1},
        ).sort("ordinal", 1).to_list(length=None)
        rows: list[dict[str, Any]] = []
        classes: Counter[str] = Counter()
        for job in jobs:
            dead_letter = await db[DEAD_LETTER_COLLECTION].find_one(
                {"_id": job.get("dead_letter_id")},
                {"_id": 0, "raw_outputs": 1, "raw_output_hashes": 1},
            )
            if not dead_letter:
                rows.append(
                    {
                        "ordinal": int(job.get("ordinal") or 0),
                        "artifact_present": False,
                    }
                )
                continue
            outputs = list(dead_letter.get("raw_outputs") or [])
            output_classes = [_shape_class(value) for value in outputs]
            classes.update(output_classes)
            rows.append(
                {
                    "ordinal": int(job.get("ordinal") or 0),
                    "artifact_present": True,
                    "attempt_count": len(outputs),
                    "shape_classes": output_classes,
                    "character_lengths": [
                        len(value) if isinstance(value, str) else None
                        for value in outputs
                    ],
                    "utf8_byte_lengths": [
                        len(value.encode("utf-8")) if isinstance(value, str) else None
                        for value in outputs
                    ],
                    "hash_count": len(dead_letter.get("raw_output_hashes") or []),
                }
            )
        receipt = {
            "schema_version": "polymath.t9_3_b4_dlq_shape_classification.v1",
            "read_only": True,
            "raw_text_emitted": False,
            "provider_calls": 0,
            "job_count": len(jobs),
            "attempt_count": sum(classes.values()),
            "shape_class_counts": dict(sorted(classes.items())),
            "rows": rows,
        }
        print(json.dumps(receipt, sort_keys=True))
        return 0 if len(jobs) == 5 and sum(classes.values()) == 10 else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
