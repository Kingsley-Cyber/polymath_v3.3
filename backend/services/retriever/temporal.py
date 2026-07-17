"""Query-side temporal routing over already-captured metadata.

The detector deliberately mirrors the qualified deterministic families in
``runpod_flash_extractor/runtime.py``.  It does not parse dates, normalize a
calendar, or invent temporal grammar.  Regex captures are supplemented by the
same pinned spaCy DATE/TIME/EVENT entity families used by the extraction
runtime.

All selection helpers are pure and fail open.  Storage hydration lives in
``hydrate.py`` so this module never writes to Mongo, Qdrant, or Neo4j.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Sequence

from models.schemas import RetrievalTier, SourceChunk

TEMPORAL_ROUTING_VERSION = "temporal_query_routing.v1"
QUALIFIED_TEMPORAL_PATTERN_VERSION = "runpod_flash_extractor.runtime.v1"
SPACY_MODEL = "en_core_web_sm"
SPACY_MODEL_VERSION = "3.8.0"
TIME_CUE_WINDOW_CHARS = 40

_MONTH_TOKEN = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
)
_YEAR_TOKEN = r"(?:19|20)\d{2}"
_SEASON_TOKEN = r"(?:spring|summer|autumn|fall|winter)"
_PERIOD_TOKEN = rf"(?:{_SEASON_TOKEN}|seasons?|quarters?|periods?)"
_MODIFIER_TOKEN = r"(?:[A-Za-z][A-Za-z'’\-]{0,31})"

# Keep ordering identical to the locked extraction runtime: specific patterns
# own overlapping spans before the bare-year family sees them.
QUALIFIED_TEMPORAL_REGEX_FAMILY: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("iso_date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    (
        "year_range",
        re.compile(
            rf"\b{_YEAR_TOKEN}\s*(?:[-–—]|to|through|until)\s*{_YEAR_TOKEN}\b",
            re.I,
        ),
    ),
    (
        "year_event_period",
        re.compile(
            rf"\b{_YEAR_TOKEN}(?:\s+{_MODIFIER_TOKEN}){{0,3}}\s+{_PERIOD_TOKEN}\b",
            re.I,
        ),
    ),
    ("season_year", re.compile(rf"\b{_SEASON_TOKEN}\s+{_YEAR_TOKEN}\b", re.I)),
    (
        "qualified_year",
        re.compile(rf"\b(?:early|mid|late)(?:\s+|[-–—]){_YEAR_TOKEN}\b", re.I),
    ),
    ("quarter", re.compile(rf"\bQ[1-4]\s+{_YEAR_TOKEN}\b")),
    ("month_year", re.compile(rf"\b(?:{_MONTH_TOKEN})\.?\s+{_YEAR_TOKEN}\b")),
    ("version", re.compile(r"\bv?\d+\.\d+(?:\.\d+)*\b")),
    ("year", re.compile(rf"\b{_YEAR_TOKEN}\b")),
)
_VERSION_CUE_RE = re.compile(
    r"\b(?:release|releases|released|version|versions)\b", re.I
)
_TIME_ROLE_CUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "publication_time",
        re.compile(
            r"\b(?:published|publishes|publish|publication|issued|released)\b",
            re.I,
        ),
    ),
    (
        "revision_time",
        re.compile(r"\b(?:updated|revised|revision|amended|modified)\b", re.I),
    ),
    (
        "reference_time",
        re.compile(r"\b(?:as of|according to|data from)\b", re.I),
    ),
    (
        "event_time",
        re.compile(
            r"\b(?:occurred|happened|took place|launched|founded|began)\b", re.I
        ),
    ),
    (
        "effective_time",
        re.compile(r"\b(?:effective|takes effect|in effect|comes into force)\b", re.I),
    ),
    (
        "forecast_time",
        re.compile(
            r"\b(?:will launch|will release|will ship|expected|forecasts?|projected|predicted|anticipated)\b",
            re.I,
        ),
    ),
    (
        "deadline_time",
        re.compile(r"\b(?:deadline|due by|due on|due date|no later than)\b", re.I),
    ),
)
_ROLE_TO_CLASS = {
    "publication_time": "versioned",
    "revision_time": "versioned",
    "effective_time": "versioned",
    "event_time": "event",
}


@dataclass(frozen=True)
class QueryTimeExpression:
    text: str
    family: str
    role_candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemporalIntent:
    expressions: tuple[QueryTimeExpression, ...] = ()
    roles: tuple[str, ...] = ()
    temporal_classes: tuple[str, ...] = ()
    detector_sources: tuple[str, ...] = ()
    detector_error: str | None = None

    @property
    def active(self) -> bool:
        return bool(self.expressions)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "active": self.active,
            "version": TEMPORAL_ROUTING_VERSION,
            "pattern_version": QUALIFIED_TEMPORAL_PATTERN_VERSION,
            "detector_sources": list(self.detector_sources),
            "expressions": [
                {
                    "text": item.text,
                    "family": item.family,
                    "role_candidates": list(item.role_candidates),
                }
                for item in self.expressions
            ],
            "roles": list(self.roles),
            "temporal_classes": list(self.temporal_classes),
            "detector_error": self.detector_error,
        }


def temporal_routing_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "TEMPORAL_QUERY_ROUTING_ENABLED", False))


def _time_context(text: str, start: int, end: int) -> str:
    return text[max(0, start - TIME_CUE_WINDOW_CHARS) : end + TIME_CUE_WINDOW_CHARS]


def _roles_for_span(text: str, start: int, end: int) -> tuple[str, ...]:
    context = _time_context(text, start, end)
    return tuple(role for role, pattern in _TIME_ROLE_CUES if pattern.search(context))


@lru_cache(maxsize=1)
def _load_temporal_nlp():
    import spacy

    nlp = spacy.load(
        SPACY_MODEL,
        disable=["parser", "tagger", "lemmatizer", "attribute_ruler"],
    )
    version = str(getattr(nlp, "meta", {}).get("version") or "")
    if version and version != SPACY_MODEL_VERSION:
        raise RuntimeError(
            f"{SPACY_MODEL} version {version} differs from pinned {SPACY_MODEL_VERSION}"
        )
    return nlp


@lru_cache(maxsize=512)
def detect_temporal_intent(query: str) -> TemporalIntent:
    text = str(query or "")
    captured: list[QueryTimeExpression] = []
    taken: list[tuple[int, int]] = []
    sources: list[str] = []

    def add(start: int, end: int, family: str, source: str) -> None:
        if start >= end or any(left < end and start < right for left, right in taken):
            return
        taken.append((start, end))
        captured.append(
            QueryTimeExpression(
                text=text[start:end],
                family=family,
                role_candidates=_roles_for_span(text, start, end),
            )
        )
        if source not in sources:
            sources.append(source)

    for family, pattern in QUALIFIED_TEMPORAL_REGEX_FAMILY:
        for match in pattern.finditer(text):
            if family == "version" and not _VERSION_CUE_RE.search(
                _time_context(text, match.start(), match.end())
            ):
                continue
            add(match.start(), match.end(), family, "regex")

    detector_error: str | None = None
    try:
        doc = _load_temporal_nlp()(text)
        for entity in getattr(doc, "ents", ()):
            if str(getattr(entity, "label_", "")) not in {"DATE", "TIME", "EVENT"}:
                continue
            add(
                int(entity.start_char),
                int(entity.end_char),
                f"spacy_{str(entity.label_).lower()}",
                "spacy",
            )
    except Exception as exc:  # fail open when the optional local model is unavailable
        detector_error = f"{type(exc).__name__}: {exc}"[:240]

    captured.sort(key=lambda item: text.find(item.text))
    roles = tuple(
        dict.fromkeys(
            role for item in captured for role in item.role_candidates if role
        )
    )
    classes = tuple(
        dict.fromkeys(_ROLE_TO_CLASS[role] for role in roles if role in _ROLE_TO_CLASS)
    )
    return TemporalIntent(
        expressions=tuple(captured),
        roles=roles,
        temporal_classes=classes,
        detector_sources=tuple(sources),
        detector_error=detector_error,
    )


def _clean_time_expressions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        row: dict[str, Any] = {"text": text}
        role = str(raw.get("role") or "").strip()
        if role:
            row["role"] = role
        for key in ("char_start", "char_end"):
            if isinstance(raw.get(key), int):
                row[key] = raw[key]
        output.append(row)
    return output


def metadata_with_temporal_carrier(
    metadata: dict[str, Any] | None,
    source: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach a normalized carrier without promoting fields to SourceChunk."""

    output = dict(metadata or {})
    source = source or {}
    temporal_class = str(source.get("temporal_class") or "").strip().lower()
    expressions = _clean_time_expressions(source.get("time_expressions"))
    if not temporal_class and not expressions:
        return output
    output["temporal"] = {
        "temporal_class": temporal_class or "unknown",
        "time_expressions": expressions,
    }
    return output


