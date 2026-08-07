"""Step 3 acceptance tests — release-identity stamps (non-authoritative).

Owner acceptance contract: stamps are attached to extraction output,
promotion records/job attempts, and query traces WITHOUT permitting or
denying any behavior. The categorical release state (models/release_state.py)
remains the sole normative authority; percentage estimates never enter
ReleasePin, readiness, certificate, or promotion decisions.

Requirement mapping:
  1. Extraction output carries the release stamp.
  2. Promotion records preserve the exact extraction stamp.
  3. Query traces report the releases actually read.
  4. Missing pins remain null/unknown rather than being inferred.
  5. Conflicting pins remain visible rather than silently normalized.
  6. No route is blocked because of a pin yet.
  7. No graph write is enabled because of a pin yet.
  9. Existing serialized contracts remain backward compatible.
(Requirement 8 — frozen extraction regression 451 passed — is verified by
running tests/extraction separately.)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from models.release_stamp import (
    RELEASE_STAMP_FIELDS,
    ReleaseStamp,
    copy_stamp,
    current_release_stamp,
    empty_release_stamp,
    observed_release_stamps,
    stamp_conflicts,
    stamp_is_empty,
    stamp_status,
)

# ---------------------------------------------------------------------------
# Stamp contract shape
# ---------------------------------------------------------------------------

_OWNER_STAMP_SHAPE = {
    "extractor_release": None,
    "extractor_commit_sha": None,
    "extractor_config_hash": None,
    "ontology_release": None,
    "ontology_hash": None,
    "acceptance_policy_release": None,
    "acceptance_policy_hash": None,
    "schema_release": None,
    "schema_hash": None,
    "promotion_release": None,
    "certificate_id": None,
}


def test_stamp_shape_matches_owner_contract_exactly():
    assert tuple(RELEASE_STAMP_FIELDS) == tuple(_OWNER_STAMP_SHAPE)
    assert empty_release_stamp() == _OWNER_STAMP_SHAPE
    assert ReleaseStamp().model_dump() == _OWNER_STAMP_SHAPE


def test_stamp_is_json_serializable():
    import json

    stamp = empty_release_stamp()
    stamp["extractor_release"] = "extraction-core-v1.0.0"
    assert json.loads(json.dumps(stamp)) == stamp


# ---------------------------------------------------------------------------
# Owner-frozen four-state stamp semantics
# ---------------------------------------------------------------------------


def test_absent_key_means_legacy_or_uninstrumented():
    assert stamp_status(None) == "legacy_uninstrumented"
    assert stamp_status("not-a-dict") == "legacy_uninstrumented"


def test_all_null_stamp_means_instrumented_pre_registry():
    assert stamp_status(empty_release_stamp()) == "pre_registry"
    assert stamp_status({}) == "pre_registry"  # present, nothing known


def test_partial_stamp_never_allows_inference_of_missing_pins():
    partial = {"extractor_release": "extraction-core-v1.0.0"}
    assert stamp_status(partial) == "partial"
    copied = copy_stamp(partial)
    # No missing value is filled in — partial stays partial downstream.
    assert stamp_status(copied) == "partial"
    assert set(copied) == {"extractor_release"}


def test_complete_stamp_is_recorded_but_still_non_authoritative():
    complete = {field: f"{field}-v1" for field in RELEASE_STAMP_FIELDS}
    assert stamp_status(complete) == "complete"
    # Completeness grants no authority: graph writes still need the
    # categorical ReleasePin chain, never an artifact stamp.
    from models.release_state import graph_write_allowed

    assert graph_write_allowed(None) is False


def test_extra_keys_do_not_make_a_stamp_complete():
    weird = dict(_OWNER_STAMP_SHAPE)
    weird["legacy_note"] = "extra"
    assert stamp_status(weird) == "pre_registry"  # canonical pins still null


# ---------------------------------------------------------------------------
# Req 1 — extraction output carries the release stamp
# ---------------------------------------------------------------------------


def test_extraction_row_stamping_attaches_full_shape():
    from services.ingestion.extraction_jobs import _stamp_extraction_row_identity

    row: dict = {"chunk_id": "doc-1_0001"}
    _stamp_extraction_row_identity(
        row,
        chunk={"chunk_id": "doc-1_0001", "text": "FACS scores movement."},
        doc=None,
        contract_hash="contract-hash-1",
    )
    assert row["release_stamp"] == _OWNER_STAMP_SHAPE
    # Identity fields remain untouched alongside the new stamp.
    assert row["extraction_contract_hash"] == "contract-hash-1"
    assert row["stage_identity"]["identity_version"] == "stage_identity.v1"


def test_chunk_extraction_contract_accepts_and_defaults_stamp():
    from models.contracts import ChunkExtraction

    base = dict(
        extractor="gliner_glirel_local",
        corpus_id="c1",
        doc_id="d1",
        chunk_id="ch1",
        parent_id="p1",
    )
    # Req 9 — historical shape (no stamp) still validates.
    assert ChunkExtraction(**base).release_stamp is None
    stamped = ChunkExtraction(
        **base,
        release_stamp={"extractor_release": "extraction-core-v1.0.0"},
    )
    assert stamped.release_stamp.extractor_release == "extraction-core-v1.0.0"
    assert stamped.release_stamp.ontology_release is None


# ---------------------------------------------------------------------------
# Req 2 — promotion records preserve the exact extraction stamp
# ---------------------------------------------------------------------------

_STAMPED_ROW = {
    "schema_version": "polymath.extract.v1",
    "corpus_id": "k",
    "doc_id": "d",
    "chunk_id": "c",
    "entities": [{"canonical_name": "TensorFlow", "query_aliases": []}],
    "relations": [],
    "facts": [],
    "release_stamp": {
        "extractor_release": "extraction-core-v1.0.0",
        "extractor_commit_sha": "86149227db703c097aad586e4a18018185e9f742",
        # deliberately non-canonical extra key: must survive verbatim
        "legacy_note": "pre-registry row",
    },
}


@pytest.fixture()
def promote_fn():
    """promote() lives behind Docker-only imports (neo4j, pydantic_settings).

    Repo convention: skip outside the backend container rather than fake the
    settings stack (see tests/test_promote.py header).
    """

    pytest.importorskip(
        "services.ingestion.promote",
        reason="promotion leg requires the Docker backend environment",
    )
    from services.ingestion.promote import promote

    return promote


def test_promotion_copies_extraction_stamp_verbatim(promote_fn):
    delta = promote_fn(dict(_STAMPED_ROW))
    assert delta["release_stamp"] == _STAMPED_ROW["release_stamp"]
    # Verbatim means verbatim: the extra key is preserved, not dropped.
    assert delta["release_stamp"]["legacy_note"] == "pre-registry row"
    # And the copy is not the same object (no shared mutable state).
    assert delta["release_stamp"] is not _STAMPED_ROW["release_stamp"]


def test_promotion_without_stamp_adds_no_key(promote_fn):
    historical = dict(_STAMPED_ROW)
    historical.pop("release_stamp")
    delta = promote_fn(historical)
    assert "release_stamp" not in delta  # unknown stays unknown, not inferred


def test_graph_promotion_attempt_carries_extraction_stamp():
    from services.ingestion.graph_promotion_jobs import (
        classify_graph_promotion_candidate,
    )

    row = {
        "corpus_id": "c1",
        "doc_id": "d1",
        "user_id": "u1",
        "filename": "doc.pdf",
        "write_state": {
            "neo4j_written": True,
            "verified": False,
            "verify_errors": ["neo4j has_chunk mismatch"],
        },
        "ghost_b_failure_count": 0,
        "failure_rows": 0,
        "staged_extractions": 3,
        "child_chunks": 3,
        "parent_chunks": 1,
        "extraction_artifact_ids": ["art-1"],
        "release_stamp": _STAMPED_ROW["release_stamp"],
    }
    candidate = classify_graph_promotion_candidate(row)
    assert candidate is not None
    assert candidate["release_stamp"] == _STAMPED_ROW["release_stamp"]

    unstamped = dict(row)
    unstamped.pop("release_stamp")
    assert classify_graph_promotion_candidate(unstamped)["release_stamp"] is None


# ---------------------------------------------------------------------------
# Req 3 — query traces report the releases actually read
# ---------------------------------------------------------------------------


@dataclass
class FakeReadChunk:
    """Duck-typed hydrated chunk: metadata carries the promoted payload."""

    chunk_id: str = "c1"
    metadata: dict = field(default_factory=dict)


def test_observed_stamps_report_what_was_read_in_order():
    stamp_a = {"extractor_release": "extraction-core-v1.0.0"}
    stamp_b = {"extractor_release": "extraction-core-v0.9.0"}
    chunks = [
        FakeReadChunk(metadata={"release_stamp": stamp_a}),
        FakeReadChunk(metadata={}),  # unstamped chunk contributes nothing
        FakeReadChunk(metadata={"release_stamp": stamp_b}),
    ]
    assert observed_release_stamps(chunks) == [stamp_a, stamp_b]


def test_query_ir_carries_release_stamp():
    from models.query_ir import QueryIR

    class FakePlan:
        version = "query_plan.v2"
        original_query = "q"
        standalone_query = "q"
        complexity = "simple"
        lanes = ()

    ir = QueryIR.from_plan(FakePlan())
    assert ir.release_stamp is None  # unknown by default, never inferred

    stamped = QueryIR.from_plan(
        FakePlan(),
        release_stamp=ReleaseStamp(extractor_release="extraction-core-v1.0.0"),
    )
    assert stamped.release_stamp.extractor_release == "extraction-core-v1.0.0"


def test_three_query_release_concepts_stay_separate():
    """release_pins (policy) vs release_stamp (recorded) vs
    diagnostics.release_stamps_read (observed) must never merge or
    overwrite one another."""

    from models.query_ir import QueryIR

    class FakePlan:
        version = "query_plan.v2"
        original_query = "q"
        standalone_query = "q"
        complexity = "simple"
        lanes = ()

    ir = QueryIR.from_plan(
        FakePlan(),
        release_pins={"extractor_release": "policy-v2.0.0"},
        release_stamp=ReleaseStamp(extractor_release="recorded-v1.0.0"),
    )
    # Both survive independently — no overwrite in either direction.
    assert ir.release_pins["extractor_release"] == "policy-v2.0.0"
    assert ir.release_stamp.extractor_release == "recorded-v1.0.0"

    # Observed evidence stamps live in retriever diagnostics, not QueryIR:
    # they cannot contaminate the IR's recorded identity.
    observed = observed_release_stamps(
        [FakeReadChunk(metadata={"release_stamp": {"extractor_release": "evidence-v0.9.0"}})]
    )
    assert observed[0]["extractor_release"] == "evidence-v0.9.0"
    assert ir.release_stamp.extractor_release == "recorded-v1.0.0"
    assert ir.release_pins["extractor_release"] == "policy-v2.0.0"


# ---------------------------------------------------------------------------
# Req 4 — missing pins remain null/unknown rather than being inferred
# ---------------------------------------------------------------------------


def test_current_stamp_is_all_null_until_registry_exists():
    stamp = current_release_stamp()
    assert stamp == _OWNER_STAMP_SHAPE
    assert stamp_is_empty(stamp) is True
    assert all(value is None for value in stamp.values())


def test_copy_stamp_never_infers_values():
    assert copy_stamp(None) is None
    assert copy_stamp({}) == {}
    assert copy_stamp("extraction-core-v1.0.0") is None  # not a dict: unknown
    partial = {"extractor_release": "extraction-core-v1.0.0"}
    copied = copy_stamp(partial)
    assert copied == partial
    assert "ontology_release" not in copied  # no shape-completion inference


# ---------------------------------------------------------------------------
# Req 5 — conflicting pins remain visible rather than silently normalized
# ---------------------------------------------------------------------------


def test_stamp_conflicts_are_reported_not_normalized():
    a = {"extractor_release": "extraction-core-v1.0.0", "ontology_release": "o.v1"}
    b = {"extractor_release": "extraction-core-v0.9.0", "ontology_release": "o.v1"}
    assert stamp_conflicts(a, b) == ("extractor_release",)
    assert stamp_conflicts(a, a) == ()
    assert stamp_conflicts(a, None) == ()  # absent ≠ conflicting


def test_observed_stamps_keep_conflicts_visible():
    # Two chunks stamped by different extractor releases: the trace must
    # carry BOTH, in read order — no merge, no winner-picking.
    chunks = [
        FakeReadChunk(metadata={"release_stamp": {"extractor_release": "v1.0.0"}}),
        FakeReadChunk(metadata={"release_stamp": {"extractor_release": "v0.9.0"}}),
    ]
    observed = observed_release_stamps(chunks)
    assert [s["extractor_release"] for s in observed] == ["v1.0.0", "v0.9.0"]


# ---------------------------------------------------------------------------
# Req 6 + 7 — stamps neither block routes nor enable graph writes
# ---------------------------------------------------------------------------


def test_no_route_is_blocked_because_of_a_pin():
    from models.release_state import ReadinessDecision

    # Same corpus, same artifacts, with and without stamped artifacts in the
    # present set: readiness is artifact-driven, stamp-blind.
    base = ReadinessDecision.decide(
        corpus_id="c1", route="entity_lookup", present_artifacts=set()
    )
    stamped = ReadinessDecision.decide(
        corpus_id="c1",
        route="entity_lookup",
        present_artifacts=set(),
        release_pins={"extractor_release": "extraction-core-v1.0.0"},
    )
    assert base.allowed == stamped.allowed is False
    assert base.missing_artifacts == stamped.missing_artifacts


def test_no_graph_write_is_enabled_because_of_a_pin():
    from models.release_state import ReadinessDecision, graph_write_allowed

    # graph_write_allowed consumes ONLY the categorical ReleasePin — stamps
    # are not an input, so no stamp can enable a write.
    assert graph_write_allowed(None) is False

    # A fully stamped artifact set still cannot open the graph_write route
    # without the categorical release bundle.
    decision = ReadinessDecision.decide(
        corpus_id="c1",
        route="graph_write",
        present_artifacts={"extraction_qualification", "graph_write_promotion"},
        release_pins={
            "extractor_release": "extraction-core-v1.0.0",
            "ontology_release": "ontology.v1",
        },
    )
    assert decision.allowed is False


# ---------------------------------------------------------------------------
# Req 9 — backward compatibility of serialized contracts
# ---------------------------------------------------------------------------


def test_release_stamp_round_trips_through_json():
    stamp = ReleaseStamp(
        extractor_release="extraction-core-v1.0.0",
        certificate_id="cert-1",
    )
    restored = ReleaseStamp.model_validate_json(stamp.model_dump_json())
    assert restored == stamp


def test_historical_extraction_row_shape_still_validates():
    from models.contracts import ChunkExtraction

    # The pre-Step-3 serialized shape — no release_stamp key anywhere.
    row = {
        "schema_version": "polymath.extract.v2",
        "extractor": "cloud_llm",
        "corpus_id": "c1",
        "doc_id": "d1",
        "chunk_id": "ch1",
        "parent_id": "p1",
        "entities": [],
        "relations": [],
        "facts": [],
    }
    parsed = ChunkExtraction.model_validate(row)
    assert parsed.release_stamp is None
    # Defaults fill in (text=""); the serialized shape gains no stamp key.
    assert "release_stamp" not in parsed.model_dump(exclude_none=True)
    assert parsed.model_dump(exclude_none=True) == {**row, "text": ""}
