from datetime import datetime, timedelta

from services.ingestion.repair_scheduler import backoff_decision


def test_scheduler_skips_corpus_with_no_actionable_gaps():
    result = backoff_decision(
        snapshot={"actionable_total": 0, "fingerprint": "healthy"},
        state=None,
        now=datetime(2026, 1, 1),
    )

    assert result == {
        "should_run": False,
        "reason": "no_actionable_gaps",
        "changed": True,
    }


def test_scheduler_resets_when_gap_fingerprint_changes():
    now = datetime(2026, 1, 1)
    result = backoff_decision(
        snapshot={"actionable_total": 1, "fingerprint": "new"},
        state={
            "gap_fingerprint": "old",
            "next_eligible_at": now + timedelta(hours=1),
        },
        now=now,
    )

    assert result["should_run"] is True
    assert result["reason"] == "gaps_changed"


def test_scheduler_honors_idle_backoff_for_unchanged_gap():
    now = datetime(2026, 1, 1)
    result = backoff_decision(
        snapshot={"actionable_total": 1, "fingerprint": "same"},
        state={
            "gap_fingerprint": "same",
            "next_eligible_at": now + timedelta(minutes=10),
        },
        now=now,
    )

    assert result["should_run"] is False
    assert result["reason"] == "idle_backoff"
