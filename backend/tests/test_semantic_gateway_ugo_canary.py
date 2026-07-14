import json
from argparse import Namespace

import pytest

from models.hash_taxonomy import namespace_hash
from scripts.semantic_gateway_ugo_canary import (
    CANONICAL_CENSUS_SCOPE_RECIPE,
    CANONICAL_CENSUS_SCOPE_RECIPE_HASH,
    CANONICAL_CENSUS_SCOPE_VERSION,
    DEFAULT_PROVIDER_PRICE_CARDS,
    DEFAULT_ROUTE_PARAMETER_CARDS,
    PACKET_SCHEMA_VERSION,
    CanaryError,
    _apply_provider_price_fallback,
    _canonical_store_census_comparison,
    _canonical_store_census_receipt,
    _canonical_store_census_snapshot,
    _FirstResponseParentFaultTransport,
    _interim_claim_id,
    _load_provider_price_card,
    _load_route_parameter_card,
    _packet_from_parent,
    _provenance_complete,
    _sample_evenly,
    _validate_run_args,
    _validate_route_parameter_args,
)


def test_census_scope_v2_reports_cotenant_drift_without_verdict_authority():
    before = _canonical_store_census_snapshot(
        mongo_count=0,
        qdrant_counts={
            "corpus_5a20bc21_naive": 10,
            "polymath_children": 3,
            "polymath_doc_summaries": 684,
            "hermes_memories": 608,
            "mem0migrations": 0,
        },
        neo4j_nodes=100,
        neo4j_relationships=200,
    )
    after = _canonical_store_census_snapshot(
        mongo_count=0,
        qdrant_counts={
            "corpus_5a20bc21_naive": 10,
            "polymath_children": 3,
            "polymath_doc_summaries": 684,
            "hermes_memories": 609,
            "mem0migrations": 0,
        },
        neo4j_nodes=100,
        neo4j_relationships=200,
    )

    receipt = _canonical_store_census_receipt(before, after)

    assert receipt["scope_version"] == CANONICAL_CENSUS_SCOPE_VERSION
    assert receipt["scope_recipe_hash"] == CANONICAL_CENSUS_SCOPE_RECIPE_HASH
    assert receipt["scope_valid"] is True
    assert receipt["protected_exactly_unchanged"] is True
    assert receipt["exactly_unchanged"] is True
    assert receipt["ambient_change_observed"] is True
    assert receipt["ambient_qdrant_collection_deltas"] == {
        "hermes_memories": {"before": 608, "after": 609, "delta": 1}
    }
    assert "hermes_memories" not in before["qdrant_collection_points"]
    assert before["ambient_qdrant_collection_points"]["hermes_memories"] == 608


def test_census_scope_v2_fails_closed_on_polymath_drift_or_bad_recipe():
    before = _canonical_store_census_snapshot(
        mongo_count=0,
        qdrant_counts={"corpus_5a20bc21_graph": 10},
        neo4j_nodes=100,
        neo4j_relationships=200,
    )
    protected_drift = _canonical_store_census_snapshot(
        mongo_count=0,
        qdrant_counts={"corpus_5a20bc21_graph": 11},
        neo4j_nodes=100,
        neo4j_relationships=200,
    )
    comparison = _canonical_store_census_comparison(before, protected_drift)
    assert comparison["scope_valid"] is True
    assert comparison["protected_exactly_unchanged"] is False

    wrong_recipe = dict(before)
    wrong_recipe["census_scope_recipe_hash"] = "sha256:wrong"
    comparison = _canonical_store_census_comparison(before, wrong_recipe)
    assert comparison["scope_valid"] is False
    assert comparison["protected_exactly_unchanged"] is False
    assert CANONICAL_CENSUS_SCOPE_RECIPE_HASH == namespace_hash(
        "scope",
        CANONICAL_CENSUS_SCOPE_RECIPE,
    )


def _parent(**updates):
    row = {
        "parent_id": "parent:one",
        "doc_id": "doc:one",
        "text": "Feedback changes the operating baseline.",
        "summary": "must never enter the interim packet",
        "source_hash": "sha256:source",
        "validation_status": "valid",
        "child_ids": ["child:one", "child:two"],
    }
    row.update(updates)
    return row


def _extraction(chunk_id="child:one", **updates):
    row = {
        "chunk_id": chunk_id,
        "status": "ok",
        "schema_version": "polymath.extract.v1",
        "entities": [
            {
                "canonical_name": "Operating Baseline",
                "entity_type": "CONCEPT",
                "surface_form": "baseline",
                "query_aliases": ["reference", "reference", ""],
                "confidence": 0.91,
                "ignored_provider_field": "not admitted",
            }
        ],
    }
    row.update(updates)
    return row


