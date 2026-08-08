from __future__ import annotations

from models.graphify_contracts import (
    CompletedMentionV1,
    DocumentEntityV1,
    EntityTerminalState,
)
from services.extraction.graphify_normalization import normalize_document
from services.extraction.graphify_relations import predicate_compiler, run_relation_fast_path
from services.extraction.graphify_relations import evaluate_relation_eligibility
from services.extraction.graphify_survey import survey_document


def _entity(document_id: str, name: str, entity_type: str) -> DocumentEntityV1:
    return DocumentEntityV1(
        entity_id=f"entity:{name}", document_id=document_id, canonical_name=name,
        entity_type=entity_type, mention_ids=(), state=EntityTerminalState.PROMOTED,
        confidence=1.0, reasons=("test",), reducer_release="test",
    )


def _mention(document_id: str, entity: DocumentEntityV1, text: str, start: int) -> CompletedMentionV1:
    return CompletedMentionV1(
        mention_id=f"mention:{start}:{entity.canonical_name}", entity_id=entity.entity_id,
        document_id=document_id, surface=text[start:start + len(entity.canonical_name)],
        normalized_start=start, normalized_end=start + len(entity.canonical_name),
        original_start=start, original_end=start + len(entity.canonical_name),
        source="exact_name", completion_release="test",
    )


def _run(text: str, specs):
    document = normalize_document("doc", text)
    entities = [_entity("doc", name, entity_type) for name, entity_type in specs]
    mentions = [_mention("doc", entity, text, text.index(entity.canonical_name)) for entity in entities]
    return run_relation_fast_path([document], [survey_document(document)], mentions, entities)


def test_active_and_coordinated_objects_are_grammar_licensed() -> None:
    output = _run(
        "Graphify uses MongoDB and Qdrant.",
        [("Graphify", "software"), ("MongoDB", "software"), ("Qdrant", "software")],
    )
    accepted = [item for item in output.mapped_relations if item.terminal_state.value == "accepted"]
    assert {(item.canonical_candidate, item.surface_predicate) for item in accepted} == {
        ("uses", "uses"),
    }
    assert len(accepted) == 2


def test_passive_direction_is_reversed_once() -> None:
    output = _run(
        "The Projection API is implemented by Graphify.",
        [("Projection API", "software"), ("Graphify", "software")],
    )
    relation = next(item for item in output.mapped_relations if item.terminal_state.value == "accepted")
    by_id = {item.mention_id: item for item in output.surface_relations for item in ()}
    assert relation.canonical_candidate == "implements"
    assert relation.voice == "passive"


def test_passive_derived_from_keeps_artifact_to_source_direction() -> None:
    output = _run(
        "A **Derived Search Index** is derived from the Canonical Event Store, and a **Derived Graph Projection** is also derived from the Canonical Event Store.",
        [
            ("Derived Search Index", "artifact"),
            ("Derived Graph Projection", "artifact"),
            ("Canonical Event Store", "artifact"),
        ],
    )
    accepted = [
        item for item in output.mapped_relations
        if item.canonical_candidate == "derived_from"
        and item.terminal_state.value == "accepted"
    ]
    assert len(accepted) == 2
    endpoint_surface = {item.mention_id: item.surface for item in output.endpoint_mentions}
    assert all(
        "Canonical Event Store" in item.object_mention_id
        or endpoint_surface.get(item.object_mention_id) == "Canonical Event Store"
        for item in accepted
    )
    assert {item.subject_mention_id.rsplit(":", 1)[-1] for item in accepted} == {
        "Derived Search Index", "Derived Graph Projection",
    }
    assert not any(
        "Canonical Event Store" in item.subject_mention_id
        for item in accepted
    )


def test_modal_and_negated_relations_are_qualified() -> None:
    output = _run(
        "Graphify may support PostgreSQL.",
        [("Graphify", "software"), ("PostgreSQL", "software")],
    )
    relation = next(item for item in output.mapped_relations if item.canonical_candidate == "supports")
    assert relation.terminal_state.value == "qualified"


def test_open_only_verb_abstains_from_canonical_mapping() -> None:
    output = _run(
        "Northstar Labs published Atlas Dataset.",
        [("Northstar Labs", "organization"), ("Atlas Dataset", "artifact")],
    )
    relation = next(item for item in output.mapped_relations if item.lemma == "publish")
    assert relation.canonical_candidate is None
    assert relation.terminal_state.value == "open"


