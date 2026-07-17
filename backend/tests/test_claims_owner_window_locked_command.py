from __future__ import annotations

from contextlib import contextmanager

from scripts import run_claims_owner_window_locked_command as locked


def test_locked_command_exports_exact_outer_window(monkeypatch):
    observed = {}

    @contextmanager
    def fake_lock(args):
        args.window_nonce = "claims-window-nonce-0001"
        args.window_not_before_utc = "2026-07-18T00:00:00+00:00"
        yield

    class Completed:
        returncode = 7

    def fake_run(command, *, env, check):
        observed["command"] = command
        observed["env"] = env
        observed["check"] = check
        return Completed()

    monkeypatch.setattr(locked, "_lock_context", fake_lock)
    monkeypatch.setattr(locked.subprocess, "run", fake_run)

    exit_code = locked.main(
        [
            "--lock-owner",
            "claims-window",
            "--",
            "python",
            "window.py",
        ]
    )

    assert exit_code == 7
    assert observed["command"] == ["python", "window.py"]
    assert observed["check"] is False
    assert observed["env"]["POLYMATH_EVAL_LOCK_OWNER"] == "claims-window"
    assert observed["env"]["POLYMATH_EVAL_WINDOW_NONCE"] == "claims-window-nonce-0001"
    assert (
        observed["env"]["POLYMATH_EVAL_WINDOW_NOT_BEFORE_UTC"]
        == "2026-07-18T00:00:00+00:00"
    )
    assert observed["env"]["POLYMATH_EVAL_OUTER_LOCK_ATTESTED"] == "1"
