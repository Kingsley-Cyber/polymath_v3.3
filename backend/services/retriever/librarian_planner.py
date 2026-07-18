"""Deterministic Librarian planning, grounding, and execution compilation.

The rule planner stays provider-free. L5 delegates its single bounded
escalation to ``librarian_decomposer`` only after every rule falls through.
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Iterable

from models.librarian_query_plan import (
    LLM_DECOMPOSER_PROMPT_HASH,
    PLANNER_VERSION,
    LibrarianPlanCacheV1,
    LibrarianRefusalSignalsV1,
    LibrarianShortlistItemV1,
    LibrarianSubqueryV1,
    QueryPlanV1,
    canonical_json_bytes,
    normalize_planner_query,
    plan_cache_key_for,
    plan_hash_for,
    replay_query_plan_v1,
)
from services.embedder import embed_queries
from services.retriever.evidence_allocation import relationship_allocation_eligible
from services.retriever.evidence_plan import build_evidence_plan
from services.retriever.four_lane_router import (
    DocumentProfile,
    bm25_document_scores,
    four_lane_document_router,
)
from services.retriever.query_plan import build_query_plan_v2
from services.retriever.query_plan import (
    QueryLane,
    QueryPlanV2,
    lexical_terms,
)
from services.retriever.query_semantics import (
    BASE_STOP_WORDS,
    query_tokens,
    split_query_sides,
)
from services.retriever.temporal import detect_temporal_intent
from services.retriever.tier0_router import tier0_document_router
from services.storage.record_status import with_active_records


SHORTLIST_LIMIT = 8
DEFAULT_SEAT_BUDGET = 8
LLM_ESCALATION_STATUS = "bounded_utility_route_default_off"
RULE_REGISTRY_ORDER = (
    "relationship_comparison",
    "temporal",
    "enumerative_trace",
    "entity_bridge",
    "simple",
)

_TRACE_RE = re.compile(
    r"\btrace\s+how\s+(.+?)\s+(?:becomes?|turns?\s+into|leads?\s+to)\s+"
    r"(.+?)(?:[?.!]|$)",
    re.IGNORECASE,
)
_RELATION_SIDE_PATTERNS = (
    re.compile(
        r"\bcompare\s+(.+?)\s+(?:with|to|against)\s+(.+?)(?:[?.!]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bhow\s+(?:does|do)\s+(.+?)\s+"
        r"(?:relate|connect|compare)\s+(?:to|with)\s+(.+?)(?:[?.!]|$)",
        re.IGNORECASE,
    ),
)
_ENUMERATIVE_RE = re.compile(
    r"\b(?:list|enumerate|steps?|stages?|sequence|trace\s+how)\b",
    re.IGNORECASE,
)
_ENTITY_BRIDGE_PATTERNS = (
    re.compile(r"\b(.+?)\s+alongside\s+(.+)$", re.IGNORECASE),
    re.compile(
        r"\b(?:relationship|connection)\s+between\s+(.+?)\s+and\s+(.+)$",
        re.IGNORECASE,
    ),
    re.compile(r"\b(.+?)\s+(?:versus|vs)\s+(.+)$", re.IGNORECASE),
)
_NAMED_SOURCE_PATTERNS = (
    re.compile(
        r"\baccording\s+to\s+(.+?)(?:,|\bwhat\b|\bhow\b|\bwhich\b|[?.!]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:book|document|source|paper)\s+(?:called|titled|named)\s+"
        r"[\"“]?(.+?)[\"”]?"
        r"(?:,|\bwhat\b|\bhow\b|\bwhich\b|\bwho\b|[?.!]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwhat\s+(?:does|do)\s+(.+?)\s+"
        r"(?:say|teach|present|recommend|describe|explain)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwhat\s+(?:guidance|advice|recommendations?)\s+(?:does|do)\s+"
        r"(.+?)\s+(?:give|offer|provide|present)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwhat\s+.+?\s+did\s+(.+?)\s+" r"(?:give|say|teach|recommend|describe)\b",
        re.IGNORECASE,
    ),
)
_NAMED_SOURCE_GENERIC_TOKENS = frozenset(
    {
        *BASE_STOP_WORDS,
        "book",
        "books",
        "doc",
        "document",
        "documents",
        "paper",
        "source",
        "sources",
    }
)
_ENTITY_BRIDGE_SIDE_STOP_WORDS = frozenset(
    {
        *BASE_STOP_WORDS,
        "compare",
        "describe",
        "discuss",
        "explain",
        "summarize",
    }
)


@dataclass(frozen=True)
class LibrarianPlanBuildResult:
    plan: QueryPlanV1
    diagnostics: dict[str, Any]


@dataclass(frozen=True)
class LibrarianExecutionPolicy:
    """Validated L3 instructions consumed by the existing planned executor."""

    active: bool
    plan_hash: str | None
    shape: str | None
    lane_seat_quotas: dict[str, int]
    lane_rerank_caps: dict[str, int]
    document_route_hints: dict[str, list[dict[str, Any]]]

    def diagnostics(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "plan_hash": self.plan_hash,
            "shape": self.shape,
            "lane_seat_quotas": dict(self.lane_seat_quotas),
            "lane_rerank_caps": dict(self.lane_rerank_caps),
            "document_route_hint_count": sum(
                len(values) for values in self.document_route_hints.values()
            ),
        }


def _execution_lane_id(index: int, role: str) -> str:
    normalized_role = re.sub(r"[^a-z0-9]+", "_", str(role).casefold()).strip("_")
    return f"librarian_{index + 1}_{normalized_role or 'main'}"


def apply_librarian_execution_plan(
    base_plan: QueryPlanV2,
    librarian_plan: QueryPlanV1,
) -> tuple[QueryPlanV2, LibrarianExecutionPolicy]:
    """Compile QueryPlanV1 into the one existing planned-fusion executor.

    Simple plans return the exact base object. Non-simple plans replace its
    candidate lanes with required librarian subqueries; they do not introduce
    a second allocator, embedding pass, or reranker.
    """

    if librarian_plan.shape == "simple":
        return (
            base_plan,
            LibrarianExecutionPolicy(
                active=False,
                plan_hash=librarian_plan.plan_hash,
                shape=librarian_plan.shape,
                lane_seat_quotas={},
                lane_rerank_caps={},
                document_route_hints={},
            ),
        )

    shortlist_by_doc_id: dict[str, list[LibrarianShortlistItemV1]] = {}
    for item in librarian_plan.shortlist:
        shortlist_by_doc_id.setdefault(item.doc_id, []).append(item)
    original_lane = next(
        (lane for lane in base_plan.lanes if lane.role == "original"),
        base_plan.lanes[0],
    )
    lanes: list[QueryLane] = [original_lane]
    lane_seat_quotas: dict[str, int] = {}
    lane_rerank_caps: dict[str, int] = {}
    document_route_hints: dict[str, list[dict[str, Any]]] = {}
    for index, subquery in enumerate(librarian_plan.subqueries):
        lane_id = _execution_lane_id(index, subquery.role)
        target_items = sorted(
            (
                item
                for doc_id in subquery.target_doc_ids
                for item in shortlist_by_doc_id.get(doc_id, [])
            ),
            key=lambda item: (item.corpus_id, item.doc_id),
        )
        target_doc_refs = tuple(
            sorted((item.corpus_id, item.doc_id) for item in target_items)
        )
        lanes.append(
            QueryLane(
                lane_id=lane_id,
                role="core",
                query=subquery.text,
                dense_text=subquery.text,
                lexical_terms=tuple(lexical_terms(subquery.text)[:16]),
                required=True,
                phrase=subquery.text,
                support_phrases=(subquery.text,),
                target_doc_refs=target_doc_refs,
                seat_quota=subquery.seat_quota,
                rerank_cap=subquery.rerank_cap,
            )
        )
        lane_seat_quotas[lane_id] = subquery.seat_quota
        lane_rerank_caps[lane_id] = subquery.rerank_cap
        if target_items:
            document_route_hints[lane_id] = [
                {
                    "corpus_id": item.corpus_id,
                    "doc_id": item.doc_id,
                    "score": item.score,
                    "title": item.title,
                    "summary": item.summary,
                    "concepts": [],
                    "section_ids": [],
                    "source": "librarian_query_plan_v1",
                }
                for item in target_items
            ]

    complexity = (
        "comparative"
        if librarian_plan.shape in {"relationship", "comparison"}
        else (
            "dependent_multi_hop"
            if librarian_plan.shape in {"entity_bridge", "enumerative_trace"}
            and len(lanes) > 1
            else "compositional"
        )
    )
    answer_shape = {
        "relationship": "relationship",
        "comparison": "comparison",
        "enumerative_trace": "enumeration",
        "temporal": "synthesis",
        "entity_bridge": "synthesis",
        "complex": "synthesis",
    }.get(librarian_plan.shape, base_plan.answer_shape)
    overlaid = replace(
        base_plan,
        complexity=complexity,
        answer_shape=answer_shape,
        concepts=tuple(subquery.text for subquery in librarian_plan.subqueries),
        lanes=tuple(lanes),
        probes=(),
        budget_obligation_count=max(
            1,
            len([probe for probe in base_plan.probes if probe.required]),
        ),
    )
    return (
        overlaid,
        LibrarianExecutionPolicy(
            active=True,
            plan_hash=librarian_plan.plan_hash,
            shape=librarian_plan.shape,
            lane_seat_quotas=lane_seat_quotas,
            lane_rerank_caps=lane_rerank_caps,
            document_route_hints=document_route_hints,
        ),
    )


class QueryPlanReplayCache:
    """Bounded cache that stores only validated canonical plan artifacts."""

    def __init__(self, max_entries: int = 512) -> None:
        self._max_entries = max(1, int(max_entries))
        self._items: OrderedDict[str, bytes] = OrderedDict()

    def get(self, key: str) -> QueryPlanV1 | None:
        payload = self._items.get(key)
        if payload is None:
            return None
        self._items.move_to_end(key)
        return replay_query_plan_v1(payload)

    def put(self, key: str, plan: QueryPlanV1) -> None:
        if key != plan.cache.key:
            raise ValueError("cache slot differs from QueryPlanV1 cache identity")
        self._items[key] = plan.canonical_bytes()
        self._items.move_to_end(key)
        while len(self._items) > self._max_entries:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()


def corpus_scope_identity(corpus_ids: Iterable[str] | None) -> str:
    scoped = tuple(
        sorted({str(value).strip() for value in corpus_ids or () if str(value).strip()})
    )
    if not scoped:
        return "no_corpus_selected"
    if len(scoped) == 1:
        return scoped[0]
    digest = hashlib.sha256(canonical_json_bytes(list(scoped))).hexdigest()
    return f"scope:sha256:{digest}"


def corpus_doc_set_version_from_rows(
    rows: Iterable[dict[str, Any]],
    *,
    corpus_ids: Iterable[str] | None,
) -> str:
    """Hash corpus-qualified document ids plus their durable content identity."""

    scoped = tuple(
        sorted({str(value).strip() for value in corpus_ids or () if str(value).strip()})
    )
    identities: list[dict[str, Any]] = []
    for row in rows:
        source_identity = (
            row.get("source_identity")
            if isinstance(row.get("source_identity"), dict)
            else {}
        )
        revision_identity = {
            "content_sha256": str(
                source_identity.get("content_sha256") or row.get("content_sha256") or ""
            ),
            "source_version_id": str(
                source_identity.get("source_version_id")
                or row.get("source_version_id")
                or ""
            ),
            "revision": str(
                source_identity.get("revision")
                or row.get("revision")
                or row.get("source_revision")
                or row.get("content_revision")
                or ""
            ),
        }
        if not any(revision_identity.values()):
            source_key = str(
                source_identity.get("source_key") or row.get("source_key") or ""
            )
            updated_at = str(row.get("updated_at") or "")
            revision_identity.update(
                {
                    "source_key": source_key,
                    "updated_at": updated_at,
                    "fallback": (
                        "identity_missing" if not source_key and not updated_at else ""
                    ),
                }
            )
        identities.append(
            {
                "corpus_id": str(row.get("corpus_id") or ""),
                "doc_id": str(row.get("doc_id") or ""),
                "revision_identity": revision_identity,
            }
        )
    identities.sort(
        key=lambda row: (
            row["corpus_id"],
            row["doc_id"],
            canonical_json_bytes(row["revision_identity"]),
        )
    )
    payload = {"corpus_ids": list(scoped), "documents": identities}
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


async def corpus_doc_set_version(
    db: Any,
    corpus_ids: list[str] | tuple[str, ...] | None,
) -> str:
    scoped = [str(value) for value in corpus_ids or () if str(value)]
    if db is None or not scoped:
        return corpus_doc_set_version_from_rows([], corpus_ids=scoped)
    rows = await (
        db["documents"]
        .find(
            with_active_records({"corpus_id": {"$in": scoped}}),
            {
                "_id": 0,
                "corpus_id": 1,
                "doc_id": 1,
                "source_identity": 1,
                "content_sha256": 1,
                "source_version_id": 1,
                "source_key": 1,
                "revision": 1,
                "source_revision": 1,
                "content_revision": 1,
                "updated_at": 1,
            },
        )
        .to_list(length=None)
    )
    return corpus_doc_set_version_from_rows(rows, corpus_ids=scoped)


def _tier_name(value: Any) -> str:
    raw = str(getattr(value, "value", value) or "")
    if raw == "qdrant_mongo_graph":
        return "graph"
    if raw == "qdrant_mongo":
        return "mongo"
    return "fast"


def _shortlist_profiles(
    shortlist: tuple[LibrarianShortlistItemV1, ...],
) -> dict[tuple[str, str], DocumentProfile]:
    return {
        (item.corpus_id, item.doc_id): DocumentProfile(
            corpus_id=item.corpus_id,
            doc_id=item.doc_id,
            title=item.title,
            summary=item.summary,
        )
        for item in shortlist
    }


def _target_doc_ids(
    text: str,
    shortlist: tuple[LibrarianShortlistItemV1, ...],
    *,
    max_targets: int = 2,
) -> tuple[str, ...]:
    profiles = _shortlist_profiles(shortlist)
    scores = bm25_document_scores(
        text,
        profiles.values(),
        include_headings=False,
    )
    if not scores:
        return ()
    top = max(scores.values())
    admitted = [
        (key, score) for key, score in scores.items() if score >= max(0.10, top * 0.65)
    ]
    admitted.sort(key=lambda row: (-row[1], row[0][0], row[0][1]))
    return tuple(sorted(key[1] for key, _score in admitted[:max_targets]))


def named_source_phrases(query: str) -> tuple[str, ...]:
    """Extract explicit source/author phrases without corpus-specific terms."""

    output: list[str] = []
    for pattern in _NAMED_SOURCE_PATTERNS:
        for match in pattern.finditer(query):
            phrase = " ".join(str(match.group(1) or "").strip(' ,.:;!?"“”').split())
            if phrase and phrase.casefold() not in {
                value.casefold() for value in output
            }:
                output.append(phrase)
    return tuple(output[:4])


_named_source_phrases = named_source_phrases


def _distinctive_source_tokens(text: str) -> tuple[str, ...]:
    return tuple(
        token
        for token in query_tokens(text, stop_words=_NAMED_SOURCE_GENERIC_TOKENS)
        if len(token) >= 3
    )


def _contains_normalized_phrase(haystack: str, needle: str) -> bool:
    return bool(needle and f" {needle} " in f" {haystack} ")


def _named_source_matches_shortlist(
    source: str,
    shortlist: tuple[LibrarianShortlistItemV1, ...],
) -> bool:
    """Require an exact phrase or strong title match; summaries never satisfy."""

    normalized_source = normalize_planner_query(source)
    all_source_tokens = set(query_tokens(source, stop_words=frozenset()))
    if not normalized_source or not all_source_tokens:
        return False
    if all_source_tokens <= _NAMED_SOURCE_GENERIC_TOKENS:
        return False
    source_tokens = set(_distinctive_source_tokens(source))
    for item in shortlist:
        normalized_title = normalize_planner_query(item.title)
        exact_phrase = _contains_normalized_phrase(
            normalized_title,
            normalized_source,
        )
        if exact_phrase:
            return True
        if not source_tokens:
            continue
        title_tokens = set(_distinctive_source_tokens(item.title))
        strong_tokens = source_tokens <= title_tokens and (
            len(source_tokens) >= 2 or all(len(token) >= 5 for token in source_tokens)
        )
        if strong_tokens:
            return True
    return False


def _title_mapped_entities(
    query: str,
    shortlist: tuple[LibrarianShortlistItemV1, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Map named document titles without depending on caller capitalization."""

    normalized_query = normalize_planner_query(query)
    query_token_set = set(
        query_tokens(normalized_query, stop_words=_NAMED_SOURCE_GENERIC_TOKENS)
    )
    mapped: list[tuple[int, str, str, str]] = []
    for item in shortlist:
        normalized_title = normalize_planner_query(item.title)
        title_tokens = set(_distinctive_source_tokens(item.title))
        if not normalized_title or not title_tokens:
            continue
        exact_phrase = _contains_normalized_phrase(
            normalized_query,
            normalized_title,
        )
        strong_tokens = title_tokens <= query_token_set and (
            len(title_tokens) >= 2 or all(len(token) >= 5 for token in title_tokens)
        )
        if not exact_phrase and not strong_tokens:
            continue
        position = normalized_query.find(normalized_title)
        mapped.append(
            (
                position if position >= 0 else len(normalized_query),
                item.corpus_id,
                item.doc_id,
                normalized_title,
            )
        )
    mapped.sort()
    return tuple(
        (title, (doc_id,)) for _position, _corpus_id, doc_id, title in mapped[:4]
    )


