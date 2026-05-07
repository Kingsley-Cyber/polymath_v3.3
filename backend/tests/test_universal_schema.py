"""
Sanity checks for the baked universal schema (GHOST B).

The schema is a contract: Neo4j entity types and RELATES_TO predicates across
every corpus are derived from these two lists. Accidentally renaming or
reordering them breaks cross-corpus queries and (when either vocabulary list
crosses SCHEMA_INLINE_LIMIT) flips ghost_b into degraded retrieval mode.
"""

from config import get_settings
from models.schemas import IngestionConfig
from services.ghost_b import (
    EntityItem,
    RelationItem,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
    SchemaContext,
    _apply_schema,
    _validate_evidence,
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
    # 30 entries: 12 universal entity types do not gate this; the relation
    # list got the canonicalization + missing-affiliation/ownership additions
    # while shedding `calls` (collapsed into `uses`) and `extracts` (merged
    # into `detects`). Net +3 brings the list to the SCHEMA_INLINE_LIMIT.
    assert len(UNIVERSAL_RELATION_SCHEMA) == 30
    assert all(isinstance(p, str) and p.strip() for p in UNIVERSAL_RELATION_SCHEMA)
    assert len(set(UNIVERSAL_RELATION_SCHEMA)) == 30, "relation schema has duplicates"
    assert UNIVERSAL_RELATION_SCHEMA[-1] == "related_to", (
        "related_to sentinel MUST be last"
    )
    for required in (
        "excepts", "overrides", "runs_on", "trained_on",
        "synonym_of", "instance_of", "owns", "affiliated_with", "overlaps",
        "detects",
    ):
        assert required in UNIVERSAL_RELATION_SCHEMA
    # Predicates that were collapsed must NOT reappear in the universal list.
    for removed in ("calls", "extracts"):
        assert removed not in UNIVERSAL_RELATION_SCHEMA


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
    # Tightened format: `Name=gloss|Name=gloss|…` with no spaces around `|`.
    # The verbose paragraphs of the old prompt were dropped to halve the
    # per-chunk extraction prompt; only essential rules remain.
    assert (
        "entity_type one of: Person=human individual|Organization=formal group"
    ) in prompt
    assert (
        "predicate one of: part_of=X subcomponent of Y|member_of=X in group Y"
    ) in prompt
    # Sentinels surface as explicit fallbacks (with [FALLBACK] tag inline)
    assert "'other'" in prompt
    assert "'related_to'" in prompt
    assert "other=fallback [FALLBACK]" in prompt
    assert "related_to=use only when no specific predicate fits [FALLBACK]" in prompt
    assert "evidence_phrase" in prompt
    # Universal predicates still listed in the JSON-example enum
    assert "runs_on" in prompt
    assert "trained_on" in prompt
    # Canonicalization + missing-relation predicates added in the schema patch
    assert "synonym_of" in prompt
    assert "instance_of" in prompt
    assert "owns" in prompt
    assert "affiliated_with" in prompt
    assert "overlaps" in prompt
    # Removed predicates should no longer be advertised in the vocab block
    assert "extracts=" not in prompt
    # `calls` was collapsed into `uses` — ensure neither the vocab line nor
    # the JSON-example enum still advertises it.
    assert "calls=" not in prompt
    assert "|calls|" not in prompt
    # Ontology facet exclusion still enforced
    assert "ontology" in prompt


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

    assert refine_related_to_predicate("related_to", subject, model_object) == "uses"
    assert (
        refine_related_to_predicate("related_to", subject, constraint_object)
        == "depends_on"
    )
    assert (
        refine_related_to_predicate("related_to", subject, vague_object)
        == "implements"
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
        == "part_of"
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
        )
        == "trained_on"
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


# ──────────────────────────────────────────────────────────────────────────
# Phase B — evidence-phrase validation gate
#
# `_validate_evidence(phrase, chunk_text)` powers the Phase B drop logic in
# `_parse`. The runtime path is integration-tested via a real ingest, but
# the cheap surface tests below pin the normalization rules so a future
# change to the regex / casefold / strip behavior surfaces here first.
# ──────────────────────────────────────────────────────────────────────────


def test_validate_evidence_exact_substring():
    chunk = "OpenAI is affiliated with Microsoft. GPT-4 runs on Microsoft Azure."
    assert _validate_evidence("GPT-4 runs on Microsoft Azure", chunk) is True


def test_validate_evidence_lowercase_match():
    chunk = "OpenAI is affiliated with Microsoft."
    assert _validate_evidence("openai is AFFILIATED with microsoft", chunk) is True


def test_validate_evidence_collapsed_whitespace():
    chunk = "GPT-4   runs\non\tMicrosoft  Azure"
    assert _validate_evidence("GPT-4 runs on Microsoft Azure", chunk) is True


def test_validate_evidence_phrase_with_extra_whitespace():
    chunk = "ChatGPT depends on GPT-4 for its responses."
    assert _validate_evidence("  ChatGPT  depends   on   GPT-4  ", chunk) is True


def test_validate_evidence_paraphrase_rejected():
    chunk = "OpenAI was founded in San Francisco in December 2015."
    # Same idea, different words → must be rejected.
    assert _validate_evidence("OpenAI started in SF in late 2015", chunk) is False


def test_validate_evidence_empty_phrase_rejected():
    chunk = "Sam Altman works for OpenAI as CEO."
    assert _validate_evidence("", chunk) is False
    assert _validate_evidence(None, chunk) is False
    assert _validate_evidence("   \n\t  ", chunk) is False


def test_validate_evidence_substring_not_found_rejected():
    chunk = "Microsoft owns a substantial stake in OpenAI."
    # The phrase is plausible English but doesn't appear in the chunk.
    assert _validate_evidence("Microsoft acquired OpenAI", chunk) is False


def test_validate_evidence_chunk_text_empty_rejected():
    # Without source text we can't verify anything — fail closed.
    assert _validate_evidence("anything", "") is False
    assert _validate_evidence("anything", None or "") is False
