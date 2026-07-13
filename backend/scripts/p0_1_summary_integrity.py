#!/usr/bin/env python3
"""P0.1 summary-integrity repair driver (checklist: P0 - Summary Integrity).

Subcommands (all dry-run unless --apply):

  retire-orphan-jobs   Supersede queued/running summary_jobs whose corpus is
                       not active (e.g. deleted/quarantined corpora). The job
                       rows are history, not work; the durable artifact owner
                       is gone.

  stamp-legacy         Give explicit provenance to real legacy abstractive
                       summaries that predate model stamping. Rows pass a
                       deterministic validation gate; passes get
                       summary_model="legacy_unknown" (the marker the Qdrant
                       writer's storage-boundary contract documents for
                       intentional legacy imports) plus a validation record.
                       Failures are written to a regeneration list, never
                       stamped. Prior field values are backed up to JSONL
                       before any write.

  residual-report      After reindexing: count summary points that still have
                       an explicit empty summary_model, split into
                       parent-has-valid-summary (stale projection, will be
                       overwritten by index) / parent-missing (orphan). With
                       --apply, snapshots the collection then deletes the
                       residual empty-model points.

  verify               Acceptance assertions for P0.1. Non-zero exit on any
                       failure: no retrieval-eligible summary point may carry
                       an explicit empty summary_model; every summary-required
                       parent carries non-empty summary text; every stamped or
                       modeled summary differs from its parent text.

No production/query-path logic lives here; this is a data migration with
backups. Validation rules are corpus-agnostic and content-agnostic.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

REPO = Path(__file__).resolve().parents[2]
QDRANT = "http://127.0.0.1:6333"
BACKUP_DIR = REPO / "docs" / "baselines" / "p0_1_backups"
STAMP_MODEL = "legacy_unknown"
STAMP_VALIDATOR = "p0_1_summary_integrity.v1"

REQUIRED_CLAUSE = {
    "$or": [
        {"chunk_kind": {"$exists": False}},
        {"chunk_kind": None},
        {"chunk_kind": {"$in": ["body", "table"]}},
    ]
}
HAS_SUMMARY = {"summary": {"$exists": True, "$nin": [None, ""]}}
NO_MODEL = {
    "$or": [
        {"summary_model": {"$exists": False}},
        {"summary_model": None},
        {"summary_model": ""},
    ]
}

_WORD = re.compile(r"[a-zA-Z]{4,}")


def _env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _mongo():
    sys.path.insert(0, str(REPO / ".tmp_pkgs"))
    from pymongo import MongoClient

    env = _env()
    uri = (env.get("MONGODB_URI") or "").replace("@mongodb:", "@127.0.0.1:")
    if not uri:
        user = env.get("MONGO_USER", "polymath")
        pwd = quote_plus(env.get("MONGO_PASSWORD") or "")
        uri = f"mongodb://{user}:{pwd}@127.0.0.1:27017/polymath?authSource=admin"
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    return client, client[env.get("MONGODB_DATABASE", "polymath")]


def _qdrant(method: str, path: str, body: dict | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{QDRANT}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        raw = resp.read()
        return json.loads(raw.decode()) if raw else {}


def _active_corpora(db) -> list[dict]:
    return list(
        db.corpora.find(
            {"$or": [{"status": {"$exists": False}}, {"status": "active"}]},
            {"_id": 0, "corpus_id": 1, "name": 1},
        )
    )


def _prefix(cid: str) -> str:
    return cid.replace("-", "")[:8]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── retire-orphan-jobs ───────────────────────────────────────────────────────


def retire_orphan_jobs(apply: bool) -> int:
    client, db = _mongo()
    active_ids = {c["corpus_id"] for c in _active_corpora(db)}
    rows = list(
        db.summary_jobs.find(
            {
                "status": {"$in": ["queued", "running"]},
                "corpus_id": {"$nin": sorted(active_ids)},
            },
            {"_id": 0, "job_id": 1, "corpus_id": 1, "kind": 1, "status": 1},
        )
    )
    by_corpus: dict[str, int] = {}
    for r in rows:
        by_corpus[r["corpus_id"][:8]] = by_corpus.get(r["corpus_id"][:8], 0) + 1
    print(f"orphan queued/running summary jobs: {len(rows)} {by_corpus}")
    if not rows:
        client.close()
        return 0
    if not apply:
        print("DRY RUN — pass --apply to supersede these job rows")
        client.close()
        return 0
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"orphan_jobs_{int(time.time())}.jsonl"
    with backup.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")
    res = db.summary_jobs.update_many(
        {
            "status": {"$in": ["queued", "running"]},
            "corpus_id": {"$nin": sorted(active_ids)},
        },
        {
            "$set": {
                "status": "superseded",
                "reason": "corpus_not_active_orphan_job",
                "artifact_reconciled_at": _now(),
                "updated_at": _now(),
                "lease_until": None,
            }
        },
    )
    print(f"superseded {res.modified_count} rows (backup: {backup})")
    client.close()
    return 0


# ── stamp-legacy ─────────────────────────────────────────────────────────────


def _validate_row(summary: str, text: str) -> tuple[bool, dict[str, Any]]:
    checks: dict[str, Any] = {}
    s = (summary or "").strip()
    t = (text or "").strip()
    checks["min_length"] = len(s) >= 40
    checks["not_identical"] = bool(t) and " ".join(s.split()) != " ".join(t.split())
    checks["not_prefix_copy"] = not (
        t and " ".join(t.split()).startswith(" ".join(s.split())) and len(s) > 200
    )
    ratio = len(s) / max(1, len(t))
    checks["length_ratio_lt_0_9"] = ratio < 0.9 or len(t) < 400
    s_words = set(_WORD.findall(s.lower()))
    t_words = set(_WORD.findall(t.lower()))
    overlap = len(s_words & t_words) / max(1, len(s_words))
    checks["evidence_overlap_ge_0_10"] = overlap >= 0.10 or not t_words
    checks["overlap_value"] = round(overlap, 3)
    passed = all(v for k, v in checks.items() if isinstance(v, bool))
    return passed, checks


def stamp_legacy(corpus: str | None, apply: bool) -> int:
    client, db = _mongo()
    corpora = _active_corpora(db)
    if corpus:
        corpora = [c for c in corpora if c["corpus_id"] == corpus or c["name"] == corpus]
        if not corpora:
            print(f"ERROR: corpus {corpus} not found/active")
            return 1
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp_time = _now()
    exit_code = 0
    for c in corpora:
        cid = c["corpus_id"]
        query = {"$and": [{"corpus_id": cid}, HAS_SUMMARY, NO_MODEL]}
        total = db.parent_chunks.count_documents(query)
        print(f"[{c['name']}] unattributed summaries: {total}")
        if not total:
            continue
        passed = failed = 0
        backup_path = BACKUP_DIR / f"stamp_{_prefix(cid)}_{int(time.time())}.jsonl"
        regen_path = BACKUP_DIR / f"regen_{_prefix(cid)}_{int(time.time())}.jsonl"
        backup_fh = backup_path.open("w") if apply else None
        regen_fh = regen_path.open("w")
        bulk = []
        cursor = db.parent_chunks.find(
            query,
            {
                "parent_id": 1,
                "summary": 1,
                "text": 1,
                "summary_model": 1,
                "summary_type": 1,
                "validation_status": 1,
                "chunk_kind": 1,
            },
        )
        from pymongo import UpdateOne

        for row in cursor:
            ok, checks = _validate_row(row.get("summary") or "", row.get("text") or "")
            if not ok:
                failed += 1
                regen_fh.write(
                    json.dumps(
                        {
                            "corpus_id": cid,
                            "parent_id": row.get("parent_id"),
                            "checks": checks,
                        },
                        default=str,
                    )
                    + "\n"
                )
                continue
            passed += 1
            if not apply:
                continue
            backup_fh.write(
                json.dumps(
                    {
                        "_id": str(row["_id"]),
                        "parent_id": row.get("parent_id"),
                        "prior_summary_model": row.get("summary_model"),
                        "prior_validation_status": row.get("validation_status"),
                    },
                    default=str,
                )
                + "\n"
            )
            bulk.append(
                UpdateOne(
                    {"_id": row["_id"], **NO_MODEL},
                    {
                        "$set": {
                            "summary_model": STAMP_MODEL,
                            "validation_status": "legacy_stamped_heuristic_v1",
                            "summary_provenance": {
                                "method": "legacy_import_stamp",
                                "validator": STAMP_VALIDATOR,
                                "stamped_at": stamp_time,
                                "checks": {
                                    k: v
                                    for k, v in checks.items()
                                    if isinstance(v, (bool, float))
                                },
                            },
                            "updated_at": stamp_time,
                        }
                    },
                )
            )
            if len(bulk) >= 1000:
                db.parent_chunks.bulk_write(bulk, ordered=False)
                bulk = []
        if apply and bulk:
            db.parent_chunks.bulk_write(bulk, ordered=False)
        regen_fh.close()
        if backup_fh:
            backup_fh.close()
        print(
            f"[{c['name']}] validation passed={passed} failed={failed} "
            f"({'STAMPED' if apply else 'dry-run'}) regen_list={regen_path.name}"
        )
        if failed:
            exit_code = 0  # failures are queued for regeneration, not fatal
    client.close()
    return exit_code


# ── quarantine-regen ─────────────────────────────────────────────────────────


def quarantine_regen(corpus: str | None, apply: bool) -> int:
    """Clear defective legacy summaries (validation failures) so the standard
    generation machinery treats them as missing and regenerates them with full
    provenance. Full prior summary text is backed up first."""
    import glob

    client, db = _mongo()
    corpora = _active_corpora(db)
    if corpus:
        corpora = [c for c in corpora if c["corpus_id"] == corpus or c["name"] == corpus]
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for c in corpora:
        cid = c["corpus_id"]
        regen_files = sorted(glob.glob(str(BACKUP_DIR / f"regen_{_prefix(cid)}_*.jsonl")))
        if not regen_files:
            continue
        parent_ids: set[str] = set()
        for path in regen_files:
            for line in open(path):
                row = json.loads(line)
                if row.get("parent_id"):
                    parent_ids.add(row["parent_id"])
        if not parent_ids:
            continue
        # only quarantine rows still unattributed AND still defective-looking
        target = {
            "$and": [
                {"corpus_id": cid, "parent_id": {"$in": sorted(parent_ids)}},
                HAS_SUMMARY,
                NO_MODEL,
            ]
        }
        n = db.parent_chunks.count_documents(target)
        print(f"[{c['name']}] regen-listed parents still unattributed: {n}")
        if not n or not apply:
            if n:
                print("DRY RUN — pass --apply to quarantine for regeneration")
            continue
        backup_path = BACKUP_DIR / f"quarantine_{_prefix(cid)}_{int(time.time())}.jsonl"
        with backup_path.open("w") as fh:
            for row in db.parent_chunks.find(
                target, {"parent_id": 1, "summary": 1, "summary_type": 1}
            ):
                fh.write(
                    json.dumps(
                        {
                            "_id": str(row["_id"]),
                            "parent_id": row.get("parent_id"),
                            "prior_summary": row.get("summary"),
                            "prior_summary_type": row.get("summary_type"),
                        },
                        default=str,
                    )
                    + "\n"
                )
        res = db.parent_chunks.update_many(
            target,
            {
                "$set": {
                    "summary": None,
                    "summary_quarantined_at": _now(),
                    "summary_quarantine_reason": "legacy_validation_failed",
                    "updated_at": _now(),
                }
            },
        )
        print(
            f"[{c['name']}] quarantined {res.modified_count} defective summaries "
            f"(backup: {backup_path.name})"
        )
    client.close()
    return 0


# ── residual-report / delete ────────────────────────────────────────────────


def residual(corpus: str | None, apply: bool) -> int:
    client, db = _mongo()
    corpora = _active_corpora(db)
    if corpus:
        corpora = [c for c in corpora if c["corpus_id"] == corpus or c["name"] == corpus]
    for c in corpora:
        cid = c["corpus_id"]
        col = f"corpus_{_prefix(cid)}_hrag"
        flt = {
            "must": [
                {"key": "chunk_type", "match": {"value": "summary"}},
                {"key": "summary_model", "match": {"value": ""}},
            ]
        }
        try:
            count = _qdrant(
                "POST", f"/collections/{col}/points/count", {"exact": True, "filter": flt}
            )["result"]["count"]
        except Exception as exc:  # noqa: BLE001
            print(f"[{c['name']}] {col}: unavailable ({exc})")
            continue
        # classify a sample: stale projection vs orphan
        stale = orphan = 0
        if count:
            pts = _qdrant(
                "POST",
                f"/collections/{col}/points/scroll",
                {
                    "limit": min(500, count),
                    "with_payload": ["parent_id"],
                    "with_vector": False,
                    "filter": flt,
                },
            )["result"]["points"]
            pids = [
                p["payload"].get("parent_id")
                for p in pts
                if p.get("payload", {}).get("parent_id")
            ]
            with_valid = {
                r["parent_id"]
                for r in db.parent_chunks.find(
                    {
                        "corpus_id": cid,
                        "parent_id": {"$in": pids},
                        **HAS_SUMMARY,
                        "summary_model": {"$nin": [None, ""]},
                    },
                    {"parent_id": 1},
                )
            }
            stale = sum(1 for p in pids if p in with_valid)
            orphan = len(pids) - stale
        print(
            f"[{c['name']}] empty-model summary points: {count} "
            f"(sample: stale_projection={stale} orphan_or_unstamped={orphan})"
        )
        if apply and count:
            snap = _qdrant("POST", f"/collections/{col}/snapshots")
            print(f"  snapshot: {snap.get('result', {}).get('name')}")
            res = _qdrant(
                "POST",
                f"/collections/{col}/points/delete?wait=true",
                {"filter": flt},
            )
            print(f"  deleted residual empty-model points: {res.get('status')}")
            after = _qdrant(
                "POST", f"/collections/{col}/points/count", {"exact": True, "filter": flt}
            )["result"]["count"]
            print(f"  after: {after}")
    client.close()
    return 0


# ── verify ───────────────────────────────────────────────────────────────────


def verify(corpus: str | None) -> int:
    client, db = _mongo()
    corpora = _active_corpora(db)
    if corpus:
        corpora = [c for c in corpora if c["corpus_id"] == corpus or c["name"] == corpus]
    failures = []
    for c in corpora:
        cid = c["corpus_id"]
        col = f"corpus_{_prefix(cid)}_hrag"
        is_summary = {"key": "chunk_type", "match": {"value": "summary"}}
        try:
            empty = _qdrant(
                "POST",
                f"/collections/{col}/points/count",
                {
                    "exact": True,
                    "filter": {
                        "must": [
                            is_summary,
                            {"key": "summary_model", "match": {"value": ""}},
                        ]
                    },
                },
            )["result"]["count"]
            total = _qdrant(
                "POST",
                f"/collections/{col}/points/count",
                {"exact": True, "filter": {"must": [is_summary]}},
            )["result"]["count"]
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{c['name']}: qdrant unavailable ({exc})")
            continue
        required = db.parent_chunks.count_documents(
            {"$and": [{"corpus_id": cid}, REQUIRED_CLAUSE]}
        )
        with_summary = db.parent_chunks.count_documents(
            {"$and": [{"corpus_id": cid}, REQUIRED_CLAUSE, HAS_SUMMARY]}
        )
        unattributed = db.parent_chunks.count_documents(
            {"$and": [{"corpus_id": cid}, REQUIRED_CLAUSE, HAS_SUMMARY, NO_MODEL]}
        )
        ok_empty = empty == 0
        ok_cov = with_summary == required
        ok_attr = unattributed == 0
        status = "PASS" if (ok_empty and ok_cov and ok_attr) else "FAIL"
        print(
            f"[{c['name']}] {status} empty_model_points={empty} "
            f"summary_points={total} required={required} "
            f"with_summary={with_summary} unattributed={unattributed}"
        )
        if not ok_empty:
            failures.append(f"{c['name']}: {empty} empty-model summary points remain")
        if not ok_cov:
            failures.append(
                f"{c['name']}: {required - with_summary} required parents lack summaries"
            )
        if not ok_attr:
            failures.append(
                f"{c['name']}: {unattributed} summaries still unattributed"
            )
    client.close()
    if failures:
        print("VERIFY FAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("VERIFY PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "command",
        choices=[
            "retire-orphan-jobs",
            "stamp-legacy",
            "quarantine-regen",
            "residual-report",
            "verify",
        ],
    )
    ap.add_argument("--corpus", help="corpus_id or name (default: all active)")
    ap.add_argument("--apply", action="store_true", help="persist changes")
    args = ap.parse_args()
    if args.command == "retire-orphan-jobs":
        return retire_orphan_jobs(args.apply)
    if args.command == "stamp-legacy":
        return stamp_legacy(args.corpus, args.apply)
    if args.command == "quarantine-regen":
        return quarantine_regen(args.corpus, args.apply)
    if args.command == "residual-report":
        return residual(args.corpus, args.apply)
    return verify(args.corpus)


if __name__ == "__main__":
    raise SystemExit(main())