def _relationship_sides(query: str) -> tuple[tuple[str, str], ...]:
    live_plan = build_query_plan_v2(query)
    if live_plan.answer_shape not in {"relationship", "comparison"}:
        return ()
    evidence_plan = build_evidence_plan(query)
    if not relationship_allocation_eligible(evidence_plan):
        return ()
    explicit_sides = split_query_sides(query)
    if len(explicit_sides) >= 2:
        return tuple(
            (
                str(side.get("name") or f"side_{index + 1}"),
                str(side.get("query") or side.get("label") or ""),
            )
            for index, side in enumerate(explicit_sides[:2])
        )
    for pattern in _RELATION_SIDE_PATTERNS:
        match = pattern.search(query)
        if match:
            return tuple(
                (
                    f"side_{index + 1}",
                    " ".join(match.group(index + 1).split()),
                )
                for index in range(2)
            )
    if len(live_plan.concepts) >= 2:
        midpoint = max(1, len(live_plan.concepts) // 2)
        concept_sides = (
            " ".join(live_plan.concepts[:midpoint]),
            " ".join(live_plan.concepts[midpoint:]),
        )
        if all(concept_sides):
            return tuple(
                (f"side_{index + 1}", value)
                for index, value in enumerate(concept_sides)
            )
    lanes = list(evidence_plan.required_lanes)
    return tuple((lane.name, lane.query) for lane in lanes[:2])


def _has_entity_bridge_evidence(query: str) -> bool:
    """Require two explicit, non-generic sides; bare conjunctions do not count."""

    planning_query = normalize_planner_query(query)
    for pattern in _ENTITY_BRIDGE_PATTERNS:
        match = pattern.search(planning_query)
        if not match:
            continue
        left = set(
            query_tokens(
                match.group(1),
                stop_words=_ENTITY_BRIDGE_SIDE_STOP_WORDS,
            )
        )
        right = set(
            query_tokens(
                match.group(2),
                stop_words=_ENTITY_BRIDGE_SIDE_STOP_WORDS,
            )
        )
        if left and right and left != right:
            return True
    return False


def llm_escalation_eligible(query: str) -> bool:
    """Use QueryPlanV2 only as an independent complexity eligibility signal."""

    live_plan = build_query_plan_v2(normalize_planner_query(query))
    required_probe_count = len([probe for probe in live_plan.probes if probe.required])
    return bool(
        live_plan.complexity in {"compositional", "dependent_multi_hop"}
        or required_probe_count > 1
    )


def planning_requires_shortlist(
    query: str,
    *,
    allow_llm_escalation: bool = False,
) -> bool:
    """Decide plan-time grounding from canonical text before any I/O."""

    planning_query = normalize_planner_query(query)
    if not planning_query:
        return False
    if len(_relationship_sides(planning_query)) >= 2:
        return True
    if detect_temporal_intent(planning_query).active:
        return True
    live_plan = build_query_plan_v2(planning_query)
    if live_plan.answer_shape == "enumeration" or _ENUMERATIVE_RE.search(
        planning_query
    ):
        return True
    if _named_source_phrases(planning_query):
        return True
    if allow_llm_escalation and llm_escalation_eligible(planning_query):
        return True
    return _has_entity_bridge_evidence(planning_query)


def _subquery(
    *,
    role: str,
    text: str,
    shortlist: tuple[LibrarianShortlistItemV1, ...],
    seat_quota: int,
    tier: str,
    target_text: str | None = None,
) -> LibrarianSubqueryV1:
    return LibrarianSubqueryV1(
        role=role,
        text=text,
        target_doc_ids=_target_doc_ids(target_text or text, shortlist),
        seat_quota=seat_quota,
        tier=tier,
        rerank_cap=max(4, seat_quota * 4),
    )


def _rule_plan(
    planning_query: str,
    *,
    raw_query: str,
    shortlist: tuple[LibrarianShortlistItemV1, ...],
    tier: str,
) -> tuple[str, str, tuple[LibrarianSubqueryV1, ...]]:
    """Run the frozen ordered registry; first deterministic match wins."""

    live_plan = build_query_plan_v2(planning_query)
    relationship_sides = _relationship_sides(planning_query)
    if len(relationship_sides) >= 2:
        shape = (
            "comparison" if live_plan.answer_shape == "comparison" else "relationship"
        )
        subqueries = tuple(
            _subquery(
                role="side_a" if index == 0 else "side_b",
                text=text,
                target_text=text,
                shortlist=shortlist,
                seat_quota=4,
                tier=tier,
            )
            for index, (_name, text) in enumerate(relationship_sides[:2])
        )
        return f"rule:{shape}", shape, subqueries

    temporal = detect_temporal_intent(planning_query)
    if temporal.active:
        time_text = " ".join(item.text for item in temporal.expressions)
        return (
            "rule:temporal",
            "temporal",
            (
                _subquery(
                    role="main",
                    text=planning_query,
                    shortlist=shortlist,
                    seat_quota=5,
                    tier=tier,
                ),
                _subquery(
                    role="time_slice",
                    text=f"{planning_query} temporal evidence {time_text}",
                    target_text=time_text,
                    shortlist=shortlist,
                    seat_quota=3,
                    tier=tier,
                ),
            ),
        )

    trace = _TRACE_RE.search(planning_query)
    if live_plan.answer_shape == "enumeration" or _ENUMERATIVE_RE.search(
        planning_query
    ):
        subqueries: list[LibrarianSubqueryV1] = [
            _subquery(
                role="main",
                text=planning_query,
                shortlist=shortlist,
                seat_quota=5 if trace else DEFAULT_SEAT_BUDGET,
                tier=tier,
            )
        ]
        if trace:
            left = " ".join(trace.group(1).split())
            right = " ".join(trace.group(2).split())
            left_targets = _target_doc_ids(left, shortlist, max_targets=1)
            right_targets = _target_doc_ids(right, shortlist, max_targets=1)
            if left_targets and right_targets and left_targets != right_targets:
                subqueries.append(
                    LibrarianSubqueryV1(
                        role="hop",
                        text=f"How does {left} become {right}?",
                        target_doc_ids=tuple(sorted(set(left_targets + right_targets))),
                        seat_quota=3,
                        tier=tier,
                        rerank_cap=12,
                    )
                )
        return "rule:enumerative_trace", "enumerative_trace", tuple(subqueries)

    mapped_entities = list(_title_mapped_entities(planning_query, shortlist))
    if (
        len(mapped_entities) >= 2
        and mapped_entities[0][1][0] != mapped_entities[1][1][0]
    ):
        first, second = mapped_entities[:2]
        return (
            "rule:entity_bridge",
            "entity_bridge",
            (
                _subquery(
                    role="side_a",
                    text=f"What does the corpus establish about {first[0]}?",
                    target_text=first[0],
                    shortlist=shortlist,
                    seat_quota=3,
                    tier=tier,
                ),
                _subquery(
                    role="side_b",
                    text=f"What does the corpus establish about {second[0]}?",
                    target_text=second[0],
                    shortlist=shortlist,
                    seat_quota=3,
                    tier=tier,
                ),
                LibrarianSubqueryV1(
                    role="hop",
                    text=f"How are {first[0]} and {second[0]} connected?",
                    target_doc_ids=tuple(sorted(set(first[1] + second[1]))),
                    seat_quota=2,
                    tier=tier,
                    rerank_cap=8,
                ),
            ),
        )

    return (
        "rule:simple",
        "simple",
        (
            _subquery(
                role="main",
                text=raw_query,
                shortlist=shortlist,
                seat_quota=DEFAULT_SEAT_BUDGET,
                tier=tier,
            ),
        ),
    )


def build_query_plan_v1(
    query: str,
    *,
    corpus_id: str,
    corpus_doc_version: str,
    shortlist: Iterable[LibrarianShortlistItemV1 | dict[str, Any]] = (),
    requested_tier: Any = "qdrant_mongo",
    cache_hit: bool = False,
    allow_llm_escalation: bool = False,
) -> QueryPlanV1:
    normalized = normalize_planner_query(query)
    if not normalized:
        raise ValueError("librarian planner requires a non-blank query")
    requires_shortlist = planning_requires_shortlist(
        normalized,
        allow_llm_escalation=allow_llm_escalation,
    )
    ordered_shortlist = (
        tuple(
            sorted(
                (
                    item
                    if isinstance(item, LibrarianShortlistItemV1)
                    else LibrarianShortlistItemV1.model_validate(item)
                    for item in shortlist
                ),
                key=lambda item: (-item.score, item.corpus_id, item.doc_id),
            )[:SHORTLIST_LIMIT]
        )
        if requires_shortlist
        else ()
    )
    tier = _tier_name(requested_tier)
    planner, shape, subqueries = _rule_plan(
        normalized,
        raw_query=query,
        shortlist=ordered_shortlist,
        tier=tier,
    )
    named_sources = _named_source_phrases(normalized)
    named_source_missing = bool(
        named_sources
        and not any(
            _named_source_matches_shortlist(source, ordered_shortlist)
            for source in named_sources
        )
    )
    cache_key = plan_cache_key_for(
        normalized_query=normalized,
        corpus_id=corpus_id,
        corpus_doc_version=corpus_doc_version,
        planner_prompt_hash=LLM_DECOMPOSER_PROMPT_HASH,
    )
    return QueryPlanV1(
        plan_hash=plan_hash_for(
            normalized,
            corpus_doc_version,
            PLANNER_VERSION,
        ),
        normalized_query=normalized,
        corpus_id=corpus_id,
        corpus_doc_version=corpus_doc_version,
        planner=planner,
        shape=shape,
        shortlist=ordered_shortlist,
        subqueries=subqueries,
        refusal_signals=LibrarianRefusalSignalsV1(
            shortlist_empty=not ordered_shortlist,
            named_source_missing=named_source_missing,
        ),
        cache=LibrarianPlanCacheV1(hit=cache_hit, key=cache_key),
    )


async def build_tier0_shortlist(
    query: str,
    *,
    corpus_ids: list[str] | tuple[str, ...],
    db: Any,
    embedding_config: dict[str, Any] | None,
) -> tuple[tuple[LibrarianShortlistItemV1, ...], dict[str, Any]]:
    """Reuse four-lane lexical+semantic document routing, never parent vectors."""

    if db is None or not corpus_ids:
        return (), {
            "status": "empty",
            "reason": "missing_database_or_corpus_scope",
            "lanes": ["lexical", "semantic"],
        }
    planning_query = normalize_planner_query(query)
    if not planning_query:
        raise ValueError("Tier-0 shortlist requires a non-blank query")
    vectors = await embed_queries([planning_query], embedding_config)
    vector = vectors[0] if vectors else None
    routes, diagnostics = await four_lane_document_router.route_summary_shortlist(
        query=planning_query,
        vector=vector,
        corpus_ids=list(corpus_ids),
        db=db,
        semantic_router=tier0_document_router,
        max_documents=SHORTLIST_LIMIT,
    )
    shortlist = tuple(
        LibrarianShortlistItemV1(
            corpus_id=route.corpus_id,
            doc_id=route.doc_id,
            title=route.title,
            summary=route.summary,
            score=route.score,
        )
        for route in routes[:SHORTLIST_LIMIT]
    )
    return shortlist, diagnostics


class LibrarianPlanner:
    def __init__(
        self,
        *,
        cache: QueryPlanReplayCache | None = None,
        decomposer: Any = None,
    ) -> None:
        self.cache = cache or QueryPlanReplayCache()
        self.decomposer = decomposer

    async def build(
        self,
        query: str,
        *,
        corpus_ids: list[str] | tuple[str, ...] | None,
        requested_tier: Any,
        db: Any,
        embedding_config: dict[str, Any] | None,
        user_id: str | None = None,
        llm_decomposer_enabled: bool = False,
    ) -> LibrarianPlanBuildResult:
        scoped = tuple(sorted({str(value) for value in corpus_ids or () if str(value)}))
        scope_identity = corpus_scope_identity(scoped)
        normalized = normalize_planner_query(query)
        if not normalized:
            raise ValueError("librarian planner requires a non-blank query")
        requires_shortlist = planning_requires_shortlist(
            normalized,
            allow_llm_escalation=llm_decomposer_enabled,
        )
        version_before = await corpus_doc_set_version(db, scoped)
        if not requires_shortlist:
            # Ordinary lookups preserve today's raw question bytes and do not
            # enter the plan cache: a normalized-equivalent earlier request
            # must never replace the current request's exact text.
            plan = build_query_plan_v1(
                query,
                corpus_id=scope_identity,
                corpus_doc_version=version_before,
                shortlist=(),
                requested_tier=requested_tier,
                cache_hit=False,
                allow_llm_escalation=False,
            )
            return LibrarianPlanBuildResult(
                plan=plan,
                diagnostics={
                    "status": "simple_bypass",
                    "registry_order": list(RULE_REGISTRY_ORDER),
                    "shortlist_calls": 0,
                    "query_embedding_calls": 0,
                    "llm_escalation": LLM_ESCALATION_STATUS,
                    "llm_escalation_eligible": False,
                    "provider_calls": 0,
                    "provider_attempts": 0,
                    "silent_fallback_count": 0,
                },
            )

        cache_key = plan_cache_key_for(
            normalized_query=normalized,
            corpus_id=scope_identity,
            corpus_doc_version=version_before,
            planner_prompt_hash=LLM_DECOMPOSER_PROMPT_HASH,
        )
        cached = self.cache.get(cache_key)
        if (
            cached is not None
            and cached.shape != "simple"
            and (cached.planner != "llm:v1" or llm_decomposer_enabled)
        ):
            return LibrarianPlanBuildResult(
                plan=cached.model_copy(
                    update={
                        "cache": LibrarianPlanCacheV1(
                            hit=True,
                            key=cache_key,
                        )
                    }
                ),
                diagnostics={
                    "status": "cache_hit",
                    "registry_order": list(RULE_REGISTRY_ORDER),
                    "shortlist_calls": 0,
                    "query_embedding_calls": 0,
                    "llm_escalation": LLM_ESCALATION_STATUS,
                    "llm_escalation_eligible": cached.planner == "llm:v1",
                    "provider_calls": 0,
                    "provider_attempts": 0,
                    "silent_fallback_count": 0,
                },
            )

        shortlist_calls = 1
        shortlist, shortlist_diagnostics = await build_tier0_shortlist(
            normalized,
            corpus_ids=scoped,
            db=db,
            embedding_config=embedding_config,
        )
        version_after = await corpus_doc_set_version(db, scoped)
        if version_after != version_before:
            # One bounded retry binds the shortlist and plan to one corpus state.
            shortlist_calls += 1
            shortlist, shortlist_diagnostics = await build_tier0_shortlist(
                normalized,
                corpus_ids=scoped,
                db=db,
                embedding_config=embedding_config,
            )
            version_final = await corpus_doc_set_version(db, scoped)
            if version_final != version_after:
                raise RuntimeError(
                    "corpus document set changed twice during librarian planning"
                )
            version_before = version_final

        plan = build_query_plan_v1(
            normalized,
            corpus_id=scope_identity,
            corpus_doc_version=version_before,
            shortlist=shortlist,
            requested_tier=requested_tier,
            cache_hit=False,
            allow_llm_escalation=llm_decomposer_enabled,
        )
        if plan.shape == "simple":
            # A conservative grounded preflight may find no bridge after
            # seeing the titles. Preserve this request's raw bytes and keep it
            # out of cache so a prior normalized-equivalent request cannot
            # leak its display text into the current trace.
            plan = build_query_plan_v1(
                query,
                corpus_id=scope_identity,
                corpus_doc_version=version_before,
                shortlist=shortlist,
                requested_tier=requested_tier,
                cache_hit=False,
                allow_llm_escalation=llm_decomposer_enabled,
            )
            if llm_decomposer_enabled and llm_escalation_eligible(normalized):
                decomposer = self.decomposer
                if decomposer is None:
                    from services.retriever.librarian_decomposer import (
                        librarian_decomposer,
                    )

                    decomposer = librarian_decomposer
                decomposition = await decomposer.decompose(
                    base_plan=plan,
                    user_id=user_id,
                )
                if decomposition.plan.planner == "llm:v1":
                    self.cache.put(decomposition.plan.cache.key, decomposition.plan)
                return LibrarianPlanBuildResult(
                    plan=decomposition.plan,
                    diagnostics={
                        "status": (
                            "llm_built"
                            if decomposition.plan.planner == "llm:v1"
                            else "llm_fallback_simple"
                        ),
                        "registry_order": list(RULE_REGISTRY_ORDER),
                        "shortlist": shortlist_diagnostics,
                        "shortlist_calls": shortlist_calls,
                        "query_embedding_calls": shortlist_calls,
                        "llm_escalation": decomposition.diagnostics(),
                        "llm_escalation_eligible": True,
                        "provider_calls": decomposition.provider_attempts,
                        "provider_attempts": decomposition.provider_attempts,
                        "silent_fallback_count": (decomposition.silent_fallback_count),
                    },
                )
            return LibrarianPlanBuildResult(
                plan=plan,
                diagnostics={
                    "status": "grounded_simple_bypass",
                    "registry_order": list(RULE_REGISTRY_ORDER),
                    "shortlist": shortlist_diagnostics,
                    "shortlist_calls": shortlist_calls,
                    "query_embedding_calls": shortlist_calls,
                    "llm_escalation": LLM_ESCALATION_STATUS,
                    "llm_escalation_eligible": False,
                    "provider_calls": 0,
                    "provider_attempts": 0,
                    "silent_fallback_count": 0,
                },
            )
        self.cache.put(plan.cache.key, plan)
        return LibrarianPlanBuildResult(
            plan=plan,
            diagnostics={
                "status": "built",
                "registry_order": list(RULE_REGISTRY_ORDER),
                "shortlist": shortlist_diagnostics,
                "shortlist_calls": shortlist_calls,
                "query_embedding_calls": shortlist_calls,
                "llm_escalation": LLM_ESCALATION_STATUS,
                "llm_escalation_eligible": False,
                "provider_calls": 0,
                "provider_attempts": 0,
                "silent_fallback_count": 0,
            },
        )


librarian_planner = LibrarianPlanner()
