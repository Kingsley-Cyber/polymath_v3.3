"""
Sanity checks for the baked universal schema (GHOST B).

The schema is a contract: Neo4j entity types and RELATES_TO predicates across
every corpus are derived from these two lists. Accidentally renaming or
reordering them breaks cross-corpus queries and (when either vocabulary list
crosses SCHEMA_INLINE_LIMIT) flips ghost_b into degraded retrieval mode.
"""

from config import get_settings
from models.schemas import IngestionConfig, IngestJobResponse, WriteState
from services.ghost_b import (
    EntityItem,
    ExtractionTask,
    RelationItem,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
    SchemaContext,
    _SYSTEM,
    _apply_schema,
    _parse,
    build_user_prompt,
    normalize_relation_predicate_alias,
)
from services.graph.neo4j_writer import (
    ONTOLOGY_VERSION,
    canonicalize_entity_name,
    entity_id_from_name,
    resolve_canonical_family,
    resolve_domain_type,
    resolve_facets,
    resolve_ontology_metadata,
    resolve_primary_entity_type,
    refine_related_to_predicate,
    relation_family_for_predicate,
)


def test_entity_schema_shape():
    assert len(UNIVERSAL_ENTITY_SCHEMA) == 12
    assert all(isinstance(t, str) and t.strip() for t in UNIVERSAL_ENTITY_SCHEMA)
    assert len(set(UNIVERSAL_ENTITY_SCHEMA)) == 12, "entity schema has duplicates"
    for required in ("Person", "Organization", "Rule", "Law"):
        assert required in UNIVERSAL_ENTITY_SCHEMA


def test_relation_schema_shape():
    assert len(UNIVERSAL_RELATION_SCHEMA) == 45
    assert all(isinstance(p, str) and p.strip() for p in UNIVERSAL_RELATION_SCHEMA)
    assert len(set(UNIVERSAL_RELATION_SCHEMA)) == 45, "relation schema has duplicates"
    assert UNIVERSAL_RELATION_SCHEMA[-1] == "related_to", (
        "related_to sentinel MUST be last"
    )
    for required in ("excepts", "overrides", "runs_on", "trained_on", "extracts"):
        assert required in UNIVERSAL_RELATION_SCHEMA
    for required in (
        "measures", "defined_in", "follows_distribution", "tests",
        "applied_to", "illustrated_in", "parameter_of", "equivalent_to",
        "embodies", "symbolizes", "motivates", "struggles_with",
        "reinforces", "undermines", "frames_as", "conceals", "leverages",
    ):
        assert required in UNIVERSAL_RELATION_SCHEMA


def test_each_vocab_stays_inline():
    # ghost_b decides inline-vs-retrieved separately for entity and relation
    # vocabularies. Keep both below SCHEMA_INLINE_LIMIT so fresh ingest never
    # needs schema-term vector retrieval before chunk embeddings exist.
    limit = get_settings().SCHEMA_INLINE_LIMIT
    assert len(UNIVERSAL_ENTITY_SCHEMA) + 1 <= limit  # + 'other' sentinel
    assert len(UNIVERSAL_RELATION_SCHEMA) <= limit


def test_default_ingestion_config_uses_universal():
    cfg = IngestionConfig()
    assert cfg.entity_schema == UNIVERSAL_ENTITY_SCHEMA
    assert cfg.relation_schema == UNIVERSAL_RELATION_SCHEMA
    assert cfg.schema_strict == "soft"


def test_default_ingestion_config_lists_are_copies():
    # Guard against accidentally sharing the module-level list — mutating
    # a corpus-level schema must not mutate every other corpus's config.
    cfg1 = IngestionConfig()
    cfg2 = IngestionConfig()
    assert cfg1.entity_schema is not cfg2.entity_schema
    assert cfg1.relation_schema is not cfg2.relation_schema


def test_ingest_job_response_accepts_current_write_state():
    response = IngestJobResponse(
        job_id="job",
        doc_id="doc",
        corpus_id="corpus",
        filename="doc.md",
        status="processing",
        write_state=WriteState(warnings=["running"]),
    )

    assert response.write_state.warnings == ["running"]


