"""Subprocess-level release-gate tests for the operator CLI scripts.

The three legacy operator scripts are gated through the SAME shared
authorization seam as every other canonical graph write:

    CLI script -> evaluate_cli_graph_write_gate -> authorize_canonical_graph_write
                 -> (allowed) gated service method -> low-level writer

Owner-mandated scenarios per script:

  gate off                          -> previous behavior preserved
  gate shadow + no active release   -> would_block recorded, execution continues
  gate enforce + no active release  -> blocked, zero low-level writer calls
  gate enforce + failing ReleasePin -> exact missing conditions
  gate enforce + passing ReleasePin -> execution proceeds (allowed path)
  ReleaseStamp only                 -> blocked
  TemporalEnvelope only             -> blocked
  malformed registry result         -> blocked
  repeated invocation after success -> idempotent

Enforced-block scenarios deliberately point MONGODB_URI at an unreachable
address: a policy block must happen BEFORE any client connection, state
mutation, counter update, or Neo4j call, so the script exits 77 instantly
instead of failing on the dead connection.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]

POLICY_BLOCK_EXIT_CODE = 77
TEST_CORPUS = "00000000-0000-4000-8000-000000000000"
DEAD_MONGO_URI = (
    "mongodb://127.0.0.1:9/nope"
    "?serverSelectionTimeoutMS=500&connectTimeoutMS=500"
)

SCRIPTS = {
    "failed_chunk": {
        "path": "scripts/polymath_failed_chunk_backfill.py",
        "name": "polymath_failed_chunk_backfill",
        "plan_args": ["--corpus-id", TEST_CORPUS, "--limit", "1"],
    },
    "promote": {
        "path": "scripts/promote_backfilled_relations.py",
        "name": "promote_backfilled_relations",
        "plan_args": ["--corpus", TEST_CORPUS],
    },
    "replay": {
        "path": "scripts/polymath_graph_replay_backlog.py",
        "name": "polymath_graph_replay_backlog",
        "plan_args": ["--corpus-id", TEST_CORPUS, "--limit", "1"],
    },
}

# Subprocess harness: optionally injects a registry result into
# active_release_pin (exactly what the future registry loader will do),
# then executes the script's real main(). Injection goes THROUGH the seam —
# a non-passing injected value is still blocked by the same authority rules.
HARNESS = '''
import importlib.util
import json
import os
import sys

sys.argv = json.loads(os.environ["POLYMATH_TEST_ARGV"])

import services.ingestion.graph_promotion_jobs as gpj
from models.release_state import ReleasePin
from models.release_stamp import empty_release_stamp

_FULL = dict(
    release_id="release-cli-1",
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

_pin_mode = os.environ.get("POLYMATH_TEST_PIN", "none")
if _pin_mode == "failing":
    gpj.active_release_pin = lambda: ReleasePin(**{**_FULL, "calibration": "pending"})
elif _pin_mode == "passing":
    gpj.active_release_pin = lambda: ReleasePin(**_FULL)
elif _pin_mode == "stamp":
    def _stamp():
        stamp = empty_release_stamp()
        for key in stamp:
            stamp[key] = "release-cli-1"
        return stamp
    gpj.active_release_pin = _stamp
elif _pin_mode == "envelope":
    gpj.active_release_pin = lambda: type(
        "Envelope", (), {"selection_status": "selected"}
    )()
elif _pin_mode == "malformed":
    gpj.active_release_pin = lambda: {
        "release_id": "malformed",
        "engineering_freeze": "sort_of",
    }

_spec = importlib.util.spec_from_file_location(
    "legacy_script_under_test", os.environ["POLYMATH_TEST_SCRIPT"]
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_result = _mod.main()
if _result is not None:
    sys.exit(_result)
'''


def _run_script(
    script_key: str,
    *,
    gate_mode: str,
    pin_mode: str = "none",
    apply: bool = False,
    dead_mongo: bool = False,
) -> tuple[int, dict, str]:
    script = SCRIPTS[script_key]
    argv = [script["path"]] + script["plan_args"] + (["--apply"] if apply else [])
    env = os.environ.copy()
    env["GRAPH_PROMOTION_RELEASE_GATE"] = gate_mode
    env["POLYMATH_TEST_PIN"] = pin_mode
    env["POLYMATH_TEST_SCRIPT"] = str(BACKEND_ROOT / script["path"])
    env["POLYMATH_TEST_ARGV"] = json.dumps(argv)
    if dead_mongo:
        env["MONGODB_URI"] = DEAD_MONGO_URI
    proc = subprocess.run(
        [sys.executable, "-c", HARNESS],
        cwd=str(BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    payload: dict = {}
    stdout = proc.stdout.strip()
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {}
    return proc.returncode, payload, proc.stderr


def _assert_frozen_block(payload: dict, *, script_name: str) -> None:
    assert payload["state"] == "blocked_no_release"
    assert payload["retryable"] is True
    assert payload["write_attempted"] is False
    assert payload["release_registry_entry"] is None
    assert payload["gate_mode"] == "enforce"
    assert payload["script"] == script_name
    assert payload["operator"]  # operator identity recorded
    assert payload["release_gate"]["decision"] == "would_block"


# ---------------------------------------------------------------------------
# gate off -> previous behavior preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script_key", sorted(SCRIPTS))
def test_gate_off_preserves_existing_behavior(script_key):
    code, payload, stderr = _run_script(script_key, gate_mode="off")
    assert code == 0, stderr
    assert payload.get("state") != "blocked_no_release"
    assert payload["release_gate"]["mode"] == "off"


# ---------------------------------------------------------------------------
# gate shadow + no active release -> would_block recorded, write continues
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script_key", sorted(SCRIPTS))
def test_shadow_records_would_block_and_continues(script_key):
    code, payload, stderr = _run_script(script_key, gate_mode="shadow")
    assert code == 0, stderr  # execution continues
    assert payload.get("state") != "blocked_no_release"
    gate = payload["release_gate"]
    assert gate["mode"] == "shadow"
    assert gate["decision"] == "would_block"
    assert "release_bundle_absent" in gate["missing_conditions"]


# ---------------------------------------------------------------------------
# gate enforce + no active release -> blocked, zero writer calls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script_key", sorted(SCRIPTS))
def test_enforce_no_release_blocks_before_any_connection(script_key):
    # --apply with an unreachable Mongo URI: only a pre-connection policy
    # block can exit 77 instantly; any writer/connection attempt would fail
    # or time out instead.
    code, payload, stderr = _run_script(
        script_key, gate_mode="enforce", apply=True, dead_mongo=True
    )
    assert code == POLICY_BLOCK_EXIT_CODE, stderr
    _assert_frozen_block(payload, script_name=SCRIPTS[script_key]["name"])
    assert "release_bundle_absent" in payload["missing_conditions"]


# ---------------------------------------------------------------------------
# gate enforce + failing ReleasePin -> exact missing conditions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script_key", sorted(SCRIPTS))
def test_enforce_failing_pin_records_exact_missing_conditions(script_key):
    code, payload, stderr = _run_script(
        script_key,
        gate_mode="enforce",
        pin_mode="failing",
        apply=True,
        dead_mongo=True,
    )
    assert code == POLICY_BLOCK_EXIT_CODE, stderr
    _assert_frozen_block(payload, script_name=SCRIPTS[script_key]["name"])
    assert payload["missing_conditions"] == ["calibration"]


# ---------------------------------------------------------------------------
# gate enforce + fully passing ReleasePin -> allowed path executes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script_key", sorted(SCRIPTS))
def test_enforce_passing_pin_allows_execution(script_key):
    # Empty test corpus -> the allowed run completes through its normal
    # no-work path (noop/APPLIED with zero documents), proving the gate
    # allowed execution instead of blocking.
    code, payload, stderr = _run_script(
        script_key, gate_mode="enforce", pin_mode="passing", apply=True
    )
    assert code == 0, stderr
    assert payload.get("state") != "blocked_no_release"
    assert payload["release_gate"]["decision"] == "would_allow"


# ---------------------------------------------------------------------------
# ReleaseStamp only / TemporalEnvelope only / malformed registry -> blocked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_mode", ["stamp", "envelope", "malformed"], ids=["stamp", "envelope", "malformed"]
)
@pytest.mark.parametrize("script_key", sorted(SCRIPTS))
def test_non_pin_authority_is_blocked(script_key, pin_mode):
    code, payload, stderr = _run_script(
        script_key,
        gate_mode="enforce",
        pin_mode=pin_mode,
        apply=True,
        dead_mongo=True,
    )
    assert code == POLICY_BLOCK_EXIT_CODE, stderr
    _assert_frozen_block(payload, script_name=SCRIPTS[script_key]["name"])
    assert "release_bundle_absent" in payload["missing_conditions"]


# ---------------------------------------------------------------------------
# repeated invocation after success -> idempotent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script_key", sorted(SCRIPTS))
def test_repeated_invocation_is_idempotent(script_key):
    first_code, first_payload, stderr = _run_script(script_key, gate_mode="off")
    assert first_code == 0, stderr
    second_code, second_payload, stderr = _run_script(script_key, gate_mode="off")
    assert second_code == 0, stderr
    assert second_payload.get("state") != "blocked_no_release"
    # Same plan shape both times — repeated runs never re-classify success
    # as failure.
    if "planned_docs" in first_payload:
        assert second_payload["planned_docs"] == first_payload["planned_docs"]