def test_even_sample_selects_ten_unique_end_to_end_parents():
    rows = [{"parent_id": f"parent:{index:04d}"} for index in range(203)]
    selected = _sample_evenly(rows, 10)

    assert len(selected) == 10
    assert selected[0]["parent_id"] == "parent:0000"
    assert selected[-1]["parent_id"] == "parent:0202"
    assert len({row["parent_id"] for row in selected}) == 10


@pytest.mark.parametrize("count", [0, -1])
def test_even_sample_rejects_nonpositive_count(count):
    with pytest.raises(CanaryError, match="positive"):
        _sample_evenly([{"parent_id": "p"}], count)


def test_even_sample_rejects_insufficient_parents():
    with pytest.raises(CanaryError, match="need 10"):
        _sample_evenly([{"parent_id": "p"}], 10)


def test_interim_claim_identity_is_deterministic_and_source_sensitive():
    first = _interim_claim_id("parent:one", "source:a")
    replay = _interim_claim_id("parent:one", "source:a")
    changed = _interim_claim_id("parent:one", "source:b")

    assert first == replay
    assert first.startswith("interim-claim:")
    assert first != changed


def test_packet_uses_only_valid_parent_text_and_accepted_extraction_entities():
    packet = _packet_from_parent(
        corpus_id="corpus:ugo",
        corpus_name="UGO_CORPUS",
        parent=_parent(),
        extraction_rows=[_extraction(), _extraction("child:two")],
        max_entities=40,
    )

    assert packet.packet["packet_schema_version"] == PACKET_SCHEMA_VERSION
    assert packet.packet["parent_text"] == _parent()["text"]
    assert "summary" not in packet.packet
    assert packet.packet["claims"][0]["text"] == _parent()["text"]
    assert packet.packet["claims"][0]["claim_id"].startswith("interim-claim:")
    assert packet.packet["extraction_entities"] == [
        {
            "canonical_name": "Operating Baseline",
            "entity_type": "CONCEPT",
            "surface_form": "baseline",
            "query_aliases": ["reference"],
            "confidence": 0.91,
        }
    ]
    assert packet.source_child_count == 2
    assert packet.entity_count == 1
    assert packet.context.parent_id == "parent:one"
    assert packet.context.claims[0].claim_id == packet.packet["claims"][0]["claim_id"]


def test_packet_filters_unaccepted_extractions_and_requires_one_accepted_child():
    with pytest.raises(CanaryError, match="no accepted extraction child"):
        _packet_from_parent(
            corpus_id="corpus:ugo",
            corpus_name="UGO_CORPUS",
            parent=_parent(),
            extraction_rows=[_extraction(status="failed")],
            max_entities=40,
        )


def test_packet_requires_valid_parent_status():
    with pytest.raises(CanaryError, match="validation_status=valid"):
        _packet_from_parent(
            corpus_id="corpus:ugo",
            corpus_name="UGO_CORPUS",
            parent=_parent(validation_status="candidate"),
            extraction_rows=[_extraction()],
            max_entities=40,
        )


@pytest.mark.asyncio
async def test_fault_transport_changes_only_first_real_response_parent_id():
    class Delegate:
        def __init__(self):
            self.calls = []

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            return json.dumps({"parent_id": "parent:one", "summary": "safe"})

    delegate = Delegate()
    transport = _FirstResponseParentFaultTransport(delegate)
    first = json.loads(
        await transport.complete(response_format={"type": "json_schema"})
    )
    second = json.loads(
        await transport.complete(response_format={"type": "json_schema"})
    )

    assert first["parent_id"] == "fault-injected:wrong-parent"
    assert second["parent_id"] == "parent:one"
    assert transport.calls == 2
    assert transport.fault_injected is True
    assert delegate.calls[0]["response_format"] == delegate.calls[1]["response_format"]


@pytest.mark.asyncio
async def test_fault_transport_does_not_fabricate_json_when_provider_output_is_invalid():
    class Delegate:
        async def complete(self, **_kwargs):
            return "not-json"

    transport = _FirstResponseParentFaultTransport(Delegate())
    assert await transport.complete() == "not-json"
    assert transport.fault_injected is False


