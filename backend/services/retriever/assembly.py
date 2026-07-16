"""W2 — §10.3 waterfall assembly wiring (behind WATERFALL_ASSEMBLY, default OFF).

PURE WIRING around the owner-designed allocator (waterfall.py): the retriever's
FINAL ranked chunks are grouped into parent candidates (parent.score = max
child score, rank = first appearance), parents are hydrated from Mongo in ONE
$in read, unplaced fragments become orphan children, graph provenance becomes
shared-entity lines, and allocate() packs the deterministic budgeted packet
(full → summary → skip; anchor lanes when TWO_LANE_ANCHORING is on).

The packet rides RetrievalResult.packet as a plain dict; packet_hash lands in
diagnostics on every flagged response (the determinism receipt). Legacy
assembly is untouched when the flag is off — this module has zero effect.

group_parent_candidates / entity_lines_from_chunks are pure (tested without
Mongo); build_waterfall_packet is the thin async I/O wrapper.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Sequence

from services.retriever.waterfall import (
    DocNote,
    OrphanChild,
    Packet,
    ParentCandidate,
    SharedEntity,
    allocate,
)
from services.ingestion.doc_artifact import format_source_role_header
from services.storage.record_status import with_active_records

logger = logging.getLogger(__name__)

_MAX_ENTITY_LINES = 12


def group_parent_candidates(
    chunks: Sequence[Any],
    parent_map: dict[tuple[str, str, str], dict],
    anchor_doc_ids: Iterable[str] = (),
) -> tuple[list[ParentCandidate], list[OrphanChild]]:
    """Final-ranked chunks → rank-ordered parents + orphan children.

    `chunks` MUST already be in final rank order (desc score) — parent rank is
    first appearance, and parent.score = first child's score (which IS the max
    in a desc-ordered list). A chunk whose parent isn't in `parent_map` (or has
    no parent_id) degrades to an orphan child — the cross-domain fragment lane.
    """
    anchors = set(anchor_doc_ids or ())
    parents: list[ParentCandidate] = []
    orphans: list[OrphanChild] = []
    seen: set[tuple[str, str, str]] = set()
    for c in chunks:
        doc_id = str(getattr(c, "doc_id", "") or "")
        corpus_id = str(getattr(c, "corpus_id", "") or "")
        parent_id = str(getattr(c, "parent_id", "") or "")
        key = (corpus_id, doc_id, parent_id)
        row = parent_map.get(key) if parent_id else None
        if row is None:
            text = str(getattr(c, "text", "") or "")
            if text:
                orphans.append(
                    OrphanChild(
                        chunk_id=str(getattr(c, "chunk_id", "") or ""),
                        parent_id=parent_id,
                        doc_id=doc_id,
                        score=float(getattr(c, "score", 0.0) or 0.0),
                        text=text,
                    )
                )
            continue
        if key in seen:
            continue
        seen.add(key)
        parents.append(
            ParentCandidate(
                parent_id=parent_id,
                doc_id=doc_id,
                score=float(getattr(c, "score", 0.0) or 0.0),
                full_text=str(row.get("text") or ""),
                summary=str(row.get("summary") or ""),
                lane="anchor" if doc_id in anchors else "",
            )
        )
    return parents, orphans


def entity_lines_from_chunks(
    chunks: Sequence[Any], cap: int = _MAX_ENTITY_LINES
) -> list[SharedEntity]:
    """Graph provenance → deduped one-line entity signals (cheapest rung).

    Deterministic: chunk rank order, then provenance order; first mention wins.
    """
    out: list[SharedEntity] = []
    seen: set[str] = set()
    for c in chunks:
        for pv in getattr(c, "provenance", None) or []:
            name = str((pv or {}).get("entity") or "").strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            family = str((pv or {}).get("relation_family") or "").strip()
            predicate = str((pv or {}).get("predicate") or "").strip()
            defn = str((pv or {}).get("definitional_phrase") or "").strip()
            line = name
            if predicate:
                line += f" — {predicate}"
            elif family:
                line += f" [{family}]"
            if defn:
                line += f": {defn}"
            out.append(SharedEntity(entity_id=name.lower(), text=line))
            if len(out) >= cap:
                return out
    return out


def doc_notes_from_chunks(chunks: Sequence[Any], cap: int = 12) -> list[DocNote]:
    """Final-ranked chunks -> one passive source-role note per distinct doc."""
    out: list[DocNote] = []
    seen: set[str] = set()
    for chunk in chunks:
        doc_id = str(getattr(chunk, "doc_id", "") or "")
        if not doc_id or doc_id in seen:
            continue
        metadata = getattr(chunk, "metadata", None) or {}
        artifact = metadata.get("doc_artifact") if isinstance(metadata, dict) else None
        if not isinstance(artifact, dict):
            continue
        label = str(getattr(chunk, "doc_name", "") or doc_id)
        note = format_source_role_header(label, artifact)
        if not note:
            continue
        out.append(DocNote(doc_id=doc_id, text=note))
        seen.add(doc_id)
        if len(out) >= cap:
            break
    return out


async def build_waterfall_packet(
    chunks: Sequence[Any],
    corpus_ids: Optional[list[str]],
    *,
    query: str,
    settings: Any,
) -> Optional[Packet]:
    """ONE parent_chunks $in read (+ optional documents read for anchor
    detection) → allocate() → Packet. Best-effort: any failure returns None
    and the caller keeps the legacy path. Never raises."""
    try:
        from services.ingestion_service import ingestion_service

        db = getattr(ingestion_service, "db", None)
        if db is None or not chunks:
            return None

        keys = {
            (
                str(getattr(c, "corpus_id", "") or ""),
                str(getattr(c, "doc_id", "") or ""),
                str(getattr(c, "parent_id", "") or ""),
            )
            for c in chunks
            if getattr(c, "parent_id", None)
        }
        parent_ids = sorted({pid for _, _, pid in keys})
        parent_map: dict[tuple[str, str, str], dict] = {}
        if parent_ids:
            q: dict[str, Any] = {"parent_id": {"$in": parent_ids}}
            if corpus_ids:
                q["corpus_id"] = {"$in": list(corpus_ids)}
            async for row in db["parent_chunks"].find(
                with_active_records(q),
                {
                    "_id": 0,
                    "corpus_id": 1,
                    "parent_id": 1,
                    "doc_id": 1,
                    "text": 1,
                    "summary": 1,
                },
            ):
                parent_map[
                    (
                        str(row.get("corpus_id") or ""),
                        str(row.get("doc_id") or ""),
                        str(row.get("parent_id") or ""),
                    )
                ] = row

        anchor_doc_ids: list[str] = []
        if bool(getattr(settings, "TWO_LANE_ANCHORING", False)):
            try:
                from services.retriever.anchor_detect import detect_anchor_doc_ids

                doc_ids = sorted(
                    {str(getattr(c, "doc_id", "") or "") for c in chunks} - {""}
                )
                document_query: dict[str, Any] = {"doc_id": {"$in": doc_ids}}
                if corpus_ids:
                    document_query["corpus_id"] = {"$in": list(corpus_ids)}
                docs = (
                    await db["documents"]
                    .find(
                        with_active_records(document_query),
                        {
                            "_id": 0,
                            "corpus_id": 1,
                            "doc_id": 1,
                            "title": 1,
                            "author": 1,
                        },
                    )
                    .to_list(length=None)
                )
                anchor_doc_ids = detect_anchor_doc_ids(query, docs)
            except Exception as exc:  # noqa: BLE001
                logger.warning("anchor detection skipped: %s", exc)

        parents, orphans = group_parent_candidates(chunks, parent_map, anchor_doc_ids)
        entities = entity_lines_from_chunks(chunks)
        doc_notes = doc_notes_from_chunks(chunks)
        packet = allocate(
            parents,
            budget_tokens=int(getattr(settings, "WATERFALL_BUDGET_TOKENS", 4000)),
            orphans=orphans,
            entities=entities,
            doc_notes=doc_notes,
            anchor_quota=0.6,
        )
        packet.diagnostics["parents_in"] = len(parents)
        packet.diagnostics["orphans_in"] = len(orphans)
        packet.diagnostics["entities_in"] = len(entities)
        packet.diagnostics["doc_notes_in"] = len(doc_notes)
        packet.diagnostics["anchor_doc_ids"] = anchor_doc_ids
        return packet
    except Exception as exc:  # noqa: BLE001
        logger.warning("waterfall assembly failed (legacy path kept): %s", exc)
        return None


def packet_to_dict(packet: Packet) -> dict[str, Any]:
    """Plain-dict projection that rides RetrievalResult.packet (pydantic-safe,
    deep-copy friendly for the retrieval cache)."""
    return {
        "packet_hash": packet.packet_hash,
        "budget_tokens": packet.budget_tokens,
        "used_tokens": packet.used_tokens,
        "items": [
            {
                "kind": it.kind,
                "ref_id": it.ref_id,
                "doc_id": it.doc_id,
                "lane": it.lane,
                "tokens": it.tokens,
                "text": it.text,
            }
            for it in packet.items
        ],
        "diagnostics": dict(packet.diagnostics),
    }
