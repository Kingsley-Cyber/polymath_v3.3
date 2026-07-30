"""Step 0b — GHOST_B_RELATION_ENGINE must never be a silent no-op.

Before this test, the env var was read into a comment and ignored:
`_rel_engine = "spacy"  # was: os.environ.get(...)`. Anyone setting
GHOST_B_RELATION_ENGINE=glirel got a no-op with NO signal, and the roadmap
documented a rollback ("set =glirel, recreate") that could never fire.

GLiREL is RETIRED from production (owner 2026-07-29/30), so the correct
behavior is not to honor the switch — it is to FAIL LOUD instead of lying.

Portable: pure logic, no live stack, no ML imports.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "services" / "ghost_b_local.py"


def _source() -> str:
    return _SRC.read_text()


def test_unsupported_engine_raises_rather_than_silently_ignoring():
    """A non-spacy engine request must raise, not no-op."""
    src = _source()
    assert "_requested_engine" in src, (
        "GHOST_B_RELATION_ENGINE is not read at all — the switch is silently "
        "dead again. It must be read and rejected loudly."
    )
    # The guard must raise, and must name the variable in the message.
    guard = re.search(
        r"if _requested_engine and _requested_engine != \"spacy\":\s*\n\s*raise RuntimeError\(",
        src,
    )
    assert guard is not None, (
        "Expected a fail-loud guard rejecting any engine other than 'spacy'."
    )


def test_glirel_is_not_eagerly_loaded_in_the_production_path():
    """The retired 1.87GB model must not be loaded on every extraction call.

    _get_glirel() is KEPT for offline R0 candidate suggestion, but nothing in
    _extract_raw may call it.
    """
    src = _source()
    # Locate the _extract_raw body (production path).
    start = src.index("def _extract_raw")
    body = src[start:]
    # Strip comments before scanning — the comment block explains the removal.
    code_only = "\n".join(
        line.split("#")[0] for line in body.splitlines()
    )
    assert "_get_glirel()" not in code_only, (
        "_extract_raw eagerly loads the RETIRED GLiREL model (1.87 GB resident "
        "for nothing). Load must stay removed; loaders are offline-only."
    )
    assert "_get_glirel_cpu()" not in code_only, (
        "_extract_raw references the retired GLiREL CPU loader."
    )


def test_no_unreachable_glirel_branch_remains():
    """The dead `else:` GLiREL branch must stay deleted, not commented out."""
    src = _source()
    start = src.index("def _extract_raw")
    body = src[start:]
    code_only = "\n".join(line.split("#")[0] for line in body.splitlines())
    assert "glirel.extract_chunks" not in code_only, (
        "Unreachable GLiREL extraction branch is back in _extract_raw."
    )


def test_timing_key_names_are_honest():
    """Stage C timing must not be labeled 'glirel' when GLiREL never runs."""
    src = _source()
    assert '"relations_s"' in src, "Stage C timing key should be relations_s."
    assert '"glirel_s"' not in src, (
        "Timing key still calls Stage C 'glirel_s' though GLiREL is retired."
    )


if __name__ == "__main__":  # pragma: no cover - manual run
    import sys

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
