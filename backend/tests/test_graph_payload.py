"""§12.6 P2 — pure tests for Mode A payload-first serving helpers.

No Neo4j, no Mongo, no clock: slug linking candidates, adjacency vote
scoring, G1 seed preference, and the G3 TTL cache (injected clock).
Runnable standalone: python3 tests/test_graph_payload.py
"""
import importlib.util
import os
import sys

# load by file path — the retriever package __init__ pulls the full app
# dependency tree, but this module is deliberately dependency-free.
_spec = importlib.util.spec_from_file_location(
    "graph_payload",
    os.path.join(
        os.path.dirname(__file__), "..", "services", "retriever", "graph_payload.py"
    ),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
ExpansionCache = _mod.ExpansionCache
prefer_relation_seeds = _mod.prefer_relation_seeds
score_payload_neighbors = _mod.score_payload_neighbors
slug_candidates = _mod.slug_candidates


def test_slug_candidates_ngram_order_and_stopwords():
    out = slug_candidates("How does layered indexing help retrieval systems?")
    # longest grams first; matches promote's entity:{slug} normalization
    assert out[0].startswith("entity:")
    # HYPHEN convention — must match neo4j_writer.entity_id_from_name
    assert "entity:layered-indexing" in out
    assert "entity:retrieval-systems" in out
    # bare stopword/short unigrams never emitted
    assert "entity:how" not in out and "entity:does" not in out
    # edge-stopword multigrams suppressed
    assert "entity:does-layered" not in out
    # n-gram outranks its component unigrams
    assert out.index("entity:layered-indexing") < out.index("entity:retrieval")
    # injectable canonical fn wins
    assert slug_candidates("alpha beta", entity_id_fn=lambda g: f"E|{g}")[0] == "E|alpha beta"
    # deterministic
    assert out == slug_candidates("How does layered indexing help retrieval systems?")


def test_slug_candidates_empty_and_cap():
    assert slug_candidates("") == []
    assert slug_candidates("the of and") == []
    assert len(slug_candidates("alpha beta gamma delta epsilon zeta eta theta", cap=5)) == 5


def test_score_payload_neighbors_votes_and_determinism():
    ranked = score_payload_neighbors({
        "s1": ["n1", "n2"],
        "s2": ["n1", "n3"],
        "s3": ["n1"],
    })
    # n1 has 3 votes -> first, highest score
    assert ranked[0][0] == "n1" and ranked[0][1] == 3
    assert ranked[0][2] == min(1.0, 0.30 + 0.20 * 3)
    # 1-vote peers tie-break lexicographically
    assert [cid for cid, _, _ in ranked[1:]] == ["n2", "n3"]
    # seeds never expand to themselves
    assert all(cid not in {"s1", "s2", "s3"} for cid, _, _ in ranked)


def test_score_payload_neighbors_exclude_and_cap():
    ranked = score_payload_neighbors(
        {"s1": ["pool1", "n1"]}, exclude={"pool1"}, cap=1
    )
    assert ranked == [("n1", 1, 0.5)]


def test_prefer_relation_seeds_groups_keep_score_order():
    ordered = ["a", "b", "c", "d"]  # score-desc
    got = prefer_relation_seeds(ordered, {"b": True, "d": True}, limit=3)
    assert got == ["b", "d", "a"]  # relation-bearing first, order kept
    # no relations info -> plain top-N
    assert prefer_relation_seeds(ordered, {}, limit=2) == ["a", "b"]
    assert prefer_relation_seeds(ordered, {"b": True}, limit=0) == []


def test_expansion_cache_hit_expiry_and_isolation():
    now = [100.0]
    cache = ExpansionCache(ttl_seconds=180, clock=lambda: now[0])
    key = ExpansionCache.key(["c2", "c1"], ["s1", "s2"], 8, "Q ")
    # key normalizes corpus order, seed order, query case/space
    assert key == ExpansionCache.key(["c1", "c2"], ["s2", "s1"], 8, "q")

    copy = lambda v: list(v)  # noqa: E731
    assert cache.get(key, copy) is None
    cache.put(key, ["r1"], copy)
    got = cache.get(key, copy)
    assert got == ["r1"]
    # mutation of the returned copy never poisons the entry
    got.append("junk")
    assert cache.get(key, copy) == ["r1"]
    # TTL expiry
    now[0] += 181
    assert cache.get(key, copy) is None
    # ttl<=0 disables entirely
    off = ExpansionCache(ttl_seconds=0, clock=lambda: now[0])
    off.put(key, ["x"], copy)
    assert off.get(key, copy) is None


def test_expansion_cache_evicts_stalest():
    now = [0.0]
    cache = ExpansionCache(ttl_seconds=1000, max_entries=2, clock=lambda: now[0])
    copy = lambda v: v  # noqa: E731
    cache.put("k1", 1, copy)
    now[0] = 1
    cache.put("k2", 2, copy)
    now[0] = 2
    cache.put("k3", 3, copy)  # evicts k1 (stalest)
    assert cache.get("k1", copy) is None
    assert cache.get("k2", copy) == 2 and cache.get("k3", copy) == 3


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
