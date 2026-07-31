"""Frame-licensed pairing model — behavior locks.

Each test here corresponds to a defect that was MEASURED on live corpus text
during the 2026-07-30 rebuild, not to a hypothetical. Hand-judged precision
moved 0.15 -> 0.70 (book) and 0.10 -> 0.75 (ASR) across these fixes.

Host venue: these need config/*.yaml, so run with the sidecar venv
(local_ghost_b/.venv), not inside polymath_v33-backend-1's old image.
"""

from __future__ import annotations

import pytest

from services.extraction.dep_path_extractor import EntitySpan, new_counters
from services.extraction.frame_extractor import (
    FrameExtractor,
    _is_bibliographic_context,
)


@pytest.fixture(scope="module")
def extractor():
    return FrameExtractor()


def _run(extractor, text, ents):
    spans = [EntitySpan(s, a, b, t) for s, a, b, t in ents]
    counters = new_counters()
    triples = extractor.extract(
        text=text, entities=spans, suppression_counters=counters
    )
    return [
        (t.subject_surface, t.predicate, t.object_surface) for t in triples
    ], counters


# ---------------------------------------------------------------------------
# The core reason this model exists: co-presence is not a relation.
# ---------------------------------------------------------------------------


class TestCoPresenceIsNotARelation:
    def test_unrelated_entities_in_one_sentence_emit_nothing(self, extractor):
        """The defect that motivated the rebuild.

        Shortest-path pairing always found a path between any two entities in a
        sentence and let the resolver name it, producing edges like
        (INSIDE, instance_of, Earth) from a chapter heading. In a frame model
        these entities fill no shared predicate's slots, so nothing is licensed.
        """
        out, _ = _run(
            extractor,
            "CHAPTER 13 INSIDE NOTHING. Once a photograph of the Earth is "
            "available, a new idea will be let loose.",
            [("INSIDE", 11, 17, "Concept"), ("Earth", 52, 57, "Location")],
        )
        assert out == [], f"co-present entities must not relate; got {out}"

    def test_caption_co_presence_emits_nothing(self, extractor):
        out, _ = _run(
            extractor,
            "Figure 16-6: Daily high and low temperatures for Death Valley.",
            [("Figure 16-6", 0, 11, "Document"),
             ("Death Valley", 48, 60, "Location")],
        )
        assert out == []


# ---------------------------------------------------------------------------
# Direction has exactly one owner per frame type.
# ---------------------------------------------------------------------------


class TestDirection:
    def test_active_and_passive_agree(self, extractor):
        """Same fact, two voices, one direction.

        XOR-ing the frame's structural swap with the resolver's signature swap
        cancelled the correction and emitted (GitHub, owns, Microsoft).
        """
        active, _ = _run(
            extractor, "Microsoft acquired GitHub in 2018.",
            [("Microsoft", 0, 9, "Organization"), ("GitHub", 19, 25, "Software")],
        )
        passive, _ = _run(
            extractor, "GitHub was acquired by Microsoft.",
            [("GitHub", 0, 6, "Software"), ("Microsoft", 23, 32, "Organization")],
        )
        assert ("Microsoft", "owns", "GitHub") in active
        assert ("Microsoft", "owns", "GitHub") in passive

    def test_object_perspective_predicate_is_inverted_from_active_voice(
        self, extractor
    ):
        """created_by names the CREATION as subject.

        The T3 synonym table maps build/create/develop -> created_by and always
        reports swap=False, so an active sentence produced
        (team, created_by, framework) — backwards.
        """
        out, _ = _run(
            extractor, "The team built the framework using Rust.",
            [("team", 4, 8, "Organization"), ("framework", 19, 28, "Software")],
        )
        assert ("framework", "created_by", "team") in out


# ---------------------------------------------------------------------------
# Nominal frames: strict slots, no verb requirement.
# ---------------------------------------------------------------------------


