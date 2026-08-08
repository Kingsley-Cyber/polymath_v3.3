from __future__ import annotations

from models.graphify_contracts import MentionTerminalState, RawMentionV1
from services.extraction.graphify_normalization import normalize_document
from services.extraction.graphify_reducer import reduce_document_entities
from services.extraction.graphify_survey import survey_document


def mention(document_id: str, surface: str, start: int, entity_type: str = "software", confidence: float = 0.95) -> RawMentionV1:
    return RawMentionV1(
        mention_id=f"mention:{document_id}:{start}:{surface}", document_id=document_id,
        window_id="window:1", sequence=start, surface=surface, entity_type=entity_type,
        confidence=confidence, local_start=start, local_end=start + len(surface),
        normalized_start=start, normalized_end=start + len(surface),
        original_start=start, original_end=start + len(surface),
        terminal_state=MentionTerminalState.ALIGNED, provider_release="test-provider",
    )


def test_reducer_assigns_every_aligned_mention_once_and_repeats() -> None:
    text = "Polymath uses MongoDB. Polymath depends on Qdrant."
    document = normalize_document("doc", text)
    mentions = [
        mention("doc", "Polymath", 0), mention("doc", "MongoDB", 14),
        mention("doc", "Polymath", 23), mention("doc", "Qdrant", 43),
    ]
    survey = survey_document(document)
    first = reduce_document_entities(document, mentions, survey)
    second = reduce_document_entities(document, mentions, survey)
    assert first == second
    assert first.report["conservation"] is True
    assert len(first.assignments) == len(mentions)
    assert first.report["identity_digest"] == second.report["identity_digest"]


def test_ambiguous_surface_cases_do_not_cross_sense_merge() -> None:
    text = "Go is a named programming language. Operators go to El Paso."
    document = normalize_document("doc", text)
    mentions = [mention("doc", "Go", 0), mention("doc", "go", 46)]
    output = reduce_document_entities(document, mentions, survey_document(document))
    by_name = {entity.canonical_name: entity for entity in output.entities}
    assert by_name["Go"].state.value == "promoted"
    assert by_name["go"].state.value == "suppressed"
    assert by_name["Go"].entity_id != by_name["go"].entity_id


def test_strong_singleton_is_allowed() -> None:
    document = normalize_document("doc", "MongoDB stores evidence.")
    output = reduce_document_entities(
        document, [mention("doc", "MongoDB", 0)], survey_document(document),
    )
    entity = next(entity for entity in output.entities if entity.canonical_name == "MongoDB")
    assert entity.state.value == "promoted"
    assert output.report["strong_singletons"] == 1


def test_generic_pronoun_and_noun_are_suppressed() -> None:
    text = "It uses MongoDB. This process failed."
    document = normalize_document("doc", text)
    mentions = [mention("doc", "It", 0, "person"), mention("doc", "process", 22, "method")]
    output = reduce_document_entities(document, mentions, survey_document(document))
    assert {entity.state.value for entity in output.entities} == {"suppressed"}


def test_reducer_has_no_hard_entity_cap() -> None:
    names = [f"Tool{index}" for index in range(150)]
    text = ". ".join(names) + "."
    document = normalize_document("doc", text)
    mentions = [mention("doc", name, text.index(name)) for name in names]
    output = reduce_document_entities(document, mentions, survey_document(document))
    assert len(output.entities) >= 150
    assert output.report["hard_entity_cap"] is None


def test_type_conflict_is_adjudicated_with_a_reason() -> None:
    text = "The Atlas Dataset is durable."
    document = normalize_document("doc", text)
    start = text.index("Atlas Dataset")
    mentions = [
        mention("doc", "Atlas Dataset", start, "artifact", 0.8),
        RawMentionV1(**{
            **mention("doc", "Atlas Dataset", start, "document", 0.9).model_dump(),
            "mention_id": "mention:conflict",
        }),
    ]
    output = reduce_document_entities(document, mentions, survey_document(document))
    entity = next(entity for entity in output.entities if entity.canonical_name == "Atlas Dataset")
    assert entity.entity_type == "artifact"
    assert "type_conflict_adjudicated" in entity.reasons


def test_heading_entity_with_body_evidence_is_not_forced_to_review() -> None:
    text = "# Incident Amber\n\nIncident Amber interrupted the deployment."
    document = normalize_document("doc", text)
    heading_start = text.index("Incident Amber")
    body_start = text.rindex("Incident Amber")
    output = reduce_document_entities(
        document,
        [
            mention("doc", "Incident Amber", heading_start, "event", 0.94),
            mention("doc", "Incident Amber", body_start, "event", 0.94),
        ],
        survey_document(document),
    )
    entity = next(entity for entity in output.entities if entity.canonical_name == "Incident Amber")
    assert entity.state.value == "promoted"
    assert "structural_heading_review_only" not in entity.reasons