def test_prompt_renders_universal_vocab():
    cfg = IngestionConfig()
    ctx = SchemaContext(
        entity_schema=cfg.entity_schema,
        relation_schema=cfg.relation_schema,
        strict=cfg.schema_strict,
    )
    prompt = build_user_prompt(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        text="sample",
        schema=ctx,
    )
    # Exact phrasing comes from ghost_b.build_user_prompt
    assert (
        "entity_type MUST be one of: Person (named human individual) | "
        "Organization (named formal group"
    ) in prompt
    assert (
        "predicate MUST be one of: part_of (X is a structural subcomponent of Y) | "
        "member_of (X is in group Y)"
    ) in prompt
    # Sentinels surface as explicit fallbacks
    assert "'other'" in prompt
    assert "'related_to'" in prompt
    assert "For product specs / PRDs" in prompt
    assert "evidence_phrase" in prompt
    assert "runs_on" in prompt
    assert "trained_on" in prompt
    assert "relation object_kind must only be 'entity' or 'literal'" in prompt
    assert "do NOT output ontology facet fields such as domain_type" in prompt


def test_ghost_b_system_prompt_primes_json_contract():
    assert "strict JSON contract mode" in _SYSTEM
    assert "json.loads" in _SYSTEM
    assert "Prefer fewer high-confidence items" in _SYSTEM


def test_candidate_fact_parsing_projects_legacy_relation():
    raw = """
    {
      "schema_version": "polymath.extract.v1",
      "chunk_id": "c1",
      "doc_id": "d1",
      "corpus_id": "corp1",
      "entities": [
        {"canonical_name": "sqlite", "surface_form": "SQLite", "entity_type": "Product", "confidence": 0.9},
        {"canonical_name": "events", "surface_form": "events", "entity_type": "Artifact", "confidence": 0.9}
      ],
      "candidate_facts": [
        {
          "atomic_fact": "SQLite stores events.",
          "candidate_subject": "sqlite",
          "candidate_predicate": "stores",
          "candidate_object": "events",
          "object_kind": "entity",
          "predicate_confidence": 0.92,
          "extraction_confidence": 0.94,
          "alternative_predicates_considered": ["produces"],
          "rejection_reasoning": "The text says stored, not created.",
          "evidence_phrase": "events are stored in SQLite"
        }
      ],
      "relations": []
    }
    """

    parsed = _parse(
        raw,
        ExtractionTask("c1", "d1", "corp1", "events are stored in SQLite"),
        threshold=0.5,
        schema=SchemaContext(
            entity_schema=UNIVERSAL_ENTITY_SCHEMA,
            relation_schema=UNIVERSAL_RELATION_SCHEMA,
            strict="soft",
        ),
    )

    assert parsed is not None
    assert parsed.candidate_facts[0].atomic_fact == "SQLite stores events."
    assert parsed.relations[0].predicate == "stores"
    assert parsed.relations[0].atomic_fact == "SQLite stores events."
    assert parsed.relations[0].candidate_predicate == "stores"
    assert parsed.relations[0].evidence_phrase == "events are stored in SQLite"


def test_candidate_fact_parser_normalizes_case_confidence_and_object_kind():
    raw = """
    {
      "schema_version": "polymath.extract.v1",
      "chunk_id": "c1",
      "doc_id": "d1",
      "corpus_id": "corp1",
      "entities": [
        {"canonical_name": "app", "surface_form": "app", "entity_type": "product", "confidence": "95%"},
        {"canonical_name": "ml kit", "surface_form": "ML Kit", "entity_type": "product", "confidence": 0.9}
      ],
      "candidate_facts": [
        {
          "atomic_fact": "The app uses ML Kit.",
          "candidate_subject": "app",
          "candidate_predicate": "Uses",
          "candidate_object": "ml kit",
          "object_kind": "Model",
          "predicate_confidence": "92%",
          "extraction_confidence": 95,
          "evidence_phrase": "app uses ML Kit"
        }
      ],
      "relations": []
    }
    """

    parsed = _parse(
        raw,
        ExtractionTask("c1", "d1", "corp1", "app uses ML Kit"),
        threshold=0.5,
        schema=SchemaContext(
            entity_schema=UNIVERSAL_ENTITY_SCHEMA,
            relation_schema=UNIVERSAL_RELATION_SCHEMA,
            strict="soft",
        ),
    )

    assert parsed is not None
    assert [entity.entity_type for entity in parsed.entities] == ["Product", "Product"]
    assert parsed.relations[0].predicate == "uses"
    assert parsed.relations[0].object_kind == "entity"
    assert parsed.relations[0].predicate_confidence == 0.92
    assert parsed.relations[0].extraction_confidence == 0.95


