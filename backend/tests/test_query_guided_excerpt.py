"""B2 query-guided excerpts (2026-07-01) — the window that fills a char
budget must be the query's best passage, not the chunk's leading chars.
Live-probe motivation: Le Guin doc-hit/passage-miss — the reranker scored
the leading 1000 chars and never saw her sentence-rhythm passage."""

from services.retriever.excerpt import query_guided_excerpt


FILLER = (
    "This chapter opens with remarks about the weather in coastal towns. "
    "The author recalls childhood summers and long walks on gravel roads. "
    "There are anecdotes about neighbors, gardens, and the local library. "
) * 8  # ~600 chars of unrelated prose per repetition block

TARGET = (
    "The test of a sentence is whether its rhythm carries the reader forward; "
    "prose rhythm and the sound of sentences are the writer's first duty. "
)


def test_returns_whole_text_when_it_fits():
    assert query_guided_excerpt("short text", "any query", max_chars=100) == "short text"


def test_picks_matching_window_deep_in_document():
    text = FILLER + TARGET + FILLER
    out = query_guided_excerpt(
        text,
        "What does Le Guin say about the sound and rhythm of prose sentences?",
        max_chars=300,
    )
    assert "rhythm" in out
    assert len(out) <= 300
    # the legacy leading window would have been pure filler
    assert "coastal towns" not in out or "rhythm" in out


def test_falls_back_to_leading_window_when_no_terms_match():
    text = FILLER + FILLER
    out = query_guided_excerpt(text, "quantum chromodynamics lattice", max_chars=250)
    assert out == text[:250]


def test_falls_back_when_query_is_all_stopwords():
    text = FILLER + TARGET
    out = query_guided_excerpt(text, "what is the and of a", max_chars=250)
    assert out == text[:250]


def test_prefers_window_covering_more_distinct_terms():
    text = (
        FILLER
        + "Sentences matter to every writer in some vague way. "
        + FILLER
        + "Prose rhythm, the sound of sentences, is what the writer tunes first. "
        + FILLER
    )
    out = query_guided_excerpt(
        text, "prose rhythm sound of sentences writer", max_chars=200
    )
    assert "rhythm" in out and "sound" in out


def test_handles_single_sentence_longer_than_budget():
    long_sentence = "rhythm " * 400  # no sentence boundaries, ~2800 chars
    out = query_guided_excerpt(long_sentence, "prose rhythm", max_chars=300)
    assert out
    assert len(out) <= 300


def test_source_excerpt_uses_query_window(monkeypatch):
    # Site 2 (evidence packet): _source_excerpt must surface the query's
    # passage, not the chunk head, when a query is provided.
    from services.chat_orchestrator import _source_excerpt

    data = {"text": FILLER + TARGET + FILLER}
    with_query = _source_excerpt(
        data, max_chars=300, query="prose rhythm and the sound of sentences"
    )
    without_query = _source_excerpt(data, max_chars=300)
    assert "rhythm" in with_query
    assert "rhythm" not in without_query  # head window is filler