def test_heading_only_entity_remains_review_only() -> None:
    text = "# Incident Amber\n\nThe deployment continued."
    document = normalize_document("doc", text)
    start = text.index("Incident Amber")
    output = reduce_document_entities(
        document,
        [mention("doc", "Incident Amber", start, "event", 0.94)],
        survey_document(document),
    )
    entity = next(entity for entity in output.entities if entity.canonical_name == "Incident Amber")
    assert entity.state.value == "review"
    assert entity.reasons == ("structural_heading_review_only",)


def test_repeated_body_entity_is_unchanged_by_heading_evidence_rule() -> None:
    text = "Incident Amber interrupted deployment. Incident Amber delayed recovery."
    document = normalize_document("doc", text)
    output = reduce_document_entities(
        document,
        [
            mention("doc", "Incident Amber", text.index("Incident Amber"), "event", 0.94),
            mention("doc", "Incident Amber", text.rindex("Incident Amber"), "event", 0.94),
        ],
        survey_document(document),
    )
    entity = next(entity for entity in output.entities if entity.canonical_name == "Incident Amber")
    assert entity.state.value == "promoted"
    assert "structural_heading_review_only" not in entity.reasons


def test_counted_noun_phrase_never_promotes() -> None:
    # Universal entity-quality class: cardinal determiner over a lowercase
    # common head names a quantity, not an identity — any domain.
    from services.extraction.graphify_reducer import _counted_noun_phrase
    assert _counted_noun_phrase("two dashboards")
    assert _counted_noun_phrase("three sensors")
    assert _counted_noun_phrase("10 workers")
    assert _counted_noun_phrase("several batches")
    # Names keep their identity: capitalized continuations and fused digits.
    assert not _counted_noun_phrase("Three Mile Island")
    assert not _counted_noun_phrase("5G networks")  # fused digit token is a name
    assert not _counted_noun_phrase("One Identity Manager")
    assert not _counted_noun_phrase("dashboards")


def test_universal_entity_guards_suppress_junk_surfaces() -> None:
    # Decision-1 guards (owner: "wire the entity guards", 2026-08-08):
    # pronouns, bare function words, discourse markers, timestamps — junk
    # in ANY domain. Real names must be untouched.
    from services.extraction.graphify_reducer import _junk_entity_surface
    for junk in ("I", "You", "And", "But", "Because", "If", "Like", "Look",
                 "And I", "Additionally", "However", "So", "5:45", "12:03"):
        assert _junk_entity_surface(junk), junk
    for real in ("Harbor Gateway", "Claude", "AutoDS", "Adobe Express",
                 "If-Then Systems", "Go", "Ac Hampton", "5G networks"):
        assert not _junk_entity_surface(real), real


def test_metadata_key_surfaces_never_promote() -> None:
    # Structural guard: a surface whose every mention sits in key-position
    # ("Channel: ...", "Duration: ...") is a metadata KEY, not an entity.
    # Entities that merely appear once before a colon but also in prose
    # keep their identity.
    from services.extraction.graphify_reducer import reduce_document_entities
    from services.extraction.graphify_normalization import normalize_document
    from services.extraction.graphify_survey import survey_document
    from services.extraction.graphify_census import InMemoryRawMentionSink, run_entity_census

    text = (
        "The upload metadata reads Channel: Ac Hampton and Duration: 25:30 "
        "for this video. AutoDS: the tool I use daily. AutoDS automates "
        "fulfillment and AutoDS integrates with Shopify.\n"
    )
    document = normalize_document("doc", text)
    survey = survey_document(document)

    class _Provider:
        release = "test-null"
        def predict_entities(self, texts, **kwargs):
            rows = []
            for t in texts:
                row = []
                for name in ("Channel", "Duration", "AutoDS", "Shopify", "Ac Hampton"):
                    i = t.find(name)
                    while i >= 0:
                        from services.extraction.gliner2_cpu_provider import EntityPrediction
                        row.append(EntityPrediction(text=name, entity_type="software",
                                                    start=i, end=i + len(name),
                                                    confidence=0.9, facet=""))
                        i = t.find(name, i + 1)
                rows.append(sorted(row, key=lambda p: (p.start, p.end)))
            return rows

    census = run_entity_census([document], [survey], _Provider(), InMemoryRawMentionSink())
    output = reduce_document_entities(document, census.mentions, survey)
    states = {e.canonical_name: e.state.value for e in output.entities}
    assert states.get("Channel") == "suppressed"
    assert states.get("Duration") == "suppressed"
    assert states.get("AutoDS") in ("promoted", "document_local")
    assert states.get("Shopify") in ("promoted", "document_local")
