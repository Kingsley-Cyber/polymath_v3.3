"""facet_tagger.py — GLiNER pass-2: object_kind facet labelling (local Ghost B).

A second GLiNER pass over the SAME model used for entity tagging, this time with
the open facet vocabulary (pipeline_config.GHOST_B_FACET_VOCAB) as the zero-shot
labels. It refines an entity's coarse entity_type ("Software") into a fine
object_kind ("vector_database"), which downstream graph_backfill stores on the
Entity node and uses for taxonomy matching.

Deduped: GLiNER runs once per unique canonical_name across the whole doc, not
once per occurrence — the facet of an entity does not change between chunks.

Conservative by design: object_kind is set ONLY when a returned facet span
matches the entity's own surface form (exact, then containment). When GLiNER
finds no facet for the entity, object_kind is left "" so the downstream
neo4j_writer.resolve_ontology_metadata taxonomy pass can still fill it from
result.text — assigning a coarse fallback here would shadow that refinement.

No SLM, no network. The GLiNER model is a lazy module-level singleton shared
with the pass-1 entity tagger (ghost_b_local imports get_gliner() from here) so
the ~500 MB of weights load exactly once. Inference is a forward pass + threshold
(no sampling), so it is reproducible on a given machine.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# How much of the entity's first-occurrence chunk to feed the facet pass.
# Chunks are ~400-600 chars; the cap guards against an oversized parent chunk.
_CONTEXT_CHARS = 1000

_PC: Any = None       # cached pipeline_config module
_MODEL: Any = None     # cached GLiNER singleton (shared with pass-1)


# --------------------------------------------------------------- config load
def _pc() -> Any:
    """Lazily import local_ghost_b/pipeline_config (single source of truth for
    the facet vocab + thresholds). Resolved via LOCAL_GHOST_B_DIR if set, else
    relative to the repo root. Cached after first load."""
    global _PC
    if _PC is not None:
        return _PC
    try:
        import pipeline_config as pc  # already importable (path set by caller)
        _PC = pc
        return pc
    except ImportError:
        pass
    candidates = []
    env_dir = os.environ.get("LOCAL_GHOST_B_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    # backend/services/ingestion/facet_tagger.py -> parents[3] == repo root
    candidates.append(Path(__file__).resolve().parents[3] / "local_ghost_b")
    for d in candidates:
        if (d / "pipeline_config.py").exists():
            sys.path.insert(0, str(d))
            import pipeline_config as pc
            _PC = pc
            return pc
    raise ImportError(
        "facet_tagger: cannot locate local_ghost_b/pipeline_config.py — set "
        "LOCAL_GHOST_B_DIR to the local_ghost_b directory."
    )


# ---------------------------------------------------------------- the model
def get_gliner() -> Any:
    """Lazy, cached GLiNER model on MPS (Metal) when available, else CPU.

    This is the ONE GLiNER instance for the local Ghost B lane — both the
    pass-1 entity tagger (ghost_b_local) and the pass-2 facet tagger call this,
    so the weights load once. Falls back to CPU if MPS placement raises."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    pc = _pc()
    from gliner import GLiNER
    import torch

    logger.info("facet_tagger: loading GLiNER %s ...", pc.GLINER_MODEL)
    model = GLiNER.from_pretrained(pc.GLINER_MODEL)
    use_mps = bool(getattr(torch.backends, "mps", None)) and torch.backends.mps.is_available()
    dev = "mps" if use_mps else "cpu"
    # NOTE: GLiNER's `.device` is a read-only @property derived from the model's
    # parameters — `.to(dev)` is the only correct way to place it; assigning
    # `.device` raises AttributeError on this build.
    try:
        model.to(dev)
    except Exception as exc:  # noqa: BLE001 — MPS placement can fail on some builds
        logger.warning("facet_tagger: %s placement failed (%s); using CPU", dev, exc)
        model.to("cpu")
        dev = "cpu"
    logger.info("facet_tagger: GLiNER ready on %s (%d facets)", dev, len(pc.GHOST_B_FACET_VOCAB))
    _MODEL = model
    return model