def candidate_temporal_carrier(chunk: SourceChunk) -> dict[str, Any]:
    metadata = chunk.metadata or {}
    carrier = metadata.get("temporal")
    if isinstance(carrier, dict):
        return {
            "temporal_class": str(carrier.get("temporal_class") or "unknown")
            .strip()
            .lower(),
            "time_expressions": _clean_time_expressions(
                carrier.get("time_expressions")
            ),
        }
    return {
        "temporal_class": str(metadata.get("temporal_class") or "unknown")
        .strip()
        .lower(),
        "time_expressions": _clean_time_expressions(metadata.get("time_expressions")),
    }


def _normalized_surface(value: str) -> str:
    surface = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    return surface.removeprefix("the ")


def _surface_matches(query_surface: str, candidate_surface: str) -> bool:
    if not query_surface or not candidate_surface:
        return False
    if query_surface == candidate_surface:
        return True
    # The locked extractor gives the most-specific overlapping regex span
    # ownership (e.g. ``1988 through 2001``). Stored source captures may be
    # the component ``2001`` or a wider ``October 1988`` spaCy span. Boundary
    # containment compares those verbatim surfaces without calendar parsing.
    query_padded = f" {query_surface} "
    candidate_padded = f" {candidate_surface} "
    return candidate_padded in query_padded or query_padded in candidate_padded


