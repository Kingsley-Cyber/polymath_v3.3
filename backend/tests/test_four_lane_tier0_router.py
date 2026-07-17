from config import Settings
from models.schemas import SourceChunk
from services.retriever.four_lane_router import (
    BRIDGE_SUBQUERY,
    DocumentProfile,
    QueryOntology,
    add_bridge_subquery_lane,
    associative_document_scores,
    bm25_document_scores,
    child_rollup_scores,
    fuse_document_lanes,
    resolve_query_ontology,
    _source_versions_by_document,
)
from services.retriever.query_plan import QueryLane


def _profile(
    doc_id: str,
    *,
    title: str,
    summary: str = "",
    domains=(),
    frames=(),
    latent=(),
) -> DocumentProfile:
    return DocumentProfile(
        corpus_id="c",
        doc_id=doc_id,
        title=title,
        summary=summary,
        domains=set(domains),
        frames=set(frames),
        latent_terms=set(latent),
        digest_parent_ids={f"parent-{doc_id}"}
        if domains or frames or latent
        else set(),
    )


def test_router_and_decomposition_flags_ship_default_off():
    settings = Settings()

    assert settings.FOUR_LANE_TIER0_ROUTER_ENABLED is False
    assert settings.FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED is False
    assert settings.FOUR_LANE_TIER0_MAX_DOCUMENTS == 6


def test_lexical_bm25_uses_title_summary_and_headings():
    directing = _profile(
        "directing",
        title="Directing the Story",
        summary="Block actors around narrative intention.",
    )
    directing.headings = ("Visual storytelling", "Dramatic beats")
    lens = _profile(
        "lens",
        title="Language of the Lens",
        summary="Focal lengths and optical characteristics.",
    )

    scores = bm25_document_scores(
        "how do dramatic beats guide visual storytelling?",
        [lens, directing],
    )

    assert scores[("c", "directing")] == 1.0
    assert ("c", "lens") not in scores


def test_child_rollup_aggregates_existing_hits_without_parent_vectors():
    chunks = [
        SourceChunk(
            chunk_id="a1",
            parent_id="pa",
            doc_id="a",
            corpus_id="c",
            text="a",
            score=0.8,
            source_tier="tier_b",
        ),
        SourceChunk(
            chunk_id="a2",
            parent_id="pa",
            doc_id="a",
            corpus_id="c",
            text="a",
            score=0.7,
            source_tier="tier_b",
        ),
        SourceChunk(
            chunk_id="b1",
            parent_id="pb",
            doc_id="b",
            corpus_id="c",
            text="b",
            score=0.75,
            source_tier="tier_b",
        ),
    ]

    scores = child_rollup_scores(chunks)

    assert scores[("c", "a")] == 1.0
    assert 0 < scores[("c", "b")] < 1.0


def test_query_ontology_reuses_t91_domain_resolver_and_affinity_view():
    ontology = resolve_query_ontology(
        "How can artificial intelligence support film storytelling?"
    )

    assert {"D09", "D13"} <= ontology.domains
    assert {"MF01", "MF02", "MF06"} <= ontology.frames
    assert ontology.resolver_recipe_hash.startswith("sha256:")


def test_associative_lane_matches_digest_ontology_and_latent_concepts():
    story = _profile(
        "story",
        title="Directing",
        domains={"D13"},
        frames={"MF01", "MF02"},
        latent={"narrative intention"},
    )
    lens = _profile(
        "lens",
        title="Lens manual",
        domains={"D09"},
        frames={"MF03"},
        latent={"focal length"},
    )
    query = QueryOntology(
        domains=frozenset({"D13"}),
        frames=frozenset({"MF01", "MF02"}),
        terms=frozenset({"narrative", "intention"}),
    )

    scores, traces = associative_document_scores(query, [story, lens])

    assert scores[("c", "story")] == 1.0
    assert ("c", "lens") not in scores
    assert traces[("c", "story")]["digest_parent_ids"] == ["parent-story"]


