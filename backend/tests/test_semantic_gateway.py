from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from models.hash_taxonomy import namespace_hash
from models.semantic_validator import ClaimScope, SemanticValidationContext
from services.semantic_gateway import (
    CACHE_COLLECTION,
    DEAD_LETTER_COLLECTION,
    PROMPT_VERSION,
    REPAIR_PROMPT_VERSION,
    REPAIR_INSTRUCTION,
    SYSTEM_PROMPT,
    TIER3_REPAIR_INSTRUCTION,
    CapabilityDecision,
    LiteLLMProxyTransport,
    MongoSemanticGatewayStore,
    SemanticGateway,
    SemanticGatewayConfig,
    SemanticGatewayRoute,
    StructuredGenerationError,
    call_tier2_grammar_constrained,
    detect_response_schema_capability,
    semantic_digest_cache_key,
    semantic_digest_input_hash,
    semantic_digest_prompt_hash,
    semantic_digest_repair_prompt_hash,
    semantic_digest_schema_hash,
    tier3_tool_choice,
    tier3_tool_definition,
)


def _digest_payload() -> dict:
    return {
        "schema_version": "semantic_digest.v1",
        "parent_id": "parent:one",
        "summary": "Feedback updates the reference used for later choices.",
        "central_thesis": "Observed outcomes can change an internal baseline.",
        "underlying_meanings": [
            {
                "text": "Repeated outcomes reshape a reference.",
                "supporting_claim_ids": ["claim:one"],
            }
        ],
        "domain_proposals": [
            {
                "registry_id": "D09",
                "proposed_label": "Technology and Engineered Systems",
                "role": "adjacent",
                "assignment_state": "candidate",
                "supporting_claim_ids": ["claim:one"],
            }
        ],
        "frame_proposals": [
            {
                "frame_id": "MF07",
                "role": "dominant",
                "assignment_state": "candidate",
                "supporting_claim_ids": ["claim:one"],
                "explanation": "Feedback updates a belief or reference.",
            },
            {
                "frame_id": "MF15",
                "role": "supporting",
                "assignment_state": "corroborated",
                "supporting_claim_ids": ["claim:two"],
                "explanation": "Repeated updates accumulate over time.",
            },
        ],
        "latent_concepts": [
            {
                "preferred_label": "adaptive reference",
                "definition": "A reference updated from observed outcomes.",
                "assignment_state": "candidate",
                "supporting_claim_ids": ["claim:one"],
                "aliases": ["moving baseline"],
            }
        ],
        "motif_proposals": [
            {
                "proposed_label": "feedback-driven adaptation",
                "frame_sequence": ["MF07", "MF15"],
                "abstract_sequence": ["update", "accumulate"],
                "supporting_claim_ids": ["claim:one", "claim:two"],
            }
        ],
        "conditions": [
            {
                "text": "Feedback remains observable.",
                "supporting_claim_ids": ["claim:two"],
            }
        ],
        "exceptions": [],
        "unresolved_interpretations": [],
    }


def _raw(payload: dict | None = None) -> str:
    return json.dumps(payload or _digest_payload())


def _packet(**updates) -> dict:
    packet = {
        "parent_id": "parent:one",
        "parent_text": "Feedback updates a baseline.",
        "claims": [
            {"claim_id": "claim:one", "text": "Feedback updates a baseline."},
            {"claim_id": "claim:two", "text": "Updates accumulate."},
        ],
    }
    packet.update(updates)
    return packet


def _context() -> SemanticValidationContext:
    return SemanticValidationContext.from_owner_registries(
        parent_id="parent:one",
        claims=(
            ClaimScope("claim:one", "parent:one"),
            ClaimScope("claim:two", "parent:one"),
        ),
        self_reference_ids=("digest:one",),
    )


def _config(**updates) -> SemanticGatewayConfig:
    values = {
        "model_id": "deepseek/deepseek-v4-flash",
        "runtime": "provider",
        "runtime_version": "deepseek-api.2026-07-14",
        "tokenizer_id": "deepseek-v4-flash.tokenizer",
        "chat_template_hash": namespace_hash(
            "recipe", {"chat_template": "deepseek-provider-managed-v1"}
        ),
        "prompt_version": PROMPT_VERSION,
        "requested_tier": "auto",
        "max_tokens": 2048,
        "timeout_seconds": 30.0,
    }
    values.update(updates)
    return SemanticGatewayConfig(**values)