def test_predicate_compiler_is_singleton_and_rejects_forced_fallback() -> None:
    first = predicate_compiler()
    second = predicate_compiler()
    assert first is second
    candidate, reason = first.compile(
        surface="unknown", lemma="unknown", canonical_hint="related_to",
        subject_type="software", object_type="software", source="test",
    )
    assert candidate is None
    assert reason == "review:forced_related_to_prohibited"


def test_parse_once_and_decision_conservation() -> None:
    output = _run(
        "Graphify uses MongoDB.",
        [("Graphify", "software"), ("MongoDB", "software")],
    )
    assert output.report["parse_once"] is True
    assert output.report["decision_conservation"] is True
    assert output.report["spacy_parses"] == output.report["eligible_units"]


def test_relation_local_completion_is_limited_to_strict_arguments() -> None:
    output = _run(
        "Graphify supports temporal queries.",
        [("Graphify", "software")],
    )
    assert [(item.surface, item.context_rule) for item in output.endpoint_mentions] == [
        ("temporal queries", "strict_dependency_argument"),
    ]
    assert any(
        item.canonical_candidate == "supports" and item.terminal_state.value == "accepted"
        for item in output.mapped_relations
    )


def test_strict_role_corrects_only_document_local_endpoint_type() -> None:
    text = "Validation Service produces a Validated Assertion Batch."
    document = normalize_document("doc", text)
    service = _entity("doc", "Validation Service", "software")
    batch = _entity("doc", "Validated Assertion Batch", "software").model_copy(update={
        "state": EntityTerminalState.DOCUMENT_LOCAL,
    })
    mentions = [
        _mention("doc", service, text, text.index(service.canonical_name)),
        _mention("doc", batch, text, text.index(batch.canonical_name)),
    ]
    output = run_relation_fast_path(
        [document], [survey_document(document)], mentions, [service, batch],
    )
    corrected = next(item for item in output.endpoint_entities if item.entity_id == batch.entity_id)
    assert corrected.entity_type == "artifact"
    assert "strict_relation_role_type" in corrected.reasons
    assert any(
        item.canonical_candidate == "produces" and item.terminal_state.value == "accepted"
        for item in output.mapped_relations
    )


def test_reduced_passive_uses_copular_subject_and_agent() -> None:
    output = _run(
        "Go is a named programming language used by the Graphify CLI.",
        [("Go", "software")],
    )
    relation = next(
        item for item in output.mapped_relations
        if item.canonical_candidate == "uses" and item.terminal_state.value == "accepted"
    )
    assert relation.voice == "passive"
    assert any(item.surface == "Graphify CLI" for item in output.endpoint_mentions)


def test_attributed_pronoun_resolves_to_controller_and_stays_qualified() -> None:
    output = _run(
        "Northstar Labs denied that it owns the Phantom Dataset.",
        [("Northstar Labs", "organization")],
    )
    relation = next(item for item in output.mapped_relations if item.canonical_candidate == "owns")
    assert relation.terminal_state.value == "qualified"
    assert any(item.surface == "Phantom Dataset" for item in output.endpoint_mentions)


def test_rejected_nominal_statement_cannot_leak_embedded_fact() -> None:
    output = _run(
        "Validator rejected the statement that Atlas owns Harbor because evidence was absent.",
        [("Validator", "method"), ("Atlas", "organization"), ("Harbor", "software")],
    )
    relation = next(item for item in output.mapped_relations if item.canonical_candidate == "owns")
    assert relation.terminal_state.value == "qualified"
    assert relation.polarity == "denied"
    assert relation.attribution == "nominal:statement"


def test_incorrect_nominal_claim_cannot_leak_after_review_language() -> None:
    output = _run(
        "The incorrect claim that Atlas uses Qdrant passed review.",
        [("Atlas", "software"), ("Qdrant", "software")],
    )
    relation = next(item for item in output.mapped_relations if item.canonical_candidate == "uses")
    assert relation.terminal_state.value == "qualified"
    assert relation.polarity == "denied"
    assert relation.attribution == "nominal:claim"


def test_nominal_cue_does_not_contaminate_independent_direct_fact() -> None:
    output = _run(
        "The claim was archived. Atlas uses Qdrant.",
        [("Atlas", "software"), ("Qdrant", "software")],
    )
    relation = next(item for item in output.mapped_relations if item.canonical_candidate == "uses")
    assert relation.terminal_state.value == "accepted"
    assert relation.polarity == "positive"
    assert relation.attribution == ""