class TestNominalFrames:
    def test_possessive_emits_even_when_the_sentence_reads_verbless(
        self, extractor
    ):
        """en_core_web_sm tags "powers" as NOUN, so the sentence looks verbless.

        The old chunk-level verbless bail discarded the whole chunk, taking a
        perfectly good possessive with it.
        """
        out, _ = _run(
            extractor, "Google's TensorFlow powers many systems.",
            [("Google", 0, 6, "Organization"), ("TensorFlow", 9, 19, "Software")],
        )
        assert ("Google", "owns", "TensorFlow") in out

    def test_possessive_requires_the_possessed_noun_itself_to_be_an_entity(
        self, extractor
    ):
        """Strict slots.

        "it has a halo effect on your prospects' perception" — the possessed
        noun is `perception`, which is not an entity. Walking up the tree found
        `halo effect` and fabricated (prospects, owns, halo effect).
        """
        out, _ = _run(
            extractor,
            "It has a halo effect on your prospects' perception of the product.",
            [("halo effect", 9, 20, "Concept"),
             ("prospects", 28, 37, "Person")],
        )
        assert not [t for t in out if t[1] == "owns"], (
            f"possessive must not bind past the possessed noun; got {out}"
        )


# ---------------------------------------------------------------------------
# Bibliographies reuse the appositive shape as formatting, not assertion.
# ---------------------------------------------------------------------------


class TestBibliographicSuppression:
    @pytest.mark.parametrize("sent", [
        "Newbury Park, CA: Sage (1990).",
        "Gandy, O.: The Panoptic Sort: A Political Economy of Personal Information.",
        "Siegrist, M., Cvetkovich, G.T., Gutscher, H.: Shared values and trust.",
        "Alva Noe, Varieties of Presence, Harvard University Press, 2015, p 125.",
        "Smith, J. et al. Advances in retrieval.",
    ])
    def test_citation_shapes_are_recognized(self, extractor, sent):
        doc = extractor._nlp(sent)
        assert any(_is_bibliographic_context(s) for s in doc.sents), (
            f"should read as bibliographic: {sent!r}"
        )

    def test_ordinary_prose_appositive_is_not_suppressed(self, extractor):
        """The guard must not eat real definitional appositives."""
        doc = extractor._nlp(
            "Qdrant, a vector database, is written in Rust."
        )
        assert not any(_is_bibliographic_context(s) for s in doc.sents)

    def test_citation_appositive_emits_no_edge(self, extractor):
        out, _ = _run(
            extractor, "Newbury Park, CA: Sage (1990).",
            [("Newbury Park", 0, 12, "Location"), ("Sage", 18, 22, "Organization")],
        )
        assert out == []


# ---------------------------------------------------------------------------
# Argument hygiene carries over from the precision ladder.
# ---------------------------------------------------------------------------


class TestArgumentHygiene:
    def test_pronoun_arguments_are_rejected(self, extractor):
        out, counters = _run(
            extractor, "We built the software ourselves.",
            [("We", 0, 2, "Person"), ("software", 13, 21, "Software")],
        )
        assert out == []
        assert counters["suppressed_pronoun_argument"] >= 1

    def test_agentless_passive_forms_no_frame(self, extractor):
        """An unstated agent is not an argument, so nothing is licensed."""
        out, _ = _run(
            extractor, "The framework was rewritten last year.",
            [("framework", 4, 13, "Software")],
        )
        assert out == []


# ---------------------------------------------------------------------------
# Counters stay honest.
# ---------------------------------------------------------------------------


def test_every_frame_counter_is_canonical():
    """Frame counters must live in the extractor's canonical key list."""
    from services.extraction.dep_path_extractor import ALL_COUNTER_KEYS
    for key in (
        "frame_slot_unfilled",
        "frame_self_loop",
        "frame_predicate_unnamed",
        "frame_bibliographic_appositive",
    ):
        assert key in ALL_COUNTER_KEYS, f"{key} not published"
