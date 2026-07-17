"""Typed, hashed, replayable librarian planning artifact.

L1 deliberately models planning only.  Nothing in this module executes
retrieval, allocates final evidence, or calls a generation provider.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PLAN_VERSION = "query_plan.v1"
PLANNER_VERSION = "librarian_rule_planner.v1"
LLM_DECOMPOSER_PROMPT_HASH = (
    "sha256:7d87d502245ed2fb98c61a50bc26553f4d538c307137c66dbcc04267caa0ea1b"
)

PlanShape = Literal[
    "relationship",
    "comparison",
    "temporal",
    "enumerative_trace",
    "entity_bridge",
    "simple",
    "complex",
]
SubqueryRole = Literal["main", "side_a", "side_b", "facet", "hop", "time_slice"]
SubqueryTier = Literal["fast", "mongo", "graph"]

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PLANNER_RE = re.compile(
    r"^(?:rule:(?:relationship|comparison|temporal|enumerative_trace|"
    r"entity_bridge|simple)|llm:v1|fallback:simple)$"
)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def normalize_planner_query(value: str) -> str:
    """Casefold and punctuation-normalize a query for durable identity."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def plan_hash_for(
    normalized_query: str,
    corpus_doc_version: str,
    planner_version: str = PLANNER_VERSION,
) -> str:
    payload = "|".join(
        (
            normalized_query,
            corpus_doc_version,
            planner_version,
        )
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def plan_cache_key_for(
    *,
    normalized_query: str,
    corpus_id: str,
    corpus_doc_version: str,
    planner_prompt_hash: str = LLM_DECOMPOSER_PROMPT_HASH,
) -> str:
    payload = canonical_json_bytes(
        {
            "normalized_query": normalized_query,
            "corpus_id": corpus_id,
            "corpus_doc_version": corpus_doc_version,
            "planner_prompt_hash": planner_prompt_hash,
        }
    )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class LibrarianShortlistItemV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    title: str = ""
    summary: str = ""
    score: float = Field(ge=0.0)

    @field_validator("score")
    @classmethod
    def _finite_score(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("shortlist score must be finite")
        return round(float(value), 6)


class LibrarianSubqueryV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: SubqueryRole
    text: str = Field(min_length=1)
    target_doc_ids: tuple[str, ...] = ()
    seat_quota: int = Field(ge=1, le=64)
    tier: SubqueryTier
    rerank_cap: int = Field(ge=1, le=256)

    @field_validator("text")
    @classmethod
    def _non_blank_text(cls, value: str) -> str:
        if not str(value or "").strip():
            raise ValueError("subquery text must not be blank")
        # A simple plan is the byte-parity path: its main subquery preserves
        # the caller's exact question text. Deterministic non-simple planners
        # construct canonical display text before model validation.
        return value

    @field_validator("target_doc_ids")
    @classmethod
    def _stable_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(
            sorted({str(item).strip() for item in value if str(item).strip()})
        )
        if len(cleaned) > 8:
            raise ValueError("a subquery may target at most 8 documents")
        return cleaned


class LibrarianRefusalSignalsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    shortlist_empty: bool
    named_source_missing: bool


class LibrarianPlanCacheV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hit: bool
    key: str

    @field_validator("key")
    @classmethod
    def _hash_key(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("cache key must be a sha256 identity")
        return value


class QueryPlanV1(BaseModel):
    """The durable L1/L2 plan. Validation rejects hash or ordering drift."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_version: Literal["query_plan.v1"] = PLAN_VERSION
    plan_hash: str
    planner_version: str = PLANNER_VERSION
    planner_prompt_hash: str = LLM_DECOMPOSER_PROMPT_HASH
    normalized_query: str = Field(min_length=1)
    corpus_id: str = Field(min_length=1)
    corpus_doc_version: str
    planner: str
    shape: PlanShape
    shortlist: tuple[LibrarianShortlistItemV1, ...] = ()
    subqueries: tuple[LibrarianSubqueryV1, ...]
    refusal_signals: LibrarianRefusalSignalsV1
    cache: LibrarianPlanCacheV1

    @field_validator(
        "plan_hash",
        "planner_prompt_hash",
        "corpus_doc_version",
    )
    @classmethod
    def _sha_identity(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("field must be a sha256 identity")
        return value

    @field_validator("normalized_query")
    @classmethod
    def _already_normalized(cls, value: str) -> str:
        if normalize_planner_query(value) != value:
            raise ValueError("normalized_query is not canonically normalized")
        return value

    @field_validator("planner")
    @classmethod
    def _planner_contract(cls, value: str) -> str:
        if not _PLANNER_RE.fullmatch(value):
            raise ValueError("planner is outside the QueryPlanV1 registry")
        return value

    @field_validator("shortlist")
    @classmethod
    def _shortlist_contract(
        cls,
        value: tuple[LibrarianShortlistItemV1, ...],
    ) -> tuple[LibrarianShortlistItemV1, ...]:
        if len(value) > 8:
            raise ValueError("shortlist may contain at most 8 documents")
        identities = [(item.corpus_id, item.doc_id) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("shortlist document identities must be unique")
        expected = sorted(
            value,
            key=lambda item: (-item.score, item.corpus_id, item.doc_id),
        )
        if list(value) != expected:
            raise ValueError("shortlist must use deterministic score/id ordering")
        return value

    @field_validator("subqueries")
    @classmethod
    def _subquery_contract(
        cls,
        value: tuple[LibrarianSubqueryV1, ...],
    ) -> tuple[LibrarianSubqueryV1, ...]:
        if not 1 <= len(value) <= 4:
            raise ValueError("QueryPlanV1 requires 1..4 subqueries")
        return value

    @model_validator(mode="after")
    def _identity_contract(self) -> "QueryPlanV1":
        expected_hash = plan_hash_for(
            self.normalized_query,
            self.corpus_doc_version,
            self.planner_version,
        )
        if self.plan_hash != expected_hash:
            raise ValueError("plan_hash does not match the durable identity inputs")
        expected_cache_key = plan_cache_key_for(
            normalized_query=self.normalized_query,
            corpus_id=self.corpus_id,
            corpus_doc_version=self.corpus_doc_version,
            planner_prompt_hash=self.planner_prompt_hash,
        )
        if self.cache.key != expected_cache_key:
            raise ValueError("cache key does not match its durable identity inputs")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))

    def seat_assignment_bytes(self) -> bytes:
        return canonical_json_bytes(
            [
                {
                    "role": item.role,
                    "target_doc_ids": list(item.target_doc_ids),
                    "seat_quota": item.seat_quota,
                    "tier": item.tier,
                    "rerank_cap": item.rerank_cap,
                }
                for item in self.subqueries
            ]
        )


def replay_query_plan_v1(payload: bytes | str | dict) -> QueryPlanV1:
    """Load a durable artifact and re-run every schema/hash invariant."""

    if isinstance(payload, bytes):
        value = json.loads(payload.decode("utf-8"))
    elif isinstance(payload, str):
        value = json.loads(payload)
    else:
        value = payload
    return QueryPlanV1.model_validate(value)
