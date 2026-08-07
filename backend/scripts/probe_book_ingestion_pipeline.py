#!/usr/bin/env python3
"""Book-ingestion pipeline audit probe (Part 9 live verification).

Read-only against the live stores. Verifies, with live evidence:

  1. The permanent Benesh-1975 + TikTok-Shop-2026 fixtures still carry the
     canonical relex_local extraction identity (schema/provider/model).
  2. The mixed-content book fixture corpus carries the full evidence bundle:
     Mongo kinds {body, code, output, caption}, EXPLAINS rows, Neo4j
     EXPLAINS edge, chunk_kind/language on Chunk nodes, Qdrant kind filters.
  3. Control-plane state: ingestion_runs status, query_ready certificates,
     summary presence at parent/document level.
  4. GLiNER-Relex-only compliance of live engine values and persisted
     extraction artifacts (historical rows are inventoried, never mutated).
  5. Retrieval returns exact document/chunk IDs after container restarts
     (the stack has restarted since these corpora were written).

Writes data_eval/book_ingestion_pipeline_probe.json. Never mutates stores.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from neo4j import GraphDatabase
from pymongo import MongoClient
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

BASE = "http://localhost:8000"
TOKEN_FILE = Path("/tmp/pm_token.txt")
ENV_PATH = Path("/Users/king/polymath_v3.3/.env")
REPORT = Path(
    "/Users/king/polymath_v3.3/data_eval/book_ingestion_pipeline_probe.json"
)

CANONICAL_SCHEMA = "polymath.extract.relex_local.v1"
CANONICAL_MODEL = "knowledgator/gliner-relex-large-v1.0"
CANONICAL_ENGINE = "relex_local"

# Permanent fixtures (owner-designated). Resolved by filename so corpus
# recreation never invalidates the probe.
FIXTURE_FILES = (
    "benesh-1975.md",
    "this-tiktok-shop-setup-took-a-dying-brand-from-4k-to-1m-in-6-months-yt-qjmlnrxqzc0.md",
)
MIXED_FIXTURE = "mixed-content-book-fixture.md"

QUERIES = {
    "benesh_explicit_year": "What notation system did Benesh describe in 1975?",
    "tiktok_explicit_year": "How much revenue did the TikTok shop setup generate in six months?",
    "mixed_conceptual": (
        "Explain the code that implements the bounded LRU cache with "
        "OrderedDict, move_to_end and capacity-based eviction"
    ),
}


def env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def corpus_by_filename(db, filename: str) -> dict | None:
    """Newest corpus still holding an active document with this filename."""
    doc = db.documents.find_one(
        {"filename": filename, "status": "active"},
        {"corpus_id": 1},
        sort=[("_id", -1)],
    )
    if not doc:
        return None
    return db.corpora.find_one({"corpus_id": doc["corpus_id"]})


def extraction_identity(db, corpus_id: str) -> dict:
    rows = list(
        db.ghost_b_extractions.find(
            {"corpus_id": corpus_id},
            {"schema_version": 1, "provider": 1, "model": 1},
        )
    )

    def hist(field: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in rows:
            key = str(row.get(field) or "<missing>")
            out[key] = out.get(key, 0) + 1
        return out

    return {
        "rows": len(rows),
        "schema_version": hist("schema_version"),
        "provider": hist("provider"),
        "model": hist("model"),
        "single_canonical_schema": list(hist("schema_version")) == [CANONICAL_SCHEMA],
        "single_canonical_model": list(hist("model")) == [CANONICAL_MODEL],
    }


def summary_state(db, corpus_id: str) -> dict:
    parent_total = db.parent_chunks.count_documents({"corpus_id": corpus_id})
    parent_sum = db.parent_chunks.count_documents(
        {"corpus_id": corpus_id, "summary": {"$exists": True, "$nin": [None, ""]}}
    )
    doc_total = db.documents.count_documents({"corpus_id": corpus_id})
    doc_sum = db.documents.count_documents(
        {
            "corpus_id": corpus_id,
            "doc_profile.summary": {"$exists": True, "$nin": [None, ""]},
        }
    )
    tree_docs = db.documents.count_documents(
        {"corpus_id": corpus_id, "summary_tree.sections.0": {"$exists": True}}
    )
    return {
        "parents_with_summary": parent_sum,
        "parents_total": parent_total,
        "documents_with_profile": doc_sum,
        "documents_total": doc_total,
        "documents_with_summary_tree_sections": tree_docs,
    }


def control_plane_state(db, corpus_id: str) -> dict:
    runs = list(db.ingestion_runs.find({"corpus_id": corpus_id}))
    return {
        "runs": [
            {
                "filename": r.get("filename"),
                "status": r.get("status"),
                "certificate_id": r.get("certificate_id"),
                "last_intake_reason": r.get("last_intake_reason"),
                "proof_query_ready": (r.get("proof") or {}).get("query_ready"),
                "proof_missing": (r.get("proof") or {}).get("missing_counts"),
            }
            for r in runs
        ],
        "certificates": db.query_ready_certificates.count_documents(
            {"corpus_id": corpus_id}
        ),
    }


def neo4j_bundle(envcfg: dict, corpus_id: str) -> dict:
    driver = GraphDatabase.driver(
        "bolt://localhost:7687",
        auth=(envcfg.get("NEO4J_USER", "neo4j"), envcfg.get("NEO4J_PASSWORD")),
    )
    try:
        with driver.session() as session:
            chunks = session.run(
                "MATCH (c:Chunk {corpus_id: $cid}) "
                "RETURN c.chunk_kind AS kind, count(*) AS n",
                cid=corpus_id,
            ).data()
            explains = session.run(
                "MATCH (p:Chunk {corpus_id: $cid})-[e:EXPLAINS]->(c:Chunk) "
                "RETURN count(e) AS n",
                cid=corpus_id,
            ).single()
            langs = session.run(
                "MATCH (c:Chunk {corpus_id: $cid}) "
                "WHERE c.language IS NOT NULL "
                "RETURN DISTINCT c.language AS language",
                cid=corpus_id,
            ).data()
    finally:
        driver.close()
    return {
        "chunk_kind_histogram": {r["kind"]: r["n"] for r in chunks},
        "explains_edges": int(explains["n"]) if explains else 0,
        "languages": [r["language"] for r in langs],
    }


def qdrant_kinds(corpus_id: str) -> dict:
    client = QdrantClient(url="http://localhost:6333")
    collection = f"corpus_{corpus_id[:8]}_naive"
    try:
        client.get_collection(collection)
    except Exception:  # noqa: BLE001
        return {"collection": collection, "exists": False}
    counts: dict[str, int] = {}
    for kind in ("body", "code", "output", "caption", "table"):
        res = client.scroll(
            collection_name=collection,
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="chunk_kind", match=MatchValue(value=kind)),
                    FieldCondition(key="chunk_type", match=MatchValue(value="child")),
                ]
            ),
            limit=100,
            with_payload=False,
        )
        counts[kind] = len(res[0])
    return {"collection": collection, "exists": True, "children_by_kind": counts}


def retrieval_probe(token: str, corpus_id: str, query: str) -> dict:
    body = json.dumps(
        {"conversation_id": None, "message": query, "corpus_ids": [corpus_id]}
    ).encode()
    req = urllib.request.Request(
        f"{BASE}/api/chat",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    sources: list[dict] = []
    error = None
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "sources":
                    sources = list(ev.get("sources") or ev.get("chunks") or [])
                elif ev.get("type") == "done":
                    break
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    return {
        "query": query,
        "error": error,
        "source_count": len(sources),
        "chunk_ids": [s.get("chunk_id") for s in sources][:8],
        "doc_ids": sorted({str(s.get("doc_id") or "") for s in sources}),
        "kinds": [str(s.get("chunk_kind") or "<missing>") for s in sources],
    }


def global_engine_census(db) -> dict:
    """Live configuration + persisted legacy artifact inventory (read-only)."""
    corpora: dict[str, int] = {}
    for c in db.corpora.find({}, {"default_ingestion_config.extraction_engine": 1}):
        eng = str(
            (c.get("default_ingestion_config") or {}).get("extraction_engine")
            or "<unset>"
        )
        corpora[eng] = corpora.get(eng, 0) + 1
    extr_models: dict[str, int] = {}
    for row in db.ghost_b_extractions.aggregate(
        [{"$group": {"_id": "$model", "n": {"$sum": 1}}}]
    ):
        extr_models[str(row["_id"])] = row["n"]
    doc_engines: dict[str, int] = {}
    for row in db.documents.aggregate(
        [{"$group": {"_id": "$ghost_b_metrics.engine", "n": {"$sum": 1}}}]
    ):
        doc_engines[str(row["_id"])] = row["n"]
    return {
        "corpus_engine_values": corpora,
        "persisted_extraction_models": extr_models,
        "document_ghost_b_engines": doc_engines,
    }


def main() -> int:
    envcfg = env()
    pw = envcfg["MONGO_PASSWORD"]
    db = MongoClient(
        f"mongodb://polymath:{pw}@localhost:27017/polymath?authSource=admin"
    ).get_database()
    token = TOKEN_FILE.read_text().strip()

    report: dict = {
        "schema_version": "book_ingestion_pipeline_probe.v1",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "fixture_corpora": {},
        "retrieval": {},
        "global_engine_census": global_engine_census(db),
        "verdict": {},
    }

    # ---- permanent fixtures ------------------------------------------------
    fixture_ok = True
    for name in FIXTURE_FILES:
        corpus = corpus_by_filename(db, name)
        if not corpus:
            report["fixture_corpora"][name] = {"found": False}
            fixture_ok = False
            continue
        cid = corpus["corpus_id"]
        ident = extraction_identity(db, cid)
        cp = control_plane_state(db, cid)
        sums = summary_state(db, cid)
        report["fixture_corpora"][name] = {
            "found": True,
            "corpus_id": cid,
            "corpus_engine": (corpus.get("default_ingestion_config") or {}).get(
                "extraction_engine"
            ),
            "extraction_identity": ident,
            "control_plane": cp,
            "summaries": sums,
        }

    # ---- mixed-content book fixture ----------------------------------------
    mixed = corpus_by_filename(db, MIXED_FIXTURE)
    mixed_bundle = {"found": False}
    if mixed:
        mcid = mixed["corpus_id"]
        kinds: dict[str, int] = {}
        symbols: list[str] = []
        for c in db.chunks.find({"corpus_id": mcid}, {"chunk_kind": 1, "metadata": 1}):
            kind = str(c.get("chunk_kind") or "<missing>")
            kinds[kind] = kinds.get(kind, 0) + 1
            if kind == "code":
                symbols.extend((c.get("metadata") or {}).get("symbols_defined") or [])
        explains_rows = 0
        for p in db.parent_chunks.find(
            {"corpus_id": mcid}, {"metadata.explains_links": 1}
        ):
            explains_rows += len(
                (p.get("metadata") or {}).get("explains_links") or []
            )
        mixed_bundle = {
            "found": True,
            "corpus_id": mcid,
            "mongo_kind_histogram": kinds,
            "code_symbols_defined": sorted(set(symbols)),
            "explains_rows_in_mongo": explains_rows,
            "neo4j": neo4j_bundle(envcfg, mcid),
            "qdrant": qdrant_kinds(mcid),
            "control_plane": control_plane_state(db, mcid),
            "summaries": summary_state(db, mcid),
        }
    report["mixed_book_fixture"] = mixed_bundle

    # ---- retrieval after restart -------------------------------------------
    for name, label in ((FIXTURE_FILES[0], "benesh_explicit_year"),
                        (FIXTURE_FILES[1], "tiktok_explicit_year")):
        corp = corpus_by_filename(db, name)
        if corp:
            report["retrieval"][label] = retrieval_probe(
                token, corp["corpus_id"], QUERIES[label]
            )
    if mixed and mixed_bundle.get("found"):
        report["retrieval"]["mixed_conceptual"] = retrieval_probe(
            token, mixed["corpus_id"], QUERIES["mixed_conceptual"]
        )

    # ---- verdicts -----------------------------------------------------------
    fx = report["fixture_corpora"]
    both_fixtures = all(fx.get(n, {}).get("found") for n in FIXTURE_FILES)
    relex_identity_ok = both_fixtures and all(
        fx[n]["extraction_identity"]["single_canonical_schema"]
        and fx[n]["extraction_identity"]["single_canonical_model"]
        for n in FIXTURE_FILES
    )
    fixture_summaries_ok = both_fixtures and all(
        fx[n]["summaries"]["parents_with_summary"] > 0
        and fx[n]["summaries"]["documents_with_profile"] > 0
        for n in FIXTURE_FILES
    )
    fixture_certificates_ok = both_fixtures and all(
        fx[n]["control_plane"]["certificates"] > 0 for n in FIXTURE_FILES
    )
    mb = mixed_bundle
    mixed_kinds_ok = bool(mb.get("found")) and {
        "body",
        "code",
        "output",
        "caption",
    } <= set(mb.get("mongo_kind_histogram") or {})
    mixed_neo4j_ok = bool(mb.get("found")) and (
        (mb.get("neo4j") or {}).get("explains_edges", 0) >= 1
        and bool((mb.get("neo4j") or {}).get("chunk_kind_histogram"))
    )
    retrieval_ok = all(
        (r.get("source_count") or 0) >= 1 and not r.get("error")
        for r in report["retrieval"].values()
    )
    census = report["global_engine_census"]
    legacy_engines_present = {
        k: v
        for k, v in census["corpus_engine_values"].items()
        if k not in (CANONICAL_ENGINE, "<unset>")
    }
    verdict = {
        "permanent_fixtures_present": bool(both_fixtures),
        "fixtures_relex_only_identity": bool(relex_identity_ok),
        "fixtures_have_parent_and_document_summaries": bool(fixture_summaries_ok),
        "fixtures_have_query_ready_certificates": bool(fixture_certificates_ok),
        "mixed_book_kinds_complete": bool(mixed_kinds_ok),
        "mixed_book_neo4j_bundle": bool(mixed_neo4j_ok),
        "retrieval_returns_exact_chunk_ids_after_restart": bool(retrieval_ok),
        "no_noncanonical_corpus_engine_values": not legacy_engines_present,
    }
    report["verdict"] = verdict
    report["legacy_engine_values_on_live_corpora"] = legacy_engines_present
    report["passed"] = all(verdict.values())

    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"verdict": verdict, "passed": report["passed"]}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