def test_fusion_reserves_associative_seat_and_demotes_divergent_surface_match():
    story = _profile(
        "story",
        title="Directing the Story",
        domains={"D13"},
        frames={"MF01"},
    )
    lens = _profile(
        "lens",
        title="Camera Language",
        domains={"D09"},
        frames={"MF03"},
    )
    profiles = {
        ("c", "story"): story,
        ("c", "lens"): lens,
    }
    routes, diagnostics = fuse_document_lanes(
        profiles=profiles,
        lane_scores={
            "lexical": {("c", "story"): 0.45, ("c", "lens"): 1.0},
            "semantic": {("c", "story"): 0.55, ("c", "lens"): 0.95},
            "child_rollup": {("c", "story"): 0.40, ("c", "lens"): 1.0},
            "associative": {("c", "story"): 0.90},
        },
        associative_traces={("c", "story"): {"domains": ["D13"], "frames": ["MF01"]}},
        query_ontology_active=True,
        max_documents=2,
    )

    assert [route.doc_id for route in routes] == ["story", "lens"]
    assert routes[0].routing_trace["seat_owner"] == "associative"
    assert routes[1].routing_trace["divergent_profile_demoted"] is True
    assert diagnostics["lane_seats"]["associative"] == ["c:story"]


def test_quota_underfill_spills_to_strong_remaining_document():
    profiles = {
        ("c", "a"): _profile("a", title="A"),
        ("c", "b"): _profile("b", title="B"),
        ("c", "c"): _profile("c", title="C"),
    }

    routes, diagnostics = fuse_document_lanes(
        profiles=profiles,
        lane_scores={
            "lexical": {("c", "a"): 1.0, ("c", "c"): 0.7},
            "semantic": {("c", "b"): 0.9},
            "child_rollup": {},
            "associative": {},
        },
        associative_traces={},
        query_ontology_active=False,
        max_documents=3,
    )

    assert {route.doc_id for route in routes} == {"a", "b", "c"}
    assert diagnostics["spillover_seats"] >= 1


def test_threshold_spillover_does_not_fill_with_weak_document():
    profiles = {
        ("c", "strong"): _profile("strong", title="Strong"),
        ("c", "weak"): _profile("weak", title="Weak"),
    }

    routes, _diagnostics = fuse_document_lanes(
        profiles=profiles,
        lane_scores={
            "lexical": {("c", "strong"): 0.9, ("c", "weak"): 0.01},
            "semantic": {},
            "child_rollup": {},
            "associative": {},
        },
        associative_traces={},
        query_ontology_active=False,
        max_documents=6,
    )

    assert [route.doc_id for route in routes] == ["strong"]


def test_bridge_subquery_is_exactly_once_and_never_required():
    existing = QueryLane(
        lane_id="planner_decomposition_0",
        role="core",
        query="What craft matters?",
        dense_text="What craft matters?",
        lexical_terms=("craft",),
        required=False,
    )

    once = add_bridge_subquery_lane([existing])
    twice = add_bridge_subquery_lane(once)

    bridge = [
        lane for lane in twice if lane.lane_id == "router_bridge_underlying_crafts"
    ]
    assert len(bridge) == 1
    assert bridge[0].query == BRIDGE_SUBQUERY
    assert bridge[0].required is False


def test_digest_payload_requires_projection_provenance():
    from services.retriever.four_lane_router import _valid_digest_payload

    valid = {
        "chunk_type": "semantic_digest",
        "projection_role": "semantic_digest",
        "corpus_id": "c",
        "doc_id": "d",
        "parent_id": "p",
        "artifact_id": "a",
        "artifact_revision_id": "rev:" + "1" * 64,
        "projection_manifest_id": "projm:" + "2" * 64,
        "projection_profile_hash": "sha256:" + "3" * 64,
        "source_cache_key": "cache",
        "source_job_id": "job",
        "source_version_id": "source",
        "schema_hash": "sha256:" + "4" * 64,
        "prompt_hash": "sha256:" + "5" * 64,
        "output_hash": "sha256:" + "6" * 64,
        "projected_payload_hash": "sha256:" + "7" * 64,
    }

    assert _valid_digest_payload(valid)
    assert not _valid_digest_payload({**valid, "source_job_id": ""})
    assert not _valid_digest_payload({**valid, "chunk_type": "parent"})


def test_digest_source_closure_fails_closed_per_document():
    documents = [
        {
            "corpus_id": "c",
            "doc_id": "valid",
            "source_identity": {"content_sha256": "a" * 64},
        },
        {
            "corpus_id": "c",
            "doc_id": "legacy",
        },
    ]

    source_versions = _source_versions_by_document(documents)

    assert ("c", "valid") in source_versions
    assert ("c", "legacy") not in source_versions
