"""spaCy dependency-path relation adapter for the Ghost B pipeline.

Drop-in replacement for GLiREL in Stage C of ghost_b_local._extract_raw.
Same interface: extract_chunks(chunks, max_related, unit_batch) → list[list[dict]]
where each edge dict is {sub, obj, pred, ev, score}.

The spaCy path is ~200x faster than GLiREL on Apple Silicon because it's a
single CPU forward pass (en_core_web_sm) vs. a transformer classifier. It
catches the 60% of relations that follow predictable dependency grammar
(nsubj-verb-dobj, appos, poss) and normalizes predicates through
config/predicate_synonyms.yaml to the 31 Ghost B Predicate Literal values.

Selected via GHOST_B_RELATION_ENGINE=spacy (default) | glirel.
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
    """spaCy dependency-path relation extractor with GLiREL-compatible interface.

    Lazily loads the spaCy model on first call. Thread-safe via the GIL for
    the model load (worst case: two loads, one is discarded).
    """

    def __init__(self) -> None:
        self._extractor: Any = None

    def _ensure_loaded(self) -> None:
        if self._extractor is not None:
            return
        from services.extraction.dep_path_extractor import DepPathExtractor
        model = os.environ.get("SPACY_MODEL", "en_core_web_sm")
        self._extractor = DepPathExtractor(model_name=model)
        logger.info(
            "SpacyRelationExtractor loaded model=%s", model
        )

    def extract_chunks(
        self,
        chunks: list[dict],
        max_related: int = 10,
        unit_batch: int = 64,  # kept for interface compat, unused
        docs: list | None = None,
        suppression_counters_list: list[dict[str, int]] | None = None,
    ) -> list[list[dict]]:
        """Extract relations from a batch of chunks.

        Args:
            chunks: list of {chunk_id, doc_id, text, entities} dicts.
                    entities is a list of GLiNER entity dicts with keys:
                    surface/text, start_char/start, end_char/end, entity_type/type.
            max_related: cap on relations per chunk.
            unit_batch: ignored (interface compat with GLiREL).
            docs: pre-parsed spaCy Docs (one per chunk). If provided, skips
                  internal nlp.pipe() — enables single-parse-per-chunk when
                  the pipeline shares Docs between Stage B and Stage C.
            suppression_counters_list: one dict per chunk for named suppression
                  counters. Every suppression rule increments a key here.

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
            triples = self._extractor.extract(
                text=text,
                entities=spans,
                chunk_id=chunk_id,
                doc_id=doc_id,
                doc=parsed_docs[idx],
                suppression_counters=_supp_ctr,
            )

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
                    continue
                pred = _normalize_to_schema(t.predicate)
                if pred is None:
                    _noise_count += 1
                    if _supp_ctr is not None:
                        _supp_ctr["adapter_noise_or_t4_dropped"] = (
                            _supp_ctr.get("adapter_noise_or_t4_dropped", 0) + 1)
                    continue  # noise verb or T4 drop
                # allowed_pairs gate: reject invalid type combinations
                subj_type = type_by_surface.get(t.subject_surface.lower(), "")
                obj_type = type_by_surface.get(t.object_surface.lower(), "")
                if not _pair_allowed(pred, subj_type, obj_type):
                    _rejection_count += 1
                    if _supp_ctr is not None:
                        _supp_ctr["adapter_allowed_pairs_rejected"] = (
                            _supp_ctr.get("adapter_allowed_pairs_rejected", 0) + 1)
                    continue
                if len(edges) >= max_related:
                    # NO SILENT CAPS (repo law): count what the cap discarded.
                    # Today the surviving edges are whichever came first in
                    # entity-offset order, not the most confident — R5 fixes the
                    # ordering; R-pre first measures how often it even matters.
                    _capped += 1
                    continue
                ev = t.sentence_text or text[:200]
                edges.append({
                    "sub": t.subject_surface[:200],
                    "obj": t.object_surface[:200],
                    "pred": pred,
                    "ev": ev[:500],
                    "score": t.confidence,
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
# Entity-type casing normalization (R-pre Finding 6, fixed in R8)
#
# ontology.yaml declares entity_types in Title Case (Concept, Software, ...)
# and allowed_pairs does an EXACT tuple match. The RunPod lane stores UPPERCASE
# types (CONCEPT, PERSON, ORGANIZATION), so every uppercase-typed entity failed
# every constrained predicate silently. MEASURED on a 5,500-chunk sample:
# normalizing recovers 84 relations (+2.9%). Minor at the time it was measured
# ONLY because pod relations were never stored at all; it becomes load-bearing
# the moment the pod lane starts emitting.
#
# Normalizing here — at the single boundary every lane passes through — keeps
# local, pod, and backfill from disagreeing about what a type is.
# ---------------------------------------------------------------------------

_ONTOLOGY_ENTITY_TYPES = (
    "Person", "Organization", "Location", "Event", "Concept", "Method",
    "Product", "Software", "Document", "Standard", "Rule", "Law",
    "Artifact", "TimeReference", "other",
)
_UPPER_TO_ONTOLOGY = {t.upper(): t for t in _ONTOLOGY_ENTITY_TYPES}

# Types emitted by upstream taggers that have no ontology equivalent. Mapped to
# the "other" wildcard so they are not silently gated out by allowed_pairs —
# "other" is an explicit pass in pair_allowed(), so these stay eligible while
# remaining honestly untyped. Extending the ontology is an OWNER decision.
_UNMAPPED_TO_WILDCARD = frozenset({
    "PLACE", "BEHAVIOR", "PROCESS", "QUALITY", "AGENT",
})


def normalize_entity_type(raw: str) -> str:
    """Map an upstream entity type onto ontology.yaml casing.

    Exact ontology values pass through. Case variants are folded. Known
    out-of-ontology types become the "other" wildcard. Anything else is
    returned unchanged so it stays visible rather than being quietly coerced.
    """
    if not raw:
        return ""
    if raw in _ONTOLOGY_ENTITY_TYPES:
        return raw
    upper = raw.strip().upper()
    mapped = _UPPER_TO_ONTOLOGY.get(upper)
    if mapped:
        return mapped
    if upper in _UNMAPPED_TO_WILDCARD:
        return "other"
    return raw


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
