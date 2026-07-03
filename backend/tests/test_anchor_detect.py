"""B5 anchor-detection tests — deterministic, lexical-first, no LLM.

    docker exec -i polymath_v33-backend-1 python /app/tests/test_anchor_detect.py
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.retriever.anchor_detect import detect_anchor_doc_ids  # noqa: E402

DOCS = [
    {"doc_id": "d_seduction", "title": "The Art of Seduction", "author": "Robert Greene"},
    {"doc_id": "d_habits", "title": "Atomic Habits", "author": "James Clear"},
    {"doc_id": "d_ddia", "title": "Designing Data-Intensive Applications", "author": "Martin Kleppmann"},
]


def test_author_name_anchors():
    out = detect_anchor_doc_ids("what does robert greene say about power", DOCS)
    assert out == ["d_seduction"]


def test_title_tokens_anchor():
    out = detect_anchor_doc_ids("summarize atomic habits chapter one", DOCS)
    assert out == ["d_habits"]


def test_generic_words_never_anchor():
    # "the art of ..." alone must not anchor The Art of Seduction
    assert detect_anchor_doc_ids("the art of making bread at home", DOCS) == []
    assert detect_anchor_doc_ids("what does the book say", DOCS) == []


def test_multiple_anchors_ranked_deterministic():
    q = "compare robert greene's seduction with james clear's atomic habits"
    out = detect_anchor_doc_ids(q, DOCS)
    assert set(out) == {"d_seduction", "d_habits"}
    assert out == detect_anchor_doc_ids(q, DOCS)          # stable twice


def test_no_named_source_no_anchor():
    assert detect_anchor_doc_ids("how do databases handle concurrency", DOCS) == []


def test_partial_author_not_enough():
    # bare "james" (common first name, must not anchor Clear's book)
    assert detect_anchor_doc_ids("james thinks this is fine", DOCS) == []


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
