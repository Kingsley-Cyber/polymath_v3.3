from services.retriever.evidence_plan import (
    build_evidence_plan,
    evidence_lane_matches_text,
)


def test_relationship_query_builds_independent_evidence_lanes():
    plan = build_evidence_plan(
        "how does different personality correlate to the art of seduction "
        "with people as men dating women"
    )

    assert plan.mode == "multi_concept_relationship"
    assert [lane.name for lane in plan.lanes] == [
        "personality_framework",
        "seduction",
    ]
    assert "relationship" in plan.operators
    assert "art" not in [lane.name for lane in plan.lanes]
    assert "personality" not in [lane.name for lane in plan.lanes]
    personality_lane = plan.lanes[0]
    assert "four tendencies" in personality_lane.query
    assert "handbook of personality" in personality_lane.query


def test_simple_definition_query_still_gets_a_semantic_lane():
    plan = build_evidence_plan("what is natural language processing")

    assert plan.mode == "single_concept"
    assert [lane.name for lane in plan.lanes] == ["nlp"]


def test_generic_relationship_query_builds_lanes_from_detected_concepts():
    plan = build_evidence_plan("how does NLP relate to Python")

    assert plan.mode == "multi_concept_relationship"
    assert [lane.name for lane in plan.lanes] == ["nlp", "python"]


def test_multi_concept_query_decomposes_without_relationship_operator():
    plan = build_evidence_plan("personality seduction")

    assert plan.mode == "multi_concept"
    assert [lane.name for lane in plan.lanes] == ["personality", "seduction"]


def test_evidence_lane_match_uses_alias_boundaries():
    plan = build_evidence_plan(
        "how does different personality correlate to the art of seduction"
    )
    lane = plan.lanes[0]

    assert evidence_lane_matches_text(lane, "The Four Tendencies is a framework.")
    assert not evidence_lane_matches_text(lane, "A fair tendency in prose.")
