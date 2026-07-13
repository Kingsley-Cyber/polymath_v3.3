"""P1.5 deterministic shelf-role engine — v0 eligibility invariants.

Fixture cards are built by hand in the ``librarian_card.v0`` schema (see
services/librarian/card_builder.py docstring). Tests validate the shared
eligibility invariants — subject/capability/principle overlap, evidence
gates, versioned policy triggers, dedupe, determinism — not any corpus's
wording.
"""

from __future__ import annotations

import random

import pytest

from services.ingestion.corpus_lexicon import normalize_identity
from services.librarian.shelf_engine import (
    ADJACENT_MAX_SUBJECT_OVERLAP,
    DIRECT_MIN_OVERLAP,
    assign_shelf_roles,
)
from services.librarian.shelf_policy_data import (
    COUNTERBALANCE_KEYS,
    HIGH_MISUSE_KEYS,
    POLICY_VERSION,
)

QUERY = ["attention", "persuasion", "visual_storytelling"]

# Policy-data-driven fixtures: pick real versioned keys instead of hardcoding
# one, so tests validate the policy mechanism, not a specific curated key.
MISUSE_KEY = sorted(HIGH_MISUSE_KEYS)[0]
COUNTER_KEY = sorted(COUNTERBALANCE_KEYS)[0]


def entry(value: str, *, source_ids=("parent-1", "chunk-1"), support=1) -> dict:
    return {
        "value": value,
        "value_key": normalize_identity(value),
        "method": "test_seed",
        "source_ids": list(source_ids),
        "confidence": 1.0,
        "support": support,
    }


def make_card(
    doc_id: str,
    *,
    corpus_id: str = "corpus-a",
    subjects=(),
    latent=(),
    mechanisms=(),
    capabilities=(),
    principles=(),
    **overrides,
) -> dict:
    def entries(values):
        return [v if isinstance(v, dict) else entry(v) for v in values]

    card = {
        "schema_version": "librarian_card.v0",
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "central_subjects": entries(subjects),
        "candidate_latent_subjects": entries(latent),
        "mechanisms_taught": entries(mechanisms),
        "capabilities_developed": entries(capabilities),
        "problems_addressed": [],
        "transferable_principles": entries(principles),
        "risks_or_likely_misuse": [],
        "counterbalancing_concepts": [],
        "evidence_spans": {
            "source_parent_ids": ["parent-1"],
            "source_chunk_ids": ["chunk-1"],
            "section_ids": [],
        },
    }
    card.update(overrides)
    return card


def roles_of(result: dict, doc_id: str) -> dict[str, dict]:
    for assignment in result["assignments"]:
        if assignment["doc_id"] == doc_id:
            return {role["role"]: role for role in assignment["roles"]}
    return {}


# ── direct ───────────────────────────────────────────────────────────────


def test_direct_via_central_subject_overlap():
    card = make_card("doc-direct", subjects=["attention", "persuasion", "pottery"])
    result = assign_shelf_roles(QUERY, [card])
    role = roles_of(result, "doc-direct")["direct"]
    assert role["matched_fields"]["central_subjects"] == ["attention", "persuasion"]
    assert role["score"] == pytest.approx(2 / 3, abs=1e-3)
    assert role["score"] >= DIRECT_MIN_OVERLAP
    assert role["evidence_ids"]  # matched entries' source ids
    assert result["shelf_counts"]["direct"] == 1


def test_latent_only_overlap_never_qualifies_direct():
    card = make_card("doc-latent", subjects=["pottery"], latent=["attention"])
    result = assign_shelf_roles(QUERY, [card])
    assert "direct" not in roles_of(result, "doc-latent")
    assert result["shelf_counts"]["direct"] == 0
    assert result["skipped_roles"]["direct"] == "no_candidate_met_v0_eligibility"


def test_latent_subjects_corroborate_a_qualifying_direct():
    card = make_card("doc-both", subjects=["attention"], latent=["persuasion"])
    result = assign_shelf_roles(QUERY, [card])
    role = roles_of(result, "doc-both")["direct"]
    # Gate satisfied by central_subjects alone; latent only corroborates.
    assert role["matched_fields"]["central_subjects"] == ["attention"]
    assert role["matched_fields"]["candidate_latent_subjects"] == ["persuasion"]
    assert any("corroborated" in reason for reason in role["reasons"])


