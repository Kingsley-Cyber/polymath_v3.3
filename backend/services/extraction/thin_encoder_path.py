"""Thin encoder extraction path — one encoder, plain Python, deterministic.

Owner-ordered 2026-08-12, after measurement killed the alternatives:

  measured, same corpus, warm process
    encoder work for 10 chunks .................  0.16s  (0.05% of wall time)
    Graphify's 16 Python stages for those chunks  352.8s (99.95%)
    -> Graphify: 0.03 chunks/s   = 113 days for this corpus
    -> LLM path: 1.4 chunks/s    = ~2 days, non-reproducible
    -> raw encoder: 12.4 windows/s on ONE Mac MPS sidecar
                                 = ~66 min for the whole corpus

The encoder was never the bottleneck. This module is the path the owner
actually asked for: window the text, call the encoder once per window,
attribute results back to child chunks BY CHARACTER OFFSET, validate, emit.

Determinism, by construction rather than by hope:
  - children are consumed in document order; windows pack deterministically
  - the label vocabularies sent to the encoder ARE the frozen schema
    vocabularies, so every returned label is schema-valid without a
    post-hoc repair step (the encoder equivalent of constrained decoding)
  - fixed thresholds, no sampling, no temperature, no batch-composition
    dependence -> same input, same bytes out
  - attribution is exact offset containment, never substring search, so it
    is both O(n) and immune to duplicate text across chunks

Cross-chunk relations — subject in one child, object in another — are
capturable here for the first time: the encoder sees the whole window. They
are attributed to the SUBJECT's chunk, which is where a reader would look
for the claim.

Output contract is unchanged: one ExtractionResult per child chunk. Job
identity, contract hashes, promotion, Neo4j writes and retrieval provenance
all key on child chunk_id and are untouched (see the 2026-08-12 blast-radius
audit, findings 1-38).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# Window budget in CHARACTERS. The census uses a 512-token target / 1024 max;
# ~4 chars per token puts 1800 comfortably inside the encoder's context while
# still packing ~20 of this corpus's median 86-token chunks per call.
DEFAULT_WINDOW_CHARS = 1800
_JOIN = "\n"
_EVIDENCE_MAX_CHARS = 160


def window_chars() -> int:
    try:
        return max(200, int(os.environ.get("THIN_WINDOW_CHARS", "") or DEFAULT_WINDOW_CHARS))
    except ValueError:
        return DEFAULT_WINDOW_CHARS


def thresholds() -> tuple[float, float]:
    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, "") or default)
        except ValueError:
            return default

    return _f("THIN_ENTITY_THRESHOLD", 0.5), _f("THIN_RELATION_THRESHOLD", 0.5)


@dataclass(frozen=True)
class Placement:
    """Where one child chunk's text sits inside a window, in window coords."""

    chunk_id: str
    doc_id: str
    corpus_id: str
    start: int
    end: int  # exclusive


@dataclass(frozen=True)
class Window:
    text: str
    placements: tuple[Placement, ...]


def build_windows(children: Sequence[Any], *, max_chars: int | None = None) -> list[Window]:
    """Pack children into windows without ever splitting a child.

    A child larger than the budget gets a window to itself rather than being
    cut, so no chunk is silently half-extracted.
    """
    budget = max_chars or window_chars()
    windows: list[Window] = []
    buf: list[str] = []
    placements: list[Placement] = []
    cursor = 0

    def _flush() -> None:
        nonlocal buf, placements, cursor
        if placements:
            windows.append(Window(text=_JOIN.join(buf), placements=tuple(placements)))
        buf, placements, cursor = [], [], 0

    for child in children:
        text = str(getattr(child, "text", "") or "")
        if not text.strip():
            continue
        addition = len(text) + (len(_JOIN) if buf else 0)
        if buf and cursor + addition > budget:
            _flush()
            addition = len(text)
        start = cursor + (len(_JOIN) if buf else 0)
        placements.append(
            Placement(
                chunk_id=str(getattr(child, "chunk_id", "")),
                doc_id=str(getattr(child, "doc_id", "")),
                corpus_id=str(getattr(child, "corpus_id", "")),
                start=start,
                end=start + len(text),
            )
        )
        buf.append(text)
        cursor = start + len(text)
    _flush()
    return windows


def owner_of(placements: Sequence[Placement], start: int) -> Placement | None:
    """Placement containing a character offset. Exact, not a text search."""
    lo, hi = 0, len(placements) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        p = placements[mid]
        if start < p.start:
            hi = mid - 1
        elif start >= p.end:
            lo = mid + 1
        else:
            return p
    return placements[0] if placements else None


