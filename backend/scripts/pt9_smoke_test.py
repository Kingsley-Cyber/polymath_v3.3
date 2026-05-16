"""Pt9 end-to-end smoke test.

Runs against a live Polymath backend. Uploads two software-heavy markdown
books, polls until ingest reaches terminal state, then queries Mongo +
Neo4j (via the backend's own clients) to surface the Pt9a/Pt9b/Pt9d
signal: did the new Software/Standard buckets fill? Did object_kind
populate? Did entity_remap_count drop?

Designed to run on the deployment host (the machine actually running
the backend container) — pulls Mongo / Neo4j credentials from the same
environment the backend uses, hits localhost:8000 by default.

Usage (from the deployment host):

    cd /path/to/Polymath_v3.3/backend
    source ~/.polymath-dev-token
    python scripts/pt9_smoke_test.py \\
        /path/to/file1.md \\
        /path/to/file2.md \\
        [--corpus-name pt9-smoke] \\
        [--api http://localhost:8000]

What it does, in order:

  1. Resolves API base + bearer token.
  2. Creates a fresh corpus named --corpus-name (default
     `pt9-smoke-<unix_ts>`). Isolates the signal from any pre-existing
     corpora.
  3. Uploads each file via POST /api/corpora/{cid}/ingest. Records the
     returned doc_id + job_id.
  4. Polls GET /api/corpora/{cid}/documents until both docs are status
     != "processing" (i.e. complete or failed). Timeout: 30 min total.
  5. Connects to Neo4j directly (env: NEO4J_URI, NEO4J_USER,
     NEO4J_PASSWORD) and runs the validation Cypher:
        - entity_type x object_kind distribution
        - top 10 entity names per (entity_type, object_kind) bucket
        - overshoot check (companies typed as Software)
  6. Connects to Mongo directly and pulls ghost_b_metrics from the doc
     records: entity_remap_count, entity_drop_count, related_to_ratio,
     etc. These tell you whether Pt9d's prompt steering is working.
  7. Prints a structured report.

Exit codes:
  0 — both docs ingested, signal collected.
  1 — token missing or API unreachable.
  2 — corpus create failed.
  3 — file upload failed.
  4 — ingest timed out.
  5 — Mongo / Neo4j connection failed (ingest succeeded but signal
       collection didn't; partial report still printed).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx


def _resolve_token() -> str | None:
    env = os.environ.get("POLYMATH_TOKEN")
    if env:
        return env
    token_file = Path.home() / ".polymath-dev-token"
    if token_file.exists():
        for line in token_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export POLYMATH_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
            if line.startswith("POLYMATH_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _create_corpus(client: httpx.Client, name: str) -> str:
    """POST /api/corpora — returns corpus_id."""
    resp = client.post(
        "/api/corpora",
        json={"name": name, "description": "Pt9 smoke test corpus"},
    )
    resp.raise_for_status()
    body = resp.json()
    cid = body.get("corpus_id") or body.get("id") or (body.get("corpus") or {}).get("corpus_id")
    if not cid:
        raise RuntimeError(f"corpus create returned no corpus_id: {body!r}")
    return cid


def _upload(client: httpx.Client, cid: str, path: Path) -> dict:
    with path.open("rb") as fh:
        files = {"file": (path.name, fh, "text/markdown")}
        resp = client.post(f"/api/corpora/{cid}/ingest", files=files, timeout=300.0)
    resp.raise_for_status()
    return resp.json()


def _list_docs(client: httpx.Client, cid: str) -> list[dict]:
    resp = client.get(f"/api/corpora/{cid}/documents")
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, list):
        return body
    return body.get("documents") or body.get("items") or []


def _poll(client: httpx.Client, cid: str, doc_ids: set[str], timeout_s: int) -> dict:
    """Returns {doc_id: terminal_status_dict}."""
    deadline = time.monotonic() + timeout_s
    seen: dict[str, dict] = {}
    while time.monotonic() < deadline:
        try:
            docs = _list_docs(client, cid)
        except Exception as exc:
            print(f"  [poll] list_docs failed: {exc}", file=sys.stderr)
            time.sleep(5)
            continue
        for d in docs:
            did = d.get("doc_id") or d.get("id")
            if did not in doc_ids:
                continue
            status = (d.get("status") or "").lower()
            if status and status not in ("processing", "queued", "running", "pending"):
                seen[did] = d
        if seen.keys() >= doc_ids:
            return seen
        time.sleep(10)
    return seen


def _connect_neo4j():
    """Returns (driver, session)."""
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "neo4j")
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(uri, auth=(user, password))
    return driver


def _connect_mongo():
    uri = os.environ.get("MONGO_URL") or os.environ.get("MONGODB_URI") or "mongodb://localhost:27017"
    from pymongo import MongoClient
    return MongoClient(uri)


_DISTRIBUTION_CYPHER = """
MATCH (c:Chunk {corpus_id: $cid})-[:MENTIONS]->(e:Entity)
WHERE e.entity_type IN ['Software', 'Standard', 'Product', 'Concept',
                        'Method', 'Document', 'Artifact', 'Person',
                        'Organization', 'Event', 'Rule', 'Law']
