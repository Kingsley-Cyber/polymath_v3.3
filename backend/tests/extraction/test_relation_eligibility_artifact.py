from services.extraction.graphify_normalization import normalize_document
from services.extraction.graphify_relations import evaluate_relation_eligibility
from services.extraction.graphify_survey import survey_document


def test_eligibility_persists_both_terminal_decisions() -> None:
    document = normalize_document(
        "doc",
        "Graphify uses MongoDB.\n\nA paragraph without a relation cue.",
    )
    decisions = evaluate_relation_eligibility([document], [survey_document(document)], [])
    assert len(decisions) == 2
    assert {item.eligible for item in decisions} == {False}
    assert all(item.reasons for item in decisions)
