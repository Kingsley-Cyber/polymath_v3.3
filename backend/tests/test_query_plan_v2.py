from services.retriever.query_plan import (
    answer_object_title_terms,
    build_query_plan_v2,
    contextualize_followup_query,
    query_plan_curation_query,
    query_plan_evidence_sides,
    query_plan_execution_lanes,
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


def test_uppercase_test_command_keeps_subject_and_drops_command_scaffolding():
    query = "CREATE ME A HTML TEST TO TEST OUT MY UNDERSTANDING ECOMMERCE AI"

    plan = build_query_plan_v2(query)

    assert plan.concepts == ("ECOMMERCE", "AI")
    assert _core_phrases(query) == ["ECOMMERCE", "AI"]
    assert not any("CREATE ME" in concept for concept in plan.concepts)
    assert not {"HTML", "TEST", "OUT", "UNDERSTANDING"} & set(plan.concepts)
    assert query_plan_curation_query(plan) == "ECOMMERCE AI"


def test_lowercase_assessment_command_keeps_only_subject_concept():
    plan = build_query_plan_v2(
        "Create a test to assess my understanding of cinematography and film editing"
    )

    assert plan.concepts == ("cinematography and film editing",)
    assert "assess" not in plan.concepts
    assert query_plan_curation_query(plan) == "cinematography and film editing"


def test_uppercase_branded_subject_is_not_fragmented_by_command_handling():
    query = "TEST MY UNDERSTANDING PURPLE OCEAN STRATEGY"

    plan = build_query_plan_v2(query)

    assert "PURPLE OCEAN STRATEGY" in plan.concepts
    assert not {"PURPLE", "OCEAN"} & set(plan.concepts)


def test_cross_domain_combine_preserves_both_semantic_sides():
    query = (
        "Combine Purple Ocean strategy with sticky messaging for an " "ecommerce offer."
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
    assert query_plan_curation_query(plan) == plan.original_query


def test_compare_named_book_and_strategy_does_not_split_titles():
    query = "Compare Purple Ocean strategy with Made to Stick principles."
    phrases = [phrase.lower() for phrase in _core_phrases(query)]

    assert "purple ocean strategy" in phrases
    assert any("made to stick" in phrase for phrase in phrases)
    assert "purple" not in phrases
    assert "ocean" not in phrases


def test_attribution_question_uses_named_source_as_one_lane():
    plan = build_query_plan_v2(
        "What makes a message sticky according to Made to Stick?"
    )

    assert plan.complexity == "simple"
    assert _core_phrases(plan.original_query) == ["Made to Stick"]
    assert plan.lanes[1].support_phrases[0] == "Made to Stick"


def test_regulatory_phrase_does_not_fragment_into_bare_tokens():
    plan = build_query_plan_v2(
        "What exact 2029 tax law does Purple Ocean require for lunar ecommerce stores?"
    )
    phrases = [phrase.lower() for phrase in _core_phrases(plan.original_query)]

    assert "purple ocean" in phrases
    assert "2029 tax law" in phrases
    assert "lunar ecommerce stores" in phrases
    assert not {"exact", "tax", "law", "lunar", "stores"} & set(phrases)


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
    phrases = {lane.phrase for lane in plan.lanes if lane.role == "core"}
    assert phrases == {"product positioning", "sticky messaging"}
    assert all("graph establish" not in str(phrase).lower() for phrase in phrases)


def test_graph_wording_does_not_pollute_product_positioning_lane():
    plan = build_query_plan_v2(
        "What relationship does the graph establish between product "
        "positioning and memorable messaging?"
    )
    phrases = {lane.phrase for lane in plan.lanes if lane.role == "core"}

    assert phrases == {"product positioning", "memorable messaging"}
    assert all("graph establish" not in str(phrase).lower() for phrase in phrases)


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
        (lane.phrase or "").lower() for lane in plan.lanes if lane.role == "core"
    ]

    assert "purple ocean differentiation mechanism" in phrases
    assert "sticky messaging" in phrases
    assert "product page" in phrases
    assert "find" not in phrases
    assert "then" not in phrases
    assert not any(
        phrase.startswith(("find ", "it to ", "evaluate ")) for phrase in phrases
    )
    sticky_lane = next(lane for lane in plan.lanes if lane.phrase == "sticky messaging")
    product_lane = next(lane for lane in plan.lanes if lane.phrase == "product page")
    assert sticky_lane.dense_text == "sticky messaging"
    assert "made to stick" in sticky_lane.query
    assert "made to stick" in sticky_lane.support_phrases
    assert product_lane.dense_text == "product page"
    assert "product detail page" in product_lane.query


def test_answer_object_books_survives_as_required_concept():
    plan = build_query_plan_v2("what books help with dropshipping and why?")

    assert plan.concepts == ("books", "dropshipping")
    assert query_plan_curation_query(plan) == plan.standalone_query
    assert "help" not in plan.concepts
    assert plan.answer_shape == "enumeration"
    books_lane = next(lane for lane in plan.lanes if lane.lane_id == "books")
    assert books_lane.dense_text == (
        "books book titles authors book recommendations lessons principles"
    )
    assert answer_object_title_terms(plan) == {"books": ("books",)}
    assert [probe.probe_id for probe in plan.probes] == [
        "books",
        "books_justification",
    ]
    assert all(len(probe.question.split()) >= 4 for probe in plan.probes)


def test_compositional_beginner_query_decomposes_into_complete_probes():
    query = (
        "SO IF I WANTED TO START DAY 0 DROPSHIPPING WHERE DO I EVEN BEGIN "
        "AND WHAT DO I DO? what books should i reads for this"
    )

    plan = build_query_plan_v2(query)
    execution_lanes = query_plan_execution_lanes(plan)

    assert plan.concepts == ("books", "dropshipping")
    assert plan.answer_shape == "synthesis"
    assert [probe.probe_id for probe in plan.probes] == [
        "day_zero_steps",
        "beginner_books",
    ]
    assert plan.probes[0].question == (
        "What steps should a beginner take on Day 0 to start dropshipping?"
    )
    assert plan.probes[1].question == (
        "Which books does the corpus recommend to a beginner starting dropshipping?"
    )
    assert all(probe.required for probe in plan.probes)
    assert not {"so", "if", "wanted", "start", "day", "begin"} & {
        concept.lower() for concept in plan.concepts
    }
    assert [lane.lane_id for lane in execution_lanes] == [
        "original",
        "day_zero_steps",
        "beginner_books",
    ]
    assert all(
        len(lane.query.split()) >= 4
        for lane in execution_lanes
        if lane.role == "core"
    )
    assert execution_lanes[2].dense_text.startswith("books book titles authors")


def test_probe_contract_is_serialized_and_drives_evidence_sides():
    plan = build_query_plan_v2(
        "How should I start dropshipping and what books should I read?"
    )

    payload = query_plan_to_dict(plan)
    sides = query_plan_evidence_sides(plan)

    assert payload["probes"]
    assert [side["name"] for side in sides] == [
        probe.probe_id for probe in plan.probes if probe.required
    ]
    assert all(str(side["query"]).endswith("?") for side in sides)


def test_terse_followup_uses_previous_user_subject_without_model_call():
    recent = [
        {"role": "user", "content": "what books help with dropshipping and why?"},
        {"role": "assistant", "content": "The evidence was incomplete."},
    ]

    standalone = contextualize_followup_query("no authors", recent)
    plan = build_query_plan_v2("no authors", standalone_query=standalone)

    assert standalone == "what books help with dropshipping and why; authors"
    assert plan.original_query == "no authors"
    assert plan.standalone_query == standalone
    assert plan.concepts == ("books", "dropshipping", "authors")
    assert query_plan_curation_query(plan) == standalone
