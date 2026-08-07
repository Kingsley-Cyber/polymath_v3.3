"""Direct lane remains the strongest weighted-RRF weight."""

from services.retriever.cross_domain_rrf import planned_retriever_rrf_weights


def test_direct_dense_weight_is_strongest():
    class S:
        CROSS_DOMAIN_RRF_WEIGHT_DIRECT = 1.0
        CROSS_DOMAIN_RRF_WEIGHT_TRUSTED_CANONICAL = 0.8
        CROSS_DOMAIN_RRF_WEIGHT_LINKED_CHILD = 0.65
        CROSS_DOMAIN_RRF_WEIGHT_MONGO_LEXICAL = 0.8
        CROSS_DOMAIN_RRF_WEIGHT_SUMMARY_GUIDED = 0.65
        CROSS_DOMAIN_RRF_WEIGHT_GRAPH_CHILD = 0.85

    weights = planned_retriever_rrf_weights(S())
    assert weights["dense"] >= max(
        v for k, v in weights.items() if k != "dense"
    )


def test_misconfigured_direct_is_bumped_above_others():
    class S:
        CROSS_DOMAIN_RRF_WEIGHT_DIRECT = 0.5
        CROSS_DOMAIN_RRF_WEIGHT_TRUSTED_CANONICAL = 0.9
        CROSS_DOMAIN_RRF_WEIGHT_LINKED_CHILD = 0.9
        CROSS_DOMAIN_RRF_WEIGHT_MONGO_LEXICAL = 0.9
        CROSS_DOMAIN_RRF_WEIGHT_SUMMARY_GUIDED = 0.9
        CROSS_DOMAIN_RRF_WEIGHT_GRAPH_CHILD = 0.95

    weights = planned_retriever_rrf_weights(S())
    assert weights["dense"] > weights["graph"]
