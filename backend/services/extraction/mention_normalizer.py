"""Deterministic mention normalization for entity boundary equivalence.

Handles singular/plural boundary differences ("cold leads" ↔ "cold lead") and
morphological variants WITHOUT introducing a SpanRuler component. This is the
minimal normalizer that lets the corroboration gate join evidence from
different pipeline stages when the model decodes slightly different entity
boundaries.

Scope:
  - Lemmatization via spaCy (plural → singular, verb tenses → root)
  - NFKC unicode normalization
  - Lowercase + whitespace collapse

NOT normalized (different entities, not morphological variants):
  - "Shadow Cities" → "Cities"  (truncation, not morphology)
  - "New York" → "York"          (dropped qualifier)
  - "Israeli army" → "army"      (dropped qualifier)

Both raw spans are always preserved — normalization produces a join key,
not a replacement for the original entity record.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from functools import lru_cache

logger = logging.getLogger(__name__)

# Lazy spaCy model — only the tokenizer + lemmatizer are needed.
_NLP: object | None = None

_WS = re.compile(r"\s+")

# Known dangerous truncations that must NOT be normalized together.
# These are prefix/suffix removals, not morphology. The normalizer does not
# do these, but this set documents the boundary and is checked by tests.
_FORBIDDEN_PAIRS: frozenset[tuple[str, str]] = frozenset()


def _get_nlp() -> object:
    """Reuse the shared spaCy singleton (parse-once / single-model invariant).

    Lemmatization needs tagger; get_shared_nlp disables only ner/textcat.
    """
    global _NLP
    if _NLP is not None:
        return _NLP
    try:
        from services.extraction.appos_enrichment import get_shared_nlp

        _NLP = get_shared_nlp()
        return _NLP
    except Exception as exc:  # noqa: BLE001
        logger.warning("shared nlp unavailable (%s); falling back", exc)
    import spacy

    try:
        _NLP = spacy.load("en_core_web_sm", disable=["ner", "textcat"])
    except OSError:
        _NLP = spacy.blank("en")
        try:
            _NLP.add_pipe("tagger")
            _NLP.add_pipe("lemmatizer")
        except Exception:
            logger.warning("Could not add lemmatizer to blank pipeline")
    return _NLP


def normalized_mention(text: str) -> str:
    """Deterministic lemmatized normal form for entity surface text.

    "cold leads"  → "cold lead"
    "CAPTCHA"     → "captcha"
    "Companies"   → "company"
    "Shadow Cities" → "shadow city"

    This is a JOIN KEY, not a display name. Both raw spans must be preserved.
    """
    text = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    if not text:
        return ""

    nlp = _get_nlp()
    # Must run the pipe (not just make_doc) for the lemmatizer to fire.
    doc = nlp(text)
    parts = []
    for token in doc:
        lemma = token.lemma_.lower().strip() if token.lemma_ else token.lower_.strip()
        if lemma:
            parts.append(lemma)
    return _WS.sub(" ", " ".join(parts)).strip()


@lru_cache(maxsize=2048)
def _normalized_cached(text: str) -> str:
    return normalized_mention(text)


def normalized_mention_cached(text: str) -> str:
    """Cached version for hot-path use."""
    return _normalized_cached(text)


def is_morphological_variant(left: str, right: str) -> bool:
    """True when two surfaces are morphological variants (singular/plural etc).

    False for truncations like "Shadow Cities" vs "Cities" — even though the
    shorter is a substring, these are different entities.

    The rule: normal forms must be EQUAL. If one normal form contains the
    other but they differ, it's a qualifier drop, not morphology.
    """
    nl = normalized_mention_cached(left)
    nr = normalized_mention_cached(right)
    if not nl or not nr:
        return False
    if nl == nr:
        return True
    return False


def resolve_mention_pair(
    model_surface: str,
    model_span: tuple[int, int],
    gold_surface: str,
    gold_span: tuple[int, int],
) -> dict | None:
    """Attempt to join a model-decoded entity to a gold entity via morphology.

    Returns a resolution dict when the two surfaces are morphological variants
    that share the same token root. Returns None for genuine truncations
    (different entities).

    Output shape:
        {
            "model_surface": "cold leads",
            "model_span": [333, 343],
            "canonical_surface": "cold lead",
            "canonical_entity_id": "concept:cold_lead",
        }
    """
    if is_morphological_variant(model_surface, gold_surface):
        canonical = gold_surface  # prefer gold's surface as canonical
        canon_id = f"entity:{normalized_mention_cached(canonical).replace(' ', '_')}"
        return {
            "model_surface": model_surface,
            "model_span": list(model_span),
            "canonical_surface": canonical,
            "canonical_entity_id": canon_id,
        }
    return None
