"""Q4 — pure tests for the cross-domain emphasis levers.
Runnable standalone: python3 tests/test_cross_domain.py
"""
import importlib.util
import os
import sys

_spec = importlib.util.spec_from_file_location(
    "cross_domain",
    os.path.join(os.path.dirname(__file__), "..", "services", "retriever", "cross_domain.py"),
)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)


class _C:
    def __init__(self, chunk_id, doc_id, score, domain=None, mechanisms=None):
        self.chunk_id = chunk_id
        self.doc_id = doc_id
        self.score = score
        self.domain = domain
        self.metadata = {"mechanisms": mechanisms} if mechanisms else {}


def test_bridge_cap_modes():
    assert _m.bridge_cap(8, "balanced") == 2      # today's throttle exactly
    assert _m.bridge_cap(20, "balanced") == 5
    assert _m.bridge_cap(8, "strong") == 4
    assert _m.bridge_cap(20, "strong") == 10
    assert _m.bridge_cap(8, "off") == 0
    assert _m.bridge_cap(0, "strong") == 0
    assert _m.bridge_cap(8, "junk-mode") == 2     # unknown -> balanced


def test_domain_reserve_swaps_last_slot_only():
    sel = [_C("a", "d1", 0.9, "psychology"), _C("b", "d2", 0.8, "psychology"),
           _C("c", "d3", 0.7, "psychology")]
    pool = sel + [_C("x", "d4", 0.5, "systems_thinking"), _C("y", "d5", 0.4, "economics")]
    out, dom = _m.domain_reserve_swap(sel, pool, mode="balanced", broad=True)
    assert dom == "systems_thinking"
    assert out[0].chunk_id == "a" and out[1].chunk_id == "b"   # top untouched
    assert out[-1].chunk_id == "x"                              # best different-domain


def test_domain_reserve_guards():
    sel = [_C("a", "d1", 0.9, "psych"), _C("b", "d2", 0.8, "econ")]
    pool = sel + [_C("x", "d3", 0.7, "systems")]
    # already diverse -> no swap
    assert _m.domain_reserve_swap(sel, pool, mode="strong", broad=True)[1] is None
    mono = [_C("a", "d1", 0.9, "psych"), _C("b", "d2", 0.8, "psych")]
    # off -> never
    assert _m.domain_reserve_swap(mono, pool, mode="off", broad=True)[1] is None
    # balanced fires on BROAD only
    assert _m.domain_reserve_swap(mono, pool, mode="balanced", broad=False)[1] is None
    # strong also fires on BALANCED intent
    assert _m.domain_reserve_swap(mono, pool, mode="strong", broad=False,
                                  balanced_intent=True)[1] == "systems"
    # floor: candidate below ratio*top never swaps in
    weak_pool = mono + [_C("w", "d9", 0.1, "systems")]
    assert _m.domain_reserve_swap(mono, weak_pool, mode="balanced", broad=True)[1] is None
    # unknown top domain -> inert (unpromoted corpora)
    nodom = [_C("a", "d1", 0.9, None), _C("b", "d2", 0.8, None)]
    assert _m.domain_reserve_swap(nodom, pool, mode="strong", broad=True)[1] is None


def test_mechanisms_bonus_cross_doc_only():
    ranked = [
        _C("l1", "d1", 0.9, mechanisms=["compounding"]),
        _C("l2", "d2", 0.8, mechanisms=["feedback_loop"]),
        _C("l3", "d3", 0.7),
        _C("same_doc", "d1", 0.5, mechanisms=["compounding"]),   # same doc as leader
        _C("bridge", "d7", 0.5, mechanisms=["Compounding"]),     # case-folded match
        _C("nomatch", "d8", 0.5, mechanisms=["osmosis"]),
    ]
    n = _m.mechanisms_overlap_bonus(ranked, mode="balanced")
    assert n == 1
    assert abs(ranked[4].score - 0.52) < 1e-9      # bridge boosted
    assert ranked[3].score == 0.5 and ranked[5].score == 0.5
    assert _m.mechanisms_overlap_bonus(ranked, mode="off") == 0
    # leaders without mechanisms -> inert (unpromoted corpora)
    bare = [_C("a", "d1", 0.9), _C("b", "d2", 0.8), _C("c", "d3", 0.7),
            _C("d", "d4", 0.5, mechanisms=["compounding"])]
    assert _m.mechanisms_overlap_bonus(bare, mode="strong") == 0


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
