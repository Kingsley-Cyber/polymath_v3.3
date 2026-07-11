"""Provider-card resolution for Ghost B extraction lanes.

The extraction lane is not just "cloud" or "local". Each model/provider pair
has a concrete contract: whether native JSON schema is trusted, whether output
must be compiler-gated, how semantic validation runs, and how concurrency is
budgeted. This module keeps that contract deterministic and reusable by Ghost B,
resource planning, preflight, and the UI-facing status layer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


SchemaMode = Literal["json_schema", "json_object", "json_object_prompt", "jsonl"]
ExtractionRoutingPolicy = Literal["work_stealing", "balanced", "primary_fallback"]
JsonRepairMode = Literal[
    "provider_native",
    "balanced_object_repair",
    "jsonl_repair_resume",
    "deterministic_compiler",
]
SemanticVerifierMode = Literal["strict", "strict_with_direction_repair"]
ConcurrencyPolicy = Literal["static_lane_cap", "adaptive_vram_85"]
FailureBackfillPolicy = Literal["retry_then_stage", "stage_failures"]

PRIVATE_NET_MARKERS = (
    "localhost",
    "127.0.0.1",
    "192.168.",
    "10.",
    "172.16.",
    "172.17.",
    "172.18.",
    "172.19.",
    "172.20.",
    "172.21.",
    "172.22.",
    "172.23.",
    "172.24.",
    "172.25.",
    "172.26.",
    "172.27.",
    "172.28.",
    "172.29.",
    "172.30.",
    "172.31.",
)

PROMOTION_GATE: tuple[str, ...] = (
    "json_parse",
    "pydantic_extraction_response",
    "allowed_predicate",
    "required_evidence_phrase",
    "sane_endpoints",
    "semantic_direction_check",
)


@dataclass(frozen=True)
class ExtractionProviderCard:
    provider: str
    model: str
    endpoint: str
    auth_mode: str
    schema_mode: SchemaMode
    json_repair_mode: JsonRepairMode
    semantic_verifier_mode: SemanticVerifierMode
    concurrency_policy: ConcurrencyPolicy
    failure_backfill_policy: FailureBackfillPolicy
    supports_json_schema: bool = False
    supports_json_object: bool = False
    disable_thinking: bool = False
    local_private: bool = False
    managed_vllm: bool = False
    lifecycle_base_url: str = ""
    context_window_tokens: int | None = None
    promotion_gate: tuple[str, ...] = PROMOTION_GATE
    notes: tuple[str, ...] = ()

    def to_safe_dict(self) -> dict[str, Any]:
        """Serializable status payload. Never includes API keys."""

        return asdict(self)


def _entry_dict(entry: Any) -> dict[str, Any]:
    if hasattr(entry, "model_dump"):
        return entry.model_dump()
    if isinstance(entry, dict):
        return dict(entry)
    return dict(entry or {})


def _extra(entry: dict[str, Any]) -> dict[str, Any]:
    value = entry.get("extra_params") or {}
    return value if isinstance(value, dict) else {}


def normalize_extraction_routing_policy(value: Any) -> ExtractionRoutingPolicy | None:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"balanced", "balanced_fanout", "fanout", "parallel"}:
        return "balanced"
    if text in {"work_stealing", "workstealing", "spillover", "fastest", "auto"}:
        return "work_stealing"
    if text in {"primary_fallback", "primary_failover", "fallback", "failover"}:
        return "primary_fallback"
    return None


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return None


def _provider_key(entry: dict[str, Any]) -> str:
    provider = str(
        entry.get("provider_preset") or entry.get("provider") or ""
    ).strip().lower()
    model = str(entry.get("model") or entry.get("model_name") or "").lower()
    base_url = str(entry.get("base_url") or entry.get("api_base") or "").lower()
    lifecycle = str(entry.get("lifecycle_base_url") or "").lower()
    extra = _extra(entry)

    if provider in {"local_private_vllm", "private_vllm", "vllm", "vllm-rtx", "rtx"}:
        return "local_private_vllm"
    if bool(extra.get("managed_vllm")) or str(extra.get("resource_class") or "").lower() == "rtx":
        return "local_private_vllm"
    if "vllm" in provider or "vllm" in model or "vllm" in base_url or "vllm" in lifecycle:
        return "local_private_vllm"
    if "polymath-extract" in model and any(marker in base_url for marker in PRIVATE_NET_MARKERS):
        return "local_private_vllm"
    if provider == "longcat" or "api.longcat.chat" in base_url or "longcat" in model:
        return "longcat"
    if provider == "siliconflow" or "siliconflow" in base_url or "tencent/hy3" in model or "hy3" in model:
        return "siliconflow"
    if provider == "openrouter" or "openrouter.ai" in base_url:
        return "openrouter"
    if provider == "deepseek" or "api.deepseek.com" in base_url or model.startswith("deepseek/"):
        return "deepseek"
    if provider in {"mimo", "xiaomi"} or "xiaomimimo" in base_url or "mimo" in model:
        return "mimo"
    if provider == "openai" or "api.openai.com" in base_url or model.startswith("openai/") or model.startswith("gpt-"):
        return "openai"
    return provider or "custom"


def _is_private_endpoint(base_url: str) -> bool:
    lower = base_url.lower()
    return any(marker in lower for marker in PRIVATE_NET_MARKERS)


def _auth_mode(entry: dict[str, Any], *, provider: str, local_private: bool) -> str:
    if entry.get("api_key"):
        return "bearer_api_key"
    if local_private:
        return "none_or_lan_bearer"
    if provider == "openrouter":
        return "bearer_api_key_with_openrouter_headers"
    return "bearer_api_key"


def resolve_extraction_provider_card(entry: Any) -> ExtractionProviderCard:
    data = _entry_dict(entry)
    extra = _extra(data)
    provider = _provider_key(data)
    model = str(data.get("model") or data.get("model_name") or "").strip()
    endpoint = str(data.get("base_url") or data.get("api_base") or "").strip()
    lifecycle = str(data.get("lifecycle_base_url") or "").strip()
    local_private = provider == "local_private_vllm" or _is_private_endpoint(endpoint)
    managed_vllm = provider == "local_private_vllm" or bool(lifecycle)

    explicit_schema = str(extra.get("schema_mode") or "").strip().lower()
    explicit_json_schema = _flag(extra.get("supports_json_schema"))
    explicit_json_object = _flag(extra.get("supports_json_object"))
    explicit_disable_thinking = _flag(extra.get("disable_thinking"))
    context_window_raw = (
        extra.get("context_window_tokens")
        or extra.get("max_context_tokens")
        or data.get("context_window_tokens")
    )
    try:
        context_window_tokens = int(context_window_raw or 0) or None
    except (TypeError, ValueError):
        context_window_tokens = None

    notes: list[str] = []
    supports_json_schema = False
    supports_json_object = False
    schema_mode: SchemaMode = "jsonl"
    json_repair_mode: JsonRepairMode = "jsonl_repair_resume"
    semantic_mode: SemanticVerifierMode = "strict_with_direction_repair"
    concurrency_policy: ConcurrencyPolicy = "static_lane_cap"

    if provider in {"openai", "deepseek"}:
        supports_json_schema = True
        supports_json_object = True
        schema_mode = "json_schema"
        json_repair_mode = "provider_native"
    elif provider == "openrouter":
        supports_json_schema = "mistral-nemo" in model.lower()
        supports_json_object = True
        schema_mode = "json_schema" if supports_json_schema else "json_object_prompt"
        json_repair_mode = "provider_native" if supports_json_schema else "balanced_object_repair"
        if supports_json_schema:
            notes.append("openrouter_mistral_nemo_native_json_schema")
    elif provider == "local_private_vllm":
        supports_json_schema = True
        supports_json_object = True
        schema_mode = "json_schema"
        json_repair_mode = "provider_native"
        concurrency_policy = "adaptive_vram_85"
        notes.append("private_openai_compatible_extraction_provider")
    elif provider == "longcat":
        supports_json_schema = False
        supports_json_object = False
        schema_mode = "json_object_prompt"
        json_repair_mode = "deterministic_compiler"
        notes.append("longcat_requires_thinking_disabled_and_compiler_gate")
    elif provider == "siliconflow":
        supports_json_schema = False
        supports_json_object = False
        schema_mode = "json_object_prompt"
        json_repair_mode = "deterministic_compiler"
        notes.append("siliconflow_hy3_prompt_json_only")
    elif provider == "mimo":
        supports_json_schema = False
        supports_json_object = False
        schema_mode = "json_object_prompt"
        json_repair_mode = "deterministic_compiler"
        notes.append("mimo_reasoning_disabled_and_compiler_gate")

    if explicit_json_schema is not None:
        supports_json_schema = explicit_json_schema
        if explicit_json_schema:
            schema_mode = "json_schema"
            json_repair_mode = "provider_native"
        elif schema_mode == "json_schema":
            schema_mode = "json_object_prompt"
            json_repair_mode = "deterministic_compiler"
    if explicit_json_object is not None:
        supports_json_object = explicit_json_object
    if explicit_schema in {"json_schema", "json_object", "json_object_prompt", "jsonl"}:
        schema_mode = explicit_schema  # type: ignore[assignment]
        if schema_mode == "json_schema":
            supports_json_schema = True
            json_repair_mode = "provider_native"
        elif schema_mode == "json_object":
            supports_json_object = True
            json_repair_mode = "balanced_object_repair"
        elif schema_mode == "json_object_prompt":
            json_repair_mode = "deterministic_compiler"
        else:
            json_repair_mode = "jsonl_repair_resume"

    disable_thinking = provider in {"longcat", "deepseek", "mimo"} or "mimo" in model.lower()
    if explicit_disable_thinking is not None:
        disable_thinking = explicit_disable_thinking

    if context_window_tokens is None:
        if provider == "local_private_vllm":
            # Current managed RTX service advertises an 8K serving context.
            # Operators can override this per card when the server changes.
            context_window_tokens = 8192
        elif provider == "longcat":
            context_window_tokens = 1_000_000

    return ExtractionProviderCard(
        provider=provider,
        model=model,
        endpoint=endpoint or "litellm_default",
        auth_mode=_auth_mode(data, provider=provider, local_private=local_private),
        schema_mode=schema_mode,
        json_repair_mode=json_repair_mode,
        semantic_verifier_mode=semantic_mode,
        concurrency_policy=concurrency_policy,
        failure_backfill_policy="retry_then_stage",
        supports_json_schema=supports_json_schema,
        supports_json_object=supports_json_object,
        disable_thinking=disable_thinking,
        local_private=local_private,
        managed_vllm=managed_vllm,
        lifecycle_base_url=lifecycle,
        context_window_tokens=context_window_tokens,
        notes=tuple(notes),
    )


def resolve_extraction_routing_policy(pool: list[Any]) -> ExtractionRoutingPolicy:
    """Resolve how a provider pool should consume chunk extraction work.

    Mixed local/private and cloud provider pools default to balanced fanout so
    the configured independent provider lanes all receive work. Operators can
    still force primary-fallback or work-stealing per lane via routing_policy or
    extra_params.routing_policy.
    """

    entries = [_entry_dict(entry) for entry in pool]
    for entry in entries:
        explicit = normalize_extraction_routing_policy(entry.get("routing_policy"))
        if explicit:
            return explicit
        extra = _extra(entry)
        explicit = normalize_extraction_routing_policy(
            extra.get("routing_policy") or extra.get("route_policy")
        )
        if explicit:
            return explicit

    if len(entries) < 2:
        return "work_stealing"
    cards = [resolve_extraction_provider_card(entry) for entry in entries]
    has_private = any(card.local_private for card in cards)
    has_cloud = any(not card.local_private for card in cards)
    if has_private and has_cloud:
        return "balanced"
    return "work_stealing"


def safe_extraction_lane_descriptor(entry: Any, *, lane: int) -> dict[str, Any]:
    data = _entry_dict(entry)
    card = resolve_extraction_provider_card(data)
    return {
        "lane": lane,
        "provider_preset": data.get("provider_preset") or data.get("provider"),
        "provider": card.provider,
        "model": data.get("model") or data.get("model_name"),
        "base_url": data.get("base_url") or data.get("api_base"),
        "max_concurrent": data.get("max_concurrent"),
        "schema_mode": card.schema_mode,
        "output_mode": card.schema_mode,
        "json_repair_mode": card.json_repair_mode,
        "semantic_verifier_mode": card.semantic_verifier_mode,
        "concurrency_policy": card.concurrency_policy,
        "local_private": card.local_private,
        "managed_vllm": card.managed_vllm,
        "provider_card": card.to_safe_dict(),
    }


def safe_extraction_pool_contract(*, pool_source: str, pool: list[Any]) -> dict[str, Any]:
    """Safe, API-key-free provider contract used by jobs, APIs, and UI."""

    entries = [_entry_dict(entry) for entry in pool]
    lanes = [
        safe_extraction_lane_descriptor(entry, lane=idx)
        for idx, entry in enumerate(entries)
    ]
    return {
        "pool_source": pool_source,
        "pool_size": len(lanes),
        "routing_policy": resolve_extraction_routing_policy(entries),
        "lanes": lanes,
        "lane_capacities": [
            {
                "lane": lane["lane"],
                "provider": lane["provider"],
                "model": lane["model"],
                "max_concurrent": lane["max_concurrent"],
                "concurrency_policy": lane["concurrency_policy"],
                "local_private": lane["local_private"],
            }
            for lane in lanes
        ],
    }


def provider_payload_defaults(card: ExtractionProviderCard) -> dict[str, Any]:
    """Provider-body defaults derived from the card.

    These are safe defaults only. Callers should merge user extra_params first
    and then setdefault these values so explicit operator choices still win.
    """

    if card.disable_thinking:
        if card.provider == "siliconflow":
            return {"enable_thinking": False}
        return {"thinking": {"type": "disabled"}}
    return {}


def extraction_lane_uses_private_vllm(entry: Any) -> bool:
    card = resolve_extraction_provider_card(entry)
    return card.provider == "local_private_vllm" or card.managed_vllm