def canonical_name(surface: str) -> str:
    """lowercase, punctuation stripped, whitespace collapsed — matches the
    convention the graph already stores so entities merge across paths."""
    cleaned = re.sub(r"[^\w\s-]", " ", str(surface or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _evidence(window_text: str, start: int, end: int) -> str:
    span = window_text[max(0, start):min(len(window_text), end)]
    span = re.sub(r"\s+", " ", span).strip()
    return span[:_EVIDENCE_MAX_CHARS]


def schema_vocabularies() -> tuple[list[str], list[str]]:
    """The label sets sent to the encoder ARE the frozen schema vocabularies.

    This is the encoder's equivalent of constrained decoding: a zero-shot
    span labeller can only return labels it was given, so schema validity is
    structural rather than validated after the fact.
    """
    from typing import get_args

    from services.ghost_b_schemas import LLMEntity, LLMRelation

    return (
        list(get_args(LLMEntity.model_fields["entity_type"].annotation)),
        list(get_args(LLMRelation.model_fields["predicate"].annotation)),
    )


def results_for_window(
    window: Window,
    relex_result: Any,
    *,
    entity_cls: Any,
    relation_cls: Any,
    entity_threshold: float,
    relation_threshold: float,
) -> dict[str, dict[str, list[Any]]]:
    """Attribute one window's encoder output back to its child chunks."""
    buckets: dict[str, dict[str, list[Any]]] = {
        p.chunk_id: {"entities": [], "relations": []} for p in window.placements
    }

    for span in list(getattr(relex_result, "entities", None) or []):
        try:
            score = float(span.get("score", 0.0))
            start, end = int(span["start"]), int(span["end"])
            surface, label = str(span.get("text") or ""), str(span.get("label") or "")
        except (KeyError, TypeError, ValueError):
            continue
        if score < entity_threshold or not surface.strip():
            continue
        owner = owner_of(window.placements, start)
        if owner is None:
            continue
        buckets[owner.chunk_id]["entities"].append(
            entity_cls(
                canonical_name=canonical_name(surface),
                surface_form=surface,
                entity_type=label,
                confidence=round(score, 4),
            )
        )

    for rel in list(getattr(relex_result, "relations", None) or []):
        score = float(getattr(rel, "score", 0.0) or 0.0)
        if score < relation_threshold:
            continue
        head_start = int(getattr(rel, "head_start", 0) or 0)
        head_text = str(getattr(rel, "head_text", "") or "")
        tail_text = str(getattr(rel, "tail_text", "") or "")
        predicate = str(getattr(rel, "label", "") or "")
        if not (head_text.strip() and tail_text.strip() and predicate):
            continue
        # The subject's chunk owns the claim — including when the object
        # lives in a sibling chunk, which the per-child path could never see.
        owner = owner_of(window.placements, head_start)
        if owner is None:
            continue
        # Evidence must be a span the READER can verify inside the owning
        # chunk. Spanning head->tail unclipped swallowed everything between
        # them when subject and object sat in different children, producing
        # "evidence" that quoted unrelated text (audit finding 2026-08-12).
        tail_start = int(getattr(rel, "tail_start", head_start) or head_start)
        span_start = min(head_start, tail_start)
        span_end = max(
            int(getattr(rel, "head_end", head_start) or head_start),
            int(getattr(rel, "tail_end", head_start) or head_start),
        )
        evidence = _evidence(
            window.text,
            max(span_start, owner.start),
            min(span_end, owner.end),
        )
        if not evidence:
            # cross-chunk pair: quote the subject's own sentence rather than
            # a span the owning chunk does not contain
            evidence = _evidence(window.text, head_start, min(owner.end, span_end))
        buckets[owner.chunk_id]["relations"].append(
            relation_cls(
                subject=canonical_name(head_text),
                predicate=predicate,
                object=canonical_name(tail_text),
                object_kind="entity",
                confidence=round(score, 4),
                evidence_phrase=evidence,
            )
        )
    return buckets


def _dedupe(items: Iterable[Any], key: Any) -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for item in items:
        k = key(item)
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out


def assemble_results(
    children: Sequence[Any],
    per_window: Sequence[dict[str, dict[str, list[Any]]]],
    *,
    result_cls: Any,
    schema_version: str = "polymath.extract.v2",
) -> list[Any]:
    """One ExtractionResult per child, in document order, deduped."""
    merged: dict[str, dict[str, list[Any]]] = {}
    for bucket in per_window:
        for chunk_id, payload in bucket.items():
            slot = merged.setdefault(chunk_id, {"entities": [], "relations": []})
            slot["entities"].extend(payload["entities"])
            slot["relations"].extend(payload["relations"])

    results: list[Any] = []
    for child in children:
        chunk_id = str(getattr(child, "chunk_id", ""))
        payload = merged.get(chunk_id) or {"entities": [], "relations": []}
        entities = _dedupe(
            payload["entities"],
            lambda e: (e.canonical_name, e.entity_type),
        )
        relations = _dedupe(
            payload["relations"],
            lambda r: (r.subject, r.predicate, r.object),
        )
        results.append(
            result_cls(
                schema_version=schema_version,
                chunk_id=chunk_id,
                doc_id=str(getattr(child, "doc_id", "")),
                corpus_id=str(getattr(child, "corpus_id", "")),
                entities=entities,
                relations=relations,
                facts=[],
                text=str(getattr(child, "text", "") or ""),
            )
        )
    return results


async def run_thin_extraction(
    children: Sequence[Any],
    *,
    entity_labels: Sequence[str] | None = None,
    relation_labels: Sequence[str] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Window -> encoder -> offset attribution -> per-child results.

    Returns (results, metrics). Raises RelexSidecarError if the encoder is
    unreachable — fail closed, never silently emit empty artifacts.
    """
    import asyncio
    import time

    from services.extraction import relex_sidecar_client as relex
    from services.ghost_b import EntityItem, ExtractionResult, RelationItem

    # Per-corpus vocabularies when the corpus defines them, else the frozen
    # global schema. Measured 2026-08-12: concrete domain labels do not just
    # type better, they stop the encoder proposing junk spans entirely —
    # "Programming Language" returns Python at 1.00 where the abstract
    # 15-class set returned "In this chapter" as an entity.
    default_entities, default_relations = schema_vocabularies()
    entity_labels = [str(x) for x in (entity_labels or default_entities) if str(x).strip()]
    relation_labels = [
        str(x) for x in (relation_labels or default_relations) if str(x).strip()
    ]
    entity_threshold, relation_threshold = thresholds()
    windows = build_windows(children)
    if not windows:
        return [], {"engine": "thin_encoder", "windows": 0, "children": len(children)}

    started = time.perf_counter()
    pool = relex.sidecar_pool()

    def _infer(batch: list[Window], base_url: str | None) -> list[Any]:
        return relex.infer(
            [w.text for w in batch],
            entity_labels=entity_labels,
            relation_labels=relation_labels,
            entity_threshold=entity_threshold,
            relation_threshold=relation_threshold,
            base_url=base_url,
        )

    # Fan windows across replicas; each replica serializes internally, so
    # parallelism across replicas is the only parallelism that helps.
    replicas = list(pool) or [None]
    # Shard by original index so results can be mapped back positionally
    # without any order assumptions beyond "infer preserves input order".
    shards: list[list[tuple[int, Window]]] = [[] for _ in replicas]
    for idx, win in enumerate(windows):
        shards[idx % len(replicas)].append((idx, win))
    active = [(replicas[i], shard) for i, shard in enumerate(shards) if shard]

    outputs: list[list[Any]] = await asyncio.gather(
        *[
            asyncio.to_thread(_infer, [w for _idx, w in shard], base_url)
            for base_url, shard in active
        ]
    )

    by_index: dict[int, Any] = {}
    for (_base_url, shard), shard_out in zip(active, outputs):
        for (idx, _win), relex_result in zip(shard, shard_out):
            by_index[idx] = relex_result

    per_window: list[dict[str, dict[str, list[Any]]]] = []
    for idx, win in enumerate(windows):
        relex_result = by_index.get(idx)
        if relex_result is None:
            continue
        per_window.append(
            results_for_window(
                win,
                relex_result,
                entity_cls=EntityItem,
                relation_cls=RelationItem,
                entity_threshold=entity_threshold,
                relation_threshold=relation_threshold,
            )
        )

    results = assemble_results(children, per_window, result_cls=ExtractionResult)
    release = _encoder_release(replicas)
    elapsed = time.perf_counter() - started
    metrics = {
        "engine": "thin_encoder",
        "model": release.get("model", ""),
        "provider": "relex_sidecar",
        "output_mode": "span_labeling",
        "release": release.get("release", ""),
        "device": release.get("device", ""),
        "entity_labels": len(entity_labels),
        "relation_labels": len(relation_labels),
        "windows": len(windows),
        "children": len(children),
        "replicas": len([s for s in shards if s]),
        "elapsed_seconds": round(elapsed, 3),
        "chunks_per_second": round(len(children) / elapsed, 2) if elapsed else 0.0,
        "entities": sum(len(r.entities) for r in results),
        "relations": sum(len(r.relations) for r in results),
    }
    return results, metrics


def _encoder_release(replicas: Sequence[Any]) -> dict[str, str]:
    """Identity of the encoder that produced this batch, for the ledger.

    Read from a replica's /health so the recorded provenance is what actually
    served the request, not a constant compiled into the caller.
    """
    import json
    import urllib.request

    for base in replicas:
        if not base:
            continue
        try:
            with urllib.request.urlopen(f"{str(base).rstrip('/')}/health", timeout=3) as resp:
                body = json.loads(resp.read())
            return {
                "model": str(body.get("model") or body.get("model_id") or ""),
                "release": str(body.get("release") or ""),
                "device": str(body.get("device") or ""),
            }
        except Exception:  # noqa: BLE001
            continue
    return {"model": "", "release": "", "device": ""}
