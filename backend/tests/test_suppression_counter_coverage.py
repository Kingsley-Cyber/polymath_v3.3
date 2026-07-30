"""R-pre — every named suppression counter must survive to durable storage.

The repo law is "never drop silently: every suppression increments a named
counter". Before R-pre that law was honored inside dep_path_extractor and
DEFEATED at the emit boundary:

  - ghost_b_local.py hand-maintained a PARTIAL copy of the key list. It omitted
    all three P2 structural guards (suppressed_multi_clause /
    suppressed_conjunct_crossing / suppressed_exception_boundary), so those were
    incremented into a dict that never declared them.
  - Only 4 counters (entity/relation/evidence/fact drop) were emitted on
    ExtractionResult. The other 10+ were garbage-collected.

Net effect: nobody could measure what the guards cost, even in principle.

These tests fail if anyone adds a suppression rule without publishing it, or
re-introduces a hand-maintained key list.

Portable: pure logic + AST, no live stack, no ML imports, no spaCy model load.
"""

from __future__ import annotations

import ast
import re
from dataclasses import fields
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
_EXTRACTOR = _BACKEND / "services" / "extraction" / "dep_path_extractor.py"
_ADAPTER = _BACKEND / "services" / "extraction" / "spacy_relation_adapter.py"
_GHOST_B_LOCAL = _BACKEND / "services" / "ghost_b_local.py"
_GHOST_B = _BACKEND / "services" / "ghost_b.py"
_WORKER = _BACKEND / "services" / "ingestion" / "worker.py"


def _canonical_keys() -> set[str]:
    """Parse ALL_COUNTER_KEYS out of the extractor without importing spaCy."""
    tree = ast.parse(_EXTRACTOR.read_text())
    groups: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        name = node.target.id
        if not name.endswith("_KEYS") or node.value is None:
            continue
        if isinstance(node.value, ast.Tuple):
            groups[name] = [
                el.value for el in node.value.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            ]
    assert groups, "No *_KEYS tuples found in dep_path_extractor."
    combined: set[str] = set()
    for name, keys in groups.items():
        if name != "ALL_COUNTER_KEYS":
            combined |= set(keys)
    return combined


def _incremented_keys() -> set[str]:
    """Every key actually passed to _inc(...) in the extractor."""
    src = _EXTRACTOR.read_text()
    return set(re.findall(r'_inc\(\s*suppression_counters\s*,\s*"([a-z_]+)"', src))


def _adapter_written_keys() -> set[str]:
    """Counter keys the adapter writes into the per-chunk dict."""
    src = _ADAPTER.read_text()
    return set(re.findall(r'_supp_ctr\[\s*"([a-z_]+)"\s*\]', src))


def test_every_incremented_key_is_declared_canonical():
    """A suppression rule that increments an undeclared key is invisible."""
    missing = _incremented_keys() - _canonical_keys()
    assert not missing, (
        f"These counters are incremented but NOT declared in the extractor's "
        f"canonical *_KEYS tuples, so they will not be zero-initialized and may "
        f"never reach storage: {sorted(missing)}"
    )


def test_every_adapter_key_is_declared_canonical():
    missing = _adapter_written_keys() - _canonical_keys()
    assert not missing, (
        f"Adapter writes undeclared counter keys: {sorted(missing)}"
    )


def test_the_three_p2_structural_guards_are_published():
    """Regression lock on the exact keys that were being dropped."""
    canonical = _canonical_keys()
    for key in (
        "suppressed_multi_clause",
        "suppressed_conjunct_crossing",
        "suppressed_exception_boundary",
    ):
        assert key in canonical, (
            f"{key} is a P2 structural guard that was silently unpublished "
            f"before R-pre. It must stay in the canonical key list."
        )


def test_ghost_b_local_does_not_hand_maintain_a_key_list():
    """counters_per must be built from new_counters(), never a literal."""
    src = _GHOST_B_LOCAL.read_text()
    assert "counters_per = [new_counters() for _ in range(n)]" in src, (
        "counters_per is no longer built from the extractor's canonical "
        "new_counters() factory. A hand-maintained literal is exactly how the "
        "three P2 guards went unpublished."
    )
    # The old literal must not come back.
    assert '{"entity_drop": 0, "relation_drop": 0' not in src, (
        "A hand-maintained counter literal has been re-introduced."
    )


def test_extraction_result_carries_the_counter_map():
    """The dataclass must have somewhere to put the counters."""
    src = _GHOST_B.read_text()
    assert re.search(
        r"extraction_counters:\s*dict\[str,\s*int\]\s*=\s*field\(", src
    ), "ExtractionResult is missing the extraction_counters field."


def test_counters_are_carried_through_both_construction_paths():
    """In-process AND sidecar/worker paths must both forward the map."""
    assert "extraction_counters=" in _GHOST_B_LOCAL.read_text(), (
        "In-process ExtractionResult construction drops extraction_counters."
    )
    assert 'extraction_counters=r.get("extraction_counters"' in _WORKER.read_text(), (
        "Worker mapping (sidecar response -> ExtractionResult) drops "
        "extraction_counters, so sidecar-extracted chunks lose them."
    )


def test_emit_block_publishes_the_full_map_not_just_four():
    src = _GHOST_B_LOCAL.read_text()
    assert '"extraction_counters": dict(counters)' in src, (
        "The per-chunk emit block does not publish the full counter map. "
        "Publishing only the 4 legacy *_drop_count fields is the original bug."
    )


def test_zero_initialized_not_defaultdict():
    """Distinguish 'guard never fired' from 'guard not wired'."""
    src = _EXTRACTOR.read_text()
    assert "dict.fromkeys(ALL_COUNTER_KEYS, 0)" in src, (
        "new_counters() must zero-initialize every canonical key. A defaultdict "
        "or sparse dict makes 'never fired' indistinguishable from 'not wired' "
        "in the persisted record."
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