def test_legacy_relation_parsing_still_works_without_candidate_facts():
    raw = """
    {
      "schema_version": "polymath.extract.v1",
      "chunk_id": "c1",
      "doc_id": "d1",
      "corpus_id": "corp1",
      "entities": [
        {"canonical_name": "sam", "surface_form": "Sam", "entity_type": "Person", "confidence": 0.9},
        {"canonical_name": "openai", "surface_form": "OpenAI", "entity_type": "Organization", "confidence": 0.9}
      ],
      "relations": [
        {
          "subject": "sam",
          "predicate": "works_for",
          "object": "openai",
          "object_kind": "entity",
          "confidence": 0.9,
          "evidence_phrase": "Sam works for OpenAI"
        }
      ]
    }
    """

    parsed = _parse(
        raw,
        ExtractionTask("c1", "d1", "corp1", "Sam works for OpenAI"),
        threshold=0.5,
        schema=SchemaContext(
            entity_schema=UNIVERSAL_ENTITY_SCHEMA,
            relation_schema=UNIVERSAL_RELATION_SCHEMA,
            strict="soft",
        ),
    )

    assert parsed is not None
    assert parsed.relations[0].predicate == "works_for"
    assert parsed.candidate_facts[0].candidate_subject == "sam"
    assert parsed.candidate_facts[0].atomic_fact == "sam works_for openai"


def test_schema_strict_legacy_values_deserialize():
    # Pre-migration Mongo docs may carry schema_strict="off" or "hard".
    # The Literal is intentionally left wide (soft|off|hard) so those records
    # still deserialize; the lifespan migration rewrites them to "soft".
    for legacy in ("soft", "off", "hard"):
        cfg = IngestionConfig.model_validate({"schema_strict": legacy})
        assert cfg.schema_strict == legacy


def test_domain_range_remaps_invalid_relation_softly():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("campaign", "campaign", "Event", 0.9),
        EntityItem("sam", "Sam", "Person", 0.9),
    ]
    relations = [RelationItem("campaign", "depends_on", "sam", "entity", 0.9)]

    _, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert out_relations[0].predicate == "related_to"
    assert out_relations[0].source_predicate == "depends_on"
    assert out_relations[0].validation_status == "domain_range_mismatch"
    assert counters["domain_range_remap_count"] == 1


def test_endpoint_completion_adds_missing_relation_entities():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [EntityItem("unsloth", "Unsloth", "Product", 0.9)]
    relations = [
        RelationItem(
            "model",
            "uses",
            "unsloth",
            "entity",
            0.9,
            evidence_phrase="Fine-tune on RTX 3090 using Unsloth.",
        )
    ]

    out_entities, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert any(e.canonical_name == "model" for e in out_entities)
    assert out_relations[0].predicate == "uses"
    assert counters["endpoint_completion_count"] == 1
    assert counters["domain_range_remap_count"] == 0


def test_domain_range_warning_preserves_evidence_backed_predicate():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("ghost memory architecture", "Ghost Memory Architecture", "Concept", 0.9),
        EntityItem("context injection", "Context Injection", "Method", 0.9),
    ]
    relations = [
        RelationItem(
            "ghost memory architecture",
            "uses",
            "context injection",
            "entity",
            0.9,
            evidence_phrase="Context injection -- not RAG search.",
        )
    ]

    _, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert out_relations[0].predicate == "uses"
    assert out_relations[0].source_predicate == "uses"
    assert "domain_range_warn" in (out_relations[0].validation_status or "")
    assert counters["domain_range_warn_count"] == 1
    assert counters["domain_range_remap_count"] == 0


def test_evidence_cue_repair_flips_stored_in_language():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("events", "events", "Artifact", 0.8),
        EntityItem("sqlite", "SQLite", "Product", 0.9),
    ]
    relations = [
        RelationItem(
            "events",
            "stores",
            "sqlite",
            "entity",
            0.9,
            evidence_phrase="events are stored in SQLite",
        )
    ]

    _, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert out_relations[0].subject == "sqlite"
    assert out_relations[0].predicate == "stores"
    assert out_relations[0].object == "events"
    assert "evidence_cue_repair" in (out_relations[0].validation_status or "")
    assert counters["evidence_cue_repair_count"] == 1


