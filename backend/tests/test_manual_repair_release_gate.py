"""Manual-repair release gate — global deny-by-default closure.

The operator repair path (IngestionService.backfill_graph_failures) routes
through the SAME shared authorization seam as the durable promotion runner
(authorize_canonical_graph_write). A release-policy block is NOT a graph
failure: zero Neo4j calls, no write-attempt increments, candidate state
and retry eligibility preserved, operator identity and gate decision
recorded.

Owner-mandated scenarios (all encoded below):

  1. manual repair + gate off → existing behavior unchanged
  2. manual repair + shadow + no release → write continues, would_block recorded
  3. manual repair + enforce + no release → blocked_no_release, zero Neo4j calls
  4. manual repair + enforce + incomplete release → exact missing conditions
  5. manual repair + enforce + complete passing ReleasePin → write executes once
  6. manual repair + complete ReleaseStamp only → blocked
  7. manual repair + TemporalEnvelope only → blocked
  8. manual repair retry after active release promotion → reconsidered once
  9. direct invocation surface cannot bypass the shared seam (strict AST
     invariant — no script exceptions)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from models.release_state import ReleasePin
from models.release_stamp import empty_release_stamp
from services.ingestion_service import IngestionService

BACKEND_ROOT = Path(__file__).resolve().parents[1]

GATE_MODULE = "services.ingestion.graph_promotion_jobs"

# Strict no-bypass invariant: no production module or script calls the
# canonical graph writers directly except the authorized execution seams.
# Definitions and test fakes are excluded from the scan. Any new caller
# fails this suite until it routes through authorize_canonical_graph_write.
_BACKFILL_ALLOWLIST = frozenset(
    {
        "services/ingestion/graph_backfill.py",   # definition
        "services/ingestion/graph_promotion_jobs.py",  # gated durable seam
        "services/ingestion_service.py",          # gated manual seam
    }
)
_CLAIMS_ALLOWLIST = frozenset(
    {
        "services/ingestion/promote.py",          # definition
        "services/ingestion/graph_promotion_jobs.py",  # gated durable seam
    }
)
# The operator CLI surfaces are gated at the seam level: they must route
# through evaluate_cli_graph_write_gate + the gated service method and must
# NOT import the low-level writers (enforced by the scans below).
_GATED_CLI_SCRIPTS = (
    "scripts/polymath_failed_chunk_backfill.py",
    "scripts/promote_backfilled_relations.py",
    "scripts/polymath_graph_replay_backlog.py",
)


def _full_pin(**overrides) -> ReleasePin:
    base = dict(
        release_id="release-test-1",
        engineering_freeze="passed",
        recoverability="passed",
        git_reproducibility="passed",
        closed_world_annotation="passed",
        calibration="passed",
        held_out_qualification="passed",
        graph_write_promotion="passed",
        extractor_hash_matches=True,
        ontology_hash_matches=True,
        acceptance_policy_hash_matches=True,
        schema_hash_matches=True,
    )
    base.update(overrides)
    return ReleasePin(**base)


class _WriteTracker:
    """Tripwire stand-in for backfill_failed_graph_chunks (Neo4j calls)."""

    def __init__(self):
        self.calls = []

    def install(self, monkeypatch):
        async def fake_backfill(**kwargs):
            self.calls.append(kwargs.get("doc_id"))
            return {
                "status": "done",
                "remaining_failed_chunks": 0,
                "neo4j_flushed": True,
            }

        monkeypatch.setattr(
            "services.ingestion.graph_backfill.backfill_failed_graph_chunks",
            fake_backfill,
        )


def _service():
    """Minimal IngestionService instance for the repair seam."""

    service = IngestionService.__new__(IngestionService)
    service._db = object()
    service._qdrant = None
    service._neo4j = object()
    service.readiness_calls = []

    async def fake_readiness(corpus_id):
        service.readiness_calls.append(corpus_id)
        return None

    service._materialize_corpus_readiness_safely = fake_readiness
    return service


def _set_mode(monkeypatch, mode):
    monkeypatch.setattr(f"{GATE_MODULE}.release_gate_mode", lambda: mode)


async def _repair(service, monkeypatch, **kwargs):
    return await service.backfill_graph_failures(
        corpus_id="corpus-1",
        doc_id="doc-1",
        user_id="operator-1",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Gate off → existing behavior unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_repair_gate_off_preserves_existing_behavior(monkeypatch):
    _set_mode(monkeypatch, "off")
    tracker = _WriteTracker()
    tracker.install(monkeypatch)
    service = _service()

    result = await _repair(service, monkeypatch)

    assert tracker.calls == ["doc-1"]
    assert result["status"] == "done"
    assert "state" not in result
    assert "release_gate_shadow" not in result
    assert "release_gate" not in result


# ---------------------------------------------------------------------------
# 2. Shadow + no release → write continues, would_block recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_repair_shadow_records_would_block_and_continues(monkeypatch):
    _set_mode(monkeypatch, "shadow")
    tracker = _WriteTracker()
    tracker.install(monkeypatch)
    service = _service()

    result = await _repair(service, monkeypatch)

    assert tracker.calls == ["doc-1"]
    assert result["status"] == "done"
    shadow = result["release_gate_shadow"]
    assert shadow["decision"] == "would_block"
    assert shadow["mode"] == "shadow"
    assert "release_bundle_absent" in shadow["missing_conditions"]


# ---------------------------------------------------------------------------
# 3. Enforce + no release → blocked_no_release, zero Neo4j calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_repair_enforce_no_release_blocks_with_zero_writes(
    monkeypatch,
):
    _set_mode(monkeypatch, "enforce")
    tracker = _WriteTracker()
    tracker.install(monkeypatch)
    service = _service()

    result = await _repair(service, monkeypatch)

    # Owner-frozen blocked contract, key-for-key.
    assert result["state"] == "blocked_no_release"
    assert result["retryable"] is True
    assert result["write_attempted"] is False
    assert result["release_registry_entry"] is None
    assert "release_bundle_absent" in result["missing_conditions"]

    # Zero Neo4j calls; a release block is not a graph failure.
    assert tracker.calls == []
    assert "status" not in result  # never classified as a failure result

    # Candidate state preserved: no readiness rematerialization side effect.
    assert service.readiness_calls == []

    # Operator identity and gate decision recorded.
    assert result["operator"] == {
        "user_id": "operator-1",
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
    }
    assert result["release_gate"]["decision"] == "would_block"
    assert result["gate_mode"] == "enforce"


# ---------------------------------------------------------------------------
# 4. Enforce + incomplete release → exact missing conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_repair_enforce_incomplete_release_records_exact_conditions(
    monkeypatch,
):
    _set_mode(monkeypatch, "enforce")
    tracker = _WriteTracker()
    tracker.install(monkeypatch)
    service = _service()

    result = await _repair(
        service, monkeypatch, release=_full_pin(calibration="pending")
    )

    assert result["state"] == "blocked_no_release"
    assert result["missing_conditions"] == ["calibration"]
    assert tracker.calls == []


# ---------------------------------------------------------------------------
# 5. Enforce + complete passing ReleasePin → write executes once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_repair_enforce_complete_pin_executes_once(monkeypatch):
    _set_mode(monkeypatch, "enforce")
    tracker = _WriteTracker()
    tracker.install(monkeypatch)
    service = _service()

    result = await _repair(service, monkeypatch, release=_full_pin())

    assert tracker.calls == ["doc-1"]
    assert result["status"] == "done"
    assert "state" not in result
    # Allowed executions carry no shadow record under enforce.
    assert "release_gate_shadow" not in result


# ---------------------------------------------------------------------------
# 6–7. Neither ReleaseStamp nor TemporalEnvelope can authorize
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_repair_complete_stamp_alone_is_blocked(monkeypatch):
    _set_mode(monkeypatch, "enforce")
    tracker = _WriteTracker()
    tracker.install(monkeypatch)
    service = _service()

    stamp = empty_release_stamp()
    for field in stamp:  # a COMPLETE stamp — still not write authority
        stamp[field] = "release-test-1"
    result = await _repair(service, monkeypatch, release=stamp)

    assert result["state"] == "blocked_no_release"
    assert "release_bundle_absent" in result["missing_conditions"]
    assert tracker.calls == []


@pytest.mark.asyncio
async def test_manual_repair_temporal_envelope_alone_is_blocked(monkeypatch):
    _set_mode(monkeypatch, "enforce")
    tracker = _WriteTracker()
    tracker.install(monkeypatch)
    service = _service()

    class _CompleteLookingEnvelope:
        selection_status = "selected"

    result = await _repair(
        service, monkeypatch, release=_CompleteLookingEnvelope()
    )

    assert result["state"] == "blocked_no_release"
    assert tracker.calls == []


# ---------------------------------------------------------------------------
# 8. Retry after release promotion → same candidate reconsidered once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_repair_retry_after_release_promotion(monkeypatch):
    _set_mode(monkeypatch, "enforce")
    tracker = _WriteTracker()
    tracker.install(monkeypatch)
    service = _service()

    first = await _repair(service, monkeypatch)
    assert first["state"] == "blocked_no_release"
    assert tracker.calls == []

    # Same candidate, release now active → reconsidered and executed once.
    second = await _repair(service, monkeypatch, release=_full_pin())
    assert second["status"] == "done"
    assert tracker.calls == ["doc-1"]  # exactly one write total


# ---------------------------------------------------------------------------
# 9. No-bypass invariant (frozen AST enumeration)
# ---------------------------------------------------------------------------


def _callers_of(module_path: str, symbol: str, roots: tuple[str, ...]) -> set[str]:
    """Files that import ``symbol`` from ``module_path`` (or the module itself)."""

    callers: set[str] = set()
    for root in roots:
        base = BACKEND_ROOT / root
        paths = (
            [base] if base.is_file() else sorted(base.rglob("*.py"))
        )
        for path in paths:
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == module_path:
                    if any(alias.name == symbol for alias in node.names):
                        callers.add(str(path.relative_to(BACKEND_ROOT)))
                elif isinstance(node, ast.Import):
                    if any(alias.name == module_path for alias in node.names):
                        callers.add(str(path.relative_to(BACKEND_ROOT)))
    return callers


_SCAN_ROOTS = ("services", "routers", "scripts", "main.py")


def test_backfill_direct_invocation_cannot_bypass_the_seam():
    """No production module or script calls the backfill writer directly.

    Only the authorized execution seams (durable runner + gated service
    method) may import ``backfill_failed_graph_chunks``. Definitions and
    test fakes are excluded. There are NO script exceptions.
    """

    callers = _callers_of(
        "services.ingestion.graph_backfill",
        "backfill_failed_graph_chunks",
        _SCAN_ROOTS,
    )
    unexpected = {caller for caller in callers if caller not in _BACKFILL_ALLOWLIST}
    assert unexpected == set(), (
        "ungated canonical Neo4j write caller added: "
        f"{sorted(unexpected)} — route it through the shared seam"
    )


def test_claims_writer_direct_invocation_cannot_bypass_the_seam():
    """promote_claims_to_graph is consumed only by the gated durable seam."""

    callers = _callers_of(
        "services.ingestion.promote",
        "promote_claims_to_graph",
        _SCAN_ROOTS,
    )
    unexpected = {caller for caller in callers if caller not in _CLAIMS_ALLOWLIST}
    assert unexpected == set(), (
        f"ungated claims writer caller added: {sorted(unexpected)}"
    )


def test_cli_scripts_route_through_the_shared_seam():
    """Every operator CLI script consumes evaluate_cli_graph_write_gate."""

    for script in _GATED_CLI_SCRIPTS:
        source = (BACKEND_ROOT / script).read_text(encoding="utf-8")
        assert "evaluate_cli_graph_write_gate" in source, (
            f"{script} no longer routes through the shared CLI gate seam"
        )
        assert "backfill_failed_graph_chunks" not in source, (
            f"{script} imports the low-level writer directly"
        )
        assert "promote_claims_to_graph" not in source, (
            f"{script} imports the claims writer directly"
        )
