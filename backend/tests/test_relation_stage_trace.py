"""The per-candidate death trace must stay honest.

WHY THIS TEST EXISTS
    Recall attribution is only trustworthy if the trace accounts for EVERY
    candidate. If a guard is added later without a trace record, candidates
    start vanishing silently and an audit that sums trace rows under-reports
    the loss while looking complete — the exact failure mode the repo's
    "no silent caps" rule exists to prevent.

    It also pins the other half of the contract: turning the trace ON must not
    change what the extractor emits. An observability hook that perturbs the
    thing it observes is worse than none.
"""

from __future__ import annotations

import pytest

from services.extraction.dep_path_extractor import EntitySpan, new_counters
from services.extraction.frame_extractor import FrameExtractor, _find_frames


@pytest.fixture(scope="module")
def fx():
    return FrameExtractor()


def _spans(text: str, pairs: list[tuple[str, str]]) -> list[EntitySpan]:
    out = []
    for surface, etype in pairs:
        idx = text.find(surface)
        assert idx >= 0, f"fixture bug: {surface!r} not in text"
        out.append(EntitySpan(surface=surface, start_char=idx,
                              end_char=idx + len(surface),
                              entity_type=etype, canonical_name=surface.lower()))
    return out


TEXT = (
    "Microsoft acquired GitHub in 2018. Qdrant, a vector database, "
    "powers the retrieval layer. The team built the framework."
)
ENTS = [("Microsoft", "Organization"), ("GitHub", "Organization"),
        ("Qdrant", "Software"), ("vector database", "Concept"),
        ("retrieval layer", "Concept"), ("framework", "Software")]


def test_trace_off_and_on_emit_identical_triples(fx):
    """Observing must not perturb."""
    spans = _spans(TEXT, ENTS)
    a = fx.extract(text=TEXT, entities=_spans(TEXT, ENTS), chunk_id="c1")
    trace: list[dict] = []
    b = fx.extract(text=TEXT, entities=spans, chunk_id="c1", trace=trace)

    assert [(t.subject_surface, t.predicate, t.object_surface) for t in a] == \
           [(t.subject_surface, t.predicate, t.object_surface) for t in b]
    assert trace, "trace was requested but nothing was recorded"


def test_trace_off_leaves_counters_identical(fx):
    """The trace hook must not add or drop a single suppression count."""
    off, on = new_counters(), new_counters()
    fx.extract(text=TEXT, entities=_spans(TEXT, ENTS), chunk_id="c1",
               suppression_counters=off)
    fx.extract(text=TEXT, entities=_spans(TEXT, ENTS), chunk_id="c1",
               suppression_counters=on, trace=[])
    assert off == on


def test_every_candidate_frame_is_accounted_for(fx):
    """No candidate may disappear without a record.

    This is the load-bearing assertion. `_find_frames` is the full candidate
    set; the trace must have a verdict — a death reason or a survival — for
    each one. A new guard that forgets its trace record fails here.
    """
    import spacy  # noqa: F401  (nlp already loaded on the extractor)

    doc = fx._nlp(TEXT)
    n_frames = len(_find_frames(doc))
    trace: list[dict] = []
    fx.extract(text=TEXT, entities=_spans(TEXT, ENTS), chunk_id="c1",
               doc=doc, trace=trace)

    assert n_frames > 0, "fixture produced no frames — test proves nothing"
    assert len(trace) == n_frames, (
        f"{n_frames} candidates entered, {len(trace)} were accounted for. "
        f"A guard is dropping candidates without a trace record."
    )
    for rec in trace:
        assert "died_at" in rec, f"trace record missing a verdict: {rec}"


def test_death_records_name_the_candidate(fx):
    """A death reason with no candidate attached cannot attribute recall loss."""
    trace: list[dict] = []
    fx.extract(text=TEXT, entities=_spans(TEXT, ENTS), chunk_id="c1",
               trace=trace)
    for rec in trace:
        assert rec.get("subject_token") or rec.get("subject_entity"), rec
        assert rec.get("object_token") or rec.get("object_entity"), rec
        assert rec.get("frame_type"), rec


def test_adapter_boundary_drops_are_traced():
    """The four post-extractor filters must report into the SAME trace.

    They sit in the adapter, not the extractor, so an extractor-only trace
    shows a candidate 'surviving' that the adapter then silently discards.
    """
    from services.extraction.spacy_relation_adapter import get_spacy_extractor

    ex = get_spacy_extractor()
    ex._ensure_loaded()
    trace: list[dict] = []
    ex.extract_chunks(
        [{"chunk_id": "c1", "doc_id": "d1", "text": TEXT,
          "entities": [{"surface_form": s, "entity_type": t} for s, t in ENTS]}],
        trace_list=[trace],
    )
    assert trace, "adapter produced no trace"
    # Every record is either a death or a survival, and survivals at the
    # adapter carry the schema-normalized predicate the graph would receive.
    for rec in trace:
        if rec.get("survived"):
            assert rec.get("predicate"), rec
