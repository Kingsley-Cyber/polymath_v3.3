"""Verify staged E2E sources through the production local-file discovery path."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from services.ingestion.batches import discover_local_files


ROOT = "/ingest-source/runpod_e2e_15doc_20260715"
MANIFEST = Path("/tmp/runpod_e2e_15doc_selection_v1.json")


def main() -> None:
    selection = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = {row["filename"]: row["sha256"] for row in selection["selected"]}
    root, files = discover_local_files(
        ROOT,
        recursive=False,
        extensions=[".md"],
    )
    actual = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files
    }
    if actual != expected:
        raise AssertionError(
            {
                "missing": sorted(set(expected) - set(actual)),
                "extra": sorted(set(actual) - set(expected)),
                "hash_mismatches": sorted(
                    name
                    for name in set(expected) & set(actual)
                    if expected[name] != actual[name]
                ),
            }
        )
    appledouble_count = sum(1 for path in root.iterdir() if path.name.startswith("._"))
    print(
        json.dumps(
            {
                "appledouble_excluded": appledouble_count,
                "discovered_count": len(actual),
                "hash_mismatches": [],
                "root": str(root),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