def test_evidence_cue_repair_evaluates_condition_to_tests():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("diagnostic", "diagnostic", "Method", 0.9),
        EntityItem("local independence condition", "local independence condition", "Concept", 0.9),
    ]
    relations = [
        RelationItem(
            "diagnostic",
            "related_to",
            "local independence condition",
            "entity",
            0.9,
            evidence_phrase="the diagnostic evaluates the local independence condition",
        )
    ]

    _, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert out_relations[0].predicate == "tests"
    assert counters["evidence_cue_repair_count"] == 1


def test_evidence_cue_repair_checks_assumption_to_tests():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("diagnostic", "diagnostic", "Method", 0.9),
        EntityItem("measurement assumption", "measurement assumption", "Concept", 0.9),
    ]
    relations = [
        RelationItem(
            "diagnostic",
            "related_to",
            "measurement assumption",
            "entity",
            0.9,
            evidence_phrase="the diagnostic checks the measurement assumption",
        )
    ]

    _, out_relations, _ = _apply_schema(entities, relations, ctx)

    assert out_relations[0].predicate == "tests"


def test_evidence_cue_repair_evaluates_score_value_trait_to_measures():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("irt model", "IRT model", "Method", 0.9),
        EntityItem("latent trait score", "latent trait score", "Concept", 0.9),
    ]
    for phrase in (
        "the IRT model evaluates the latent trait score",
        "the IRT model scores the latent trait value",
        "the IRT model estimates the latent trait quantity",
    ):
        relations = [
            RelationItem(
                "irt model",
                "related_to",
                "latent trait score",
                "entity",
                0.9,
                evidence_phrase=phrase,
            )
        ]

        _, out_relations, counters = _apply_schema(entities, relations, ctx)

        assert out_relations[0].predicate == "measures"
        assert counters["evidence_cue_repair_count"] == 1


def test_evidence_cue_repair_does_not_promote_bare_evaluates():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("method", "method", "Method", 0.9),
        EntityItem("material", "material", "Concept", 0.9),
    ]
    relations = [
        RelationItem(
            "method",
            "related_to",
            "material",
            "entity",
            0.9,
            evidence_phrase="the method evaluates the material",
        )
    ]

    _, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert out_relations[0].predicate == "related_to"
    assert counters["evidence_cue_repair_count"] == 0


def test_evidence_cue_repair_defined_by_person_stays_related_to():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("measurement model", "measurement model", "Concept", 0.9),
        EntityItem("van der linden", "van der Linden", "Person", 0.9),
    ]
    relations = [
        RelationItem(
            "measurement model",
            "defined_by",
            "van der linden",
            "entity",
            0.9,
            evidence_phrase="the measurement model is defined by van der Linden",
        )
    ]

    _, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert out_relations[0].predicate == "related_to"
    assert out_relations[0].source_predicate == "defined_by"
    assert counters["relation_remap_count"] == 1
    assert counters["evidence_cue_repair_count"] == 0


def test_evidence_cue_repair_defined_in_equation_to_defined_in():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("t matrix", "T matrix", "Concept", 0.9),
        EntityItem("equation 4 6", "Equation 4.6", "Document", 0.9),
    ]
    relations = [
        RelationItem(
            "t matrix",
            "related_to",
            "equation 4 6",
            "entity",
            0.9,
            evidence_phrase="T matrix is defined in Equation 4.6",
        )
    ]

    _, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert out_relations[0].predicate == "defined_in"
    assert counters["evidence_cue_repair_count"] == 1


def test_low_predicate_confidence_blocks_evidence_repair():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    raw = """
    {
      "schema_version": "polymath.extract.v1",
      "chunk_id": "c1",
      "doc_id": "d1",
      "corpus_id": "corp1",
      "entities": [
        {"canonical_name": "diagnostic", "surface_form": "diagnostic", "entity_type": "Method", "confidence": 0.9},
        {"canonical_name": "condition", "surface_form": "condition", "entity_type": "Concept", "confidence": 0.9}
      ],
      "candidate_facts": [
        {
          "atomic_fact": "The diagnostic tests the condition.",
          "candidate_subject": "diagnostic",
          "candidate_predicate": "tests",
          "candidate_object": "condition",
          "object_kind": "entity",
          "predicate_confidence": 0.42,
          "extraction_confidence": 0.9,
          "evidence_phrase": "the diagnostic tests the condition"
        }
      ],
      "relations": []
    }
    """

    parsed = _parse(
        raw,
        ExtractionTask("c1", "d1", "corp1", "the diagnostic tests the condition"),
        threshold=0.5,
        schema=ctx,
    )

    assert parsed is not None
    assert parsed.relations[0].predicate == "related_to"
    assert parsed.relations[0].source_predicate == "tests"


