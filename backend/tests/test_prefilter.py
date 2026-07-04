"""Q2/U2 — pure tests for the promoted-payload soft-prefilter helpers.

Runnable standalone: python3 tests/test_prefilter.py (loads by file path so
the retriever package __init__ never imports).
"""
import importlib.util
import os
import sys

_spec = importlib.util.spec_from_file_location(
    "prefilter",
    os.path.join(
        os.path.dirname(__file__), "..", "services", "retriever", "prefilter.py"
    ),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
query_payload_terms = _mod.query_payload_terms
query_operator = _mod.query_operator
semantic_rank_bonus = _mod.semantic_rank_bonus


def test_terms_match_promote_conventions():
    terms, eids = query_payload_terms("What is layered indexing in retrieval systems?")
    # concepts[] convention: lowercase, space-joined (promote._norm_term)
    assert "layered indexing" in terms
    assert "retrieval systems" in terms
    # entity_ids convention: hyphens (ENTITY-ID LAW)
    assert "entity:layered-indexing" in eids
    assert "entity:retrieval-systems" in eids
    # positional pairing: eids derive 1:1 from terms
    assert len(terms) == len(eids)
    # stopword unigrams never emitted
    assert "what" not in terms and "in" not in terms
    # deterministic
    assert (terms, eids) == query_payload_terms("What is layered indexing in retrieval systems?")


def test_terms_empty_and_cap():
    assert query_payload_terms("") == ([], [])
    assert query_payload_terms("the of and") == ([], [])
    terms, eids = query_payload_terms("alpha beta gamma delta epsilon zeta", cap=4)
    assert len(terms) == 4 and len(eids) == 4


def test_operator_precedence_and_detection():
    assert query_operator("What is the difference between X and Y?") == "comparison"
    assert query_operator("What is layered indexing?") == "definition"
    assert query_operator("How do I configure the reranker?") == "procedure"
    assert query_operator("Why does compounding accelerate returns?") == "causal"
    assert query_operator("Summarize the third chapter") is None
    assert query_operator("") is None
    # embedded marker, not just prefix
    assert query_operator("Explain deep work vs shallow work tradeoffs") == "comparison"


def test_semantic_rank_bonus_rank_only_semantics():
    assert semantic_rank_bonus("definition", "definition") == 0.03
    assert semantic_rank_bonus("definition", "principle", bonus=0.05) == 0.05
    assert semantic_rank_bonus("definition", "narrative") == 0.0
    assert semantic_rank_bonus(None, "definition") == 0.0
    assert semantic_rank_bonus("definition", None) == 0.0
    assert semantic_rank_bonus("definition", "definition", bonus=0.0) == 0.0
    assert semantic_rank_bonus("comparison", "framework") == 0.03


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
