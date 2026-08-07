from services.extraction.graphify_assertion_semantics import reported_attribution_source


def test_fronted_according_to_is_reported() -> None:
    assert reported_attribution_source(
        "According to a dockside joke, Norvale Group owns the south quay."
    ) == "a dockside joke"


def test_trailing_comma_detached_according_to_is_reported() -> None:
    assert reported_attribution_source(
        "Norvale Group owns the south quay, according to a dockside joke."
    ) == "a dockside joke"


def test_midclause_manner_according_to_never_fires() -> None:
    # Compliance/manner adjunct — not reported speech (counterexample gate).
    assert reported_attribution_source(
        "The pump operates according to the maintenance plan."
    ) is None


def test_plain_direct_sentence_never_fires() -> None:
    assert reported_attribution_source("Harbor uses MongoDB.") is None
