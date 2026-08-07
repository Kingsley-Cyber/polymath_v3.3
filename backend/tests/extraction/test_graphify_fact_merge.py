from services.extraction.graphify_relations import openie_fact_merge_disposition

KEY = ("m:s", "m:o", "uses")


def test_qualified_vetoes_only_same_evidence() -> None:
    qualified = {KEY: [(100, 150)]}
    assert openie_fact_merge_disposition(KEY, (100, 150), set(), qualified) \
        == "blocked_same_evidence_qualified"
    assert openie_fact_merge_disposition(KEY, (90, 120), set(), qualified) \
        == "blocked_same_evidence_qualified"  # overlap = same evidence context
    # Different sentence: direct fact evidence is never silenced by an
    # attributed restatement elsewhere.
    assert openie_fact_merge_disposition(KEY, (300, 340), set(), qualified) == "promote"


def test_accepted_fact_identity_dedupes_globally() -> None:
    assert openie_fact_merge_disposition(KEY, (300, 340), {KEY}, {}) == "duplicate"


def test_unrelated_key_promotes() -> None:
    assert openie_fact_merge_disposition(
        ("m:a", "m:b", "produces"), (0, 40), {KEY}, {KEY: [(0, 40)]},
    ) == "promote"
