#!/usr/bin/env python3
"""Phase 0 snapshot for q9 final E2E — source hashes + store counts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

CID = "6a766597-29f3-4a3e-8918-5de10f0053b3"
SRC = "/ingest-source/isolated_alias_fixture"
OUT = Path(os.environ.get("Q9_PHASE0_OUT", "/tmp/q9_phase0_snapshot.json"))


async def main() -> int:
    from services.conversation import conversation_service

    await conversation_service.connect()
    db = conversation_service._db

    files: list[dict] = []
    for p in sorted(Path(SRC).iterdir()):
        if p.name.startswith(".") or p.name.startswith("._"):
            continue
        try:
            if not p.is_file():
                continue
            raw = p.read_bytes()
        except (PermissionError, OSError):
            continue
        files.append(
            {
                "name": p.name,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )

    cols = await db.list_collection_names()
    n_chunks = await db.chunks.count_documents({"corpus_id": CID})
    n_docs = await db.documents.count_documents({"corpus_id": CID})
    n_parents = (
        await db.parent_chunks.count_documents({"corpus_id": CID})
        if "parent_chunks" in cols
        else None
    )
    related: dict[str, int] = {}
    for c in cols:
        if any(
            x in c.lower()
            for x in (
                "summ",
                "lexic",
                "schema",
                "bundle",
                "assert",
                "extract",
                "entity",
                "alias",
                "graph_job",
                "vocab",
            )
        ):
            try:
                related[c] = await db[c].count_documents({"corpus_id": CID})
            except Exception:  # noqa: BLE001
                pass

    import httpx

    qurl = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    qdrant: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        cols_r = await client.get(f"{qurl}/collections")
        for c in ((cols_r.json().get("result") or {}).get("collections") or []):
            name = c.get("name") if isinstance(c, dict) else str(c)
            if "6a766597" in name:
                info = await client.get(f"{qurl}/collections/{name}")
                qdrant.append(
                    {
                        "name": name,
                        "points": (info.json().get("result") or {}).get("points_count"),
                    }
                )

    neo: dict = {"ok": False}
    try:
        from neo4j import GraphDatabase

        uri = os.environ.get("NEO4J_URI") or "bolt://neo4j:7687"
        user = os.environ.get("NEO4J_USER", "neo4j")
        pw = os.environ.get("NEO4J_PASSWORD") or "password"
        driver = GraphDatabase.driver(uri, auth=(user, pw))
        with driver.session() as s:
            rows = s.run(
                "MATCH (n) WHERE n.corpus_id=$c "
                "RETURN labels(n)[0] AS lab, count(*) AS n ORDER BY n DESC",
                c=CID,
            ).data()
            neo = {"ok": True, "by_label": rows}
        driver.close()
    except Exception as exc:  # noqa: BLE001
        neo = {"ok": False, "error": f"{type(exc).__name__}:{exc}"[:200]}

    redis_ok: bool | str = False
    try:
        import redis

        r = redis.Redis(
            host=os.environ.get("REDIS_HOST", "redis"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            socket_timeout=2,
        )
        redis_ok = bool(r.ping())
    except Exception as exc:  # noqa: BLE001
        redis_ok = str(exc)[:80]

    corp = await db.corpora.find_one({"corpus_id": CID})
    snap = {
        "schema_version": "q9_phase0_snapshot.v1",
        "corpus_id": CID,
        "corpus_name": (corp or {}).get("name"),
        "source_dir": SRC,
        "source_files": files,
        "source_file_count": len(files),
        "mongo": {
            "documents": n_docs,
            "chunks": n_chunks,
            "parent_chunks": n_parents,
            "related_counts": related,
        },
        "graph_capabilities": (corp or {}).get("graph_capabilities"),
        "qdrant": qdrant,
        "neo4j": neo,
        "redis_ok": redis_ok,
    }
    OUT.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(OUT),
                "source_file_count": len(files),
                "docs": n_docs,
                "chunks": n_chunks,
                "parents": n_parents,
                "qdrant": qdrant,
                "neo_ok": neo.get("ok"),
                "neo_top": (neo.get("by_label") or [])[:10],
                "redis_ok": redis_ok,
                "caps": (corp or {}).get("graph_capabilities"),
                "related_nonzero": {k: v for k, v in related.items() if v},
            },
            indent=2,
            default=str,
        )
    )
    return 0 if len(files) == 10 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
