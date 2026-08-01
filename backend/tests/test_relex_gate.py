"""Guards on the GLiNER-Relex precision gate.

Each case here is a REAL relation from the 150 hand-judged sample
(docs/baselines/RELEX_GATE_JUDGEMENTS.jsonl), so a regression breaks something
that was actually measured rather than something imagined.
"""

from __future__ import annotations

import pytest
import spacy

from services.extraction.relex_gate import gate_relations, new_gate_counters


@pytest.fixture(scope="module")
def nlp():
    return spacy.load("en_core_web_sm", disable=["ner", "textcat"])


def _rel(text, s, o, label, score=0.7, s_type="software", o_type="software"):
    si, oi = text.find(s), text.find(o)
    assert si >= 0 and oi >= 0, "fixture bug: endpoint not in text"
    return {
        "head": {"start": si, "end": si + len(s), "text": s, "type": s_type},
        "tail": {"start": oi, "end": oi + len(o), "text": o, "type": o_type},
        "relation": label, "score": score,
    }


def _run(nlp, text, rels):
    ctr = new_gate_counters()
    out = gate_relations(rels, text=text, doc=nlp(text), chunk_id="c",
                         counters=ctr)
    return out, ctr


def test_markdown_heading_does_not_condemn_the_prose_after_it(nlp):
    """The bug that killed 159 of 757 relations, including correct ones.

    spaCy does not break a sentence at a markdown heading (no terminal
    punctuation), so the heading and the following prose arrive as ONE
    sentence and the bibliographic guard's `startswith("#")` rule condemned
    both.
    """
    text = ("# Retrieval Architecture Overview\n\nThe retrieval service "
            "depends on Qdrant for vector search.")
    out, ctr = _run(nlp, text, [_rel(text, "retrieval service", "Qdrant",
                                     "depends on", 0.98)])
    assert ctr["relex_bibliographic_context"] == 0
    assert [g.kept for g in out] == [True]


def test_real_citation_is_still_suppressed(nlp):
    """The guard must keep working where it was right.

    Uses the author-initials shape the guard actually targets. Note what is
    DELIBERATELY not tested here: a bare publisher line ("Cambridge, MA: MIT
    Press.") splits into its own sentence carrying no citation marker, so it
    survives — and that is fine. Both such edges in the judged sample,
    (MIT Press, located_in, Cambridge MA) and (University of Wisconsin Press,
    located_in, Madison WI), were judged CORRECT. The guard exists to stop
    citations manufacturing FALSE relations, not to suppress true ones.
    """
    text = ("Siegrist, M., Cvetkovich, G.T.: Shared values, social trust, "
            "and the perception of risk.")
    out, ctr = _run(nlp, text, [_rel(text, "Siegrist", "Shared values",
                                     "created by", 0.9, "person", "document")])
    assert ctr["relex_bibliographic_context"] == 1
    assert not out[0].kept


@pytest.mark.parametrize("s,o,label", [
    ("public cloud", "public cloud arena", "synonym of"),
    ("Google", "Google DeepMind", "owns"),
    ("virtual simulation", "authorized virtual simulation", "depends on"),
])
def test_containment_between_endpoints_is_degenerate(nlp, s, o, label):
    """24 of 81 judged errors were this shape, and NONE were correct."""
    text = f"The {s} and the {o} are discussed here."
    out, ctr = _run(nlp, text, [_rel(text, s, o, label, 0.8,
                                     "organization", "organization")])
    assert ctr["relex_endpoint_containment"] == 1
    assert not out[0].kept


def test_created_by_is_exempt_from_containment(nlp):
    """"(Aristotle's Poetics) -created_by-> (Aristotle)" contains its object
    and is CORRECT — possessive authorship puts the creator inside the work."""
    text = "Aristotle's Poetics shaped later criticism."
    out, ctr = _run(nlp, text, [_rel(text, "Aristotle's Poetics", "Aristotle",
                                     "created by", 0.9, "document", "person")])
    assert ctr["relex_endpoint_containment"] == 0
    assert out[0].kept


def test_contrast_sentence_cannot_assert_equivalence(nlp):
    """"Don't confuse X with Y" asserts they are NOT the same."""
    text = "Don't confuse Total Cost per Unit with Variable Cost per Unit here."
    out, ctr = _run(nlp, text, [_rel(text, "Total Cost per Unit",
                                     "Variable Cost per Unit", "synonym of",
                                     0.4, "concept", "concept")])
    assert ctr["relex_contrast_negated_equivalence"] == 1
    assert not out[0].kept


def test_table_cell_is_not_a_referent(nlp):
    text = "Columns: Agent | Responsibility and other notes follow."
    out, ctr = _run(nlp, text, [_rel(text, "Columns", "Agent | Responsibility",
                                     "includes", 0.5, "concept", "concept")])
    assert ctr["relex_malformed_endpoint"] == 1
    assert not out[0].kept


def test_pronoun_endpoint_is_rejected(nlp):
    """Relex still emits ~2.1% pronoun endpoints: "(I) -works for-> (Al)"."""
    text = "I worked with Al on the shoot."
    out, ctr = _run(nlp, text, [_rel(text, "I", "Al", "works for", 0.6,
                                     "person", "person")])
    assert ctr["relex_anchor_rejected"] == 1
    assert not out[0].kept


def test_every_candidate_is_returned_marked(nlp):
    """Nothing is silently discarded — a dropped relation comes back with a
    reason, which is what makes a precision number auditable."""
    text = "The retrieval service depends on Qdrant. I worked with Al."
    rels = [_rel(text, "retrieval service", "Qdrant", "depends on", 0.9),
            _rel(text, "I", "Al", "works for", 0.6, "person", "person")]
    out, _ = _run(nlp, text, rels)
    assert len(out) == len(rels)
    assert all(g.kept or g.dropped_reason for g in out)


def test_subsumption_keeps_the_most_specific_mention(nlp):
    """Relex emits overlapping spans by design; keeping all of them triple
    counts one fact in the graph."""
    text = ("Web hypertexts are part of a global experiment in digital "
            "textuality today.")
    rels = [
        _rel(text, "Web hypertexts", "global experiment in digital textuality",
             "part of", 0.9, "concept", "concept"),
        _rel(text, "Web hypertexts", "global experiment", "part of", 0.8,
             "concept", "concept"),
    ]
    out, ctr = _run(nlp, text, rels)
    assert ctr["relex_subsumed_duplicate"] == 1
    kept = [g for g in out if g.kept]
    assert len(kept) == 1
    assert kept[0].object == "global experiment in digital textuality"