def test_parse_keeps_alternative_predicate_string_as_single_item():
    raw = """
    {
      "schema_version": "polymath.extract.v1",
      "chunk_id": "c1",
      "doc_id": "d1",
      "corpus_id": "corp1",
      "entities": [
        {"canonical_name": "model", "surface_form": "model", "entity_type": "Product", "confidence": 0.9},
        {"canonical_name": "android", "surface_form": "Android", "entity_type": "Product", "confidence": 0.9}
      ],
      "relations": [
        {
          "subject": "model",
          "predicate": "runs_on",
          "object": "android",
          "object_kind": "entity",
          "confidence": 0.9,
          "predicate_confidence": 0.95,
          "extraction_confidence": 0.9,
          "alternative_predicates_considered": "uses",
          "rejection_reasoning": "Android is the runtime substrate.",
          "evidence_phrase": "the model runs on Android"
        }
      ]
    }
    """

    result = _parse(
        raw,
        ExtractionTask("c1", "d1", "corp1", "text"),
        threshold=0.5,
        schema=SchemaContext(
            entity_schema=UNIVERSAL_ENTITY_SCHEMA,
            relation_schema=UNIVERSAL_RELATION_SCHEMA,
            strict="soft",
        ),
    )

    assert result is not None
    assert result.relations[0].alternative_predicates_considered == ["uses"]


def test_relation_aliases_normalize_before_soft_remap():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("snapshot json", "Snapshot JSON", "Document", 0.9),
        EntityItem("sqlite", "SQLite", "Product", 0.9),
    ]
    relations = [
        RelationItem("snapshot json", "stored_in", "sqlite", "entity", 0.9)
    ]

    _, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert out_relations[0].subject == "sqlite"
    assert out_relations[0].predicate == "stores"
    assert out_relations[0].object == "snapshot json"
    assert out_relations[0].source_predicate == "stored_in"
    assert counters["relation_remap_count"] == 0


def test_relation_alias_normalizer_reports_direction():
    assert normalize_relation_predicate_alias("used_by") == ("uses", True)
    assert normalize_relation_predicate_alias("provides") == ("supports", False)
    assert normalize_relation_predicate_alias("measured_by") == ("measures", True)
    assert normalize_relation_predicate_alias("defined_by") == ("defined_by", False)
    assert normalize_relation_predicate_alias("shown_in") == ("illustrated_in", False)
    assert normalize_relation_predicate_alias("same_as") == ("equivalent_to", False)
    assert normalize_relation_predicate_alias("motivated_by") == ("motivates", True)
    assert normalize_relation_predicate_alias("uses strategically") == ("leverages", False)
    assert normalize_relation_predicate_alias("evaluates") == ("evaluates", False)
    assert normalize_relation_predicate_alias("checks") == ("checks", False)
    assert normalize_relation_predicate_alias("scores") == ("scores", False)
    assert normalize_relation_predicate_alias("estimates") == ("estimates", False)


def test_domain_range_keeps_valid_relation():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("sam", "Sam", "Person", 0.9),
        EntityItem("openai", "OpenAI", "Organization", 0.9),
    ]
    relations = [RelationItem("sam", "works_for", "openai", "entity", 0.9)]

    _, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert out_relations[0].predicate == "works_for"
    assert counters["domain_range_remap_count"] == 0


def test_relation_family_groups_raw_predicates():
    assert relation_family_for_predicate("part_of") == "Structural"
    assert relation_family_for_predicate("uses") == "Operational"
    assert relation_family_for_predicate("runs_on") == "Operational"
    assert relation_family_for_predicate("trained_on") == "Operational"
    assert relation_family_for_predicate("represents") == "Referential"
    assert relation_family_for_predicate("references") == "Referential"
    assert relation_family_for_predicate("causes") == "Causal"
    assert relation_family_for_predicate("contradicts") == "Conflict"
    assert relation_family_for_predicate("related_to") == "WeakAssociation"