# ── foundational ─────────────────────────────────────────────────────────


def test_foundational_needs_capability_and_mechanism_evidence():
    no_mechanisms = make_card("doc-cap-only", capabilities=["persuasion"])
    backed = make_card(
        "doc-foundational",
        capabilities=["persuasion"],
        mechanisms=[entry("framing", source_ids=("parent-9",))],
    )
    result = assign_shelf_roles(QUERY, [no_mechanisms, backed])
    assert "foundational" not in roles_of(result, "doc-cap-only")
    role = roles_of(result, "doc-foundational")["foundational"]
    assert role["matched_fields"]["capabilities_developed"] == ["persuasion"]
    assert role["matched_fields"]["mechanisms_taught"] == ["framing"]
    assert "parent-9" in role["evidence_ids"]


# ── adjacent ─────────────────────────────────────────────────────────────


def test_adjacent_requires_meaningfully_different_subjects():
    same_subjects = make_card(
        "doc-same",
        subjects=["attention", "persuasion"],
        capabilities=["visual_storytelling"],
    )
    different_subjects = make_card(
        "doc-adjacent",
        subjects=["ceramics"],
        capabilities=["visual_storytelling"],
    )
    result = assign_shelf_roles(QUERY, [same_subjects, different_subjects])
    assert "adjacent" not in roles_of(result, "doc-same")  # 2/3 >= max bound
    role = roles_of(result, "doc-adjacent")["adjacent"]
    assert role["matched_fields"]["capabilities_developed"] == ["visual_storytelling"]
    assert any(
        f"ADJACENT_MAX_SUBJECT_OVERLAP={ADJACENT_MAX_SUBJECT_OVERLAP}" in r
        for r in role["reasons"]
    )


# ── bridge ───────────────────────────────────────────────────────────────


def test_bridge_requires_shared_principle_evidence_on_both_sides():
    query = ["feedback_loops", "attention", "persuasion", "visual_storytelling"]
    bridged = make_card(
        "doc-bridge",
        subjects=["ceramics"],
        principles=[entry("feedback_loops", source_ids=("parent-7", "chunk-7"))],
    )
    no_card_evidence = make_card(
        "doc-no-evidence",
        subjects=["gardening"],
        principles=[entry("feedback_loops", source_ids=())],
    )
    same_subjects = make_card(
        "doc-bridge-same-subject",
        subjects=["attention", "persuasion"],
        principles=[entry("feedback_loops", source_ids=("parent-8",))],
    )
    result = assign_shelf_roles(query, [bridged, no_card_evidence, same_subjects])

    role = roles_of(result, "doc-bridge")["bridge"]
    assert role["matched_fields"]["transferable_principles"] == ["feedback_loops"]
    assert role["evidence_ids"] == ["chunk-7", "parent-7"]  # card-side evidence
    assert role["query_evidence_ids"] == ["feedback_loops"]  # query-side record
    assert role["chains"] == [
        {
            "document": "doc-bridge",
            "concept": "feedback_loops",
            "transferable_principle": "feedback_loops",
            "user_goal": "feedback_loops",
            "via_field": "transferable_principles",
        }
    ]
    # Entries without source ids never bridge (evidence on BOTH sides).
    assert "bridge" not in roles_of(result, "doc-no-evidence")
    # Similar central subjects never bridge (different-subject requirement).
    assert "bridge" not in roles_of(result, "doc-bridge-same-subject")


# ── counterbalance ───────────────────────────────────────────────────────


def test_counterbalance_triggers_only_via_policy_keys():
    query = [MISUSE_KEY, "brand_building"]
    counter = make_card("doc-counter", subjects=[COUNTER_KEY])
    plain = make_card("doc-plain", subjects=["ceramics"])
    result = assign_shelf_roles(query, [counter, plain])
    role = roles_of(result, "doc-counter")["counterbalance"]
    assert role["matched_fields"]["central_subjects"] == [COUNTER_KEY]
    assert any(POLICY_VERSION in reason for reason in role["reasons"])
    assert "counterbalance" not in roles_of(result, "doc-plain")
    assert result["policy_version"] == POLICY_VERSION