def test_negated_speech_governor_cannot_leak_embedded_fact() -> None:
    output = _run(
        "Maya did not say Harbor uses Qdrant.",
        [("Maya", "person"), ("Harbor", "software"), ("Qdrant", "software")],
    )
    relation = next(item for item in output.mapped_relations if item.canonical_candidate == "uses")
    assert relation.terminal_state.value == "qualified"
    assert relation.polarity == "negative"
    assert relation.attribution == "say"


def test_negated_copular_relation_cannot_enter_positive_graph() -> None:
    output = _run(
        "The possibility is not part of the current production architecture.",
        [("possibility", "concept"), ("current production architecture", "concept")],
    )
    relation = next(item for item in output.mapped_relations if item.canonical_candidate == "part_of")
    assert relation.terminal_state.value == "qualified"
    assert relation.polarity == "negative"


def test_conditional_scope_is_local_to_governing_conjunct() -> None:
    output = _run(
        "Harbor uses Qdrant, and if Redis fails, Recovery Service supports Recovery Process.",
        [
            ("Harbor", "software"), ("Qdrant", "software"), ("Redis", "software"),
            ("Recovery Service", "software"), ("Recovery Process", "method"),
        ],
    )
    uses = next(
        item for item in output.mapped_relations
        if item.canonical_candidate == "uses" and "Qdrant" in item.object_mention_id
    )
    supports = next(
        item for item in output.mapped_relations
        if item.canonical_candidate == "supports" and "Recovery Service" in item.subject_mention_id
    )
    assert uses.terminal_state.value == "accepted"
    assert uses.modality == "asserted"
    assert supports.terminal_state.value == "qualified"
    assert supports.modality == "conditional"


def test_matrix_relation_with_if_modifier_is_conditional() -> None:
    output = _run(
        "Harbor uses Qdrant if Redis fails.",
        [("Harbor", "software"), ("Qdrant", "software"), ("Redis", "software")],
    )
    relation = next(item for item in output.mapped_relations if item.canonical_candidate == "uses")
    assert relation.terminal_state.value == "qualified"
    assert relation.modality == "conditional"


def test_wildcard_relation_endpoint_is_never_generated_or_promoted() -> None:
    output = _run(
        "Graphify uses *.",
        [("Graphify", "software")],
    )
    assert not output.endpoint_entities
    assert not output.endpoint_mentions
    assert not output.mapped_relations
    assert output.report["wildcard_non_rejected_endpoints"] == 0


def test_relative_clause_objects_do_not_attach_to_passive_parent_predicate() -> None:
    output = _run(
        "Projection Assertions are consumed by Validation Service, which applies Identity Rules, Predicate Constraints, and Provenance Checks.",
        [
            ("Projection Assertions", "artifact"), ("Validation Service", "software"),
            ("Identity Rules", "concept"), ("Predicate Constraints", "concept"),
            ("Provenance Checks", "concept"),
        ],
    )
    accepted = [
        item for item in output.mapped_relations
        if item.canonical_candidate == "consumes" and item.terminal_state.value == "accepted"
    ]
    assert len(accepted) == 1
    assert "Validation Service" in accepted[0].subject_mention_id
    assert "Projection Assertions" in accepted[0].object_mention_id


def test_applies_maps_to_uses_for_each_dependency_licensed_object() -> None:
    output = _run(
        "**Validation Service**, which applies Identity Rules, Predicate Constraints, and Provenance Checks, produces a Validated Batch.",
        [
            ("Validation Service", "software"), ("Identity Rules", "concept"),
            ("Predicate Constraints", "concept"), ("Provenance Checks", "concept"),
            ("Validated Batch", "artifact"),
        ],
    )
    accepted = [
        item for item in output.mapped_relations
        if item.canonical_candidate == "uses" and item.terminal_state.value == "accepted"
    ]
    assert len(accepted) == 3
    assert {item.object_mention_id.rsplit(":", 1)[-1] for item in accepted} == {
        "Identity Rules", "Predicate Constraints", "Provenance Checks",
    }


def test_markdown_emphasis_is_offset_preserving_but_parse_inert() -> None:
    output = _run(
        "Northstar Labs owns the Canonical Event Dataset, and the **Data Retention Guide** defines the retention policy.",
        [
            ("Northstar Labs", "organization"), ("Canonical Event Dataset", "artifact"),
            ("Data Retention Guide", "document"), ("retention policy", "concept"),
        ],
    )
    accepted = {
        (item.canonical_candidate, item.subject_mention_id, item.object_mention_id)
        for item in output.mapped_relations if item.terminal_state.value == "accepted"
    }
    assert any(
        predicate == "owns" and "Northstar Labs" in subject and "Canonical Event Dataset" in obj
        for predicate, subject, obj in accepted
    )
    assert any(
        predicate == "defines" and "Data Retention Guide" in subject and "retention policy" in obj
        for predicate, subject, obj in accepted
    )
    assert not any(
        predicate == "owns" and "Data Retention Guide" in obj
        for predicate, _subject, obj in accepted
    )
    assert not any(item.surface == "*" for item in output.endpoint_mentions)


