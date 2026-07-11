from services.retriever.intent_policy import (
    QueryNeed,
    adaptive_funnel_limits,
    infer_retrieval_intent,
    promote_compositional_intent,
)


def test_broad_query_uses_even_child_summary_mix():
    intent = infer_retrieval_intent("summarize the main themes across my documents")
    limits = adaptive_funnel_limits(intent, child_base=40, summary_base=20)

    assert intent.need == QueryNeed.BROAD
    assert intent.child_ratio == 0.50
    assert intent.summary_ratio == 0.50
    assert limits.child_top_k == 30
    assert limits.summary_top_k == 30


def test_specific_query_favors_child_chunks():
    intent = infer_retrieval_intent("show me the exact line for MoveTo")
    limits = adaptive_funnel_limits(intent, child_base=40, summary_base=20)

    assert intent.need == QueryNeed.SPECIFIC
    assert intent.child_ratio == 0.80
    assert intent.summary_ratio == 0.20
    assert limits.child_top_k == 48
    assert limits.summary_top_k == 12


def test_assessment_generation_is_broad_retrieval():
    intent = infer_retrieval_intent(
        "create an HTML test to test out my understanding ecommerce AI"
    )

    assert intent.need == QueryNeed.BROAD
    assert intent.broad_score >= 3


def test_ambiguous_query_uses_balanced_mix():
    intent = infer_retrieval_intent("the spider rig")
    limits = adaptive_funnel_limits(intent, child_base=40, summary_base=20)

    assert intent.need == QueryNeed.BALANCED
    assert intent.child_ratio == 0.65
    assert intent.summary_ratio == 0.35
    assert limits.child_top_k == 39
    assert limits.summary_top_k == 21


def test_relationship_query_uses_balanced_mix():
    intent = infer_retrieval_intent(
        "how does different personality correlate to the art of seduction"
    )
    limits = adaptive_funnel_limits(intent, child_base=40, summary_base=20)

    assert intent.need == QueryNeed.BALANCED
    assert intent.child_ratio == 0.65
    assert intent.summary_ratio == 0.35
    assert limits.child_top_k == 39
    assert limits.summary_top_k == 21


def test_intent_is_deterministic_and_idempotent():
    query = "Summarize how Humanoid MoveTo works"

    first = infer_retrieval_intent(query)
    second = infer_retrieval_intent(query)

    assert first == second


def test_compositional_plan_promotes_local_wording_to_broad_retrieval():
    initial = infer_retrieval_intent(
        "how do I find an audience and how do they respond and how do I open"
    )

    promoted = promote_compositional_intent(
        initial,
        complexity="compositional",
        required_lane_count=3,
    )

    assert promoted.need == QueryNeed.BROAD
    assert promoted.summary_ratio == 0.5


def test_single_obligation_keeps_original_retrieval_intent():
    initial = infer_retrieval_intent("how does this exact mechanism work")

    assert (
        promote_compositional_intent(
            initial,
            complexity="simple",
            required_lane_count=1,
        )
        == initial
    )
