"""Asserting test for the answerability text-coverage fallback."""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.chat_orchestrator import _atoms_covered_by_source_text  # noqa: E402


class _Src:
    def __init__(self, text):
        self.text = text


def test_concept_terms_in_text_are_counted_covered():
    srcs = [_Src("Only four Cobalt Zorblax have ever been recorded, all on Vholm.")]
    covered = _atoms_covered_by_source_text(
        ["concept:cobalt", "concept:zorblax", "concept:dragon", "relationship"],
        srcs,
    )
    assert "concept:cobalt" in covered      # term present in text
    assert "concept:zorblax" in covered
    assert "concept:dragon" not in covered  # term NOT in text
    assert "relationship" not in covered     # operator atom, not a lexical term


def test_punctuation_does_not_block_a_match():
    # "eggs." (trailing period) must still match the term "eggs"
    srcs = [_Src("A single clutch contains exactly thirteen eggs.")]
    covered = _atoms_covered_by_source_text(["concept:eggs", "concept:single"], srcs)
    assert "concept:eggs" in covered
    assert "concept:single" in covered


def test_empty_sources_cover_nothing():
    assert _atoms_covered_by_source_text(["concept:x"], []) == set()
    assert _atoms_covered_by_source_text(["concept:x"], None) == set()


def test_short_terms_ignored():
    # < 3 chars shouldn't match (avoid spurious substring hits)
    covered = _atoms_covered_by_source_text(["concept:ai"], [_Src("the air is cold")])
    assert "concept:ai" not in covered


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
