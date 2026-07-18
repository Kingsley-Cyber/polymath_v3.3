#!/usr/bin/env python3
"""Verify the immutable protected Fact residue and restorable edge topology."""

from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(
    "/data/ingest-files/runpod-job-journals/"
    "e2e-isolation-backup-20260716T0046Z"
)
PROTECTED = "fd460347-61cc-4358-87fc-4b2a80533f0a"
EXPECTED_FACTS = 2305


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    require(manifest["protected_corpus_id"] == PROTECTED, "protected ID drifted")
    doc_ids = set(str(value) for value in manifest["shared_doc_ids"])
    require(len(doc_ids) == 15, "shared document manifest drifted")

    facts: list[dict[str, Any]] = []
    with gzip.open(ROOT / "neo4j_nodes.jsonl.gz", "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            props = row.get("props") or {}
            if (
                "Fact" in (row.get("labels") or [])
                and str(props.get("corpus_id") or "") == PROTECTED
                and str(props.get("doc_id") or "") in doc_ids
            ):
                facts.append(
                    {
                        "labels": sorted(str(value) for value in row["labels"]),
                        "props": props,
                    }
                )

    fact_ids = [str(row["props"].get("fact_id") or "") for row in facts]
    require(len(facts) == EXPECTED_FACTS, f"Fact count drifted: {len(facts)}")
    require("" not in fact_ids, "Fact identity is empty")
    require(len(set(fact_ids)) == EXPECTED_FACTS, "Fact identities are not unique")
    fact_id_set = set(fact_ids)

    edges: list[dict[str, Any]] = []
    types = Counter()
    fact_edge_counts: dict[str, Counter[str]] = {
        fact_id: Counter() for fact_id in fact_id_set
    }
    support_chunk_ids: set[str] = set()
    with gzip.open(
        ROOT / "neo4j_relationships.jsonl.gz", "rt", encoding="utf-8"
    ) as handle:
        for line in handle:
            row = json.loads(line)
            edge_type = str(row.get("type") or "")
            if edge_type not in {"HAS_FACT", "SUPPORTS_FACT"}:
                continue
            end_props = row.get("end_props") or {}
            fact_id = str(end_props.get("fact_id") or "")
            if (
                fact_id not in fact_id_set
                or str(end_props.get("corpus_id") or "") != PROTECTED
            ):
                continue
            start_props = row.get("start_props") or {}
            if edge_type == "HAS_FACT":
                start_identity = {
                    "entity_id": str(start_props.get("entity_id") or "")
                }
                require(start_identity["entity_id"], "HAS_FACT Entity identity empty")
            else:
                chunk_id = str(start_props.get("chunk_id") or "")
                require(chunk_id, "SUPPORTS_FACT Chunk identity empty")
                support_chunk_ids.add(chunk_id)
                start_identity = {
                    "corpus_id": PROTECTED,
                    "chunk_id": chunk_id,
                }
            types[edge_type] += 1
            fact_edge_counts[fact_id][edge_type] += 1
            edges.append(
                {
                    "type": edge_type,
                    "start_identity": start_identity,
                    "end_identity": {
                        "corpus_id": PROTECTED,
                        "fact_id": fact_id,
                    },
                    "props": row.get("props") or {},
                }
            )

    require(
        types == {"HAS_FACT": EXPECTED_FACTS, "SUPPORTS_FACT": EXPECTED_FACTS},
        f"original Fact edge counts drifted: {dict(types)}",
    )
    bad_facts = [
        fact_id
        for fact_id, counts in fact_edge_counts.items()
        if counts != {"HAS_FACT": 1, "SUPPORTS_FACT": 1}
    ]
    require(not bad_facts, f"Fact edge closure failed for {len(bad_facts)} facts")

    fact_rows = sorted(facts, key=lambda row: str(row["props"]["fact_id"]))
    edge_rows = sorted(
        edges,
        key=lambda row: (
            row["type"],
            json.dumps(row["start_identity"], sort_keys=True),
            str(row["end_identity"]["fact_id"]),
        ),
    )
    output = {
        "schema_version": "e2e_original_fact_backup_verification.v1",
        "manifest_sha256": (ROOT / "MANIFEST.sha256").read_text().strip(),
        "protected_corpus_id": PROTECTED,
        "document_count": len(doc_ids),
        "fact_count": len(fact_rows),
        "fact_content_sha256": stable_hash(fact_rows),
        "edge_counts": dict(sorted(types.items())),
        "edge_topology_sha256": stable_hash(edge_rows),
        "support_chunk_id_count": len(support_chunk_ids),
        "all_facts_have_exactly_one_subject_and_support_edge": True,
    }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
