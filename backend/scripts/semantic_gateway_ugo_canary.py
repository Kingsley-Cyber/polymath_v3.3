#!/usr/bin/env python3
"""Run the T4.4 structured-gateway canary on accepted UGO evidence.

The driver discovers ``UGO_CORPUS`` from Mongo, selects ten evenly spaced
valid parents, and builds interim claim-grounded packets from accepted parent
text plus ``polymath.extract.v1`` entities. It never reads Ghost A summaries
into the packet and never writes canonical artifacts.

Provider credentials are resolved only through encrypted ``settings.api_keys``
and are never printed or written to the receipt. The receipt contains hashes,
counts, validator results, capability tiers, and noncanonical cache/DLQ IDs --
never packet text, model output, entities, or plaintext credentials. Tier 3 or
Tier 4 is an explicit adjudicated mode when every configured route is
provider-blocked for native JSON Schema; neither is ever relabeled as Tier 1.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence
from urllib.parse import urlsplit

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient


HERE = Path(__file__).resolve()
BACKEND_ROOT = HERE.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from config import get_settings  # noqa: E402
from models.hash_taxonomy import canonical_json_v1, namespace_hash  # noqa: E402
from models.semantic_validator import (  # noqa: E402
    ClaimScope,
    SemanticValidationContext,
    semantic_validate,
)
from services.semantic_gateway import (  # noqa: E402
    LiteLLMProxyTransport,
    MongoSemanticGatewayStore,
    SemanticGateway,
    SemanticGatewayConfig,
    SemanticGatewayResult,
    SemanticGatewayRoute,
    StructuredGenerationError,
    semantic_digest_schema_hash,
)
from services.settings import settings_service  # noqa: E402


DEFAULT_CORPUS_NAME = "UGO_CORPUS"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_API_BASE = "https://api.deepseek.com/v1"
DEFAULT_RUNTIME_VERSION = "deepseek-api.openai-compatible.2026-07-14"
DEFAULT_TOKENIZER_ID = "deepseek-v4-flash.provider-managed"
DEFAULT_PROVIDER_PRICE_CARDS = (
    BACKEND_ROOT / "registries" / "semantic_gateway_provider_prices.v1.json"
)
PROVIDER_PRICE_SCHEMA_VERSION = "polymath.semantic_gateway_provider_prices.v1"
DEFAULT_ROUTE_PARAMETER_CARDS = (
    BACKEND_ROOT / "registries" / "semantic_gateway_route_parameters.v1.json"
)
ROUTE_PARAMETER_SCHEMA_VERSION = "polymath.semantic_gateway_route_parameters.v1"
PACKET_SCHEMA_VERSION = "semantic_parent_packet.interim.v1"
CANONICAL_CENSUS_SCOPE_VERSION = "canonical_store_census.scope.v2"
POLYMATH_SHARED_QDRANT_COLLECTIONS = frozenset(
    {"polymath_children", "polymath_doc_summaries"}
)
POLYMATH_CORPUS_QDRANT_KINDS = ("graph", "hrag", "naive", "schemas")
CANONICAL_CENSUS_SCOPE_RECIPE = {
    "scope_version": CANONICAL_CENSUS_SCOPE_VERSION,
    "mongo_collections": ["semantic_artifacts"],
    "neo4j_counts": ["all_nodes", "all_relationships"],
    "qdrant_shared_collections": sorted(POLYMATH_SHARED_QDRANT_COLLECTIONS),
    "qdrant_per_corpus_name_rule": ("^corpus_[0-9a-f]{8}_(graph|hrag|naive|schemas)$"),
    "ambient_policy": "report_deltas_without_verdict_authority",
}
CANONICAL_CENSUS_SCOPE_RECIPE_HASH = namespace_hash(
    "scope",
    CANONICAL_CENSUS_SCOPE_RECIPE,
)

REQUIRED_PROVENANCE_FIELDS = frozenset(
    {
        "model_id",
        "runtime",
        "runtime_version",
        "tokenizer_id",
        "chat_template_hash",
        "schema_version",
        "schema_hash",
        "prompt_version",
        "prompt_hash",
        "repair_prompt_version",
        "repair_prompt_hash",
        "temperature",
        "input_hash",
        "output_hash",
        "capability_tier",
        "capability_detection",
        "attempts",
        "repair_attempted",
        "cache_key",
    }
)


class CanaryError(RuntimeError):
    """A safe, operator-facing canary contract failure."""


@dataclass(frozen=True)
class CanaryPacket:
    packet: dict[str, Any]
    context: SemanticValidationContext
    parent_id: str
    doc_id: str
    entity_count: int
    source_child_count: int


@dataclass(frozen=True)
class ProviderPriceCard:
    schema_version: str
    route_id: str
    model_id: str
    api_base: str
    price_unit_tokens: int
    uncached_input_usd: float
    output_usd: float
    source_checked_at: str
    source_url: str

    @property
    def receipt_source(self) -> str:
        return (
            f"provider-card:{self.schema_version}:{self.route_id}:"
            "published-list-uncached-input"
        )


@dataclass(frozen=True)
class RouteParameterCard:
    schema_version: str
    route_id: str
    model_id: str
    api_base: str
    capability_tier: str
    temperature: int
    thinking: str
    max_tokens: int
    timeout_seconds: float
    parameter_version: str
    runtime_version: str
    tokenizer_id: str
    diagnosis_evidence: str
    recanary_target_packets: int
    recanary_minimum_accepted: int
    recanary_max_cost_usd: float


def _load_route_parameter_card(
    path: Path,
    *,
    route_id: str,
    model_id: str,
    api_base: str,
) -> RouteParameterCard:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanaryError(
            f"cannot load route parameter cards: {type(exc).__name__}"
        ) from exc
    if payload.get("schema_version") != ROUTE_PARAMETER_SCHEMA_VERSION:
        raise CanaryError("route parameter-card schema version is invalid")
    routes = payload.get("routes")
    if not isinstance(routes, list):
        raise CanaryError("route parameter-card routes must be a list")
    matches = [row for row in routes if row.get("route_id") == route_id]
    if len(matches) != 1:
        raise CanaryError(f"route parameter {route_id!r} did not resolve once")
    row = matches[0]
    if row.get("model_id") != model_id or row.get("api_base") != api_base:
        raise CanaryError("route parameter card does not match requested route")
    if (
        row.get("capability_tier") != "tier3"
        or row.get("temperature") != 0
        or row.get("thinking") != "disabled"
    ):
        raise CanaryError("route parameter card changes a frozen gateway field")
    try:
        max_tokens = int(row["max_tokens"])
        timeout_seconds = float(row["timeout_seconds"])
        target = int(row["recanary_target_packets"])
        minimum = int(row["recanary_minimum_accepted"])
        max_cost = float(row["recanary_max_cost_usd"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CanaryError("route parameter card has invalid numeric fields") from exc
    if max_tokens <= 0 or timeout_seconds <= 0 or target != 10:
        raise CanaryError("route parameter card has invalid execution bounds")
    if not 0 < minimum <= target or max_cost <= 0:
        raise CanaryError("route parameter card has invalid acceptance bounds")
    return RouteParameterCard(
        schema_version=ROUTE_PARAMETER_SCHEMA_VERSION,
        route_id=route_id,
        model_id=model_id,
        api_base=api_base,
        capability_tier="tier3",
        temperature=0,
        thinking="disabled",
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        parameter_version=str(row.get("parameter_version") or ""),
        runtime_version=str(row.get("runtime_version") or ""),
        tokenizer_id=str(row.get("tokenizer_id") or ""),
        diagnosis_evidence=str(row.get("diagnosis_evidence") or ""),
        recanary_target_packets=target,
        recanary_minimum_accepted=minimum,
        recanary_max_cost_usd=max_cost,
    )


def _validate_route_parameter_args(
    args: argparse.Namespace,
    card: RouteParameterCard,
) -> None:
    expected = {
        "canary_tier": card.capability_tier,
        "max_tokens": card.max_tokens,
        "timeout_seconds": card.timeout_seconds,
        "runtime_version": card.runtime_version,
        "tokenizer_id": card.tokenizer_id,
        "count": card.recanary_target_packets,
    }
    mismatches = [
        field for field, value in expected.items() if getattr(args, field) != value
    ]
    if mismatches:
        raise CanaryError(
            "run arguments drift from versioned route parameters: "
            + ", ".join(sorted(mismatches))
        )
    if args.max_provider_cost_usd > card.recanary_max_cost_usd:
        raise CanaryError("run cost ceiling exceeds versioned route parameter cap")


def _load_provider_price_card(
    path: Path,
    *,
    route_id: str,
    model_id: str,
    api_base: str,
) -> ProviderPriceCard:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanaryError(
            f"cannot load provider price cards: {type(exc).__name__}"
        ) from exc
    if payload.get("schema_version") != PROVIDER_PRICE_SCHEMA_VERSION:
        raise CanaryError("provider price-card schema version is invalid")
    routes = payload.get("routes")
    if not isinstance(routes, list):
        raise CanaryError("provider price-card routes must be a list")
    matches = [row for row in routes if row.get("route_id") == route_id]
    if len(matches) != 1:
        raise CanaryError(f"provider price route {route_id!r} did not resolve once")
    row = matches[0]
    if row.get("model_id") != model_id or row.get("api_base") != api_base:
        raise CanaryError("provider price card does not match the requested route")
    if (
        row.get("currency") != "USD"
        or row.get("fallback_input_basis") != "uncached_input_conservative"
        or row.get("price_tier") != "published_list_price"
    ):
        raise CanaryError("provider price card billing basis is invalid")
    try:
        unit = int(row["price_unit_tokens"])
        input_price = float(row["uncached_input_usd"])
        output_price = float(row["output_usd"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CanaryError("provider price card has invalid numeric rates") from exc
    if unit <= 0 or input_price < 0 or output_price < 0:
        raise CanaryError("provider price card rates must be nonnegative")
    return ProviderPriceCard(
        schema_version=PROVIDER_PRICE_SCHEMA_VERSION,
        route_id=route_id,
        model_id=model_id,
        api_base=api_base,
        price_unit_tokens=unit,
        uncached_input_usd=input_price,
        output_usd=output_price,
        source_checked_at=str(payload.get("source_checked_at") or ""),
        source_url=str(row.get("source_url") or ""),
    )


def _apply_provider_price_fallback(
    rows: Sequence[dict[str, Any]],
    card: ProviderPriceCard,
) -> list[dict[str, Any]]:
    priced: list[dict[str, Any]] = []
    for row in rows:
        receipt = dict(row)
        if receipt.get("actual_cost_usd") is None:
            usage = receipt.get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            if not all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in (prompt_tokens, completion_tokens)
            ):
                priced.append(receipt)
                continue
            receipt["actual_cost_usd"] = round(
                (
                    prompt_tokens * card.uncached_input_usd
                    + completion_tokens * card.output_usd
                )
                / card.price_unit_tokens,
                12,
            )
            receipt["cost_source"] = card.receipt_source
        priced.append(receipt)
    return priced


class _MemoryStore:
    """Isolated store for the real-call downgrade comparison."""

    def __init__(self) -> None:
        self.successes: list[SemanticGatewayResult] = []

    async def load_success(self, _cache_key: str) -> None:
        return None

    async def save_success(self, result: SemanticGatewayResult) -> None:
        self.successes.append(result)

    async def save_dead_letter(self, **_kwargs) -> str:
        return "semantic-dlq:memory"


class _StaticInvalidTransport:
    """Deterministic two-attempt fault used only for the synthetic DLQ demo."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **_kwargs) -> str:
        self.calls += 1
        return "{}"

    async def complete_tool(self, **_kwargs) -> str:
        self.calls += 1
        return "{}"


