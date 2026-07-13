"""Corpus-grounded vocabulary resolution before document routing.

Focused retrieval reads only the corpus's Qdrant ``entity_lexicon`` points.
Hybrid may add Mongo exact/fuzzy alias evidence. Graph may additionally attach
canonical Neo4j neighbors. All expansions retain source provenance and remain
exploratory unless the user's own words establish the concept directly.
"""

from __future__ import annotations

import asyncio
import math
import re
from difflib import SequenceMatcher
from time import perf_counter
from typing import Any

from models.schemas import RetrievalTier
from qdrant_client import models
from services.ingestion.corpus_lexicon import normalize_identity
from services.ingestion.tier0 import SHARED_DOCSUM
from services.retriever import vocabulary_cache
from services.retriever.query_plan import QueryLane, QueryPlanV2
from services.retriever.query_semantics import lexical_terms

VOCABULARY_RESOLVER_VERSION = "corpus_vocabulary.v3"

_QUERY_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "before",
        "could",
        "does",
        "from",
        "have",
        "initial",
        "into",
        "make",
        "should",
        "their",
        "these",
        "this",
        "those",
        "what",
        "when",
        "where",
        "which",
        "with",
        "would",
    }
)
_UPPER_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9-]{1,9}\b")
_TRUSTED_EXACT_ALIAS_METHODS = frozenset(
    {
        "explicit_alias_pattern",
        "schwartz_hearst_acronym",
        "extraction_surface_form",
    }
)
_MONGO_LOOKUP_DEADLINE_SECONDS = 0.8


def query_exact_terms(query: str, *, max_ngram: int = 6) -> list[str]:
    """Generate bounded phrase candidates for indexed exact alias lookup."""

    # Exact identity spans must preserve order, stopwords, and duplicates.
    # Building these from ``lexical_terms`` can drop meaningful connector words
    # and make a canonical corpus phrase impossible to match.
    tokens = normalize_identity(query).split()[:24]
    output: list[str] = []
    for size in range(min(max_ngram, len(tokens)), 1, -1):
        output.extend(
            " ".join(tokens[start : start + size])
            for start in range(len(tokens) - size + 1)
        )
    output.extend(normalize_identity(value) for value in _UPPER_TOKEN_RE.findall(query))
    if len(tokens) == 1:
        output.append(tokens[0])
    elif len(tokens) <= 3:
        # Narrow concept lanes may contain possessives or punctuation that split
        # during identity normalization (``actor's`` -> ``actor s``). Add only
        # bounded, non-generic content unigrams; the full question never gets a
        # bag-of-words exact-identity pass.
        output.extend(
            term
            for term in _content_terms(query)
            if len(term) >= 4 and term not in _QUERY_STOPWORDS
        )
    return list(dict.fromkeys(value for value in output if value))[:96]


def _content_terms(value: str) -> set[str]:
    return {
        term.removesuffix("'s")
        for term in lexical_terms(value)
        if len(term) >= 3 and term not in _QUERY_STOPWORDS
    }


def _morphological_root(value: str) -> str:
    """Return a conservative root for lightweight retrieval overlap checks."""

    term = value.casefold().strip()
    for suffix in ("ational", "tional", "ation", "ities", "ingly", "ments", "ment"):
        if term.endswith(suffix) and len(term) - len(suffix) >= 4:
            term = term[: -len(suffix)]
            break
    for suffix in ("ical", "ial", "ing", "ied", "ies", "ed", "es", "s", "ly"):
        if term.endswith(suffix) and len(term) - len(suffix) >= 3:
            term = term[: -len(suffix)]
            break
    if term.endswith("e") and len(term) >= 4:
        term = term[:-1]
    return term


def _morphologically_related(left: str, right: str) -> bool:
    if left == right:
        return True
    left_root = _morphological_root(left)
    right_root = _morphological_root(right)
    return bool(
        len(left_root) >= 3
        and left_root == right_root
        or (
            len(left) >= 4
            and len(right) >= 4
            and SequenceMatcher(None, left, right).ratio() >= 0.78
        )
    )


def _match_overlap(query: str, match: dict[str, Any]) -> tuple[int, float]:
    query_terms = _content_terms(query)
    text = " ".join(
        [
            str(match.get("term") or match.get("canonical_name") or ""),
            str(match.get("retrieval_gloss") or ""),
            str(match.get("utility_gloss") or ""),
            " ".join(str(value) for value in (match.get("aliases") or [])),
        ]
    )
    target_terms = _content_terms(text)
    overlap = query_terms & target_terms
    # Generic morphology bridge (face/facial, act/acting, etc.). It is only a
    # grounding signal after ANN retrieval, never an identity merge or a query
    # rewrite by itself.
    overlap.update(
        query_term
        for query_term in query_terms - overlap
        if any(
            _morphologically_related(query_term, target_term)
            for target_term in target_terms
        )
    )
    return len(overlap), len(overlap) / max(1, min(len(query_terms), 6))


def _exact_surface_is_excluded(
    match: dict[str, Any], excluded_terms: list[str]
) -> bool:
    """Return whether an exact lexicon identity is itself negated.

    A user may explicitly request a grounded concept while excluding one of
    its ordinary-language attributes. That positive exact mention remains
    valid. Only a canonical, member, alias, or abbreviation surface that is
    itself inside the typed exclusion is suppressed.
    """

    excluded_keys = {
        key
        for value in excluded_terms
        for key in [
            normalize_identity(value),
            *(normalize_identity(value).split()),
        ]
        if key
    }
    surfaces = {
        normalize_identity(value)
        for value in [
            match.get("canonical_key"),
            match.get("term") or match.get("canonical_name"),
            *(match.get("member_keys") or []),
            *(match.get("aliases") or []),
            *(match.get("abbreviations") or []),
        ]
        if value
    }
    return bool(excluded_keys & surfaces)


def _trusted_exact_identity_match(
    match: dict[str, Any], exact_terms: list[str]
) -> bool:
    """Require identity evidence before granting an alias exact authority."""

    query_keys = {
        normalize_identity(value) for value in exact_terms if normalize_identity(value)
    }
    canonical_keys = {
        normalize_identity(value)
        for value in [
            match.get("canonical_key"),
            match.get("term") or match.get("canonical_name"),
            *(match.get("member_keys") or []),
            *(match.get("abbreviations") or []),
            *(match.get("abbreviations_normalized") or []),
        ]
        if value
    }
    if query_keys & canonical_keys:
        return True

    alias_keys = {
        normalize_identity(value)
        for value in [
            *(match.get("aliases") or []),
            *(match.get("aliases_normalized") or []),
        ]
        if value
    }
    matched_aliases = query_keys & alias_keys
    if not matched_aliases:
        return False
    evidence_by_alias: dict[str, list[dict[str, Any]]] = {}
    for evidence in match.get("alias_evidence") or []:
        if not isinstance(evidence, dict):
            continue
        alias_key = normalize_identity(
            evidence.get("alias_key") or evidence.get("alias")
        )
        if alias_key:
            evidence_by_alias.setdefault(alias_key, []).append(evidence)
    for alias_key in matched_aliases:
        evidence = evidence_by_alias.get(alias_key) or []
        if any(
            str(item.get("method") or "") in _TRUSTED_EXACT_ALIAS_METHODS
            for item in evidence
        ):
            return True
        if (
            len(
                {
                    str(item.get("chunk_id") or "")
                    for item in evidence
                    if item.get("chunk_id")
                }
            )
            >= 2
        ):
            return True
    return False


def _evidence_adjusted_score(match: dict[str, Any]) -> float:
    """Combine ANN relevance with source-evidence strength.

    The lexicon can contain valid but weak one-off extraction phrases. They
    remain auditable in Mongo, while high-support defined concepts receive a
    bounded ranking prior so generic surface overlap cannot crowd them out.
    """

    raw = float(match.get("score") or 0.0)
    if "exact" in str(match.get("match_type") or ""):
        return max(raw, 1.0)
    support = max(0, int(match.get("support_count") or 0))
    bonus = min(0.12, math.log1p(support) * 0.018)
    if match.get("definitions"):
        bonus += 0.08
    if match.get("aliases") or match.get("abbreviations"):
        bonus += 0.02
    if (
        match.get("factual_relations")
        or match.get("application_contexts")
        or match.get("components")
        or match.get("component_of")
    ):
        bonus += 0.05
    if (
        support <= 1
        and not match.get("definitions")
        and not match.get("factual_relations")
        and not match.get("application_contexts")
        and not match.get("components")
        and not match.get("component_of")
    ):
        bonus -= 0.12
    return round(raw + bonus, 6)


def _safe_match(match: dict[str, Any]) -> dict[str, Any]:
    """Remove vectors/internal fields before diagnostics leave the retriever."""

    safe = {key: value for key, value in match.items() if not key.startswith("_")}
    for field, cap in {
        "aliases": 12,
        "abbreviations": 8,
        "definitions": 4,
        "structural_contexts": 12,
        "contextual_usages": 12,
        "entity_ids": 16,
        "source_document_ids": 24,
        "source_document_support": 24,
        "source_parent_ids": 32,
        "source_chunk_ids": 32,
        "components": 12,
        "component_of": 12,
        "application_contexts": 12,
        "factual_relations": 16,
        "cooccurrence_neighbors": 12,
        "mutual_semantic_neighbors": 6,
    }.items():
        if isinstance(safe.get(field), list):
            safe[field] = safe[field][:cap]
    if safe.get("retrieval_gloss"):
        safe["retrieval_gloss"] = str(safe["retrieval_gloss"])[:1800]
    if safe.get("embedding_gloss"):
        safe["embedding_gloss"] = str(safe["embedding_gloss"])[:900]
    if safe.get("utility_gloss"):
        safe["utility_gloss"] = str(safe["utility_gloss"])[:900]
    return safe


