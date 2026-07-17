from scripts.run_tier0_bridge_diagnostic import (
    REQUIRED_ATTRIBUTION,
    _score_case,
    rank_routed_documents,
)


def _attribution():
    scores = {lane: 0.0 for lane in REQUIRED_ATTRIBUTION}
    scores["associative"] = 0.8
    return {
        "router_version": "four_lane_tier0_router.v1",
        "seat_owner": "associative",
        "lane_scores": scores,
        "effective_lane_scores": scores,
    }


def test_rank_routed_documents_uses_max_score_and_canonical_document_name():
    ranked = rank_routed_documents(
        {
            "lane_b": [
                {
                    "corpus_id": "c",
                    "doc_id": "story",
                    "title": "slug.md",
                    "score": 0.4,
                    "routing_trace": _attribution(),
                }
            ],
            "lane_a": [
                {
                    "corpus_id": "c",
                    "doc_id": "lens",
                    "title": "lens-slug.md",
                    "score": 0.5,
                    "routing_trace": _attribution(),
                },
                {
                    "corpus_id": "c",
                    "doc_id": "story",
                    "title": "slug.md",
                    "score": 0.9,
                    "routing_trace": _attribution(),
                },
            ],
        },
        document_names={"story": "Directing the Story.md", "lens": "Lens.md"},
    )

    assert [row["doc_id"] for row in ranked] == ["story", "lens"]
    assert ranked[0]["title"] == "Directing the Story.md"
    assert ranked[0]["max_score"] == 0.9
    assert ranked[0]["lane_ids"] == ["lane_a", "lane_b"]


def test_bridge_case_requires_expected_top_three_and_complete_attribution():
    routes = {
        "craft": [
            {
                "corpus_id": "c",
                "doc_id": "story",
                "title": "slug.md",
                "score": 0.9,
                "routing_trace": _attribution(),
            },
            {
                "corpus_id": "c",
                "doc_id": "other",
                "title": "other.md",
                "score": 0.5,
                "routing_trace": _attribution(),
            },
        ]
    }
    raw = {
        "errors": [],
        "done": {"type": "done"},
        "elapsed_seconds": 1.0,
        "traces": [
            {
                "title": "Chat model route",
                "metadata": {"model": "anthropic/minimax-m2.7"},
            },
            {
                "title": "Local RAG retrieval",
                "metadata": {
                    "retrieval_diagnostics": {
                        "document_routing": {
                            "version": "four_lane_tier0_router.v1",
                            "routes": routes,
                        }
                    }
                },
            },
        ],
    }

    scored = _score_case(
        case={
            "id": "bridge",
            "question": "question",
            "expected_title_any": ["Directing the Story.md"],
        },
        raw=raw,
        expect_router_enabled=True,
        document_names={"story": "Directing the Story.md", "other": "Other.md"},
    )

    assert scored["technical_success"] is True
    assert scored["expected_hit_top_three"] is True
    assert scored["forbidden_rank1"] is False
    assert scored["attribution_complete"] is True
    assert scored["passed"] is True