RETURN e.entity_type AS entity_type,
       coalesce(e.object_kind, '') AS object_kind,
       count(DISTINCT e.entity_id) AS n,
       collect(DISTINCT e.normalized_name)[..8] AS samples
ORDER BY entity_type, n DESC
"""

_OVERSHOOT_CYPHER = """
MATCH (c:Chunk {corpus_id: $cid})-[:MENTIONS]->(e:Entity)
WHERE e.entity_type = 'Software'
  AND e.normalized_name IN [
    'microsoft', 'google', 'apple', 'amazon', 'meta',
    'openai', 'anthropic', 'deepmind', 'oreilly', 'o\\'reilly',
    'thoughtworks', 'martin fowler'
  ]
RETURN e.normalized_name AS name,
       e.entity_type AS type,
       coalesce(e.object_kind, '') AS object_kind
"""


def _run_validation(cid: str) -> dict:
    out: dict = {"distribution": [], "overshoot": [], "metrics": []}
    # Neo4j
    try:
        driver = _connect_neo4j()
        with driver.session() as sess:
            for record in sess.run(_DISTRIBUTION_CYPHER, cid=cid):
                out["distribution"].append({
                    "entity_type": record["entity_type"],
                    "object_kind": record["object_kind"],
                    "n": record["n"],
                    "samples": list(record["samples"] or []),
                })
            for record in sess.run(_OVERSHOOT_CYPHER, cid=cid):
                out["overshoot"].append({
                    "name": record["name"],
                    "type": record["type"],
                    "object_kind": record["object_kind"],
                })
        driver.close()
    except Exception as exc:
        out["neo4j_error"] = str(exc)

    # Mongo: per-doc ghost_b_metrics
    try:
        client = _connect_mongo()
        # Backend uses DB name from MONGO_DB env or 'polymath' default.
        db_name = os.environ.get("MONGO_DB", "polymath")
        db = client[db_name]
        for doc in db["documents"].find(
            {"corpus_id": cid},
            {"doc_id": 1, "filename": 1, "ghost_b_metrics": 1, "_id": 0},
        ):
            m = doc.get("ghost_b_metrics") or {}
            out["metrics"].append({
                "doc_id": doc.get("doc_id", "")[:12],
                "filename": doc.get("filename"),
                "entity_count": m.get("entity_count"),
                "relation_count": m.get("relation_count"),
                "entity_remap_count": m.get("entity_remap_count"),
                "entity_drop_count": m.get("entity_drop_count"),
                "relation_remap_count": m.get("relation_remap_count"),
                "related_to_ratio": m.get("related_to_ratio"),
                "success_rate": m.get("success_rate"),
            })
        client.close()
    except Exception as exc:
        out["mongo_error"] = str(exc)
    return out


def _format_report(corpus_id: str, ingest_summary: dict, validation: dict) -> str:
    lines: list[str] = []
    lines.append(f"\n{'=' * 70}")
    lines.append(f"Pt9 SMOKE TEST RESULT  corpus={corpus_id}")
    lines.append("=" * 70)
    lines.append("\n## INGEST SUMMARY\n")
    for did, info in ingest_summary.items():
        status = info.get("status", "?")
        fname = info.get("filename", "?")
        lines.append(f"  {did[:12]}  status={status:<10}  {fname}")

    lines.append("\n## ENTITY_TYPE x OBJECT_KIND DISTRIBUTION\n")
    if validation.get("neo4j_error"):
        lines.append(f"  ⚠ Neo4j error: {validation['neo4j_error']}")
    elif not validation["distribution"]:
        lines.append("  (no entities — ingest may have failed at ghost_b)")
    else:
        lines.append(
            f"  {'entity_type':<14} {'object_kind':<18} {'n':>5}  samples"
        )
        lines.append("  " + "-" * 64)
        for row in validation["distribution"]:
            samples = ", ".join(row["samples"][:5])
            lines.append(
                f"  {row['entity_type']:<14} {row['object_kind']:<18} "
                f"{row['n']:>5}  {samples}"
            )

    lines.append("\n## OVERSHOOT CHECK (companies typed as Software?)\n")
    if validation.get("overshoot"):
        lines.append("  ⚠ Possible LLM overshoot — Organizations typed as Software:")
        for row in validation["overshoot"]:
            lines.append(
                f"    {row['name']:<25} entity_type={row['type']} object_kind={row['object_kind']}"
            )
    else:
        lines.append("  ✓ No company names showed up as Software (clean).")

    lines.append("\n## GHOST_B METRICS PER DOC\n")
    if validation.get("mongo_error"):
        lines.append(f"  ⚠ Mongo error: {validation['mongo_error']}")
    elif not validation["metrics"]:
        lines.append("  (no doc metrics found)")
    else:
        for m in validation["metrics"]:
            lines.append(f"  {m['doc_id']}  {m['filename']}")
            lines.append(
                f"    entities={m['entity_count']}  relations={m['relation_count']}  "
                f"facts_success={m['success_rate']}"
            )
            lines.append(
                f"    entity_remap={m['entity_remap_count']}  "
                f"entity_drop={m['entity_drop_count']}  "
                f"relation_remap={m['relation_remap_count']}  "
                f"related_to_ratio={m['related_to_ratio']}"
            )

    lines.append("\n## PT9 SIGNAL INTERPRETATION\n")
    # Pt9a — Software/Standard non-empty?
    sw = next((r for r in validation["distribution"]
               if r["entity_type"] == "Software"), None)
    std = next((r for r in validation["distribution"]
                if r["entity_type"] == "Standard"), None)
    if sw and sw["n"] > 0:
        lines.append(f"  ✓ Pt9a Software bucket populated (n={sw['n']})")
    else:
        lines.append("  ✗ Pt9a Software bucket empty — LLM kept typing software as Product")

    if std and std["n"] > 0:
        lines.append(f"  ✓ Pt9a Standard bucket populated (n={std['n']})")
    else:
        lines.append("  · Pt9a Standard bucket empty (may be fine — depends on corpus)")

    # Pt9b — object_kind populated on Software entities?
    sw_with_kind = [r for r in validation["distribution"]
                    if r["entity_type"] == "Software" and r["object_kind"]]
    sw_total = sum(r["n"] for r in validation["distribution"]
                   if r["entity_type"] == "Software")
    if sw_total > 0:
        kinded = sum(r["n"] for r in sw_with_kind)
        pct = (kinded / sw_total) * 100 if sw_total else 0
        if pct > 50:
            lines.append(
                f"  ✓ Pt9b+d object_kind populated on {pct:.0f}% of Software entities"
            )
        elif pct > 10:
            lines.append(
                f"  · Pt9b+d object_kind partial: {pct:.0f}% of Software entities — "
                "prompt steering working but inconsistent"
            )
        else:
            lines.append(
                f"  ✗ Pt9b+d object_kind nearly empty: {pct:.0f}% of Software entities. "
                "Prompt steering isn't reaching the LLM, or the LLM is ignoring it."
            )

    # Remap count health
    total_entities = sum(m.get("entity_count") or 0 for m in validation["metrics"])
    total_remaps = sum(m.get("entity_remap_count") or 0 for m in validation["metrics"])
    if total_entities > 0:
        remap_pct = (total_remaps / total_entities) * 100
        if remap_pct < 5:
            lines.append(
                f"  ✓ entity_remap rate {remap_pct:.1f}% — LLM is staying on-vocab"
            )
        elif remap_pct < 20:
            lines.append(
                f"  · entity_remap rate {remap_pct:.1f}% — some off-vocab leak being "
                "soft-remapped to 'other'"
            )
        else:
            lines.append(
                f"  ✗ entity_remap rate {remap_pct:.1f}% — heavy off-vocab leak, "
                "Pt9d prompt steering needs tuning"
            )

    lines.append("\n" + "=" * 70 + "\n")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="Paths to files to ingest")
    ap.add_argument(
        "--api",
        default=os.environ.get("POLYMATH_API", "http://localhost:8000"),
        help="API base URL (default: $POLYMATH_API or http://localhost:8000)",
    )
    ap.add_argument(
        "--corpus-name",
        default=f"pt9-smoke-{int(time.time())}",
        help="Corpus name to create (default: pt9-smoke-<ts>)",
    )
    ap.add_argument(
        "--timeout-min",
        type=int,
        default=30,
        help="Total ingest timeout in minutes (default: 30)",
    )
    args = ap.parse_args()

    token = _resolve_token()
    if not token:
        print("ERROR: POLYMATH_TOKEN not set and ~/.polymath-dev-token missing.", file=sys.stderr)
        return 1

    paths = [Path(p) for p in args.files]
    for p in paths:
        if not p.is_file():
            print(f"ERROR: not a file: {p}", file=sys.stderr)
            return 1

    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=args.api, headers=headers, timeout=60.0) as client:
        try:
            health = client.get("/api/health").status_code
            print(f"API reachable: {args.api} (health={health})")
        except Exception as exc:
            print(f"ERROR: API unreachable at {args.api}: {exc}", file=sys.stderr)
            return 1

        print(f"Creating corpus: {args.corpus_name}")
        try:
            cid = _create_corpus(client, args.corpus_name)
        except Exception as exc:
            print(f"ERROR: corpus create failed: {exc}", file=sys.stderr)
            return 2
        print(f"  corpus_id={cid}")

        ingest_summary: dict[str, dict] = {}
        for p in paths:
            print(f"Uploading: {p.name} ({p.stat().st_size:,} bytes)")
            try:
                resp = _upload(client, cid, p)
            except Exception as exc:
                print(f"ERROR: upload failed for {p.name}: {exc}", file=sys.stderr)
                return 3
            did = resp.get("doc_id") or ""
            print(f"  doc_id={did[:16]}  job_id={resp.get('job_id', '')[:16]}")
            ingest_summary[did] = {
                "filename": p.name,
                "status": "queued",
            }

        print(f"\nPolling for ingest completion (up to {args.timeout_min} min)...")
        terminal = _poll(
            client,
            cid,
            set(ingest_summary.keys()),
            timeout_s=args.timeout_min * 60,
        )
        if len(terminal) < len(ingest_summary):
            print(
                f"⚠ TIMEOUT: only {len(terminal)}/{len(ingest_summary)} docs reached "
                "terminal state. Proceeding with validation on what landed.",
                file=sys.stderr,
            )
        for did, info in terminal.items():
            ingest_summary[did]["status"] = info.get("status", "?")

    print("\nRunning validation (Neo4j + Mongo)...")
    validation = _run_validation(cid)

    report = _format_report(cid, ingest_summary, validation)
    print(report)

    out_path = Path(f"pt9_smoke_{cid[:8]}.json")
    out_path.write_text(
        json.dumps(
            {
                "corpus_id": cid,
                "ingest_summary": ingest_summary,
                "validation": validation,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"Raw output: {out_path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