_ASSOCIATION_FIELDS: tuple[tuple[str, float, bool], ...] = (
    ("component_of", 1.0, True),
    ("components", 0.96, True),
    ("application_contexts", 0.92, True),
    ("factual_relations", 0.88, True),
    ("cooccurrence_neighbors", 0.48, False),
)


def _grounded_association_targets(
    seeds: list[dict[str, Any]],
    *,
    limit: int = 48,
) -> list[dict[str, Any]]:
    """Rank bounded, source-backed concept links from accepted seed concepts.

    This is the deterministic Stage 2 vocabulary walk. It follows links already
    present in the corpus lexicon; it does not invent synonyms or infer a new
    relation from the query. Co-occurrence is retained as a lower-priority
    exploratory signal and must have repeated source support.
    """

    ranked: dict[str, dict[str, Any]] = {}
    for seed in seeds:
        seed_key = normalize_identity(
            str(seed.get("canonical_key") or seed.get("term") or "")
        )
        seed_name = str(seed.get("term") or seed.get("canonical_name") or "")
        seed_specific = bool(
            len(_content_terms(seed_name)) >= 2
            or _UPPER_TOKEN_RE.fullmatch(seed_name.strip())
        )
        seed_score = float(
            seed.get("evidence_adjusted_score") or seed.get("score") or 0.0
        )
        seed_direct = str(seed.get("applicability") or "") == "direct"
        seed_overlap = max(0, int(seed.get("overlap_count") or 0))
        for field, field_weight, factual in _ASSOCIATION_FIELDS:
            relations = [
                relation
                for relation in (seed.get(field) or [])
                if isinstance(relation, dict)
            ]
            # A broad one-word identity such as ``face`` may have hundreds of
            # typed children. Without degree normalization those links crowd
            # out a narrow, source-proven bridge such as a specific visibility
            # code belonging to a canonical coding system. Specific concepts
            # and acronyms retain their full relationship signal.
            fanout_damping = (
                1.0
                if seed_specific or len(relations) <= 1
                else 1.0 / math.sqrt(len(relations))
            )
            for relation in relations:
                target_key = normalize_identity(
                    str(
                        relation.get("target_lexicon_key")
                        or relation.get("target_key")
                        or relation.get("target")
                        or ""
                    )
                )
                if not target_key or target_key == seed_key:
                    continue
                support = max(0, int(relation.get("support_count") or 0))
                confidence = max(0.0, float(relation.get("confidence") or 0.0))
                if not factual and support < 2:
                    continue
                association_score = (
                    field_weight * fanout_damping
                    + min(0.12, confidence * 0.12)
                    + min(0.12, math.log1p(support) * 0.035)
                    + min(0.12, seed_score * 0.12)
                    + (0.08 if seed_direct else 0.0)
                    + min(0.12, seed_overlap * 0.05)
                    - (0.16 if seed_overlap == 0 else 0.0)
                )
                candidate = {
                    "target_key": target_key,
                    "association_field": field,
                    "association_score": round(association_score, 6),
                    "factual": factual,
                    "seed_lexicon_id": str(seed.get("lexicon_id") or ""),
                    "seed_canonical_name": str(
                        seed.get("term") or seed.get("canonical_name") or ""
                    ),
                    "seed_specific": seed_specific,
                    "seed_overlap_count": seed_overlap,
                    "seed_field_degree": len(relations),
                    "fanout_damping": round(fanout_damping, 6),
                    "predicate": str(relation.get("predicate") or ""),
                    "direction": str(relation.get("direction") or ""),
                    "confidence": confidence,
                    "support_count": support,
                    "chunk_id": str(relation.get("chunk_id") or ""),
                    "parent_id": str(relation.get("parent_id") or ""),
                    "evidence_phrase": str(relation.get("evidence_phrase") or "")[:500],
                }
                current = ranked.get(target_key)
                if current is None or association_score > float(
                    current.get("association_score") or 0.0
                ):
                    ranked[target_key] = candidate
    return sorted(
        ranked.values(),
        key=lambda row: (
            -float(row.get("association_score") or 0.0),
            str(row.get("target_key") or ""),
        ),
    )[: max(1, int(limit))]


