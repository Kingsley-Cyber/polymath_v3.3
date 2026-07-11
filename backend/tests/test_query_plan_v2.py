from services.retriever.query_plan import (
    build_query_plan_v2,
    query_plan_evidence_sides,
    query_plan_to_dict,
)


def _core_phrases(query: str) -> list[str]:
    plan = build_query_plan_v2(query)
    return [lane.phrase or "" for lane in plan.lanes if lane.role == "core"]


def test_named_strategy_stays_one_phrase_lane():
    plan = build_query_plan_v2("What is Purple Ocean strategy?")

    assert plan.complexity == "simple"
    assert plan.lanes[0].lane_id == "original"
    assert _core_phrases(plan.original_query) == ["Purple Ocean strategy"]
    assert "purple" not in [lane.lane_id for lane in plan.lanes]
    assert "ocean" not in [lane.lane_id for lane in plan.lanes]


def test_cross_domain_combine_preserves_both_semantic_sides():
    query = (
        "Combine Purple Ocean strategy with sticky messaging for an "
        "ecommerce offer."
    )
    plan = build_query_plan_v2(query, corpus_ids=["marketing", "commerce"])
    phrases = [phrase.lower() for phrase in _core_phrases(query)]

    assert plan.complexity == "comparative"
    assert any("purple ocean strategy" == phrase for phrase in phrases)
    assert any("sticky messag" in phrase for phrase in phrases)
    assert not any(phrase in {"combine", "brand", "ecommerce"} for phrase in phrases)
    assert "combine purple ocean" not in phrases
    assert plan.corpus_ids == ("marketing", "commerce")
    bridge = [lane for lane in plan.lanes if lane.role == "bridge"]
    assert len(bridge) == 1
    assert set(bridge[0].depends_on) == {
        lane.lane_id for lane in plan.lanes if lane.role == "core"
    }


def test_compare_named_book_and_strategy_does_not_split_titles():
    query = "Compare Purple Ocean strategy with Made to Stick principles."
    phrases = [phrase.lower() for phrase in _core_phrases(query)]

    assert "purple ocean strategy" in phrases
    assert any("made to stick" in phrase for phrase in phrases)
    assert "purple" not in phrases
    assert "ocean" not in phrases


def test_plan_is_serializable_and_evidence_compatible():
    plan = build_query_plan_v2(
        "What is the relationship between product positioning and sticky messaging?"
    )
    payload = query_plan_to_dict(plan)
    sides = query_plan_evidence_sides(plan)

    assert payload["version"] == "query_plan.v2"
    assert payload["lanes"][0]["role"] == "original"
    assert len(sides) >= 2
    assert all(side["query"] for side in sides)


def test_dependency_language_marks_multi_hop_plan():
    plan = build_query_plan_v2(
        "Find Purple Ocean strategy, then use it to evaluate sticky messaging."
    )
    assert plan.complexity == "dependent_multi_hop"
    assert plan.max_repair_rounds == 1


def test_multi_hop_plan_removes_imperative_scaffolding():
    plan = build_query_plan_v2(
        "Find the Purple Ocean differentiation mechanism, then use it to "
        "evaluate sticky messaging for a product page."
    )
    phrases = [
        (lane.phrase or "").lower()
        for lane in plan.lanes
        if lane.role == "core"
    ]

    assert "purple ocean differentiation mechanism" in phrases
    assert "sticky messaging" in phrases
    assert "product page" in phrases
    assert not any(phrase.startswith(("find ", "it to ", "evaluate ")) for phrase in phrases)
    sticky_lane = next(lane for lane in plan.lanes if lane.phrase == "sticky messaging")
    product_lane = next(lane for lane in plan.lanes if lane.phrase == "product page")
    assert sticky_lane.dense_text == "sticky messaging"
    assert "made to stick" in sticky_lane.query
    assert product_lane.dense_text == "product page"
    assert "product detail page" in product_lane.query