def test_counterbalance_triggered_by_direct_shelf_subjects():
    # Query has no misuse concept; the direct-shelf doc's subjects do.
    query = ["brand_building", "storytelling"]
    direct_with_misuse = make_card(
        "doc-direct-misuse", subjects=["brand_building", MISUSE_KEY]
    )
    counter = make_card("doc-counter", subjects=[COUNTER_KEY])
    result = assign_shelf_roles(query, [direct_with_misuse, counter])
    assert "direct" in roles_of(result, "doc-direct-misuse")
    assert "counterbalance" in roles_of(result, "doc-counter")


def test_counterbalance_skipped_when_not_triggered():
    result = assign_shelf_roles(
        ["ceramics", "gardening"],
        [make_card("doc-a", subjects=["ceramics"]), make_card("doc-b", subjects=[COUNTER_KEY])],
    )
    assert result["shelf_counts"]["counterbalance"] == 0
    assert "policy_not_triggered" in result["skipped_roles"]["counterbalance"]


def test_counterbalance_skipped_with_reason_when_no_candidate_qualifies():
    result = assign_shelf_roles(
        [MISUSE_KEY], [make_card("doc-a", subjects=[MISUSE_KEY, "ceramics"])]
    )
    assert result["shelf_counts"]["counterbalance"] == 0
    reason = result["skipped_roles"]["counterbalance"]
    assert "no candidate" in reason and POLICY_VERSION in reason


# ── multi-role dedupe / determinism / non-inputs ─────────────────────────


def test_multi_role_dedupe_by_doc_retains_all_validated_roles():
    card = make_card(
        "doc-multi",
        subjects=["attention", "persuasion"],
        capabilities=["visual_storytelling"],
        mechanisms=[entry("framing", source_ids=("parent-2",))],
    )
    result = assign_shelf_roles(QUERY, [card, dict(card)])  # duplicate input
    docs = [a["doc_id"] for a in result["assignments"]]
    assert docs == ["doc-multi"]  # deduped by document
    roles = roles_of(result, "doc-multi")
    assert set(roles) == {"direct", "foundational"}  # multiple roles retained
    assert result["shelf_counts"]["direct"] == 1
    assert result["shelf_counts"]["foundational"] == 1


def test_determinism_under_shuffled_input():
    cards = [
        make_card("doc-1", subjects=["attention", "persuasion"]),
        make_card("doc-2", subjects=["attention"], latent=["persuasion"]),
        make_card(
            "doc-3",
            subjects=["ceramics"],
            capabilities=["persuasion"],
            mechanisms=[entry("framing", source_ids=("parent-3",))],
        ),
        make_card(
            "doc-4",
            subjects=["gardening"],
            principles=[entry("visual_storytelling", source_ids=("parent-4",))],
        ),
        make_card("doc-5", corpus_id="corpus-b", subjects=["attention", "pottery"]),
        make_card("doc-6", subjects=["woodworking"]),
    ]
    baseline = assign_shelf_roles(QUERY, list(cards))
    for seed in (1, 7, 42):
        shuffled = list(cards)
        random.Random(seed).shuffle(shuffled)
        assert assign_shelf_roles(QUERY, shuffled) == baseline


def test_embedding_score_fields_never_grant_roles():
    scored_junk = make_card(
        "doc-embed",
        subjects=["woodworking"],
        embedding_score=0.99,
        dense_score=0.97,
        rerank_score=0.95,
        score=1.0,
    )
    overlap = make_card("doc-overlap", subjects=["attention"])
    overlap_scored = make_card("doc-overlap", subjects=["attention"], embedding_score=0.01)
    result = assign_shelf_roles(QUERY, [scored_junk, overlap])
    assert roles_of(result, "doc-embed") == {}  # no field overlap -> no roles
    # Score-like keys change nothing for a qualifying card either.
    role_plain = roles_of(result, "doc-overlap")["direct"]
    role_scored = roles_of(
        assign_shelf_roles(QUERY, [overlap_scored]), "doc-overlap"
    )["direct"]
    assert role_plain == role_scored


def test_unknown_policy_version_is_rejected():
    with pytest.raises(ValueError):
        assign_shelf_roles(QUERY, [], policy_version="shelf_policy.v999")


def test_empty_query_concepts_assigns_nothing():
    result = assign_shelf_roles([], [make_card("doc-a", subjects=["attention"])])
    assert result["assignments"] == []
    assert set(result["skipped_roles"].values()) == {"empty_query_concepts"}
