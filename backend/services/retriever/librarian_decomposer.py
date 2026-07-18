"""Bounded configured-route escalation for Librarian planning and refinement."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.librarian_query_plan import (
    LLM_DECOMPOSER_SYSTEM_PROMPT,
    LLM_REFINER_PROMPT_HASH,
    LLM_REFINER_SYSTEM_PROMPT,
    LibrarianPlanCacheV1,
    LibrarianSubqueryV1,
    QueryPlanV1,
    librarian_execution_lane_id,
    normalize_planner_query,
)
from services.llm import llm_service
from services.query_model_resolver import resolve as resolve_query_model_kind


DECOMPOSER_TIMEOUT_SECONDS = 2.0
DECOMPOSER_MAX_TOKENS = 600
DECOMPOSER_MAX_QUESTION_CHARS = 1200
DECOMPOSER_MAX_TITLE_CHARS = 240
DECOMPOSER_MAX_SUMMARY_CHARS = 1000
REFINER_TIMEOUT_SECONDS = 2.0
REFINER_MAX_TOKENS = 600
REFINER_CACHE_LIMIT = 256
_ROLE_ORDER = {
    "main": 0,
    "side_a": 1,
    "side_b": 2,
    "facet": 3,
    "hop": 4,
    "time_slice": 5,
}


async def _resolve_librarian_call_route(
    resolver: Any,
    user_id: str,
) -> dict[str, Any] | None:
    """Use the configured synthesis route for every Librarian model call.

    Decomposition, refinement, and answer synthesis must share one concrete
    provider/model identity.  Falling back to the independently configured
    Utility role caused DeepSeek V4 Flash helper calls to dispatch as
    ``openai/deepseek-v4-flash`` while synthesis used the registered
    ``deepseek/deepseek-v4-flash`` route.  Keep a Utility fallback only for
    installations that have not configured a synthesis role yet.
    """

    route = await resolver(user_id, "synthesis")
    if isinstance(route, dict) and str(route.get("model") or "").strip():
        return route
    return await resolver(user_id, "utility")


class _ProposedSubquery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["main", "side_a", "side_b", "facet", "hop", "time_slice"]
    text: str = Field(min_length=1, max_length=600)
    target_doc_ids: tuple[str, ...] = ()

    @field_validator("text")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        cleaned = " ".join(str(value or "").split())
        if not cleaned:
            raise ValueError("subquery text must not be blank")
        return cleaned

    @field_validator("target_doc_ids")
    @classmethod
    def _stable_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        targets = tuple(
            sorted({str(item).strip() for item in value if str(item).strip()})
        )
        if len(targets) > 8:
            raise ValueError("subquery may target at most eight shortlist documents")
        return targets


class _DecompositionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    shape: Literal["complex"]
    subqueries: tuple[_ProposedSubquery, ...]

    @field_validator("subqueries")
    @classmethod
    def _bounded_subqueries(
        cls,
        value: tuple[_ProposedSubquery, ...],
    ) -> tuple[_ProposedSubquery, ...]:
        if not 1 <= len(value) <= 4:
            raise ValueError("decomposer requires one to four subqueries")
        return value

    @model_validator(mode="after")
    def _no_ambiguous_role_or_text_duplicates(self) -> "_DecompositionProposal":
        singleton_roles = {"main", "side_a", "side_b", "time_slice"}
        present = [
            item.role for item in self.subqueries if item.role in singleton_roles
        ]
        if len(present) != len(set(present)):
            raise ValueError("singleton decomposer roles may not repeat")
        normalized_texts = [
            normalize_planner_query(item.text) for item in self.subqueries
        ]
        if len(normalized_texts) != len(set(normalized_texts)):
            raise ValueError("decomposer subquery texts must be distinct")
        return self


class LibrarianRefinementGap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subquery_index: int = Field(ge=0, le=3)
    lane_id: str = Field(min_length=1)
    role: Literal["main", "side_a", "side_b", "facet", "hop", "time_slice"]
    reasons: tuple[
        Literal[
            "empty_above_admission",
            "required_role_without_seated_document",
            "targeted_shortlist_miss",
        ],
        ...,
    ]

    @field_validator("reasons")
    @classmethod
    def _canonical_reasons(cls, value):
        canonical = (
            "empty_above_admission",
            "required_role_without_seated_document",
            "targeted_shortlist_miss",
        )
        ordered = tuple(reason for reason in canonical if reason in set(value))
        if not ordered:
            raise ValueError("a refinement gap requires at least one reason")
        return ordered


class LibrarianSeatedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    title: str = ""
    summary: str = ""
    score: float = Field(ge=0.0)
    lane_ids: tuple[str, ...] = ()

    @field_validator("lane_ids")
    @classmethod
    def _stable_lane_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({str(item) for item in value if str(item)}))

    @field_validator("score")
    @classmethod
    def _finite_score(cls, value: float) -> float:
        if not float("-inf") < value < float("inf"):
            raise ValueError("seated document score must be finite")
        return round(float(value), 6)


class _RefinedSubquery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subquery_index: int = Field(ge=0, le=3)
    role: Literal["main", "side_a", "side_b", "facet", "hop", "time_slice"]
    text: str = Field(min_length=1, max_length=600)
    target_doc_ids: tuple[str, ...] = ()

    @field_validator("text")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        cleaned = " ".join(str(value or "").split())
        if not cleaned:
            raise ValueError("refined subquery text must not be blank")
        return cleaned

    @field_validator("target_doc_ids")
    @classmethod
    def _stable_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        targets = tuple(
            sorted({str(item).strip() for item in value if str(item).strip()})
        )
        if len(targets) > 8:
            raise ValueError("refined subquery may target at most eight documents")
        return targets


class _RefinementProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subqueries: tuple[_RefinedSubquery, ...]

    @field_validator("subqueries")
    @classmethod
    def _bounded_subqueries(
        cls,
        value: tuple[_RefinedSubquery, ...],
    ) -> tuple[_RefinedSubquery, ...]:
        if not 1 <= len(value) <= 4:
            raise ValueError("refiner requires one to four subqueries")
        indexes = [item.subquery_index for item in value]
        if len(indexes) != len(set(indexes)):
            raise ValueError("refined subquery indexes must be unique")
        texts = [normalize_planner_query(item.text) for item in value]
        if len(texts) != len(set(texts)):
            raise ValueError("refined subquery texts must be distinct")
        return value


@dataclass(frozen=True)
class LibrarianDecompositionResult:
    plan: QueryPlanV1
    status: str
    reason: str
    provider_attempts: int
    silent_fallback_count: int

    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "provider_calls": self.provider_attempts,
            "provider_attempts": self.provider_attempts,
            "silent_fallback_count": self.silent_fallback_count,
            "timeout_seconds": DECOMPOSER_TIMEOUT_SECONDS,
            "max_tokens": DECOMPOSER_MAX_TOKENS,
            "route": "utility",
        }


@dataclass(frozen=True)
class LibrarianRefinementResult:
    plan: QueryPlanV1
    status: str
    reason: str
    gaps: tuple[LibrarianRefinementGap, ...]
    refined_subquery_indexes: tuple[int, ...]
    cache_hit: bool
    cache_key: str
    seated_document_identity_hash: str
    provider_attempts: int
    silent_fallback_count: int

    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "fired": bool(self.gaps),
            "gaps": [item.model_dump(mode="json") for item in self.gaps],
            "refined_subquery_indexes": list(self.refined_subquery_indexes),
            "refined_plan": (
                self.plan.model_dump(mode="json") if self.status == "built" else None
            ),
            "cache": {"hit": self.cache_hit, "key": self.cache_key},
            "seated_document_identity_hash": self.seated_document_identity_hash,
            "prompt_hash": LLM_REFINER_PROMPT_HASH,
            "provider_calls": self.provider_attempts,
            "provider_attempts": self.provider_attempts,
            "silent_fallback_count": self.silent_fallback_count,
            "planner_refinement_unavailable": self.status == "fallback",
            "round": 1 if self.status == "built" else 0,
            "timeout_seconds": REFINER_TIMEOUT_SECONDS,
            "max_tokens": REFINER_MAX_TOKENS,
            "route": "utility",
        }


def detect_librarian_refinement_gaps(
    *,
    plan: QueryPlanV1,
    reservation_receipt: dict[str, Any],
    seated_doc_ids_by_lane: dict[str, set[str]],
) -> tuple[LibrarianRefinementGap, ...]:
    """Name post-allocation gaps without invoking a model or changing scores."""

    if plan.shape == "simple":
        return ()
    lane_candidates = dict(reservation_receipt.get("lane_candidates") or {})
    gaps: list[LibrarianRefinementGap] = []
    for index, subquery in enumerate(plan.subqueries):
        lane_id = librarian_execution_lane_id(index, subquery.role)
        seated_doc_ids = {
            str(value)
            for value in seated_doc_ids_by_lane.get(lane_id, set())
            if str(value)
        }
        reasons: list[str] = []
        lane_candidate_receipt = dict(lane_candidates.get(lane_id) or {})
        if int(lane_candidate_receipt.get("score_eligible_candidates") or 0) <= 0:
            reasons.append("empty_above_admission")
        if not seated_doc_ids:
            reasons.append("required_role_without_seated_document")
        if subquery.target_doc_ids and not (
            set(subquery.target_doc_ids) & seated_doc_ids
        ):
            reasons.append("targeted_shortlist_miss")
        if reasons:
            gaps.append(
                LibrarianRefinementGap(
                    subquery_index=index,
                    lane_id=lane_id,
                    role=subquery.role,
                    reasons=tuple(reasons),
                )
            )
    return tuple(gaps)


def _seated_identity_hash(
    documents: tuple[LibrarianSeatedDocument, ...],
) -> str:
    identities = sorted(
        (item.corpus_id, item.doc_id, item.lane_ids) for item in documents
    )
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(identities, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def _refinement_cache_key(
    *,
    plan: QueryPlanV1,
    seated_document_identity_hash: str,
) -> str:
    payload = {
        "normalized_query": plan.normalized_query,
        "corpus_doc_version": plan.corpus_doc_version,
        "seated_document_identity_hash": seated_document_identity_hash,
        "decomposer_prompt_hash": LLM_REFINER_PROMPT_HASH,
    }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def _strict_refinement_proposal(raw: str) -> _RefinementProposal:
    text = str(raw or "")
    if not text.strip():
        raise ValueError("refiner returned empty content")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("refiner root must be a JSON object")
    return _RefinementProposal.model_validate(value)


def _compile_refined_plan(
    *,
    base_plan: QueryPlanV1,
    proposal: _RefinementProposal,
    gaps: tuple[LibrarianRefinementGap, ...],
    seated_documents: tuple[LibrarianSeatedDocument, ...],
) -> QueryPlanV1:
    gaps_by_index = {item.subquery_index: item for item in gaps}
    proposed_by_index = {item.subquery_index: item for item in proposal.subqueries}
    if set(proposed_by_index) != set(gaps_by_index):
        raise ValueError("refiner must return every and only gapped subquery")
    admitted_doc_ids = {item.doc_id for item in seated_documents}
    subqueries = list(base_plan.subqueries)
    for index, refined in proposed_by_index.items():
        gap = gaps_by_index[index]
        if refined.role != gap.role:
            raise ValueError("refiner changed a gapped subquery role")
        if set(refined.target_doc_ids) - admitted_doc_ids:
            raise ValueError("refiner targeted a document that was not seated")
        original = subqueries[index]
        if (
            normalize_planner_query(refined.text)
            == normalize_planner_query(original.text)
            and refined.target_doc_ids == original.target_doc_ids
        ):
            raise ValueError("refiner returned an unchanged gapped subquery")
        subqueries[index] = LibrarianSubqueryV1(
            role=original.role,
            text=refined.text,
            target_doc_ids=refined.target_doc_ids,
            seat_quota=original.seat_quota,
            tier=original.tier,
            rerank_cap=original.rerank_cap,
        )
    payload = base_plan.model_dump(mode="json")
    payload["subqueries"] = [item.model_dump(mode="json") for item in subqueries]
    return QueryPlanV1.model_validate(payload)


class LibrarianRefiner:
    def __init__(
        self,
        *,
        resolver: Any = None,
        completion_service: Any = None,
        cache_limit: int = REFINER_CACHE_LIMIT,
    ) -> None:
        self._resolver = resolver or resolve_query_model_kind
        self._completion_service = completion_service or llm_service
        self._cache_limit = max(1, int(cache_limit))
        self._cache: OrderedDict[str, QueryPlanV1] = OrderedDict()

    def _fallback(
        self,
        *,
        plan: QueryPlanV1,
        gaps: tuple[LibrarianRefinementGap, ...],
        seated_hash: str,
        cache_key: str,
        reason: str,
        provider_attempts: int,
    ) -> LibrarianRefinementResult:
        return LibrarianRefinementResult(
            plan=plan,
            status="fallback",
            reason=reason,
            gaps=gaps,
            refined_subquery_indexes=(),
            cache_hit=False,
            cache_key=cache_key,
            seated_document_identity_hash=seated_hash,
            provider_attempts=provider_attempts,
            silent_fallback_count=1,
        )

    async def refine(
        self,
        *,
        base_plan: QueryPlanV1,
        original_query: str,
        gaps: tuple[LibrarianRefinementGap, ...],
        seated_documents: tuple[LibrarianSeatedDocument, ...],
        user_id: str | None,
    ) -> LibrarianRefinementResult:
        ordered_documents = tuple(
            sorted(
                seated_documents,
                key=lambda item: (-item.score, item.corpus_id, item.doc_id),
            )[:8]
        )
        seated_hash = _seated_identity_hash(ordered_documents)
        cache_key = _refinement_cache_key(
            plan=base_plan,
            seated_document_identity_hash=seated_hash,
        )
        if not gaps:
            return LibrarianRefinementResult(
                plan=base_plan,
                status="not_needed",
                reason="no_deterministic_gap",
                gaps=(),
                refined_subquery_indexes=(),
                cache_hit=False,
                cache_key=cache_key,
                seated_document_identity_hash=seated_hash,
                provider_attempts=0,
                silent_fallback_count=0,
            )
        cached = self._cache.get(cache_key)
        if cached is not None:
            expected_indexes = {item.subquery_index for item in gaps}
            changed_indexes = {
                index
                for index, (before, after) in enumerate(
                    zip(base_plan.subqueries, cached.subqueries)
                )
                if before != after
            }
            if changed_indexes == expected_indexes:
                self._cache.move_to_end(cache_key)
                return LibrarianRefinementResult(
                    plan=cached,
                    status="built",
                    reason="validated_cached_refinement",
                    gaps=gaps,
                    refined_subquery_indexes=tuple(
                        item.subquery_index for item in gaps
                    ),
                    cache_hit=True,
                    cache_key=cache_key,
                    seated_document_identity_hash=seated_hash,
                    provider_attempts=0,
                    silent_fallback_count=0,
                )
            self._cache.pop(cache_key, None)
        if not ordered_documents:
            return self._fallback(
                plan=base_plan,
                gaps=gaps,
                seated_hash=seated_hash,
                cache_key=cache_key,
                reason="seated_document_context_empty",
                provider_attempts=0,
            )
        if not user_id:
            return self._fallback(
                plan=base_plan,
                gaps=gaps,
                seated_hash=seated_hash,
                cache_key=cache_key,
                reason="user_id_unavailable",
                provider_attempts=0,
            )
        try:
            route = await _resolve_librarian_call_route(self._resolver, user_id)
        except asyncio.CancelledError:
            return self._fallback(
                plan=base_plan,
                gaps=gaps,
                seated_hash=seated_hash,
                cache_key=cache_key,
                reason="utility_resolver_cancelled",
                provider_attempts=0,
            )
        except Exception as exc:  # noqa: BLE001 - fail-open is the contract
            return self._fallback(
                plan=base_plan,
                gaps=gaps,
                seated_hash=seated_hash,
                cache_key=cache_key,
                reason=f"utility_resolver_error:{type(exc).__name__}",
                provider_attempts=0,
            )
        if not isinstance(route, dict) or not str(route.get("model") or "").strip():
            return self._fallback(
                plan=base_plan,
                gaps=gaps,
                seated_hash=seated_hash,
                cache_key=cache_key,
                reason="utility_route_unavailable",
                provider_attempts=0,
            )
        extra_params = {
            key: value
            for key, value in dict(route.get("extra_params") or {}).items()
            if key
            not in {
                "disable_thinking",
                "enable_thinking",
                "reasoning_effort",
                "think",
                "thinking",
            }
        }
        extra_params["disable_thinking"] = True
        request_payload = {
            "question": str(original_query)[:DECOMPOSER_MAX_QUESTION_CHARS],
            "plan": base_plan.model_dump(mode="json"),
            "gaps": [item.model_dump(mode="json") for item in gaps],
            "seated_documents": [
                {
                    "corpus_id": item.corpus_id,
                    "doc_id": item.doc_id,
                    "title": item.title[:DECOMPOSER_MAX_TITLE_CHARS],
                    "summary": item.summary[:DECOMPOSER_MAX_SUMMARY_CHARS],
                    "lane_ids": list(item.lane_ids),
                }
                for item in ordered_documents
            ],
        }
        try:
            raw = await self._completion_service.complete_sync(
                [
                    {"role": "system", "content": LLM_REFINER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            request_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                ],
                model=route["model"],
                temperature=0,
                max_tokens=REFINER_MAX_TOKENS,
                api_base=route.get("api_base"),
                api_key=route.get("api_key"),
                extra_params=extra_params or None,
                response_format={"type": "json_object"},
                timeout=REFINER_TIMEOUT_SECONDS,
            )
            proposal = _strict_refinement_proposal(raw)
            plan = _compile_refined_plan(
                base_plan=base_plan,
                proposal=proposal,
                gaps=gaps,
                seated_documents=ordered_documents,
            )
        except asyncio.CancelledError:
            return self._fallback(
                plan=base_plan,
                gaps=gaps,
                seated_hash=seated_hash,
                cache_key=cache_key,
                reason="provider_cancelled",
                provider_attempts=1,
            )
        except Exception as exc:  # noqa: BLE001 - exactly one fail-open attempt
            return self._fallback(
                plan=base_plan,
                gaps=gaps,
                seated_hash=seated_hash,
                cache_key=cache_key,
                reason=f"planner_refinement_unavailable:{type(exc).__name__}",
                provider_attempts=1,
            )
        self._cache[cache_key] = plan
        self._cache.move_to_end(cache_key)
        while len(self._cache) > self._cache_limit:
            self._cache.popitem(last=False)
        return LibrarianRefinementResult(
            plan=plan,
            status="built",
            reason="validated_utility_refinement",
            gaps=gaps,
            refined_subquery_indexes=tuple(item.subquery_index for item in gaps),
            cache_hit=False,
            cache_key=cache_key,
            seated_document_identity_hash=seated_hash,
            provider_attempts=1,
            silent_fallback_count=0,
        )


def _fallback_plan(base_plan: QueryPlanV1) -> QueryPlanV1:
    payload = base_plan.model_dump(mode="json")
    payload["planner"] = "fallback:simple"
    payload["shape"] = "simple"
    payload["refusal_signals"] = {
        **dict(payload.get("refusal_signals") or {}),
        "planner_llm_unavailable": True,
    }
    payload["cache"] = {
        "hit": False,
        "key": base_plan.cache.key,
    }
    return QueryPlanV1.model_validate(payload)


def _seat_quotas(proposals: tuple[_ProposedSubquery, ...]) -> list[int]:
    count = len(proposals)
    if count == 1:
        return [8]
    roles = [item.role for item in proposals]
    if count == 2:
        if set(roles) == {"side_a", "side_b"}:
            return [4, 4]
        return (
            [5 if role == "main" else 3 for role in roles]
            if "main" in roles
            else [4, 4]
        )
    if count == 3:
        if {"side_a", "side_b"} <= set(roles):
            return [3 if role in {"side_a", "side_b"} else 2 for role in roles]
        if "main" in roles:
            return [4 if role == "main" else 2 for role in roles]
        return [3, 3, 2]
    return [2, 2, 2, 2]


def _compile_plan(
    base_plan: QueryPlanV1,
    proposal: _DecompositionProposal,
) -> QueryPlanV1:
    known_doc_ids = {item.doc_id for item in base_plan.shortlist}
    for subquery in proposal.subqueries:
        unknown = set(subquery.target_doc_ids) - known_doc_ids
        if unknown:
            raise ValueError("decomposer targeted a document outside the shortlist")
    ordered = tuple(
        sorted(
            proposal.subqueries,
            key=lambda item: (
                _ROLE_ORDER[item.role],
                normalize_planner_query(item.text),
                item.target_doc_ids,
            ),
        )
    )
    quotas = _seat_quotas(ordered)
    tier = base_plan.subqueries[0].tier
    subqueries = tuple(
        LibrarianSubqueryV1(
            role=item.role,
            text=item.text,
            target_doc_ids=item.target_doc_ids,
            seat_quota=quota,
            tier=tier,
            rerank_cap=max(4, quota * 4),
        )
        for item, quota in zip(ordered, quotas)
    )
    payload = base_plan.model_dump(mode="json")
    payload.update(
        {
            "planner": "llm:v1",
            "shape": "complex",
            "subqueries": [item.model_dump(mode="json") for item in subqueries],
            "refusal_signals": {
                **dict(payload.get("refusal_signals") or {}),
                "planner_llm_unavailable": False,
            },
            "cache": LibrarianPlanCacheV1(
                hit=False,
                key=base_plan.cache.key,
            ).model_dump(mode="json"),
        }
    )
    return QueryPlanV1.model_validate(payload)


def _strict_proposal(raw: str) -> _DecompositionProposal:
    text = str(raw or "")
    if not text.strip():
        raise ValueError("decomposer returned empty content")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("decomposer root must be a JSON object")
    return _DecompositionProposal.model_validate(value)


class LibrarianDecomposer:
    def __init__(
        self,
        *,
        resolver: Any = None,
        completion_service: Any = None,
    ) -> None:
        self._resolver = resolver or resolve_query_model_kind
        self._completion_service = completion_service or llm_service

    async def decompose(
        self,
        *,
        base_plan: QueryPlanV1,
        user_id: str | None,
    ) -> LibrarianDecompositionResult:
        fallback = _fallback_plan(base_plan)
        if not base_plan.shortlist:
            return LibrarianDecompositionResult(
                plan=fallback,
                status="fallback",
                reason="shortlist_empty",
                provider_attempts=0,
                silent_fallback_count=1,
            )
        if not user_id:
            return LibrarianDecompositionResult(
                plan=fallback,
                status="fallback",
                reason="user_id_unavailable",
                provider_attempts=0,
                silent_fallback_count=1,
            )
        try:
            route = await _resolve_librarian_call_route(self._resolver, user_id)
        except asyncio.CancelledError:
            return LibrarianDecompositionResult(
                plan=fallback,
                status="fallback",
                reason="utility_resolver_cancelled",
                provider_attempts=0,
                silent_fallback_count=1,
            )
        except Exception as exc:  # noqa: BLE001 - fail-open is the contract
            return LibrarianDecompositionResult(
                plan=fallback,
                status="fallback",
                reason=f"utility_resolver_error:{type(exc).__name__}",
                provider_attempts=0,
                silent_fallback_count=1,
            )
        if not isinstance(route, dict) or not str(route.get("model") or "").strip():
            return LibrarianDecompositionResult(
                plan=fallback,
                status="fallback",
                reason="utility_route_unavailable",
                provider_attempts=0,
                silent_fallback_count=1,
            )

        request_payload = {
            "question": base_plan.normalized_query[:DECOMPOSER_MAX_QUESTION_CHARS],
            "shortlist": [
                {
                    "doc_id": item.doc_id,
                    "title": item.title[:DECOMPOSER_MAX_TITLE_CHARS],
                    "summary": item.summary[:DECOMPOSER_MAX_SUMMARY_CHARS],
                }
                for item in base_plan.shortlist[:8]
            ],
        }
        extra_params = {
            key: value
            for key, value in dict(route.get("extra_params") or {}).items()
            if key
            not in {
                "disable_thinking",
                "enable_thinking",
                "reasoning_effort",
                "think",
                "thinking",
            }
        }
        extra_params["disable_thinking"] = True
        try:
            raw = await self._completion_service.complete_sync(
                [
                    {
                        "role": "system",
                        "content": LLM_DECOMPOSER_SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            request_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                ],
                model=route["model"],
                temperature=0,
                max_tokens=DECOMPOSER_MAX_TOKENS,
                api_base=route.get("api_base"),
                api_key=route.get("api_key"),
                extra_params=extra_params or None,
                response_format={"type": "json_object"},
                timeout=DECOMPOSER_TIMEOUT_SECONDS,
            )
            proposal = _strict_proposal(raw)
            plan = _compile_plan(base_plan, proposal)
        except asyncio.CancelledError:
            return LibrarianDecompositionResult(
                plan=fallback,
                status="fallback",
                reason="provider_cancelled",
                provider_attempts=1,
                silent_fallback_count=1,
            )
        except Exception as exc:  # noqa: BLE001 - one attempt, deterministic fallback
            return LibrarianDecompositionResult(
                plan=fallback,
                status="fallback",
                reason=f"provider_or_output_error:{type(exc).__name__}",
                provider_attempts=1,
                silent_fallback_count=1,
            )
        return LibrarianDecompositionResult(
            plan=plan,
            status="built",
            reason="validated_utility_decomposition",
            provider_attempts=1,
            silent_fallback_count=0,
        )


librarian_decomposer = LibrarianDecomposer()
librarian_refiner = LibrarianRefiner()
