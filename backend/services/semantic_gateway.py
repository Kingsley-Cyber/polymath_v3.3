"""Structured-output gateway for claim-grounded ``SemanticDigestV1`` calls.

This service owns capability routing, structural + semantic validation, the
single targeted repair, deterministic provenance/cache identity, and
noncanonical dead-letter isolation. It does not project or activate artifacts
and does not modify the legacy Ghost A summary path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from models.hash_taxonomy import canonical_json_v1, canonicalize, namespace_hash
from models.semantic_digest import SemanticDigestV1
from models.semantic_validator import SemanticValidationContext, semantic_validate
from models.structured_output_capabilities import (
    load_structured_output_capabilities,
)


LEGACY_SYSTEM_PROMPT_V5 = (
    "Generate a SemanticDigestV1 from the supplied evidence. Use only claim "
    "IDs present in the input. Do not invent registry IDs. Use empty arrays "
    "when no supported result exists. Treat latent concepts and motifs as "
    "proposals, not facts. Separate source-backed conclusions from proposed "
    "interpretations. Never mark your own proposal as validated. Return only "
    "a JSON object. Return the SemanticDigestV1 object itself at the top "
    "level. Do not wrap it under digest or add other top-level fields."
)
SYSTEM_PROMPT_V6 = (
    "Generate a SemanticDigestV1 from the supplied evidence. Use only claim IDs "
    "present in the input. Do not invent registry IDs. Use empty arrays when no "
    "supported result exists. Treat latent concepts and motifs as proposals, not "
    "facts. Separate source-backed conclusions from proposed interpretations. "
    "Never mark your own proposal as validated. Every domain, frame, latent-"
    "concept, or motif proposal must have a non-empty supporting_claim_ids array "
    "containing only claim IDs present in the input; otherwise omit that "
    "proposal. Propose a motif only when every frame_id in its frame_sequence "
    "also appears in frame_proposals, and use at least two frames. Every "
    "latent_concepts item must contain exactly these five fields: preferred_label "
    "as a string, definition as a string, assignment_state as candidate, "
    "corroborated, unresolved, or rejected, supporting_claim_ids as an array of "
    "input claim IDs, and aliases as an array of strings. Fewer proposals are "
    "correct when support is uncertain; empty proposal arrays are always lawful. "
    "Return only a JSON object. Return the SemanticDigestV1 object itself at the "
    "top level. Do not wrap it under digest or add other top-level fields."
)
PROMPT_VERSION = "parent-digest.v6"
REPAIR_PROMPT_VERSION = "parent-digest-repair.v3"
LEGACY_REPAIR_INSTRUCTION_V2 = (
    "Correct only the validation failures. Return every required array; use "
    "an empty array when no supported result exists. Return the "
    "SemanticDigestV1 object itself at the top level. Do not wrap it under "
    "digest or add other top-level fields."
)
REPAIR_INSTRUCTION_V3 = (
    "Correct only the validation failures. When a validation error names an "
    "unsupported proposal or an invalid proposal reference, remove that entire "
    "optional proposal. Never preserve a failing proposal by inventing, "
    "substituting, or reassigning claims, frames, registry IDs, aliases, "
    "definitions, or justification. Preserve valid content. Fewer proposals are "
    "correct; empty proposal arrays are always lawful. Return every required "
    "array, using an empty array when no supported result exists. Return the "
    "SemanticDigestV1 object itself at the top level. Do not wrap it under digest "
    "or add other top-level fields."
)
TIER3_REPAIR_SUFFIX = (
    " Resubmit the correction through the SAME forced "
    "submit_semantic_digest tool. Put all 12 SemanticDigestV1 fields directly "
    "at the tool-arguments root. Do not nest them under parameters or any "
    "other wrapper."
)
SYSTEM_PROMPTS = {
    "parent-digest.v5": LEGACY_SYSTEM_PROMPT_V5,
    "parent-digest.v6": SYSTEM_PROMPT_V6,
}
REPAIR_INSTRUCTIONS = {
    "parent-digest-repair.v2": LEGACY_REPAIR_INSTRUCTION_V2,
    "parent-digest-repair.v3": REPAIR_INSTRUCTION_V3,
}
SYSTEM_PROMPT = SYSTEM_PROMPTS[PROMPT_VERSION]
REPAIR_INSTRUCTION = REPAIR_INSTRUCTIONS[REPAIR_PROMPT_VERSION]
TIER3_REPAIR_INSTRUCTION = REPAIR_INSTRUCTION + TIER3_REPAIR_SUFFIX

CACHE_COLLECTION = "semantic_digest_cache"
DEAD_LETTER_COLLECTION = "semantic_digest_dead_letters"
REQUIRED_PROVIDER_TELEMETRY_CONTRACT_VERSION = "litellm-response-telemetry.v1"

Tier = Literal["tier1", "tier2", "tier3", "tier4"]
RequestedTier = Literal["auto", "tier1", "tier2", "tier3", "tier4"]
RuntimeKind = Literal["llama.cpp", "vllm", "mlx", "provider"]


def provider_telemetry_contract_receipt() -> dict[str, Any]:
    """Inspect the loaded LLM wrapper contract without reading a credential."""

    try:
        from services import llm

        observed = getattr(llm, "PROVIDER_TELEMETRY_CONTRACT_VERSION", None)
    except Exception:
        observed = None
    return {
        "required_version": REQUIRED_PROVIDER_TELEMETRY_CONTRACT_VERSION,
        "observed_version": observed,
        "available": observed == REQUIRED_PROVIDER_TELEMETRY_CONTRACT_VERSION,
        "credential_read": False,
        "provider_call": False,
    }


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        protected_namespaces=(),
    )


def _canonical_hash(value: str, field_name: str) -> str:
    if not (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(char in "0123456789abcdef" for char in value[7:])
    ):
        raise ValueError(f"{field_name} must be canonical sha256:<64 lowercase hex>")
    return value


class SemanticGatewayConfig(StrictModel):
    model_id: str = Field(min_length=1)
    runtime: RuntimeKind
    runtime_version: str = Field(min_length=1)
    tokenizer_id: str = Field(min_length=1)
    chat_template_hash: str
    prompt_version: Literal["parent-digest.v5", "parent-digest.v6"] = PROMPT_VERSION
    repair_prompt_version: Literal[
        "parent-digest-repair.v2", "parent-digest-repair.v3"
    ] = REPAIR_PROMPT_VERSION
    requested_tier: RequestedTier = "auto"
    max_tokens: int = Field(default=4096, ge=1)
    timeout_seconds: float = Field(default=120.0, gt=0)

    @field_validator("chat_template_hash")
    @classmethod
    def validate_chat_template_hash(cls, value: str) -> str:
        return _canonical_hash(value, "chat_template_hash")


@dataclass(frozen=True)
class SemanticGatewayRoute:
    """Secret-bearing provider route; never persisted or hashed."""

    api_base: str | None = None
    api_key: str | None = None
    extra_params: dict[str, Any] | None = None


class SemanticGatewayProvenance(StrictModel):
    model_id: str
    runtime: RuntimeKind
    runtime_version: str
    tokenizer_id: str
    chat_template_hash: str
    schema_version: Literal["semantic_digest.v1"]
    schema_hash: str
    prompt_version: Literal["parent-digest.v5", "parent-digest.v6"]
    prompt_hash: str
    repair_prompt_version: Literal["parent-digest-repair.v2", "parent-digest-repair.v3"]
    repair_prompt_hash: str
    temperature: Literal[0]
    input_hash: str
    output_hash: str
    capability_tier: Literal["tier1", "tier3", "tier4"]
    capability_detection: str
    attempts: int = Field(ge=1, le=2)
    repair_attempted: bool
    cache_key: str

    @field_validator(
        "chat_template_hash",
        "schema_hash",
        "prompt_hash",
        "repair_prompt_hash",
        "input_hash",
        "output_hash",
        "cache_key",
    )
    @classmethod
    def validate_hashes(cls, value: str, info) -> str:
        return _canonical_hash(value, info.field_name)


class SemanticGatewayResult(StrictModel):
    digest: SemanticDigestV1
    provenance: SemanticGatewayProvenance
    cache_hit: bool


@dataclass(frozen=True)
class CapabilityDecision:
    supported: bool
    source: str


class StructuredGenerationError(RuntimeError):
    """A bounded structured call failed without exposing raw provider output."""

    def __init__(
        self,
        *,
        errors: list[str],
        dead_letter_id: str | None,
        attempts: int,
    ) -> None:
        self.errors = tuple(errors)
        self.dead_letter_id = dead_letter_id
        self.attempts = attempts
        detail = "; ".join(errors[:3]) or "unknown structured-generation failure"
        super().__init__(
            f"SemanticDigest generation failed after {attempts} attempt(s): "
            f"{detail}; dead_letter_id={dead_letter_id or 'none'}"
        )


class SemanticGatewayTransport(Protocol):
    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        response_format: dict[str, Any],
        config: SemanticGatewayConfig,
        route: SemanticGatewayRoute,
    ) -> str:
        ...

    async def complete_tool(
        self,
        *,
        messages: list[dict[str, str]],
        tool: dict[str, Any],
        tool_choice: dict[str, Any],
        config: SemanticGatewayConfig,
        route: SemanticGatewayRoute,
    ) -> str:
        ...


class SemanticGatewayStore(Protocol):
    async def load_success(self, cache_key: str) -> dict[str, Any] | None:
        ...

    async def save_success(self, result: SemanticGatewayResult) -> None:
        ...

    async def save_dead_letter(
        self,
        *,
        cache_key: str,
        config: SemanticGatewayConfig,
        input_hash: str,
        schema_hash: str,
        prompt_hash: str,
        repair_prompt_hash: str,
        tier: Literal["tier1", "tier3", "tier4"],
        capability_detection: str,
        raw_outputs: list[str],
        errors: list[str],
        attempts: int,
    ) -> str:
        ...


class LiteLLMProxyTransport:
    """Dispatch through Polymath's existing secret-aware LiteLLM wrapper."""

    def __init__(self, service=None) -> None:
        if service is None:
            from services.llm import llm_service

            service = llm_service
        self._service = service
        self._call_telemetry: list[dict[str, Any]] = []

    @property
    def call_telemetry(self) -> tuple[dict[str, Any], ...]:
        """Redacted per-call usage/cost receipts, never provider content."""

        return tuple(dict(row) for row in self._call_telemetry)

    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        response_format: dict[str, Any],
        config: SemanticGatewayConfig,
        route: SemanticGatewayRoute,
    ) -> str:
        return await self._service.complete_sync(
            messages=messages,
            model=config.model_id,
            temperature=0,
            max_tokens=config.max_tokens,
            api_base=route.api_base,
            api_key=route.api_key,
            extra_params=dict(route.extra_params or {}),
            response_format=response_format,
            timeout=config.timeout_seconds,
        )

    async def complete_tool(
        self,
        *,
        messages: list[dict[str, str]],
        tool: dict[str, Any],
        tool_choice: dict[str, Any],
        config: SemanticGatewayConfig,
        route: SemanticGatewayRoute,
    ) -> str:
        from models.schemas import ModelOverrides

        response = await self._service.complete_tool_calls(
            messages=messages,
            model=config.model_id,
            overrides=ModelOverrides(
                model=config.model_id,
                temperature=0,
                max_tokens=config.max_tokens,
            ),
            tools=[tool],
            tool_choice=tool_choice,
            api_base=route.api_base,
            api_key=route.api_key,
            extra_params=dict(route.extra_params or {}),
            timeout=config.timeout_seconds,
        )
        telemetry = (
            response.get("provider_telemetry") if isinstance(response, dict) else None
        )
        if isinstance(telemetry, dict):
            self._call_telemetry.append(dict(telemetry))
        calls = response.get("tool_calls") if isinstance(response, dict) else None
        if not isinstance(calls, list) or len(calls) != 1:
            return ""
        call = calls[0]
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or function.get("name") != TOOL_NAME:
            return ""
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            return canonical_json_v1(arguments)
        return arguments if isinstance(arguments, str) else ""


