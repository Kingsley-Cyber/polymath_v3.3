"""Asserting tests for B1 hydration-mode text assembly (pure)."""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.retriever.hydrate import _assemble_hydrated_text  # noqa: E402


def test_parent_mode_returns_full_parent_body():
    out = _assemble_hydrated_text(
        "parent", child_text="precise passage", parent_text="full parent body", summary="s"
    )
    assert out == "full parent body"


def test_child_summary_keeps_child_and_appends_summary_only():
    out = _assemble_hydrated_text(
        "child_summary",
        child_text="the precise passage",
        parent_text="the full 1200-token parent body",
        summary="this section covers X",
    )
    assert "the precise passage" in out
    assert "this section covers X" in out
    # the bloaty parent body must NOT be in the prompt under this mode
    assert "full 1200-token parent body" not in out


def test_child_summary_without_summary_returns_just_child():
    out = _assemble_hydrated_text(
        "child_summary", child_text="precise", parent_text="full", summary=""
    )
    assert out == "precise"


def test_child_summary_falls_back_to_parent_when_child_empty():
    out = _assemble_hydrated_text(
        "child_summary", child_text="   ", parent_text="full parent", summary="s"
    )
    assert out == "full parent"


def test_unknown_mode_defaults_to_parent_behaviour():
    assert _assemble_hydrated_text("nonsense", child_text="c", parent_text="p", summary="s") == "p"


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