def test_related_to_refinement_uses_deterministic_facets():
    subject = {
        "canonical_name": "the council",
        "primary_entity_type": "Product",
        "domain_type": "Feature",
    }
    model_object = {
        "canonical_name": "local model",
        "primary_entity_type": "Product",
        "domain_type": "AIModel",
        "object_kind": "Model",
    }
    constraint_object = {
        "canonical_name": "message limit",
        "primary_entity_type": "Rule",
        "domain_type": "Constraint",
    }
    vague_object = {
        "canonical_name": "ambiguous idea",
        "primary_entity_type": "Concept",
    }

    assert refine_related_to_predicate("related_to", subject, model_object) == "related_to"
    assert (
        refine_related_to_predicate("related_to", subject, constraint_object)
        == "related_to"
    )
    assert (
        refine_related_to_predicate("related_to", subject, vague_object)
        == "related_to"
    )
    assert (
        refine_related_to_predicate(
            "related_to",
            subject,
            model_object,
            evidence_phrase="the council uses the local model",
        )
        == "uses"
    )
    assert refine_related_to_predicate("uses", subject, model_object) == "uses"


def test_related_to_refinement_recovers_source_predicate_with_evidence():
    subject = {
        "canonical_name": "module",
        "primary_entity_type": "Concept",
    }
    target = {
        "canonical_name": "monetization model",
        "primary_entity_type": "Concept",
    }

    assert (
        refine_related_to_predicate(
            "related_to",
            subject,
            target,
            source_predicate="part_of",
            evidence_phrase="module purchase, additional book generation, and annual me book",
        )
        == "related_to"
    )


def test_related_to_refinement_keeps_vague_associations_weak():
    subject = {
        "canonical_name": "chapter one",
        "primary_entity_type": "Document",
    }
    target = {
        "canonical_name": "chapter two",
        "primary_entity_type": "Document",
    }

    assert (
        refine_related_to_predicate(
            "related_to",
            subject,
            target,
            source_predicate="references",
            evidence_phrase="See also chapter two for a similar comparison.",
        )
        == "related_to"
    )


def test_related_to_refinement_low_predicate_confidence_stays_weak():
    app = {
        "canonical_name": "council app",
        "primary_entity_type": "Product",
        "domain_type": "Feature",
        "object_kind": "App",
    }
    api = {
        "canonical_name": "profile api",
        "primary_entity_type": "Product",
        "domain_type": "CloudService",
        "object_kind": "API",
    }

    assert (
        refine_related_to_predicate(
            "related_to",
            app,
            api,
            evidence_phrase="the app calls the profile API",
            validation_status="low_predicate_confidence+review_required",
            predicate_confidence=0.42,
        )
        == "related_to"
    )


def test_related_to_refinement_embodies_does_not_become_implements():
    practice = {
        "canonical_name": "daily practice",
        "primary_entity_type": "Product",
        "domain_type": "Feature",
    }
    discipline = {
        "canonical_name": "self discipline",
        "primary_entity_type": "Concept",
    }

    assert (
        refine_related_to_predicate(
            "related_to",
            practice,
            discipline,
            evidence_phrase="daily practice embodies self-discipline",
        )
        == "embodies"
    )


def test_related_to_refinement_uses_evidence_and_source_predicate():
    subject = {
        "canonical_name": "tensorflow lite",
        "primary_entity_type": "Product",
        "domain_type": "AIModel",
        "object_kind": "Model",
    }
    device = {
        "canonical_name": "android device",
        "primary_entity_type": "Product",
        "domain_type": "Device",
        "object_kind": "Device",
    }
    dataset = {
        "canonical_name": "fashion mnist",
        "primary_entity_type": "Document",
        "domain_type": "Dataset",
        "object_kind": "Dataset",
    }

    assert (
        refine_related_to_predicate(
            "related_to",
            subject,
            device,
            evidence_phrase="TensorFlow Lite runs on Android devices for on-device inference.",
        )
        == "runs_on"
    )
    assert (
        refine_related_to_predicate(
            "related_to",
            subject,
            dataset,
            source_predicate="trained_on",
            evidence_phrase="TensorFlow Lite is trained on Fashion MNIST.",
        )
        == "trained_on"
    )


