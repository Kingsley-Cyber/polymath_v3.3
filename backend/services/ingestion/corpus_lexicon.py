"""Durable corpus vocabulary materialization from existing extraction artifacts.

The lexicon is a projection, not a new extraction lane.  It turns the entity,
alias, definition, relation, and source identities already stored in
``ghost_b_extractions`` into two idempotent Mongo projections:

``corpus_lexicon_sources``
    One document-scoped contribution per reconciled entity identity.  This is
    the deletion/update boundary and retains bounded source-level provenance.

``corpus_lexicon``
    The corpus-scoped materialized vocabulary used by retrieval and mirrored
    into the corpus's isolated Qdrant ``schemas`` collection.

Aliases are identity evidence. Components, applications, factual relations,
and co-occurrence are deliberately separate association types and can never
silently merge identities.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from itertools import combinations
from typing import Any, Iterable

from pymongo import ReplaceOne, UpdateOne

from models.extraction_artifact import (
    CANDIDATE_EXTRACTION_SCHEMA_HASH,
    CandidateExtractionArtifact,
)
from services.ingestion.extraction_artifacts import (
    candidate_artifact_to_lexicon_row,
)

logger = logging.getLogger(__name__)

LEXICON_SCHEMA_VERSION = "corpus_lexicon.v3"
LEXICON_SOURCE_VERSION = "corpus_lexicon_source.v3"
LEXICON_COLLECTION = "corpus_lexicon"
LEXICON_SOURCE_COLLECTION = "corpus_lexicon_sources"
LEXICON_RUN_COLLECTION = "lexicon_backfill_runs"

_MAX_SOURCE_CHUNKS = 256
_MAX_SOURCE_PARENTS = 192
_MAX_DEFINITIONS = 12
_MAX_RELATIONS = 96
_MAX_NEIGHBORS = 32
_MAX_ENTITY_IDS = 32
_MAX_STRUCTURAL_CONTEXTS = 24
_MAX_CONTEXTUAL_USAGES = 12

_SPACE_RE = re.compile(r"\s+")
_YEAR_NOISE_RE = re.compile(
    r"(?:\(|\[)?(?:19|20)\d{2}(?:\s*(?:and|/|,|-)\s*(?:19|20)\d{2})*(?:\)|\])?$",
    re.IGNORECASE,
)
_PAGE_NOISE_RE = re.compile(
    r"(?:^|\b)(?:p{1,2}|page|pages|fig(?:ure)?|table)\.?\s*\d+(?:[-:]\d+)?\b",
    re.IGNORECASE,
)
_CITATION_NOISE_RE = re.compile(
    r"\b(?:doi|isbn|issn)\b|https?://|www\.|\bet\s+al\.?\b",
    re.IGNORECASE,
)
_FILE_SUFFIX_RE = re.compile(r"\.(?:pdf|epub|md|txt|docx?)$", re.IGNORECASE)
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
_TRAILING_PAGE_RE = re.compile(r"[\s_-]+\d{1,4}[a-z]?$", re.IGNORECASE)
_TRAILING_IDENTITY_CODE_RE = re.compile(r"^\d{1,4}[a-z]?$", re.IGNORECASE)
_ABBREVIATION_RE = re.compile(r"^[A-Z][A-Z0-9-]{1,9}$")
_PAREN_SHORT_FORM_RE = re.compile(r"\((?P<short>[A-Z][A-Z0-9-]{1,9})\)")
_SHORT_LONG_FORM_RE = re.compile(
    r"\b(?P<short>[A-Z][A-Z0-9-]{1,9})\s*\(" r"(?P<long>[A-Za-z][^()\n]{2,100})\)"
)
_NUMERIC_COMBINATION_RE = re.compile(
    r"^(?:\d+[A-Z]?(?:\s*[+/]\s*\d+[A-Z]?)+)(?:\s*[,;]\s*\d+[A-Z]?(?:\s*[+/]\s*\d+[A-Z]?)+)*$",
    re.IGNORECASE,
)
_HEADING_PREFIX_RE = re.compile(
    r"^(?:#{1,6}\s*|(?:chapter|section|part|unit|module)\s+"
    r"(?:[A-Z0-9IVXLC]+(?:[.:-]\d+)*[.):-]?\s*)?)",
    re.IGNORECASE,
)
_GENERIC_HEADING_KEYS = frozenset(
    {
        "abstract",
        "appendix",
        "chapter",
        "conclusion",
        "contents",
        "introduction",
        "notes",
        "overview",
        "references",
        "section",
        "summary",
        "table of contents",
    }
)

_CONTEXTUAL_BOILERPLATE_RE = re.compile(
    r"^(?:this\s+)?(?:chapter|document|manual|passage|section|table)\s+"
    r"(?:catalogs?|contains?|describes?|discusses?|is\s+(?:a|an|from)|lists?|"
    r"presents?|provides?\s+(?:a\s+)?(?:bibliography|catalog|source\s+map))\b|"
    r"\b(?:acknowledgements?|bibliograph(?:y|ies)|front\s+matter)\b",
    re.IGNORECASE,
)
_CONTEXTUAL_ADMIN_RE = re.compile(
    r"\b(?:annotated\s+source\s+catalog|bibliograph(?:y|ies)|catalog(?:s|ue)?|"
    r"checklist|data\s+volume|extraction\s+completeness|file\s+status|front\s+"
    r"matter|ocr\s+(?:pass|report)|references?\s+for\s+deeper\s+study|source\s+map)\b",
    re.IGNORECASE,
)

_IDENTITY_STOPWORDS = frozenset(
    {
        "actor",
        "author",
        "book",
        "company",
        "concept",
        "document",
        "framework",
        "method",
        "model",
        "organization",
        "person",
        "process",
        "product",
        "strategy",
        "system",
        "technique",
        "tool",
    }
)

_APPLICATION_PREDICATES = frozenset(
    {
        "affects",
        "applied_to",
        "causes",
        "enables",
        "improves",
        "influences",
        "produces",
        "targets",
        "used_for",
        "uses",
    }
)
_IDENTITY_EQUIVALENCE_PREDICATES = frozenset({"alias_of", "equivalent_to", "same_as"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _lexicon_document_counts(db: Any, corpus_id: str) -> dict[str, int]:
    active = {
        "$and": [
            {"corpus_id": corpus_id},
            {
                "$or": [
                    {"status": {"$exists": False}},
                    {"status": "active"},
                ]
            },
        ]
    }
    total = await db["documents"].count_documents(active)
    processed = await db["documents"].count_documents(
        {
            "$and": [
                *active["$and"],
                {
                    "lexicon_state": {
                        "$in": [
                            "lexicon_pending",
                            "lexicon_materialized",
                            "lexicon_ready",
                        ]
                    }
                },
            ]
        }
    )
    ready = await db["documents"].count_documents(
        {"$and": [*active["$and"], {"lexicon_state": "lexicon_ready"}]}
    )
    return {"total": int(total), "processed": int(processed), "ready": int(ready)}


def normalize_identity(value: Any) -> str:
    """Normalize punctuation variants without erasing meaningful digits."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _clean_display(value: Any) -> str:
    text = _SPACE_RE.sub(" ", str(value or "")).strip(" \t\r\n,;:")
    return _FILE_SUFFIX_RE.sub("", text).strip()


def _is_abbreviation(value: str) -> bool:
    return bool(_ABBREVIATION_RE.fullmatch(value.strip()))


def _initialism(value: str) -> str:
    return "".join(
        token if token.isdigit() else token[0]
        for token in normalize_identity(value).split()
        if token
    )


def _abbreviation_expands(abbreviation: str, phrase: str) -> bool:
    compact = re.sub(r"[^A-Za-z0-9]", "", abbreviation).lower()
    return bool(compact and len(compact) >= 2 and compact == _initialism(phrase))


def mine_acronym_pairs(value: Any) -> list[dict[str, str]]:
    """Extract conservative long-form/short-form identity evidence.

    This is a bounded Schwartz-Hearst-style pass over already stored chunk
    text. A pair is accepted only when the short form exactly matches the long
    form's initialism, so parenthetical citations and ordinary asides cannot
    become aliases.
    """

    text = str(value or "")
    pairs: dict[tuple[str, str], dict[str, str]] = {}
    for match in _PAREN_SHORT_FORM_RE.finditer(text):
        short = match.group("short")
        prefix = re.split(r"[.!?;:\n]", text[: match.start()])[-1]
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'/-]*", prefix)[-12:]
        candidates = [
            " ".join(words[start:])
            for start in range(len(words))
            if 2 <= len(words[start:]) <= 12
            and _abbreviation_expands(short, " ".join(words[start:]))
        ]
        if not candidates:
            continue
        long_form = min(candidates, key=lambda item: (len(item.split()), len(item)))
        key = (normalize_identity(long_form), normalize_identity(short))
        pairs[key] = {
            "long_form": long_form,
            "short_form": short,
            "evidence": _SPACE_RE.sub(
                " ", text[max(0, match.start() - 140) : match.end()]
            ).strip()[:240],
        }

    for match in _SHORT_LONG_FORM_RE.finditer(text):
        short = match.group("short")
        long_form = _SPACE_RE.sub(" ", match.group("long")).strip(" ,;:")
        if not _abbreviation_expands(short, long_form):
            continue
        key = (normalize_identity(long_form), normalize_identity(short))
        pairs[key] = {
            "long_form": long_form,
            "short_form": short,
            "evidence": _SPACE_RE.sub(" ", match.group(0)).strip()[:240],
        }
    return list(pairs.values())


