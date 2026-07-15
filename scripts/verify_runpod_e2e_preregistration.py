#!/usr/bin/env python3
"""Verify frozen 15-doc selection and retrieval targets before live work."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SELECTION = ROOT / "backend/evals/runpod_e2e_15doc_selection_v1.json"
DEFAULT_EVAL = ROOT / "backend/evals/runpod_e2e_retrieval_preregister_v1.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(selection_path: Path, eval_path: Path) -> dict[str, Any]:
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    spec = json.loads(eval_path.read_text(encoding="utf-8"))
    if spec["selection_manifest"]["sha256"] != sha256(selection_path):
        raise ValueError("retrieval preregistration selection hash drifted")
    selected = {row["filename"]: row for row in selection["selected"]}
    if len(selected) != 15 or selection["source_count"] != 75:
        raise ValueError("selection cardinality drifted")
    if (
        sorted(Counter(row["topic_band"] for row in selected.values()).values())
        != [3] * 5
    ):
        raise ValueError("selection topic-band coverage drifted")
    if (
        sorted(Counter(row["size_band"] for row in selected.values()).values())
        != [5] * 3
    ):
        raise ValueError("selection size-band coverage drifted")
    source_root = Path(selection["source_root"])
    source_hash_mismatches = []
    anchor_misses = []
    query_ids = set()
    shapes = Counter()
    for query in spec["queries"]:
        query_id = query["id"]
        if query_id in query_ids:
            raise ValueError("retrieval query IDs must be unique")
        query_ids.add(query_id)
        shapes[query["shape"]] += 1
        expected = set(query["expected_any"])
        if not expected <= set(selected):
            raise ValueError(
                f"query {query_id} targets a file outside the frozen selection"
            )
        if query["shape"] == "negative_control":
            if expected or query.get("must_refuse") is not True:
                raise ValueError(
                    "negative controls must have no target and must refuse"
                )
            continue
        if not 1 <= int(query["expected_min_distinct"]) <= len(expected):
            raise ValueError(f"query {query_id} has an invalid distinct-target gate")
        for filename, anchors in query["evidence_anchors"].items():
            path = source_root / filename
            if sha256(path) != selected[filename]["sha256"]:
                source_hash_mismatches.append(filename)
                continue
            text = path.read_text(encoding="utf-8", errors="replace").casefold()
            for anchor in anchors:
                if str(anchor).casefold() not in text:
                    anchor_misses.append(
                        {"query_id": query_id, "filename": filename, "anchor": anchor}
                    )
    required_shapes = {
        "direct_expert",
        "direct_fact",
        "lay_language",
        "relationship_multi_document",
        "negative_control",
    }
    return {
        "schema_version": "polymath.runpod_e2e_preregistration_verify.v1",
        "selection_sha256": sha256(selection_path),
        "eval_sha256": sha256(eval_path),
        "source_count": selection["source_count"],
        "selection_count": len(selected),
        "query_count": len(query_ids),
        "tier_count": len(spec["tiers"]),
        "execution_count": len(query_ids) * len(spec["tiers"]),
        "shape_counts": dict(sorted(shapes.items())),
        "source_hash_mismatches": sorted(set(source_hash_mismatches)),
        "anchor_misses": anchor_misses,
        "all_green": (
            not source_hash_mismatches
            and not anchor_misses
            and required_shapes <= set(shapes)
            and spec["tiers"]
            == [
                "qdrant_only",
                "qdrant_mongo",
                "qdrant_mongo_graph",
            ]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    parser.add_argument("--eval", type=Path, default=DEFAULT_EVAL)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    report = verify(args.selection, args.eval)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["all_green"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