def temporal_match_details(
    chunk: SourceChunk,
    intent: TemporalIntent,
) -> dict[str, Any]:
    carrier = candidate_temporal_carrier(chunk)
    query_surfaces = {
        surface
        for surface in (_normalized_surface(item.text) for item in intent.expressions)
        if surface
    }
    candidate_rows = carrier["time_expressions"]
    candidate_surfaces = {
        surface
        for surface in (
            _normalized_surface(str(row.get("text") or "")) for row in candidate_rows
        )
        if surface
    }
    exact = sorted(
        {
            candidate_surface
            for query_surface in query_surfaces
            for candidate_surface in candidate_surfaces
            if _surface_matches(query_surface, candidate_surface)
        }
    )
    candidate_roles = {
        str(row.get("role") or "").strip()
        for row in candidate_rows
        if str(row.get("role") or "").strip()
    }
    roles = sorted(set(intent.roles) & candidate_roles)
    temporal_class = str(carrier.get("temporal_class") or "unknown")
    class_match = temporal_class in set(intent.temporal_classes)
    return {
        # temporal_class is a tie-break/refinement, never enough by itself to
        # admit a different dated event when the requested surface is absent.
        "matched": bool(exact),
        "exact_surfaces": exact,
        "role_matches": roles,
        "class_match": class_match,
        "temporal_class": temporal_class,
        "candidate_expression_count": len(candidate_rows),
    }


def candidate_key(chunk: SourceChunk) -> str:
    content_id = str(chunk.chunk_id or chunk.parent_id or "")
    return f"{str(chunk.corpus_id or '')}|{content_id}" if content_id else ""


def _is_relevant_candidate(
    candidate: SourceChunk,
    *,
    rank: int,
    ranked: Sequence[SourceChunk],
    max_candidates: int,
) -> bool:
    if rank < max(3, int(max_candidates)):
        return True
    top_score = max((float(item.score or 0.0) for item in ranked), default=0.0)
    score = float(candidate.score or 0.0)
    if 0.0 < top_score <= 1.0 and 0.0 <= score <= 1.0:
        return score >= max(0.05, top_score * 0.25)
    return rank < max(12, int(max_candidates) * 3)


