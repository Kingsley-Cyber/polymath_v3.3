"""Corpus ingest wall-time report — instrument, never narrate.

Owner directive (2026-08-08): the primary ingest metric is
CORPUS_INPUT → FULLY_INGESTED_AND_VERIFIED total wall-clock, decomposed into
MEASURED stage time — never invented explanations. Sources of truth:

  * documents lifecycle timestamps        → per-doc + corpus wall
  * stage_attempts (duration_ms)          → Graphify stage costs
  * graphify_stage_receipts reports       → census inference s, OpenIE engine/s
  * ingest-worker docker logs (-t)        → phase boundaries (parse/embed/
                                            summary/ghost/neo4j timings)
  * chunks/parents/bytes                  → throughput normalization

Usage:  report_ingest_walltime.py --corpus <corpus_id> [--container polymath_v33-ingest-worker-2]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import timezone

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from dotenv import dotenv_values  # noqa: E402
from pymongo import MongoClient  # noqa: E402

ENV = dotenv_values(os.path.join(_BACKEND, "..", ".env"))


def _mongo():
    uri = re.sub(r"@mongodb:", "@localhost:", ENV.get("MONGODB_URI") or ENV.get("MONGO_URI") or "")
    return MongoClient(uri, serverSelectionTimeoutMS=8000)[ENV.get("MONGODB_DB", "polymath")]


def _epoch(value) -> float | None:
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--container", default="polymath_v33-ingest-worker-2")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    db = _mongo()
    cid = args.corpus

    docs = list(db.documents.find({"corpus_id": cid}))
    chunks = db.chunks.count_documents({"corpus_id": cid})
    parents = db.parent_chunks.count_documents({"corpus_id": cid})
    source_bytes = sum(int(d.get("file_size") or d.get("source_bytes") or 0) for d in docs)
    starts = [t for d in docs if (t := _epoch(d.get("created_at")))]
    ends = [t for d in docs if (t := _epoch(d.get("updated_at")))]
    corpus_wall = (max(ends) - min(starts)) if starts and ends else None

    # ── Graphify stage costs (authoritative durations) ─────────────────
    attempts = list(db.stage_attempts.find({"corpus_id": cid, "duration_ms": {"$ne": None}}))
    stage_ms: Counter[str] = Counter()
    for row in attempts:
        stage_ms[row["stage"]] += int(row["duration_ms"] or 0)
    receipts = list(db.graphify_stage_receipts.find({"corpus_id": cid}))
    census_inference_s = 0.0
    openie_s = 0.0
    openie_units = 0
    openie_engines: Counter[str] = Counter()
    windows = 0
    for row in receipts:
        rep = (row.get("receipt") or {}).get("report") or (row.get("receipt") or {})
        if row["stage"] == "ENTITY_CENSUS_COMPLETE":
            census_inference_s += float(rep.get("inference_seconds") or 0)
            windows += int(rep.get("windows") or rep.get("window_count") or 0)
        if row["stage"] == "OPENIE_EXTRACTION_COMPLETE":
            openie_s += float(rep.get("elapsed_seconds") or 0)
            openie_units += int(rep.get("eligible_prose_units") or 0)
            openie_engines[str(rep.get("engine") or "?")] += 1

    # ── worker phase boundaries from timestamped container logs ────────
    phase_seconds: dict[str, float] = defaultdict(float)
    log_lines: list[tuple[float, str]] = []
    try:
        raw = subprocess.run(
            ["docker", "logs", "-t", "--since", "6h", args.container],
            capture_output=True, text=True, timeout=120,
        )
        stamp = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z?\s+(.*)$")
        from datetime import datetime
        for line in (raw.stdout + raw.stderr).splitlines():
            match = stamp.match(line)
            if not match:
                continue
            ts = datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone.utc).timestamp()
            log_lines.append((ts, match.group(2)))
        doc_ids = {str(d.get("doc_id") or d.get("_id"))[:12] for d in docs}
        marks = [
            (ts, text) for ts, text in log_lines
            if cid[:8] in text or any(did in text for did in doc_ids)
        ]
        # Attribute inter-mark gaps to the phase named by the EARLIER mark.
        phase_re = re.compile(r"phase=([a-z0-9_]+)")
        for (ts_a, text_a), (ts_b, _text_b) in zip(marks, marks[1:]):
            found = phase_re.search(text_a)
            if found and 0 <= ts_b - ts_a < 1800:
                phase_seconds[found.group(1)] += ts_b - ts_a
        for ts, text in marks:
            neo = re.search(r"delete_document_graph_s=([\d.]+)", text)
            if neo:
                phase_seconds["neo4j_delete"] += float(neo.group(1))
            proj = re.search(r"project_via_control_plane_s=([\d.]+)", text)
            if proj:
                phase_seconds["neo4j_project"] += float(proj.group(1))
    except Exception as exc:  # noqa: BLE001
        phase_seconds["log_harvest_error"] = -1
        print(f"(worker log harvest unavailable: {exc})", file=sys.stderr)

    graphify_stage_seconds = {k: v / 1000 for k, v in sorted(stage_ms.items(), key=lambda i: -i[1])}
    graphify_total_s = sum(graphify_stage_seconds.values())
    report = {
        "corpus_total": {
            "corpus_id": cid,
            "source_bytes": source_bytes,
            "documents": len(docs),
            "chunks": chunks,
            "parents": parents,
            "wall_seconds": round(corpus_wall, 1) if corpus_wall else None,
        },
        "graphify_stage_seconds": {k: round(v, 1) for k, v in graphify_stage_seconds.items()},
        "graphify_total_seconds": round(graphify_total_s, 1),
        "gliner2": {
            "inference_seconds": round(census_inference_s, 1),
        },
        "openie": {
            "stage_seconds": round(openie_s, 1),
            "eligible_units": openie_units,
            "units_per_sec": round(openie_units / openie_s, 2) if openie_s else None,
            "engines": dict(openie_engines),
        },
        "worker_phase_seconds": {
            k: round(v, 1) for k, v in sorted(phase_seconds.items(), key=lambda i: -i[1])
        },
        "throughput": {
            "source_mb_per_min": round(source_bytes / 1e6 / (corpus_wall / 60), 3)
            if corpus_wall and source_bytes else None,
            "chunks_per_sec": round(chunks / corpus_wall, 3) if corpus_wall else None,
            "docs_per_hour": round(len(docs) / (corpus_wall / 3600), 2) if corpus_wall else None,
        },
        "accounting": {
            "attributed_seconds": round(graphify_total_s + sum(
                v for k, v in phase_seconds.items() if v > 0 and not k.startswith("log_")
            ), 1),
            "note": "gaps between attributed time and wall = queueing/serialization/"
                    "unlogged phases — the next instrumentation target, never a guess",
        },
    }
    print(json.dumps(report, indent=1))
    if args.json_out:
        with open(args.json_out, "w") as handle:
            json.dump(report, handle, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