def mine_entity_text_evidence(
    value: Any,
    canonical_name: str,
    *,
    acronym_pairs: list[dict[str, str]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Mine explicit aliases and definitions anchored to one known entity."""

    text = str(value or "")
    canonical = _clean_display(canonical_name)
    if not text or not canonical:
        return {"aliases": [], "definitions": []}

    aliases: list[dict[str, str]] = []
    canonical_key = normalize_identity(canonical)
    for pair in (
        acronym_pairs if acronym_pairs is not None else mine_acronym_pairs(text)
    ):
        long_form = pair["long_form"]
        short_form = pair["short_form"]
        long_key = normalize_identity(long_form)
        short_key = normalize_identity(short_form)
        if canonical_key == long_key:
            alias = short_form
        elif canonical_key == short_key:
            alias = long_form
        else:
            continue
        aliases.append(
            {
                "alias": alias,
                "method": "schwartz_hearst_acronym",
                "evidence": pair["evidence"],
            }
        )

    escaped = re.escape(canonical)
    alias_pattern = re.compile(
        rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])\s*"
        r"(?:,?\s*(?:also\s+known\s+as|also\s+called|a\.?k\.?a\.?|short\s+for))\s+"
        r"(?P<alias>[A-Za-z0-9][A-Za-z0-9'&/\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9'&/\-]*){0,9})"
        r"(?=[,.;:!?\n]|$)",
        re.IGNORECASE,
    )
    for match in alias_pattern.finditer(text):
        aliases.append(
            {
                "alias": _SPACE_RE.sub(" ", match.group("alias")).strip(),
                "method": "explicit_alias_pattern",
                "evidence": _SPACE_RE.sub(" ", match.group(0)).strip()[:240],
            }
        )

    definitions: list[dict[str, str]] = []
    definition_pattern = re.compile(
        rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
        r"(?:\s*\([A-Z][A-Z0-9-]{1,9}\))?\s+"
        r"(?:is|means|refers\s+to|is\s+defined\s+as)\s+"
        r"(?P<definition>[^.!?\n]{12,500})",
        re.IGNORECASE,
    )
    for match in definition_pattern.finditer(text):
        definition = clean_definition(match.group("definition"))
        if definition:
            definitions.append(
                {
                    "text": definition,
                    "method": "explicit_definition_pattern",
                }
            )

    return {
        "aliases": _dedupe_dicts(aliases, ("alias", "method"), 12),
        "definitions": _dedupe_dicts(definitions, ("text", "method"), 6),
    }


def mine_structural_contexts(
    heading_path: Any,
    canonical_name: str,
) -> list[dict[str, str]]:
    """Return source headings as typed context, never as identity aliases.

    Headings are useful naive-language landing signals, but a heading can name
    a topic broader than the entity mentioned below it. Keeping this evidence
    in a separate field lets gloss retrieval use it without allowing exact
    alias matching or identity reconciliation to collapse the two concepts.
    """

    values = heading_path if isinstance(heading_path, (list, tuple)) else [heading_path]
    canonical_key = normalize_identity(canonical_name)
    output: list[dict[str, str]] = []
    for raw in values:
        text = _SPACE_RE.sub(" ", str(raw or "")).strip(" \t\r\n#*_-:;|")
        text = _HEADING_PREFIX_RE.sub("", text).strip(" \t\r\n#*_-:;|")
        key = normalize_identity(text)
        letters = re.sub(r"[^A-Za-z]", "", text)
        if (
            not key
            or key == canonical_key
            or key in _GENERIC_HEADING_KEYS
            or len(letters) < 5
            or len(text) > 180
            or _PAGE_NOISE_RE.search(text)
            or _CITATION_NOISE_RE.search(text)
        ):
            continue
        output.append(
            {
                "text": text,
                "context_key": key,
                "method": "heading_path",
            }
        )
    return _dedupe_dicts(output, ("context_key", "method"), 12)


def clean_alias(
    value: Any, *, canonical_name: str = ""
) -> tuple[str | None, str | None]:
    """Return ``(clean_alias, rejection_reason)`` for extracted alias text."""

    raw = _clean_display(value)
    if not raw:
        return None, "blank"
    if len(raw) > 120:
        return None, "too_long"
    if _CITATION_NOISE_RE.search(raw) or _PAGE_NOISE_RE.search(raw):
        return None, "citation_noise"
    if raw.isdigit():
        return None, "numeric"
    if _NUMERIC_COMBINATION_RE.fullmatch(raw):
        return None, "numeric_code"
    if len(re.sub(r"[^A-Za-z]", "", raw)) < 4 and any(char.isdigit() for char in raw):
        return None, "numeric_code"

    canonical_key = normalize_identity(canonical_name)
    alias_key = normalize_identity(raw)
    if not alias_key or alias_key == canonical_key:
        return None, "same_as_canonical"
    if _YEAR_NOISE_RE.search(raw):
        base = normalize_identity(_YEAR_NOISE_RE.sub("", raw))
        if base and (base == canonical_key or len(base.split()) <= 2):
            return None, "edition_or_citation"
    trailing_number = _TRAILING_PAGE_RE.search(raw)
    if trailing_number:
        base = normalize_identity(raw[: trailing_number.start()])
        if base and (
            base == canonical_key
            or base == _initialism(canonical_name)
            or canonical_key in base
        ):
            return None, "trailing_number_variant"
    if len(alias_key) <= 3 and not _is_abbreviation(raw):
        return None, "malformed_short_alias"
    if len(alias_key.split()) == 1 and alias_key in _IDENTITY_STOPWORDS:
        return None, "generic_alias"
    return raw, None


def clean_definition(value: Any) -> str | None:
    text = _SPACE_RE.sub(" ", str(value or "")).strip()
    if len(text) < 12 or _CITATION_NOISE_RE.search(text):
        return None
    return text[:700]


class _UnionFind:
    def __init__(self, values: Iterable[str] = ()) -> None:
        self.parent = {value: value for value in values if value}

    def add(self, value: str) -> None:
        if value:
            self.parent.setdefault(value, value)

    def find(self, value: str) -> str:
        self.add(value)
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a == b:
            return
        root, child = sorted((a, b))
        self.parent[child] = root


def _strong_identity_link(
    source: str,
    target: str,
    *,
    reciprocal: bool,
    raw_target: str = "",
) -> bool:
    """Conservative merge rule for an asserted alias targeting an entity."""

    if not source or not target or source == target:
        return False
    if target in _IDENTITY_STOPWORDS:
        return False
    if _is_abbreviation(raw_target) and _abbreviation_expands(raw_target, source):
        return True
    if _abbreviation_expands(source.upper(), target) or _abbreviation_expands(
        target.upper(), source
    ):
        return True
    source_terms = set(source.split())
    target_terms = set(target.split())
    if not source_terms or not target_terms:
        return False
    overlap = len(source_terms & target_terms)
    containment = overlap / max(1, min(len(source_terms), len(target_terms)))
    length_delta = abs(len(source_terms) - len(target_terms))
    # Reciprocal aliases still need lexical compatibility. This preserves
    # punctuation/qualifier variants while refusing to collapse related
    # concepts merely because an extractor emitted reciprocal query aliases.
    return bool(reciprocal and containment >= 0.75 and length_delta <= 2)


def _is_short_identity_key(value: Any) -> bool:
    key = normalize_identity(value)
    compact = re.sub(r"[^a-z0-9]", "", key)
    return bool(key and len(key.split()) == 1 and 1 < len(compact) <= 3)


def _ambiguous_short_identity_keys(
    source_rows: Iterable[dict[str, Any]],
) -> set[str]:
    """Find short codes that point at more than one long canonical identity.

    A shared acronym may remain an alias on each card, but it cannot be a
    union-find bridge. Otherwise ``AD`` transitively collapses advertisement,
    assistant director, and Action Descriptor into one corpus identity.
    """

    targets_by_short: dict[str, set[str]] = defaultdict(set)
    for row in source_rows:
        keys = {
            normalize_identity(value)
            for value in (row.get("canonical_keys") or [row.get("canonical_key")])
            if normalize_identity(value)
        }
        long_keys = {key for key in keys if not _is_short_identity_key(key)}
        short_keys = {key for key in keys if _is_short_identity_key(key)}
        short_keys.update(
            normalize_identity(value)
            for field in (
                "aliases_normalized",
                "abbreviations_normalized",
                "aliases",
                "abbreviations",
            )
            for value in (row.get(field) or [])
            if _is_short_identity_key(value)
        )
        for short_key in short_keys:
            if _is_short_identity_key(short_key):
                targets_by_short[short_key].update(long_keys)
        for link in row.get("identity_links") or []:
            source = normalize_identity(link.get("source"))
            target = normalize_identity(link.get("target"))
            if _is_short_identity_key(source) and target and not _is_short_identity_key(target):
                targets_by_short[source].add(target)
            elif _is_short_identity_key(target) and source and not _is_short_identity_key(source):
                targets_by_short[target].add(source)
    return {
        short_key
        for short_key, targets in targets_by_short.items()
        if len(targets) > 1
    }


def _ambiguous_document_short_identity_keys(
    canonical_keys: set[str],
    links: dict[str, list[tuple[str, str]]],
) -> set[str]:
    """Find short codes that name multiple concepts inside one document."""

    targets_by_short: dict[str, set[str]] = defaultdict(set)
    for source, values in links.items():
        for target, _surface in values:
            if target not in canonical_keys:
                continue
            if _is_short_identity_key(source) and not _is_short_identity_key(target):
                targets_by_short[source].add(target)
            elif _is_short_identity_key(target) and not _is_short_identity_key(source):
                targets_by_short[target].add(source)
    return {
        short_key
        for short_key, targets in targets_by_short.items()
        if len(targets) > 1
    }


def _entity_id(name: str) -> str:
    key = normalize_identity(name)
    return f"entity:{key.replace(' ', '-')}" if key else ""


def _dedupe_dicts(
    rows: Iterable[dict[str, Any]], keys: tuple[str, ...], cap: int
) -> list[dict[str, Any]]:
    selected: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        signature = tuple(str(row.get(key) or "") for key in keys)
        if not any(signature):
            continue
        existing = selected.get(signature)
        if existing is None or float(row.get("confidence") or 0.0) > float(
            existing.get("confidence") or 0.0
        ):
            selected[signature] = row
    return sorted(
        selected.values(),
        key=lambda row: (
            -float(row.get("confidence") or 0.0),
            tuple(str(row.get(key) or "") for key in keys),
        ),
    )[:cap]


def _contextual_usage_rank(
    row: dict[str, Any],
    *,
    identity_terms: tuple[str, ...] = (),
) -> tuple[float, str, str, str]:
    """Rank source-backed use context by retrieval utility, not insertion order."""

    text = _SPACE_RE.sub(" ", str(row.get("text") or "")).strip()
    method = str(row.get("method") or "")
    method_weight = {
        "parent_main_mechanism": 4.0,
        "parent_retrieval_use": 3.5,
        "parent_central_claim": 2.0,
    }.get(method, 0.5)
    confidence = max(0.0, min(1.0, float(row.get("confidence") or 0.0)))
    normalized_text = normalize_identity(text)
    token_count = len(set(normalized_text.split()))
    information = min(token_count, 36) / 36.0
    identity_bonus = (
        0.8
        if any(
            term and len(term) >= 3 and term in normalized_text
            for term in identity_terms
        )
        else 0.0
    )
    boilerplate_penalty = 1.25 if _CONTEXTUAL_BOILERPLATE_RE.search(text) else 0.0
    administrative_penalty = 1.6 if _CONTEXTUAL_ADMIN_RE.search(text) else 0.0
    score = (
        method_weight
        + confidence
        + information
        + identity_bonus
        - boilerplate_penalty
        - administrative_penalty
    )
    return (
        round(score, 6),
        normalized_text,
        str(row.get("parent_id") or ""),
        method,
    )


def _select_contextual_usages(
    rows: Iterable[dict[str, Any]],
    *,
    cap: int = _MAX_CONTEXTUAL_USAGES,
    identity_terms: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Keep high-information contexts while reserving representation per document."""

    normalized_identity_terms = tuple(
        dict.fromkeys(
            term
            for value in identity_terms
            for term in [normalize_identity(value)]
            if term
        )
    )

    def rank(row: dict[str, Any]) -> tuple[float, str, str, str]:
        return _contextual_usage_rank(
            row,
            identity_terms=normalized_identity_terms,
        )

    by_signature: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        signature = (
            normalize_identity(row.get("text")),
            str(row.get("parent_id") or ""),
            str(row.get("method") or ""),
        )
        if not any(signature):
            continue
        existing = by_signature.get(signature)
        if existing is None or rank(row)[0] > rank(existing)[0]:
            by_signature[signature] = row
    deduped = list(by_signature.values())
    ranked = sorted(
        deduped,
        key=lambda row: (
            -rank(row)[0],
            rank(row)[1:],
        ),
    )
    ranked = [
        row
        for row in ranked
        if not _CONTEXTUAL_ADMIN_RE.search(str(row.get("text") or ""))
    ]
    useful_ranked = [row for row in ranked if rank(row)[0] >= 2.5]
    if useful_ranked:
        ranked = useful_ranked
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    best_by_document: dict[str, dict[str, Any]] = {}
    for row in ranked:
        doc_id = str(row.get("doc_id") or "")
        if doc_id and doc_id not in best_by_document:
            best_by_document[doc_id] = row
    for row in sorted(
        best_by_document.values(),
        key=lambda item: (
            -rank(item)[0],
            rank(item)[1:],
        ),
    ):
        if len(selected) >= cap:
            break
        selected.append(row)
        selected_ids.add(id(row))
    for row in ranked:
        if len(selected) >= cap:
            break
        if id(row) in selected_ids:
            continue
        selected.append(row)
    return selected


def _new_accumulator() -> dict[str, Any]:
    return {
        "canonical_names": Counter(),
        "canonical_keys": set(),
        "aliases": set(),
        "aliases_normalized": set(),
        "abbreviations": set(),
        "abbreviations_normalized": set(),
        "alias_evidence": [],
        "definitions": [],
        "structural_contexts": [],
        "contextual_usages": [],
        "entity_types": Counter(),
        "object_kinds": Counter(),
        "entity_ids": set(),
        "source_chunk_ids": set(),
        "source_parent_ids": set(),
        "source_hashes": set(),
        "relations": [],
        "applications": [],
        "components": [],
        "component_of": [],
        "cooccurrence": Counter(),
        "support_count": 0,
        "confidence_total": 0.0,
        "quality_flags": set(),
    }


def _merge_accumulator(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in ("canonical_names", "entity_types", "object_kinds", "cooccurrence"):
        target[key].update(source[key])
    for key in (
        "canonical_keys",
        "aliases",
        "aliases_normalized",
        "abbreviations",
        "abbreviations_normalized",
        "entity_ids",
        "source_chunk_ids",
        "source_parent_ids",
        "source_hashes",
        "quality_flags",
    ):
        target[key].update(source[key])
    for key in (
        "alias_evidence",
        "definitions",
        "structural_contexts",
        "contextual_usages",
        "relations",
        "applications",
        "components",
        "component_of",
    ):
        target[key].extend(source[key])
    target["support_count"] += int(source["support_count"])
    target["confidence_total"] += float(source["confidence_total"])


def _resolve_doc_identities(
    rows: list[dict[str, Any]],
    acronym_pairs_by_chunk: dict[str, list[dict[str, str]]] | None = None,
) -> tuple[
    _UnionFind,
    dict[str, list[tuple[str, str]]],
    set[frozenset[str]],
    set[str],
]:
    canonical_keys: set[str] = set()
    links: dict[str, list[tuple[str, str]]] = defaultdict(list)
    separated_pairs: set[frozenset[str]] = set()
    acronym_pairs_by_chunk = acronym_pairs_by_chunk or {}
    for row in rows:
        chunk_pairs = acronym_pairs_by_chunk.get(str(row.get("chunk_id") or ""), [])
        for entity in row.get("entities") or []:
            source = normalize_identity(
                entity.get("canonical_name") or entity.get("surface_form")
            )
            if not source:
                continue
            canonical_keys.add(source)
            for raw_alias in entity.get("query_aliases") or []:
                alias, _ = clean_alias(raw_alias, canonical_name=source)
                if alias:
                    links[source].append((normalize_identity(alias), alias))
            for pair in chunk_pairs:
                long_form = str(pair.get("long_form") or "")
                short_form = str(pair.get("short_form") or "")
                long_key = normalize_identity(long_form)
                short_key = normalize_identity(short_form)
                if source == long_key:
                    links[source].append((short_key, short_form))
                elif source == short_key:
                    links[source].append((long_key, long_form))
        for relation in row.get("relations") or []:
            if str(relation.get("object_kind") or "entity") != "entity":
                continue
            predicate = normalize_identity(relation.get("predicate")).replace(" ", "_")
            if predicate in _IDENTITY_EQUIVALENCE_PREDICATES:
                continue
            subject = normalize_identity(relation.get("subject"))
            target = normalize_identity(relation.get("object"))
            if subject and target and subject != target:
                separated_pairs.add(frozenset((subject, target)))

    uf = _UnionFind(canonical_keys)
    ambiguous_short_keys = _ambiguous_document_short_identity_keys(
        canonical_keys,
        links,
    )
    link_pairs = {
        (source, target) for source, values in links.items() for target, _ in values
    }
    for source, values in links.items():
        for target, raw_target in values:
            if target not in canonical_keys:
                continue
            if source in ambiguous_short_keys or target in ambiguous_short_keys:
                continue
            if frozenset((source, target)) in separated_pairs:
                continue
            if _strong_identity_link(
                source,
                target,
                reciprocal=(target, source) in link_pairs,
                raw_target=raw_target,
            ):
                uf.union(source, target)
    return uf, links, separated_pairs, ambiguous_short_keys


async def build_document_lexicon_sources(
    db: Any,
    *,
    corpus_id: str,
    doc_id: str,
    candidate_artifacts: Iterable[CandidateExtractionArtifact] | None = None,
) -> list[dict[str, Any]]:
    """Build contributions through one projector for every extraction engine."""

    artifacts = list(candidate_artifacts) if candidate_artifacts is not None else None
    if artifacts is None:
        rows = (
            await db["ghost_b_extractions"]
            .find(
                {"corpus_id": corpus_id, "doc_id": doc_id, "status": "ok"},
                {
                    "_id": 0,
                    "chunk_id": 1,
                    "chunk_hash": 1,
                    "extraction_contract_hash": 1,
                    "entities": 1,
                    "relations": 1,
                    "facts": 1,
                },
            )
            .to_list(length=None)
        )
    else:
        if any(
            artifact.corpus_id != corpus_id or artifact.doc_id != doc_id
            for artifact in artifacts
        ):
            raise ValueError("candidate artifact ownership escapes document scope")
        chunk_ids = [artifact.chunk_id for artifact in artifacts]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("candidate artifacts contain duplicate chunk identities")
        if any(
            artifact.provenance.shared_contract_hash != CANDIDATE_EXTRACTION_SCHEMA_HASH
            for artifact in artifacts
        ):
            raise ValueError("candidate artifact shared contract hash drifted")
        rows = [candidate_artifact_to_lexicon_row(artifact) for artifact in artifacts]
    if not rows:
        return []

    chunk_context = {
        str(row.get("chunk_id") or ""): {
            "parent_id": str(row.get("parent_id") or ""),
            "text": str(row.get("text") or ""),
            "heading_path": list(row.get("heading_path") or []),
        }
        async for row in db["chunks"].find(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {
                "_id": 0,
                "chunk_id": 1,
                "parent_id": 1,
                "text": 1,
                "heading_path": 1,
            },
        )
    }
    if artifacts is not None:
        for artifact in artifacts:
            context = chunk_context.get(artifact.chunk_id)
            if context is None:
                raise ValueError("candidate artifact chunk is absent from the document")
            current_hash = (
                "sha256:"
                + hashlib.sha256(
                    str(context.get("text") or "").encode("utf-8")
                ).hexdigest()
            )
            if artifact.source_text_sha256 != current_hash:
                raise ValueError("candidate artifact source text is stale")
    parent_ids = {
        str(context.get("parent_id") or "")
        for context in chunk_context.values()
        if str(context.get("parent_id") or "")
    }
    try:
        parent_collection = db["parent_chunks"]
    except (KeyError, TypeError):
        parent_collection = None
    parent_context = (
        {
            str(row.get("parent_id") or ""): row
            async for row in parent_collection.find(
                {
                    "corpus_id": corpus_id,
                    "doc_id": doc_id,
                    "parent_id": {"$in": sorted(parent_ids)},
                },
                {
                    "_id": 0,
                    "parent_id": 1,
                    "central_claim": 1,
                    "main_mechanism": 1,
                    "retrieval_uses": 1,
                    "quality_score": 1,
                    "validation_status": 1,
                },
            )
        }
        if parent_collection is not None and parent_ids
        else {}
    )
    acronym_pairs_by_chunk = {
        chunk_id: mine_acronym_pairs(context.get("text"))
        for chunk_id, context in chunk_context.items()
    }
    uf, alias_links, separated_pairs, ambiguous_short_keys = _resolve_doc_identities(
        rows,
        acronym_pairs_by_chunk,
    )
    members_by_root: dict[str, set[str]] = defaultdict(set)
    for canonical_key in uf.parent:
        members_by_root[uf.find(canonical_key)].add(canonical_key)

    def relation_conflicts_with_identity(source_key: str, target_key: str) -> bool:
        source_members = members_by_root.get(
            uf.find(source_key),
            {source_key},
        )
        return any(
            frozenset((member, target_key)) in separated_pairs
            for member in source_members
        )

    acc_by_root: dict[str, dict[str, Any]] = defaultdict(_new_accumulator)
    name_to_roots: dict[str, set[str]] = defaultdict(set)

    def add_identity_alias(
        *,
        acc: dict[str, Any],
        source_key: str,
        root: str,
        canonical_name: str,
        raw_alias: Any,
        method: str,
        chunk_id: str,
        parent_id: str,
        confidence: float,
        evidence: str = "",
    ) -> bool:
        alias, reason = clean_alias(raw_alias, canonical_name=canonical_name)
        if not alias:
            if reason not in {"blank", "same_as_canonical"}:
                acc["quality_flags"].add(f"rejected_alias:{reason}")
            return False
        alias_key = normalize_identity(alias)
        if relation_conflicts_with_identity(source_key, alias_key):
            acc["quality_flags"].add("rejected_alias:relation_conflict")
            return False
        if (
            _is_abbreviation(alias)
            and source_key != alias_key
            and not _abbreviation_expands(alias, canonical_name)
        ):
            acc["quality_flags"].add("rejected_alias:abbreviation_scope_mismatch")
            return False
        acc["aliases"].add(alias)
        acc["aliases_normalized"].add(alias_key)
        name_to_roots[alias_key].add(root)
        if _is_abbreviation(alias):
            acc["abbreviations"].add(alias)
            acc["abbreviations_normalized"].add(alias_key)
        acc["alias_evidence"].append(
            {
                "alias": alias,
                "alias_key": alias_key,
                "method": method,
                "chunk_id": chunk_id,
                "parent_id": parent_id,
                "confidence": round(confidence, 4),
                "evidence": _SPACE_RE.sub(" ", evidence).strip()[:240],
            }
        )
        return True

    for row in rows:
        chunk_id = str(row.get("chunk_id") or "")
        context = chunk_context.get(chunk_id) or {}
        parent_id = str(context.get("parent_id") or "")
        chunk_text = str(context.get("text") or "")
        source_hash = str(
            row.get("chunk_hash") or row.get("extraction_contract_hash") or ""
        )
        row_roots: set[str] = set()
        for entity in row.get("entities") or []:
            raw_name = _clean_display(
                entity.get("canonical_name") or entity.get("surface_form")
            )
            key = normalize_identity(raw_name)
            if not key:
                continue
            root = uf.find(key)
            row_roots.add(root)
            name_to_roots[key].add(root)
            acc = acc_by_root[root]
            if key in ambiguous_short_keys or any(
                target in ambiguous_short_keys
                for target, _surface in alias_links.get(key, [])
            ):
                acc["quality_flags"].add("ambiguous_short_identity")
            acc["canonical_names"][raw_name] += 1
            acc["canonical_keys"].add(key)
            acc["support_count"] += 1
            confidence = max(0.0, min(1.0, float(entity.get("confidence") or 0.0)))
            acc["confidence_total"] += confidence
            if chunk_id:
                acc["source_chunk_ids"].add(chunk_id)
            if parent_id:
                acc["source_parent_ids"].add(parent_id)
            if source_hash:
                acc["source_hashes"].add(source_hash)
            entity_type = _clean_display(entity.get("entity_type"))
            object_kind = _clean_display(entity.get("object_kind"))
            if entity_type:
                acc["entity_types"][entity_type] += 1
            if object_kind:
                acc["object_kinds"][object_kind] += 1
            entity_id = str(entity.get("entity_id") or _entity_id(raw_name))
            if entity_id:
                acc["entity_ids"].add(entity_id)

            definition = clean_definition(entity.get("definitional_phrase"))
            if definition:
                acc["definitions"].append(
                    {
                        "text": definition,
                        "chunk_id": chunk_id,
                        "parent_id": parent_id,
                        "confidence": confidence,
                        "method": "extraction_definitional_phrase",
                    }
                )
            for raw_alias in entity.get("query_aliases") or []:
                add_identity_alias(
                    acc=acc,
                    source_key=key,
                    root=root,
                    canonical_name=raw_name,
                    raw_alias=raw_alias,
                    method="extraction_query_alias",
                    chunk_id=chunk_id,
                    parent_id=parent_id,
                    confidence=confidence,
                )

            surface = _clean_display(entity.get("surface_form"))
            if surface and normalize_identity(surface) != key:
                surface_alias, reason = clean_alias(surface, canonical_name=raw_name)
                if surface_alias:
                    surface_key = normalize_identity(surface_alias)
                    # Surface forms are identity aliases only when they retain
                    # the canonical phrase or explicitly carry an abbreviation.
                    if (
                        key in surface_key
                        or surface_key in key
                        or _abbreviation_expands(surface_alias, raw_name)
                    ) and not relation_conflicts_with_identity(key, surface_key):
                        add_identity_alias(
                            acc=acc,
                            source_key=key,
                            root=root,
                            canonical_name=raw_name,
                            raw_alias=surface_alias,
                            method="extraction_surface_form",
                            chunk_id=chunk_id,
                            parent_id=parent_id,
                            confidence=confidence,
                        )

            deterministic = mine_entity_text_evidence(
                chunk_text,
                raw_name,
                acronym_pairs=acronym_pairs_by_chunk.get(chunk_id, []),
            )
            for item in deterministic["aliases"]:
                add_identity_alias(
                    acc=acc,
                    source_key=key,
                    root=root,
                    canonical_name=raw_name,
                    raw_alias=item.get("alias"),
                    method=str(item.get("method") or "deterministic_alias"),
                    chunk_id=chunk_id,
                    parent_id=parent_id,
                    confidence=1.0,
                    evidence=str(item.get("evidence") or ""),
                )
            for item in deterministic["definitions"]:
                definition = clean_definition(item.get("text"))
                if definition:
                    acc["definitions"].append(
                        {
                            "text": definition,
                            "chunk_id": chunk_id,
                            "parent_id": parent_id,
                            "confidence": 1.0,
                            "method": str(
                                item.get("method") or "deterministic_definition"
                            ),
                        }
                    )

            for item in mine_structural_contexts(
                context.get("heading_path") or [],
                raw_name,
            ):
                acc["structural_contexts"].append(
                    {
                        **item,
                        "chunk_id": chunk_id,
                        "parent_id": parent_id,
                        "confidence": 1.0,
                    }
                )

            parent_usage = parent_context.get(parent_id) or {}
            if str(parent_usage.get("validation_status") or "valid") == "valid":
                quality = max(
                    0.0,
                    min(1.0, float(parent_usage.get("quality_score") or confidence)),
                )
                usage_values = [
                    (
                        "parent_retrieval_use",
                        str(value or ""),
                    )
                    for value in (parent_usage.get("retrieval_uses") or [])
                ]
                usage_values.extend(
                    [
                        (
                            "parent_central_claim",
                            str(parent_usage.get("central_claim") or ""),
                        ),
                        (
                            "parent_main_mechanism",
                            str(parent_usage.get("main_mechanism") or ""),
                        ),
                    ]
                )
                for method, value in usage_values:
                    contextual_text = _SPACE_RE.sub(" ", value).strip()
                    if len(contextual_text) < 12:
                        continue
                    acc["contextual_usages"].append(
                        {
                            "text": contextual_text[:500],
                            "method": method,
                            "chunk_id": chunk_id,
                            "parent_id": parent_id,
                            "confidence": round(quality, 4),
                        }
                    )

        for left, right in combinations(sorted(row_roots)[:24], 2):
            acc_by_root[left]["cooccurrence"][right] += 1
            acc_by_root[right]["cooccurrence"][left] += 1

    def resolve_endpoint(value: Any) -> str:
        key = normalize_identity(value)
        roots = name_to_roots.get(key) or set()
        return (
            next(iter(roots))
            if len(roots) == 1
            else uf.find(key) if key in uf.parent else key
        )

    for row in rows:
        chunk_id = str(row.get("chunk_id") or "")
        parent_id = str((chunk_context.get(chunk_id) or {}).get("parent_id") or "")
        for relation in row.get("relations") or []:
            subject_key = resolve_endpoint(relation.get("subject"))
            object_label = _clean_display(relation.get("object"))
            object_key = resolve_endpoint(object_label)
            predicate = normalize_identity(relation.get("predicate")).replace(" ", "_")
            confidence = max(0.0, min(1.0, float(relation.get("confidence") or 0.0)))
            evidence = _SPACE_RE.sub(
                " ", str(relation.get("evidence_phrase") or "")
            ).strip()[:500]
            object_is_entity = str(relation.get("object_kind") or "entity") == "entity"
            for current, direction, neighbor_key, neighbor_label in (
                (subject_key, "outgoing", object_key, object_label),
                (
                    object_key,
                    "incoming",
                    subject_key,
                    _clean_display(relation.get("subject")),
                ),
            ):
                if current not in acc_by_root or not predicate:
                    continue
                if direction == "incoming" and not object_is_entity:
                    continue
                rel = {
                    "predicate": predicate,
                    "direction": direction,
                    "target_key": neighbor_key,
                    "target": neighbor_label,
                    "chunk_id": chunk_id,
                    "parent_id": parent_id,
                    "confidence": confidence,
                    "evidence_phrase": evidence,
                }
                acc_by_root[current]["relations"].append(rel)
                if predicate in _APPLICATION_PREDICATES:
                    acc_by_root[current]["applications"].append(rel)
            if predicate == "part_of" and subject_key in acc_by_root:
                acc_by_root[subject_key]["component_of"].append(
                    {
                        "target_key": object_key,
                        "target": object_label,
                        "chunk_id": chunk_id,
                        "confidence": confidence,
                    }
                )
            if predicate == "part_of" and object_key in acc_by_root:
                acc_by_root[object_key]["components"].append(
                    {
                        "target_key": subject_key,
                        "target": _clean_display(relation.get("subject")),
                        "chunk_id": chunk_id,
                        "confidence": confidence,
                    }
                )

    now = utcnow()
    documents: list[dict[str, Any]] = []
    for root, acc in sorted(acc_by_root.items()):
        canonical_names = [
            {"value": name, "count": count}
            for name, count in sorted(
                acc["canonical_names"].items(),
                key=lambda item: (-item[1], item[0].lower()),
            )
        ]
        documents.append(
            {
                "schema_version": LEXICON_SOURCE_VERSION,
                "source_id": hashlib.sha256(
                    f"{corpus_id}:{doc_id}:{root}".encode("utf-8")
                ).hexdigest(),
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "canonical_key": root,
                "canonical_keys": sorted(acc["canonical_keys"]),
                "canonical_names": canonical_names,
                "aliases": sorted(acc["aliases"], key=str.lower),
                "aliases_normalized": sorted(acc["aliases_normalized"]),
                "abbreviations": sorted(acc["abbreviations"]),
                "abbreviations_normalized": sorted(acc["abbreviations_normalized"]),
                "alias_evidence": _dedupe_dicts(
                    acc["alias_evidence"],
                    ("alias_key", "method", "chunk_id"),
                    96,
                ),
                "definitions": _dedupe_dicts(
                    acc["definitions"], ("text", "chunk_id"), _MAX_DEFINITIONS
                ),
                "structural_contexts": _dedupe_dicts(
                    acc["structural_contexts"],
                    ("context_key", "chunk_id", "parent_id"),
                    _MAX_STRUCTURAL_CONTEXTS,
                ),
                "contextual_usages": _select_contextual_usages(
                    acc["contextual_usages"],
                    identity_terms=[
                        *acc["canonical_names"].keys(),
                        *acc["aliases"],
                        *acc["abbreviations"],
                    ],
                ),
                "entity_types": [
                    name for name, _ in acc["entity_types"].most_common(8)
                ],
                "object_kinds": [
                    name for name, _ in acc["object_kinds"].most_common(8)
                ],
                "entity_ids": sorted(acc["entity_ids"])[:_MAX_ENTITY_IDS],
                "source_chunk_ids": sorted(acc["source_chunk_ids"])[
                    :_MAX_SOURCE_CHUNKS
                ],
                "source_chunk_count": len(acc["source_chunk_ids"]),
                "source_parent_ids": sorted(acc["source_parent_ids"])[
                    :_MAX_SOURCE_PARENTS
                ],
                "source_parent_count": len(acc["source_parent_ids"]),
                "source_hashes": sorted(acc["source_hashes"])[:_MAX_SOURCE_CHUNKS],
                "relations": _dedupe_dicts(
                    acc["relations"],
                    ("predicate", "direction", "target_key", "chunk_id"),
                    _MAX_RELATIONS,
                ),
                "application_contexts": _dedupe_dicts(
                    acc["applications"],
                    ("predicate", "direction", "target_key", "chunk_id"),
                    32,
                ),
                "components": _dedupe_dicts(
                    acc["components"], ("target_key", "chunk_id"), 32
                ),
                "component_of": _dedupe_dicts(
                    acc["component_of"], ("target_key", "chunk_id"), 32
                ),
                "cooccurrence_counts": dict(acc["cooccurrence"].most_common(96)),
                "support_count": int(acc["support_count"]),
                "mean_confidence": round(
                    float(acc["confidence_total"]) / max(1, int(acc["support_count"])),
                    4,
                ),
                "quality_flags": sorted(acc["quality_flags"]),
                "identity_links": [
                    {"source": source, "target": target, "surface": surface}
                    for source in sorted(acc["canonical_keys"])
                    for target, surface in alias_links.get(source, [])
                    if not relation_conflicts_with_identity(source, target)
                ][:64],
                "updated_at": now,
            }
        )
    return documents


def _choose_canonical_name(rows: list[dict[str, Any]]) -> tuple[str, str]:
    counts: Counter[str] = Counter()
    for row in rows:
        for item in row.get("canonical_names") or []:
            value = _clean_display(item.get("value"))
            if value:
                counts[value] += max(1, int(item.get("count") or 1))
    if not counts:
        key = min((str(row.get("canonical_key") or "") for row in rows), default="")
        return key, key

    observed_keys = {normalize_identity(name) for name in counts}

    def score(item: tuple[str, int]) -> tuple[int, int, int, int, str]:
        name, count = item
        key = normalize_identity(name)
        tokens = key.split()
        abbreviation = _is_abbreviation(name)
        trailing_page = bool(
            len(tokens) >= 5
            and _TRAILING_IDENTITY_CODE_RE.fullmatch(tokens[-1])
            and " ".join(tokens[:-1]) in observed_keys
        )
        return (
            0 if trailing_page else 1,
            0 if abbreviation else 1,
            min(len(tokens), 8),
            count,
            name.lower(),
        )

    selected = max(counts.items(), key=score)[0]
    return _clean_display(selected), normalize_identity(selected)


def _natural_language_label(value: Any) -> str:
    return _SPACE_RE.sub(" ", _clean_display(value).replace("_", " ")).strip()


def _useful_target(value: Any) -> str:
    target = _natural_language_label(value)
    letters = re.sub(r"[^A-Za-z]", "", target)
    return target if len(letters) >= 4 else ""


def _functional_contextual_usage_texts(entry: dict[str, Any]) -> list[str]:
    """Return only summary fields whose schema explicitly represents utility."""

    return [
        text
        for item in (entry.get("contextual_usages") or [])
        if str(item.get("method") or "")
        in {"parent_main_mechanism", "parent_retrieval_use"}
        for text in [_natural_language_label(item.get("text"))]
        if text
    ]


def _build_embedding_gloss(entry: dict[str, Any]) -> str:
    """Build concise vector text centered on source-backed meaning."""

    canonical = _natural_language_label(entry.get("canonical_name"))
    parts = [canonical]
    aliases = list(entry.get("abbreviations") or []) + list(entry.get("aliases") or [])
    clean_aliases = [
        _natural_language_label(alias)
        for alias in dict.fromkeys(aliases)
        if normalize_identity(alias) != entry.get("canonical_key")
        and clean_alias(alias, canonical_name=canonical)[0]
    ]
    if clean_aliases:
        parts.append("Also called " + ", ".join(clean_aliases[:3]) + ".")
    definitions = entry.get("definitions") or []
    for definition in definitions[:2]:
        text = clean_definition(definition.get("text"))
        if text:
            parts.append(text.rstrip(".") + ".")
    structural_contexts = [
        _natural_language_label(item.get("text"))
        for item in (entry.get("structural_contexts") or [])
        if _natural_language_label(item.get("text"))
    ]
    if structural_contexts:
        parts.append(
            "Discussed under " + "; ".join(dict.fromkeys(structural_contexts[:3])) + "."
        )
    contextual_usages = _functional_contextual_usage_texts(entry)
    if contextual_usages:
        parts.append(
            "Useful for " + "; ".join(dict.fromkeys(contextual_usages[:3])) + "."
        )
    applications = [
        (
            str(item.get("predicate") or "").replace("_", " ").strip(),
            _useful_target(item.get("target")),
        )
        for item in (entry.get("application_contexts") or [])
    ]
    application_phrases = [
        f"{predicate} {target}".strip() for predicate, target in applications if target
    ][:3]
    if application_phrases:
        parts.append("Applications: " + "; ".join(application_phrases) + ".")
    if not definitions and not application_phrases:
        components = [
            _useful_target(item.get("target"))
            for item in (entry.get("components") or [])
            if _useful_target(item.get("target"))
        ][:4]
        if components:
            parts.append("Includes " + ", ".join(components) + ".")
    if len(parts) == 1:
        related = [
            _useful_target(item.get("target"))
            for item in (entry.get("related_concepts") or [])
            if _useful_target(item.get("target"))
        ][:3]
        if related:
            parts.append("Related to " + ", ".join(related) + ".")
    return _SPACE_RE.sub(" ", " ".join(part for part in parts if part)).strip()[:900]


def _build_utility_gloss(entry: dict[str, Any]) -> str:
    """Describe source-backed functional use without inventing target products."""

    parts: list[str] = []
    usages = _functional_contextual_usage_texts(entry)
    if usages:
        parts.append("Useful for " + "; ".join(dict.fromkeys(usages[:6])) + ".")
    applications = [
        (
            f"{str(item.get('predicate') or '').replace('_', ' ')} "
            f"{_useful_target(item.get('target'))}"
        ).strip()
        for item in (entry.get("application_contexts") or [])
        if _useful_target(item.get("target"))
    ]
    if applications:
        parts.append("Applies to " + "; ".join(applications[:5]) + ".")
    contexts = [
        _natural_language_label(item.get("text"))
        for item in (entry.get("structural_contexts") or [])
        if _natural_language_label(item.get("text"))
    ]
    if contexts:
        parts.append("Context: " + "; ".join(dict.fromkeys(contexts[:4])) + ".")
    if not parts:
        return ""
    canonical = _natural_language_label(entry.get("canonical_name"))
    if canonical:
        parts.insert(0, canonical + ".")
    return _SPACE_RE.sub(" ", " ".join(parts)).strip()[:900]


def _build_retrieval_gloss(entry: dict[str, Any]) -> str:
    parts = [str(entry.get("canonical_name") or "").strip()]
    aliases = list(entry.get("abbreviations") or []) + list(entry.get("aliases") or [])
    aliases = [
        alias
        for alias in dict.fromkeys(aliases)
        if normalize_identity(alias) != entry.get("canonical_key")
        and clean_alias(alias, canonical_name=str(entry.get("canonical_name") or ""))[0]
    ]
    if aliases:
        parts.append("Also called " + ", ".join(aliases[:4]) + ".")
    definitions = entry.get("definitions") or []
    if definitions:
        parts.append(
            "Source definition: "
            + str(definitions[0].get("text") or "").rstrip(".")
            + "."
        )
    structural_contexts = [
        _natural_language_label(item.get("text"))
        for item in (entry.get("structural_contexts") or [])
        if _natural_language_label(item.get("text"))
    ]
    if structural_contexts:
        parts.append(
            "Source structure: "
            + "; ".join(dict.fromkeys(structural_contexts[:4]))
            + "."
        )
    contextual_usages = [
        _natural_language_label(item.get("text"))
        for item in (entry.get("contextual_usages") or [])
        if _natural_language_label(item.get("text"))
    ]
    if contextual_usages:
        parts.append(
            "Source-backed contextual uses: "
            + "; ".join(dict.fromkeys(contextual_usages[:4]))
            + "."
        )
    applications = entry.get("application_contexts") or []
    if applications:
        values = [
            f"{item.get('predicate', '').replace('_', ' ')} {_useful_target(item.get('target'))}".strip()
            for item in applications[:4]
            if _useful_target(item.get("target"))
        ]
        if any(values):
            parts.append(
                "Source-backed applications: "
                + "; ".join(value for value in values if value)
                + "."
            )
    components = entry.get("components") or []
    if components:
        values = [
            _useful_target(item.get("target"))
            for item in components
            if _useful_target(item.get("target"))
        ]
        if values:
            parts.append("Source components: " + ", ".join(values[:6]) + ".")
    related = entry.get("related_concepts") or []
    related_values = [
        _useful_target(item.get("target"))
        for item in related
        if _useful_target(item.get("target"))
    ]
    if related_values:
        parts.append(
            "Related source concepts: " + ", ".join(related_values[:6])[:500] + "."
        )
    return _SPACE_RE.sub(" ", " ".join(part for part in parts if part)).strip()[:1800]


def _refreshed_gloss_fields(entry: dict[str, Any]) -> dict[str, Any]:
    """Re-render one materialized concept without replaying source reconciliation."""

    contextual_usages = _select_contextual_usages(
        entry.get("contextual_usages") or [],
        identity_terms=[
            entry.get("canonical_name"),
            *(entry.get("aliases") or []),
            *(entry.get("abbreviations") or []),
        ],
    )
    refreshed = {**entry, "contextual_usages": contextual_usages}
    return {
        "contextual_usages": contextual_usages,
        "utility_gloss": _build_utility_gloss(refreshed),
        "embedding_gloss": _build_embedding_gloss(refreshed),
        "retrieval_gloss": _build_retrieval_gloss(refreshed),
    }


def materialize_entries(
    source_rows: list[dict[str, Any]], corpus_id: str
) -> list[dict[str, Any]]:
    """Pure corpus reconciliation used by backfill, incremental repair, and tests."""

    canonical_keys = {
        key
        for row in source_rows
        for key in (row.get("canonical_keys") or [row.get("canonical_key")])
        if key
    }
    uf = _UnionFind(canonical_keys)
    ambiguous_short_keys = _ambiguous_short_identity_keys(source_rows)
    separated_pairs: set[frozenset[str]] = set()
    for row in source_rows:
        source_keys = {
            str(value)
            for value in (row.get("canonical_keys") or [row.get("canonical_key")])
            if value
        }
        for field in ("components", "component_of"):
            for relation in row.get(field) or []:
                target = str(relation.get("target_key") or "")
                for source in source_keys:
                    if source and target and source != target:
                        separated_pairs.add(frozenset((source, target)))
        for relation in row.get("relations") or []:
            predicate = normalize_identity(relation.get("predicate")).replace(" ", "_")
            if predicate in _IDENTITY_EQUIVALENCE_PREDICATES:
                continue
            target = str(relation.get("target_key") or "")
            for source in source_keys:
                if source and target and source != target:
                    separated_pairs.add(frozenset((source, target)))
    reconciled_trailing_code_keys: set[str] = set()
    trailing_variants: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for key in canonical_keys:
        tokens = key.split()
        if len(tokens) < 5 or not _TRAILING_IDENTITY_CODE_RE.fullmatch(tokens[-1]):
            continue
        base = " ".join(tokens[:-1])
        if len(base.split()) >= 4 and base in canonical_keys:
            numeric = int(re.match(r"\d+", tokens[-1]).group(0))
            trailing_variants[base].append((key, numeric))
    for base, variants in trailing_variants.items():
        for key, numeric in variants:
            if len(variants) < 2 and numeric <= 9:
                continue
            uf.union(base, key)
            reconciled_trailing_code_keys.add(key)
    links = {
        (str(link.get("source") or ""), str(link.get("target") or ""))
        for row in source_rows
        for link in (row.get("identity_links") or [])
    }
    for row in source_rows:
        keys = [key for key in (row.get("canonical_keys") or []) if key]
        unionable_keys = [key for key in keys if key not in ambiguous_short_keys]
        for key in unionable_keys[1:]:
            uf.union(unionable_keys[0], key)
        for link in row.get("identity_links") or []:
            source = str(link.get("source") or "")
            target = str(link.get("target") or "")
            surface = str(link.get("surface") or "")
            if (
                target in canonical_keys
                and source not in ambiguous_short_keys
                and target not in ambiguous_short_keys
                and frozenset((source, target)) not in separated_pairs
                and _strong_identity_link(
                    source,
                    target,
                    reciprocal=(target, source) in links,
                    raw_target=surface,
                )
            ):
                uf.union(source, target)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source_rows:
        key = str(row.get("canonical_key") or "")
        if key in ambiguous_short_keys:
            key = next(
                (
                    str(value)
                    for value in (row.get("canonical_keys") or [])
                    if str(value) and str(value) not in ambiguous_short_keys
                ),
                key,
            )
        if key:
            grouped[uf.find(key)].append(row)

    prelim: dict[str, dict[str, Any]] = {}
    root_for_key = {key: uf.find(key) for key in canonical_keys}
    for root, rows in grouped.items():
        canonical_name, canonical_key = _choose_canonical_name(rows)
        support_count = sum(int(row.get("support_count") or 0) for row in rows)
        confidence_total = sum(
            float(row.get("mean_confidence") or 0.0)
            * int(row.get("support_count") or 0)
            for row in rows
        )
        member_keys = sorted(
            {
                key
                for row in rows
                for key in (row.get("canonical_keys") or [row.get("canonical_key")])
                if key
            }
        )
        aliases: set[str] = set()
        rejected_relation_alias = False
        for value in (
            value
            for row in rows
            for value in [
                *(row.get("aliases") or []),
                *(item.get("value") for item in (row.get("canonical_names") or [])),
            ]
        ):
            cleaned, _reason = clean_alias(
                value,
                canonical_name=canonical_name,
            )
            if cleaned:
                alias_key = normalize_identity(cleaned)
                if any(
                    frozenset((member_key, alias_key)) in separated_pairs
                    for member_key in member_keys
                ):
                    rejected_relation_alias = True
                    continue
                aliases.add(cleaned)
        aliases = {
            value for value in aliases if normalize_identity(value) != canonical_key
        }
        abbreviations = {
            value
            for row in rows
            for value in (row.get("abbreviations") or [])
            if _is_abbreviation(str(value))
        }
        alias_evidence = _dedupe_dicts(
            (item for row in rows for item in (row.get("alias_evidence") or [])),
            ("alias_key", "method", "chunk_id"),
            128,
        )
        definitions = _dedupe_dicts(
            (item for row in rows for item in (row.get("definitions") or [])),
            ("text", "chunk_id"),
            _MAX_DEFINITIONS,
        )
        structural_contexts = _dedupe_dicts(
            (item for row in rows for item in (row.get("structural_contexts") or [])),
            ("context_key", "chunk_id", "parent_id"),
            _MAX_STRUCTURAL_CONTEXTS,
        )
        contextual_usages = _select_contextual_usages(
            (
                {
                    **item,
                    "doc_id": str(item.get("doc_id") or row.get("doc_id") or ""),
                }
                for row in rows
                for item in (row.get("contextual_usages") or [])
            ),
            identity_terms=[canonical_name, *aliases, *abbreviations],
        )
        component_key = canonical_key or root
        document_support: dict[str, dict[str, Any]] = {}
        for row in rows:
            doc_id = str(row.get("doc_id") or "")
            if not doc_id:
                continue
            current = document_support.setdefault(
                doc_id,
                {
                    "doc_id": doc_id,
                    "support_count": 0,
                    "source_chunk_count": 0,
                    "source_parent_count": 0,
                },
            )
            current["support_count"] += int(row.get("support_count") or 0)
            current["source_chunk_count"] += int(row.get("source_chunk_count") or 0)
            current["source_parent_count"] += int(row.get("source_parent_count") or 0)
        lexicon_id = hashlib.sha256(
            f"{corpus_id}:{component_key}".encode("utf-8")
        ).hexdigest()
        prelim[root] = {
            "schema_version": LEXICON_SCHEMA_VERSION,
            "lexicon_id": lexicon_id,
            "corpus_id": corpus_id,
            "canonical_name": canonical_name,
            "canonical_key": canonical_key,
            "member_keys": member_keys,
            "aliases": sorted(aliases, key=str.lower),
            "aliases_normalized": sorted(
                {normalize_identity(value) for value in aliases}
            ),
            "abbreviations": sorted(abbreviations),
            "abbreviations_normalized": sorted(
                {normalize_identity(value) for value in abbreviations}
            ),
            "alias_evidence": alias_evidence,
            "definitions": definitions,
            "structural_contexts": structural_contexts,
            "contextual_usages": contextual_usages,
            "entity_types": list(
                dict.fromkeys(
                    value for row in rows for value in (row.get("entity_types") or [])
                )
            )[:12],
            "object_kinds": list(
                dict.fromkeys(
                    value for row in rows for value in (row.get("object_kinds") or [])
                )
            )[:12],
            "entity_ids": sorted(
                {value for row in rows for value in (row.get("entity_ids") or [])}
            )[:_MAX_ENTITY_IDS],
            "source_document_ids": sorted(
                {str(row.get("doc_id")) for row in rows if row.get("doc_id")}
            ),
            "source_document_support": sorted(
                document_support.values(),
                key=lambda item: (
                    -int(item["support_count"]),
                    -int(item["source_chunk_count"]),
                    str(item["doc_id"]),
                ),
            )[:128],
            "source_chunk_ids": sorted(
                {value for row in rows for value in (row.get("source_chunk_ids") or [])}
            )[:_MAX_SOURCE_CHUNKS],
            "source_chunk_count": sum(
                int(row.get("source_chunk_count") or 0) for row in rows
            ),
            "source_parent_ids": sorted(
                {
                    value
                    for row in rows
                    for value in (row.get("source_parent_ids") or [])
                }
            )[:_MAX_SOURCE_PARENTS],
            "source_parent_count": sum(
                int(row.get("source_parent_count") or 0) for row in rows
            ),
            "source_hashes": sorted(
                {value for row in rows for value in (row.get("source_hashes") or [])}
            )[:_MAX_SOURCE_CHUNKS],
            "support_count": support_count,
            "mean_confidence": round(confidence_total / max(1, support_count), 4),
            "quality_flags": sorted(
                {value for row in rows for value in (row.get("quality_flags") or [])}
                | (
                    {"reconciled_trailing_identity_code"}
                    if any(
                        key in reconciled_trailing_code_keys
                        for row in rows
                        for key in (row.get("canonical_keys") or [])
                    )
                    else set()
                )
                | (
                    {"rejected_alias:relation_conflict"}
                    if rejected_relation_alias
                    else set()
                )
                | (
                    {"ambiguous_short_identity"}
                    if any(key in ambiguous_short_keys for key in member_keys)
                    else set()
                )
            ),
            "_source_rows": rows,
        }

    canonical_by_root = {
        root: (entry["canonical_name"], entry["canonical_key"], entry["support_count"])
        for root, entry in prelim.items()
    }
    entries: list[dict[str, Any]] = []
    for root, entry in prelim.items():
        rows = entry.pop("_source_rows")
        relations: list[dict[str, Any]] = []
        applications: list[dict[str, Any]] = []
        components: list[dict[str, Any]] = []
        component_of: list[dict[str, Any]] = []
        cooccurrence: Counter[str] = Counter()
        for row in rows:
            for raw in row.get("relations") or []:
                target_key = str(raw.get("target_key") or "")
                target_root = root_for_key.get(target_key, target_key)
                if target_root == root:
                    continue
                target_name = canonical_by_root.get(
                    target_root, (raw.get("target") or target_key, "", 0)
                )[0]
                relation = {
                    **raw,
                    "target_key": target_key,
                    "target_lexicon_key": target_root,
                    "target": target_name,
                }
                relations.append(relation)
            for raw in row.get("application_contexts") or []:
                target_key = str(raw.get("target_key") or "")
                target_root = root_for_key.get(target_key, target_key)
                if target_root == root:
                    continue
                target_name = canonical_by_root.get(
                    target_root, (raw.get("target") or target_key, "", 0)
                )[0]
                applications.append(
                    {**raw, "target_lexicon_key": target_root, "target": target_name}
                )
            for field, bucket in (
                ("components", components),
                ("component_of", component_of),
            ):
                for raw in row.get(field) or []:
                    target_key = str(raw.get("target_key") or "")
                    target_root = root_for_key.get(target_key, target_key)
                    if target_root == root:
                        continue
                    target_name = canonical_by_root.get(
                        target_root, (raw.get("target") or target_key, "", 0)
                    )[0]
                    bucket.append(
                        {
                            **raw,
                            "target_lexicon_key": target_root,
                            "target": target_name,
                        }
                    )
            for target_key, count in (row.get("cooccurrence_counts") or {}).items():
                target_root = root_for_key.get(str(target_key), str(target_key))
                if target_root != root:
                    cooccurrence[target_root] += int(count or 0)

        entry["factual_relations"] = _dedupe_dicts(
            relations,
            ("predicate", "direction", "target_lexicon_key", "chunk_id"),
            _MAX_RELATIONS,
        )
        entry["application_contexts"] = _dedupe_dicts(
            applications,
            ("predicate", "direction", "target_lexicon_key", "chunk_id"),
            32,
        )
        entry["components"] = _dedupe_dicts(
            components, ("target_lexicon_key", "chunk_id"), 32
        )
        entry["component_of"] = _dedupe_dicts(
            component_of, ("target_lexicon_key", "chunk_id"), 32
        )
        entry["related_concepts"] = _dedupe_dicts(
            relations, ("target_lexicon_key", "predicate", "direction"), _MAX_NEIGHBORS
        )
        neighbors: list[dict[str, Any]] = []
        for target_root, count in cooccurrence.most_common(_MAX_NEIGHBORS):
            target_name, target_key, target_support = canonical_by_root.get(
                target_root, (target_root, target_root, count)
            )
            neighbors.append(
                {
                    "target_lexicon_key": target_root,
                    "target_key": target_key,
                    "target": target_name,
                    "support_count": int(count),
                    "association": "chunk_cooccurrence",
                    "weight": round(
                        int(count)
                        / math.sqrt(
                            max(1, int(entry["support_count"]))
                            * max(1, int(target_support))
                        ),
                        6,
                    ),
                    "directional": False,
                    "factual": False,
                }
            )
        entry["cooccurrence_neighbors"] = neighbors
        entry["semantic_neighbors"] = []
        entry["utility_gloss"] = _build_utility_gloss(entry)
        entry["embedding_gloss"] = _build_embedding_gloss(entry)
        entry["retrieval_gloss"] = _build_retrieval_gloss(entry)
        compact_key = re.sub(r"[^a-z0-9]", "", str(entry["canonical_key"]))
        low_information_identity = bool(
            len(str(entry["canonical_key"]).split()) == 1
            and len(compact_key) <= 3
            and int(entry["support_count"]) < 3
            and not entry["definitions"]
            and not any(
                len(normalize_identity(alias)) >= 4 for alias in entry["aliases"]
            )
        )
        if low_information_identity:
            entry["quality_flags"] = sorted(
                {*entry["quality_flags"], "low_information_identity"}
            )
        token_count = len(str(entry["canonical_key"]).split())
        evidence_bound = bool(
            int(entry["support_count"]) >= 2
            or entry["definitions"]
            or entry["aliases"]
            or entry["abbreviations"]
            or entry["factual_relations"]
            or entry["application_contexts"]
            or entry["components"]
            or entry["component_of"]
        )
        unanchored_phrase_identity = bool(
            int(entry["support_count"]) < 2 and token_count >= 2 and not evidence_bound
        )
        if unanchored_phrase_identity:
            entry["quality_flags"] = sorted(
                {*entry["quality_flags"], "unanchored_phrase_identity"}
            )
        entry["retrieval_eligible"] = not (
            low_information_identity or unanchored_phrase_identity
        )
        entry["lexicon_state"] = "lexicon_ready"
        entry["updated_at"] = utcnow()
        entries.append(entry)
    return sorted(
        entries, key=lambda entry: (entry["canonical_key"], entry["lexicon_id"])
    )


async def ensure_lexicon_indexes(db: Any) -> None:
    """Create all durable identity and retrieval indexes idempotently."""

    sources = db[LEXICON_SOURCE_COLLECTION]
    entries = db[LEXICON_COLLECTION]
    runs = db[LEXICON_RUN_COLLECTION]
    await sources.create_index(
        [("corpus_id", 1), ("doc_id", 1), ("canonical_key", 1)],
        name="lexicon_source_identity_unique",
        unique=True,
        background=True,
    )
    await sources.create_index(
        [("corpus_id", 1), ("canonical_keys", 1)],
        name="lexicon_source_canonical_keys",
        background=True,
    )
    await sources.create_index(
        [("corpus_id", 1), ("canonical_key", 1)],
        name="lexicon_source_canonical_key_scan",
        background=True,
    )
    await sources.create_index(
        [("corpus_id", 1), ("identity_links.source", 1)],
        name="lexicon_source_identity_link_source",
        background=True,
    )
    await sources.create_index(
        [("corpus_id", 1), ("identity_links.target", 1)],
        name="lexicon_source_identity_link_target",
        background=True,
    )
    await entries.create_index(
        [("corpus_id", 1), ("lexicon_id", 1)],
        name="corpus_lexicon_identity_unique",
        unique=True,
        background=True,
    )
    await entries.create_index(
        [("corpus_id", 1), ("canonical_key", 1)],
        name="corpus_lexicon_canonical",
        background=True,
    )
    await entries.create_index(
        [("corpus_id", 1), ("aliases_normalized", 1)],
        name="corpus_lexicon_aliases",
        background=True,
    )
    await entries.create_index(
        [("corpus_id", 1), ("abbreviations_normalized", 1)],
        name="corpus_lexicon_abbreviations",
        background=True,
    )
    await entries.create_index(
        [("corpus_id", 1), ("source_parent_ids", 1)],
        name="corpus_lexicon_source_parents",
        background=True,
    )
    await entries.create_index(
        [("corpus_id", 1), ("materialization_id", 1)],
        name="corpus_lexicon_materialization",
        background=True,
    )
    try:
        await entries.create_index(
            [
                ("canonical_name", "text"),
                ("aliases", "text"),
                ("retrieval_gloss", "text"),
            ],
            name="corpus_lexicon_text",
            background=True,
        )
    except Exception:
        # Mongo permits one text index per collection. A deployment may already
        # carry an equivalent operator-created index.
        pass
    await runs.create_index(
        [("run_id", 1)],
        name="lexicon_backfill_run_id_unique",
        unique=True,
        background=True,
    )
    await runs.create_index(
        [("status", 1), ("updated_at", -1)],
        name="lexicon_backfill_status_updated",
        background=True,
    )


async def refresh_document_lexicon_sources(
    db: Any,
    *,
    corpus_id: str,
    doc_id: str,
) -> dict[str, Any]:
    """Replace one document's contribution rows and mark it pending indexing."""

    await ensure_lexicon_indexes(db)
    collection = db[LEXICON_SOURCE_COLLECTION]
    previous = await collection.find(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {"_id": 0, "canonical_key": 1, "canonical_keys": 1, "cooccurrence_counts": 1},
    ).to_list(length=None)
    contributions = await build_document_lexicon_sources(
        db, corpus_id=corpus_id, doc_id=doc_id
    )
    previous_keys = {
        str(row.get("canonical_key") or "")
        for row in previous
        if row.get("canonical_key")
    }
    current_keys = {
        str(row.get("canonical_key") or "")
        for row in contributions
        if row.get("canonical_key")
    }
    if contributions:
        await collection.bulk_write(
            [
                ReplaceOne(
                    {
                        "corpus_id": corpus_id,
                        "doc_id": doc_id,
                        "canonical_key": contribution["canonical_key"],
                    },
                    contribution,
                    upsert=True,
                )
                for contribution in contributions
            ],
            ordered=False,
        )
    stale_keys = sorted(previous_keys - current_keys)
    if stale_keys:
        await collection.delete_many(
            {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "canonical_key": {"$in": stale_keys},
            }
        )
    identity_keys = {
        value
        for row in [*previous, *contributions]
        for value in [row.get("canonical_key"), *(row.get("canonical_keys") or [])]
        if value
    }
    neighbor_keys = {
        value
        for row in [*previous, *contributions]
        for value in (row.get("cooccurrence_counts") or {}).keys()
        if value
    }
    affected = identity_keys | neighbor_keys
    await db["documents"].update_one(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {
            "$set": {
                "lexicon_state": "lexicon_pending",
                "lexicon_source_count": len(contributions),
                "lexicon_updated_at": utcnow(),
            }
        },
    )
    return {
        "doc_id": doc_id,
        "source_entries": len(contributions),
        "identity_keys": sorted(identity_keys),
        "neighbor_keys": sorted(neighbor_keys),
        "affected_keys": sorted(affected),
    }


async def _source_identity_closure(
    db: Any,
    *,
    corpus_id: str,
    seed_keys: Iterable[str],
    max_rounds: int = 4,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Load the bounded alias-connected source rows for incremental repair."""

    terms = {str(value) for value in seed_keys if str(value)}
    rows_by_id: dict[str, dict[str, Any]] = {}
    for _ in range(max(1, int(max_rounds))):
        if not terms:
            break
        rows = (
            await db[LEXICON_SOURCE_COLLECTION]
            .find(
                {
                    "corpus_id": corpus_id,
                    "$or": [
                        {"canonical_key": {"$in": sorted(terms)}},
                        {"canonical_keys": {"$in": sorted(terms)}},
                        {"identity_links.source": {"$in": sorted(terms)}},
                        {"identity_links.target": {"$in": sorted(terms)}},
                    ],
                },
                {"_id": 0},
            )
            .to_list(length=None)
        )
        before = len(terms)
        for row in rows:
            source_id = str(row.get("source_id") or "")
            if source_id:
                rows_by_id[source_id] = row
            terms.update(
                str(value)
                for value in [
                    row.get("canonical_key"),
                    *(row.get("canonical_keys") or []),
                    *(link.get("source") for link in (row.get("identity_links") or [])),
                    *(link.get("target") for link in (row.get("identity_links") or [])),
                ]
                if value
            )
        if len(terms) == before:
            break
    return list(rows_by_id.values()), terms


async def materialize_affected_lexicon(
    db: Any,
    *,
    corpus_id: str,
    affected_keys: Iterable[str],
) -> dict[str, Any]:
    """Reconcile only alias-connected entries touched by one document update."""

    await ensure_lexicon_indexes(db)
    source_rows, closure = await _source_identity_closure(
        db,
        corpus_id=corpus_id,
        seed_keys=affected_keys,
    )
    collection = db[LEXICON_COLLECTION]
    old_entries = await collection.find(
        {
            "corpus_id": corpus_id,
            "$or": [
                {"canonical_key": {"$in": sorted(closure)}},
                {"member_keys": {"$in": sorted(closure)}},
            ],
        },
        {"_id": 0, "lexicon_id": 1},
    ).to_list(length=None)
    old_ids = {
        str(row.get("lexicon_id") or "") for row in old_entries if row.get("lexicon_id")
    }
    entries = materialize_entries(source_rows, corpus_id)
    new_ids = {
        str(row.get("lexicon_id") or "") for row in entries if row.get("lexicon_id")
    }
    if entries:
        await collection.bulk_write(
            [
                ReplaceOne(
                    {"corpus_id": corpus_id, "lexicon_id": entry["lexicon_id"]},
                    entry,
                    upsert=True,
                )
                for entry in entries
            ],
            ordered=False,
        )
    stale_ids = sorted(old_ids - new_ids)
    if stale_ids:
        await collection.delete_many(
            {"corpus_id": corpus_id, "lexicon_id": {"$in": stale_ids}}
        )
    _bump_vocabulary_cache_epoch(corpus_id)
    return {
        "entries": entries,
        "closure_keys": sorted(closure),
        "stale_lexicon_ids": stale_ids,
        "replaced_lexicon_ids": sorted(old_ids & new_ids),
    }


async def materialize_corpus_lexicon(
    db: Any,
    *,
    corpus_id: str,
    materialization_id: str | None = None,
    key_batch_size: int = 2_000,
    progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Reconcile a corpus with bounded identity-closure batches.

    Full corpora can contain hundreds of thousands of source contributions.
    Loading those rows, their merged entries, and every ``ReplaceOne`` in one
    process exceeds the normal API container memory budget.  This path keeps
    only one alias-connected slice resident at a time.  A generation stamp
    makes the rebuild crash-safe: the previous searchable projection remains
    in place, and stale rows are deleted only after every source identity is
    represented and no non-acronym identity was split across slices.
    """

    await ensure_lexicon_indexes(db)
    source_collection = db[LEXICON_SOURCE_COLLECTION]
    collection = db[LEXICON_COLLECTION]
    generation = str(materialization_id or "").strip() or hashlib.sha256(
        f"{corpus_id}:{utcnow().isoformat()}".encode("utf-8")
    ).hexdigest()
    batch_limit = max(1, min(int(key_batch_size), 5_000))
    source_entry_count = await source_collection.count_documents(
        {"corpus_id": corpus_id}
    )

    source_keys: list[str] = []
    key_cursor = source_collection.aggregate(
        [
            {
                "$match": {
                    "corpus_id": corpus_id,
                    "canonical_key": {"$type": "string", "$ne": ""},
                }
            },
            {"$sort": {"canonical_key": 1}},
            {"$group": {"_id": "$canonical_key"}},
            {"$sort": {"_id": 1}},
        ],
        allowDiskUse=True,
        batchSize=5_000,
    )
    async for row in key_cursor:
        key = str(row.get("_id") or "")
        if key:
            source_keys.append(key)
    source_key_set = set(source_keys)

    processed_keys: set[str] = set()
    async for row in collection.find(
        {"corpus_id": corpus_id, "materialization_id": generation},
        {"_id": 0, "canonical_key": 1, "member_keys": 1},
    ):
        processed_keys.update(
            str(value)
            for value in (
                row.get("member_keys") or [row.get("canonical_key")]
            )
            if str(value)
        )
    processed_source_keys = processed_keys.intersection(source_key_set)
    position = 0
    batches = rows_examined = entries_written = 0
    while position < len(source_keys):
        seeds: list[str] = []
        while position < len(source_keys) and len(seeds) < batch_limit:
            key = source_keys[position]
            position += 1
            if key not in processed_keys:
                seeds.append(key)
        if not seeds:
            continue

        source_rows, _closure = await _source_identity_closure(
            db,
            corpus_id=corpus_id,
            seed_keys=seeds,
            max_rounds=12,
        )
        row_keys = {
            str(value)
            for row in source_rows
            for value in [
                row.get("canonical_key"),
                *(row.get("canonical_keys") or []),
            ]
            if str(value)
        }
        # Only loaded source rows prove that an identity was materialized. A
        # frontier term may be discovered on the final closure round without
        # its own source row having been queried yet.
        processed_keys.update(row_keys)
        processed_source_keys.update(
            key for key in row_keys if key in source_key_set
        )
        rows_examined += len(source_rows)

        entries = materialize_entries(source_rows, corpus_id)
        for entry in entries:
            entry["materialization_id"] = generation
        for start in range(0, len(entries), 250):
            batch = entries[start : start + 250]
            if not batch:
                continue
            await collection.bulk_write(
                [
                    ReplaceOne(
                        {
                            "corpus_id": corpus_id,
                            "lexicon_id": entry["lexicon_id"],
                        },
                        entry,
                        upsert=True,
                    )
                    for entry in batch
                ],
                ordered=False,
            )
        entries_written += len(entries)
        batches += 1
        if batches == 1 or batches % 5 == 0:
            logger.info(
                "corpus=%s materialize_batches=%d source_keys=%d/%d "
                "source_rows=%d entries_written=%d",
                corpus_id[:8],
                batches,
                len(processed_source_keys),
                len(source_keys),
                rows_examined,
                entries_written,
            )
        if progress_callback is not None and (batches == 1 or batches % 5 == 0):
            await progress_callback(
                {
                    "phase": "materialize",
                    "source_keys_total": len(source_keys),
                    "source_keys_scanned": min(position, len(source_keys)),
                    "identity_keys_resolved": len(processed_source_keys),
                    "source_rows_examined": rows_examined,
                    "entries_written": entries_written,
                    "batches": batches,
                    "materialization_id": generation,
                }
            )

    represented_keys: set[str] = set()
    duplicate_long_keys: set[str] = set()
    generated_entries = 0
    async for row in collection.find(
        {"corpus_id": corpus_id, "materialization_id": generation},
        {"_id": 0, "canonical_key": 1, "member_keys": 1},
    ):
        generated_entries += 1
        member_keys = {
            str(value)
            for value in (
                row.get("member_keys") or [row.get("canonical_key")]
            )
            if str(value)
        }
        duplicate_long_keys.update(
            key
            for key in member_keys & represented_keys
            if not _is_short_identity_key(key)
        )
        represented_keys.update(member_keys)

    missing_keys = sorted(source_key_set - represented_keys)
    if missing_keys or duplicate_long_keys:
        raise RuntimeError(
            "bounded lexicon materialization validation failed: "
            f"missing_source_keys={len(missing_keys)} "
            f"split_identity_keys={len(duplicate_long_keys)} "
            f"missing_sample={missing_keys[:8]} "
            f"split_sample={sorted(duplicate_long_keys)[:8]}"
        )

    stale_result = await collection.delete_many(
        {
            "corpus_id": corpus_id,
            "materialization_id": {"$ne": generation},
        }
    )
    now = utcnow()
    await db["documents"].update_many(
        {"corpus_id": corpus_id, "lexicon_state": "lexicon_pending"},
        {"$set": {"lexicon_state": "lexicon_materialized", "lexicon_updated_at": now}},
    )
    coverage = await _lexicon_document_counts(db, corpus_id)
    corpus_state = (
        "lexicon_materialized"
        if coverage["processed"] >= coverage["total"]
        else "lexicon_pending"
    )
    await db["corpora"].update_one(
        {"corpus_id": corpus_id},
        {
            "$set": {
                "lexicon_state": corpus_state,
                "lexicon_entry_count": generated_entries,
                "lexicon_documents_processed": coverage["processed"],
                "lexicon_documents_total": coverage["total"],
                "lexicon_schema_version": LEXICON_SCHEMA_VERSION,
                "lexicon_materialization_id": generation,
                "lexicon_updated_at": now,
            }
        },
    )
    _bump_vocabulary_cache_epoch(corpus_id)
    return {
        "corpus_id": corpus_id,
        "source_entries": source_entry_count,
        "source_keys": len(source_keys),
        "source_rows_examined": rows_examined,
        "lexicon_entries": generated_entries,
        "materialization_batches": batches,
        "materialization_id": generation,
        "stale_entries_deleted": int(getattr(stale_result, "deleted_count", 0)),
        "coverage": coverage,
        "entries": None,
    }


async def refresh_corpus_lexicon_glosses(
    db: Any,
    *,
    corpus_id: str,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Stream a gloss-only migration and invalidate vector readiness safely."""

    await ensure_lexicon_indexes(db)
    collection = db[LEXICON_COLLECTION]
    projection = {
        "_id": 0,
        "lexicon_id": 1,
        "canonical_name": 1,
        "canonical_key": 1,
        "aliases": 1,
        "abbreviations": 1,
        "definitions": 1,
        "structural_contexts": 1,
        "contextual_usages": 1,
        "application_contexts": 1,
        "components": 1,
        "related_concepts": 1,
    }
    operations: list[UpdateOne] = []
    refreshed_count = 0
    size = max(1, min(int(batch_size), 2_000))
    cursor = collection.find({"corpus_id": corpus_id}, projection).sort("lexicon_id", 1)
    async for entry in cursor:
        lexicon_id = str(entry.get("lexicon_id") or "")
        if not lexicon_id:
            continue
        operations.append(
            UpdateOne(
                {"corpus_id": corpus_id, "lexicon_id": lexicon_id},
                {
                    "$set": {
                        **_refreshed_gloss_fields(entry),
                        "updated_at": utcnow(),
                    }
                },
            )
        )
        if len(operations) >= size:
            await collection.bulk_write(operations, ordered=False)
            refreshed_count += len(operations)
            operations = []
    if operations:
        await collection.bulk_write(operations, ordered=False)
        refreshed_count += len(operations)

    now = utcnow()
    await db["documents"].update_many(
        {
            "corpus_id": corpus_id,
            "lexicon_state": {
                "$in": ["lexicon_ready", "lexicon_indexing", "lexicon_materialized"]
            },
        },
        {
            "$set": {
                "lexicon_state": "lexicon_materialized",
                "lexicon_updated_at": now,
            }
        },
    )
    await db["corpora"].update_one(
        {"corpus_id": corpus_id},
        {
            "$set": {
                "lexicon_state": "lexicon_materialized",
                "lexicon_entry_count": refreshed_count,
                "lexicon_schema_version": LEXICON_SCHEMA_VERSION,
                "lexicon_updated_at": now,
            },
            "$unset": {
                "lexicon_version": "",
                "lexicon_index_cursor": "",
                "lexicon_index_missing_count": "",
                "lexicon_index_missing_sample": "",
            },
        },
    )
    return {
        "corpus_id": corpus_id,
        "phase": "gloss_refresh",
        "refreshed_entries": refreshed_count,
        "lexicon_state": "lexicon_materialized",
    }


def lexicon_version(entries: list[dict[str, Any]]) -> str:
    """Content version used by planner caches and retrieval diagnostics."""

    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: str(item.get("lexicon_id") or "")):
        digest.update(str(entry.get("lexicon_id") or "").encode("utf-8"))
        digest.update(str(entry.get("embedding_gloss") or "").encode("utf-8"))
        digest.update(str(entry.get("utility_gloss") or "").encode("utf-8"))
        digest.update(str(entry.get("retrieval_gloss") or "").encode("utf-8"))
        for source_hash in entry.get("source_hashes") or []:
            digest.update(str(source_hash).encode("utf-8"))
    return digest.hexdigest()


async def _lexicon_snapshot_from_db(
    db: Any,
    *,
    corpus_id: str,
) -> tuple[set[str], str, int]:
    """Return eligible IDs and a deterministic version without loading rows."""

    digest = hashlib.sha256()
    eligible_ids: set[str] = set()
    total = 0
    cursor = (
        db[LEXICON_COLLECTION]
        .find(
            {"corpus_id": corpus_id},
            {
                "_id": 0,
                "lexicon_id": 1,
                "embedding_gloss": 1,
                "utility_gloss": 1,
                "retrieval_gloss": 1,
                "source_hashes": 1,
                "retrieval_eligible": 1,
            },
        )
        .sort("lexicon_id", 1)
    )
    async for entry in cursor:
        lexicon_id = str(entry.get("lexicon_id") or "")
        if not lexicon_id:
            continue
        total += 1
        digest.update(lexicon_id.encode("utf-8"))
        digest.update(str(entry.get("embedding_gloss") or "").encode("utf-8"))
        digest.update(str(entry.get("utility_gloss") or "").encode("utf-8"))
        digest.update(str(entry.get("retrieval_gloss") or "").encode("utf-8"))
        for source_hash in entry.get("source_hashes") or []:
            digest.update(str(source_hash).encode("utf-8"))
        if entry.get("retrieval_eligible", True):
            eligible_ids.add(lexicon_id)
    return eligible_ids, digest.hexdigest(), total


async def _defer_lexicon_qdrant_optimization(
    db: Any,
    qdrant_client: Any,
    *,
    corpus_id: str,
) -> int:
    """Persist and disable the schemas HNSW threshold for a bulk index window."""

    from qdrant_client.models import OptimizersConfigDiff

    from services.storage.qdrant_writer import _col_for_corpus

    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {
            "_id": 0,
            "lexicon_qdrant_optimizer_deferred": 1,
            "lexicon_qdrant_optimizer_restore_threshold": 1,
        },
    )
    deferred = bool((corpus or {}).get("lexicon_qdrant_optimizer_deferred"))
    restore_threshold = int(
        (corpus or {}).get("lexicon_qdrant_optimizer_restore_threshold") or 0
    )
    collection_name = _col_for_corpus(corpus_id, "schemas")
    if not deferred:
        info = await qdrant_client.get_collection(collection_name)
        config = getattr(getattr(info, "config", None), "optimizer_config", None)
        restore_threshold = int(getattr(config, "indexing_threshold", 0) or 10_000)
        await db["corpora"].update_one(
            {"corpus_id": corpus_id},
            {
                "$set": {
                    "lexicon_qdrant_optimizer_deferred": True,
                    "lexicon_qdrant_optimizer_restore_threshold": restore_threshold,
                    "lexicon_updated_at": utcnow(),
                }
            },
        )
    await qdrant_client.update_collection(
        collection_name,
        optimizers_config=OptimizersConfigDiff(indexing_threshold=0),
    )
    return restore_threshold


async def _restore_lexicon_qdrant_optimization(
    db: Any,
    qdrant_client: Any,
    *,
    corpus_id: str,
) -> int | None:
    """Restore HNSW only after Mongo/Qdrant parity has succeeded."""

    from qdrant_client.models import OptimizersConfigDiff

    from services.storage.qdrant_writer import _col_for_corpus

    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {
            "_id": 0,
            "lexicon_qdrant_optimizer_deferred": 1,
            "lexicon_qdrant_optimizer_restore_threshold": 1,
        },
    )
    if not bool((corpus or {}).get("lexicon_qdrant_optimizer_deferred")):
        return None
    restore_threshold = int(
        (corpus or {}).get("lexicon_qdrant_optimizer_restore_threshold") or 10_000
    )
    await qdrant_client.update_collection(
        _col_for_corpus(corpus_id, "schemas"),
        optimizers_config=OptimizersConfigDiff(
            indexing_threshold=restore_threshold
        ),
    )
    await db["corpora"].update_one(
        {"corpus_id": corpus_id},
        {
            "$unset": {
                "lexicon_qdrant_optimizer_deferred": "",
                "lexicon_qdrant_optimizer_restore_threshold": "",
            },
            "$set": {"lexicon_updated_at": utcnow()},
        },
    )
    return restore_threshold


async def index_corpus_lexicon_slice(
    db: Any,
    qdrant_client: Any,
    *,
    corpus_id: str,
    resume_after_lexicon_id: str | None = None,
    limit: int = 10_000,
    batch_size: int = 64,
    progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Index a durable keyset slice without declaring the corpus ready.

    Large corpora must not hold every Mongo row and every embedding in one
    process. Each slice is idempotent: retrying the same cursor range overwrites
    the same deterministic Qdrant IDs, while final stale deletion and readiness
    are deferred to :func:`finalize_corpus_lexicon_index`.
    """

    from services.embedder import embed_documents
    from services.storage.qdrant_writer import (
        _lexicon_payload,
        ensure_collections_for_corpus,
        retrieve_lexicon_entries,
        upsert_lexicon_entries,
    )

    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "default_ingestion_config": 1, "name": 1},
    )
    config = (corpus or {}).get("default_ingestion_config") or {}
    await ensure_collections_for_corpus(
        qdrant_client,
        corpus_id,
        dim=int(config.get("embedding_dimension") or 1024),
        corpus_name=str((corpus or {}).get("name") or "") or None,
    )
    await _defer_lexicon_qdrant_optimization(
        db,
        qdrant_client,
        corpus_id=corpus_id,
    )
    query: dict[str, Any] = {
        "corpus_id": corpus_id,
        "retrieval_eligible": {"$ne": False},
    }
    if resume_after_lexicon_id:
        query["lexicon_id"] = {"$gt": str(resume_after_lexicon_id)}
    slice_limit = max(1, min(int(limit), 50_000))
    entries = (
        await db[LEXICON_COLLECTION]
        .find(query, {"_id": 0})
        .sort("lexicon_id", 1)
        .limit(slice_limit)
        .to_list(length=slice_limit)
    )
    written = embedded = reused_vectors = unchanged = 0
    # Scan a larger Qdrant/Mongo window than the embedder microbatch. Delta
    # migrations often change only a small fraction of points; accumulating
    # those changes fills the provider's own bounded batches instead of issuing
    # dozens of under-filled model calls.
    size = max(1, min(int(batch_size), 2_048))
    owner_verified = False
    for start in range(0, len(entries), size):
        batch = entries[start : start + size]
        existing = await retrieve_lexicon_entries(
            qdrant_client,
            corpus_id,
            [str(row.get("lexicon_id") or "") for row in batch],
            with_vectors=True,
            check_exists=False,
        )
        to_embed: list[dict[str, Any]] = []
        to_reuse: list[dict[str, Any]] = []
        reused_batch_vectors: list[list[float]] = []
        for row in batch:
            lexicon_id = str(row.get("lexicon_id") or "")
            current = existing.get(lexicon_id) or {}
            current_payload = dict(current.get("payload") or {})
            current_vector = current.get("vector")
            target_payload = _lexicon_payload({**row, "corpus_id": corpus_id})
            vector_text_matches = bool(current_payload) and str(
                current_payload.get("embedding_gloss") or ""
            ) == str(target_payload.get("embedding_gloss") or "")
            payload_matches = bool(current_payload) and all(
                current_payload.get(key) == value
                for key, value in target_payload.items()
            )
            if vector_text_matches and isinstance(current_vector, list):
                if payload_matches:
                    unchanged += 1
                    continue
                to_reuse.append(row)
                reused_batch_vectors.append(current_vector)
                continue
            to_embed.append(row)

        if to_reuse:
            reused_vectors += await upsert_lexicon_entries(
                qdrant_client,
                corpus_id,
                to_reuse,
                reused_batch_vectors,
                verify_owner=not owner_verified,
            )
            owner_verified = True
        if to_embed:
            vectors = await embed_documents(
                [
                    str(
                        row.get("embedding_gloss")
                        or row.get("retrieval_gloss")
                        or row.get("canonical_name")
                        or ""
                    )
                    for row in to_embed
                ],
                config,
            )
            embedded += await upsert_lexicon_entries(
                qdrant_client,
                corpus_id,
                to_embed,
                vectors,
                verify_owner=not owner_verified,
            )
            owner_verified = True
        written = embedded + reused_vectors
        if progress_callback is not None and (
            start == 0 or start + len(batch) >= len(entries) or (start // size) % 4 == 3
        ):
            await progress_callback(
                {
                    "phase": "vector_index",
                    "examined_in_slice": start + len(batch),
                    "indexed_in_slice": written,
                    "embedded_in_slice": embedded,
                    "reused_vectors_in_slice": reused_vectors,
                    "unchanged_in_slice": unchanged,
                    "slice_entries": len(entries),
                    "last_lexicon_id": str(batch[-1].get("lexicon_id") or ""),
                }
            )

    last_lexicon_id = str(entries[-1].get("lexicon_id") or "") if entries else None
    remaining_query: dict[str, Any] = {
        "corpus_id": corpus_id,
        "retrieval_eligible": {"$ne": False},
    }
    if last_lexicon_id:
        remaining_query["lexicon_id"] = {"$gt": last_lexicon_id}
    elif resume_after_lexicon_id:
        remaining_query["lexicon_id"] = {"$gt": str(resume_after_lexicon_id)}
    remaining = await db[LEXICON_COLLECTION].count_documents(remaining_query)
    eligible_total = await db[LEXICON_COLLECTION].count_documents(
        {
            "corpus_id": corpus_id,
            "retrieval_eligible": {"$ne": False},
        }
    )
    now = utcnow()
    await db["corpora"].update_one(
        {"corpus_id": corpus_id},
        {
            "$set": {
                "lexicon_state": "lexicon_indexing",
                "lexicon_index_cursor": last_lexicon_id or resume_after_lexicon_id,
                "lexicon_index_remaining": remaining,
                "lexicon_indexed_entry_count": max(0, eligible_total - remaining),
                "lexicon_updated_at": now,
            }
        },
    )
    return {
        "corpus_id": corpus_id,
        "indexed": written,
        "embedded": embedded,
        "reused_vectors": reused_vectors,
        "unchanged": unchanged,
        "slice_entries": len(entries),
        "eligible_entries": eligible_total,
        "remaining_entries": remaining,
        "last_lexicon_id": last_lexicon_id,
        "has_more": remaining > 0,
        "finalized": False,
    }


async def finalize_corpus_lexicon_index(
    db: Any,
    qdrant_client: Any,
    *,
    corpus_id: str,
) -> dict[str, Any]:
    """Reconcile exact Mongo/Qdrant identity sets, then publish readiness."""

    from services.storage.qdrant_writer import (
        delete_lexicon_entries,
        list_lexicon_ids,
    )

    eligible_ids, version, total = await _lexicon_snapshot_from_db(
        db, corpus_id=corpus_id
    )
    qdrant_ids = set(await list_lexicon_ids(qdrant_client, corpus_id))
    missing_ids = sorted(eligible_ids - qdrant_ids)
    stale_ids = sorted(qdrant_ids - eligible_ids)
    now = utcnow()
    if missing_ids:
        await db["corpora"].update_one(
            {"corpus_id": corpus_id},
            {
                "$set": {
                    "lexicon_state": "lexicon_indexing",
                    "lexicon_index_missing_count": len(missing_ids),
                    "lexicon_index_missing_sample": missing_ids[:24],
                    "lexicon_updated_at": now,
                }
            },
        )
        raise RuntimeError(
            "lexicon index cannot finalize: "
            f"{len(missing_ids)} Mongo-eligible IDs are missing from Qdrant"
        )
    if stale_ids:
        await delete_lexicon_entries(qdrant_client, corpus_id, stale_ids)

    optimizer_restore_threshold = await _restore_lexicon_qdrant_optimization(
        db,
        qdrant_client,
        corpus_id=corpus_id,
    )

    await db["documents"].update_many(
        {
            "corpus_id": corpus_id,
            "lexicon_state": {
                "$in": [
                    "lexicon_pending",
                    "lexicon_materialized",
                    "lexicon_indexing",
                ]
            },
        },
        {
            "$set": {
                "lexicon_state": "lexicon_ready",
                "lexicon_schema_version": LEXICON_SCHEMA_VERSION,
                "lexicon_version": version,
                "lexicon_updated_at": now,
            }
        },
    )
    coverage = await _lexicon_document_counts(db, corpus_id)
    corpus_state = (
        "lexicon_ready" if coverage["ready"] >= coverage["total"] else "lexicon_pending"
    )
    await db["corpora"].update_one(
        {"corpus_id": corpus_id},
        {
            "$set": {
                "lexicon_state": corpus_state,
                "lexicon_entry_count": total,
                "lexicon_indexed_entry_count": len(eligible_ids),
                "lexicon_documents_ready": coverage["ready"],
                "lexicon_documents_total": coverage["total"],
                "lexicon_schema_version": LEXICON_SCHEMA_VERSION,
                "lexicon_version": version,
                "lexicon_index_remaining": 0,
                "lexicon_updated_at": now,
            },
            "$unset": {
                "lexicon_index_cursor": "",
                "lexicon_index_missing_count": "",
                "lexicon_index_missing_sample": "",
            },
        },
    )
    return {
        "corpus_id": corpus_id,
        "lexicon_entries": total,
        "eligible_entries": len(eligible_ids),
        "qdrant_entries": len(qdrant_ids) - len(stale_ids),
        "stale_deleted": len(stale_ids),
        "missing_entries": 0,
        "lexicon_version": version,
        "coverage": coverage,
        "optimizer_restore_threshold": optimizer_restore_threshold,
        "finalized": corpus_state == "lexicon_ready",
    }


async def index_corpus_lexicon(
    db: Any,
    qdrant_client: Any,
    *,
    corpus_id: str,
    entries: list[dict[str, Any]] | None = None,
    batch_size: int = 64,
) -> dict[str, Any]:
    """Embed retrieval glosses and reconcile the Qdrant lexicon projection.

    New points are written before stale points are removed, so an embedding or
    Qdrant failure leaves the previous searchable projection intact.
    """

    from services.embedder import embed_documents
    from services.storage.qdrant_writer import (
        delete_lexicon_entries,
        ensure_collections_for_corpus,
        list_lexicon_ids,
        upsert_lexicon_entries,
    )

    if entries is None:
        entries = (
            await db[LEXICON_COLLECTION]
            .find({"corpus_id": corpus_id}, {"_id": 0})
            .sort("canonical_key", 1)
            .to_list(length=None)
        )
    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "default_ingestion_config": 1, "name": 1},
    )
    config = (corpus or {}).get("default_ingestion_config") or {}
    dimension = int(config.get("embedding_dimension") or 1024)
    await ensure_collections_for_corpus(
        qdrant_client,
        corpus_id,
        dim=dimension,
        corpus_name=str((corpus or {}).get("name") or "") or None,
    )
    previous_ids = set(await list_lexicon_ids(qdrant_client, corpus_id))
    eligible_entries = [
        entry for entry in entries if entry.get("retrieval_eligible", True)
    ]
    written = 0
    size = max(1, min(int(batch_size), 256))
    for start in range(0, len(eligible_entries), size):
        batch = eligible_entries[start : start + size]
        texts = [
            str(
                entry.get("embedding_gloss")
                or entry.get("retrieval_gloss")
                or entry.get("canonical_name")
                or ""
            )
            for entry in batch
        ]
        vectors = await embed_documents(texts, config)
        written += await upsert_lexicon_entries(
            qdrant_client,
            corpus_id,
            batch,
            vectors,
            verify_owner=(start == 0),
        )
    current_ids = {
        str(entry.get("lexicon_id") or "")
        for entry in eligible_entries
        if entry.get("lexicon_id")
    }
    stale_ids = sorted(previous_ids - current_ids)
    if stale_ids:
        await delete_lexicon_entries(qdrant_client, corpus_id, stale_ids)

    now = utcnow()
    version = lexicon_version(entries)
    await db["documents"].update_many(
        {
            "corpus_id": corpus_id,
            "lexicon_state": {"$in": ["lexicon_pending", "lexicon_materialized"]},
        },
        {
            "$set": {
                "lexicon_state": "lexicon_ready",
                "lexicon_schema_version": LEXICON_SCHEMA_VERSION,
                "lexicon_version": version,
                "lexicon_updated_at": now,
            }
        },
    )
    coverage = await _lexicon_document_counts(db, corpus_id)
    corpus_state = (
        "lexicon_ready" if coverage["ready"] >= coverage["total"] else "lexicon_pending"
    )
    await db["corpora"].update_one(
        {"corpus_id": corpus_id},
        {
            "$set": {
                "lexicon_state": corpus_state,
                "lexicon_entry_count": len(entries),
                "lexicon_indexed_entry_count": len(eligible_entries),
                "lexicon_documents_ready": coverage["ready"],
                "lexicon_documents_total": coverage["total"],
                "lexicon_schema_version": LEXICON_SCHEMA_VERSION,
                "lexicon_version": version,
                "lexicon_updated_at": now,
            }
        },
    )
    return {
        "corpus_id": corpus_id,
        "lexicon_entries": len(entries),
        "eligible_entries": len(eligible_entries),
        "indexed": written,
        "stale_deleted": len(stale_ids),
        "lexicon_version": version,
        "coverage": coverage,
    }


async def index_affected_lexicon(
    db: Any,
    qdrant_client: Any,
    *,
    corpus_id: str,
    entries: list[dict[str, Any]],
    stale_lexicon_ids: list[str] | None = None,
    doc_id: str | None = None,
    batch_size: int = 64,
) -> dict[str, Any]:
    """Mirror one incremental identity closure without touching other points."""

    from services.embedder import embed_documents
    from services.storage.qdrant_writer import (
        delete_lexicon_entries,
        ensure_collections_for_corpus,
        upsert_lexicon_entries,
    )

    corpus = await db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "default_ingestion_config": 1, "name": 1},
    )
    config = (corpus or {}).get("default_ingestion_config") or {}
    await ensure_collections_for_corpus(
        qdrant_client,
        corpus_id,
        dim=int(config.get("embedding_dimension") or 1024),
        corpus_name=str((corpus or {}).get("name") or "") or None,
    )
    eligible_entries = [
        entry for entry in entries if entry.get("retrieval_eligible", True)
    ]
    written = 0
    size = max(1, min(int(batch_size), 256))
    for start in range(0, len(eligible_entries), size):
        batch = eligible_entries[start : start + size]
        vectors = await embed_documents(
            [
                str(
                    row.get("embedding_gloss")
                    or row.get("retrieval_gloss")
                    or row.get("canonical_name")
                    or ""
                )
                for row in batch
            ],
            config,
        )
        written += await upsert_lexicon_entries(
            qdrant_client,
            corpus_id,
            batch,
            vectors,
            verify_owner=(start == 0),
        )
    stale = list(
        dict.fromkeys(
            [
                *(value for value in (stale_lexicon_ids or []) if value),
                *(
                    str(entry.get("lexicon_id") or "")
                    for entry in entries
                    if not entry.get("retrieval_eligible", True)
                    and entry.get("lexicon_id")
                ),
            ]
        )
    )
    if stale:
        await delete_lexicon_entries(qdrant_client, corpus_id, stale)
    now = utcnow()
    if doc_id:
        await db["documents"].update_one(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {
                "$set": {
                    "lexicon_state": "lexicon_ready",
                    "lexicon_schema_version": LEXICON_SCHEMA_VERSION,
                    "lexicon_updated_at": now,
                }
            },
        )
    coverage = await _lexicon_document_counts(db, corpus_id)
    corpus_state = (
        "lexicon_ready" if coverage["ready"] >= coverage["total"] else "lexicon_pending"
    )
    await db["corpora"].update_one(
        {"corpus_id": corpus_id},
        {
            "$set": {
                "lexicon_state": corpus_state,
                "lexicon_schema_version": LEXICON_SCHEMA_VERSION,
                "lexicon_documents_ready": coverage["ready"],
                "lexicon_documents_total": coverage["total"],
                "lexicon_updated_at": now,
            }
        },
    )
    return {
        "indexed": written,
        "stale_deleted": len(stale),
        "coverage": coverage,
    }


