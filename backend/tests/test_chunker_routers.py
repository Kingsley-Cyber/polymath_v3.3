"""Asserting tests for chunker routers 1+2 (POLYMATH_ARCHITECTURE §3.S2).

Mirrors the empirical probes that exposed the gaps:
  probe D — bullet lists shredded at arbitrary points  → router 1 (items intact)
  probe C — line-structured text collapsed             → router 2a (line grouping)
  probe H — mega-sentence hard-split MID-WORD          → router 2 (boundary-safe)
  probe A — punctuated wall of text                    → unchanged (regression)
  probe E — blank-line structure preserved             → unchanged (regression)

Run inside the backend container:
    docker exec -i polymath_v33-backend-1 python /app/tests/test_chunker_routers.py
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import services.ingestion.tier_chunker as tc  # noqa: E402

# Force deterministic regex engine for the rule tests (SaT availability must
# not change router behaviour assertions); SaT gets its own gated test.
tc._SAT_FAILED = True

TARGET, MAX = 128, 256


def _tok(s):
    return tc._count_tokens(s)


# ── Router 1: list blocks (probe D) ────────────────────────────────────────
def _bullet_list(n=50):
    return "\n".join(
        f"- item {i}: a short actionable point about topic number {i} to remember"
        for i in range(n)
    )


def test_list_block_detected():
    assert tc._is_list_block(_bullet_list()) is True
    assert tc._is_list_block("Just one prose paragraph. Nothing else.") is False


def test_list_items_never_split_across_children():
    text = _bullet_list(50)
    out = tc._split_by_paragraph_idea(text, TARGET, MAX)
    assert len(out) > 1                                   # actually split
    for chunk in out:
        for ln in chunk.splitlines():
            assert ln.startswith("- item "), f"broken item line: {ln!r}"
    # every item survives exactly once
    all_lines = [ln for c in out for ln in c.splitlines()]
    assert len(all_lines) == 50
    assert all(_tok(c) <= MAX for c in out)


def test_numbered_list_with_continuation_lines():
    items = []
    for i in range(12):
        items.append(f"{i + 1}. Step {i + 1} of the procedure with details")
        items.append("   continued explanation line that belongs to the step above")
    text = "\n".join(items)
    assert tc._is_list_block(text)
    units = tc._split_list_items(text)
    assert len(units) == 12                               # continuations attached
    assert all(u.count("\n") == 1 for u in units)


def test_small_list_stays_whole():
    # A list block UNDER max tokens is one coherent child — not exploded.
    text = "\n".join(f"- point {i}" for i in range(8))
    out = tc._split_by_paragraph_idea(text, TARGET, MAX)
    assert out == [text]


def test_mixed_prose_and_list_paragraphs():
    prose = "This is an ordinary paragraph about a topic. " * 3
    text = prose.strip() + "\n\n" + _bullet_list(50) + "\n\n" + prose.strip()
    out = tc._split_by_paragraph_idea(text, TARGET, MAX)
    assert out[0] == prose.strip()                        # prose para untouched
    assert out[-1] == prose.strip()
    for chunk in out[1:-1]:
        assert chunk.splitlines()[0].startswith("- item ")


# ── Router 2a: line-structured low-punctuation text (probe C) ──────────────
def _chat_log(n=60):
    return "\n".join(f"[00:{i:02d}] user{i % 3}: message fragment {i} no ending" for i in range(n))


def test_line_structured_blocks_group_by_lines():
    text = _chat_log(60)
    assert tc._is_low_punct_multiline(text) is True
    out = tc._split_by_paragraph_idea(text, TARGET, MAX)
    assert len(out) > 1
    original = tc._nonempty_lines(text)
    rebuilt = [ln for c in out for ln in c.splitlines()]
    assert rebuilt == original                            # cuts ONLY at line boundaries


def test_punctuated_prose_not_misrouted():
    # Hard-wrapped punctuated prose must NOT trigger line grouping.
    text = "\n".join("This is a full sentence that ends properly." for _ in range(20))
    assert tc._is_low_punct_multiline(text) is False


# ── Router 2: boundary-safe hard split (probe H) ───────────────────────────
def test_hard_split_never_mid_word():
    words = " ".join(f"word{i}visible" for i in range(600))  # >256 tokens, no punct
    out = tc._hard_split_oversize([words], 256)
    assert len(out) >= 2
    vocab = set(words.split())
    for c in out:
        for w in c.split():
            assert w in vocab, f"mid-word fragment: {w!r}"
    assert all(_tok(c) <= 256 for c in out)


def test_hard_split_no_whitespace_still_caps():
    blob = "x" * 4000                                     # no whitespace at all
    out = tc._hard_split_oversize([blob], 256)
    assert len(out) >= 2
    assert all(_tok(c) <= 256 for c in out)               # cap holds regardless


# ── Regressions: prior behaviour preserved ─────────────────────────────────
def test_wall_of_text_still_sentence_packs():
    text = " ".join(
        f"Sentence number {i} makes a specific point about the subject." for i in range(100)
    )
    out = tc._split_by_paragraph_idea(text, TARGET, MAX)
    assert len(out) >= 4
    assert all(_tok(c) <= MAX for c in out)
    assert all(c.rstrip().endswith(".") for c in out)     # sentence boundaries held


def test_blank_line_structure_preserved():
    para1 = "First idea paragraph."
    table = "| a | b |\n|---|---|\n| 1 | 2 |"
    para2 = "Second idea paragraph."
    out = tc._split_by_paragraph_idea(f"{para1}\n\n{table}\n\n{para2}", TARGET, MAX)
    assert out == [para1, table, para2]


def test_routers_kill_switch_reverts():
    text = _bullet_list(50)
    import config

    config.get_settings().CHUNKER_STRUCTURED_ROUTERS = False
    try:
        out = tc._split_by_paragraph_idea(text, TARGET, MAX)
        # legacy behaviour: sentence/hard splitting, items may break — just
        # assert the router did NOT run (chunks not all item-aligned)
        assert not all(
            ln.startswith("- item ") for c in out for ln in c.splitlines()
        ) or len(out) == 1
    finally:
        config.get_settings().CHUNKER_STRUCTURED_ROUTERS = True


# ── SaT engine (gated: asserts fallback contract; asserts splits if present) ──
def test_sentence_engine_contract():
    tc._SAT_FAILED = False
    tc._SAT_MODEL = None
    mega = "the quick brown fox keeps running and the story continues " * 40  # no punct
    sents = tc._split_at_sentences(mega)
    if tc._SAT_FAILED or tc._SAT_MODEL is None:
        assert sents == [mega.strip()]                    # regex fallback: 1 piece, logged
        print("  (SaT unavailable — regex fallback verified)")
    else:
        assert len(sents) > 1                             # SaT splits punctuation-less text
        print(f"  (SaT active — {len(sents)} segments)")
    tc._SAT_FAILED = True                                  # restore determinism for reruns


# ── Router 4: VTT/SRT subtitle lane ────────────────────────────────────────
def test_srt_parses_to_transcript_blocks():
    from services.ingestion.docling_adapter import _parse_subtitle_file

    srt = "\n".join(
        f"{i + 1}\n00:00:{i:02d},000 --> 00:00:{i + 1:02d},500\nALICE: line number {i} spoken here\n"
        for i in range(20)
    )
    res = _parse_subtitle_file(srt.encode(), "meeting.srt", "")
    assert res is not None
    assert res.source_format == "subtitle_srt"
    assert res.sections and res.sections[0].element_type == "transcript_block"
    md = res.sections[0].metadata
    assert md["source_format"] == "subtitle_srt"
    assert md["time_start"] and md["time_end"]
    assert "ALICE" in md["speakers"]
    assert "ALICE: line number 0" in res.sections[0].text


def test_vtt_parses_with_voice_tags():
    from services.ingestion.docling_adapter import _parse_subtitle_file

    cues = "\n\n".join(
        f"00:0{i}.000 --> 00:0{i + 1}.000\n<v Bob>utterance {i} continues</v>"
        for i in range(1, 8)
    )
    vtt = "WEBVTT\n\n" + cues.replace("00:0", "00:00:0")
    res = _parse_subtitle_file(vtt.encode(), "talk.vtt", "")
    assert res is not None
    assert res.source_format == "subtitle_vtt"
    assert "Bob" in res.sections[0].metadata["speakers"]
    assert "<v" not in res.sections[0].text          # tags stripped


def test_non_subtitle_text_returns_none():
    from services.ingestion.docling_adapter import _parse_subtitle_file

    assert _parse_subtitle_file(b"just some prose text", "notes.txt", "") is None
    assert _parse_subtitle_file(b"not really cues", "fake.srt", "") is None  # <3 cues


# ── Router 5: semantic-deviation escalation (fake embedder) ────────────────
def test_deviation_split_finds_topic_boundaries():
    # 3 synthetic topics: sentences within a topic share a vector direction.
    topics = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    sentences = [f"topic{t} sentence {i} content." for t in range(3) for i in range(4)]
    fake_vecs = [topics[t] for t in range(3) for _ in range(4)]
    orig = tc._embed_for_escalation
    tc._embed_for_escalation = lambda s: fake_vecs
    try:
        out = tc._semantic_deviation_split(sentences, TARGET, MAX)
        assert out is not None and len(out) == 3          # one chunk per topic
        for t, chunk in enumerate(out):
            assert all(f"topic{t}" in s for s in chunk.split(".") if s.strip())
    finally:
        tc._embed_for_escalation = orig


def test_escalation_falls_back_when_embedder_down():
    orig = tc._embed_for_escalation
    tc._embed_for_escalation = lambda s: None
    try:
        assert tc._semantic_deviation_split(["a."] * 10, TARGET, MAX) is None
    finally:
        tc._embed_for_escalation = orig


def test_uniform_text_declines_escalation():
    # No topical structure → returns None → caller greedy-packs.
    sentences = [f"same topic sentence {i}." for i in range(10)]
    orig = tc._embed_for_escalation
    tc._embed_for_escalation = lambda s: [[1.0, 0.0]] * len(s)
    try:
        assert tc._semantic_deviation_split(sentences, TARGET, MAX) is None
    finally:
        tc._embed_for_escalation = orig


# ── Semantic parents for structureless text (tier_c) ───────────────────────
def _three_topic_doc():
    t = []
    for topic in range(3):
        for p in range(8):
            t.append(f"topic{topic} paragraph {p}. " + f"More topic{topic} discussion sentence {p} with enough words to carry token weight across the budget. " * 4)
    return "\n\n".join(t)


def _fake_topic_vecs(units):
    vecs = []
    for u in units:
        t = 0 if "topic0" in u else (1 if "topic1" in u else 2)
        v = [0.0, 0.0, 0.0]
        v[t] = 1.0
        vecs.append(v)
    return vecs


def test_semantic_parents_align_to_topics():
    doc = _three_topic_doc()
    orig = tc._embed_for_escalation
    tc._embed_for_escalation = _fake_topic_vecs
    try:
        parents = tc._semantic_parent_blocks(doc, min_tokens=100, target_tokens=400, max_tokens=1800)
        assert parents is not None and len(parents) == 3
        for t, p in enumerate(parents):
            assert f"topic{t}" in p and f"topic{(t + 1) % 3}" not in p
    finally:
        tc._embed_for_escalation = orig


def test_semantic_parents_deterministic():
    doc = _three_topic_doc()
    orig = tc._embed_for_escalation
    tc._embed_for_escalation = _fake_topic_vecs
    try:
        a = tc._semantic_parent_blocks(doc, min_tokens=100, target_tokens=400, max_tokens=1800)
        b = tc._semantic_parent_blocks(doc, min_tokens=100, target_tokens=400, max_tokens=1800)
        assert a == b                                  # byte-identical, twice
    finally:
        tc._embed_for_escalation = orig


def test_semantic_parents_respect_max_budget():
    # single-topic long doc: no semantic dips → budget cap still slices
    doc = "\n\n".join(f"same topic paragraph {i}. " + "filler sentence with several words here. " * 6 for i in range(40))
    orig = tc._embed_for_escalation
    tc._embed_for_escalation = lambda units: [[1.0, 0.0]] * len(units)
    try:
        parents = tc._semantic_parent_blocks(doc, min_tokens=100, target_tokens=400, max_tokens=600)
        assert parents is not None and len(parents) >= 2
        assert all(_tok(p) <= 600 for p in parents)
    finally:
        tc._embed_for_escalation = orig


def test_semantic_parents_fallback_contract():
    doc = _three_topic_doc()
    orig = tc._embed_for_escalation
    tc._embed_for_escalation = lambda units: None      # embedder down
    try:
        assert tc._semantic_parent_blocks(doc, min_tokens=100, target_tokens=400, max_tokens=1800) is None
    finally:
        tc._embed_for_escalation = orig
    # small doc → None (single parent, nothing to gain)
    assert tc._semantic_parent_blocks("short doc.", min_tokens=100, target_tokens=400, max_tokens=1800) is None


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
