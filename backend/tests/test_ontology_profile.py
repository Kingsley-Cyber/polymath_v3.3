"""Ontology profiles: core always present, modules compose, resolution pure."""

from services.extraction import ontology_profile as op


def test_core_alone_is_the_fallback_vocabulary():
    p = op.resolve_profile([])
    assert p.profile_id == "core"
    for universal in ("Person", "Organization", "Location", "Event", "TimeReference"):
        assert universal in p.entity_labels


def test_domain_module_ADDS_to_core_never_replaces_it():
    # the measured regression: film_production replaced the global vocabulary
    # and the corpus lost Location/Event/TimeReference entirely
    p = op.resolve_profile(["film_production"])
    for universal in ("Person", "Organization", "Location", "Event", "TimeReference"):
        assert universal in p.entity_labels, f"{universal} lost when composing"
    # and the domain's own types are present too
    assert "Shot Type" in p.entity_labels or "Film" in p.entity_labels


def test_core_leads_so_truncation_drops_specialist_labels_first():
    p = op.resolve_profile(["film_production"], max_labels=12)
    assert p.entity_labels[0] == "Person"
    assert len(p.entity_labels) == 12
    assert p.dropped_labels, "truncation must be reported, never silent"


def test_active_vocabulary_stays_small_by_default():
    for module in ("film_production", "it_security", "consumer_psychology"):
        p = op.resolve_profile([module])
        assert len(p.entity_labels) <= op.MAX_ACTIVE_LABELS


def test_signatures_merge_per_predicate_rather_than_overwrite():
    core = op.resolve_profile([])
    film = op.resolve_profile(["film_production"])
    # a core pair survives when a domain also defines `uses`
    assert ("person", "technology") in core.signatures["uses"]
    assert ("person", "technology") in film.signatures["uses"]
    # and the domain's own pair is added
    assert ("film", "shot type") in film.signatures["uses"]


def test_unknown_module_degrades_to_core_without_raising():
    p = op.resolve_profile(["no_such_domain"])
    assert p.modules == ("core",)
    assert "Person" in p.entity_labels


def test_module_name_is_sanitised_against_path_traversal():
    assert op.load_module("../../etc/passwd") == {}
    assert op.load_module("") == {}


def test_resolution_is_deterministic():
    a = op.resolve_profile(["film_production"])
    b = op.resolve_profile(["film_production"])
    assert a.entity_labels == b.entity_labels
    assert a.relation_labels == b.relation_labels
    assert a.signatures == b.signatures


def test_module_order_does_not_change_the_result_set_when_uncapped():
    # Without truncation the composition is a set union: order-independent.
    a = op.resolve_profile(["film_production", "it_security"], max_labels=200)
    b = op.resolve_profile(["it_security", "film_production"], max_labels=200)
    assert set(a.entity_labels) == set(b.entity_labels)
    assert a.signatures == b.signatures


def test_truncation_is_order_dependent_by_design_and_always_reported():
    # Two domains exceed the active-label cap, so SOME specialist label must
    # go. Which one depends on requested order — that is the documented
    # contract, not a bug — but core survives and the drop is never silent.
    a = op.resolve_profile(["film_production", "it_security"])
    b = op.resolve_profile(["it_security", "film_production"])
    assert len(a.entity_labels) == len(b.entity_labels) == op.MAX_ACTIVE_LABELS
    assert a.dropped_labels and b.dropped_labels
    for universal in ("Person", "Organization", "Location", "Event"):
        assert universal in a.entity_labels and universal in b.entity_labels
    # signatures are a union and stay order-independent regardless
    assert a.signatures == b.signatures


def test_content_precedence_is_chunk_then_document_then_corpus():
    p = op.profile_for_document(
        corpus_domain="consumer_psychology",
        document_domain="it_security",
        chunk_domain="film_production",
    )
    assert "film_production" in p.modules
    p2 = op.profile_for_document(
        corpus_domain="consumer_psychology", document_domain="it_security"
    )
    assert "it_security" in p2.modules
    p3 = op.profile_for_document(corpus_domain="consumer_psychology")
    assert "consumer_psychology" in p3.modules


def test_no_domain_anywhere_falls_back_to_core_not_a_guess():
    p = op.profile_for_document()
    assert p.modules == ("core",)