class MongoSemanticGatewayStore:
    """Noncanonical cache + dead-letter persistence.

    This store never writes ``semantic_artifacts``. Only a structurally and
    semantically accepted digest enters the cache collection; failed output is
    isolated in the dead-letter collection with ``canonical_write=false``.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    async def load_success(self, cache_key: str) -> dict[str, Any] | None:
        return await self._db[CACHE_COLLECTION].find_one(
            {
                "_id": cache_key,
                "status": "accepted_cache",
                "serving_eligible": {"$ne": False},
            },
            {"_id": 0},
        )

    async def save_success(self, result: SemanticGatewayResult) -> None:
        now = datetime.now(timezone.utc)
        doc = {
            "_id": result.provenance.cache_key,
            "status": "accepted_cache",
            "serving_eligible": True,
            "canonical_write": False,
            "digest": result.digest.model_dump(mode="python"),
            "provenance": result.provenance.model_dump(mode="python"),
            "updated_at": now,
        }
        await self._db[CACHE_COLLECTION].replace_one(
            {"_id": result.provenance.cache_key},
            doc,
            upsert=True,
        )

    async def save_dead_letter(
        self,
        *,
        cache_key: str,
        config: SemanticGatewayConfig,
        input_hash: str,
        schema_hash: str,
        prompt_hash: str,
        repair_prompt_hash: str,
        tier: Literal["tier1", "tier3", "tier4"],
        capability_detection: str,
        raw_outputs: list[str],
        errors: list[str],
        attempts: int,
    ) -> str:
        raw_output_hashes = [
            namespace_hash(
                "raw-output",
                {
                    "producer": config.model_id,
                    "output_text_or_json": raw_output,
                },
            )
            for raw_output in raw_outputs
        ]
        identity_hash = namespace_hash(
            "raw-output",
            {
                "cache_key": cache_key,
                "raw_output_hashes": raw_output_hashes,
                "validation_errors": errors,
            },
        )
        dead_letter_id = f"semantic-dlq:{identity_hash.split(':', 1)[1]}"
        now = datetime.now(timezone.utc)
        doc = {
            "_id": dead_letter_id,
            "status": "dead_letter",
            "canonical_write": False,
            "cache_key": cache_key,
            "model_id": config.model_id,
            "runtime": config.runtime,
            "runtime_version": config.runtime_version,
            "tokenizer_id": config.tokenizer_id,
            "chat_template_hash": config.chat_template_hash,
            "schema_version": "semantic_digest.v1",
            "schema_hash": schema_hash,
            "prompt_version": config.prompt_version,
            "prompt_hash": prompt_hash,
            "repair_prompt_version": config.repair_prompt_version,
            "repair_prompt_hash": repair_prompt_hash,
            "temperature": 0,
            "input_hash": input_hash,
            "capability_tier": tier,
            "capability_detection": capability_detection,
            "attempts": attempts,
            "raw_outputs": list(raw_outputs),
            "raw_output_hashes": raw_output_hashes,
            "validation_errors": list(errors),
            "created_at": now,
        }
        await self._db[DEAD_LETTER_COLLECTION].replace_one(
            {"_id": dead_letter_id},
            doc,
            upsert=True,
        )
        return dead_letter_id


def detect_response_schema_capability(
    model_id: str,
    *,
    api_base: str | None = None,
) -> CapabilityDecision:
    """Resolve runtime-verified capability; metadata is advisory only.

    A route earns Tier 1 only through the versioned live-probe registry.
    LiteLLM symbols/metadata are retained solely to explain why an unverified
    route was considered; they can never grant Tier 1. Unknown/error always
    fails closed to Tier 4.
    """

    try:
        registry = load_structured_output_capabilities()
        verified = registry.resolve(model_id=model_id, api_base=api_base)
    except Exception:
        return CapabilityDecision(False, "runtime-capability-registry.error")
    if verified is not None:
        return CapabilityDecision(
            supported=verified.native_json_schema,
            source=(
                f"runtime-capability-registry:{registry.recipe_version}:"
                f"{verified.route_id}:{verified.verification_status}"
            ),
        )

    try:
        import litellm

        detector = getattr(litellm, "supports_response_schema", None)
        if callable(detector):
            return CapabilityDecision(
                supported=False,
                source=(
                    "litellm.supports_response_schema.unverified"
                    if detector(model=model_id) is True
                    else "litellm.supports_response_schema.unsupported"
                ),
            )
        model_info = getattr(litellm, "get_model_info", None)
        if callable(model_info):
            info = model_info(model_id) or {}
            return CapabilityDecision(
                supported=False,
                source=(
                    "litellm.get_model_info.compat.unverified"
                    if info.get("supports_response_schema") is True
                    else "litellm.get_model_info.compat.unsupported"
                ),
            )
    except Exception:
        return CapabilityDecision(False, "litellm.capability_error")
    return CapabilityDecision(False, "litellm.capability_unavailable")


def semantic_digest_schema_hash() -> str:
    return namespace_hash("schema", SemanticDigestV1.model_json_schema())


def semantic_digest_repair_prompt_hash(
    repair_prompt_version: str = REPAIR_PROMPT_VERSION,
) -> str:
    try:
        generic_instruction = REPAIR_INSTRUCTIONS[repair_prompt_version]
    except KeyError as exc:
        raise ValueError(
            f"unsupported repair prompt version: {repair_prompt_version}"
        ) from exc
    return namespace_hash(
        "recipe",
        {
            "repair_prompt_version": repair_prompt_version,
            "generic_instruction": generic_instruction,
            "tier3_instruction": generic_instruction + TIER3_REPAIR_SUFFIX,
        },
    )


def semantic_digest_prompt_hash(
    prompt_version: str = PROMPT_VERSION,
    repair_prompt_version: str = REPAIR_PROMPT_VERSION,
) -> str:
    try:
        system_prompt = SYSTEM_PROMPTS[prompt_version]
    except KeyError as exc:
        raise ValueError(f"unsupported prompt version: {prompt_version}") from exc
    return namespace_hash(
        "recipe",
        {
            "prompt_version": prompt_version,
            "system_prompt": system_prompt,
            "repair_prompt_version": repair_prompt_version,
            "repair_prompt_hash": semantic_digest_repair_prompt_hash(
                repair_prompt_version
            ),
        },
    )


def semantic_digest_input_hash(packet: dict[str, Any]) -> str:
    canonical_packet = canonicalize(packet)
    if not isinstance(canonical_packet, dict):
        raise TypeError("semantic digest packet must be a JSON object")
    packet_body_hash = namespace_hash(
        "body",
        {
            "artifact_type": "parent_evidence_packet",
            "body": canonical_packet,
        },
    )
    return namespace_hash("input-set", frozenset({packet_body_hash}))


def semantic_digest_cache_key(
    *,
    input_hash: str,
    model_id: str,
    schema_hash: str,
    prompt_hash: str,
    runtime_version: str,
) -> str:
    for field_name, value in (
        ("input_hash", input_hash),
        ("schema_hash", schema_hash),
        ("prompt_hash", prompt_hash),
    ):
        _canonical_hash(value, field_name)
    return namespace_hash(
        "work",
        {
            "input_hash": input_hash,
            "model_id": model_id,
            "schema_hash": schema_hash,
            "prompt_hash": prompt_hash,
            "runtime_version": runtime_version,
        },
    )


def tier1_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "semantic_digest_v1",
            "strict": True,
            "schema": SemanticDigestV1.model_json_schema(),
        },
    }


def tier4_response_format() -> dict[str, str]:
    return {"type": "json_object"}


TOOL_NAME = "submit_semantic_digest"


def tier3_tool_definition() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Submit the SemanticDigestV1 for this evidence packet.",
            "strict": True,
            "parameters": SemanticDigestV1.model_json_schema(),
        },
    }


def tier3_tool_choice() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": TOOL_NAME},
    }


async def call_tier2_grammar_constrained(*_args, **_kwargs) -> str:
    raise NotImplementedError(
        "Tier 2 grammar-constrained decoding is not implemented; configure "
        "and prove one specific vLLM/llama.cpp/Outlines runtime first"
    )


def _select_tier(
    requested: RequestedTier,
    capability: CapabilityDecision,
) -> Literal["tier1", "tier3", "tier4"]:
    if requested == "tier2":
        raise NotImplementedError(
            "Tier 2 grammar-constrained decoding is not implemented"
        )
    if requested == "tier3":
        return "tier3"
    if requested == "tier1":
        if not capability.supported:
            raise NotImplementedError(
                "Tier 1 requested but LiteLLM does not report response-schema "
                f"support ({capability.source})"
            )
        return "tier1"
    if requested == "tier4":
        return "tier4"
    return "tier1" if capability.supported else "tier4"


def _initial_messages(
    packet: dict[str, Any],
    *,
    prompt_version: str = PROMPT_VERSION,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPTS[prompt_version]},
        {
            "role": "user",
            "content": "Evidence packet:\n" + canonical_json_v1(packet),
        },
    ]


def _repair_messages(
    *,
    packet: dict[str, Any],
    original_output: str,
    validation_errors: list[str],
    tier: Literal["tier1", "tier3", "tier4"],
    prompt_version: str = PROMPT_VERSION,
    repair_prompt_version: str = REPAIR_PROMPT_VERSION,
) -> list[dict[str, str]]:
    generic_instruction = REPAIR_INSTRUCTIONS[repair_prompt_version]
    instruction = (
        generic_instruction + TIER3_REPAIR_SUFFIX
        if tier == "tier3"
        else generic_instruction
    )
    repair_packet = {
        "evidence_packet": packet,
        "original_output": original_output,
        "validation_errors": validation_errors,
        "instruction": instruction,
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPTS[prompt_version]},
        {
            "role": "user",
            "content": canonical_json_v1(repair_packet),
        },
    ]


def _structural_errors(exc: ValidationError) -> list[str]:
    errors: list[str] = []
    for item in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in item.get("loc") or ()) or "$"
        errors.append(f"{location}: structural {item.get('type')}: {item.get('msg')}")
    return errors


def _validate_raw_output(
    raw_output: str,
    context: SemanticValidationContext,
) -> tuple[SemanticDigestV1 | None, list[str]]:
    try:
        digest = SemanticDigestV1.model_validate_json(raw_output)
    except (ValidationError, ValueError, TypeError) as exc:
        if isinstance(exc, ValidationError):
            return None, _structural_errors(exc)
        return None, [f"$: structural {type(exc).__name__}"]
    semantic_errors = semantic_validate(digest, context)
    if semantic_errors:
        return None, semantic_errors
    return digest, []


class SemanticGateway:
    def __init__(
        self,
        *,
        transport: SemanticGatewayTransport,
        store: SemanticGatewayStore,
        capability_detector=detect_response_schema_capability,
    ) -> None:
        self._transport = transport
        self._store = store
        self._capability_detector = capability_detector

    async def generate(
        self,
        *,
        packet: dict[str, Any],
        context: SemanticValidationContext,
        config: SemanticGatewayConfig,
        route: SemanticGatewayRoute | None = None,
    ) -> SemanticGatewayResult:
        if not isinstance(packet, dict):
            raise TypeError("semantic digest packet must be a dict")
        if not isinstance(context, SemanticValidationContext):
            raise TypeError("context must be SemanticValidationContext")
        if not isinstance(config, SemanticGatewayConfig):
            raise TypeError("config must be SemanticGatewayConfig")
        if route is not None and not isinstance(route, SemanticGatewayRoute):
            raise TypeError("route must be SemanticGatewayRoute")

        canonical_packet = canonicalize(packet)
        if not isinstance(canonical_packet, dict):
            raise TypeError("semantic digest packet must canonicalize to an object")
        route = route or SemanticGatewayRoute()
        input_hash = semantic_digest_input_hash(canonical_packet)
        schema_hash = semantic_digest_schema_hash()
        prompt_hash = semantic_digest_prompt_hash(
            config.prompt_version,
            config.repair_prompt_version,
        )
        repair_prompt_hash = semantic_digest_repair_prompt_hash(
            config.repair_prompt_version
        )
        cache_key = semantic_digest_cache_key(
            input_hash=input_hash,
            model_id=config.model_id,
            schema_hash=schema_hash,
            prompt_hash=prompt_hash,
            runtime_version=config.runtime_version,
        )

        cached = await self._store.load_success(cache_key)
        if cached and cached.get("status") == "accepted_cache":
            try:
                digest = SemanticDigestV1.model_validate(cached["digest"])
                provenance = SemanticGatewayProvenance.model_validate(
                    cached["provenance"]
                )
                if (
                    provenance.cache_key == cache_key
                    and provenance.input_hash == input_hash
                    and provenance.schema_hash == schema_hash
                    and provenance.prompt_hash == prompt_hash
                    and provenance.repair_prompt_hash == repair_prompt_hash
                    and provenance.repair_prompt_version == config.repair_prompt_version
                    and provenance.model_id == config.model_id
                    and provenance.runtime_version == config.runtime_version
                    and not semantic_validate(digest, context)
                ):
                    return SemanticGatewayResult(
                        digest=digest,
                        provenance=provenance,
                        cache_hit=True,
                    )
            except (KeyError, TypeError, ValueError, ValidationError):
                pass

        capability = self._capability_detector(
            config.model_id,
            api_base=route.api_base,
        )
        if not isinstance(capability, CapabilityDecision):
            raise TypeError("capability detector must return CapabilityDecision")
        tier = _select_tier(config.requested_tier, capability)
        capability_source = (
            f"explicit-tier3-forced-tool:{capability.source}"
            if tier == "tier3"
            else capability.source
        )
        response_format = None
        if tier == "tier1":
            response_format = tier1_response_format()
        elif tier == "tier4":
            response_format = tier4_response_format()

        raw_outputs: list[str] = []
        validation_errors: list[str] = []
        for attempt in (1, 2):
            messages = (
                _initial_messages(
                    canonical_packet,
                    prompt_version=config.prompt_version,
                )
                if attempt == 1
                else _repair_messages(
                    packet=canonical_packet,
                    original_output=raw_outputs[0],
                    validation_errors=validation_errors,
                    tier=tier,
                    prompt_version=config.prompt_version,
                    repair_prompt_version=config.repair_prompt_version,
                )
            )
            try:
                if tier == "tier3":
                    raw_output = await self._transport.complete_tool(
                        messages=messages,
                        tool=tier3_tool_definition(),
                        tool_choice=tier3_tool_choice(),
                        config=config,
                        route=route,
                    )
                else:
                    raw_output = await self._transport.complete(
                        messages=messages,
                        response_format=response_format,
                        config=config,
                        route=route,
                    )
            except Exception as exc:
                validation_errors = [
                    f"transport.attempt[{attempt}]: {type(exc).__name__}"
                ]
                dead_letter_id = await self._store.save_dead_letter(
                    cache_key=cache_key,
                    config=config,
                    input_hash=input_hash,
                    schema_hash=schema_hash,
                    prompt_hash=prompt_hash,
                    repair_prompt_hash=repair_prompt_hash,
                    tier=tier,
                    capability_detection=capability_source,
                    raw_outputs=raw_outputs,
                    errors=validation_errors,
                    attempts=attempt,
                )
                raise StructuredGenerationError(
                    errors=validation_errors,
                    dead_letter_id=dead_letter_id,
                    attempts=attempt,
                ) from exc

            if not isinstance(raw_output, str):
                raw_output = ""
                validation_errors = [
                    f"$: structural transport_output_type: expected str"
                ]
                digest = None
            else:
                digest, validation_errors = _validate_raw_output(
                    raw_output,
                    context,
                )
            raw_outputs.append(raw_output)
            if digest is None:
                if attempt == 1:
                    continue
                dead_letter_id = await self._store.save_dead_letter(
                    cache_key=cache_key,
                    config=config,
                    input_hash=input_hash,
                    schema_hash=schema_hash,
                    prompt_hash=prompt_hash,
                    repair_prompt_hash=repair_prompt_hash,
                    tier=tier,
                    capability_detection=capability_source,
                    raw_outputs=raw_outputs,
                    errors=validation_errors,
                    attempts=attempt,
                )
                raise StructuredGenerationError(
                    errors=validation_errors,
                    dead_letter_id=dead_letter_id,
                    attempts=attempt,
                )

            output_hash = namespace_hash(
                "body",
                digest.model_dump(mode="python"),
            )
            provenance = SemanticGatewayProvenance(
                model_id=config.model_id,
                runtime=config.runtime,
                runtime_version=config.runtime_version,
                tokenizer_id=config.tokenizer_id,
                chat_template_hash=config.chat_template_hash,
                schema_version="semantic_digest.v1",
                schema_hash=schema_hash,
                prompt_version=config.prompt_version,
                prompt_hash=prompt_hash,
                repair_prompt_version=config.repair_prompt_version,
                repair_prompt_hash=repair_prompt_hash,
                temperature=0,
                input_hash=input_hash,
                output_hash=output_hash,
                capability_tier=tier,
                capability_detection=capability_source,
                attempts=attempt,
                repair_attempted=attempt == 2,
                cache_key=cache_key,
            )
            result = SemanticGatewayResult(
                digest=digest,
                provenance=provenance,
                cache_hit=False,
            )
            await self._store.save_success(result)
            return result

        raise AssertionError("semantic gateway attempt loop exhausted")
