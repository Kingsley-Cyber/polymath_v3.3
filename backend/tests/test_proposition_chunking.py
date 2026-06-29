"""Asserting tests for the semantic_split (proposition) child chunker.

Run in the backend image (tiktoken needed):
  docker run --rm -e LITELLM_MASTER_KEY=test -e AUTH_SECRET_KEY=test \
    -e DEFAULT_ADMIN_PASSWORD=test -v $PWD/backend:/app -w /app \
    --entrypoint python polymath_v33-backend tests/test_proposition_chunking.py
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.ingestion.tier_chunker import (  # noqa: E402
    _build_policy,
    _count_tokens,
    _make_children,
    _split_at_boundary,
    _split_by_paragraph_idea,
)

# Three distinct paragraphs (ideas). sentence_merge PACKS them into ONE child;
# semantic_split keeps them SEPARATE (one idea per child).
THREE_IDEAS = (
    "The Big Five model identifies five personality dimensions.\n\n"
    "Costa and McCrae established the NEO-PI-R as the standard assessment tool.\n\n"
    "Meta-analyses confirmed cross-cultural validity across many countries."
)


def test_paragraph_idea_keeps_one_chunk_per_paragraph():
    parts = _split_by_paragraph_idea(THREE_IDEAS, target_tokens=128, max_tokens=256)
    assert len(parts) == 3, parts
    assert "Big Five" in parts[0]
    assert "NEO-PI-R" in parts[1]
    assert "cross-cultural" in parts[2]


def test_sentence_merge_packs_the_same_text_into_fewer_chunks():
    merged = _split_at_boundary(THREE_IDEAS, target_tokens=128)
    # The old behaviour packs all three short paragraphs into one 128-tok child.
    assert len(merged) == 1, merged


def test_oversize_paragraph_splits_at_sentences():
    big = " ".join(f"This is sentence number {i} with filler." for i in range(60))
    parts = _split_by_paragraph_idea(big, target_tokens=30, max_tokens=40)
    assert len(parts) > 1
    assert all(_count_tokens(p) <= 80 for p in parts)


def test_make_children_semantic_is_finer_than_sentence_merge():
    base = dict(
        parent_id="p", doc_id="d", corpus_id="c", parent_text=THREE_IDEAS,
        heading_path=None, source_tier="t", child_index=0,
        child_target_tokens=128, child_min_tokens=1, child_max_tokens=256,
    )
    merged, _ = _make_children(**base, child_strategy="sentence_merge")
    semantic, _ = _make_children(**base, child_strategy="semantic_split")
    assert len(merged) == 1
    assert len(semantic) == 3, [c.text for c in semantic]
    assert len(semantic) > len(merged)
    # parent/child link + schema preserved (so retrieval + hydration still work)
    for c in semantic:
        assert c.parent_id == "p" and c.doc_id == "d" and c.text.strip()


def test_policy_default_is_semantic_split_and_grandfathers_sentence_merge():
    # New corpora (no explicit config) -> semantic_split
    assert _build_policy(None).resolved_child_strategy == "semantic_split"
    # Old corpora with frozen sentence_merge -> grandfathered
    old = SimpleNamespace(child_chunk_algorithm="sentence_merge")
    assert _build_policy(old).resolved_child_strategy == "sentence_merge"
    # Explicit semantic_split honoured
    new = SimpleNamespace(child_chunk_algorithm="semantic_split")
    assert _build_policy(new).resolved_child_strategy == "semantic_split"


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
