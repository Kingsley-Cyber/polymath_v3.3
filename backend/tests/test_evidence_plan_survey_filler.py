"""Regression: broad "survey" queries must not decompose into filler lanes.

A query like "what are the major recurring themes across my entire library"
carries no substantive concept — only thematic scaffolding ("themes"),
generic modifiers ("major", "recurring", "entire") and a possessive pronoun
("my"). Commit 33d030c ("avoid fake lanes like tactics/full/across/spectrum")
started closing this class of bug but missed the thematic-survey vocabulary,
so the evidence plan built lanes = [major, recurring, themes, my].

Downstream damage that made this a real quality bug (observed live against
authentic_library):
  * the evidence plan reported `covered: major, recurring, themes, my` —
    coverage theater over non-concepts,
  * the final-context answerability gate scored chunks on stopword overlap
    (matched_terms=['my'], ['themes']) and DROPPED otherwise-valid breadth
    chunks whose only miss was not containing an arbitrary filler token,
  * the LLM received a nonsense evidence contract ("required lanes: major,
    recurring, themes, my").

Invariant: filler / thematic-scaffolding tokens never become evidence lanes;
real concepts in the same query still do.
"""

from services.retriever.evidence_plan import build_evidence_plan
from services.retriever.query_semantics import concept_groups

# Exact query reproduced live against authentic_library.
_SURVEY_QUERY = (
    "What are the major recurring themes across my entire library, "
    "and how do the different books connect?"
)

# Tokens that must never survive as a standalone evidence lane / concept.
_FILLER = {
    "major",
    "minor",
    "recurring",
    "theme",
    "themes",
    "entire",
    "my",
    "overall",
}


def test_survey_filler_tokens_do_not_become_concepts():
    keys = {group.key for group in concept_groups(_SURVEY_QUERY)}
    leaked = keys & _FILLER
    assert not leaked, f"filler tokens leaked as concepts: {sorted(leaked)}"


def test_broad_survey_query_builds_no_filler_lanes():
    plan = build_evidence_plan(_SURVEY_QUERY)
    lane_names = {lane.name for lane in plan.lanes}
    leaked = lane_names & _FILLER
    assert not leaked, f"filler tokens leaked as lanes: {sorted(leaked)}"
    # With no substantive concept, the plan must be inactive rather than
    # fabricate a lane contract the gate will then enforce on stopwords.
    assert not plan.active, (
        f"survey query should yield an inactive plan, got "
        f"mode={plan.mode!r} lanes={sorted(lane_names)}"
    )


def test_possessive_pronoun_my_is_never_a_lane():
    plan = build_evidence_plan("How do my documents describe personality types?")
    assert "my" not in {lane.name for lane in plan.lanes}


def test_survey_filler_is_stripped_but_real_concepts_survive():
    # Same thematic wrapper, but now two genuine concepts are present. The
    # filler must drop out while the real concepts remain as clean lanes.
    plan = build_evidence_plan(
        "What are the major recurring themes across personality frameworks "
        "and the art of seduction?"
    )
    lane_names = [lane.name for lane in plan.lanes]
    assert lane_names == ["personality_framework", "seduction"], lane_names
    assert not (set(lane_names) & _FILLER)