async def refresh_and_index_document_lexicon(
    db: Any,
    qdrant_client: Any,
    *,
    corpus_id: str,
    doc_id: str,
) -> dict[str, Any]:
    """Best-effort ingest hook: source refresh -> materialize -> vector mirror."""

    source = await refresh_document_lexicon_sources(
        db, corpus_id=corpus_id, doc_id=doc_id
    )
    materialized = await materialize_affected_lexicon(
        db,
        corpus_id=corpus_id,
        affected_keys=source["affected_keys"],
    )
    indexed = await index_affected_lexicon(
        db,
        qdrant_client,
        corpus_id=corpus_id,
        entries=materialized["entries"],
        stale_lexicon_ids=materialized["stale_lexicon_ids"],
        doc_id=doc_id,
    )
    return {
        **source,
        **indexed,
        "materialized_entries": len(materialized["entries"]),
        "closure_keys": len(materialized["closure_keys"]),
    }


async def remove_document_lexicon_sources(
    db: Any,
    *,
    corpus_id: str,
    doc_id: str,
) -> dict[str, Any]:
    """Remove stale contributions before/after a durable document deletion."""

    await ensure_lexicon_indexes(db)
    collection = db[LEXICON_SOURCE_COLLECTION]
    previous = await collection.find(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {
            "_id": 0,
            "canonical_key": 1,
            "canonical_keys": 1,
            "cooccurrence_counts": 1,
        },
    ).to_list(length=None)
    affected_keys = {
        str(value)
        for row in previous
        for value in [
            row.get("canonical_key"),
            *(row.get("canonical_keys") or []),
            *((row.get("cooccurrence_counts") or {}).keys()),
        ]
        if value
    }
    now = utcnow()
    await db["corpora"].update_one(
        {"corpus_id": corpus_id},
        {"$set": {"lexicon_state": "lexicon_pending", "lexicon_updated_at": now}},
    )
    result = await collection.delete_many({"corpus_id": corpus_id, "doc_id": doc_id})
    materialized = await materialize_affected_lexicon(
        db,
        corpus_id=corpus_id,
        affected_keys=affected_keys,
    )
    return {
        "deleted_source_entries": int(getattr(result, "deleted_count", 0)),
        "affected_keys": sorted(affected_keys),
        "entries": materialized["entries"],
        "stale_lexicon_ids": materialized["stale_lexicon_ids"],
        "closure_keys": materialized["closure_keys"],
    }


def _bump_vocabulary_cache_epoch(corpus_id: str) -> None:
    """Invalidate in-process vocabulary-resolution cache entries (P1.7).

    Best-effort: the resolver usually runs in the backend process while
    materialization runs in the ingest worker, where this bump is a no-op for
    the backend's cache — its TTL bounds that staleness window."""

    try:
        from services.retriever.vocabulary_cache import bump_corpus_epoch

        bump_corpus_epoch(corpus_id)
    except Exception:  # noqa: BLE001 — cache invalidation must never break writes
        logger.debug("vocabulary cache epoch bump failed", exc_info=True)


async def delete_corpus_lexicon(db: Any, corpus_id: str) -> dict[str, int]:
    sources = await db[LEXICON_SOURCE_COLLECTION].delete_many({"corpus_id": corpus_id})
    entries = await db[LEXICON_COLLECTION].delete_many({"corpus_id": corpus_id})
    _bump_vocabulary_cache_epoch(corpus_id)
    return {
        "source_entries": int(getattr(sources, "deleted_count", 0)),
        "lexicon_entries": int(getattr(entries, "deleted_count", 0)),
    }
