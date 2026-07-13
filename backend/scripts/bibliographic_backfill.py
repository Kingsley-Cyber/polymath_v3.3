#!/usr/bin/env python3
"""T-HOOK-3 / P2.1 deterministic bibliographic backfill (documents only).

The migration is dry-run by default and never rewrites parents/chunks.  Apply
mode is deliberately two phase: plan every document, durably write a complete
collision-proof JSONL pre-image backup, then execute CAS-guarded document
updates.  ``--restore-backup`` replays the same presence-aware snapshots and is
also dry-run unless ``--apply`` is supplied.

An apply requires an explicit durable ``--backup-dir`` (or
``BIBLIO_BACKUP_DIR``).  Container-local ``/tmp`` is not an acceptable default.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
for candidate in (HERE.parent.parent, HERE.parent.parent / "backend"):
    if (candidate / "services" / "ingestion" / "bibliographic.py").exists():
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        break

from services.ingestion.bibliographic import (  # noqa: E402
    BIBLIO_DOC_FIELDS,
    DATE_IDENTITY_FIELDS,
    DEFAULT_HEAD_CHARS,
    KIND_AMBIGUOUS,
    KIND_FILE_CREATION,
    DateCandidate,
    build_provenance,
    extract_text_head_biblio,
    filename_year_candidate,
    merge_persisted_bibliographic,
    parse_citation_name,
    resolve_document_dates,
)

BACKFILL_ORIGIN = "backfill_v2"
BACKUP_VERSION = 2
COVERAGE_FIELDS = (
    "author", "title", "language", "document_date", "source_published_at",
    "date_confidence", "bibliographic_provenance",
)
# Pre-hook parsers whose legacy document_date was a file/container timestamp.
_FILE_TIME_PARSERS = ("local_docx", "pypdf_fast_text", "docling_sidecar")
_FILE_TIME_METHODS = {
    "docx_core_created", "docx_core_modified",
    "pdf_creation_date", "pdf_mod_date",
    "frontmatter_created", "frontmatter_modified",
}


def _mongo():
    from motor.motor_asyncio import AsyncIOMotorClient

    uri = (
        os.environ.get("MONGODB_URI")
        or os.environ.get("MONGO_URL")
        or os.environ.get("MONGODB_URL")
    )
    if not uri:
        try:
            from config import get_settings

            settings = get_settings()
            uri = (
                getattr(settings, "MONGODB_URI", None)
                or getattr(settings, "MONGODB_URL", None)
            )
        except Exception:
            uri = None
    if not uri:
        raise SystemExit("MONGODB_URI not set and config unavailable")
    client = AsyncIOMotorClient(uri)
    return client, client.get_default_database()


def _present(value: Any) -> bool:
    return value is not None and value != ""


def _stem_title(filename: str) -> str:
    stem = Path(filename or "").stem
    return re.sub(r"[_\-]+", " ", stem).strip()[:300]


def _title_is_filename_derived(doc: dict) -> bool:
    trace = doc.get("routing_trace") or {}
    if trace.get("title_source") == "filename":
        return True
    title = (doc.get("title") or "").strip()
    return bool(title) and title == _stem_title(doc.get("filename") or "")


def _backfill_stamp(provenance: Any) -> dict:
    if not isinstance(provenance, dict):
        return {}
    nested = provenance.get("backfill")
    if isinstance(nested, dict):
        return nested
    if provenance.get("origin") == BACKFILL_ORIGIN:
        return provenance
    return {}


def _already_backfilled(doc: dict) -> bool:
    return _backfill_stamp(doc.get("bibliographic_provenance")).get("origin") \
        == BACKFILL_ORIGIN


async def _first_parent_head(db, corpus_id: str, doc_id: str, head_chars: int) -> str:
    row = await db["parent_chunks"].find_one(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {"text": 1, "parent_index": 1},
        sort=[("parent_index", 1), ("parent_id", 1)],
    )
    if not row:
        row = await db["chunks"].find_one(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {"text": 1, "chunk_index": 1},
            sort=[("chunk_index", 1), ("chunk_id", 1)],
        )
    return ((row or {}).get("text") or "")[:head_chars]


def _snapshot_fields(doc: dict) -> dict:
    return {
        field: {"present": field in doc, "value": doc.get(field)}
        for field in BIBLIO_DOC_FIELDS
    }


def _snapshot_after(pre_image: dict, set_fields: dict, unset_fields: list[str]) -> dict:
    after = {
        field: {"present": bool(state.get("present")), "value": state.get("value")}
        for field, state in pre_image.items()
    }
    for field, value in set_fields.items():
        if field in after:
            after[field] = {"present": True, "value": value}
    for field in unset_fields:
        if field in after:
            after[field] = {"present": False, "value": None}
    return after


def _snapshot_filter(doc_id: str, corpus_id: str, snapshot: dict) -> dict:
    clauses: list[dict] = [{"doc_id": doc_id}, {"corpus_id": corpus_id}]
    for field, state in snapshot.items():
        if state.get("present"):
            clauses.append({field: state.get("value")})
        else:
            clauses.append({field: {"$exists": False}})
    return {"$and": clauses}


def _mongo_update(set_fields: dict, unset_fields: list[str]) -> dict:
    update: dict = {}
    if set_fields:
        update["$set"] = set_fields
    if unset_fields:
        update["$unset"] = {field: "" for field in unset_fields}
    return update


def plan_for_document(
    doc: dict,
    head_text: str,
    *,
    captured_at: str | None = None,
    run_id: str | None = None,
) -> dict | None:
    """Return a pure, presence-aware CAS plan for one document.

    The plan contains separate ``set_fields``/``unset_fields`` and complete
    before/after snapshots.  Known file-time legacy dates are removed unless a
    publication-grade candidate supersedes them.  An ingest provenance stamp
    with an honest null reason does not block a later deterministic date.
    """

    captured_at = captured_at or datetime.now(timezone.utc).isoformat()
    run_id = run_id or uuid.uuid4().hex
    existing_provenance = doc.get("bibliographic_provenance")
    provenance = existing_provenance if isinstance(existing_provenance, dict) else {}
    candidates: list[DateCandidate] = []
    set_fields: dict = {}
    unset_fields: list[str] = []

    legacy_date = doc.get("document_date")
    parser = str((doc.get("routing_trace") or {}).get("parser") or "")
    legacy_without_provenance = bool(legacy_date and not provenance)
    legacy_file_only = bool(legacy_date) and (
        (legacy_without_provenance and parser in _FILE_TIME_PARSERS)
        or provenance.get("method") in _FILE_TIME_METHODS
        or provenance.get("reason") == "file_date_only"
    )
    if legacy_without_provenance:
        if legacy_file_only:
            candidates.append(DateCandidate(
                raw=str(legacy_date)[:80],
                kind=KIND_FILE_CREATION,
                method="docx_core_created" if parser == "local_docx"
                else "pdf_creation_date",
                source=f"legacy_document_date:{parser}",
            ))
        else:
            candidates.append(DateCandidate(
                raw=str(legacy_date)[:80],
                kind=KIND_AMBIGUOUS,
                method="legacy_document_date",
                source=f"legacy_document_date:{parser or 'unknown'}",
            ))
    elif legacy_file_only:
        # Unsafe backfill_v1 may already have stamped the row without clearing
        # its file-time date.  Reconstitute that observation so the v2 null
        # provenance records the honest file_date_only reason.
        method = str(provenance.get("method") or "")
        if method not in _FILE_TIME_METHODS:
            method = "docx_core_created" if parser == "local_docx" \
                else "pdf_creation_date"
        candidates.append(DateCandidate(
            raw=str(legacy_date)[:80],
            kind=KIND_FILE_CREATION,
            method=method,
            source=f"legacy_document_date:{parser or 'unsafe_backfill_v1'}",
        ))

    filename = doc.get("filename") or ""
    citation = parse_citation_name(filename)
    author = citation.get("author")
    citation_title = citation.get("title")
    if citation.get("year_raw"):
        candidates.append(DateCandidate(
            raw=citation["year_raw"], kind=KIND_AMBIGUOUS,
            method="citation_pattern", source=f"filename:{filename[:150]}",
        ))
    filename_year = filename_year_candidate(filename)
    if filename_year:
        candidates.append(filename_year)

    head = extract_text_head_biblio(head_text)
    candidates.extend(head.get("candidates") or [])
    author = author or head.get("author")
    citation_title = citation_title or head.get("title")

    if author and not _present(doc.get("author")):
        set_fields["author"] = author
    if citation_title and _title_is_filename_derived(doc) \
            and citation_title.lower() != str(doc.get("title") or "").lower():
        set_fields["title"] = citation_title

    resolution = resolve_document_dates(candidates)
    candidate_provenance = build_provenance(
        method=resolution["method"],
        source=resolution["source"],
        precision=resolution["precision"],
        reason=resolution["reason"],
        origin=BACKFILL_ORIGIN,
        captured_at=captured_at,
    )
    chosen_provenance: dict = dict(provenance)
    if resolution["document_date"]:
        candidate_family = {
            "document_date": resolution["document_date"],
            "source_published_at": resolution["source_published_at"],
            "date_confidence": resolution["date_confidence"],
            "bibliographic_provenance": candidate_provenance,
        }
        chosen = merge_persisted_bibliographic(candidate_family, doc)
        chosen_provenance = dict(chosen.get("bibliographic_provenance") or {})
        for field in DATE_IDENTITY_FIELDS:
            if _present(chosen.get(field)) and doc.get(field) != chosen.get(field):
                set_fields[field] = chosen[field]
    elif legacy_file_only:
        # The only observed legacy value is a file/container time.  It is not a
        # low-confidence publication date; remove the whole identity family.
        for field in DATE_IDENTITY_FIELDS:
            if field in doc:
                unset_fields.append(field)
        chosen_provenance = candidate_provenance
    elif not chosen_provenance:
        chosen_provenance = candidate_provenance

    data_fields_set = sorted(set_fields)
    data_fields_unset = sorted(set(unset_fields))
    needs_stamp = bool(data_fields_set or data_fields_unset or not provenance)
    # Old unsafe backfill_v1 rows are intentionally eligible for a v2 receipt;
    # an honest ingest capture with no new deterministic information is not.
    if provenance.get("origin") == "backfill_v1":
        needs_stamp = True
    if not needs_stamp:
        return None

    chosen_provenance = dict(chosen_provenance or candidate_provenance)
    chosen_provenance["backfill"] = {
        "origin": BACKFILL_ORIGIN,
        "run_id": run_id,
        "captured_at": captured_at,
        "fields_set": data_fields_set,
        "fields_unset": data_fields_unset,
    }
    if provenance and provenance != chosen_provenance:
        chosen_provenance.setdefault("prior", provenance)
    set_fields["bibliographic_provenance"] = chosen_provenance

    unset_fields = sorted(set(unset_fields) - set(set_fields))
    pre_image = _snapshot_fields(doc)
    post_image = _snapshot_after(pre_image, set_fields, unset_fields)
    return {
        "doc_id": doc["doc_id"],
        "corpus_id": doc["corpus_id"],
        "set_fields": set_fields,
        "unset_fields": unset_fields,
        "pre_image": pre_image,
        "post_image": post_image,
        "cas_filter": _snapshot_filter(doc["doc_id"], doc["corpus_id"], pre_image),
    }


async def coverage(db, corpus_id: str) -> dict:
    out = {"docs": await db["documents"].count_documents({"corpus_id": corpus_id})}
    for field in COVERAGE_FIELDS:
        out[field] = await db["documents"].count_documents({
            "corpus_id": corpus_id,
            field: {"$exists": True, "$nin": [None, ""]},
        })
    return out


async def corpora_map(db) -> dict[str, str]:
    rows = await db["corpora"].find(
        {}, {"corpus_id": 1, "name": 1}
    ).to_list(500)
    return {row["corpus_id"]: (row.get("name") or row["corpus_id"]) for row in rows}


def _backup_row(plan: dict, *, run_id: str, captured_at: str) -> dict:
    return {
        "_backup_kind": "documents_biblio",
        "backup_version": BACKUP_VERSION,
        "run_id": run_id,
        "captured_at": captured_at,
        "doc_id": plan["doc_id"],
        "corpus_id": plan["corpus_id"],
        "pre_image": plan["pre_image"],
        "post_image": plan["post_image"],
        "planned_set": plan["set_fields"],
        "planned_unset": plan["unset_fields"],
    }


def _fsync_directory(path: Path) -> None:
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_durable_backup(
    plans: list[dict],
    *,
    backup_dir: Path,
    corpus_id: str,
    run_id: str,
    captured_at: str,
) -> tuple[Path, str]:
    """Write all pre-images durably before any database update."""

    if not plans:
        raise ValueError("refusing to create an empty backup")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = backup_dir / (
        f"documents_{corpus_id[:8]}_{stamp}_{run_id[:12]}.jsonl"
    )
    digest = hashlib.sha256()
    with path.open("x", encoding="utf-8") as handle:
        for plan in plans:
            encoded = (
                json.dumps(
                    _backup_row(plan, run_id=run_id, captured_at=captured_at),
                    sort_keys=True,
                    default=str,
                )
                + "\n"
            ).encode("utf-8")
            handle.write(encoded.decode("utf-8"))
            digest.update(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(backup_dir)
    return path, digest.hexdigest()


async def run_corpus(
    db,
    corpus_id: str,
    corpus_name: str,
    *,
    apply: bool,
    force: bool,
    head_chars: int,
    backup_dir: Path | None,
    limit: int | None,
) -> dict:
    before = await coverage(db, corpus_id)
    run_id = uuid.uuid4().hex
    captured_at = datetime.now(timezone.utc).isoformat()
    plans: list[dict] = []
    skipped_stamped = 0
    reason_hist: Counter = Counter()
    field_hist: Counter = Counter()
    spot_row: dict | None = None

    projection = {
        "doc_id": 1, "corpus_id": 1, "filename": 1, "title": 1,
        "author": 1, "language": 1, "document_date": 1,
        "source_published_at": 1, "date_confidence": 1,
        "bibliographic_provenance": 1, "routing_trace": 1,
    }
    cursor = db["documents"].find(
        {"corpus_id": corpus_id}, projection
    ).sort("doc_id", 1)
    seen = 0
    async for doc in cursor:
        if limit is not None and seen >= limit:
            break
        seen += 1
        if _already_backfilled(doc) and not force:
            skipped_stamped += 1
            continue
        head_text = await _first_parent_head(db, corpus_id, doc["doc_id"], head_chars)
        plan = plan_for_document(
            doc,
            head_text,
            captured_at=captured_at,
            run_id=run_id,
        )
        if plan is None:
            skipped_stamped += 1
            continue
        provenance = plan["set_fields"]["bibliographic_provenance"]
        reason_hist[provenance.get("reason") or "date_resolved"] += 1
        for field in plan["set_fields"]:
            if field != "bibliographic_provenance":
                field_hist[field] += 1
        for field in plan["unset_fields"]:
            field_hist[f"unset:{field}"] += 1
        if spot_row is None:
            spot_row = {
                "doc_id": doc["doc_id"][:16],
                "filename": (doc.get("filename") or "")[:80],
                "set_fields": sorted(plan["set_fields"]),
                "unset_fields": plan["unset_fields"],
            }
        plans.append(plan)

    backup_path: Path | None = None
    backup_sha256: str | None = None
    applied = 0
    modified = 0
    cas_conflicts = 0
    noops = 0
    aborted = False
    if apply and plans:
        if backup_dir is None:
            raise ValueError("apply requires an explicit durable backup_dir")
        backup_path, backup_sha256 = _write_durable_backup(
            plans,
            backup_dir=backup_dir,
            corpus_id=corpus_id,
            run_id=run_id,
            captured_at=captured_at,
        )
        for plan in plans:
            result = await db["documents"].update_one(
                plan["cas_filter"],
                _mongo_update(plan["set_fields"], plan["unset_fields"]),
            )
            if int(getattr(result, "matched_count", 0)) != 1:
                cas_conflicts += 1
                aborted = True
                break
            applied += 1
            changed = int(getattr(result, "modified_count", 0))
            modified += changed
            if changed != 1:
                noops += 1
                aborted = True
                break

    after = await coverage(db, corpus_id) if apply else None
    report = {
        "corpus": corpus_name,
        "corpus_id": corpus_id,
        "mode": "apply" if apply else "dry-run",
        "run_id": run_id,
        "planned": len(plans),
        "processed": applied if apply else len(plans),
        "applied": applied,
        "modified": modified,
        "cas_conflicts": cas_conflicts,
        "noops": noops,
        "aborted": aborted,
        # ``applied`` already includes a matched-but-unmodified row.  Conflicts
        # are the only attempted rows not represented by that counter.
        "not_attempted": max(0, len(plans) - applied - cas_conflicts),
        "skipped_already_stamped": skipped_stamped,
        "coverage_before": before,
        "coverage_after": after,
        "fields_set_histogram": dict(field_hist),
        "reason_histogram": dict(reason_hist),
        "backup": str(backup_path) if backup_path else None,
        "backup_rows": len(plans) if backup_path else 0,
        "backup_sha256": backup_sha256,
        "spot_check": spot_row,
    }
    print(json.dumps(report, indent=2, default=str))
    return report


def _load_backup(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("_backup_kind") != "documents_biblio" \
                    or row.get("backup_version") != BACKUP_VERSION:
                raise ValueError(f"unsupported backup row at line {line_no}")
            rows.append(row)
    if not rows:
        raise ValueError("refusing to restore an empty backup")
    return rows


def _restore_operation(row: dict) -> tuple[dict, list[str]]:
    set_fields: dict = {}
    unset_fields: list[str] = []
    for field, state in row["pre_image"].items():
        if state.get("present"):
            set_fields[field] = state.get("value")
        else:
            unset_fields.append(field)
    return set_fields, sorted(unset_fields)


async def restore_backup(db, path: Path, *, apply: bool) -> dict:
    """Presence-aware, CAS-guarded restore. Dry-run unless apply=True."""

    rows = _load_backup(path)
    restored = 0
    modified = 0
    cas_conflicts = 0
    noops = 0
    aborted = False
    if apply:
        for row in rows:
            set_fields, unset_fields = _restore_operation(row)
            result = await db["documents"].update_one(
                _snapshot_filter(row["doc_id"], row["corpus_id"], row["post_image"]),
                _mongo_update(set_fields, unset_fields),
            )
            if int(getattr(result, "matched_count", 0)) != 1:
                cas_conflicts += 1
                aborted = True
                break
            restored += 1
            changed = int(getattr(result, "modified_count", 0))
            modified += changed
            if changed != 1:
                noops += 1
                aborted = True
                break
    report = {
        "mode": "restore-apply" if apply else "restore-dry-run",
        "backup": str(path),
        "planned": len(rows),
        "restored": restored,
        "modified": modified,
        "cas_conflicts": cas_conflicts,
        "noops": noops,
        "aborted": aborted,
        "not_attempted": max(0, len(rows) - restored - cas_conflicts),
    }
    print(json.dumps(report, indent=2))
    return report


async def _amain(args) -> int:
    client, db = _mongo()
    try:
        if args.restore_backup:
            report = await restore_backup(db, Path(args.restore_backup), apply=args.apply)
            return 3 if report["aborted"] else 0

        names = await corpora_map(db)
        targets = args.corpus_id or list(names.keys())
        unknown = [corpus_id for corpus_id in targets if corpus_id not in names]
        if unknown:
            print(f"ERROR: unknown corpus ids: {unknown}")
            return 2

        if args.verify:
            for corpus_id in targets:
                print(json.dumps({
                    "corpus": names[corpus_id],
                    "corpus_id": corpus_id,
                    "coverage": await coverage(db, corpus_id),
                }, indent=2))
            return 0

        if args.apply and not args.backup_dir:
            print("ERROR: --apply requires --backup-dir or BIBLIO_BACKUP_DIR")
            return 2
        exit_code = 0
        for corpus_id in targets:
            report = await run_corpus(
                db,
                corpus_id,
                names[corpus_id],
                apply=args.apply,
                force=args.force,
                head_chars=args.head_chars,
                backup_dir=Path(args.backup_dir) if args.backup_dir else None,
                limit=args.limit,
            )
            if report["aborted"]:
                exit_code = 3
                break
        if not args.apply:
            print("Dry run complete. Re-run with --apply and a durable backup dir.")
        return exit_code
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus-id", action="append",
                        help="restrict to corpus id (repeatable; default: all)")
    parser.add_argument("--apply", action="store_true",
                        help="apply a planned backfill/restore; default is dry-run")
    parser.add_argument("--verify", action="store_true",
                        help="print coverage and exit without planning")
    parser.add_argument("--force", action="store_true",
                        help="re-plan rows already stamped by backfill_v2")
    parser.add_argument("--restore-backup",
                        help="restore a v2 backup JSONL (dry-run unless --apply)")
    parser.add_argument("--head-chars", type=int, default=DEFAULT_HEAD_CHARS)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--backup-dir",
        default=os.environ.get("BIBLIO_BACKUP_DIR"),
        help="durable host-mounted pre-image directory (required for --apply)",
    )
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
