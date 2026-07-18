"""Bounded Utility-route escalation for otherwise-unparsed Librarian plans."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.librarian_query_plan import (
    LLM_DECOMPOSER_SYSTEM_PROMPT,
    LibrarianPlanCacheV1,
    LibrarianSubqueryV1,
    QueryPlanV1,
    normalize_planner_query,
)
from services.llm import llm_service
from services.query_model_resolver import resolve as resolve_query_model_kind


DECOMPOSER_TIMEOUT_SECONDS = 2.0
DECOMPOSER_MAX_TOKENS = 600
DECOMPOSER_MAX_QUESTION_CHARS = 1200
DECOMPOSER_MAX_TITLE_CHARS = 240
DECOMPOSER_MAX_SUMMARY_CHARS = 1000
_ROLE_ORDER = {
    "main": 0,
    "side_a": 1,
    "side_b": 2,
    "facet": 3,
    "hop": 4,
    "time_slice": 5,
}


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
            route = await self._resolver(user_id, "utility")
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