class _FakeTransport:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output

    async def complete_tool(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


class _FakeStore:
    def __init__(self):
        self.cache: dict[str, dict] = {}
        self.successes = []
        self.dead_letters = []

    async def load_success(self, cache_key):
        return self.cache.get(cache_key)

    async def save_success(self, result):
        self.successes.append(result)
        self.cache[result.provenance.cache_key] = {
            "status": "accepted_cache",
            "canonical_write": False,
            "digest": result.digest.model_dump(mode="python"),
            "provenance": result.provenance.model_dump(mode="python"),
        }

    async def save_dead_letter(self, **kwargs):
        self.dead_letters.append(kwargs)
        return "semantic-dlq:test"


def _capability(supported: bool, source: str = "test.detector"):
    def detector(_model_id, *, api_base=None):
        del api_base
        return CapabilityDecision(supported=supported, source=source)

    return detector


@pytest.mark.asyncio
async def test_tier1_success_uses_native_strict_schema_and_records_provenance():
    transport = _FakeTransport([_raw()])
    store = _FakeStore()
    gateway = SemanticGateway(
        transport=transport,
        store=store,
        capability_detector=_capability(True),
    )
    route = SemanticGatewayRoute(
        api_base="https://provider.invalid",
        api_key="secret-never-persisted",
        extra_params={"thinking": {"type": "disabled"}},
    )

    result = await gateway.generate(
        packet=_packet(),
        context=_context(),
        config=_config(),
        route=route,
    )

    assert result.cache_hit is False
    assert result.provenance.capability_tier == "tier1"
    assert result.provenance.capability_detection == "test.detector"
    assert result.provenance.temperature == 0
    assert result.provenance.attempts == 1
    assert result.provenance.repair_attempted is False
    assert result.provenance.schema_hash == semantic_digest_schema_hash()
    assert result.provenance.prompt_hash == semantic_digest_prompt_hash()
    assert result.provenance.repair_prompt_version == REPAIR_PROMPT_VERSION
    assert result.provenance.repair_prompt_hash == semantic_digest_repair_prompt_hash()
    assert result.provenance.input_hash == semantic_digest_input_hash(_packet())
    assert len(store.successes) == 1
    assert store.dead_letters == []

    call = transport.calls[0]
    response_format = call["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["name"] == "semantic_digest_v1"
    schema = response_format["json_schema"]["schema"]
    assert set(schema["required"]) == set(schema["properties"])
    assert call["route"] is route
    assert call["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    prompt_text = "\n".join(message["content"] for message in call["messages"])
    assert "$defs" not in prompt_text
    assert "additionalProperties" not in prompt_text
    assert "secret-never-persisted" not in str(store.cache)


@pytest.mark.asyncio
async def test_tier4_fallback_is_explicit_in_provenance_and_uses_json_mode():
    transport = _FakeTransport([_raw()])
    store = _FakeStore()
    gateway = SemanticGateway(
        transport=transport,
        store=store,
        capability_detector=_capability(False, "litellm.capability_unavailable"),
    )

    result = await gateway.generate(
        packet=_packet(),
        context=_context(),
        config=_config(),
    )

    assert result.provenance.capability_tier == "tier4"
    assert result.provenance.capability_detection == "litellm.capability_unavailable"
    assert result.provenance.prompt_version == "parent-digest.v6"
    assert transport.calls[0]["response_format"] == {"type": "json_object"}
    system_prompt = transport.calls[0]["messages"][0]["content"]
    assert "json" in system_prompt.casefold()
    assert "top level" in system_prompt
    assert "Do not wrap it under digest" in system_prompt
    assert "$defs" not in system_prompt
    assert "additionalProperties" not in system_prompt


def test_v6_prompt_freezes_conservative_proposal_contract_verbatim():
    assert PROMPT_VERSION == "parent-digest.v6"
    assert REPAIR_PROMPT_VERSION == "parent-digest-repair.v3"
    assert (
        "Every domain, frame, latent-concept, or motif proposal must have a "
        "non-empty supporting_claim_ids array"
    ) in SYSTEM_PROMPT
    assert (
        "every frame_id in its frame_sequence also appears in frame_proposals"
        in SYSTEM_PROMPT
    )
    assert (
        "assignment_state as candidate, corroborated, unresolved, or rejected"
        in SYSTEM_PROMPT
    )
    assert "empty proposal arrays are always lawful" in SYSTEM_PROMPT
    assert "assignment_state as candidate, corroborated, validated" not in SYSTEM_PROMPT


def test_repair_v3_requires_pruning_and_forbids_invented_support():
    assert "remove that entire optional proposal" in REPAIR_INSTRUCTION
    assert "Never preserve a failing proposal by inventing" in REPAIR_INSTRUCTION
    assert "empty proposal arrays are always lawful" in REPAIR_INSTRUCTION
    assert TIER3_REPAIR_INSTRUCTION.startswith(REPAIR_INSTRUCTION)
    assert "SAME forced submit_semantic_digest tool" in TIER3_REPAIR_INSTRUCTION


def test_legacy_v5_and_v2_hashes_remain_reconstructable_for_skip_validation():
    legacy_repair = semantic_digest_repair_prompt_hash("parent-digest-repair.v2")
    legacy_prompt = semantic_digest_prompt_hash(
        "parent-digest.v5", "parent-digest-repair.v2"
    )

    assert legacy_repair != semantic_digest_repair_prompt_hash()
    assert legacy_prompt != semantic_digest_prompt_hash()


@pytest.mark.asyncio
async def test_tier3_forces_one_digest_tool_and_validates_arguments():
    transport = _FakeTransport([_raw()])
    store = _FakeStore()
    gateway = SemanticGateway(
        transport=transport,
        store=store,
        capability_detector=_capability(False, "runtime.route.native-rejected"),
    )

    result = await gateway.generate(
        packet=_packet(),
        context=_context(),
        config=_config(requested_tier="tier3"),
    )

    assert result.provenance.capability_tier == "tier3"
    assert result.provenance.capability_detection == (
        "explicit-tier3-forced-tool:runtime.route.native-rejected"
    )
    call = transport.calls[0]
    assert call["tool_choice"] == tier3_tool_choice()
    assert call["tool"] == tier3_tool_definition()
    function = call["tool"]["function"]
    assert function["name"] == "submit_semantic_digest"
    assert function["strict"] is True
    schema = function["parameters"]
    assert set(schema["required"]) == set(schema["properties"])
    assert store.dead_letters == []


@pytest.mark.asyncio
async def test_tier3_repair_reuses_exact_forced_tool_contract():
    invalid = _digest_payload()
    invalid["parent_id"] = "parent:wrong"
    transport = _FakeTransport([_raw(invalid), _raw()])
    gateway = SemanticGateway(
        transport=transport,
        store=_FakeStore(),
        capability_detector=_capability(False),
    )

    result = await gateway.generate(
        packet=_packet(),
        context=_context(),
        config=_config(requested_tier="tier3"),
    )

    assert result.provenance.attempts == 2
    assert result.provenance.repair_attempted is True
    assert transport.calls[0]["tool"] is not transport.calls[1]["tool"]
    assert transport.calls[0]["tool"] == transport.calls[1]["tool"]
    assert transport.calls[0]["tool_choice"] == transport.calls[1]["tool_choice"]
    repair = json.loads(transport.calls[1]["messages"][1]["content"])
    assert repair["instruction"] == TIER3_REPAIR_INSTRUCTION
    assert "SAME forced submit_semantic_digest tool" in repair["instruction"]
    assert "all 12 SemanticDigestV1 fields" in repair["instruction"]
    assert "Do not nest them under parameters" in repair["instruction"]
    assert repair["validation_errors"] == [
        "parent_id: digest parent 'parent:wrong' does not match supplied parent "
        "'parent:one'"
    ]


@pytest.mark.asyncio
async def test_tier3_second_invalid_arguments_dead_letter_after_two_tool_calls():
    transport = _FakeTransport(["{}", "{}"])
    store = _FakeStore()
    gateway = SemanticGateway(
        transport=transport,
        store=store,
        capability_detector=_capability(False),
    )

    with pytest.raises(StructuredGenerationError) as caught:
        await gateway.generate(
            packet=_packet(),
            context=_context(),
            config=_config(requested_tier="tier3"),
        )

    assert caught.value.attempts == 2
    assert len(transport.calls) == 2
    assert all("tool" in call for call in transport.calls)
    assert store.successes == []
    assert store.dead_letters[0]["tier"] == "tier3"
    assert store.dead_letters[0]["attempts"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_tier", ["tier2"])
async def test_unimplemented_tiers_fail_clearly_without_provider_call(requested_tier):
    transport = _FakeTransport([_raw()])
    store = _FakeStore()
    gateway = SemanticGateway(
        transport=transport,
        store=store,
        capability_detector=_capability(True),
    )

    with pytest.raises(NotImplementedError, match=f"Tier {requested_tier[-1]}"):
        await gateway.generate(
            packet=_packet(),
            context=_context(),
            config=_config(requested_tier=requested_tier),
        )

    assert transport.calls == []
    assert store.successes == []
    assert store.dead_letters == []


@pytest.mark.asyncio
async def test_public_tier2_stub_raises_clear_message():
    with pytest.raises(NotImplementedError, match="grammar-constrained"):
        await call_tier2_grammar_constrained()


@pytest.mark.asyncio
async def test_forced_tier1_fails_when_litellm_does_not_report_support():
    gateway = SemanticGateway(
        transport=_FakeTransport([_raw()]),
        store=_FakeStore(),
        capability_detector=_capability(False, "test.unsupported"),
    )

    with pytest.raises(NotImplementedError, match="Tier 1 requested"):
        await gateway.generate(
            packet=_packet(),
            context=_context(),
            config=_config(requested_tier="tier1"),
        )


@pytest.mark.asyncio
async def test_structural_failure_gets_one_targeted_same_schema_repair():
    incomplete = {
        "schema_version": "semantic_digest.v1",
        "parent_id": "parent:one",
        "summary": "Incomplete.",
        "central_thesis": "Missing required arrays.",
    }
    transport = _FakeTransport([_raw(incomplete), _raw()])
    store = _FakeStore()
    gateway = SemanticGateway(
        transport=transport,
        store=store,
        capability_detector=_capability(True),
    )

    result = await gateway.generate(
        packet=_packet(),
        context=_context(),
        config=_config(),
    )

    assert result.provenance.attempts == 2
    assert result.provenance.repair_attempted is True
    assert len(transport.calls) == 2
    assert (
        transport.calls[0]["response_format"] is transport.calls[1]["response_format"]
    )
    repair = json.loads(transport.calls[1]["messages"][1]["content"])
    assert repair["original_output"] == _raw(incomplete)
    assert repair["instruction"] == REPAIR_INSTRUCTION
    assert "Do not wrap it under digest" in repair["instruction"]
    assert any("Field required" in error for error in repair["validation_errors"])
    assert "empty array" in repair["instruction"]
    assert "$defs" not in transport.calls[1]["messages"][1]["content"]
    assert len(store.successes) == 1
    assert store.dead_letters == []


@pytest.mark.asyncio
async def test_semantic_failure_repairs_using_exact_location_indexed_errors():
    unsupported = _digest_payload()
    unsupported["conditions"][0]["supporting_claim_ids"] = ["claim:missing"]
    transport = _FakeTransport([_raw(unsupported), _raw()])
    store = _FakeStore()
    gateway = SemanticGateway(
        transport=transport,
        store=store,
        capability_detector=_capability(False),
    )

    result = await gateway.generate(
        packet=_packet(),
        context=_context(),
        config=_config(),
    )

    repair = json.loads(transport.calls[1]["messages"][1]["content"])
    assert repair["validation_errors"] == [
        "conditions[0].supporting_claim_ids[0]: unknown claim_id 'claim:missing'"
    ]
    assert result.provenance.capability_tier == "tier4"
    assert result.provenance.attempts == 2


@pytest.mark.asyncio
async def test_second_invalid_output_dead_letters_and_never_saves_success():
    bad = _digest_payload()
    bad["frame_proposals"][0]["supporting_claim_ids"] = []
    transport = _FakeTransport([_raw(bad), _raw(bad)])
    store = _FakeStore()
    gateway = SemanticGateway(
        transport=transport,
        store=store,
        capability_detector=_capability(True),
    )

    with pytest.raises(StructuredGenerationError) as caught:
        await gateway.generate(
            packet=_packet(),
            context=_context(),
            config=_config(),
        )

    assert caught.value.attempts == 2
    assert caught.value.dead_letter_id == "semantic-dlq:test"
    assert len(transport.calls) == 2
    assert store.successes == []
    assert len(store.dead_letters) == 1
    dead = store.dead_letters[0]
    assert dead["attempts"] == 2
    assert dead["raw_outputs"] == [_raw(bad), _raw(bad)]
    assert dead["tier"] == "tier1"


@pytest.mark.asyncio
async def test_transport_failure_dead_letters_without_leaking_exception_secret():
    transport = _FakeTransport([RuntimeError("api_key=super-secret-value")])
    store = _FakeStore()
    gateway = SemanticGateway(
        transport=transport,
        store=store,
        capability_detector=_capability(False),
    )

    with pytest.raises(StructuredGenerationError) as caught:
        await gateway.generate(
            packet=_packet(),
            context=_context(),
            config=_config(),
        )

    assert caught.value.attempts == 1
    assert "super-secret-value" not in str(caught.value)
    assert store.successes == []
    assert store.dead_letters[0]["errors"] == ["transport.attempt[1]: RuntimeError"]
    assert "super-secret-value" not in str(store.dead_letters)


@pytest.mark.asyncio
async def test_valid_cache_hit_skips_provider_and_revalidates_semantics():
    store = _FakeStore()
    first_transport = _FakeTransport([_raw()])
    first_gateway = SemanticGateway(
        transport=first_transport,
        store=store,
        capability_detector=_capability(True),
    )
    first = await first_gateway.generate(
        packet=_packet(),
        context=_context(),
        config=_config(),
    )

    second_transport = _FakeTransport([])
    second_gateway = SemanticGateway(
        transport=second_transport,
        store=store,
        capability_detector=_capability(False),
    )
    second = await second_gateway.generate(
        packet=_packet(),
        context=_context(),
        config=_config(),
    )

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.digest == first.digest
    assert second.provenance == first.provenance
    assert second_transport.calls == []


@pytest.mark.asyncio
async def test_semantically_corrupt_cache_is_not_returned():
    store = _FakeStore()
    seed_gateway = SemanticGateway(
        transport=_FakeTransport([_raw()]),
        store=store,
        capability_detector=_capability(True),
    )
    seed = await seed_gateway.generate(
        packet=_packet(),
        context=_context(),
        config=_config(),
    )
    cached = store.cache[seed.provenance.cache_key]
    cached["digest"]["conditions"][0]["supporting_claim_ids"] = ["claim:missing"]

    transport = _FakeTransport([_raw()])
    gateway = SemanticGateway(
        transport=transport,
        store=store,
        capability_detector=_capability(False),
    )
    result = await gateway.generate(
        packet=_packet(),
        context=_context(),
        config=_config(),
    )

    assert result.cache_hit is False
    assert len(transport.calls) == 1
    assert result.digest.conditions[0].supporting_claim_ids == ["claim:two"]


def test_input_and_cache_identity_flip_only_on_spec_inputs():
    first_input = semantic_digest_input_hash(_packet())
    changed_input = semantic_digest_input_hash(_packet(parent_text="Changed."))
    schema_hash = semantic_digest_schema_hash()
    prompt_hash = semantic_digest_prompt_hash()
    base = {
        "input_hash": first_input,
        "model_id": "deepseek/deepseek-v4-flash",
        "schema_hash": schema_hash,
        "prompt_hash": prompt_hash,
        "runtime_version": "runtime.v1",
    }
    baseline = semantic_digest_cache_key(**base)

    assert first_input != changed_input
    for field_name, replacement in (
        ("input_hash", changed_input),
        ("model_id", "openai/gpt-4o-mini"),
        ("schema_hash", namespace_hash("schema", {"changed": True})),
        ("prompt_hash", namespace_hash("recipe", {"changed": True})),
        ("runtime_version", "runtime.v2"),
    ):
        changed = {**base, field_name: replacement}
        assert semantic_digest_cache_key(**changed) != baseline


def test_unverified_metadata_never_grants_tier1_and_errors_fail_closed(monkeypatch):
    direct = SimpleNamespace(supports_response_schema=lambda *, model: model == "m")
    monkeypatch.setitem(sys.modules, "litellm", direct)
    assert detect_response_schema_capability("m") == CapabilityDecision(
        False, "litellm.supports_response_schema.unverified"
    )

    compat = SimpleNamespace(
        get_model_info=lambda model: {"supports_response_schema": model == "m"}
    )
    monkeypatch.setitem(sys.modules, "litellm", compat)
    assert detect_response_schema_capability("m") == CapabilityDecision(
        False, "litellm.get_model_info.compat.unverified"
    )

    broken = SimpleNamespace(get_model_info=lambda _model: 1 / 0)
    monkeypatch.setitem(sys.modules, "litellm", broken)
    assert detect_response_schema_capability("m") == CapabilityDecision(
        False, "litellm.capability_error"
    )


def test_runtime_verified_registry_can_grant_tier1(monkeypatch):
    route = SimpleNamespace(
        native_json_schema=True,
        route_id="test-route",
        verification_status="accepted",
    )
    registry = SimpleNamespace(
        recipe_version="test-probe.v1",
        resolve=lambda **kwargs: route
        if kwargs == {"model_id": "model", "api_base": "https://provider.invalid/v1"}
        else None,
    )
    monkeypatch.setattr(
        "services.semantic_gateway.load_structured_output_capabilities",
        lambda: registry,
    )

    decision = detect_response_schema_capability(
        "model",
        api_base="https://provider.invalid/v1",
    )

    assert decision == CapabilityDecision(
        True,
        "runtime-capability-registry:test-probe.v1:test-route:accepted",
    )


def test_owner_flash_route_is_runtime_pinned_tier4_after_live_rejection():
    decision = detect_response_schema_capability(
        "deepseek/deepseek-v4-flash",
        api_base="https://api.deepseek.com/v1",
    )
    assert decision.supported is False
    assert decision.source == (
        "runtime-capability-registry:"
        "structured-output-capability.runtime-probe.v1:"
        "deepseek-api__deepseek-v4-flash:provider_rejected"
    )


@pytest.mark.asyncio
async def test_runtime_rejected_flash_override_source_is_persisted_in_provenance():
    gateway = SemanticGateway(
        transport=_FakeTransport([_raw()]),
        store=_FakeStore(),
    )

    result = await gateway.generate(
        packet=_packet(),
        context=_context(),
        config=_config(requested_tier="auto"),
        route=SemanticGatewayRoute(api_base="https://api.deepseek.com/v1"),
    )

    assert result.provenance.capability_tier == "tier4"
    assert result.provenance.capability_detection == (
        "runtime-capability-registry:"
        "structured-output-capability.runtime-probe.v1:"
        "deepseek-api__deepseek-v4-flash:provider_rejected"
    )


@pytest.mark.asyncio
async def test_litellm_transport_forwards_schema_and_secret_route_without_persisting():
    class _FakeLLMService:
        def __init__(self):
            self.kwargs = None

        async def complete_sync(self, **kwargs):
            self.kwargs = kwargs
            return _raw()

    service = _FakeLLMService()
    transport = LiteLLMProxyTransport(service)
    response_format = {"type": "json_object"}
    route = SemanticGatewayRoute(
        api_base="https://provider.invalid",
        api_key="secret-value",
        extra_params={"thinking": {"type": "disabled"}},
    )

    output = await transport.complete(
        messages=[{"role": "system", "content": "x"}],
        response_format=response_format,
        config=_config(),
        route=route,
    )

    assert output == _raw()
    assert service.kwargs["response_format"] is response_format
    assert service.kwargs["api_key"] == "secret-value"
    assert service.kwargs["temperature"] == 0


@pytest.mark.asyncio
async def test_litellm_transport_returns_only_forced_tool_arguments():
    class _FakeLLMService:
        def __init__(self):
            self.kwargs = None

        async def complete_tool_calls(self, **kwargs):
            self.kwargs = kwargs
            return {
                "tool_calls": [
                    {
                        "function": {
                            "name": "submit_semantic_digest",
                            "arguments": _raw(),
                        }
                    }
                ],
                "content": "must-not-be-used",
                "provider_telemetry": {
                    "usage": {
                        "prompt_tokens": 120,
                        "completion_tokens": 80,
                        "total_tokens": 200,
                    },
                    "actual_cost_usd": 0.0004,
                    "cost_source": "litellm.x-litellm-response-cost",
                },
            }

    service = _FakeLLMService()
    transport = LiteLLMProxyTransport(service)
    route = SemanticGatewayRoute(
        api_base="https://provider.invalid",
        api_key="secret-value",
    )

    output = await transport.complete_tool(
        messages=[{"role": "system", "content": "x"}],
        tool=tier3_tool_definition(),
        tool_choice=tier3_tool_choice(),
        config=_config(requested_tier="tier3"),
        route=route,
    )

    assert output == _raw()
    assert service.kwargs["tools"] == [tier3_tool_definition()]
    assert service.kwargs["tool_choice"] == tier3_tool_choice()
    assert service.kwargs["api_base"] == "https://provider.invalid"
    assert service.kwargs["api_key"] == "secret-value"
    assert service.kwargs["overrides"].temperature == 0
    assert service.kwargs["overrides"].max_tokens == 2048
    assert "response_format" not in service.kwargs
    assert transport.call_telemetry == (
        {
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 80,
                "total_tokens": 200,
            },
            "actual_cost_usd": 0.0004,
            "cost_source": "litellm.x-litellm-response-cost",
        },
    )


class _MongoCollection:
    def __init__(self):
        self.rows = {}

    async def find_one(self, query, projection):
        row = self.rows.get(query["_id"])
        if row is None:
            return None
        return {key: value for key, value in row.items() if key != "_id"}

    async def replace_one(self, query, doc, *, upsert):
        assert upsert is True
        self.rows[query["_id"]] = doc


class _MongoDb:
    def __init__(self):
        self.collections = {}
        self.accessed = []

    def __getitem__(self, name):
        self.accessed.append(name)
        return self.collections.setdefault(name, _MongoCollection())


@pytest.mark.asyncio
async def test_mongo_store_separates_accepted_cache_and_dead_letter_from_canonical():
    db = _MongoDb()
    mongo_store = MongoSemanticGatewayStore(db)
    result_store = _FakeStore()
    gateway = SemanticGateway(
        transport=_FakeTransport([_raw()]),
        store=result_store,
        capability_detector=_capability(True),
    )
    result = await gateway.generate(
        packet=_packet(),
        context=_context(),
        config=_config(),
        route=SemanticGatewayRoute(api_key="secret-never-stored"),
    )

    await mongo_store.save_success(result)
    dead_id = await mongo_store.save_dead_letter(
        cache_key=result.provenance.cache_key,
        config=_config(),
        input_hash=result.provenance.input_hash,
        schema_hash=result.provenance.schema_hash,
        prompt_hash=result.provenance.prompt_hash,
        repair_prompt_hash=result.provenance.repair_prompt_hash,
        tier="tier1",
        capability_detection="test.detector",
        raw_outputs=["invalid output"],
        errors=["$.summary: structural missing"],
        attempts=2,
    )

    assert dead_id.startswith("semantic-dlq:")
    assert set(db.collections) == {CACHE_COLLECTION, DEAD_LETTER_COLLECTION}
    assert "semantic_artifacts" not in db.accessed
    cache_doc = next(iter(db.collections[CACHE_COLLECTION].rows.values()))
    dead_doc = next(iter(db.collections[DEAD_LETTER_COLLECTION].rows.values()))
    assert cache_doc["status"] == "accepted_cache"
    assert cache_doc["canonical_write"] is False
    assert dead_doc["status"] == "dead_letter"
    assert dead_doc["canonical_write"] is False
    assert dead_doc["repair_prompt_version"] == REPAIR_PROMPT_VERSION
    assert dead_doc["repair_prompt_hash"] == semantic_digest_repair_prompt_hash()
    assert "secret-never-stored" not in str(db.collections)