def test_pathological_nominal_coordination_expands_without_all_pairs() -> None:
    output = _run(
        "MongoDB, Qdrant, and Redis support the Data Platform and Query Platform.",
        [
            ("MongoDB", "software"), ("Qdrant", "software"), ("Redis", "software"),
            ("Data Platform", "software"), ("Query Platform", "software"),
        ],
    )
    accepted = [
        item for item in output.mapped_relations
        if item.canonical_candidate == "supports" and item.terminal_state.value == "accepted"
    ]
    assert len(accepted) == 6
    assert output.report["pair_tail"]["max"] == 6


def test_generic_relation_argument_is_not_completed() -> None:
    output = _run(
        "Northstar Labs produces information.",
        [("Northstar Labs", "organization")],
    )
    assert not output.endpoint_mentions
    assert not [
        item for item in output.mapped_relations if item.terminal_state.value == "accepted"
    ]


def test_relation_units_are_sentence_scoped_without_blank_lines() -> None:
    text = "Graphify may support SQLite. Graphify uses MongoDB."
    document = normalize_document("doc", text)
    graphify = _entity("doc", "Graphify", "software")
    sqlite = _entity("doc", "SQLite", "software")
    mongodb = _entity("doc", "MongoDB", "software")
    mentions = [
        _mention("doc", graphify, text, text.index("Graphify")),
        _mention("doc", sqlite, text, text.index("SQLite")),
        CompletedMentionV1(
            **{
                **_mention("doc", graphify, text, text.rindex("Graphify")).model_dump(),
                "mention_id": "mention:second:Graphify",
            }
        ),
        _mention("doc", mongodb, text, text.index("MongoDB")),
    ]
    decisions = evaluate_relation_eligibility(
        [document], [survey_document(document)], mentions
    )
    assert [text[item.start:item.end] for item in decisions] == [
        "Graphify may support SQLite.",
        "Graphify uses MongoDB.",
    ]
    output = run_relation_fast_path(
        [document], [survey_document(document)], mentions, [graphify, sqlite, mongodb]
    )
    by_evidence = {item.evidence_text: item for item in output.mapped_relations}
    assert by_evidence["Graphify may support SQLite."].terminal_state.value == "qualified"
    assert by_evidence["Graphify uses MongoDB."].terminal_state.value == "accepted"


def test_unique_nominal_and_pronoun_subjects_resolve_deterministically() -> None:
    text = (
        "Ingestion Worker consumes Event Envelope. The worker produces Normalized Event. "
        "Projection Worker implements Assertion Compilation Process. It depends on Schema Registry. "
        "Projection Assertions are not written directly to graph. They are consumed by Validation Service. "
        "August Recovery Event occurred in El Paso. That event causes Temporary Query Latency."
    )
    output = _run(text, [
        ("Ingestion Worker", "software"), ("Event Envelope", "artifact"),
        ("Normalized Event", "event"), ("Projection Worker", "software"),
        ("Assertion Compilation Process", "method"), ("Schema Registry", "software"),
        ("Projection Assertions", "artifact"), ("Validation Service", "software"),
        ("August Recovery Event", "event"), ("El Paso", "location"),
        ("Temporary Query Latency", "concept"),
    ])
    accepted = {
        (item.canonical_candidate, item.subject_mention_id, item.object_mention_id)
        for item in output.mapped_relations if item.terminal_state.value == "accepted"
    }
    assert any(p == "produces" and "discourse-subject-mention" in s and "Normalized Event" in o for p, s, o in accepted)
    assert any(p == "depends_on" and "discourse-subject-mention" in s and "Schema Registry" in o for p, s, o in accepted)
    assert any(p == "consumes" and "Validation Service" in s and "discourse-subject-mention" in o for p, s, o in accepted)
    assert any(p == "causes" and "discourse-subject-mention" in s and "Temporary Query Latency" in o for p, s, o in accepted)
    assert output.report["discourse_resolved_mentions"] == 4


