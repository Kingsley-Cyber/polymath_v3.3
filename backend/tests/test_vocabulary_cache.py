"""P1.7 — vocabulary-resolution cache semantics."""

import pytest

from services.retriever import vocabulary_cache as vc


@pytest.fixture(autouse=True)
def _clean_cache():
    vc.clear()
    yield
    vc.clear()


def _key(**overrides):
    base = dict(
        query="How do I improve focus?",
        corpus_ids=["c2", "c1"],
        tier="qdrant_mongo",
        top_k_per_corpus=6,
        lane_queries=[("original", "How do I improve focus?")],
        disabled_lexicon_ids=[],
        excluded_terms=[],
    )
    base.update(overrides)
    return vc.resolution_cache_key(**base)


def test_key_is_stable_and_order_insensitive_for_corpora():
    assert _key() == _key(corpus_ids=["c1", "c2"])
    assert _key() == _key(query="how do i  improve focus?")  # normalized


def test_key_changes_with_inputs_that_change_results():
    assert _key() != _key(query="How do I improve sleep?")
    assert _key() != _key(corpus_ids=["c1"])
    assert _key() != _key(top_k_per_corpus=12)
    assert _key() != _key(disabled_lexicon_ids=["lex-1"])
    assert _key() != _key(excluded_terms=["focus"])
    assert _key() != _key(lane_queries=[("original", "other lane text")])


def test_put_get_roundtrip_returns_isolated_copy():
    key = _key()
    payload = {"matches": [{"term": "focus"}], "duration_s": 9.9}
    vc.put(key, payload)
    first = vc.get(key)
    assert first == payload
    first["matches"].append({"term": "mutated"})
    second = vc.get(key)
    assert second == payload  # mutation of a returned copy never leaks back


def test_epoch_bump_invalidates_only_touched_corpus():
    key_before = _key()
    vc.put(key_before, {"matches": []})
    assert vc.get(key_before) is not None
    vc.bump_corpus_epoch("c1")
    key_after = _key()
    assert key_after != key_before
    assert vc.get(key_after) is None  # fresh key -> miss -> re-resolve
    other = _key(corpus_ids=["c9"])
    vc.put(other, {"matches": []})
    vc.bump_corpus_epoch("c1")
    assert vc.get(other) is not None  # untouched corpus unaffected


def test_ttl_expiry(monkeypatch):
    key = _key()
    vc.put(key, {"matches": []})
    assert vc.get(key) is not None
    real_monotonic = vc.time.monotonic
    monkeypatch.setattr(vc.time, "monotonic", lambda: real_monotonic() + 10_000)
    assert vc.get(key) is None


def test_lru_eviction(monkeypatch):
    monkeypatch.setattr(vc, "_max_entries", lambda: 16)
    keys = [_key(query=f"q{i}") for i in range(20)]
    for i, key in enumerate(keys):
        vc.put(key, {"i": i})
    assert vc.get(keys[0]) is None  # evicted
    assert vc.get(keys[-1]) is not None