async def _expand_grounded_associations(
    qdrant_client: Any,
    *,
    corpus_id: str,
    seeds: list[dict[str, Any]],
    disabled: set[str],
    query_lanes: list[dict[str, Any]],
    limit: int = 3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve association targets to canonical, source-proven lexicon rows."""

    from services.storage.qdrant_writer import search_lexicon_entries

    targets = _grounded_association_targets(seeds)
    if not targets or limit <= 0:
        return [], []
    target_by_key = {str(row["target_key"]): row for row in targets}
    rows = await search_lexicon_entries(
        qdrant_client,
        corpus_id,
        query_vec=None,
        exact_terms=list(target_by_key),
        top_k=max(16, len(target_by_key) * 2),
        with_vectors=False,
    )
    seed_ids = {str(seed.get("lexicon_id") or "") for seed in seeds}
    expanded: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        lexicon_id = str(row.get("lexicon_id") or "")
        if not lexicon_id or lexicon_id in seed_ids or lexicon_id in disabled:
            continue
        identity_keys = {
            normalize_identity(str(value or ""))
            for value in [
                row.get("canonical_key"),
                row.get("term"),
                *(row.get("member_keys") or []),
                *(row.get("aliases_normalized") or []),
                *(row.get("abbreviations_normalized") or []),
            ]
            if str(value or "")
        }
        evidence = max(
            (target_by_key[key] for key in identity_keys if key in target_by_key),
            key=lambda item: float(item.get("association_score") or 0.0),
            default=None,
        )
        if evidence is None or row.get("retrieval_eligible", True) is False:
            continue
        target_overlap = max(
            (_match_overlap(str(lane.get("query") or ""), row) for lane in query_lanes),
            default=(0, 0.0),
        )
        seed_name = str(evidence.get("seed_canonical_name") or "")
        seed_specific = bool(
            len(_content_terms(seed_name)) >= 2
            or _UPPER_TOKEN_RE.fullmatch(seed_name.strip())
        )
        # A single generic seed such as "actor" or "scene" must not turn an
        # arbitrary neighbor into a query rewrite. Typed links from a specific
        # seed remain valid; co-occurrence additionally requires target-query
        # overlap because it is associative rather than factual evidence.
        association_field = str(evidence.get("association_field") or "")
        if target_overlap[0] == 0 and (
            association_field == "cooccurrence_neighbors" or not seed_specific
        ):
            rejected.append(
                {
                    "corpus_id": corpus_id,
                    "lexicon_id": lexicon_id,
                    "canonical_name": row.get("term") or row.get("canonical_name"),
                    "reason": "association_not_query_grounded",
                    "association_field": association_field,
                    "seed_lexicon_id": evidence.get("seed_lexicon_id"),
                }
            )
            continue
        row = dict(row)
        row.update(
            {
                "corpus_id": corpus_id,
                "match_type": "grounded_association",
                "score": round(
                    min(0.94, float(evidence["association_score"]) * 0.68),
                    6,
                ),
                "evidence_adjusted_score": round(
                    min(0.98, float(evidence["association_score"]) * 0.72),
                    6,
                ),
                "overlap_count": 0,
                "overlap_ratio": 0.0,
                "applicability": "corpus_association",
                "required": False,
                "stores": ["qdrant"],
                "association_evidence": evidence,
                "overlap_count": target_overlap[0],
                "overlap_ratio": round(target_overlap[1], 4),
            }
        )
        expanded.append(row)
    return (
        sorted(
            expanded,
            key=lambda row: (
                -float(row.get("evidence_adjusted_score") or 0.0),
                str(row.get("canonical_key") or ""),
            ),
        )[: max(0, int(limit))],
        rejected,
    )


def _definition_reference_terms(
    seeds: list[dict[str, Any]],
    *,
    limit: int = 128,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Mine exact corpus concepts named inside accepted source-backed context."""

    evidence_by_phrase: dict[str, dict[str, Any]] = {}
    ranked: list[tuple[int, float, str]] = []
    for seed in seeds:
        seed_name = str(seed.get("term") or seed.get("canonical_name") or "")
        seed_score = float(
            seed.get("evidence_adjusted_score") or seed.get("score") or 0.0
        )
        contexts = [
            (
                "definition",
                str(item.get("text") or ""),
                str(item.get("chunk_id") or ""),
                str(item.get("parent_id") or ""),
            )
            for item in (seed.get("definitions") or [])[:4]
        ]
        contexts.extend(
            (
                "contextual_usage",
                str(item.get("text") or ""),
                str(item.get("chunk_id") or ""),
                str(item.get("parent_id") or ""),
            )
            for item in (seed.get("contextual_usages") or [])[:4]
        )
        for source_field, text, chunk_id, parent_id in contexts:
            for raw_acronym in _UPPER_TOKEN_RE.findall(text):
                acronym = normalize_identity(raw_acronym)
                if len(acronym) < 3:
                    continue
                current = evidence_by_phrase.get(acronym)
                if current is None or seed_score > float(
                    current.get("seed_score") or 0.0
                ):
                    evidence_by_phrase[acronym] = {
                        "seed_lexicon_id": str(seed.get("lexicon_id") or ""),
                        "seed_canonical_name": seed_name,
                        "seed_score": seed_score,
                        "source_field": source_field,
                        "evidence_phrase": text[:500],
                        "chunk_id": chunk_id,
                        "parent_id": parent_id,
                    }
                ranked.append((7, seed_score, acronym))
            tokens = normalize_identity(text).split()[:80]
            for size in range(min(6, len(tokens)), 1, -1):
                for start in range(len(tokens) - size + 1):
                    phrase = " ".join(tokens[start : start + size]).strip()
                    content = _content_terms(phrase)
                    if (
                        len(content) < 2
                        or len(phrase) < 9
                        or tokens[start] in _QUERY_STOPWORDS
                        or tokens[start + size - 1] in _QUERY_STOPWORDS
                    ):
                        continue
                    current = evidence_by_phrase.get(phrase)
                    if current is None or seed_score > float(
                        current.get("seed_score") or 0.0
                    ):
                        evidence_by_phrase[phrase] = {
                            "seed_lexicon_id": str(seed.get("lexicon_id") or ""),
                            "seed_canonical_name": seed_name,
                            "seed_score": seed_score,
                            "source_field": source_field,
                            "evidence_phrase": text[:500],
                            "chunk_id": chunk_id,
                            "parent_id": parent_id,
                        }
                    ranked.append((size, seed_score, phrase))
    ordered = [
        phrase
        for _size, _score, phrase in sorted(
            set(ranked),
            key=lambda item: (-item[0], -item[1], item[2]),
        )
    ]
    ordered = list(dict.fromkeys(ordered))[: max(1, int(limit))]
    return ordered, {phrase: evidence_by_phrase[phrase] for phrase in ordered}


async def _expand_definition_references(
    qdrant_client: Any,
    *,
    corpus_id: str,
    seeds: list[dict[str, Any]],
    disabled: set[str],
    query_lanes: list[dict[str, Any]],
    limit: int = 3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve concept names explicitly cited by a seed's definition/usage."""

    from services.storage.qdrant_writer import search_lexicon_entries

    terms, evidence_by_phrase = _definition_reference_terms(seeds)
    if not terms or limit <= 0:
        return [], []
    rows = await search_lexicon_entries(
        qdrant_client,
        corpus_id,
        query_vec=None,
        exact_terms=terms,
        top_k=max(24, limit * 8),
        with_vectors=False,
    )
    seed_ids = {str(seed.get("lexicon_id") or "") for seed in seeds}
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        lexicon_id = str(row.get("lexicon_id") or "")
        if (
            not lexicon_id
            or lexicon_id in seed_ids
            or lexicon_id in disabled
            or row.get("retrieval_eligible", True) is False
        ):
            continue
        identity_keys = {
            normalize_identity(str(value or ""))
            for value in [
                row.get("canonical_key"),
                row.get("term"),
                *(row.get("member_keys") or []),
                *(row.get("aliases_normalized") or []),
                *(row.get("abbreviations_normalized") or []),
            ]
            if str(value or "")
        }
        referenced_phrase = next(
            (term for term in terms if term in identity_keys),
            "",
        )
        evidence = evidence_by_phrase.get(referenced_phrase)
        if evidence is None:
            continue
        target_overlap = max(
            (_match_overlap(str(lane.get("query") or ""), row) for lane in query_lanes),
            default=(0, 0.0),
        )
        seed_name = str(evidence.get("seed_canonical_name") or "")
        seed_specific = bool(
            len(_content_terms(seed_name)) >= 2
            or _UPPER_TOKEN_RE.fullmatch(seed_name.strip())
        )
        if target_overlap[0] == 0 and not seed_specific:
            rejected.append(
                {
                    "corpus_id": corpus_id,
                    "lexicon_id": lexicon_id,
                    "canonical_name": row.get("term"),
                    "reason": "definition_reference_from_generic_seed",
                    "seed_lexicon_id": evidence.get("seed_lexicon_id"),
                }
            )
            continue
        score = min(0.96, 0.68 + float(evidence.get("seed_score") or 0.0) * 0.24)
        expanded = dict(row)
        expanded.update(
            {
                "corpus_id": corpus_id,
                "match_type": "definition_reference",
                "score": round(score, 6),
                "evidence_adjusted_score": round(min(0.98, score + 0.03), 6),
                "overlap_count": target_overlap[0],
                "overlap_ratio": round(target_overlap[1], 4),
                "applicability": "corpus_definition_reference",
                "required": False,
                "stores": ["qdrant"],
                "definition_reference_evidence": {
                    **evidence,
                    "referenced_phrase": referenced_phrase,
                },
            }
        )
        accepted.append(expanded)
        if len(accepted) >= limit:
            break
    return accepted, rejected


async def definition_reference_vocabulary_matches(
    qdrant_client: Any,
    *,
    corpus_id: str,
    seeds: list[dict[str, Any]],
    disabled_lexicon_ids: list[str] | None = None,
    query_lanes: list[dict[str, Any]] | None = None,
    limit: int = 3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve canonical concepts explicitly cited by source-backed cards."""

    return await _expand_definition_references(
        qdrant_client,
        corpus_id=corpus_id,
        seeds=seeds,
        disabled={
            str(value) for value in (disabled_lexicon_ids or []) if str(value)
        },
        query_lanes=list(query_lanes or []),
        limit=limit,
    )


async def _mongo_matches(
    db: Any,
    *,
    corpus_id: str,
    query: str,
    exact_terms: list[str],
    top_k: int,
) -> list[dict[str, Any]]:
    if db is None:
        return []
    collection = db["corpus_lexicon"]
    exact = (
        await collection.find(
            {
                "corpus_id": corpus_id,
                "$or": [
                    {"canonical_key": {"$in": exact_terms}},
                    {"member_keys": {"$in": exact_terms}},
                    {"aliases_normalized": {"$in": exact_terms}},
                    {"abbreviations_normalized": {"$in": exact_terms}},
                ],
            },
            {"_id": 0},
        )
        .limit(max(8, top_k * 2))
        .to_list(length=max(8, top_k * 2))
    )
    for row in exact:
        row["term"] = row.get("canonical_name")
        row["score"] = 1.0
        row["match_type"] = "mongo_exact_alias"
        row["store"] = "mongo"

    fuzzy: list[dict[str, Any]] = []
    long_phrases = [
        term for term in exact_terms if len(term) >= 7 and len(term.split()) <= 6
    ]
    if len(exact) < top_k and long_phrases:
        try:
            candidates = (
                await collection.find(
                    {
                        "corpus_id": corpus_id,
                        "$text": {"$search": " ".join(long_phrases[:12])},
                    },
                    {"_id": 0, "text_score": {"$meta": "textScore"}},
                )
                .sort([("text_score", {"$meta": "textScore"})])
                .limit(32)
                .to_list(length=32)
            )
            for row in candidates:
                labels = [
                    str(row.get("canonical_key") or ""),
                    *(str(value) for value in (row.get("aliases_normalized") or [])),
                ]
                ratio = max(
                    (
                        SequenceMatcher(None, phrase, label).ratio()
                        for phrase in long_phrases
                        for label in labels
                        if len(label) >= 7
                    ),
                    default=0.0,
                )
                if ratio < 0.88:
                    continue
                row["term"] = row.get("canonical_name")
                row["score"] = round(ratio, 4)
                row["match_type"] = "mongo_fuzzy_alias"
                row["store"] = "mongo"
                fuzzy.append(row)
        except Exception:
            # Text index absence or an operator-disabled fuzzy lane must not
            # affect Qdrant exact/gloss resolution.
            fuzzy = []
    return [*exact, *fuzzy]


async def _graph_neighbors(
    driver: Any,
    *,
    corpus_id: str,
    entity_ids: list[str],
) -> list[dict[str, Any]]:
    if driver is None or not entity_ids:
        return []
    query = """
    MATCH (e:Entity)-[r:RELATES_TO]-(n:Entity)
    WHERE e.entity_id IN $entity_ids
      AND (
        r.corpus_id = $corpus_id OR $corpus_id IN coalesce(r.corpus_ids, []) OR
        e.corpus_id = $corpus_id OR $corpus_id IN coalesce(e.corpus_ids, [])
      )
    WITH e, n, r ORDER BY coalesce(r.confidence, 0.0) DESC
    RETURN e.entity_id AS source_entity_id,
           n.entity_id AS target_entity_id,
           coalesce(n.display_name, n.canonical_name, n.entity_id) AS target,
           r.predicate AS predicate,
           coalesce(r.confidence, 0.0) AS confidence
    LIMIT 24
    """
    async with driver.session() as session:
        result = await session.run(
            query,
            corpus_id=corpus_id,
            entity_ids=list(dict.fromkeys(entity_ids))[:24],
        )
        return [dict(row) async for row in result]


async def _qdrant_document_profiles(
    qdrant_client: Any,
    *,
    corpus_id: str,
    doc_ids: list[str],
    limit: int = 64,
) -> list[dict[str, Any]]:
    """Load source-backed root routing cards without crossing store tiers."""

    scoped_ids = list(dict.fromkeys(value for value in doc_ids if value))[:64]
    if not scoped_ids or qdrant_client is None:
        return []
    try:
        points, _offset = await qdrant_client.scroll(
            collection_name=SHARED_DOCSUM,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="corpus_id",
                        match=models.MatchValue(value=corpus_id),
                    ),
                    models.FieldCondition(
                        key="doc_id",
                        match=models.MatchAny(any=scoped_ids),
                    ),
                ]
            ),
            limit=max(1, min(int(limit), 64)),
            with_payload=True,
            with_vectors=False,
        )
    except Exception:
        return []
    profiles: list[dict[str, Any]] = []
    for point in points or []:
        payload = dict(getattr(point, "payload", None) or {})
        doc_id = str(payload.get("doc_id") or "")
        summary = str(payload.get("summary") or "").strip()
        if not doc_id or not summary:
            continue
        profiles.append(
            {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "title": str(payload.get("title") or ""),
                "summary": summary[:1800],
                "concepts": [
                    str(value) for value in (payload.get("concepts") or [])[:24]
                ],
                "section_ids": [
                    str(value) for value in (payload.get("section_ids") or [])[:32]
                ],
                "node_type": "document",
                "store": "qdrant_document_profile",
            }
        )
    return profiles


async def _mongo_raptor_ancestors(
    db: Any,
    *,
    corpus_id: str,
    doc_ids: list[str],
    source_parent_ids: list[str],
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Load bounded section/rollup ancestors for Hybrid and Graph planning."""

    scoped_docs = list(dict.fromkeys(value for value in doc_ids if value))[:32]
    if db is None or not scoped_docs:
        return []
    rows = (
        await db["summary_tree"]
        .find(
            {
                "corpus_id": corpus_id,
                "doc_id": {"$in": scoped_docs},
                "node_type": {"$in": ["section", "rollup"]},
                "summary": {"$type": "string", "$ne": ""},
            },
            {
                "_id": 0,
                "node_id": 1,
                "node_type": 1,
                "doc_id": 1,
                "summary": 1,
                "parent_ids": 1,
                "child_node_ids": 1,
                "section_range": 1,
            },
        )
        .limit(160)
        .to_list(length=160)
    )
    source_parents = set(source_parent_ids)

    def priority(row: dict[str, Any]) -> tuple[int, int, str]:
        parent_overlap = len(source_parents & set(row.get("parent_ids") or []))
        node_rank = 0 if str(row.get("node_type") or "") == "section" else 1
        return (-parent_overlap, node_rank, str(row.get("node_id") or ""))

    output: list[dict[str, Any]] = []
    for row in sorted(rows, key=priority)[: max(1, min(int(limit), 40))]:
        output.append(
            {
                "corpus_id": corpus_id,
                "doc_id": str(row.get("doc_id") or ""),
                "node_id": str(row.get("node_id") or ""),
                "node_type": str(row.get("node_type") or ""),
                "summary": str(row.get("summary") or "")[:1800],
                "parent_ids": [
                    str(value) for value in (row.get("parent_ids") or [])[:48]
                ],
                "child_node_ids": [
                    str(value) for value in (row.get("child_node_ids") or [])[:48]
                ],
                "source_parent_overlap": len(
                    source_parents & set(row.get("parent_ids") or [])
                ),
                "store": "mongo_summary_tree",
            }
        )
    return output


async def _mutual_semantic_neighbors(
    qdrant_client: Any,
    *,
    corpus_id: str,
    match: dict[str, Any],
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return only vector neighbors that retrieve the seed in reverse."""

    from services.storage.qdrant_writer import search_lexicon_entries

    vector = match.get("_vector")
    seed_id = str(match.get("lexicon_id") or "")
    if vector is None or not seed_id:
        return []
    candidates = await search_lexicon_entries(
        qdrant_client,
        corpus_id,
        query_vec=vector,
        top_k=max(4, limit + 1),
        score_threshold=0.55,
        with_vectors=True,
    )
    output: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = str(candidate.get("lexicon_id") or "")
        candidate_vector = candidate.get("_vector")
        if not candidate_id or candidate_id == seed_id or candidate_vector is None:
            continue
        reverse = await search_lexicon_entries(
            qdrant_client,
            corpus_id,
            query_vec=candidate_vector,
            top_k=6,
            score_threshold=0.55,
            with_vectors=False,
        )
        if not any(str(row.get("lexicon_id") or "") == seed_id for row in reverse):
            continue
        output.append(
            {
                "lexicon_id": candidate_id,
                "canonical_name": candidate.get("term"),
                "score": round(float(candidate.get("score") or 0.0), 4),
                "association": "mutual_gloss_similarity",
                "directional": False,
                "factual": False,
            }
        )
        if len(output) >= limit:
            break
    return output


class CorpusVocabularyResolver:
    async def resolve(
        self,
        *,
        query: str,
        corpus_ids: list[str] | None,
        tier: RetrievalTier,
        query_vector: list[float] | None,
        qdrant_client: Any,
        db: Any = None,
        neo4j_driver: Any = None,
        top_k_per_corpus: int = 6,
        disabled_lexicon_ids: list[str] | None = None,
        excluded_terms: list[str] | None = None,
        query_lanes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        started = perf_counter()
        search_lanes: list[dict[str, Any]] = [
            {
                "lane_id": "original",
                "query": query,
                "query_vector": query_vector,
            }
        ]
        seen_lane_queries = {normalize_identity(query)}
        for lane in query_lanes or []:
            lane_query = str(lane.get("query") or lane.get("dense_text") or "").strip()
            normalized_lane_query = normalize_identity(lane_query)
            if not lane_query or normalized_lane_query in seen_lane_queries:
                continue
            seen_lane_queries.add(normalized_lane_query)
            search_lanes.append(
                {
                    "lane_id": str(lane.get("lane_id") or "lane"),
                    "query": lane_query,
                    "query_vector": lane.get("query_vector"),
                }
            )
            if len(search_lanes) >= 8:
                break
        exact_terms = list(
            dict.fromkeys(
                term
                for lane in search_lanes
                for term in query_exact_terms(str(lane["query"]))
            )
        )[:192]
        disabled = {str(value) for value in (disabled_lexicon_ids or [])[:64] if value}
        exclusions = [
            str(value).strip()
            for value in (excluded_terms or [])[:32]
            if str(value).strip()
        ]
        # P1.7 — resolution cache: key on query/lanes/corpus-set/knobs plus
        # per-corpus epochs; query vectors are deliberately excluded (they are
        # deterministic per query text under the deployed embedder).
        _resolution_cache_key = None
        if vocabulary_cache.enabled():
            _resolution_cache_key = vocabulary_cache.resolution_cache_key(
                query=query,
                corpus_ids=corpus_ids,
                tier=tier,
                top_k_per_corpus=top_k_per_corpus,
                lane_queries=[
                    (str(lane["lane_id"]), str(lane["query"]))
                    for lane in search_lanes
                ],
                disabled_lexicon_ids=sorted(disabled),
                excluded_terms=exclusions,
            )
            cached = vocabulary_cache.get(_resolution_cache_key)
            if cached is not None:
                cached["cache"] = {"hit": True}
                cached["duration_s"] = round(perf_counter() - started, 4)
                return cached
        per_corpus: dict[str, Any] = {}
        accepted_global: list[dict[str, Any]] = []
        document_profiles_global: list[dict[str, Any]] = []
        raptor_ancestors_global: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        degraded_stores: list[dict[str, str]] = []
        store_usage = {"qdrant": True, "mongo": False, "neo4j": False}
        from services.storage.qdrant_writer import search_lexicon_entries

        scoped_corpora = list(dict.fromkeys(corpus_ids or []))
        mongo_enabled = tier in {
            RetrievalTier.qdrant_mongo,
            RetrievalTier.qdrant_mongo_graph,
        }
        graph_enabled = tier == RetrievalTier.qdrant_mongo_graph
        store_usage["mongo"] = mongo_enabled
        store_usage["neo4j"] = graph_enabled

        async def candidate_rows(
            corpus_id: str,
        ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], str | None]:
            async def lane_rows(lane: dict[str, Any]) -> list[dict[str, Any]]:
                lane_query = str(lane["query"])
                lane_id = str(lane["lane_id"])
                lane_exact_terms = query_exact_terms(lane_query)
                qdrant_rows = await search_lexicon_entries(
                    qdrant_client,
                    corpus_id,
                    query_vec=lane.get("query_vector"),
                    exact_terms=lane_exact_terms,
                    top_k=max(16, top_k_per_corpus * 3),
                    score_threshold=0.32,
                    with_vectors=True,
                )
                return [
                    {
                        **row,
                        "_matched_lane_id": lane_id,
                        "_matched_lane_query": lane_query,
                        "_matched_lane_dense_rank": row.get("dense_rank"),
                    }
                    for row in qdrant_rows
                ]

            # Mongo exact/text lookup is corpus-scoped, not lane-scoped. Running
            # the same text query once per concept lane multiplied Hybrid latency
            # without adding store evidence. It runs concurrently with Qdrant
            # and has its own deadline; a slow legacy text index cannot erase
            # the already-available dense/exact concept bridge.
            lane_task = asyncio.gather(*(lane_rows(lane) for lane in search_lanes))
            mongo_task = (
                asyncio.wait_for(
                    _mongo_matches(
                        db,
                        corpus_id=corpus_id,
                        query=query,
                        exact_terms=exact_terms,
                        top_k=max(top_k_per_corpus, 8),
                    ),
                    timeout=_MONGO_LOOKUP_DEADLINE_SECONDS,
                )
                if mongo_enabled
                else asyncio.sleep(0, result=[])
            )
            lane_batches, mongo_result = await asyncio.gather(
                lane_task,
                mongo_task,
                return_exceptions=True,
            )
            if isinstance(lane_batches, BaseException):
                raise lane_batches
            mongo_error = (
                f"{type(mongo_result).__name__}: {mongo_result}"[:240]
                if isinstance(mongo_result, BaseException)
                else None
            )
            mongo_rows = [] if mongo_error else list(mongo_result or [])
            return (
                corpus_id,
                [row for qdrant_rows in lane_batches for row in qdrant_rows],
                [
                    {
                        **row,
                        "_matched_lane_id": "mongo_lexical",
                        "_matched_lane_query": query,
                    }
                    for row in mongo_rows
                ],
                mongo_error,
            )

        candidate_batches = await asyncio.gather(
            *(candidate_rows(corpus_id) for corpus_id in scoped_corpora)
        )
        for corpus_id, qdrant_rows, mongo_rows, mongo_error in candidate_batches:
            if mongo_error:
                degraded_stores.append(
                    {
                        "store": "mongo_lexicon",
                        "corpus_id": corpus_id,
                        "error": mongo_error,
                    }
                )
            merged: dict[str, dict[str, Any]] = {}
            for row in [*qdrant_rows, *mongo_rows]:
                lexicon_id = str(row.get("lexicon_id") or "")
                if not lexicon_id or lexicon_id in disabled:
                    continue
                current = merged.get(lexicon_id)
                if current is None:
                    merged[lexicon_id] = dict(row)
                    merged[lexicon_id]["stores"] = [str(row.get("store") or "qdrant")]
                    merged[lexicon_id]["matched_lane_ids"] = [
                        str(row.get("_matched_lane_id") or "original")
                    ]
                    merged[lexicon_id]["matched_lane_queries"] = [
                        str(row.get("_matched_lane_query") or query)
                    ]
                    dense_rank = row.get("_matched_lane_dense_rank")
                    merged[lexicon_id]["dense_rank_by_lane"] = (
                        {
                            str(row.get("_matched_lane_id") or "original"): int(
                                dense_rank
                            )
                        }
                        if dense_rank is not None
                        else {}
                    )
                else:
                    current["stores"] = list(
                        dict.fromkeys(
                            [
                                *current.get("stores", []),
                                str(row.get("store") or "qdrant"),
                            ]
                        )
                    )
                    current["matched_lane_ids"] = list(
                        dict.fromkeys(
                            [
                                *current.get("matched_lane_ids", []),
                                str(row.get("_matched_lane_id") or "original"),
                            ]
                        )
                    )
                    current["matched_lane_queries"] = list(
                        dict.fromkeys(
                            [
                                *current.get("matched_lane_queries", []),
                                str(row.get("_matched_lane_query") or query),
                            ]
                        )
                    )
                    dense_rank = row.get("_matched_lane_dense_rank")
                    if dense_rank is not None:
                        lane_id = str(row.get("_matched_lane_id") or "original")
                        dense_rank_by_lane = current.setdefault(
                            "dense_rank_by_lane", {}
                        )
                        dense_rank_by_lane[lane_id] = min(
                            int(dense_rank_by_lane.get(lane_id) or 1_000_000),
                            int(dense_rank),
                        )
                    if float(row.get("score") or 0.0) > float(
                        current.get("score") or 0.0
                    ):
                        keep_stores = current["stores"]
                        keep_lane_ids = current["matched_lane_ids"]
                        keep_lane_queries = current["matched_lane_queries"]
                        keep_dense_ranks = current["dense_rank_by_lane"]
                        current.update(row)
                        current["stores"] = keep_stores
                        current["matched_lane_ids"] = keep_lane_ids
                        current["matched_lane_queries"] = keep_lane_queries
                        current["dense_rank_by_lane"] = keep_dense_ranks

            eligible: list[dict[str, Any]] = []
            for row in merged.values():
                if "exact" in str(row.get("match_type") or ""):
                    trusted_exact = _trusted_exact_identity_match(row, exact_terms)
                    row["exact_identity_trusted"] = trusted_exact
                    if not trusted_exact:
                        row["match_type"] = "unverified_query_alias"
                        row["score"] = float(row.get("gloss_score") or 0.0)
                if row.get("retrieval_eligible", True) is False:
                    rejected.append(
                        {
                            "corpus_id": corpus_id,
                            "lexicon_id": row.get("lexicon_id"),
                            "canonical_name": row.get("term")
                            or row.get("canonical_name"),
                            "score": round(float(row.get("score") or 0.0), 4),
                            "reason": "lexicon_quality_gate",
                        }
                    )
                    continue
                overlap_lane_id = "original"
                overlap_query = query
                overlap_count = 0
                overlap_ratio = 0.0
                for lane in search_lanes:
                    lane_count, lane_ratio = _match_overlap(str(lane["query"]), row)
                    if (lane_count, lane_ratio) > (overlap_count, overlap_ratio):
                        overlap_count = lane_count
                        overlap_ratio = lane_ratio
                        overlap_lane_id = str(lane["lane_id"])
                        overlap_query = str(lane["query"])
                match_type = str(row.get("match_type") or "")
                score = float(row.get("score") or 0.0)
                exact = "exact" in match_type
                evidence_adjusted = round(
                    _evidence_adjusted_score(row)
                    + min(
                        0.06,
                        math.log1p(len(row.get("matched_lane_ids") or [])) * 0.025,
                    )
                    + min(0.06, overlap_ratio * 0.06),
                    6,
                )
                row["evidence_adjusted_score"] = evidence_adjusted
                excluded_overlap_count, _excluded_overlap_ratio = _match_overlap(
                    " ".join(exclusions), row
                )
                if excluded_overlap_count and (
                    not exact or _exact_surface_is_excluded(row, exclusions)
                ):
                    rejected.append(
                        {
                            "corpus_id": corpus_id,
                            "lexicon_id": row.get("lexicon_id"),
                            "canonical_name": row.get("term")
                            or row.get("canonical_name"),
                            "score": round(score, 4),
                            "reason": "negated_query_concept",
                        }
                    )
                    continue
                support_count = max(0, int(row.get("support_count") or 0))
                overlap_grounded = bool(
                    overlap_count > 0
                    and evidence_adjusted >= 0.66
                    and (score >= 0.66 or support_count >= 3)
                )
                exploratory_grounded = bool(
                    overlap_count == 0 and score >= 0.74 and evidence_adjusted >= 0.72
                )
                accept = exact or overlap_grounded or exploratory_grounded
                applicability = (
                    "direct"
                    if exact
                    else (
                        "source_term_overlap"
                        if overlap_count > 0
                        else "exploratory_semantic"
                    )
                )
                row.update(
                    {
                        "corpus_id": corpus_id,
                        "overlap_count": overlap_count,
                        "overlap_ratio": round(overlap_ratio, 4),
                        "overlap_lane_id": overlap_lane_id,
                        "overlap_query": overlap_query,
                        "applicability": applicability,
                        "required": False,
                        "selection_reason": (
                            "trusted_exact_identity"
                            if exact
                            else (
                                "source_evidence_and_concept_overlap"
                                if overlap_grounded
                                else "high_confidence_gloss_semantic"
                            )
                        ),
                    }
                )
                if not accept:
                    rejected.append(
                        {
                            "corpus_id": corpus_id,
                            "lexicon_id": row.get("lexicon_id"),
                            "canonical_name": row.get("term")
                            or row.get("canonical_name"),
                            "score": round(score, 4),
                            "reason": "below_grounded_expansion_threshold",
                        }
                    )
                    continue
                eligible.append(row)

            def candidate_rank(row: dict[str, Any]) -> tuple[Any, ...]:
                return (
                    -int(row.get("overlap_count") or 0),
                    -float(row.get("overlap_ratio") or 0.0),
                    -float(row.get("evidence_adjusted_score") or 0.0),
                    -float(row.get("score") or 0.0),
                    -int(row.get("support_count") or 0),
                    str(row.get("canonical_key") or ""),
                )

            accepted: list[dict[str, Any]] = []
            accepted_ids: set[str] = set()

            def select(row: dict[str, Any]) -> None:
                lexicon_id = str(row.get("lexicon_id") or "")
                if (
                    not lexicon_id
                    or lexicon_id in accepted_ids
                    or len(accepted) >= top_k_per_corpus
                ):
                    return
                accepted.append(row)
                accepted_ids.add(lexicon_id)

            exact_specific = sorted(
                (
                    row
                    for row in eligible
                    if "exact" in str(row.get("match_type") or "")
                    and (
                        len(_content_terms(str(row.get("term") or ""))) >= 2
                        or bool(
                            set(row.get("abbreviations_normalized") or [])
                            & set(exact_terms)
                        )
                    )
                ),
                key=candidate_rank,
            )
            for row in exact_specific[: max(1, top_k_per_corpus // 2)]:
                select(row)

            # Reserve concept diversity before generic exact anchors can consume
            # the whole vocabulary budget. One strong semantic bridge per narrow
            # lane is enough to expose expert terminology without turning every
            # ANN neighbor into a retrieval obligation.
            semantic_budget = max(1, top_k_per_corpus // 2)
            semantic_selected = 0
            for lane in search_lanes[1:]:
                lane_id = str(lane.get("lane_id") or "")
                lane_candidates = sorted(
                    (
                        row
                        for row in eligible
                        if "exact" not in str(row.get("match_type") or "")
                        and lane_id in (row.get("matched_lane_ids") or [])
                        and str(row.get("lexicon_id") or "") not in accepted_ids
                    ),
                    key=candidate_rank,
                )
                if lane_candidates:
                    select(lane_candidates[0])
                    semantic_selected += 1
                if semantic_selected >= semantic_budget:
                    break

            for row in sorted(eligible, key=candidate_rank):
                select(row)

            (
                definition_matches,
                definition_rejections,
            ) = await _expand_definition_references(
                qdrant_client,
                corpus_id=corpus_id,
                seeds=accepted,
                disabled=disabled,
                query_lanes=search_lanes,
                limit=max(1, min(3, max(1, top_k_per_corpus // 2))),
            )
            rejected.extend(definition_rejections)
            (
                association_matches,
                association_rejections,
            ) = await _expand_grounded_associations(
                qdrant_client,
                corpus_id=corpus_id,
                seeds=accepted,
                disabled=disabled,
                query_lanes=search_lanes,
                limit=max(1, min(3, max(1, top_k_per_corpus // 2))),
            )
            rejected.extend(association_rejections)
            definition_matches = [
                row
                for row in definition_matches
                if str(row.get("lexicon_id") or "") not in accepted_ids
            ]
            accepted.extend(definition_matches)
            accepted_ids.update(
                str(row.get("lexicon_id") or "") for row in definition_matches
            )
            association_matches = [
                row
                for row in association_matches
                if str(row.get("lexicon_id") or "") not in accepted_ids
            ]
            accepted.extend(association_matches)

            source_document_support: dict[str, int] = {}
            for row in accepted:
                supported_ids: set[str] = set()
                for support in row.get("source_document_support") or []:
                    doc_id = str(support.get("doc_id") or "")
                    if not doc_id:
                        continue
                    supported_ids.add(doc_id)
                    source_document_support[doc_id] = source_document_support.get(
                        doc_id, 0
                    ) + int(support.get("support_count") or 0)
                for doc_id in row.get("source_document_ids") or []:
                    normalized_doc_id = str(doc_id or "")
                    if normalized_doc_id and normalized_doc_id not in supported_ids:
                        source_document_support.setdefault(normalized_doc_id, 1)
            source_doc_ids = [
                doc_id
                for doc_id, _support in sorted(
                    source_document_support.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ][:64]
            source_parent_ids = list(
                dict.fromkeys(
                    str(parent_id)
                    for row in accepted
                    for parent_id in (row.get("source_parent_ids") or [])
                    if str(parent_id)
                )
            )
            mutual_coro = (
                _mutual_semantic_neighbors(
                    qdrant_client,
                    corpus_id=corpus_id,
                    match=accepted[0],
                )
                if accepted
                else asyncio.sleep(0, result=[])
            )
            profiles_coro = _qdrant_document_profiles(
                qdrant_client,
                corpus_id=corpus_id,
                doc_ids=source_doc_ids,
            )
            ancestors_coro = (
                _mongo_raptor_ancestors(
                    db,
                    corpus_id=corpus_id,
                    doc_ids=source_doc_ids,
                    source_parent_ids=source_parent_ids,
                )
                if mongo_enabled
                else asyncio.sleep(0, result=[])
            )
            graph_coro = (
                _graph_neighbors(
                    neo4j_driver,
                    corpus_id=corpus_id,
                    entity_ids=[
                        str(entity_id)
                        for row in accepted
                        for entity_id in (row.get("entity_ids") or [])
                    ],
                )
                if graph_enabled and accepted
                else asyncio.sleep(0, result=[])
            )
            enrichment = await asyncio.gather(
                mutual_coro,
                profiles_coro,
                ancestors_coro,
                graph_coro,
                return_exceptions=True,
            )

            def rows_or_empty(index: int) -> list[dict[str, Any]]:
                value = enrichment[index]
                return [] if isinstance(value, BaseException) else list(value or [])

            mutual_neighbors = rows_or_empty(0)
            document_profiles = rows_or_empty(1)
            mongo_ancestors = rows_or_empty(2)
            graph_rows = rows_or_empty(3)
            if accepted:
                accepted[0]["mutual_semantic_neighbors"] = mutual_neighbors
            raptor_ancestors: list[dict[str, Any]] = [
                {
                    **profile,
                    "ancestor_level": "document_root",
                }
                for profile in document_profiles
            ]
            raptor_ancestors.extend(mongo_ancestors)
            safe_matches = [_safe_match(row) for row in accepted]
            for corpus_rank, match in enumerate(safe_matches, start=1):
                match["corpus_rank"] = corpus_rank
            accepted_global.extend(safe_matches)
            document_profiles_global.extend(document_profiles)
            raptor_ancestors_global.extend(raptor_ancestors)
            per_corpus[corpus_id] = {
                "matches": safe_matches,
                "graph_neighbors": graph_rows,
                "document_profiles": document_profiles,
                "raptor_ancestors": raptor_ancestors,
                "match_count": len(safe_matches),
                "definition_reference_match_count": len(definition_matches),
                "association_match_count": len(association_matches),
            }

        accepted_global.sort(
            key=lambda row: (
                0 if str(row.get("applicability") or "") == "direct" else 1,
                -float(
                    row.get("evidence_adjusted_score")
                    or row.get("score")
                    or 0.0
                ),
                -float(row.get("score") or 0.0),
                -int(row.get("support_count") or 0),
                str(row.get("corpus_id") or ""),
                str(row.get("canonical_key") or ""),
            )
        )
        for global_rank, match in enumerate(accepted_global, start=1):
            match["global_rank"] = global_rank

        resolution = {
            "version": VOCABULARY_RESOLVER_VERSION,
            "query": query,
            "query_lanes": [
                {
                    "lane_id": str(lane["lane_id"]),
                    "query": str(lane["query"]),
                    "has_vector": lane.get("query_vector") is not None,
                }
                for lane in search_lanes
            ],
            "exact_terms": exact_terms,
            "matches": accepted_global,
            "document_profiles": document_profiles_global,
            "raptor_ancestors": raptor_ancestors_global,
            "per_corpus": per_corpus,
            "global_search": {
                "mode": "selected_corpus_fanout_global_merge",
                "selected_corpus_ids": scoped_corpora,
                "represented_corpus_ids": list(
                    dict.fromkeys(
                        str(row.get("corpus_id") or "")
                        for row in accepted_global
                        if str(row.get("corpus_id") or "")
                    )
                ),
                "per_corpus_reservation": max(1, int(top_k_per_corpus)),
                "match_count": len(accepted_global),
            },
            "association_expansion_count": sum(
                int(row.get("association_match_count") or 0)
                for row in per_corpus.values()
            ),
            "definition_reference_expansion_count": sum(
                int(row.get("definition_reference_match_count") or 0)
                for row in per_corpus.values()
            ),
            "rejected_expansions": rejected[:24],
            "disabled_lexicon_ids": sorted(disabled),
            "excluded_terms": exclusions,
            "store_usage": store_usage,
            "degraded_stores": degraded_stores,
            "duration_s": round(perf_counter() - started, 4),
        }
        # P1.7 — cache by query/lanes/corpus-set/knobs + per-corpus epoch so
        # repeated conversational queries skip the slowest pre-retrieval stage.
        resolution["cache"] = {"hit": False}
        if _resolution_cache_key is not None:
            vocabulary_cache.put(_resolution_cache_key, resolution)
        return resolution


def grounded_vocabulary_lanes(
    plan: QueryPlanV2,
    resolution: dict[str, Any],
    *,
    max_translation_lanes: int = 3,
    max_translation_lanes_per_corpus: int = 1,
) -> tuple[list[QueryLane], dict[str, Any]]:
    """Create optional translated and step-back lanes from proven corpus terms."""

    lanes: list[QueryLane] = []
    used_ids: list[str] = []
    existing_text = normalize_identity(" ".join(lane.dense_text for lane in plan.lanes))
    executable_applicabilities = {
        "direct",
        "hierarchy_bound",
        "corpus_definition_reference",
    }

    def execution_eligible(row: dict[str, Any]) -> bool:
        applicability = str(row.get("applicability") or "")
        if applicability in executable_applicabilities:
            return True
        if applicability == "corpus_association":
            evidence = row.get("association_evidence") or {}
            evidence_score = float(
                row.get("evidence_adjusted_score") or row.get("score") or 0.0
            )
            return bool(
                evidence.get("factual") is True
                and evidence.get("seed_specific") is True
                and str(evidence.get("association_field") or "")
                in {
                    "component_of",
                    "components",
                    "application_contexts",
                    "factual_relations",
                }
                and float(evidence.get("confidence") or 0.0) >= 0.8
                and evidence_score >= 0.82
                and int(row.get("support_count") or 0) >= 3
            )
        if applicability != "source_term_overlap":
            return False
        evidence_score = float(
            row.get("evidence_adjusted_score") or row.get("score") or 0.0
        )
        return bool(
            evidence_score >= 0.74
            and int(row.get("support_count") or 0) >= 8
            and (
                row.get("definitions")
                or row.get("factual_relations")
                or row.get("application_contexts")
                or row.get("components")
                or row.get("component_of")
            )
        )

    skipped_non_executable_ids = [
        str(row.get("lexicon_id") or "")
        for row in (resolution.get("matches") or [])
        if not execution_eligible(row) and str(row.get("lexicon_id") or "")
    ]
    ranked_matches = sorted(
        (
            row
            for row in (resolution.get("matches") or [])
            if execution_eligible(row)
            if normalize_identity(
                str(row.get("term") or row.get("canonical_name") or "")
            )
            not in existing_text
        ),
        key=lambda row: (
            {
                "direct": 0,
                "corpus_association": 1,
                "corpus_definition_reference": 2,
                "hierarchy_bound": 3,
                "source_term_overlap": 4,
                "exploratory_semantic": 5,
            }.get(str(row.get("applicability") or ""), 5),
            -int(row.get("overlap_count") or 0),
            -float(row.get("evidence_adjusted_score") or row.get("score") or 0.0),
            str(row.get("canonical_name") or row.get("term") or ""),
        ),
    )
    matches_by_corpus: dict[str, list[dict[str, Any]]] = {}
    for row in ranked_matches:
        matches_by_corpus.setdefault(str(row.get("corpus_id") or ""), []).append(row)
    matches: list[dict[str, Any]] = []
    for offset in range(max(1, int(max_translation_lanes_per_corpus))):
        for corpus_id in sorted(matches_by_corpus):
            corpus_matches = matches_by_corpus[corpus_id]
            if offset < len(corpus_matches):
                matches.append(corpus_matches[offset])
            if len(matches) >= max(1, int(max_translation_lanes)):
                break
        if len(matches) >= max(1, int(max_translation_lanes)):
            break
    lane_lexicon_ids: dict[str, list[str]] = {}
    for row in matches:
        canonical = str(row.get("term") or row.get("canonical_name") or "").strip()
        lexicon_id = str(row.get("lexicon_id") or "")
        if not canonical or not lexicon_id:
            continue
        canonical_key = normalize_identity(canonical)
        if canonical_key and canonical_key in existing_text:
            continue
        gloss = str(row.get("retrieval_gloss") or "").strip()
        aliases = [str(value) for value in (row.get("aliases") or []) if str(value)]
        support_phrases = tuple(dict.fromkeys([canonical, *aliases[:5]]))
        lane_id = "translation_" + lexicon_id[:12]
        dense_text = (
            f"{plan.standalone_query} Corpus-grounded concept: {canonical}. {gloss}"
        ).strip()
        lanes.append(
            QueryLane(
                lane_id=lane_id,
                role="core",
                query=f"How does {canonical} apply to: {plan.standalone_query}",
                dense_text=dense_text,
                lexical_terms=tuple(
                    dict.fromkeys(
                        [
                            *lexical_terms(canonical),
                            *lexical_terms(" ".join(aliases[:3])),
                        ]
                    )
                ),
                required=False,
                phrase=canonical,
                support_phrases=support_phrases,
            )
        )
        lane_lexicon_ids[lane_id] = [lexicon_id]
        used_ids.append(lexicon_id)
        if len(used_ids) >= max_translation_lanes:
            break

    step_back_lanes: list[str] = []
    if lanes and (
        plan.complexity in {"comparative", "dependent_multi_hop"}
        or plan.answer_shape == "relationship"
    ):
        seed_id = used_ids[0]
        seed = next(
            (row for row in matches if str(row.get("lexicon_id") or "") == seed_id),
            None,
        )
        related = []
        if seed:
            related = [
                str(item.get("target") or item.get("canonical_name") or "")
                for field in (
                    "application_contexts",
                    "component_of",
                    "components",
                    "mutual_semantic_neighbors",
                )
                for item in (seed.get(field) or [])
                if str(item.get("target") or item.get("canonical_name") or "")
            ]
        related = list(dict.fromkeys(related))[:4]
        if seed and related:
            canonical = str(seed.get("term") or seed.get("canonical_name") or "")
            lane_id = "stepback_" + seed_id[:12]
            lanes.append(
                QueryLane(
                    lane_id=lane_id,
                    role="core",
                    query=(
                        f"What broader principles connect {canonical} with "
                        + ", ".join(related)
                        + "?"
                    ),
                    dense_text=(
                        f"Broader corpus-grounded principles for {canonical}: "
                        + ", ".join(related)
                    ),
                    lexical_terms=tuple(
                        dict.fromkeys(
                            [
                                *lexical_terms(canonical),
                                *lexical_terms(" ".join(related)),
                            ]
                        )
                    ),
                    required=False,
                    phrase=canonical,
                    support_phrases=tuple([canonical, *related]),
                )
            )
            lane_lexicon_ids[lane_id] = [seed_id]
            step_back_lanes.append(lane_id)

    return lanes, {
        "translation_lane_ids": [
            lane.lane_id for lane in lanes if lane.lane_id.startswith("translation_")
        ],
        "step_back_lane_ids": step_back_lanes,
        "lane_lexicon_ids": lane_lexicon_ids,
        "introduced_lexicon_ids": used_ids,
        "skipped_non_executable_lexicon_ids": skipped_non_executable_ids,
        "required": False,
    }


def grounded_translation_lane_targets(
    resolution: dict[str, Any],
    expansion: dict[str, Any],
    *,
    required_lane_ids: list[str],
) -> dict[str, list[str]]:
    """Map grounded translation lanes back to their originating obligations.

    Vocabulary search uses prefixed, non-required probe lanes so it cannot
    silently create answer obligations. When a corpus lexicon entry was found
    by one of those probes, a passage that later grounds that exact lexicon
    entry can legitimately satisfy the corresponding required execution lane.
    Step-back lanes stay exploratory because their broader query is not an
    identity-preserving translation of the original obligation.
    """

    required = {
        str(lane_id)
        for lane_id in required_lane_ids
        if str(lane_id).strip()
    }
    if not required:
        return {}
    matches = {
        str(row.get("lexicon_id") or ""): row
        for row in (resolution.get("matches") or [])
        if isinstance(row, dict) and str(row.get("lexicon_id") or "")
    }

    def execution_lane_id(value: object) -> str | None:
        candidate = str(value or "").strip()
        if candidate in required:
            return candidate
        for prefix in ("probe_", "concept_"):
            if candidate.startswith(prefix) and candidate[len(prefix) :] in required:
                return candidate[len(prefix) :]
        return None

    output: dict[str, list[str]] = {}
    for lane_id, lexicon_ids in (
        (expansion.get("lane_lexicon_ids") or {}).items()
    ):
        source_lane_id = str(lane_id or "")
        if not (
            source_lane_id.startswith("translation_")
            or source_lane_id.startswith("planner_translation_")
        ):
            continue
        targets: list[str] = []
        for lexicon_id in lexicon_ids or []:
            match = matches.get(str(lexicon_id))
            if not match:
                continue
            for matched_lane_id in match.get("matched_lane_ids") or []:
                target = execution_lane_id(matched_lane_id)
                if target and target not in targets:
                    targets.append(target)
        if targets:
            output[source_lane_id] = targets
    return output


async def hierarchy_bound_vocabulary_matches(
    *,
    qdrant_client: Any,
    summary_tree_routes: dict[str, list[Any]],
    lane_vectors: dict[str, list[float] | None],
    lane_queries: dict[str, str],
    existing_matches: list[dict[str, Any]] | None = None,
    disabled_lexicon_ids: list[str] | None = None,
    max_matches: int = 4,
    max_per_corpus: int | None = None,
    max_per_lane_corpus: int = 1,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve expert concepts bound to the hierarchy neighborhoods already hit.

    Summary-tree points carry source-proven ``lexicon_ids``.  This pass scores
    only those bound cards with the existing query vector, then applies the
    normal evidence prior.  It is the deterministic hierarchy-as-translator
    bridge: no generated synonym, domain regex, or second corpus-wide ANN scan.
    """

    from services.storage.qdrant_writer import (
        retrieve_lexicon_entries,
        search_lexicon_entries,
    )

    existing_ids = {
        str(row.get("lexicon_id") or "") for row in (existing_matches or [])
    }
    disabled = {str(value) for value in (disabled_lexicon_ids or []) if str(value)}
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    lane_order = [str(lane_id) for lane_id in (summary_tree_routes or {})]
    corpus_order: list[str] = []
    for lane_id, routes in (summary_tree_routes or {}).items():
        vector = lane_vectors.get(str(lane_id))
        if vector is None:
            continue
        for route in routes or []:
            corpus_id = str(getattr(route, "corpus_id", "") or "")
            doc_id = str(getattr(route, "doc_id", "") or "")
            lexicon_ids = [
                str(value)
                for value in (getattr(route, "lexicon_ids", ()) or ())
                if str(value)
            ]
            if not corpus_id or not doc_id or not lexicon_ids:
                continue
            if corpus_id not in corpus_order:
                corpus_order.append(corpus_id)
            document_title = str(getattr(route, "document_title", "") or "")
            group = grouped.setdefault(
                (str(lane_id), corpus_id),
                {
                    "lane_id": str(lane_id),
                    "corpus_id": corpus_id,
                    "lexicon_ids": [],
                    "lexicon_ids_by_document": {},
                    "documents_by_lexicon_id": {},
                    "document_titles_by_lexicon_id": {},
                },
            )
            group["lexicon_ids_by_document"][doc_id] = list(
                dict.fromkeys(
                    [
                        *group["lexicon_ids_by_document"].get(doc_id, []),
                        *lexicon_ids,
                    ]
                )
            )
            for lexicon_id in lexicon_ids:
                documents = group["documents_by_lexicon_id"].setdefault(lexicon_id, [])
                if doc_id not in documents:
                    documents.append(doc_id)
                titles = group["document_titles_by_lexicon_id"].setdefault(
                    lexicon_id, []
                )
                if document_title and document_title not in titles:
                    titles.append(document_title)

    for group in grouped.values():
        by_document = group["lexicon_ids_by_document"]
        document_ids = list(by_document)
        seen: set[str] = set()
        fair_ids: list[str] = []
        max_depth = max((len(by_document[doc_id]) for doc_id in document_ids), default=0)
        for offset in range(max_depth):
            for doc_id in document_ids:
                values = by_document[doc_id]
                if offset >= len(values):
                    continue
                lexicon_id = str(values[offset] or "")
                if not lexicon_id or lexicon_id in seen:
                    continue
                fair_ids.append(lexicon_id)
                seen.add(lexicon_id)
                if len(fair_ids) >= 512:
                    break
            if len(fair_ids) >= 512:
                break
        group["lexicon_ids"] = fair_ids

    async def search_group(group: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        lane_id = str(group["lane_id"])
        vector = lane_vectors.get(lane_id)
        dense_outcome, priority_outcome = await asyncio.gather(
            search_lexicon_entries(
                qdrant_client,
                str(group["corpus_id"]),
                query_vec=vector,
                allowed_lexicon_ids=list(group["lexicon_ids"]),
                top_k=min(128, max(16, len(group["lexicon_ids"]))),
                score_threshold=0.2,
                with_vectors=False,
            ),
            # The first IDs are round-robin, high-support concepts from every
            # routed document. Fetching this bounded payload set prevents ANN
            # top-k from erasing a later document's canonical title concept.
            retrieve_lexicon_entries(
                qdrant_client,
                str(group["corpus_id"]),
                list(group["lexicon_ids"])[:96],
                with_vectors=False,
            ),
            return_exceptions=True,
        )
        if isinstance(dense_outcome, BaseException) and isinstance(
            priority_outcome, BaseException
        ):
            return group, dense_outcome
        rows = [] if isinstance(dense_outcome, BaseException) else list(dense_outcome)
        seen_ids = {str(row.get("lexicon_id") or "") for row in rows}
        if not isinstance(priority_outcome, BaseException):
            for lexicon_id, stored in priority_outcome.items():
                if lexicon_id in seen_ids:
                    continue
                payload = dict(stored.get("payload") or {})
                payload.update(
                    {
                        "score": 0.0,
                        "match_type": "hierarchy_bound_priority",
                        "_hierarchy_priority": True,
                    }
                )
                rows.append(payload)
        return group, rows

    searched = await asyncio.gather(
        *(search_group(group) for group in grouped.values())
    )
    candidates_by_group: dict[tuple[str, str], list[dict[str, Any]]] = {}
    failures: list[dict[str, str]] = []
    for group, outcome in searched:
        lane_id = str(group["lane_id"])
        corpus_id = str(group["corpus_id"])
        if isinstance(outcome, BaseException):
            failures.append(
                {
                    "lane_id": lane_id,
                    "corpus_id": corpus_id,
                    "error": f"{type(outcome).__name__}: {outcome}"[:240],
                }
            )
            continue
        lane_query = str(lane_queries.get(lane_id) or "")
        candidates: list[dict[str, Any]] = []
        for raw in outcome or []:
            row = dict(raw)
            lexicon_id = str(row.get("lexicon_id") or "")
            if (
                not lexicon_id
                or lexicon_id in existing_ids
                or lexicon_id in disabled
                or row.get("retrieval_eligible", True) is False
            ):
                continue
            overlap_count, overlap_ratio = _match_overlap(lane_query, row)
            support_count = max(0, int(row.get("support_count") or 0))
            title_surfaces = [
                str(row.get("term") or row.get("canonical_name") or ""),
                *(str(value) for value in (row.get("aliases") or []) if str(value)),
                *(
                    str(value)
                    for value in (row.get("abbreviations") or [])
                    if str(value)
                ),
            ]
            bound_titles = list(
                group["document_titles_by_lexicon_id"].get(lexicon_id) or []
            )
            normalized_titles = [normalize_identity(value) for value in bound_titles]
            title_identity_match = any(
                len(surface_key.split()) >= 2 and surface_key in title_key
                for surface in title_surfaces
                if (surface_key := normalize_identity(surface))
                for title_key in normalized_titles
                if title_key
            )
            if row.get("_hierarchy_priority") and title_identity_match:
                row["score"] = max(0.58, float(row.get("score") or 0.0))
            raw_score = float(row.get("score") or 0.0)
            evidence_score = round(
                min(
                    1.0,
                    _evidence_adjusted_score(row)
                    + 0.08
                    + min(0.04, overlap_ratio * 0.04),
                ),
                6,
            )
            if evidence_score < 0.62:
                continue
            if overlap_count <= 0 and not title_identity_match and not (
                evidence_score >= 0.76 and raw_score >= 0.48
            ):
                continue
            bound_documents = list(
                group["documents_by_lexicon_id"].get(lexicon_id) or []
            )
            if not bound_documents:
                continue
            source_support = [
                dict(value)
                for value in (row.get("source_document_support") or [])
                if isinstance(value, dict)
            ]
            support_by_doc = {
                str(value.get("doc_id") or ""): value for value in source_support
            }
            bound_support = [
                support_by_doc.get(doc_id)
                or {"doc_id": doc_id, "support_count": max(1, support_count)}
                for doc_id in bound_documents
            ]
            row.update(
                {
                    "corpus_id": corpus_id,
                    "match_type": "hierarchy_bound_gloss",
                    "evidence_adjusted_score": evidence_score,
                    "overlap_count": overlap_count,
                    "overlap_ratio": round(overlap_ratio, 4),
                    "overlap_lane_id": lane_id,
                    "overlap_query": lane_query,
                    "applicability": "hierarchy_bound",
                    "required": False,
                    "selection_reason": "preindexed_hierarchy_binding",
                    "hierarchy_document_ids": bound_documents,
                    "hierarchy_document_titles": bound_titles[:8],
                    "title_identity_match": title_identity_match,
                    "source_document_ids": list(
                        dict.fromkeys(
                            [
                                *bound_documents,
                                *(
                                    str(value)
                                    for value in (row.get("source_document_ids") or [])
                                    if str(value)
                                ),
                            ]
                        )
                    )[:64],
                    "source_document_support": [
                        *bound_support,
                        *[
                            value
                            for value in source_support
                            if str(value.get("doc_id") or "")
                            not in set(bound_documents)
                        ],
                    ][:32],
                }
            )
            candidates.append(row)
        candidates_by_group[(lane_id, corpus_id)] = sorted(
            candidates,
            key=lambda row: (
                -int(bool(row.get("title_identity_match"))),
                -float(row.get("evidence_adjusted_score") or 0.0),
                -int(row.get("support_count") or 0),
                -int(row.get("overlap_count") or 0),
                -float(row.get("score") or 0.0),
                str(row.get("canonical_key") or row.get("term") or ""),
            ),
        )

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    selected_by_corpus: dict[str, int] = {}
    selected_groups: list[dict[str, str]] = []
    limit = max(1, int(max_matches))
    per_group_limit = max(1, int(max_per_lane_corpus))

    # Required lanes express independent information needs. Select through the
    # lane/corpus matrix before considering a second concept from any group so
    # one high-support document cannot monopolize a compositional query. The
    # caller supplies only required lanes; this remains a bounded bridge rather
    # than a general concept fan-out.
    for offset in range(per_group_limit):
        for lane_id in lane_order:
            for corpus_id in corpus_order:
                rows = candidates_by_group.get((lane_id, corpus_id)) or []
                available = [
                    row
                    for row in rows
                    if str(row.get("lexicon_id") or "") not in selected_ids
                ]
                if not available:
                    continue
                if (
                    max_per_corpus is not None
                    and selected_by_corpus.get(corpus_id, 0)
                    >= max(1, int(max_per_corpus))
                ):
                    continue
                row = available[0]
                lexicon_id = str(row.get("lexicon_id") or "")
                if not lexicon_id:
                    continue
                selected.append(_safe_match(row))
                selected_ids.add(lexicon_id)
                selected_by_corpus[corpus_id] = (
                    selected_by_corpus.get(corpus_id, 0) + 1
                )
                selected_groups.append(
                    {"lane_id": lane_id, "corpus_id": corpus_id}
                )
                if len(selected) >= limit:
                    break
            if len(selected) >= limit:
                break
        if len(selected) >= limit:
            break

    return selected, {
        "status": "resolved" if selected else "no_novel_matches",
        "group_count": len(grouped),
        "candidate_count": sum(len(rows) for rows in candidates_by_group.values()),
        "match_count": len(selected),
        "matched_lexicon_ids": [str(row.get("lexicon_id") or "") for row in selected],
        "selected_groups": selected_groups,
        "failures": failures,
    }


def grounded_document_route_hints(
    resolution: dict[str, Any],
    expansion: dict[str, Any],
    *,
    max_docs_per_lane: int = 4,
) -> dict[str, list[dict[str, Any]]]:
    """Map optional vocabulary lanes to their strongest source documents.

    These are provenance-backed routing hints, not answer evidence. Leaf and
    parent retrieval must still return cited passages before synthesis.
    """

    matches = {
        str(row.get("lexicon_id") or ""): row
        for row in resolution.get("matches") or []
        if row.get("lexicon_id")
    }
    profiles = {
        (str(row.get("corpus_id") or ""), str(row.get("doc_id") or "")): row
        for row in resolution.get("document_profiles") or []
        if row.get("corpus_id") and row.get("doc_id")
    }
    output: dict[str, list[dict[str, Any]]] = {}
    for lane_id, lexicon_ids in (expansion.get("lane_lexicon_ids") or {}).items():
        candidates: dict[tuple[str, str], dict[str, Any]] = {}
        for lexicon_id in lexicon_ids or []:
            match = matches.get(str(lexicon_id))
            if not match:
                continue
            corpus_id = str(match.get("corpus_id") or "")
            support_rows = list(match.get("source_document_support") or [])
            if not support_rows:
                support_rows = [
                    {"doc_id": doc_id, "support_count": 1}
                    for doc_id in (match.get("source_document_ids") or [])
                ]
            strongest = max(
                (int(row.get("support_count") or 0) for row in support_rows),
                default=1,
            )
            for support in support_rows:
                doc_id = str(support.get("doc_id") or "")
                profile = profiles.get((corpus_id, doc_id))
                if not profile:
                    continue
                source_strength = int(support.get("support_count") or 0) / max(
                    1, strongest
                )
                match_score = min(
                    1.0,
                    max(
                        0.0,
                        float(
                            match.get("evidence_adjusted_score")
                            or match.get("score")
                            or 0.0
                        ),
                    ),
                )
                score = 0.52 + 0.30 * match_score + 0.12 * source_strength
                if match.get("applicability") == "direct":
                    score += 0.04
                key = (corpus_id, doc_id)
                candidate = {
                    "lane_id": str(lane_id),
                    "corpus_id": corpus_id,
                    "doc_id": doc_id,
                    "score": round(min(0.98, score), 6),
                    "title": str(profile.get("title") or ""),
                    "summary": str(profile.get("summary") or ""),
                    "concepts": list(profile.get("concepts") or []),
                    "section_ids": list(profile.get("section_ids") or []),
                    "lexicon_id": str(lexicon_id),
                    "source_support": int(support.get("support_count") or 0),
                    "route_source": "corpus_lexicon_provenance",
                }
                existing = candidates.get(key)
                if existing is None or float(candidate["score"]) > float(
                    existing["score"]
                ):
                    candidates[key] = candidate
        if candidates:
            output[str(lane_id)] = sorted(
                candidates.values(),
                key=lambda item: (
                    -float(item["score"]),
                    -int(item["source_support"]),
                    str(item["corpus_id"]),
                    str(item["doc_id"]),
                ),
            )[: max(1, int(max_docs_per_lane))]
    return output


corpus_vocabulary_resolver = CorpusVocabularyResolver()
