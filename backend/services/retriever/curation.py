"""Retrieval Layer v4 — deterministic final-packet curation (spec §2.5).

A pure, I/O-free selector that replaces the MMR black box in
``ranking_policy.select_with_diversity`` with an explicit, fully-traced
curation stage. The cross-encoder rerank score is the ONLY relevance input:
no grounding multipliers, heading penalties, or degree boosts touch the
ordering here (those score-mutation paths are the "scoring wall" of v4 P1,
already annotation-only upstream). Diversity / coverage / junk-filtering are
expressed as deterministic quotas and constraints, not as hidden score edits.

Stage order (mirrors CONTINUITY/RETRIEVAL_LAYER_SPEC.md §2.5):
  1. relative floor      drop score < top1 * FLOOR_RATIO, always keep best 4
  2. parent coalesce     best child per parent_id; packet score = max child
  3. near-dup            8-token shingle containment >= 0.60 drops lower score
  4. allocation          side-guarantee -> corpus reservation -> graph-bridge
                         -> score-desc fill with per-doc cap
  5. ordering            docs by best score desc; within doc, rerank order
  6. emit                Packet{items, diagnostics, packet_hash}

Shadow-gated by ``RETRIEVAL_CURATION_V4_ENABLED`` (default OFF) in the
orchestrator; the legacy ``select_with_diversity`` path stays the default
until the heldout/acceptance eval shows this path wins.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from models.schemas import RetrievalTier, SourceChunk
from services.retriever.intent_policy import RetrievalIntent
from services.retriever.ranking_policy import (
    _candidate_atoms,
    _chunk_text,
    _evaluate_sufficiency,
    _fingerprint,
    _is_graph_expansion,
    _token_set,
)
from services.retriever.reservation_policy import passes_corpus_reservation

# ── Tunable constants (spec §2.5) ─────────────────────────────────────────
# Relative relevance floor: a candidate scoring below this fraction of the
# pool's top cross-encoder score is junk. Only applied to bounded (0..1)
# score families — raw-logit pools have no ratio semantics, so the floor is
# skipped there (the keep-best-4 guarantee still bounds the packet).
FLOOR_RATIO = 0.35
# Graph-provenance evidence (Mode A/B expansion, fact seeds) is demoted by
# the text-similarity cross-encoder despite being genuinely useful, so it
# gets a relaxed floor — matching ranking_policy._GRAPH_GROUNDED_FLOOR_RATIO.
GRAPH_FLOOR_RATIO = 0.25
# Never strand the packet: keep at least this many top candidates regardless
# of the floor so a weak pool still yields context.
MIN_KEEP = 4
# Near-duplicate: 8-token shingles, drop the lower-scored candidate when
# containment >= this fraction.
SHINGLE_SIZE = 8
NEAR_DUP_CONTAINMENT = 0.60
# Per-document cap on the score-desc fill pass — no single book collapses the
# context window (the "6 of 9 chunks from one book" pathology).
PER_DOC_CAP = 3
# Tier-3 graph-bridge reservation: at most this many cap-exempt seats for
# graph-supported candidates that survived the floor.
GRAPH_BRIDGE_SEATS = 2


@dataclass(frozen=True)
class Packet:
    """Curated final packet. ``items`` are SourceChunks in packet order so the
    orchestrator can hydrate them exactly like the legacy path; ``diagnostics``
    carries the per-item score ledger + stage counts; ``packet_hash`` is the
    determinism fingerprint (sha1 of the ordered chunk ids)."""

    items: list[SourceChunk]
    diagnostics: dict[str, Any]
    packet_hash: str


@dataclass
class _Slot:
    """Internal bookkeeping for one seated candidate."""

    chunk: SourceChunk
    rerank_idx: int
    role: str
    doc_key: str
    events: list[str] = field(default_factory=list)


# ── Small deterministic helpers ───────────────────────────────────────────
def _doc_key(chunk: SourceChunk) -> str:
    metadata = chunk.metadata or {}
    return (
        str(chunk.doc_id or metadata.get("source_file_hash") or chunk.doc_name or "")
        .strip()
        .lower()
    )


def _tokens(chunk: SourceChunk) -> set[str]:
    return _token_set(_chunk_text(chunk))


def _shingles(tokens: set[str]) -> set[tuple[str, ...]]:
    """8-token shingles over the sorted token set. Sorted (not positional) so
    the result is deterministic regardless of upstream token ordering."""

    ordered = sorted(tokens)
    if len(ordered) < SHINGLE_SIZE:
        return {tuple(ordered)} if ordered else set()
    return {
        tuple(ordered[i : i + SHINGLE_SIZE])
        for i in range(len(ordered) - SHINGLE_SIZE + 1)
    }


def _is_graph_supported(chunk: SourceChunk) -> bool:
    if _is_graph_expansion(chunk):
        return True
    if (chunk.source_tier or "").lower() == "graph_fact_seed":
        return True
    for item in chunk.provenance or []:
        if not isinstance(item, dict):
            continue
        retriever = str(item.get("retriever") or "")
        if "neo4j" in retriever or "graph" in retriever:
            return True
    return False


def _side_keys(chunk: SourceChunk) -> list[str]:
    """Query 'sides' a chunk speaks to, derived from the annotation-only
    query_grounding metadata stamped upstream (v4 P1). Deterministic order."""

    grounding = (chunk.metadata or {}).get("query_grounding")
    if not isinstance(grounding, dict):
        return []
    keys: list[str] = []
    for item in grounding.get("matched") or []:
        key = str(item).strip().lower()
        if key and key not in keys:
            keys.append(key)
    return keys


# ── Stage 1: relative floor ───────────────────────────────────────────────
def _apply_floor(
    ranked: list[SourceChunk], *, floor_ratio: float
) -> tuple[list[SourceChunk], dict[str, Any]]:
    if not ranked:
        return [], {"kept": 0, "dropped": 0, "bounded": False}
    top = float(ranked[0].score or 0.0)
    bounded = 0.0 <= top <= 1.0 and top > 0.0
    kept: list[SourceChunk] = []
    dropped = 0
    for idx, chunk in enumerate(ranked):
        if idx < MIN_KEEP:
            kept.append(chunk)
            continue
        if not bounded:
            kept.append(chunk)
            continue
        ratio = GRAPH_FLOOR_RATIO if _is_graph_supported(chunk) else floor_ratio
        if float(chunk.score or 0.0) >= top * ratio:
            kept.append(chunk)
        else:
            dropped += 1
    return kept, {
        "kept": len(kept),
        "dropped": dropped,
        "bounded": bounded,
        "top_score": round(top, 4),
    }


# ── Stage 2: parent coalesce ──────────────────────────────────────────────
def _coalesce_parents(
    chunks: list[SourceChunk],
) -> tuple[list[SourceChunk], dict[str, Any]]:
    """Best child per parent_id wins; its packet score is the max of all member
    children and ``sibling_hits`` records how many siblings it coalesced."""

    groups: dict[str, list[SourceChunk]] = {}
    order: list[str] = []
    for chunk in chunks:
        key = str(chunk.parent_id or chunk.chunk_id or id(chunk))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(chunk)

    coalesced: list[SourceChunk] = []
    sibling_total = 0
    for key in order:
        members = groups[key]
        best = max(members, key=lambda c: float(c.score or 0.0))
        if len(members) > 1:
            best_score = max(float(c.score or 0.0) for c in members)
            best = best.model_copy()
            best.score = best_score
            metadata = dict(best.metadata or {})
            metadata["curation_sibling_hits"] = len(members) - 1
            best.metadata = metadata
            sibling_total += len(members) - 1
        coalesced.append(best)
    coalesced.sort(key=lambda c: float(c.score or 0.0), reverse=True)
    return coalesced, {
        "groups": len(order),
        "sibling_hits": sibling_total,
        "dropped": len(chunks) - len(coalesced),
    }


# ── Stage 3: near-duplicate drop ──────────────────────────────────────────
def _drop_near_duplicates(
    chunks: list[SourceChunk],
) -> tuple[list[SourceChunk], dict[str, Any]]:
    shingle_sets = [_shingles(_tokens(chunk)) for chunk in chunks]
    drop_idx: set[int] = set()
    events: dict[int, str] = {}
    for i in range(len(chunks)):
        if i in drop_idx or not shingle_sets[i]:
            continue
        for j in range(i + 1, len(chunks)):
            if j in drop_idx or not shingle_sets[j]:
                continue
            inter = len(shingle_sets[i] & shingle_sets[j])
            if inter == 0:
                continue
            contain_ij = inter / len(shingle_sets[i])
            contain_ji = inter / len(shingle_sets[j])
            if max(contain_ij, contain_ji) < NEAR_DUP_CONTAINMENT:
                continue
            si = float(chunks[i].score or 0.0)
            sj = float(chunks[j].score or 0.0)
            # Drop the lower-scored; on a tie drop the later rerank position.
            loser = j if sj <= si else i
            winner = i if loser == j else j
            drop_idx.add(loser)
            events[loser] = str(chunks[winner].chunk_id or winner)
            if loser == i:
                break
    kept = [c for idx, c in enumerate(chunks) if idx not in drop_idx]
    return kept, {"dropped": len(drop_idx), "dropped_against": events}


# ── Stage 4: quota allocation ─────────────────────────────────────────────
def _allocate(
    ranked: list[SourceChunk],
    *,
    query: str,
    tier: RetrievalTier,
    final_top_k: int,
    multi_corpus: bool,
    corpus_ids: list[str],
    top_score: float,
) -> tuple[list[_Slot], dict[str, Any]]:
    target = max(1, int(final_top_k))
    fps = [_fingerprint(chunk) for chunk in ranked]
    slots: list[_Slot] = []
    seated: set[int] = set()
    doc_counts: dict[str, int] = {}
    corpus_seated: dict[str, int] = {}

    def seat(idx: int, role: str, *, cap_exempt: bool = False) -> bool:
        if idx in seated:
            return False
        chunk = ranked[idx]
        dk = _doc_key(chunk)
        if not cap_exempt and doc_counts.get(dk, 0) >= PER_DOC_CAP:
            return False
        slots.append(
            _Slot(
                chunk=chunk,
                rerank_idx=idx,
                role=role,
                doc_key=dk,
                events=[f"seated_by:{role}"],
            )
        )
        seated.add(idx)
        doc_counts[dk] = doc_counts.get(dk, 0) + 1
        cid = str(chunk.corpus_id or "")
        if cid:
            corpus_seated[cid] = corpus_seated.get(cid, 0) + 1
        return True

    # 4a. Side-guarantee — one cap-exempt seat per query side, taken from that
    # side's best distinct document. Preserves multi-book/comparison coverage.
    side_best_idx: dict[str, int] = {}
    for idx, chunk in enumerate(ranked):
        for side in _side_keys(chunk):
            if side not in side_best_idx:
                side_best_idx[side] = idx
    side_seats = 0
    for side, idx in side_best_idx.items():
        if seat(idx, f"side_guarantee:{side}", cap_exempt=True):
            side_seats += 1

    # 4b. Corpus reservation — multi-corpus queries guarantee each requested
    # corpus a seat iff it has a candidate passing the shared calibrated gate.
    corpus_seats = 0
    if multi_corpus:
        for cid in corpus_ids:
            if not cid or corpus_seated.get(cid, 0) > 0:
                continue
            best_idx = next(
                (
                    idx
                    for idx, chunk in enumerate(ranked)
                    if str(chunk.corpus_id or "") == cid
                    and passes_corpus_reservation(
                        float(chunk.score or 0.0), top_score
                    )
                ),
                None,
            )
            if best_idx is not None and seat(best_idx, "corpus_reservation", cap_exempt=True):
                corpus_seats += 1

    # 4c. Graph-bridge — Tier-3 reserves up to 2 cap-exempt seats for graph
    # evidence that survived the floor but lost the score-desc fill.
    graph_seats = 0
    if tier == RetrievalTier.qdrant_mongo_graph:
        for idx, chunk in enumerate(ranked):
            if graph_seats >= GRAPH_BRIDGE_SEATS:
                break
            if idx in seated or not fps[idx]["graph_supported"]:
                continue
            if seat(idx, "graph_bridge", cap_exempt=True):
                graph_seats += 1

    # 4d. Score-desc fill with per-doc cap (ranked is already rerank order).
    fill_seats = 0
    for idx in range(len(ranked)):
        if len(slots) >= target:
            break
        if seat(idx, "fill"):
            fill_seats += 1

    # Trim to the packet target, protecting guaranteed seats.
    if len(slots) > target:
        protected = [s for s in slots if s.role != "fill"]
        fill = [s for s in slots if s.role == "fill"]
        slots = protected + fill
        slots = slots[:target]

    return slots, {
        "side_seats": side_seats,
        "corpus_seats": corpus_seats,
        "graph_seats": graph_seats,
        "fill_seats": fill_seats,
        "doc_counts": doc_counts,
        "corpus_counts": dict(corpus_seated),
    }


# ── Stage 5 + 6: ordering and emit ────────────────────────────────────────
def build_packet(
    ranked: list[SourceChunk],
    *,
    query: str,
    intent: RetrievalIntent,
    tier: RetrievalTier,
    final_top_k: int,
    multi_corpus: bool = False,
    corpus_ids: list[str] | None = None,
    floor_ratio: float = FLOOR_RATIO,
) -> Packet:
    """Curate the final packet from reranked candidates (highest score first).

    Pure and deterministic: the same input yields the same ``packet_hash``.
    No I/O. ``intent`` is accepted for signature parity with the legacy
    selector and future per-intent quota tuning; the current quotas are
    intent-independent by design (deterministic, not adaptive).
    """

    corpus_ids = list(corpus_ids or [])
    if not ranked:
        return Packet(items=[], diagnostics={"empty": True}, packet_hash="")

    # Defensive: the orchestrator passes rerank order, but sort to guarantee
    # the score-desc contract with a total-order tie-break for determinism.
    ordered = sorted(
        ranked,
        key=lambda c: (
            -float(c.score or 0.0),
            str(c.corpus_id or ""),
            str(c.doc_id or ""),
            str(c.chunk_id or ""),
        ),
    )
    top_score = float(ordered[0].score or 0.0)

    floored, floor_meta = _apply_floor(ordered, floor_ratio=floor_ratio)
    coalesced, coalesce_meta = _coalesce_parents(floored)
    deduped, dedup_meta = _drop_near_duplicates(coalesced)
    slots, alloc_meta = _allocate(
        deduped,
        query=query,
        tier=tier,
        final_top_k=final_top_k,
        multi_corpus=multi_corpus,
        corpus_ids=corpus_ids,
        top_score=top_score,
    )

    # Ordering: documents by best (lowest) rerank position; within a document,
    # rerank order. Coherent reading order for the model.
    doc_best_pos: dict[str, int] = {}
    for slot in slots:
        pos = slot.rerank_idx
        if slot.doc_key not in doc_best_pos or pos < doc_best_pos[slot.doc_key]:
            doc_best_pos[slot.doc_key] = pos
    slots.sort(key=lambda s: (doc_best_pos[s.doc_key], s.rerank_idx))

    items = [slot.chunk for slot in slots]

    # Per-item score ledger (spec §2.5 step 8) for trace comparison.
    ledgers = [
        {
            "chunk_id": str(slot.chunk.chunk_id or ""),
            "doc": slot.doc_key,
            "p": round(float(slot.chunk.score or 0.0), 4),
            "role": slot.role,
            "rerank_idx": slot.rerank_idx,
            "curation_events": list(slot.events),
        }
        for slot in slots
    ]

    selected_indices = [slot.rerank_idx for slot in slots]
    fingerprints = [_fingerprint(chunk) for chunk in deduped]
    sufficiency = _evaluate_sufficiency(
        query=query,
        selected_indices=selected_indices,
        fingerprints=fingerprints,
    )

    diagnostics: dict[str, Any] = {
        "stage": "curation_v4",
        "input_count": len(ranked),
        "floor": floor_meta,
        "coalesce": coalesce_meta,
        "near_dup": {
            "dropped": dedup_meta["dropped"],
        },
        "allocation": alloc_meta,
        "final_count": len(items),
        "distinct_docs": len(doc_best_pos),
        "corpus_counts": alloc_meta["corpus_counts"],
        "sufficiency": sufficiency,
        "ledger": ledgers,
    }

    packet_hash = hashlib.sha1(
        "|".join(str(c.chunk_id or "") for c in items).encode("utf-8")
    ).hexdigest()

    return Packet(items=items, diagnostics=diagnostics, packet_hash=packet_hash)