def reserve_temporal_candidates(
    selected: Sequence[SourceChunk],
    ranked: Sequence[SourceChunk],
    *,
    intent: TemporalIntent,
    max_candidates: int,
    tier: RetrievalTier,
    protected_keys: Iterable[str] = (),
) -> tuple[list[SourceChunk], dict[str, Any]]:
    """Preserve one relevant exact/role/class temporal match through a cut.

    Graph provenance is only a tie-break among candidates that already match
    the temporal intent and pass the shared relevance floor.  No score is
    changed, and the helper never hard-filters the candidate set.
    """

    limit = max(1, int(max_candidates))
    output = list(selected)[:limit]
    diagnostics: dict[str, Any] = {
        "active": intent.active,
        "version": TEMPORAL_ROUTING_VERSION,
        "matched_candidates": 0,
        "relevant_matches": 0,
        "reserved": False,
        "graph_preferred": False,
    }
    if not intent.active or not ranked or not output:
        diagnostics["reason"] = "inactive_or_empty"
        return output, diagnostics

    from services.retriever.ranking_policy import is_graph_supported

    ranked_list = list(ranked)
    scored: list[tuple[tuple[Any, ...], SourceChunk, dict[str, Any]]] = []
    for rank, candidate in enumerate(ranked_list):
        details = temporal_match_details(candidate, intent)
        if not details["matched"]:
            continue
        diagnostics["matched_candidates"] += 1
        if not _is_relevant_candidate(
            candidate,
            rank=rank,
            ranked=ranked_list,
            max_candidates=limit,
        ):
            continue
        diagnostics["relevant_matches"] += 1
        graph_supported = bool(is_graph_supported(candidate))
        graph_tie_break = int(
            tier == RetrievalTier.qdrant_mongo_graph and graph_supported
        )
        score_key = (
            len(details["exact_surfaces"]),
            len(details["role_matches"]),
            int(details["class_match"]),
            graph_tie_break,
            float(candidate.score or 0.0),
            -rank,
        )
        scored.append((score_key, candidate, details))

    if not scored:
        diagnostics["reason"] = "no_relevant_temporal_match"
        return output, diagnostics
    scored.sort(key=lambda row: row[0], reverse=True)
    _score_key, best, details = scored[0]
    best_key = candidate_key(best)
    diagnostics["best_candidate"] = best_key
    diagnostics["best_match"] = details
    diagnostics["graph_preferred"] = bool(
        tier == RetrievalTier.qdrant_mongo_graph and is_graph_supported(best)
    )
    if any(candidate_key(item) == best_key for item in output):
        diagnostics["reason"] = "already_selected"
        return output, diagnostics

    protected = set(protected_keys)
    replace_index = next(
        (
            index
            for index in range(len(output) - 1, -1, -1)
            if candidate_key(output[index]) not in protected
        ),
        None,
    )
    if replace_index is None:
        diagnostics["reason"] = "all_selected_candidates_protected"
        return output, diagnostics
    removed = output[replace_index]
    output[replace_index] = best
    ranked_positions = {
        candidate_key(item): index for index, item in enumerate(ranked_list)
    }
    output.sort(key=lambda item: ranked_positions.get(candidate_key(item), 10**9))
    diagnostics.update(
        {
            "reserved": True,
            "reason": "replaced_unprotected_tail",
            "replaced_candidate": candidate_key(removed),
        }
    )
    return output, diagnostics


def temporal_protected_keys(reservation_diagnostics: dict[str, Any]) -> set[str]:
    protected: set[str] = set()
    for field in (
        "protected_lane_reservation_refs",
        "lane_reservation_refs",
        "corpus_reservation_refs",
        "routed_document_reservation_refs",
    ):
        values = reservation_diagnostics.get(field) or {}
        if isinstance(values, dict):
            protected.update(str(value) for value in values.values() if str(value))
    return protected
