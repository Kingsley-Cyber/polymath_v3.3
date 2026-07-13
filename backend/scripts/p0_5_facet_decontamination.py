#!/usr/bin/env python3
"""P0.5 facet decontamination: measure and strip corpus-lens-inherited facet
ids that lack per-row content evidence (dry-run by default).

Checklist item (docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md, P0.5 adopted):
"strip corpus-lens-inherited facets that lack per-document content evidence
(measure facet DF per corpus; a lens category is not evidence every document
teaches it), then backfill cleaned facet payloads."

What it does, per active corpus discovered from Mongo `corpora` (never
hardcoded):

  - Computes facet document-frequency (DF) across `parent_chunks.facet_ids`.
  - Classifies every row's facet_ids as content-evidenced vs lens-inherited
    with `classify_facets` (structural rule, identical for every corpus):
      * lens id set = ids the corpus `schema_lens` (union per-document
        `documents.schema_lens` snapshot) would stamp, via the exact
        production mapping `services.facets.normalizer.schema_lens_facet_ids`.
      * per-row content evidence = the row's own `content_facet_ids` plus
        facet ids derived from the row's own `heading_path`
        (`normalizer.heading_local_facet_ids`).
      * a facet id is lens-inherited iff it is in the lens id set AND the row
        carries no content evidence for it. All other ids (filename, heading,
        document-content) are never candidates for removal.
  - Documents have no content_facet_ids field; a document's per-content
    evidence is the union of its parent rows' evidence.
  - Prints the top-10 facet DF per corpus with lens-inherited counts.

--apply (destructive, reversible):
  - FIRST writes JSONL backups (modified `_id` + prior facet_ids) to
    docs/baselines/p0_5_backups/, THEN issues $pull updates on
    `parent_chunks.facet_ids` and `documents.facet_profile.facet_ids`
    (documents carry facet ids inside `facet_profile`; there is no top-level
    documents.facet_ids field) in 1000-op bulk batches.
  - Restore: for each backup line, `$set` the stored field back to
    `facet_ids` for `ObjectId(_id)`.

Never touches `content_facet_ids`, `facet_text`, the corpus `schema_lens`
record, or Qdrant/Neo4j projections.

Run from the deployment host (127.0.0.1 service ports + repo .env), same
pattern as scripts/capture_raptor_baseline.py:

    python3 backend/scripts/p0_5_facet_decontamination.py            # dry run
    python3 backend/scripts/p0_5_facet_decontamination.py --apply    # write
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

BACKEND = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(REPO / ".tmp_pkgs"))

from services.facets.normalizer import (  # noqa: E402
    heading_local_facet_ids,
    schema_lens_facet_ids,
)

BACKUP_DIR = REPO / "docs" / "baselines" / "p0_5_backups"
DEFAULT_BATCH_SIZE = 1000


def classify_facets(row: dict, lens_facet_ids: set | None = None) -> tuple:
    """Partition ``row["facet_ids"]`` into (content_evidenced, lens_inherited).

    Structural evidence-field rule, identical for every corpus:

      - ``lens_facet_ids`` is the set of facet ids the corpus/document schema
        lens would stamp. When not passed, it is read from the row's optional
        ``schema_lens_facet_ids`` field (missing -> empty set).
      - Per-row content evidence = the row's own ``content_facet_ids`` plus
        facet ids derived from the row's own ``heading_path``.
      - A facet id is lens-inherited iff it is in the lens id set and absent
        from the row's evidence. Every other id counts as content-evidenced
        (filename/heading/document-content ids are never removal candidates).

    Missing fields and empty lists are treated as "no ids"/"no evidence".
    Returns ``(content_evidenced_ids, lens_inherited_ids)`` as sets.
    """

    facet_ids = {str(f) for f in (row.get("facet_ids") or ()) if f}
    if not facet_ids:
        return set(), set()
    if lens_facet_ids is None:
        lens_facet_ids = {
            str(f) for f in (row.get("schema_lens_facet_ids") or ()) if f
        }
    lens_ids = {str(f) for f in lens_facet_ids if f}
    if not lens_ids:
        return facet_ids, set()
    evidence = {str(f) for f in (row.get("content_facet_ids") or ()) if f}
    evidence.update(
        fid for fid in heading_local_facet_ids(row.get("heading_path") or []) if fid
    )
    lens_inherited = {f for f in facet_ids if f in lens_ids and f not in evidence}
    return facet_ids - lens_inherited, lens_inherited


def _env() -> dict:
    out: dict = {}
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _mongo(env: dict):
    from pymongo import MongoClient

    uri = (env.get("MONGODB_URI") or "").replace("@mongodb:", "@127.0.0.1:")
    if not uri:
        user = env.get("MONGO_USER", "polymath")
        pwd = quote_plus(env.get("MONGO_PASSWORD") or "")
        uri = f"mongodb://{user}:{pwd}@127.0.0.1:27017/polymath?authSource=admin"
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    db = client[env.get("MONGODB_DATABASE", "polymath")]
    db.command("ping")
    return client, db


def _lens_ids(schema_lens: Any) -> set:
    if not isinstance(schema_lens, dict):
        return set()
    return {fid for fid in schema_lens_facet_ids(schema_lens) if fid}


def _active_corpora(db, requested: "list | None") -> list:
    corpora = [
        c
        for c in db.corpora.find(
            {}, {"_id": 0, "corpus_id": 1, "name": 1, "status": 1}
        )
        if c.get("corpus_id") and c.get("status") in (None, "active")
    ]
    if requested:
        wanted = set(requested)
        corpora = [c for c in corpora if c["corpus_id"] in wanted]
    return corpora


def audit_corpus(db, corpus: dict, *, top_n: int) -> dict:
    """Read-only census + classification for one corpus.

    Returns per-corpus stats plus the exact removal operations an --apply run
    would perform (list of (_id, prior_facet_ids, removed_ids) per collection).
    """

    cid = corpus["corpus_id"]
    corpus_row = db.corpora.find_one({"corpus_id": cid}, {"schema_lens": 1}) or {}
    corpus_lens = _lens_ids(corpus_row.get("schema_lens"))

    docs = list(
        db.documents.find(
            {"corpus_id": cid},
            {"_id": 1, "doc_id": 1, "schema_lens": 1, "facet_profile.facet_ids": 1},
        )
    )
    doc_lens: dict = {}
    for doc in docs:
        doc_lens[str(doc.get("doc_id") or "")] = corpus_lens | _lens_ids(
            doc.get("schema_lens")
        )

    facet_df: Counter = Counter()
    removal_df: Counter = Counter()
    doc_evidence: dict = defaultdict(set)
    parent_removals: list = []
    parents_total = 0
    parents_with_removal = 0

    cursor = db.parent_chunks.find(
        {"corpus_id": cid},
        {"_id": 1, "doc_id": 1, "facet_ids": 1, "content_facet_ids": 1, "heading_path": 1},
    )
    for row in cursor:
        parents_total += 1
        doc_id = str(row.get("doc_id") or "")
        fids = [str(f) for f in (row.get("facet_ids") or []) if f]
        facet_df.update(set(fids))
        evidence = {str(f) for f in (row.get("content_facet_ids") or ()) if f}
        evidence.update(
            fid
            for fid in heading_local_facet_ids(row.get("heading_path") or [])
            if fid
        )
        doc_evidence[doc_id].update(evidence)
        _kept, removed = classify_facets(row, doc_lens.get(doc_id, corpus_lens))
        if removed:
            parents_with_removal += 1
            removal_df.update(removed)
            parent_removals.append((row["_id"], fids, sorted(removed)))

    doc_removals: list = []
    docs_with_removal = 0
    for doc in docs:
        doc_id = str(doc.get("doc_id") or "")
        prior = [
            str(f)
            for f in ((doc.get("facet_profile") or {}).get("facet_ids") or [])
            if f
        ]
        # A document's per-content evidence = union of its parents' evidence.
        synthetic = {
            "facet_ids": prior,
            "content_facet_ids": sorted(doc_evidence.get(doc_id, ())),
        }
        _kept, removed = classify_facets(synthetic, doc_lens.get(doc_id, corpus_lens))
        if removed:
            docs_with_removal += 1
            doc_removals.append((doc["_id"], prior, sorted(removed)))

    top = []
    for fid, df in facet_df.most_common(top_n):
        top.append(
            {
                "facet_id": fid,
                "parent_df": df,
                "lens_inherited": removal_df.get(fid, 0),
                "evidenced_or_non_lens": df - removal_df.get(fid, 0),
                "in_lens": fid in corpus_lens
                or any(fid in ids for ids in doc_lens.values()),
            }
        )

    return {
        "corpus_id": cid,
        "name": corpus.get("name") or cid,
        "lens_facet_id_count": len(corpus_lens),
        "documents_total": len(docs),
        "documents_with_lens_inherited": docs_with_removal,
        "parents_total": parents_total,
        "parents_with_lens_inherited": parents_with_removal,
        "parent_facet_removals_total": sum(removal_df.values()),
        "top_facets": top,
        "_parent_removals": parent_removals,
        "_doc_removals": doc_removals,
    }


def _print_corpus_report(stats: dict, *, top_n: int) -> None:
    pct = (
        100.0 * stats["parents_with_lens_inherited"] / stats["parents_total"]
        if stats["parents_total"]
        else 0.0
    )
    print(
        f"\n== corpus {stats['name']} ({stats['corpus_id'][:8]}) =="
        f"\n  lens facet ids: {stats['lens_facet_id_count']}"
        f"\n  parent_chunks: total={stats['parents_total']}"
        f" rows_with_lens_inherited={stats['parents_with_lens_inherited']}"
        f" ({pct:.1f}%)"
        f" facet_id_removals={stats['parent_facet_removals_total']}"
        f"\n  documents:     total={stats['documents_total']}"
        f" docs_with_lens_inherited={stats['documents_with_lens_inherited']}"
        f"\n  top-{top_n} parent facet DF"
        " (facet_id, df, lens_inherited, evidenced_or_non_lens, in_lens):"
    )
    for row in stats["top_facets"]:
        print(
            f"    {row['facet_id']:<44} df={row['parent_df']:<7}"
            f" lens_inherited={row['lens_inherited']:<7}"
            f" kept={row['evidenced_or_non_lens']:<7}"
            f" in_lens={row['in_lens']}"
        )


def _write_backup(path: Path, corpus_id: str, collection: str, removals: list) -> int:
    lines = 0
    with path.open("a", encoding="utf-8") as handle:
        for _id, prior, removed in removals:
            handle.write(
                json.dumps(
                    {
                        "collection": collection,
                        "corpus_id": corpus_id,
                        "_id": str(_id),
                        "facet_ids": prior,
                        "removed": removed,
                    },
                    default=str,
                )
                + "\n"
            )
            lines += 1
    return lines


def _apply_removals(
    collection,
    removals: list,
    *,
    field: str,
    batch_size: int,
) -> int:
    from pymongo import UpdateOne

    modified = 0
    for start in range(0, len(removals), batch_size):
        batch = [
            UpdateOne({"_id": _id}, {"$pull": {field: {"$in": removed}}})
            for _id, _prior, removed in removals[start : start + batch_size]
        ]
        result = collection.bulk_write(batch, ordered=False)
        modified += result.modified_count
    return modified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--corpus-id",
        action="append",
        dest="corpus_ids",
        help="Corpus id to process. Repeat for multiple. Default: all active corpora from Mongo.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write $pull updates (JSONL backups are written first). Default: dry run, no writes.",
    )
    parser.add_argument("--top", type=int, default=10, help="Top-N facets per corpus (default 10).")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Bulk-write batch size for --apply (default 1000).",
    )
    args = parser.parse_args()

    env = _env()
    client, db = _mongo(env)
    try:
        corpora = _active_corpora(db, args.corpus_ids)
        if not corpora:
            print("No matching active corpora found in Mongo `corpora`.")
            return 1
        mode = "APPLY" if args.apply else "DRY-RUN (no writes)"
        print(f"P0.5 facet decontamination — {mode}; corpora={len(corpora)}")

        all_stats = []
        for corpus in corpora:
            stats = audit_corpus(db, corpus, top_n=args.top)
            _print_corpus_report(stats, top_n=args.top)
            all_stats.append(stats)

        total_parent = sum(len(s["_parent_removals"]) for s in all_stats)
        total_doc = sum(len(s["_doc_removals"]) for s in all_stats)
        print(
            f"\nTOTAL rows needing decontamination:"
            f" parent_chunks={total_parent} documents={total_doc}"
        )

        if not args.apply:
            print("Dry run complete. Re-run with --apply to write (backups first).")
            return 0

        # --apply: backups FIRST, then batched $pull updates.
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
        parent_backup = BACKUP_DIR / f"parent_chunks_{stamp}.jsonl"
        doc_backup = BACKUP_DIR / f"documents_{stamp}.jsonl"
        backed_up = 0
        for stats in all_stats:
            backed_up += _write_backup(
                parent_backup, stats["corpus_id"], "parent_chunks", stats["_parent_removals"]
            )
            backed_up += _write_backup(
                doc_backup, stats["corpus_id"], "documents", stats["_doc_removals"]
            )
        print(f"Backups written FIRST: {backed_up} rows -> {parent_backup} , {doc_backup}")

        modified_parents = 0
        modified_docs = 0
        for stats in all_stats:
            modified_parents += _apply_removals(
                db.parent_chunks,
                stats["_parent_removals"],
                field="facet_ids",
                batch_size=args.batch_size,
            )
            modified_docs += _apply_removals(
                db.documents,
                stats["_doc_removals"],
                field="facet_profile.facet_ids",
                batch_size=args.batch_size,
            )
        print(
            f"APPLIED: parent_chunks modified={modified_parents}"
            f" documents modified={modified_docs}"
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