def test_ambiguous_nominal_antecedent_abstains() -> None:
    output = _run(
        "Ingestion Worker uses Kafka. Projection Worker uses Redis. The worker produces Normalized Event.",
        [
            ("Ingestion Worker", "software"), ("Kafka", "software"),
            ("Projection Worker", "software"), ("Redis", "software"),
            ("Normalized Event", "artifact"),
        ],
    )
    assert output.report["discourse_resolved_mentions"] == 0
    assert not [
        item for item in output.mapped_relations
        if item.canonical_candidate == "produces" and item.terminal_state.value == "accepted"
    ]


def test_bare_article_entity_does_not_block_unique_previous_subject() -> None:
    output = _run(
        "The Projection Worker implements Assertion Compilation Process. It depends on Schema Registry.",
        [
            ("The", "concept"), ("Projection Worker", "software"),
            ("Assertion Compilation Process", "method"), ("Schema Registry", "software"),
        ],
    )
    assert output.report["discourse_resolved_mentions"] == 1
    assert any(
        item.canonical_candidate == "depends_on"
        and item.terminal_state.value == "accepted"
        and "discourse-subject-mention" in item.subject_mention_id
        for item in output.mapped_relations
    )


def _pairs(output, states=("accepted", "open")):
    return {
        (
            item.subject_mention_id.rsplit(":", 1)[-1],
            item.object_mention_id.rsplit(":", 1)[-1],
        )
        for item in output.mapped_relations
        if item.terminal_state.value in states
    }


def test_participial_acl_frame_relates_head_noun_to_its_object() -> None:
    # Active participial modifier: the modified noun is the participle's
    # subject — mirror class of the long-standing VBN passive-acl recovery.
    # "contain" is an inverse-direction synonym: the compiled canonical
    # relation is checksums part_of registry, never the reverse.
    output = _run(
        "The registry containing checksums was archived last year.",
        [("registry", "artifact"), ("checksums", "artifact")],
    )
    assert ("checksums", "registry") in _pairs(output)
    assert not any(
        item.subject_mention_id.endswith(":registry")
        and item.canonical_candidate == "part_of"
        for item in output.mapped_relations
        if item.terminal_state.value in ("accepted", "open")
    )


def test_explicit_object_list_distributes_across_any_frame() -> None:
    # Comma/and NP lists extend a frame's objects for every cue, not only
    # "apply" — licensing is adjacency, robust to parser-fractured conj arcs.
    output = _run(
        "The vault holds keys, certificates, and revocation lists.",
        [
            ("vault", "artifact"), ("keys", "artifact"),
            ("certificates", "artifact"), ("revocation lists", "artifact"),
        ],
    )
    pairs = _pairs(output)
    assert {("vault", "keys"), ("vault", "certificates"), ("vault", "revocation lists")} <= pairs


def test_participial_frame_with_fracture_prone_list_reaches_every_member() -> None:
    # Same structural shape that fractures the small parser on long
    # heterogeneous lists (a second acl island, a compound misread as a
    # verb): every list member must still pair with the participle's head.
    output = _run(
        "The kit ships a sensor array containing heat probes, dust filters, flow meters, and manually calibrated pressure gauges.",
        [
            ("sensor array", "artifact"), ("heat probes", "artifact"),
            ("dust filters", "artifact"), ("flow meters", "artifact"),
            ("pressure gauges", "artifact"),
        ],
    )
    pairs = _pairs(output)
    assert {
        ("heat probes", "sensor array"), ("dust filters", "sensor array"),
        ("flow meters", "sensor array"), ("pressure gauges", "sensor array"),
    } <= pairs


def test_participial_container_list_never_inverts_or_cross_pairs() -> None:
    # The class that minted false book facts: a container participle over a
    # determiner-separated list whose conj arcs the parser attaches to the
    # container noun itself. The container must never become a part_of
    # subject, and list members must never pair with each other.
    output = _run(
        "After authentication, the Harbor Gateway produces an Event Envelope containing the device identifier, an event timestamp, a schema identifier, and the payload bytes.",
        [
            ("Harbor Gateway", "software"), ("Event Envelope", "artifact"),
            ("device identifier", "artifact"), ("schema identifier", "artifact"),
            ("payload bytes", "artifact"),
        ],
    )
    kept = [
        item for item in output.mapped_relations
        if item.terminal_state.value in ("accepted", "open")
        and item.canonical_candidate == "part_of"
    ]
    assert not any(item.subject_mention_id.endswith(":Event Envelope") for item in kept)
    member_pairs = {
        (item.subject_mention_id.rsplit(":", 1)[-1], item.object_mention_id.rsplit(":", 1)[-1])
        for item in output.mapped_relations
        if item.terminal_state.value in ("accepted", "open")
    }
    assert ("schema identifier", "device identifier") not in member_pairs
    assert ("payload bytes", "device identifier") not in member_pairs


