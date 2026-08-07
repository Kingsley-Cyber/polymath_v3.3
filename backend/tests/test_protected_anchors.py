"""Protected anchors are not the final evidence count."""

from dataclasses import dataclass

from services.retriever.protected_anchors import (
    dynamic_final_child_target,
    select_protected_anchors,
)


@dataclass
class C:
    chunk_id: str
    doc_id: str = "d1"
    parent_id: str = "p1"
    score: float = 1.0
    source_tier: str = "child"


def test_protects_at_most_four_strongest():
    ranked = [
        C(f"c{i}", doc_id=f"d{i}", parent_id=f"p{i}", score=1.0 - i * 0.05)
        for i in range(10)
    ]
    result = select_protected_anchors(ranked, maximum_protected=4)
    assert result.diagnostics["protected_is_not_final_total"] is True
    assert len(result.protected) == 4
    assert len(result.remainder) == 6
    assert [c.chunk_id for c in result.protected] == ["c0", "c1", "c2", "c3"]


def test_max_one_protected_per_parent_and_two_per_document():
    ranked = [
        C("a1", doc_id="D", parent_id="P1", score=1.0),
        C("a2", doc_id="D", parent_id="P1", score=0.9),  # same parent → skip
        C("a3", doc_id="D", parent_id="P2", score=0.8),
        C("a4", doc_id="D", parent_id="P3", score=0.7),  # 3rd from D → skip (max 2)
        C("b1", doc_id="E", parent_id="P4", score=0.6),
    ]
    result = select_protected_anchors(ranked, maximum_protected=4)
    ids = [c.chunk_id for c in result.protected]
    assert ids == ["a1", "a3", "b1"]


def test_dynamic_cross_domain_target_does_not_pad():
    assert (
        dynamic_final_child_target(
            query_class="cross_domain", available_qualified=7
        )
        == 7
    )
    n = dynamic_final_child_target(
        query_class="cross_domain", available_qualified=18
    )
    assert 10 <= n <= 18
    assert n == 12  # preferred cross-domain target when quality allows
