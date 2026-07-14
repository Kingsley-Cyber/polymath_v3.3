"""P2.5b outbox contract: deterministic keys, legal state machine, retry budget."""

from __future__ import annotations

import pytest

from models.projection_outbox import (
    MAX_ATTEMPTS_DEFAULT,
    OUTBOX_VERSION,
    make_entry,
    outbox_key,
)


def test_key_deterministic_and_op_scoped():
    a = outbox_key("rev:1", "projm:1", "upsert")
    b = outbox_key("rev:1", "projm:1", "upsert")
    assert a == b and a.startswith("outbox:")
    assert outbox_key("rev:1", "projm:1", "delete") != a
    assert outbox_key("rev:1", "projm:2", "upsert") != a
    assert outbox_key("rev:2", "projm:1", "upsert") != a


def test_redelivery_collapses_to_same_entry_id():
    e1 = make_entry("rev:9", "projm:9", "upsert")
    e2 = make_entry("rev:9", "projm:9", "upsert")
    assert e1.outbox_id == e2.outbox_id  # interruption/retry -> no duplicate


def test_happy_path_transitions():
    e = make_entry("rev:1", "projm:1", "upsert")
    e = e.transition("in_flight")
    assert e.attempt_count == 1
    e = e.transition("applied")
    assert e.state == "applied" and e.last_error is None


def test_illegal_transitions_raise():
    e = make_entry("rev:1", "projm:1", "upsert")
    with pytest.raises(ValueError):
        e.transition("applied")  # pending -> applied skips in_flight
    done = e.transition("in_flight").transition("applied")
    with pytest.raises(ValueError):
        done.transition("in_flight")  # applied is terminal


def test_failed_requires_error_and_retry_increments():
    e = make_entry("rev:1", "projm:1", "upsert").transition("in_flight")
    with pytest.raises(ValueError):
        e.transition("failed")  # no error message
    f = e.transition("failed", error="qdrant timeout")
    assert f.state == "failed" and f.last_error == "qdrant timeout"
    r = f.transition("in_flight")
    assert r.attempt_count == 2


def test_dead_letter_after_budget_never_silent():
    e = make_entry("rev:1", "projm:1", "upsert", max_attempts=2)
    e = e.transition("in_flight").transition("failed", error="e1")
    e = e.transition("in_flight").transition("failed", error="e2")
    assert e.state == "dead"  # budget exhausted -> dead, not endless retry
    assert e.last_error == "e2"
    revived = e.transition("in_flight")  # explicit operator revive allowed
    assert revived.attempt_count == 3


def test_version_frozen():
    assert OUTBOX_VERSION == "projection_outbox.v1"
    assert MAX_ATTEMPTS_DEFAULT == 5
