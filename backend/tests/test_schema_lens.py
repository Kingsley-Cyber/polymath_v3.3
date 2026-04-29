from types import SimpleNamespace

from services.ghost_b import (
    SchemaContext,
    build_user_prompt,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
)
from services.ingestion.schema_lens import (
    build_deterministic_schema_lens,
    sanitize_schema_lens,
)


def _child(text: str):
    return SimpleNamespace(text=text)


def test_deterministic_schema_lens_detects_local_domains():
    lens = build_deterministic_schema_lens(
        corpus_id="c" * 36,
        filename="Architecture_Feasibility_Report.md",
        parents=[],
        children=[
            _child(
                "The PRD describes a generative AI app that uses an LLM, "
                "depends on vector embeddings, and implements identity extraction."
            )
        ],
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
    )

    assert "product_prd" in lens.corpus_domains
    assert "generative_ai" in lens.corpus_domains
    assert "architecture_feasibility" in lens.canonical_families
    assert "uses" in lens.preferred_relations
    assert "depends_on" in lens.preferred_relations
    assert "Document" in lens.preferred_entity_types


def test_schema_lens_sanitizer_clamps_llm_output_to_approved_schema():
    base = build_deterministic_schema_lens(
        corpus_id="c" * 36,
        filename="sample.md",
        parents=[],
        children=[_child("The app is built on a model and powered by an API.")],
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
    )
    lens = sanitize_schema_lens(
        {
            "preferred_entity_types": ["Concept", "MagicalType"],
            "preferred_relations": ["uses", "conceptually_echoes"],
            "relation_aliases": {
                "powered by": "uses",
                "emotionally supports": "conceptually_echoes",
            },
            "corpus_domains": ["Product Strategy"],
            "canonical_families": ["Identity Extraction"],
        },
        base=base,
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        source="llm+deterministic",
    )

    assert "Concept" in lens.preferred_entity_types
    assert "MagicalType" not in lens.preferred_entity_types
    assert "uses" in lens.preferred_relations
    assert "conceptually_echoes" not in lens.preferred_relations
    assert lens.relation_aliases["powered by"] == "uses"
    assert "emotionally supports" not in lens.relation_aliases
    assert "product_strategy" in lens.corpus_domains
    assert "identity_extraction" in lens.canonical_families


def test_schema_lens_renders_as_guidance_not_schema():
    lens = build_deterministic_schema_lens(
        corpus_id="c" * 36,
        filename="sample.md",
        parents=[],
        children=[_child("The app is built on an LLM and produces book JSON.")],
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
    )
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    prompt = build_user_prompt(
        chunk_id="chunk-1",
        doc_id="doc-1",
        corpus_id="corpus-1",
        text="sample",
        schema=ctx,
        schema_lens=lens,
    )

    assert "Corpus schema lens (guidance only; not new output fields)" in prompt
    assert "prefer these approved predicates" in prompt
    assert "Never output the lens fields themselves" in prompt
    assert "Never invent a new predicate" in prompt
