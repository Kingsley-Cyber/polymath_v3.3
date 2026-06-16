from services.chat_orchestrator import (
    _is_weak_ingest_profile_lane,
)


def test_weak_long_ingest_profile_lane_is_breadth_hint():
    assert _is_weak_ingest_profile_lane(
        {
            "source": "ingest_facet_profile",
            "name": "denis_rothman_transformers_natural_language_processing_build_train",
            "matched": ["network", "neural"],
            "match_score": 4.4,
        }
    )


def test_strong_ingest_profile_lane_can_still_be_explicit():
    assert not _is_weak_ingest_profile_lane(
        {
            "source": "ingest_facet_profile",
            "name": "neural_networks",
            "matched": ["neural networks"],
            "match_score": 10.0,
        }
    )