def test_inverse_synonym_compiles_part_of_with_endpoint_swap() -> None:
    candidate, rule = predicate_compiler().compile(
        surface="contains", lemma="contain", canonical_hint=None,
        subject_type="artifact", object_type="artifact", source="test",
    )
    assert candidate == "part_of"
    assert ":inverse_direction" in rule


def test_object_list_never_crosses_a_clause_subject() -> None:
    # Comma splice: the second clause's subject terminates the list walk —
    # no derived pair may reach across the clause boundary.
    output = _run(
        "The scheduler notified Alpha, Beta follows Gamma.",
        [
            ("scheduler", "software"), ("Alpha", "software"),
            ("Beta", "software"), ("Gamma", "software"),
        ],
    )
    assert ("scheduler", "Beta") not in _pairs(output, states=("accepted", "open", "review"))
    assert ("scheduler", "Gamma") not in _pairs(output, states=("accepted", "open", "review"))


def test_object_list_never_absorbs_a_following_verbs_arguments() -> None:
    # The walk stops at any verb: objects of a later predicate are never
    # folded into the earlier frame's list.
    output = _run(
        "The gateway sends requests, logs failures, and updates metrics.",
        [
            ("gateway", "software"), ("requests", "artifact"),
            ("failures", "event"), ("metrics", "artifact"),
        ],
    )
    routed = {
        (item.subject_mention_id.rsplit(":", 1)[-1], item.object_mention_id.rsplit(":", 1)[-1])
        for item in output.mapped_relations
        if item.surface_predicate.startswith("send")
    }
    assert ("gateway", "failures") not in routed
    assert ("gateway", "metrics") not in routed


def test_relex_semantic_accepts_only_same_canonical_corroboration(monkeypatch) -> None:
    # The semantic lane strengthens structurally-accepted triples; it never
    # introduces a new canonical edge (round-2 burned measurement: pair-level
    # corroboration let it name OPEN pairs and promote battery negatives).
    from services.extraction import relex_sidecar_client

    def fake_infer(texts, **kwargs):
        results = []
        for text in texts:
            rels = []
            if "Envoy" in text:
                a, b = text.find("Harbor Gateway"), text.find("Envoy")
                rels = [
                    relex_sidecar_client.RelexRelation(
                        head_start=a, head_end=a + 14, head_text="Harbor Gateway",
                        tail_start=b, tail_end=b + 5, tail_text="Envoy",
                        label="uses", score=0.97,
                    ),
                    relex_sidecar_client.RelexRelation(
                        head_start=a, head_end=a + 14, head_text="Harbor Gateway",
                        tail_start=b, tail_end=b + 5, tail_text="Envoy",
                        label="owns", score=0.96,
                    ),
                ]
            results.append(relex_sidecar_client.RelexResult(entities=(), relations=tuple(rels)))
        return results

    monkeypatch.setattr(relex_sidecar_client, "infer", fake_infer)
    monkeypatch.setenv("GRAPHIFY_RELEX_RELATIONS", "1")
    output = _run(
        "The Harbor Gateway uses Envoy.",
        [("Harbor Gateway", "software"), ("Envoy", "software")],
    )
    relex_rows = [r for r in output.mapped_relations if r.dependency_frame == "relex:semantic"]
    by_candidate = {r.canonical_candidate: r.terminal_state.value for r in relex_rows}
    assert by_candidate.get("uses") == "accepted"          # same-canonical duplicate
    assert by_candidate.get("owns") in (None, "review")     # novel canonical: never accepted


def test_relex_qualified_rows_never_enter_qualifier_veto(monkeypatch) -> None:
    # A demoted semantic candidate is a withheld proposal, not qualifier
    # evidence — it must not block same-evidence OpenIE FACTs (round-2
    # burned sealed losses T010/T040).
    from services.extraction.graphify_relations import openie_fact_merge_disposition
    key = ("m1", "m2", "depends_on")
    disposition = openie_fact_merge_disposition(key, (0, 50), set(), {})
    assert disposition == "promote"
    blocked = openie_fact_merge_disposition(key, (0, 50), set(), {key: [(0, 50)]})
    assert blocked == "blocked_same_evidence_qualified"
