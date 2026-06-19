"""Deterministic document-deduplication pipeline: DETECT / PREVENT / CORRECT.

Single source of truth for Polymath's near-duplicate *document* handling. All
three stages share one shingle core so they can never disagree:

  PREVENT  — ingest-time guard. ``worker.run_ingest_job`` calls
             ``find_near_duplicates`` before chunking. A near-IDENTICAL incoming
             doc (containment >= block threshold) is skipped; a merely near-dup
             one is INGESTED and FLAGGED for review — never silently dropped, so
             a legitimately distinct work (e.g. the Java edition of a book whose
             C++ edition is already present) can never be lost at ingest.
  DETECT   — corpus-wide all-pairs scan. ``find_duplicate_clusters`` finds every
             cluster of near-duplicate documents already inside a corpus.
  CORRECT  — ``resolve_duplicate_clusters`` keeps one canonical copy per cluster
             and cascade-deletes the redundant copies, after BACKING UP each
             doc record and VERIFYING the cascade left no orphans.

WHY TWO SIGNALS (Jaccard *and* containment). A single similarity threshold is
unsafe: a true reformat (PDF vs MD of one book) scores LOWER Jaccard (~0.32,
different parse) than two distinct works that share prose but differ in code
(a C++ vs Java textbook edition, ~0.82). No Jaccard cutoff separates "drop the
reformat" from "keep both editions." So we add **containment** = |A∩B| / |the
smaller set| — how fully the smaller file is already inside the larger. A
redundant copy is ~fully contained; two distinct-but-similar works are not.
Even so, lexical signals CANNOT distinguish "same prose / different code", so
only the near-identical tier is ever auto-actioned; everything else is
surfaced for a human decision.

DETERMINISM CONTRACT — a corpus's content fully determines the output:
  * the shingle set is a pure function of text (fixed regex + stop-words),
  * Jaccard and containment are exact (no MinHash/LSH randomness, no seeds),
  * clustering is union-find over doc_ids sorted lexicographically,
  * canonical selection is a total order (chunk quality -> recency -> doc_id).
Same corpus -> same clusters -> same canonical -> same deletions, every run.

(NVIDIA's RAG-blueprint / NeMo-Retriever skills ship no document dedup at all —
only filename idempotency and a 0.95 hard-negative training margin. This
pipeline is a net addition over that reference stack.)
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from services.ingestion.section_classifier import NOISY_KINDS
from services.storage import mongo_reader

logger = logging.getLogger(__name__)

# ── Deterministic shingle fingerprint (shared by PREVENT + DETECT) ───────────
# A content word: a letter-led token of length >= 3. Numbers/punctuation alone
# never start a token, so page numbers and markup symbols don't inflate overlap.
DUPLICATE_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_'-]{2,}")
DUPLICATE_STOP_WORDS = {
    "and", "are", "but", "for", "from", "have", "into", "not", "the", "that",
    "this", "with", "you", "your", "their", "there", "then", "than", "was",
    "were", "will", "would", "could", "should", "about", "which",
}
DEFAULT_SHINGLE_K = 5
# Jaccard threshold to even CONSIDER two docs related. Deliberately low — it is
# only the first gate; containment (below) is what actually decides duplication.
DEFAULT_DUPLICATE_THRESHOLD = 0.10
# Below this many shingles a document is too short to fingerprint reliably; it is
# excluded from clustering (never a false-positive duplicate on a stub).
MIN_SHINGLES = 24

# ── Containment-based confidence ─────────────────────────────────────────────
# containment = |A∩B| / |smaller set|. The single most useful signal: how fully
# the smaller file is already contained in the larger.
#   certain : the smaller file is ~entirely inside the larger -> removing it
#             loses essentially nothing. The ONLY tier ever auto-actioned.
#   likely  : strong overlap, but the smaller file has some unique passages ->
#             almost always a duplicate, but a human confirms.
#   review  : weak containment OR both files carry substantial unique content
#             (the "same prose / different code" trap) -> human required.
CERTAIN_CONTAINMENT = 0.95
LIKELY_CONTAINMENT = 0.65
# Two docs are not even an edge in the duplicate graph unless the smaller is at
# least half inside the larger. This is what kills transitive false-positive
# clusters (distinct notes that merely share boilerplate link/tool lists).
MIN_EDGE_CONTAINMENT = 0.50

DUP_CERTAIN = "certain"
DUP_LIKELY = "likely"
DUP_REVIEW = "review"


def classify_confidence(containment: float) -> str:
    if containment >= CERTAIN_CONTAINMENT:
        return DUP_CERTAIN
    if containment >= LIKELY_CONTAINMENT:
        return DUP_LIKELY
    return DUP_REVIEW


def content_words(texts: Iterable[str]) -> list[str]:
    """Ordered content words across `texts` (lowercased, stop-words removed)."""
    words: list[str] = []
    for text in texts:
        for match in DUPLICATE_TOKEN_RE.finditer(str(text or "").lower()):
            token = match.group(0).strip("'_-")
            if token and token not in DUPLICATE_STOP_WORDS:
                words.append(token)
    return words


def shingle_set(texts: Iterable[str], k: int = DEFAULT_SHINGLE_K) -> set[str]:
    """Near-duplicate fingerprint keyed on shared TEXT (overlapping content-word
    k-grams), not just shared vocabulary. Two different docs on the same topic
    share lots of words but few k-grams; this separates "same book, reformat"
    from "different books, same field" far more reliably than a token set."""
    words = content_words(texts)
    if len(words) < k:
        return set()
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def overlap(a: set[str], b: set[str]) -> tuple[float, float]:
    """Return (jaccard, containment). Exact; deterministic. containment is the
    intersection over the SMALLER set — ~1.0 means the smaller document adds
    almost nothing not already in the larger one."""
    if not a or not b:
        return 0.0, 0.0
    inter = len(a & b)
    if not inter:
        return 0.0, 0.0
    union = len(a) + len(b) - inter
    smaller = min(len(a), len(b))
    return (inter / union if union else 0.0), (inter / smaller if smaller else 0.0)


def jaccard(a: set[str], b: set[str]) -> float:
    """Exact Jaccard overlap (kept for the ingest-time PREVENT path)."""
    return overlap(a, b)[0]


def containment(a: set[str], b: set[str]) -> float:
    """Exact containment of the smaller set in the larger."""
    return overlap(a, b)[1]


def _can_exceed_threshold(size_a: int, size_b: int, threshold: float) -> bool:
    """Sound prune: Jaccard <= min(|A|,|B|) / max(|A|,|B|). If that ceiling is
    below the threshold the pair CANNOT be a duplicate, so the expensive set
    intersection is skipped. Never drops a true positive (it's an upper bound),
    so the scan stays exact while pruning length-mismatched pairs cheaply."""
    if size_a <= 0 or size_b <= 0:
        return False
    return (min(size_a, size_b) / max(size_a, size_b)) >= threshold


# ── Records ──────────────────────────────────────────────────────────────────
@dataclass
class DuplicateMember:
    doc_id: str
    filename: str
    chunk_count: int          # total child chunks
    retrievable_count: int    # non-noisy child chunks (the real retrieval weight)
    ingested_at: Optional[datetime]
    ingest_stage: str
    shingle_count: int
    is_canonical: bool = False
    similarity_to_canonical: float = 0.0       # Jaccard
    containment_to_canonical: float = 0.0      # fraction of THIS file inside canonical
    confidence: str = ""                       # certain | likely | review (non-canonical)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "filename": self.filename,
            "chunk_count": self.chunk_count,
            "retrievable_count": self.retrievable_count,
            "ingested_at": (
                self.ingested_at.isoformat()
                if isinstance(self.ingested_at, datetime)
                else self.ingested_at
            ),
            "ingest_stage": self.ingest_stage,
            "shingle_count": self.shingle_count,
            "is_canonical": self.is_canonical,
            "similarity_to_canonical": round(self.similarity_to_canonical, 4),
            "containment_to_canonical": round(self.containment_to_canonical, 4),
            "confidence": self.confidence,
        }


@dataclass
class DuplicateCluster:
    members: list[DuplicateMember]
    canonical_doc_id: str
    max_similarity: float
    pairwise: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)

    @property
    def canonical(self) -> DuplicateMember:
        return next(m for m in self.members if m.doc_id == self.canonical_doc_id)

    @property
    def redundant(self) -> list[DuplicateMember]:
        return [m for m in self.members if m.doc_id != self.canonical_doc_id]

    @property
    def confidence(self) -> str:
        """Cluster confidence = the WEAKEST redundant member (most cautious)."""
        order = {DUP_CERTAIN: 0, DUP_LIKELY: 1, DUP_REVIEW: 2}
        worst = max(
            (order.get(m.confidence, 2) for m in self.redundant), default=2
        )
        return {0: DUP_CERTAIN, 1: DUP_LIKELY, 2: DUP_REVIEW}[worst]

    @property
    def auto_safe(self) -> bool:
        """True only if every redundant copy is near-identical to the canonical."""
        return all(m.confidence == DUP_CERTAIN for m in self.redundant)

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_doc_id": self.canonical_doc_id,
            "confidence": self.confidence,
            "auto_safe": self.auto_safe,
            "max_similarity": round(self.max_similarity, 4),
            "members": [m.to_dict() for m in self.members],
            "redundant_doc_ids": [m.doc_id for m in self.redundant],
            "redundant_chunks": sum(m.chunk_count for m in self.redundant),
        }


def _ingested_sort_key(value: Any) -> float:
    """Earlier-first sort key; missing/unknown timestamps sort LAST (worst)."""
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return math.inf


def _noisy_kind_values() -> list[str]:
    """The chunk_kind string values that drop out of retrieval — the
    authoritative NOISY_KINDS tuple from the section classifier, so a doc's
    "retrievable" weight here matches exactly what search actually serves."""
    return list(NOISY_KINDS)


def choose_canonical(members: list[DuplicateMember]) -> str:
    """Pick the copy to KEEP — a total deterministic order, best first:
      1. most retrievable (non-noisy) chunks — the copy carrying the most real
         search weight (a cleaner parse usually beats a noisier one),
      2. most total chunks (tie-break on completeness),
      3. earliest ingested_at — keep the original, drop the later re-add,
      4. lexicographically smallest doc_id — guarantees a total order so the
         result is identical on every run regardless of input ordering.
    """
    return sorted(
        members,
        key=lambda m: (
            -int(m.retrievable_count),
            -int(m.chunk_count),
            _ingested_sort_key(m.ingested_at),
            m.doc_id,
        ),
    )[0].doc_id


# ── DETECT ────────────────────────────────────────────────────────────────────
async def _list_corpus_documents(
    db: AsyncIOMotorDatabase, corpus_id: str, *, user_id: Optional[str] = None
) -> list[dict]:
    """All documents in a corpus (paged), decorated with chunk_count/parent_count
    by ``mongo_reader.list_documents``. user_id=None scans the whole corpus."""
    out: list[dict] = []
    offset = 0
    page = 500
    while True:
        rows = await mongo_reader.list_documents(
            db, corpus_id, user_id=user_id, limit=page, offset=offset
        )
        if not rows:
            break
        out.extend(rows)
        if len(rows) < page:
            break
        offset += page
    return out


async def _retrievable_counts(
    db: AsyncIOMotorDatabase, corpus_id: str, doc_ids: list[str]
) -> dict[str, int]:
    """Per-doc count of NON-noisy child chunks — the real retrieval weight, used
    to pick the canonical copy. One aggregation for the whole corpus."""
    if not doc_ids:
        return {}
    noisy = _noisy_kind_values()
    pipeline = [
        {
            "$match": {
                "corpus_id": corpus_id,
                "doc_id": {"$in": doc_ids},
                "chunk_kind": {"$nin": noisy},
            }
        },
        {"$group": {"_id": "$doc_id", "count": {"$sum": 1}}},
    ]
    return {
        row["_id"]: int(row["count"])
        async for row in db["chunks"].aggregate(pipeline)
    }


async def _document_fingerprint(
    db: AsyncIOMotorDatabase, corpus_id: str, doc_id: str, *, k: int
) -> set[str]:
    """Shingle fingerprint for one doc, built from its parent chunks (the same
    text basis the ingest-time PREVENT check uses). Falls back to child chunks
    for legacy docs with no parent rows."""
    parents = await mongo_reader.get_parent_chunks(db, doc_id, corpus_id)
    texts = [str(p.get("text") or "") for p in parents if isinstance(p, dict)]
    if not any(t.strip() for t in texts):
        rows = await db["chunks"].find(
            {"doc_id": doc_id, "corpus_id": corpus_id}, {"text": 1, "_id": 0}
        ).to_list(length=None)
        texts = [str(r.get("text") or "") for r in rows]
    return shingle_set(texts, k=k)


async def find_duplicate_clusters(
    db: AsyncIOMotorDatabase,
    corpus_id: str,
    *,
    threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
    min_edge_containment: float = MIN_EDGE_CONTAINMENT,
    k: int = DEFAULT_SHINGLE_K,
    min_shingles: int = MIN_SHINGLES,
    user_id: Optional[str] = None,
) -> list[DuplicateCluster]:
    """Corpus-wide near-duplicate DETECT. Returns one cluster per group of
    mutually/transitively near-duplicate documents (a cluster has >= 2 members).

    Two docs form an edge only if Jaccard >= threshold AND containment >=
    min_edge_containment — the containment gate is what stops distinct notes that
    merely share boilerplate from chaining into bogus clusters.

    Exact, deterministic, O(n^2) over documents with a sound length-ratio prune.
    """
    docs = await _list_corpus_documents(db, corpus_id, user_id=user_id)
    docs.sort(key=lambda d: str(d.get("doc_id") or ""))

    retrievable = await _retrievable_counts(
        db, corpus_id, [str(d.get("doc_id") or "") for d in docs]
    )

    fps: dict[str, set[str]] = {}
    meta: dict[str, dict] = {}
    for d in docs:
        doc_id = str(d.get("doc_id") or "")
        if not doc_id:
            continue
        fp = await _document_fingerprint(db, corpus_id, doc_id, k=k)
        if len(fp) < min_shingles:
            continue
        fps[doc_id] = fp
        meta[doc_id] = d

    ids = sorted(fps.keys())
    if len(ids) < 2:
        return []

    parent: dict[str, str] = {i: i for i in ids}

    def find(x: str) -> str:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            parent[hi] = lo

    pair_stats: dict[tuple[str, str], tuple[float, float]] = {}
    for i in range(len(ids)):
        a = ids[i]
        fa = fps[a]
        for j in range(i + 1, len(ids)):
            b = ids[j]
            fb = fps[b]
            if not _can_exceed_threshold(len(fa), len(fb), threshold):
                continue
            jac, cont = overlap(fa, fb)
            # Edge requires BOTH: enough shared text (jaccard) AND the smaller
            # file being substantially inside the larger (containment).
            if jac >= threshold and cont >= min_edge_containment:
                pair_stats[(a, b)] = (jac, cont)
                union(a, b)

    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)

    clusters: list[DuplicateCluster] = []
    for root, member_ids in groups.items():
        if len(member_ids) < 2:
            continue
        member_ids.sort()
        members = [
            DuplicateMember(
                doc_id=mid,
                filename=str(meta[mid].get("filename") or ""),
                chunk_count=int(meta[mid].get("chunk_count") or 0),
                retrievable_count=int(retrievable.get(mid, 0)),
                ingested_at=meta[mid].get("ingested_at"),
                ingest_stage=str(meta[mid].get("ingest_stage") or ""),
                shingle_count=len(fps[mid]),
            )
            for mid in member_ids
        ]
        canonical_id = choose_canonical(members)
        canonical_fp = fps[canonical_id]
        max_sim = 0.0
        for m in members:
            m.is_canonical = m.doc_id == canonical_id
            if m.is_canonical:
                continue
            jac, _ = overlap(canonical_fp, fps[m.doc_id])
            # containment of THIS (redundant) file inside the canonical: how much
            # of it the canonical already covers. Decides the confidence tier.
            mfp = fps[m.doc_id]
            inter = len(mfp & canonical_fp)
            m.similarity_to_canonical = jac
            m.containment_to_canonical = inter / len(mfp) if mfp else 0.0
            m.confidence = classify_confidence(m.containment_to_canonical)
            max_sim = max(max_sim, jac)
        cluster_pairs = {
            (a, b): s for (a, b), s in pair_stats.items() if find(a) == root
        }
        clusters.append(
            DuplicateCluster(
                members=members,
                canonical_doc_id=canonical_id,
                max_similarity=max_sim,
                pairwise=cluster_pairs,
            )
        )

    clusters.sort(key=lambda c: c.canonical_doc_id)
    logger.info(
        "dedup.detect corpus=%s docs=%d fingerprinted=%d clusters=%d "
        "redundant=%d (certain=%d likely=%d review=%d)",
        corpus_id[:8],
        len(docs),
        len(ids),
        len(clusters),
        sum(len(c.redundant) for c in clusters),
        sum(1 for c in clusters for m in c.redundant if m.confidence == DUP_CERTAIN),
        sum(1 for c in clusters for m in c.redundant if m.confidence == DUP_LIKELY),
        sum(1 for c in clusters for m in c.redundant if m.confidence == DUP_REVIEW),
    )
    return clusters


# ── CORRECT ───────────────────────────────────────────────────────────────────
_BACKUP_COLLECTION = "dedup_deleted_backup"


async def _backup_document(db: AsyncIOMotorDatabase, corpus_id: str, doc_id: str,
                           *, keep_doc_id: str, similarity: float,
                           containment_val: float) -> bool:
    """Snapshot a document record before it is cascade-deleted, so a removal is
    auditable and the metadata is recoverable. Idempotent on (doc_id,
    deleted_at-less) — re-backing a doc just refreshes the record."""
    try:
        doc = await db["documents"].find_one(
            {"doc_id": doc_id, "corpus_id": corpus_id}, {"_id": 0}
        )
        if not doc:
            return False
        doc["_dedup_backup"] = {
            "deleted_at": datetime.utcnow(),
            "duplicate_of": keep_doc_id,
            "similarity": round(float(similarity), 4),
            "containment": round(float(containment_val), 4),
        }
        await db[_BACKUP_COLLECTION].update_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {"$set": doc},
            upsert=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("dedup backup failed for %s: %s", doc_id[:12], exc)
        return False


async def _verify_cascade(db: AsyncIOMotorDatabase, corpus_id: str,
                          doc_id: str) -> dict[str, int]:
    """After a delete, confirm Mongo left no orphaned chunk rows for the doc."""
    try:
        chunks = await db["chunks"].count_documents(
            {"doc_id": doc_id, "corpus_id": corpus_id}
        )
        parents = await db["parent_chunks"].count_documents(
            {"doc_id": doc_id, "corpus_id": corpus_id}
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dedup verify failed for %s: %s", doc_id[:12], exc)
        return {"orphan_chunks": -1, "orphan_parents": -1}
    return {"orphan_chunks": int(chunks), "orphan_parents": int(parents)}


async def resolve_duplicate_clusters(
    service: Any,
    corpus_id: str,
    clusters: list[DuplicateCluster],
    *,
    apply: bool = False,
    min_confidence: Optional[str] = None,
    keep_overrides: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """CORRECT — keep one canonical per cluster, cascade-delete the rest, safely.

    Dry-run by default (``apply=False``): returns exactly what *would* be
    deleted, touching nothing. Each real deletion first BACKS UP the doc record
    to ``dedup_deleted_backup`` then runs the production ``service.delete_document``
    cascade (Qdrant -> Neo4j -> Mongo chunks -> doc), and finally VERIFIES no
    orphan chunk rows remain.

    ``min_confidence`` restricts which redundant copies are eligible:
      "certain" -> only near-identical copies (the safe-auto set),
      "likely"  -> certain + likely, None -> every detected redundant copy.
    A copy below the bar is reported with ``skipped_low_confidence``.
    ``keep_overrides`` maps a cluster's canonical_doc_id -> the doc_id to keep
    instead (lets a human flip which copy survives).
    """
    keep_overrides = keep_overrides or {}
    rank = {DUP_CERTAIN: 0, DUP_LIKELY: 1, DUP_REVIEW: 2}
    # Safe by default: with no explicit floor, only near-identical ("certain")
    # copies are eligible. Broadening to "likely"/"review" is an explicit opt-in.
    bar = rank.get(min_confidence or DUP_CERTAIN, 0)

    actions: list[dict[str, Any]] = []
    deleted = 0
    freed_chunks = 0
    errors = 0
    skipped = 0

    for cluster in clusters:
        keep_id = str(keep_overrides.get(cluster.canonical_doc_id, cluster.canonical_doc_id))
        for member in cluster.members:
            if member.doc_id == keep_id:
                continue
            eligible = rank.get(member.confidence, 2) <= bar
            action = {
                "corpus_id": corpus_id,
                "keep_doc_id": keep_id,
                "delete_doc_id": member.doc_id,
                "delete_filename": member.filename,
                "deleted_chunks": member.chunk_count,
                "similarity": round(member.similarity_to_canonical, 4),
                "containment": round(member.containment_to_canonical, 4),
                "confidence": member.confidence,
                "applied": False,
                "skipped_low_confidence": not eligible,
                "backed_up": False,
                "verify": None,
                "error": None,
            }
            if not eligible:
                skipped += 1
                actions.append(action)
                continue
            if apply:
                try:
                    action["backed_up"] = await _backup_document(
                        service.db, corpus_id, member.doc_id,
                        keep_doc_id=keep_id,
                        similarity=member.similarity_to_canonical,
                        containment_val=member.containment_to_canonical,
                    )
                    ok = await service.delete_document(corpus_id, member.doc_id)
                    action["applied"] = bool(ok)
                    if ok:
                        deleted += 1
                        freed_chunks += member.chunk_count
                        action["verify"] = await _verify_cascade(
                            service.db, corpus_id, member.doc_id
                        )
                    else:
                        action["error"] = "document not found"
                    logger.info(
                        "dedup.correct corpus=%s deleted=%s keep=%s conf=%s "
                        "cont=%.3f chunks=%d",
                        corpus_id[:8], member.doc_id[:12], keep_id[:12],
                        member.confidence, member.containment_to_canonical,
                        member.chunk_count,
                    )
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    action["error"] = str(exc)
                    logger.exception(
                        "dedup.correct delete failed corpus=%s doc=%s: %s",
                        corpus_id[:8], member.doc_id[:12], exc,
                    )
            else:
                freed_chunks += member.chunk_count
            actions.append(action)

    eligible_actions = [a for a in actions if not a["skipped_low_confidence"]]
    return {
        "corpus_id": corpus_id,
        "applied": apply,
        "min_confidence": min_confidence,
        "clusters": len(clusters),
        ("documents_deleted" if apply else "documents_to_delete"): (
            deleted if apply else len(eligible_actions)
        ),
        ("chunks_freed" if apply else "chunks_to_free"): freed_chunks,
        "skipped_low_confidence": skipped,
        "errors": errors,
        "actions": actions,
    }


def summarize_clusters(clusters: list[DuplicateCluster]) -> dict[str, Any]:
    """Compact JSON-serializable detect summary for endpoints / audits / CLI."""
    redundant = [m for c in clusters for m in c.redundant]
    return {
        "cluster_count": len(clusters),
        "duplicate_document_count": len(redundant),
        "redundant_chunk_count": sum(m.chunk_count for m in redundant),
        "by_confidence": {
            DUP_CERTAIN: sum(1 for m in redundant if m.confidence == DUP_CERTAIN),
            DUP_LIKELY: sum(1 for m in redundant if m.confidence == DUP_LIKELY),
            DUP_REVIEW: sum(1 for m in redundant if m.confidence == DUP_REVIEW),
        },
        "clusters": [c.to_dict() for c in clusters],
    }