class _FirstResponseParentFaultTransport:
    """Inject one semantic parent mismatch after a real provider response.

    The provider still performs attempt 1. Only its returned ``parent_id`` is
    changed before the gateway validates it, deterministically exercising the
    exact location-indexed repair path with a real attempt-2 provider call.
    """

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.calls = 0
        self.fault_injected = False

    def _inject_fault(self, raw: str) -> str:
        self.calls += 1
        if self.calls != 1:
            return raw
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return raw
        if not isinstance(parsed, dict):
            return raw
        parsed["parent_id"] = "fault-injected:wrong-parent"
        self.fault_injected = True
        return canonical_json_v1(parsed)

    async def complete(self, **kwargs) -> str:
        return self._inject_fault(await self._delegate.complete(**kwargs))

    async def complete_tool(self, **kwargs) -> str:
        return self._inject_fault(await self._delegate.complete_tool(**kwargs))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sample_evenly(rows: Sequence[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if count < 1:
        raise CanaryError("packet count must be positive")
    if len(rows) < count:
        raise CanaryError(f"need {count} eligible parents, found {len(rows)}")
    if count == 1:
        return [rows[0]]
    indexes = [(index * (len(rows) - 1)) // (count - 1) for index in range(count)]
    if len(set(indexes)) != count:
        raise CanaryError("even sampling produced duplicate parent indexes")
    return [rows[index] for index in indexes]


def _interim_claim_id(parent_id: str, source_hash: str | None) -> str:
    identity = namespace_hash(
        "logical-artifact",
        {
            "artifact_kind": "interim_parent_evidence_handle",
            "natural_keys": {
                "parent_id": parent_id,
                "source_hash": source_hash or "unavailable",
            },
        },
    )
    return "interim-claim:" + identity.split(":", 1)[1]


def _safe_entity(entity: Any) -> dict[str, Any] | None:
    if not isinstance(entity, dict):
        return None
    canonical_name = str(entity.get("canonical_name") or "").strip()
    entity_type = str(entity.get("entity_type") or "").strip()
    if not canonical_name or not entity_type:
        return None
    out: dict[str, Any] = {
        "canonical_name": canonical_name,
        "entity_type": entity_type,
    }
    for field in ("surface_form", "object_kind", "definitional_phrase"):
        value = str(entity.get(field) or "").strip()
        if value:
            out[field] = value
    aliases = entity.get("query_aliases")
    if isinstance(aliases, list):
        clean_aliases = sorted(
            {
                str(value).strip()
                for value in aliases
                if isinstance(value, str) and value.strip()
            }
        )
        if clean_aliases:
            out["query_aliases"] = clean_aliases[:8]
    confidence = entity.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        out["confidence"] = float(confidence)
    return out


def _packet_from_parent(
    *,
    corpus_id: str,
    corpus_name: str,
    parent: dict[str, Any],
    extraction_rows: Sequence[dict[str, Any]],
    max_entities: int,
) -> CanaryPacket:
    parent_id = str(parent.get("parent_id") or "").strip()
    doc_id = str(parent.get("doc_id") or "").strip()
    parent_text = str(parent.get("text") or "").strip()
    if not parent_id or not doc_id or not parent_text:
        raise CanaryError("eligible parent is missing parent_id, doc_id, or text")
    if parent.get("validation_status") != "valid":
        raise CanaryError(f"parent {parent_id} is not validation_status=valid")

    entities_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    accepted_children: set[str] = set()
    for extraction in extraction_rows:
        if extraction.get("status") != "ok":
            continue
        if extraction.get("schema_version") != "polymath.extract.v1":
            continue
        chunk_id = str(extraction.get("chunk_id") or "").strip()
        if chunk_id:
            accepted_children.add(chunk_id)
        for raw_entity in extraction.get("entities") or []:
            entity = _safe_entity(raw_entity)
            if entity is None:
                continue
            key = (
                entity["canonical_name"].casefold(),
                entity["entity_type"].casefold(),
                str(entity.get("surface_form") or "").casefold(),
            )
            entities_by_key.setdefault(key, entity)
    entities = [entities_by_key[key] for key in sorted(entities_by_key)][:max_entities]
    if not accepted_children:
        raise CanaryError(f"parent {parent_id} has no accepted extraction child")
    if not entities:
        raise CanaryError(f"parent {parent_id} has no accepted extraction entity")

    claim_id = _interim_claim_id(parent_id, parent.get("source_hash"))
    packet = {
        "packet_schema_version": PACKET_SCHEMA_VERSION,
        "corpus_id": corpus_id,
        "corpus_name": corpus_name,
        "doc_id": doc_id,
        "parent_id": parent_id,
        "parent_text": parent_text,
        "claims": [
            {
                "claim_id": claim_id,
                "parent_id": parent_id,
                "text": parent_text,
                "evidence_kind": "accepted_parent_text_interim",
            }
        ],
        "extraction_entities": entities,
        "evidence_contract": {
            "parent_validation_status": "valid",
            "extraction_status": "ok",
            "extraction_schema_version": "polymath.extract.v1",
            "source_child_ids": sorted(accepted_children),
            "claims_interim": True,
        },
    }
    context = SemanticValidationContext.from_owner_registries(
        parent_id=parent_id,
        claims=(ClaimScope(claim_id, parent_id),),
        claim_grounded_mode=True,
    )
    return CanaryPacket(
        packet=packet,
        context=context,
        parent_id=parent_id,
        doc_id=doc_id,
        entity_count=len(entities),
        source_child_count=len(accepted_children),
    )


async def _discover_packets(
    db: Any,
    *,
    corpus_name: str,
    count: int,
    max_entities: int,
) -> tuple[str, int, list[CanaryPacket]]:
    corpora = (
        await db["corpora"]
        .find(
            {"name": corpus_name, "status": {"$ne": "deleted"}},
            {"_id": 0, "corpus_id": 1, "name": 1},
        )
        .to_list(length=3)
    )
    if len(corpora) != 1:
        raise CanaryError(
            f"expected exactly one active corpus named {corpus_name!r}, found {len(corpora)}"
        )
    corpus_id = str(corpora[0].get("corpus_id") or "")
    document_count = await db["documents"].count_documents({"corpus_id": corpus_id})
    if document_count != 1:
        raise CanaryError(
            f"{corpus_name} must remain the one-document canary; found {document_count}"
        )
    parents = (
        await db["parent_chunks"]
        .find(
            {
                "corpus_id": corpus_id,
                "validation_status": "valid",
                "text": {"$exists": True, "$nin": [None, ""]},
                "child_ids.0": {"$exists": True},
            },
            {
                "_id": 0,
                "parent_id": 1,
                "doc_id": 1,
                "text": 1,
                "source_hash": 1,
                "child_ids": 1,
                "validation_status": 1,
            },
        )
        .sort("parent_id", 1)
        .to_list(length=None)
    )
    selected = _sample_evenly(parents, count)
    packets: list[CanaryPacket] = []
    for parent in selected:
        child_ids = [str(value) for value in parent.get("child_ids") or [] if value]
        extraction_rows = (
            await db["ghost_b_extractions"]
            .find(
                {
                    "corpus_id": corpus_id,
                    "chunk_id": {"$in": child_ids},
                    "status": "ok",
                    "schema_version": "polymath.extract.v1",
                },
                {
                    "_id": 0,
                    "chunk_id": 1,
                    "status": 1,
                    "schema_version": 1,
                    "entities": 1,
                },
            )
            .sort("chunk_id", 1)
            .to_list(length=None)
        )
        packets.append(
            _packet_from_parent(
                corpus_id=corpus_id,
                corpus_name=corpus_name,
                parent=parent,
                extraction_rows=extraction_rows,
                max_entities=max_entities,
            )
        )
    return corpus_id, len(parents), packets


def _gateway_config(
    args: argparse.Namespace, *, requested_tier: str
) -> SemanticGatewayConfig:
    chat_template_hash = namespace_hash(
        "recipe",
        {
            "name": "provider_managed_chat_template",
            "version": args.runtime_version,
            "params": {
                "model": args.model,
                "api_base": args.api_base,
                "thinking": "disabled",
            },
        },
    )
    return SemanticGatewayConfig(
        model_id=args.model,
        runtime="provider",
        runtime_version=args.runtime_version,
        tokenizer_id=args.tokenizer_id,
        chat_template_hash=chat_template_hash,
        requested_tier=requested_tier,
        max_tokens=args.max_tokens,
        timeout_seconds=args.timeout_seconds,
    )


def _digest_counts(result: SemanticGatewayResult) -> dict[str, int]:
    digest = result.digest
    return {
        "underlying_meanings": len(digest.underlying_meanings),
        "domain_proposals": len(digest.domain_proposals),
        "frame_proposals": len(digest.frame_proposals),
        "latent_concepts": len(digest.latent_concepts),
        "motif_proposals": len(digest.motif_proposals),
        "conditions": len(digest.conditions),
        "exceptions": len(digest.exceptions),
        "unresolved_interpretations": len(digest.unresolved_interpretations),
    }


def _provenance_complete(result: SemanticGatewayResult) -> bool:
    row = result.provenance.model_dump(mode="python")
    if set(row) != REQUIRED_PROVENANCE_FIELDS:
        return False
    for field in REQUIRED_PROVENANCE_FIELDS:
        value = row[field]
        if value is None or (isinstance(value, str) and not value):
            return False
    return True


def _validate_run_args(args: argparse.Namespace) -> None:
    if args.count != 10:
        raise CanaryError("T4.4 acceptance requires exactly 10 packets")
    if not 0 <= args.force_repair_index < args.count:
        raise CanaryError("force-repair-index must select one of the ten packets")
    if args.concurrency < 1:
        raise CanaryError("concurrency must be positive")
    if getattr(args, "max_provider_cost_usd", 2.0) <= 0:
        raise CanaryError("max-provider-cost-usd must be positive")
    if args.canary_tier in {"tier3", "tier4"} and not args.tier1_provider_blocked:
        raise CanaryError(
            "Tier3/Tier4 acceptance requires the explicit "
            "--tier1-provider-blocked ruling"
        )
    if args.canary_tier not in {"tier3", "tier4"} and args.tier1_provider_blocked:
        raise CanaryError("provider-blocked Path B can execute only as Tier3/Tier4")


def _is_polymath_qdrant_collection(collection_name: str) -> bool:
    if collection_name in POLYMATH_SHARED_QDRANT_COLLECTIONS:
        return True
    return bool(
        re.fullmatch(
            r"corpus_[0-9a-f]{8}_(?:graph|hrag|naive|schemas)",
            collection_name,
        )
    )


def _canonical_store_census_snapshot(
    *,
    mongo_count: int,
    qdrant_counts: dict[str, int],
    neo4j_nodes: int,
    neo4j_relationships: int,
) -> dict[str, Any]:
    protected_qdrant = {
        name: int(count)
        for name, count in sorted(qdrant_counts.items())
        if _is_polymath_qdrant_collection(name)
    }
    ambient_qdrant = {
        name: int(count)
        for name, count in sorted(qdrant_counts.items())
        if not _is_polymath_qdrant_collection(name)
    }
    return {
        "census_scope_version": CANONICAL_CENSUS_SCOPE_VERSION,
        "census_scope_recipe_hash": CANONICAL_CENSUS_SCOPE_RECIPE_HASH,
        "mongo_semantic_artifacts": int(mongo_count),
        "qdrant_collection_points": protected_qdrant,
        "qdrant_total_points": sum(protected_qdrant.values()),
        "neo4j_nodes": int(neo4j_nodes),
        "neo4j_relationships": int(neo4j_relationships),
        "ambient_qdrant_collection_points": ambient_qdrant,
        "ambient_qdrant_total_points": sum(ambient_qdrant.values()),
        "observed_qdrant_total_points": sum(qdrant_counts.values()),
    }


def _canonical_store_census_shape_valid(census: dict[str, Any]) -> bool:
    if (
        census.get("census_scope_version") != CANONICAL_CENSUS_SCOPE_VERSION
        or census.get("census_scope_recipe_hash") != CANONICAL_CENSUS_SCOPE_RECIPE_HASH
    ):
        return False
    protected = census.get("qdrant_collection_points")
    ambient = census.get("ambient_qdrant_collection_points")
    if not isinstance(protected, dict) or not isinstance(ambient, dict):
        return False
    if not all(
        isinstance(name, str)
        and _is_polymath_qdrant_collection(name)
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 0
        for name, count in protected.items()
    ):
        return False
    if not all(
        isinstance(name, str)
        and not _is_polymath_qdrant_collection(name)
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 0
        for name, count in ambient.items()
    ):
        return False
    protected_total = sum(protected.values())
    ambient_total = sum(ambient.values())
    if census.get("qdrant_total_points") != protected_total:
        return False
    if census.get("ambient_qdrant_total_points") != ambient_total:
        return False
    if census.get("observed_qdrant_total_points") != (protected_total + ambient_total):
        return False
    return all(
        isinstance(census.get(field), int)
        and not isinstance(census.get(field), bool)
        and int(census[field]) >= 0
        for field in (
            "mongo_semantic_artifacts",
            "neo4j_nodes",
            "neo4j_relationships",
        )
    )


def _canonical_store_census_comparison(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    scope_valid = _canonical_store_census_shape_valid(
        before
    ) and _canonical_store_census_shape_valid(after)
    protected_fields = (
        "mongo_semantic_artifacts",
        "qdrant_collection_points",
        "qdrant_total_points",
        "neo4j_nodes",
        "neo4j_relationships",
    )
    protected_unchanged = scope_valid and all(
        before.get(field) == after.get(field) for field in protected_fields
    )
    before_ambient = before.get("ambient_qdrant_collection_points")
    after_ambient = after.get("ambient_qdrant_collection_points")
    before_ambient = before_ambient if isinstance(before_ambient, dict) else {}
    after_ambient = after_ambient if isinstance(after_ambient, dict) else {}
    ambient_deltas: dict[str, dict[str, int]] = {}
    for name in sorted(set(before_ambient) | set(after_ambient)):
        old = int(before_ambient.get(name, 0) or 0)
        new = int(after_ambient.get(name, 0) or 0)
        if old != new:
            ambient_deltas[name] = {
                "before": old,
                "after": new,
                "delta": new - old,
            }
    return {
        "scope_version": CANONICAL_CENSUS_SCOPE_VERSION,
        "scope_recipe_hash": CANONICAL_CENSUS_SCOPE_RECIPE_HASH,
        "scope_valid": scope_valid,
        "protected_exactly_unchanged": protected_unchanged,
        "ambient_change_observed": bool(ambient_deltas),
        "ambient_qdrant_collection_deltas": ambient_deltas,
    }


def _canonical_store_census_receipt(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    comparison = _canonical_store_census_comparison(before, after)
    return {
        "before": before,
        "after": after,
        **comparison,
        "exactly_unchanged": comparison["protected_exactly_unchanged"],
    }


async def _canonical_store_census(
    *,
    db: Any,
    settings: Any,
) -> dict[str, Any]:
    """Capture exact canonical-store counts without reading semantic content."""

    mongo_count = await db["semantic_artifacts"].count_documents({})
    qdrant = AsyncQdrantClient(
        url=settings.QDRANT_URL,
        timeout=settings.QDRANT_TIMEOUT_SECONDS,
    )
    try:
        collection_rows = (await qdrant.get_collections()).collections
        qdrant_counts: dict[str, int] = {}
        for collection in sorted(collection_rows, key=lambda row: row.name):
            info = await qdrant.get_collection(collection.name)
            qdrant_counts[collection.name] = int(info.points_count or 0)
    finally:
        await qdrant.close()

    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        async with driver.session() as session:
            node_row = await (
                await session.run("MATCH (node) RETURN count(node) AS count")
            ).single()
            relationship_row = await (
                await session.run(
                    "MATCH ()-[relationship]->() RETURN count(relationship) AS count"
                )
            ).single()
    finally:
        await driver.close()
    return _canonical_store_census_snapshot(
        mongo_count=int(mongo_count),
        qdrant_counts=qdrant_counts,
        neo4j_nodes=int(node_row["count"] if node_row else 0),
        neo4j_relationships=int(relationship_row["count"] if relationship_row else 0),
    )


def _result_receipt(
    item: CanaryPacket,
    result: SemanticGatewayResult,
    *,
    fault_injected: bool,
    provider_telemetry: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    semantic_errors = semantic_validate(result.digest, item.context)
    cost_values = [row.get("actual_cost_usd") for row in provider_telemetry]
    cost_complete = bool(cost_values) and all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in cost_values
    )
    usage = {
        field: sum(
            int((row.get("usage") or {}).get(field) or 0) for row in provider_telemetry
        )
        for field in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    return {
        "status": "accepted",
        "parent_id": item.parent_id,
        "doc_id": item.doc_id,
        "entity_count": item.entity_count,
        "source_child_count": item.source_child_count,
        "input_hash": result.provenance.input_hash,
        "output_hash": result.provenance.output_hash,
        "cache_key": result.provenance.cache_key,
        "cache_hit": result.cache_hit,
        "capability_tier": result.provenance.capability_tier,
        "capability_detection": result.provenance.capability_detection,
        "attempts": result.provenance.attempts,
        "repair_attempted": result.provenance.repair_attempted,
        "repair_fault_injected": fault_injected,
        "semantic_validation_errors": semantic_errors,
        "provenance_complete": _provenance_complete(result),
        "digest_counts": _digest_counts(result),
        "provider_calls": len(provider_telemetry),
        "usage": usage,
        "actual_cost_usd": (
            sum(float(value) for value in cost_values) if cost_complete else None
        ),
        "cost_complete": cost_complete,
        "call_costs_usd": cost_values,
        "call_cost_sources": [row.get("cost_source") for row in provider_telemetry],
    }


def _failure_receipt(
    item: CanaryPacket,
    exc: StructuredGenerationError,
    *,
    fault_injected: bool,
    provider_telemetry: Sequence[dict[str, Any]],
    requested_tier: str,
) -> dict[str, Any]:
    cost_values = [row.get("actual_cost_usd") for row in provider_telemetry]
    cost_complete = bool(cost_values) and all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in cost_values
    )
    usage = {
        field: sum(
            int((row.get("usage") or {}).get(field) or 0) for row in provider_telemetry
        )
        for field in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    return {
        "status": "dead_letter",
        "parent_id": item.parent_id,
        "doc_id": item.doc_id,
        "entity_count": item.entity_count,
        "source_child_count": item.source_child_count,
        "capability_tier": requested_tier,
        "attempts": exc.attempts,
        "repair_attempted": exc.attempts == 2,
        "repair_fault_injected": fault_injected,
        "dead_letter_id": exc.dead_letter_id,
        "validation_errors": list(exc.errors),
        "provider_calls": len(provider_telemetry),
        "usage": usage,
        "actual_cost_usd": (
            sum(float(value) for value in cost_values) if cost_complete else None
        ),
        "cost_complete": cost_complete,
        "call_costs_usd": cost_values,
        "call_cost_sources": [row.get("cost_source") for row in provider_telemetry],
        "canonical_write": False,
    }


async def _run_packets(
    *,
    packets: Sequence[CanaryPacket],
    config: SemanticGatewayConfig,
    route: SemanticGatewayRoute,
    store: MongoSemanticGatewayStore,
    concurrency: int,
    force_repair_index: int,
    max_provider_cost_usd: float,
    provider_price_card: ProviderPriceCard,
) -> list[dict[str, Any]]:
    if concurrency != 1:
        raise CanaryError("cost-ceilinged provider canary requires concurrency=1")
    receipts: list[dict[str, Any]] = []
    running_cost = 0.0
    for index, item in enumerate(packets):
        base_transport = LiteLLMProxyTransport()
        if index == force_repair_index:
            transport: Any = _FirstResponseParentFaultTransport(base_transport)
        else:
            transport = base_transport
        try:
            result = await SemanticGateway(transport=transport, store=store).generate(
                packet=item.packet,
                context=item.context,
                config=config,
                route=route,
            )
            fault_injected = bool(getattr(transport, "fault_injected", False))
            receipt = _result_receipt(
                item,
                result,
                fault_injected=fault_injected,
                provider_telemetry=_apply_provider_price_fallback(
                    base_transport.call_telemetry,
                    provider_price_card,
                ),
            )
        except StructuredGenerationError as exc:
            fault_injected = bool(getattr(transport, "fault_injected", False))
            receipt = _failure_receipt(
                item,
                exc,
                fault_injected=fault_injected,
                provider_telemetry=_apply_provider_price_fallback(
                    base_transport.call_telemetry,
                    provider_price_card,
                ),
                requested_tier=config.requested_tier,
            )
        receipts.append(receipt)
        cost = receipt.get("actual_cost_usd")
        if not receipt.get("cost_complete") or not isinstance(cost, (int, float)):
            raise CanaryError(
                f"packet index {index} returned incomplete provider cost telemetry"
            )
        running_cost += float(cost)
        if running_cost > max_provider_cost_usd:
            raise CanaryError(
                f"provider cost ceiling exceeded after packet index {index}"
            )
    return receipts


async def _verify_cache_rows(db: Any, receipts: Sequence[dict[str, Any]]) -> int:
    keys = [row["cache_key"] for row in receipts]
    rows = (
        await db["semantic_digest_cache"]
        .find(
            {"_id": {"$in": keys}},
            {"_id": 1, "status": 1, "canonical_write": 1, "provenance": 1},
        )
        .to_list(length=len(keys))
    )
    if len(rows) != len(keys):
        raise CanaryError(f"expected {len(keys)} cache rows, found {len(rows)}")
    for row in rows:
        if (
            row.get("status") != "accepted_cache"
            or row.get("canonical_write") is not False
        ):
            raise CanaryError(
                "gateway success escaped accepted noncanonical cache status"
            )
        provenance = row.get("provenance") or {}
        if set(provenance) != REQUIRED_PROVENANCE_FIELDS:
            raise CanaryError("persisted provenance field set is incomplete")
    return len(rows)


async def _synthetic_dead_letter(
    *,
    db: Any,
    item: CanaryPacket,
    base_config: SemanticGatewayConfig,
) -> dict[str, Any]:
    config_values = base_config.model_dump(mode="python")
    config_values["runtime_version"] = base_config.runtime_version + ".synthetic-dlq"
    config = SemanticGatewayConfig(**config_values)
    packet = dict(item.packet)
    packet["probe_kind"] = "synthetic_dead_letter"
    transport = _StaticInvalidTransport()
    try:
        await SemanticGateway(
            transport=transport,
            store=MongoSemanticGatewayStore(db),
        ).generate(packet=packet, context=item.context, config=config)
    except StructuredGenerationError as exc:
        if exc.attempts != 2 or not exc.dead_letter_id:
            raise CanaryError(
                "synthetic DLQ did not exhaust exactly two attempts"
            ) from exc
        row = await db["semantic_digest_dead_letters"].find_one(
            {"_id": exc.dead_letter_id},
            {
                "_id": 1,
                "status": 1,
                "canonical_write": 1,
                "attempts": 1,
                "validation_errors": 1,
                "raw_output_hashes": 1,
            },
        )
        if not row:
            raise CanaryError("synthetic DLQ receipt was not persisted") from exc
        if (
            row.get("status") != "dead_letter"
            or row.get("canonical_write") is not False
        ):
            raise CanaryError(
                "synthetic failure escaped noncanonical DLQ status"
            ) from exc
        return {
            "dead_letter_id": exc.dead_letter_id,
            "attempts": row.get("attempts"),
            "transport_calls": transport.calls,
            "validation_error_count": len(row.get("validation_errors") or []),
            "raw_output_hash_count": len(row.get("raw_output_hashes") or []),
            "canonical_write": row.get("canonical_write"),
        }
    raise CanaryError("synthetic invalid output unexpectedly produced a success")


async def _downgrade_probe(
    *,
    item: CanaryPacket,
    args: argparse.Namespace,
    route: SemanticGatewayRoute,
) -> dict[str, Any]:
    values = vars(args).copy()
    values["runtime_version"] = args.runtime_version + ".downgrade"
    probe_args = argparse.Namespace(**values)
    tier1 = await SemanticGateway(
        transport=LiteLLMProxyTransport(),
        store=_MemoryStore(),
    ).generate(
        packet=item.packet,
        context=item.context,
        config=_gateway_config(probe_args, requested_tier="tier1"),
        route=route,
    )
    tier4 = await SemanticGateway(
        transport=LiteLLMProxyTransport(),
        store=_MemoryStore(),
    ).generate(
        packet=item.packet,
        context=item.context,
        config=_gateway_config(probe_args, requested_tier="tier4"),
        route=route,
    )
    tier1_errors = semantic_validate(tier1.digest, item.context)
    tier4_errors = semantic_validate(tier4.digest, item.context)
    tier1_keys = sorted(tier1.digest.model_dump(mode="python"))
    tier4_keys = sorted(tier4.digest.model_dump(mode="python"))
    schema_hash = semantic_digest_schema_hash()
    schema_identical = (
        not tier1_errors
        and not tier4_errors
        and tier1_keys == tier4_keys
        and tier1.provenance.schema_hash == schema_hash
        and tier4.provenance.schema_hash == schema_hash
    )
    if not schema_identical:
        raise CanaryError(
            "Tier1/Tier4 downgrade outputs did not share one valid schema"
        )
    if tier1.provenance.capability_tier != "tier1":
        raise CanaryError("downgrade Tier1 call was not tagged tier1")
    if tier4.provenance.capability_tier != "tier4":
        raise CanaryError("downgrade Tier4 call was not explicitly tagged tier4")
    return {
        "schema_identical": True,
        "schema_hash": schema_hash,
        "root_fields": tier1_keys,
        "tier1": {
            "capability_tier": tier1.provenance.capability_tier,
            "attempts": tier1.provenance.attempts,
            "semantic_validation_errors": tier1_errors,
            "output_hash": tier1.provenance.output_hash,
        },
        "tier4": {
            "capability_tier": tier4.provenance.capability_tier,
            "attempts": tier4.provenance.attempts,
            "semantic_validation_errors": tier4_errors,
            "output_hash": tier4.provenance.output_hash,
        },
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    _validate_run_args(args)

    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = client.get_default_database()
        except Exception:
            db = client[settings.MONGODB_DATABASE]
        active_batches = await db["ingest_batches"].count_documents(
            {"status": {"$in": ["queued", "running"]}}
        )
        if active_batches:
            raise CanaryError(
                f"refusing provider canary while {active_batches} ingest batch(es) are active"
            )
        canonical_before = await _canonical_store_census(db=db, settings=settings)
        corpus_id, eligible_count, packets = await _discover_packets(
            db,
            corpus_name=args.corpus_name,
            count=args.count,
            max_entities=args.max_entities,
        )
        settings_service.attach(db)
        route_parameter_card = _load_route_parameter_card(
            args.route_parameter_cards,
            route_id=args.provider_price_route_id,
            model_id=args.model,
            api_base=args.api_base,
        )
        _validate_route_parameter_args(args, route_parameter_card)
        api_key = await settings_service.get_plaintext_key_any_user(
            args.credential_provider
        )
        if not api_key:
            raise CanaryError(
                f"encrypted {args.credential_provider} credential is not configured"
            )
        route = SemanticGatewayRoute(api_base=args.api_base, api_key=api_key)
        config = _gateway_config(args, requested_tier=args.canary_tier)
        store = MongoSemanticGatewayStore(db)
        provider_price_card = _load_provider_price_card(
            args.provider_price_cards,
            route_id=args.provider_price_route_id,
            model_id=args.model,
            api_base=args.api_base,
        )

        receipts = await _run_packets(
            packets=packets,
            config=config,
            route=route,
            store=store,
            concurrency=args.concurrency,
            force_repair_index=args.force_repair_index,
            max_provider_cost_usd=args.max_provider_cost_usd,
            provider_price_card=provider_price_card,
        )
        accepted_receipts = [row for row in receipts if row["status"] == "accepted"]
        failed_receipts = [row for row in receipts if row["status"] != "accepted"]
        fresh_calls = not any(row.get("cache_hit") for row in accepted_receipts)
        requested_tier_only = all(
            row["capability_tier"] == args.canary_tier for row in receipts
        )
        semantic_replay_green = not any(
            row.get("semantic_validation_errors") for row in accepted_receipts
        )
        provenance_complete = all(
            bool(row.get("provenance_complete")) for row in accepted_receipts
        )
        repair_count = sum(bool(row["repair_attempted"]) for row in receipts)
        forced_repairs = sum(bool(row["repair_fault_injected"]) for row in receipts)
        repair_contract_green = repair_count >= 1
        persisted_cache_rows = await _verify_cache_rows(db, accepted_receipts)
        dead_letter = await _synthetic_dead_letter(
            db=db,
            item=packets[0],
            base_config=config,
        )
        if args.tier1_provider_blocked:
            downgrade = {
                "status": "provider_blocked",
                "reason": "0_of_5_configured_routes_accepted_native_json_schema",
                "tier1_acceptance_open": True,
                "schema_identical": None,
                "schema_hash": semantic_digest_schema_hash(),
                "retest_at": "CP9_preflight",
                "verified_digest_path_under_test": args.canary_tier,
            }
        else:
            downgrade = await _downgrade_probe(item=packets[-1], args=args, route=route)
        canonical_after = await _canonical_store_census(db=db, settings=settings)
        canonical_census_receipt = _canonical_store_census_receipt(
            canonical_before,
            canonical_after,
        )
        if canonical_census_receipt["protected_exactly_unchanged"] is not True:
            raise CanaryError(
                "protected canonical Mongo/Qdrant/Neo4j counts changed "
                "during canary or census_scope.v2 was invalid"
            )

        actual_cost = sum(float(row["actual_cost_usd"]) for row in receipts)
        cost_complete = all(bool(row["cost_complete"]) for row in receipts)
        within_cost_ceiling = actual_cost <= args.max_provider_cost_usd
        acceptance_bar_met = (
            len(accepted_receipts) >= route_parameter_card.recanary_minimum_accepted
        )
        all_green = (
            len(receipts) == 10
            and acceptance_bar_met
            and fresh_calls
            and requested_tier_only
            and semantic_replay_green
            and provenance_complete
            and repair_contract_green
            and persisted_cache_rows == len(accepted_receipts)
            and cost_complete
            and within_cost_ceiling
        )

        return {
            "schema_version": "polymath.semantic_gateway_ugo_canary.v1",
            "generated_at": _utc_now(),
            "corpus": {
                "name": args.corpus_name,
                "corpus_id": corpus_id,
                "document_count": 1,
                "eligible_parent_count": eligible_count,
                "sample_strategy": "ten_evenly_spaced_by_parent_id",
            },
            "provider_contract": {
                "model_id": args.model,
                "runtime": "provider",
                "runtime_version": args.runtime_version,
                "tokenizer_id": args.tokenizer_id,
                "api_base_origin": urlsplit(args.api_base).netloc,
                "credential_source": (
                    "encrypted settings.api_keys." + args.credential_provider
                ),
                "provider_price_card": {
                    "schema_version": provider_price_card.schema_version,
                    "route_id": provider_price_card.route_id,
                    "source_checked_at": provider_price_card.source_checked_at,
                    "source_url": provider_price_card.source_url,
                    "fallback_cost_source": provider_price_card.receipt_source,
                    "uncached_input_usd_per_million": (
                        provider_price_card.uncached_input_usd
                    ),
                    "output_usd_per_million": provider_price_card.output_usd,
                },
                "route_parameter_card": {
                    "schema_version": route_parameter_card.schema_version,
                    "route_id": route_parameter_card.route_id,
                    "parameter_version": route_parameter_card.parameter_version,
                    "diagnosis_evidence": route_parameter_card.diagnosis_evidence,
                    "max_tokens": route_parameter_card.max_tokens,
                    "timeout_seconds": route_parameter_card.timeout_seconds,
                    "temperature": route_parameter_card.temperature,
                    "thinking": route_parameter_card.thinking,
                    "recanary_target_packets": (
                        route_parameter_card.recanary_target_packets
                    ),
                    "recanary_minimum_accepted": (
                        route_parameter_card.recanary_minimum_accepted
                    ),
                    "recanary_max_cost_usd": (
                        route_parameter_card.recanary_max_cost_usd
                    ),
                },
            },
            "packet_canary": {
                "capability_tier": args.canary_tier,
                "target_packet_count": 10,
                "packet_count": len(receipts),
                "accepted_count": len(accepted_receipts),
                "failed_count": len(failed_receipts),
                "minimum_accepted": (route_parameter_card.recanary_minimum_accepted),
                "dead_letter_ids": [
                    row.get("dead_letter_id")
                    for row in failed_receipts
                    if row.get("dead_letter_id")
                ],
                "repair_count": repair_count,
                "forced_semantic_repair_count": forced_repairs,
                "provenance_complete_count": sum(
                    bool(row.get("provenance_complete")) for row in receipts
                ),
                "persisted_noncanonical_cache_rows": persisted_cache_rows,
                "receipts": receipts,
            },
            "cost_accounting": {
                "actual_cost_usd": actual_cost,
                "max_provider_cost_usd": args.max_provider_cost_usd,
                "cost_complete": cost_complete,
                "within_ceiling": within_cost_ceiling,
                "per_digest_actual_cost_usd": [
                    {
                        "parent_id": row["parent_id"],
                        "status": row["status"],
                        "provider_calls": row["provider_calls"],
                        "actual_cost_usd": row["actual_cost_usd"],
                    }
                    for row in receipts
                ],
            },
            "synthetic_dead_letter": dead_letter,
            "ladder_downgrade": downgrade,
            "canonical_store_census": canonical_census_receipt,
            "security": {
                "credentials_from_encrypted_settings": True,
                "plaintext_credentials_in_receipt": False,
                "packet_text_in_receipt": False,
                "raw_provider_output_in_receipt": False,
                "canonical_store_counts_exactly_unchanged": True,
                "ghost_a_changes": 0,
            },
            "acceptance": {
                "ten_tier1_packets": (
                    args.canary_tier == "tier1" and len(accepted_receipts) == 10
                ),
                "ten_tier3_packets": (
                    args.canary_tier == "tier3" and len(accepted_receipts) == 10
                ),
                "ten_tier4_packets": (
                    args.canary_tier == "tier4" and len(accepted_receipts) == 10
                ),
                "tier1_provider_blocked": args.tier1_provider_blocked,
                "tier1_acceptance_open": args.tier1_provider_blocked,
                "adjudicated_provider_blocked_path_complete": (
                    args.tier1_provider_blocked
                    and args.canary_tier in {"tier3", "tier4"}
                    and acceptance_bar_met
                ),
                "fresh_provider_calls": fresh_calls,
                "requested_tier_only": requested_tier_only,
                "zero_final_structural_failures": not failed_receipts,
                "zero_final_semantic_failures": semantic_replay_green,
                "provenance_complete": provenance_complete,
                "repair_exercised": repair_count >= 1,
                "preregistered_acceptance_bar_met": acceptance_bar_met,
                "synthetic_dead_letter_persisted": bool(
                    dead_letter.get("dead_letter_id")
                ),
                "tier1_tier4_schema_identical": downgrade["schema_identical"],
                "verified_digest_path": all_green,
                "all_green": all_green,
            },
        }
    finally:
        client.close()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-name", default=DEFAULT_CORPUS_NAME)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--max-entities", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--force-repair-index", type=int, default=0)
    parser.add_argument(
        "--canary-tier", choices=("tier1", "tier3", "tier4"), default="tier1"
    )
    parser.add_argument("--tier1-provider-blocked", action="store_true")
    parser.add_argument("--credential-provider", default="deepseek")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--runtime-version", default=DEFAULT_RUNTIME_VERSION)
    parser.add_argument("--tokenizer-id", default=DEFAULT_TOKENIZER_ID)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--max-provider-cost-usd", type=float, default=2.0)
    parser.add_argument(
        "--provider-price-cards",
        type=Path,
        default=DEFAULT_PROVIDER_PRICE_CARDS,
    )
    parser.add_argument(
        "--provider-price-route-id",
        default="longcat-api__longcat-2.0",
    )
    parser.add_argument(
        "--route-parameter-cards",
        type=Path,
        default=DEFAULT_ROUTE_PARAMETER_CARDS,
    )
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = asyncio.run(run(args))
        _write_report(args.out, report)
    except Exception as exc:
        print(f"T4.4 CANARY FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    canary = report["packet_canary"]
    verdict = "GREEN" if report["acceptance"]["all_green"] else "FAILED"
    print(
        f"CP9 PREFLIGHT CANARY {verdict} "
        f"tier={canary['capability_tier']} packets={canary['packet_count']} "
        f"accepted={canary['accepted_count']} failed={canary['failed_count']} "
        f"repairs={canary['repair_count']} "
        f"cache_rows={canary['persisted_noncanonical_cache_rows']} "
        f"actual_cost_usd={report['cost_accounting']['actual_cost_usd']:.8f} "
        f"dlq={report['synthetic_dead_letter']['dead_letter_id']} "
        f"tier1_provider_blocked={report['acceptance']['tier1_provider_blocked']}"
    )
    print(f"receipt={args.out}")
    return 0 if report["acceptance"]["all_green"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
