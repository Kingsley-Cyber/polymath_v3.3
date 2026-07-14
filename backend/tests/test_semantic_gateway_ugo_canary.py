import json
from argparse import Namespace

import pytest

from models.hash_taxonomy import namespace_hash
from scripts.semantic_gateway_ugo_canary import (
    PACKET_SCHEMA_VERSION,
    CanaryError,
    _FirstResponseParentFaultTransport,
    _interim_claim_id,
    _packet_from_parent,
    _provenance_complete,
    _sample_evenly,
    _validate_run_args,
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