@pytest.mark.asyncio
async def test_fault_transport_applies_same_single_fault_to_tool_arguments():
    class Delegate:
        def __init__(self):
            self.calls = 0

        async def complete_tool(self, **_kwargs):
            self.calls += 1
            return json.dumps({"parent_id": "parent:one", "summary": "safe"})

    delegate = Delegate()
    transport = _FirstResponseParentFaultTransport(delegate)
    first = json.loads(await transport.complete_tool())
    second = json.loads(await transport.complete_tool())

    assert first["parent_id"] == "fault-injected:wrong-parent"
    assert second["parent_id"] == "parent:one"
    assert delegate.calls == 2
    assert transport.fault_injected is True


def test_receipt_provenance_field_check_fails_closed_on_wrong_shape():
    class Provenance:
        def model_dump(self, mode):
            assert mode == "python"
            return {"model_id": "m"}

    class Result:
        provenance = Provenance()

    assert _provenance_complete(Result()) is False


def _run_args(**updates):
    values = {
        "count": 10,
        "force_repair_index": 0,
        "concurrency": 2,
        "canary_tier": "tier4",
        "tier1_provider_blocked": True,
    }
    values.update(updates)
    return Namespace(**values)


def test_provider_blocked_path_requires_explicit_tier3_or_tier4_contract():
    _validate_run_args(_run_args())
    _validate_run_args(_run_args(canary_tier="tier3"))

    with pytest.raises(CanaryError, match="explicit"):
        _validate_run_args(_run_args(tier1_provider_blocked=False))
    with pytest.raises(CanaryError, match="only as Tier3/Tier4"):
        _validate_run_args(_run_args(canary_tier="tier1"))


def test_test_fixture_contains_no_credential_shaped_value():
    fixture = json.dumps({"parent": _parent(), "extraction": _extraction()})
    assert "sk-" not in fixture
    assert "api_key" not in fixture
    assert namespace_hash("body", {"fixture": fixture}).startswith("sha256:")


def test_versioned_longcat_price_card_is_route_exact_and_secret_free():
    card = _load_provider_price_card(
        DEFAULT_PROVIDER_PRICE_CARDS,
        route_id="longcat-api__longcat-2.0",
        model_id="openai/LongCat-2.0",
        api_base="https://api.longcat.chat/openai/v1",
    )

    assert card.uncached_input_usd == 0.75
    assert card.output_usd == 2.95
    assert card.receipt_source.endswith("published-list-uncached-input")
    raw = DEFAULT_PROVIDER_PRICE_CARDS.read_text(encoding="utf-8")
    assert "api_key" not in raw
    assert "ciphertext" not in raw


def test_provider_price_fallback_uses_usage_and_names_versioned_source():
    card = _load_provider_price_card(
        DEFAULT_PROVIDER_PRICE_CARDS,
        route_id="longcat-api__longcat-2.0",
        model_id="openai/LongCat-2.0",
        api_base="https://api.longcat.chat/openai/v1",
    )

    rows = _apply_provider_price_fallback(
        [
            {
                "usage": {
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 1_000_000,
                    "total_tokens": 2_000_000,
                },
                "actual_cost_usd": None,
                "cost_source": None,
            }
        ],
        card,
    )

    assert rows[0]["actual_cost_usd"] == 3.70
    assert rows[0]["cost_source"] == card.receipt_source


def test_versioned_route_parameter_card_freezes_only_completion_cap_change():
    card = _load_route_parameter_card(
        DEFAULT_ROUTE_PARAMETER_CARDS,
        route_id="longcat-api__longcat-2.0",
        model_id="openai/LongCat-2.0",
        api_base="https://api.longcat.chat/openai/v1",
    )

    assert card.capability_tier == "tier3"
    assert card.temperature == 0
    assert card.thinking == "disabled"
    assert card.max_tokens == 8192
    assert card.recanary_target_packets == 10
    assert card.recanary_minimum_accepted == 9
    assert card.recanary_max_cost_usd == 1.0


def test_recanary_arguments_must_match_versioned_route_parameters():
    card = _load_route_parameter_card(
        DEFAULT_ROUTE_PARAMETER_CARDS,
        route_id="longcat-api__longcat-2.0",
        model_id="openai/LongCat-2.0",
        api_base="https://api.longcat.chat/openai/v1",
    )
    args = Namespace(
        canary_tier="tier3",
        max_tokens=8192,
        timeout_seconds=180.0,
        runtime_version=card.runtime_version,
        tokenizer_id=card.tokenizer_id,
        count=10,
        max_provider_cost_usd=1.0,
    )

    _validate_route_parameter_args(args, card)
    args.max_tokens = 4096
    with pytest.raises(CanaryError, match="max_tokens"):
        _validate_route_parameter_args(args, card)
