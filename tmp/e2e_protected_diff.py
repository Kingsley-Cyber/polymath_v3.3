#!/usr/bin/env python3
"""Read-only exact diff of the frozen pre-E2E protected-corpus census."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from e2e_protected_census import BASELINE, _capture


OUTPUT = Path("/tmp/e2e_protected_diff.json")
FIELDS = (
    "mongo_counts_by_collection_and_corpus",
    "qdrant_counts_by_corpus",
    "neo4j_counts_by_corpus",
)


def _flatten(value: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    if not isinstance(value, dict):
        return {prefix: value}
    rows: dict[tuple[str, ...], Any] = {}
    for key, nested in value.items():
        rows.update(_flatten(nested, (*prefix, str(key))))
    return rows


def _atomic_write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


async def main() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = await _capture(frozen_ids=baseline["frozen_existing_corpus_ids"])
    changes = []
    for field in FIELDS:
        before = _flatten(baseline.get(field) or {})
        after = _flatten(current.get(field) or {})
        for path in sorted(set(before) | set(after)):
            old = before.get(path, 0)
            new = after.get(path, 0)
            if old == new:
                continue
            numeric_delta = new - old if isinstance(old, int) and isinstance(new, int) else None
            changes.append(
                {
                    "surface": field,
                    "path": list(path),
                    "before": old,
                    "after": new,
                    "delta": numeric_delta,
                }
            )
    result = {
        "schema_version": "runpod_e2e_protected_diff.v1",
        "canonical_scope": current["canonical_store_census"][
            "census_scope_version"
        ],
        "protected_corpus_ids": baseline["frozen_existing_corpus_ids"],
        "change_count": len(changes),
        "changes": changes,
        "unchanged": not changes,
    }
    _atomic_write(OUTPUT, result)
    print(json.dumps(result, indent=2, sort_keys=True))


asyncio.run(main())
