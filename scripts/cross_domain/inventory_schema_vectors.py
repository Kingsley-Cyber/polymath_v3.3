#!/usr/bin/env python3
"""Inventory schemas collection dense-vector coverage for cross-domain Phase 0."""
from __future__ import annotations

import json
import os
from pathlib import Path

CORPUS = os.environ.get("Q9_CORPUS_ID", "6a766597-29f3-4a3e-8918-5de10f0053b3")
OUT = Path(os.environ.get("OUT", "/app/_schema_vector_inventory.json"))


def main() -> None:
    from qdrant_client import QdrantClient
    from qdrant_client.http import models as qm

    from config import get_settings

    settings = get_settings()
    client = QdrantClient(
        url=os.environ.get("QDRANT_URL") or settings.QDRANT_URL,
        api_key=os.environ.get("QDRANT_API_KEY") or getattr(settings, "QDRANT_API_KEY", None),
        timeout=60,
    )
    coll = f"corpus_{CORPUS[:8]}_schemas" if False else None
    # Prefer full corpus id collection naming used in polymath
    candidates = [
        f"corpus_{CORPUS}_schemas",
        f"corpus_{CORPUS.replace('-', '')}_schemas",
    ]
    # also list matching
    existing = {c.name for c in client.get_collections().collections}
    coll_name = next((c for c in candidates if c in existing), None)
    if coll_name is None:
        matches = sorted(n for n in existing if "schemas" in n and CORPUS[:8] in n)
        coll_name = matches[0] if matches else None
    if coll_name is None:
        out = {
            "corpus_id": CORPUS,
            "error": "schemas_collection_not_found",
            "existing_schema_collections_sample": sorted(
                n for n in existing if "schema" in n.lower()
            )[:30],
        }
        OUT.write_text(json.dumps(out, indent=2))
        print(json.dumps(out, indent=2))
        return

    info = client.get_collection(coll_name)
    vectors_cfg = info.config.params.vectors
    # named or unnamed
    if isinstance(vectors_cfg, dict):
        vec_names = list(vectors_cfg.keys())
        dims = {k: getattr(vectors_cfg[k], "size", None) for k in vec_names}
    else:
        vec_names = [""]
        dims = {"": getattr(vectors_cfg, "size", None)}

    total = int(info.points_count or 0)
    # sample scroll to inspect payload shape + whether vectors present
    points, _ = client.scroll(
        collection_name=coll_name,
        limit=64,
        with_payload=True,
        with_vectors=True,
    )
    with_vec = 0
    without_vec = 0
    payload_keys = set()
    record_kinds = {}
    sample = []
    for p in points:
        payload_keys.update((p.payload or {}).keys())
        kind = (p.payload or {}).get("record_kind") or (p.payload or {}).get("kind") or (p.payload or {}).get("schema_kind")
        record_kinds[str(kind)] = record_kinds.get(str(kind), 0) + 1
        vec = p.vector
        has = False
        if vec is None:
            has = False
        elif isinstance(vec, dict):
            has = any(v is not None and len(v) > 0 for v in vec.values())
        else:
            has = len(vec) > 0
        if has:
            with_vec += 1
        else:
            without_vec += 1
        if len(sample) < 5:
            sample.append(
                {
                    "id": str(p.id),
                    "has_vector": has,
                    "payload_subset": {
                        k: (p.payload or {}).get(k)
                        for k in (
                            "canonical_term",
                            "term",
                            "name",
                            "aliases",
                            "trusted_aliases",
                            "record_kind",
                            "trust_class",
                            "trust_release",
                            "chunk_type",
                            "concept_id",
                            "linked_child_ids",
                        )
                        if k in (p.payload or {})
                    },
                }
            )

    # Approximate full coverage via counted filter if possible — else extrapolate sample
    # Try retrieving with dummy vector to see if search works
    semantic_enabled = with_vec > 0 and any(d for d in dims.values() if d)
    # Count points missing vectors by scrolling in pages (cap)
    scanned = 0
    with_all = 0
    without_all = 0
    offset = None
    while scanned < min(total, 2000):
        batch, offset = client.scroll(
            collection_name=coll_name,
            limit=256,
            offset=offset,
            with_payload=False,
            with_vectors=True,
        )
        if not batch:
            break
        for p in batch:
            scanned += 1
            vec = p.vector
            has = False
            if isinstance(vec, dict):
                has = any(v is not None and len(v) > 0 for v in vec.values())
            elif vec is not None:
                has = len(vec) > 0
            if has:
                with_all += 1
            else:
                without_all += 1
        if offset is None:
            break

    out = {
        "corpus_id": CORPUS,
        "collection": coll_name,
        "vocabulary_points_total": total,
        "vocabulary_points_scanned": scanned,
        "vocabulary_points_with_dense_vectors": with_all,
        "vocabulary_points_without_dense_vectors": without_all
        if scanned >= total
        else f"{without_all}+_unscanned",
        "vector_config": {"names": vec_names, "dims": dims},
        "embedding_model_and_dimension": {
            "expected_live_embedder": "mlx-community/Qwen3-Embedding-0.6B-mxfp8",
            "dimension": 1024,
        },
        "exact_alias_lookup_enabled": True,  # Phase 8 shadow / mongo+payload path exists
        "semantic_vocabulary_vector_lookup_enabled": bool(semantic_enabled and with_all > 0),
        "sample_payload_keys": sorted(payload_keys),
        "sample_record_kinds": record_kinds,
        "samples": sample,
        "points_count_info": total,
        "status": info.status,
    }
    OUT.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps({k: out[k] for k in out if k != "samples"}, indent=2, default=str))


if __name__ == "__main__":
    main()