# ----------------------------------------------------------------- matching
def _match_facet(spans: list[dict], surfaces: set[str]) -> str:
    """Pick the facet label for an entity from GLiNER's pass-2 spans.

    Match policy (highest wins): a span whose text equals one of the entity's
    surfaces (exact) beats a containment match, and within a tier the higher
    GLiNER score wins. Returns "" when no span refers to the entity — we do not
    guess a facet from an unrelated span."""
    best_label, best_rank = "", None
    for sp in spans or []:
        t = (sp.get("text") or "").strip().lower()
        label = (sp.get("label") or "").strip()
        if not t or not label:
            continue
        score = float(sp.get("score") or 0.0)
        if t in surfaces:
            rank = (2, score)
        elif any((t in s) or (s in t) for s in surfaces if s):
            rank = (1, score)
        else:
            continue
        if best_rank is None or rank > best_rank:
            best_rank, best_label = rank, label
    return best_label


def _get(entity: Any, key: str) -> str:
    """Field access that works for EntityItem-like objects AND the plain wire
    dicts the local lane ships between sidecar and worker."""
    if isinstance(entity, dict):
        return str(entity.get(key) or "")
    return str(getattr(entity, key, "") or "")


def _set(entity: Any, key: str, value: str) -> None:
    if isinstance(entity, dict):
        entity[key] = value
    else:
        setattr(entity, key, value)


def _surfaces_of(entity: Any) -> set[str]:
    """Lowercased {canonical_name, surface_form} for an entity (object or dict)."""
    out: set[str] = set()
    canon = _get(entity, "canonical_name").strip().lower()
    if canon:
        out.add(canon)
    surf = _get(entity, "surface_form").strip().lower()
    if surf:
        out.add(surf)
    return out


# ------------------------------------------------------------------- public
def tag_facets(
    entities: Iterable[Any],
    context_by_entity: dict[str, str] | None,
    *,
    model: Any = None,
) -> dict[str, str]:
    """Set EntityItem.object_kind via GLiNER pass-2, deduped per canonical_name.

    Args:
        entities: iterable of EntityItem-like objects OR plain wire dicts (need
            canonical_name, surface_form, and a settable object_kind). Mutated
            in place.
        context_by_entity: canonical_name (lowercased) -> context text, usually
            the entity's first-occurrence chunk. Entities with no context are
            skipped (object_kind stays "").
        model: optional GLiNER instance; defaults to the shared get_gliner().

    Returns the canonical_name -> object_kind map that was applied (handy for
    logging / tests). Additive: an entity that already has object_kind is left
    untouched."""
    items = [e for e in entities if _get(e, "canonical_name").strip()]
    if not items:
        return {}
    context_by_entity = context_by_entity or {}

    # Dedup canonical_name -> union of its surfaces (for span matching).
    reps: dict[str, set[str]] = {}
    for e in items:
        canon = _get(e, "canonical_name").strip().lower()
        if not canon:
            continue
        reps.setdefault(canon, set()).update(_surfaces_of(e))

    pc = _pc()
    vocab = pc.GHOST_B_FACET_VOCAB
    threshold = pc.GLINER_FACET_THRESHOLD
    batch_size = max(1, int(getattr(pc, "FACET_BATCH", 32)))

    # Stable order -> deterministic batch composition.
    queue = [(canon, context_by_entity[canon][:_CONTEXT_CHARS])
             for canon in sorted(reps) if context_by_entity.get(canon)]

    facet_map: dict[str, str] = {}
    mdl = model
    for start in range(0, len(queue), batch_size):
        sl = queue[start:start + batch_size]
        if mdl is None:
            mdl = get_gliner()
        try:
            # One forward pass per slice instead of one per entity — the facet
            # pass was a per-entity hot spot on entity-dense docs.
            batches = mdl.batch_predict_entities(
                [ctx for _c, ctx in sl], vocab, threshold=threshold)
        except Exception as exc:  # noqa: BLE001 — never let a tag failure abort ingestion
            logger.warning("facet_tagger: batch predict failed (%d ctxs): %s", len(sl), exc)
            continue
        for (canon, _ctx), spans in zip(sl, batches):
            facet = _match_facet(spans, reps[canon])
            if facet:
                facet_map[canon] = facet[:100]  # LLMEntity.object_kind max_length

    # Apply — additive, never overwrite an existing object_kind.
    for e in items:
        canon = _get(e, "canonical_name").strip().lower()
        facet = facet_map.get(canon)
        if facet and not _get(e, "object_kind"):
            _set(e, "object_kind", facet)
    return facet_map
