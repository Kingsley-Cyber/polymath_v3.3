"""Summary verifier writer-stamp/backfill decision table.

Pure: loads expected_summary_points_from_state by file path (verify.py's
other imports need the app tree, so we exec only the function's module after
stubbing heavy imports is overkill — instead we import via a tiny shim that
reads the function source. Simplest robust approach: import inside container
for integration; here we test the DECISION TABLE via a copied reference
implementation check against the real source text to prevent drift.)

Runnable standalone: python3 tests/test_verify_summary_expectation.py
"""
import __future__
import ast
import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "services", "ingestion", "verify.py")


def _load_fn():
    # execute ONLY the target function's def from verify.py — no module imports
    tree = ast.parse(open(SRC).read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "expected_summary_points_from_state":
            mod = ast.Module(body=[node], type_ignores=[])
            ns = {"Any": object}
            exec(compile(mod, SRC, "exec",
                         flags=__future__.annotations.compiler_flag), ns)
            return ns["expected_summary_points_from_state"]
    raise AssertionError("function not found in verify.py")


fn = _load_fn()


def test_stamp_preferred():
    assert fn({"summary_points": 0}) == 0          # tiny doc: writer wrote none
    assert fn({"summary_points": 7}) == 7


def test_later_backfill_invalidates_original_writer_stamp():
    assert fn({
        "summary_points": 0,
        "summaries_indexed": True,
        "summary_backfilled_at": "2026-07-10T00:00:00Z",
    }) is None


def test_unstamped_falls_back():
    assert fn(None) is None                        # legacy doc
    assert fn({}) is None                          # legacy write_state
    assert fn({"summaries_indexed": False}) is None  # never infer from the bool
    assert fn({"summary_points": None}) is None    # gate-required-but-none-produced
    assert fn({"summary_points": -3}) is None      # corrupt -> strict path
    assert fn({"summary_points": True}) is None    # bool is not a count
    assert fn("junk") is None


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
