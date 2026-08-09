"""spaCy dependency-path relation adapter for the Ghost B pipeline.

Structural relation lane used by the canonical Relex extractor. Same
interface: extract_chunks(chunks, max_related, unit_batch) → list[list[dict]]
where each edge dict is {sub, obj, pred, ev, score}.

The spaCy path is a single CPU forward pass (en_core_web_sm) rather than a
transformer classifier. It catches the ~60% of relations that follow
predictable dependency grammar (nsubj-verb-dobj, appos, poss) and normalizes
predicates through config/predicate_synonyms.yaml to the Ghost B Predicate
Literal values.

This is the only structural relation lane in the Relex-only runtime.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Valid Ghost B predicates (ghost_b_schemas.Predicate Literal + ontology extensions)
VALID_PREDICATES = frozenset({
    "part_of", "member_of", "located_in", "works_for", "created_by",
    "owns", "affiliated_with", "synonym_of", "instance_of", "uses",
    "runs_on", "trained_on", "references", "implements", "depends_on",
    "produces", "stores", "detects", "supports", "defines", "represents",
    "maps_to", "preceded_by", "causes", "overlaps", "derived_from",
    "contradicts", "excepts", "overrides", "related_to",
    # Ontology extensions (defined in config/ontology.yaml, emitted by spaCy extractor)
    "has_part", "includes", "example_of", "evaluates", "deploys",
    "creates", "trains", "runs", "quantizes",
})

# allowed_pairs gate: predicate → set of (subject_type, object_type) tuples.
# Mirrored from config/ontology.yaml. DEFENSE-IN-DEPTH: the primary gate is
# now in dep_path_extractor.pair_allowed() (enforced at extraction time with
# rejection counters). This adapter-level check is a safety net only.
_ALLOWED_PAIRS: dict[str, frozenset[tuple[str, str]]] = {}


def _load_allowed_pairs() -> dict[str, frozenset[tuple[str, str]]]:
    global _ALLOWED_PAIRS
    if _ALLOWED_PAIRS:
        return _ALLOWED_PAIRS
    try:
        import yaml
        from pathlib import Path
        _this = Path(__file__).resolve()
        candidates = [
            _this.parents[3] / "config" / "ontology.yaml",
            _this.parents[2] / "config" / "ontology.yaml",
        ]
        path = next((p for p in candidates if p.exists()), None)
        if path:
            data = yaml.safe_load(path.read_text()) or {}
            predicates = data.get("predicates") or {}
            for pred, spec in predicates.items():
                pairs = spec.get("allowed_pairs") or []
                _ALLOWED_PAIRS[pred] = frozenset(
                    (str(a), str(b)) for a, b in pairs
                )
    except Exception as exc:
        logger.warning("Could not load ontology.yaml allowed_pairs: %s", exc)
    return _ALLOWED_PAIRS


def _pair_allowed(predicate: str, subject_type: str, object_type: str) -> bool:
    """Check if (subject_type, object_type) is valid for this predicate.

    Returns True if:
      - predicate has no allowed_pairs entry (unconstrained, e.g. related_to)
      - the pair is in the allowed set
      - either type is 'other' (wildcard — GLiNER low-confidence)
    """
    allowed = _load_allowed_pairs()
    pair_set = allowed.get(predicate)
    if pair_set is None:
        return True  # unconstrained predicate
    if not subject_type or not object_type:
        return True  # missing type info — don't block
    if subject_type == "other" or object_type == "other":
        return True  # wildcard
    return (subject_type, object_type) in pair_set

# Predicates that are noise for graph edges (cognitive/perceptual verbs that
# slip through normalization). These get dropped, not mapped to related_to.
_NOISE_PREDICATES = frozenset({
    "SEE", "WANT", "FIND", "KNOW", "FEEL", "THINK", "SAY", "ASK",
    "TELL", "LEARN", "TRY", "KEEP", "GO", "COME", "LOOK", "GET",
    "DO", "CONSIDER", "UNDERSTAND", "FOCUS", "SUGGEST", "CHOOSE",
    "EXAMINE", "START", "SET", "WORK", "CLICK", "REACH",
})


class SpacyRelationExtractor:
    """spaCy dependency-path relation extractor.

    Lazily loads the spaCy model on first call. Thread-safe via the GIL for
    the model load (worst case: two loads, one is discarded).
    """

    def __init__(self) -> None:
        self._extractor: Any = None

    def _ensure_loaded(self) -> None:
        if self._extractor is not None:
            return
        model = os.environ.get("SPACY_MODEL", "en_core_web_sm")
        # PAIRING MODEL (2026-07-30). "frame" is the rebuilt, frame-licensed
        # model: find predicate constructions, then fill their argument slots.
        # "deppath" is the legacy O(n^2) shortest-path pairing it replaces —
        # kept only so the two can be A/B'd on the same corpus. Legacy hand-
        # judged at ~0.25 precision; see frame_extractor.py for why.
        mode = os.environ.get("GHOST_B_PAIRING_MODEL", "frame").strip().lower()
        if mode == "deppath":
            from services.extraction.dep_path_extractor import DepPathExtractor
            self._extractor = DepPathExtractor(model_name=model)
        elif mode == "frame":
            from services.extraction.frame_extractor import FrameExtractor
            self._extractor = FrameExtractor(model_name=model)
        else:
            raise RuntimeError(
                f"FATAL: GHOST_B_PAIRING_MODEL={mode!r} is not a known pairing "
                "model. Use 'frame' (default) or 'deppath' (legacy A/B only)."
            )
        self._pairing_model = mode
        logger.info(
            "SpacyRelationExtractor loaded model=%s pairing=%s", model, mode
        )

    def extract_chunks(
        self,
        chunks: list[dict],
        max_related: int = 10,
        unit_batch: int = 64,  # kept for interface compat, unused
        docs: list | None = None,
        suppression_counters_list: list[dict[str, int]] | None = None,
        trace_list: list[list[dict]] | None = None,
    ) -> list[list[dict]]:
        """Extract relations from a batch of chunks.

        Args:
            chunks: list of {chunk_id, doc_id, text, entities} dicts.
                    entities is a list of GLiNER entity dicts with keys:
                    surface/text, start_char/start, end_char/end, entity_type/type.
            max_related: cap on relations per chunk.
            unit_batch: ignored (kept for interface compatibility).
            docs: pre-parsed spaCy Docs (one per chunk). If provided, skips
                  internal nlp.pipe() — enables single-parse-per-chunk when
                  the pipeline shares Docs between Stage B and Stage C.
            suppression_counters_list: one dict per chunk for named suppression
                  counters. Every suppression rule increments a key here.
            trace_list: one list per chunk receiving a PER-CANDIDATE record of
                  where each candidate died. Counters say how many died at each
                  guard; only this says WHICH one died where — the difference
                  between "recall is 0.028" and "recall is 0.028 BECAUSE".
                  Covers the extractor's internal guards AND the four
                  adapter-boundary drops below (is_graph_edge, schema
                  normalization, the second allowed_pairs gate, and the cap),
                  which are invisible to the extractor's own trace.

        Returns:
            List of edge lists, one per chunk. Each edge is a dict:
            {sub, obj, pred, ev, score}
        """
        self._ensure_loaded()
        from services.extraction.dep_path_extractor import EntitySpan

        results: list[list[dict]] = []
        _rejection_count = 0
        _noise_count = 0
        _t4_drop_count = 0

        # Batch-parse all texts in one nlp.pipe() call (one parse per chunk).
        # The resulting Docs are shared with both Stage B (appos) and Stage C.
        texts = [chunk.get("text") or "" for chunk in chunks]
        if docs is not None:
            # Pre-parsed Docs provided by the pipeline (single-parse path)
            parsed_docs = docs
        else:
            nlp = self._extractor._nlp
            parsed_docs = list(nlp.pipe(texts, batch_size=max(16, len(texts))))

        for idx, chunk in enumerate(chunks):
            text = texts[idx]
            raw_entities = chunk.get("entities") or []
            chunk_id = chunk.get("chunk_id") or ""
            doc_id = chunk.get("doc_id") or ""

            if not text.strip() or len(raw_entities) < 2:
                results.append([])
                continue

            # ENTITY QUALITY: only graph-eligible entities may anchor a
            # relation. Un-annotated rows pass through, so corpora predating
            # the gate are unaffected. MEASURED: of relations judged CORRECT,
            # 28.6% still had a generic endpoint -- that is what this removes.
            # RELAXED tier: hard rules only (pronouns, artifacts, noise
            # labels). The strict node gate collapsed relation yield 34x, so
            # genericness is judged at the NODE boundary, not here.
            from services.extraction.entity_quality import relation_anchors
            raw_entities = relation_anchors(raw_entities)
            if len(raw_entities) < 2:
                results.append([])
                continue

            # Convert GLiNER entity dicts → EntitySpan
            spans = _to_entity_spans(raw_entities, text)
            if len(spans) < 2:
                results.append([])
                continue

            # Extract triples using the pre-parsed Doc (no re-parse)
            _supp_ctr = (
                suppression_counters_list[idx]
                if suppression_counters_list is not None and idx < len(suppression_counters_list)
                else None
            )
            _trace = (
                trace_list[idx]
                if trace_list is not None and idx < len(trace_list)
                else None
            )
            triples = self._extractor.extract(
                text=text,
                entities=spans,
                chunk_id=chunk_id,
                doc_id=doc_id,
                doc=parsed_docs[idx],
                suppression_counters=_supp_ctr,
                trace=_trace,
            )

            def _adapter_die(t, reason: str) -> None:
                """Record an adapter-boundary drop against its candidate."""
                if _trace is None:
                    return
                _trace.append({
                    "subject_entity": t.subject_surface,
                    "object_entity": t.object_surface,
                    "predicate_token": t.predicate_surface,
                    "frame_type": t.dep_signature.split(":", 1)[0],
                    "signature": t.dep_signature,
                    "predicate": t.predicate,
                    "died_at": reason,
                })

            # Convert to edge dicts, filter noise, enforce allowed_pairs, cap
            edges: list[dict] = []
            # Build surface→type lookup for allowed_pairs gate
            type_by_surface = {
                s.surface.lower(): s.entity_type for s in spans if s.entity_type
            }
            # R-pre: attribute every adapter-boundary drop to THIS chunk. These
            # were batch-level locals logged only in aggregate, so a corpus
            # could bleed relations here with no way to locate where.
            _capped = 0
            for t in triples:
                # CONTRACT: only is_graph_edge triples reach the graph writer.
                # Qualified triples (negated, modal, attributed, conditional)
                # are preserved for the ClaimRecord path but never emitted here.
                if not t.is_graph_edge:
                    if _supp_ctr is not None:
                        _supp_ctr["adapter_non_graph_edge"] = (
                            _supp_ctr.get("adapter_non_graph_edge", 0) + 1)
                    _adapter_die(t, "adapter_non_graph_edge")
                    continue
                pred = _normalize_to_schema(t.predicate)
                if pred is None:
                    _noise_count += 1
                    if _supp_ctr is not None:
                        _supp_ctr["adapter_noise_or_t4_dropped"] = (
                            _supp_ctr.get("adapter_noise_or_t4_dropped", 0) + 1)
                    _adapter_die(t, "adapter_noise_or_t4_dropped")
                    continue  # noise verb or T4 drop
                # allowed_pairs gate: reject invalid type combinations
                subj_type = type_by_surface.get(t.subject_surface.lower(), "")
                obj_type = type_by_surface.get(t.object_surface.lower(), "")
                if not _pair_allowed(pred, subj_type, obj_type):
                    _rejection_count += 1
                    if _supp_ctr is not None:
                        _supp_ctr["adapter_allowed_pairs_rejected"] = (
                            _supp_ctr.get("adapter_allowed_pairs_rejected", 0) + 1)
                    _adapter_die(
                        t,
                        f"adapter_allowed_pairs_rejected[{pred}:"
                        f"{subj_type or '?'}->{obj_type or '?'}]",
                    )
                    continue
                if len(edges) >= max_related:
                    # NO SILENT CAPS (repo law): count what the cap discarded.
                    # Today the surviving edges are whichever came first in
                    # entity-offset order, not the most confident — R5 fixes the
                    # ordering; R-pre first measures how often it even matters.
                    _capped += 1
                    _adapter_die(t, "adapter_cap_truncated")
                    continue
                ev = t.sentence_text or text[:200]
                edges.append({
                    "sub": t.subject_surface[:200],
                    "obj": t.object_surface[:200],
                    "pred": pred,
                    "ev": ev[:500],
                    "score": t.confidence,
                })
                if _trace is not None:
                    _trace.append({
                        "subject_entity": t.subject_surface,
                        "object_entity": t.object_surface,
                        "predicate_token": t.predicate_surface,
                        "frame_type": t.dep_signature.split(":", 1)[0],
                        "signature": t.dep_signature,
                        "predicate": pred,
                        "died_at": None,
                        "survived": True,
                    })

            if _capped and _supp_ctr is not None:
                _supp_ctr["adapter_cap_truncated"] = (
                    _supp_ctr.get("adapter_cap_truncated", 0) + _capped)

            results.append(edges)

        # Structured rejection counter — never drop silently
        if _rejection_count or _noise_count:
            logger.info(
                "spacy_relation_adapter: chunks=%d emitted=%d "
                "allowed_pairs_rejected=%d noise_or_t4_dropped=%d",
                len(chunks),
                sum(len(r) for r in results),
                _rejection_count,
                _noise_count,
            )

        return results


# ---------------------------------------------------------------------------
# Entity-type casing normalization is now owned by services.extraction.canonical.
# Import normalize_entity_type from there — single source of truth shared by
# this adapter, entity_quality, relex_gate, and neo4j_writer.
# ---------------------------------------------------------------------------
from services.extraction.canonical import normalize_entity_type  # noqa: E402


def _to_entity_spans(raw_entities: list[dict], text: str = "") -> list:
    """Convert GLiNER entity dicts to EntitySpan objects.

    Handles both the gate-fixture format (surface, start_char, end_char) and
    the live pipeline format (surface_form, no offsets). When char offsets are
    missing, locates the surface in the text via sequential find.
    """
    from services.extraction.dep_path_extractor import EntitySpan

    spans = []
    _search_from = 0  # sequential anchor for offset resolution
    for ent in raw_entities:
        surface = (
            ent.get("surface") or ent.get("surface_form") or ent.get("text") or ""
        ).strip()
        etype = normalize_entity_type(
            ent.get("entity_type") or ent.get("type") or ""
        )
        canonical = ent.get("canonical_name") or surface

        if not surface:
            continue

        start = ent.get("start_char") or ent.get("start") or 0
        end = ent.get("end_char") or ent.get("end") or 0

        # Pipeline entities lack char offsets — resolve from text.
        if end <= start and text:
            idx = text.find(surface, _search_from)
            if idx == -1:
                idx = text.lower().find(surface.lower(), _search_from)
            if idx == -1:
                idx = text.find(surface)  # full-text fallback
            if idx >= 0:
                start = idx
                end = idx + len(surface)
                _search_from = idx + 1  # advance for next entity
            else:
                continue  # surface not found in text — skip
        elif end <= start:
            continue

        spans.append(EntitySpan(
            surface=surface,
            start_char=int(start),
            end_char=int(end),
            entity_type=etype,
            canonical_name=canonical,
        ))
    return spans


def _normalize_to_schema(predicate: str) -> str | None:
    """Normalize a predicate to a valid Ghost B schema value.

    Returns None for noise predicates that should be dropped entirely.
    The upstream resolver (dep_path_extractor.resolve_predicate) already
    returns only valid predicates or None, so this is a safety net.
    Unmapped predicates are DROPPED, not mapped to related_to.
    """
    # Already valid
    if predicate in VALID_PREDICATES:
        return predicate

    # Check noise (uppercase form from legacy normalize_predicate fallback)
    upper = predicate.upper().replace(" ", "_")
    if upper in _NOISE_PREDICATES:
        return None

    # Try lowercase match against valid set
    lower = predicate.lower().replace(" ", "_")
    if lower in VALID_PREDICATES:
        return lower

    # Unmapped → DROP (never manufacture related_to pollution)
    return None


# Module-level singleton (lazy)
_INSTANCE: SpacyRelationExtractor | None = None


def get_spacy_extractor() -> SpacyRelationExtractor:
    """Get or create the module-level SpacyRelationExtractor singleton."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = SpacyRelationExtractor()
    return _INSTANCE