def test_object_kind_and_domain_type_compatibility_fallback():
    app = {
        "canonical_name": "council app",
        "primary_entity_type": "Product",
        "domain_type": "Feature",
        "object_kind": "App",
    }
    api = {
        "canonical_name": "profile api",
        "primary_entity_type": "Product",
        "domain_type": "CloudService",
        "object_kind": "API",
    }
    report = {
        "canonical_name": "architecture report",
        "primary_entity_type": "Document",
        "domain_type": "OutputArtifact",
        "object_kind": "Report",
    }

    assert refine_related_to_predicate("related_to", app, api) == "related_to"
    assert (
        refine_related_to_predicate(
            "related_to",
            app,
            api,
            source_predicate="calls",
            evidence_phrase="the app calls the profile API",
        )
        == "calls"
    )
    assert (
        refine_related_to_predicate(
            "related_to",
            app,
            report,
            evidence_phrase="the app generates the architecture report",
        )
        == "produces"
    )


def test_entity_aliases_canonicalize_before_id_generation():
    assert canonicalize_entity_name("Open AI Inc.") == "openai"
    assert entity_id_from_name("Open AI Inc.", "Organization") == "entity:openai"


def test_entity_id_collapses_type_splits():
    ids = {
        entity_id_from_name("PVector", "Product"),
        entity_id_from_name("p-vector", "Method"),
        entity_id_from_name("p vector", "Concept"),
    }
    assert ids == {"entity:pvector"}


def test_primary_entity_type_uses_curated_override_then_observed_types():
    assert resolve_primary_entity_type(
        "pvector", ["Product", "Method", "Concept"]
    ) == "Artifact"
    assert resolve_primary_entity_type(
        "OpenAI", ["Concept", "Organization"]
    ) == "Organization"


def test_object_kind_facets_infer_library():
    assert resolve_facets("Box2D", "Artifact") == {
        "object_kind": "Library",
        "object_kind_parent": "CodeArtifact",
        "object_kind_root": "Artifact",
    }
    assert resolve_facets("Box2D", "Product") == {
        "object_kind": "Library",
        "object_kind_parent": "CodeArtifact",
        "object_kind_root": "Product",
    }


def test_object_kind_facets_infer_report():
    assert resolve_facets("Architecture_Feasibility_Report.docx", "Document") == {
        "object_kind": "Report",
        "object_kind_parent": "Document",
        "object_kind_root": "Document",
    }


def test_canonical_family_resolution():
    assert resolve_canonical_family("PBox2D") == "physics_simulation"
    assert resolve_canonical_family("gen ai") == "generative_ai"
    assert resolve_canonical_family("user profile extraction") == "identity_extraction"
    assert resolve_canonical_family("PVector") == "creative_coding"
    assert resolve_canonical_family("The Council") == "council_chat"
    assert resolve_canonical_family("Book JSON") == "book_generation"


def test_domain_type_facets_infer_prd_roles():
    assert resolve_domain_type("The Council", "Product") == {
        "domain_type": "Feature",
        "domain_type_parent": "ProductBehavior",
        "domain_type_root": "PRD",
    }
    assert resolve_domain_type("Book JSON", "Document") == {
        "domain_type": "DataObject",
        "domain_type_parent": "ProductData",
        "domain_type_root": "PRD",
    }
    assert resolve_domain_type("Gate C", "Rule") == {
        "domain_type": "Constraint",
        "domain_type_parent": "ProductRule",
        "domain_type_root": "PRD",
    }


def test_ontology_metadata_combines_facets_family_and_version():
    assert resolve_ontology_metadata("Box2D", "Product") == {
        "object_kind": "Library",
        "object_kind_parent": "CodeArtifact",
        "object_kind_root": "Product",
        "canonical_family": "physics_simulation",
        "ontology_version": ONTOLOGY_VERSION,
    }

    assert resolve_ontology_metadata("The Council", "Product") == {
        "object_kind": "App",
        "object_kind_parent": "Product",
        "object_kind_root": "Product",
        "domain_type": "Feature",
        "domain_type_parent": "ProductBehavior",
        "domain_type_root": "PRD",
        "canonical_family": "council_chat",
        "ontology_version": ONTOLOGY_VERSION,
    }
