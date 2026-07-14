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
PACKET_SCHEMA_VERSION = "semantic_parent_packet.interim.v1"

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
    if args.canary_tier in {"tier3", "tier4"} and not args.tier1_provider_blocked:
        raise CanaryError(
            "Tier3/Tier4 acceptance requires the explicit "
            "--tier1-provider-blocked ruling"
        )
    if args.canary_tier not in {"tier3", "tier4"} and args.tier1_provider_blocked:
        raise CanaryError("provider-blocked Path B can execute only as Tier3/Tier4")


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
    return {
        "mongo_semantic_artifacts": int(mongo_count),
        "qdrant_collection_points": qdrant_counts,
        "qdrant_total_points": sum(qdrant_counts.values()),
        "neo4j_nodes": int(node_row["count"] if node_row else 0),
        "neo4j_relationships": int(
            relationship_row["count"] if relationship_row else 0
        ),
    }


def _result_receipt(
    item: CanaryPacket,
    result: SemanticGatewayResult,
    *,
    fault_injected: bool,
) -> dict[str, Any]:
    semantic_errors = semantic_validate(result.digest, item.context)
    return {
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
    }


async def _run_packets(
    *,
    packets: Sequence[CanaryPacket],
    config: SemanticGatewayConfig,
    route: SemanticGatewayRoute,
    store: MongoSemanticGatewayStore,
    concurrency: int,
    force_repair_index: int,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(concurrency)
    base_transport = LiteLLMProxyTransport()

    async def run_one(index: int, item: CanaryPacket) -> dict[str, Any]:
        async with semaphore:
            if index == force_repair_index:
                transport: Any = _FirstResponseParentFaultTransport(base_transport)
            else:
                transport = base_transport
            result = await SemanticGateway(transport=transport, store=store).generate(
                packet=item.packet,
                context=item.context,
                config=config,
                route=route,
            )
            fault_injected = bool(getattr(transport, "fault_injected", False))
            return _result_receipt(
                item,
                result,
                fault_injected=fault_injected,
            )

    return list(
        await asyncio.gather(
            *(run_one(index, item) for index, item in enumerate(packets))
        )
    )


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

        receipts = await _run_packets(
            packets=packets,
            config=config,
            route=route,
            store=store,
            concurrency=args.concurrency,
            force_repair_index=args.force_repair_index,
        )
        if any(row["cache_hit"] for row in receipts):
            raise CanaryError(
                "T4.4 requires fresh real calls; one or more packets hit cache"
            )
        if any(row["capability_tier"] != args.canary_tier for row in receipts):
            raise CanaryError(
                "one or more UGO packets did not execute through the requested tier"
            )
        if any(row["semantic_validation_errors"] for row in receipts):
            raise CanaryError("one or more accepted UGO outputs failed semantic replay")
        if any(not row["provenance_complete"] for row in receipts):
            raise CanaryError("one or more UGO provenance rows is incomplete")
        repair_count = sum(bool(row["repair_attempted"]) for row in receipts)
        forced_repairs = sum(bool(row["repair_fault_injected"]) for row in receipts)
        if repair_count < 1 or forced_repairs != 1:
            raise CanaryError(
                "targeted repair was not exercised by the single fault probe"
            )
        persisted_cache_rows = await _verify_cache_rows(db, receipts)
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
        if canonical_after != canonical_before:
            raise CanaryError(
                "canonical Mongo/Qdrant/Neo4j counts changed during canary"
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
            },
            "packet_canary": {
                "capability_tier": args.canary_tier,
                "packet_count": len(receipts),
                "structural_failures": 0,
                "semantic_failures": 0,
                "repair_count": repair_count,
                "forced_semantic_repair_count": forced_repairs,
                "provenance_complete_count": sum(
                    bool(row["provenance_complete"]) for row in receipts
                ),
                "persisted_noncanonical_cache_rows": persisted_cache_rows,
                "receipts": receipts,
            },
            "synthetic_dead_letter": dead_letter,
            "ladder_downgrade": downgrade,
            "canonical_store_census": {
                "before": canonical_before,
                "after": canonical_after,
                "exactly_unchanged": True,
            },
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
                    args.canary_tier == "tier1" and len(receipts) == 10
                ),
                "ten_tier3_packets": (
                    args.canary_tier == "tier3" and len(receipts) == 10
                ),
                "ten_tier4_packets": (
                    args.canary_tier == "tier4" and len(receipts) == 10
                ),
                "tier1_provider_blocked": args.tier1_provider_blocked,
                "tier1_acceptance_open": args.tier1_provider_blocked,
                "adjudicated_provider_blocked_path_complete": (
                    args.tier1_provider_blocked
                    and args.canary_tier in {"tier3", "tier4"}
                    and len(receipts) == 10
                ),
                "zero_final_structural_failures": True,
                "zero_final_semantic_failures": True,
                "repair_exercised": repair_count >= 1,
                "synthetic_dead_letter_persisted": bool(
                    dead_letter.get("dead_letter_id")
                ),
                "tier1_tier4_schema_identical": downgrade["schema_identical"],
                "all_green": True,
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
    print(
        "T4.4 CANARY GREEN "
        f"tier={canary['capability_tier']} packets={canary['packet_count']} "
        f"repairs={canary['repair_count']} "
        f"cache_rows={canary['persisted_noncanonical_cache_rows']} "
        f"dlq={report['synthetic_dead_letter']['dead_letter_id']} "
        f"tier1_provider_blocked={report['acceptance']['tier1_provider_blocked']}"
    )
    print(f"receipt={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
