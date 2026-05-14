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


# ─── Phase 5 Gate 2 — roblox domain rule tests ──────────────────────────────


def test_roblox_domain_triggers_on_luau_code_corpus():
    """A Luau code corpus's sampled text contains game:GetService and
    related triggers — the deterministic builder must surface a roblox
    domain in corpus_domains."""
    lens = build_deterministic_schema_lens(
        corpus_id="c" * 36,
        filename="combat.luau",
        parents=[],
        children=[
            _child(
                "local TweenService = game:GetService('TweenService')\n"
                "local Players = game:GetService('Players')\n"
                "function Combat.PunchAttack(player)\n"
                "    local part = Instance.new('Part')\n"
                "    humanoid:MoveTo(player.Character.HumanoidRootPart.Position)\n"
                "end"
            )
        ],
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
    )
    assert "roblox" in lens.corpus_domains


def test_roblox_lens_method_entity_preference():
    """When the roblox domain fires, the preferred entity types should
    bias toward Method/Product/Artifact — NOT include Person or
    Organization (those make no sense in Roblox API extraction)."""
    lens = build_deterministic_schema_lens(
        corpus_id="c" * 36,
        filename="combat.luau",
        parents=[],
        children=[
            _child(
                "game:GetService('TweenService'):Create(part, info, goal):Play()\n"
                "RemoteEvent:FireServer(target)\n"
                "Instance.new('ParticleEmitter')"
            )
        ],
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
    )
    assert "Method" in lens.preferred_entity_types
    # Roblox lens biases toward technical entity types only.
    # Person/Organization shouldn't appear (those need to come from a
    # research_literature corpus, not a code corpus).
    assert "Person" not in lens.preferred_entity_types
    assert "Organization" not in lens.preferred_entity_types


def test_roblox_relation_aliases_present():
    """Roblox lens supplies aliases that map domain-local phrases
    (`fires`, `connects`, `binds`) to approved predicates."""
    lens = build_deterministic_schema_lens(
        corpus_id="c" * 36,
        filename="net.luau",
        parents=[],
        children=[
            _child(
                "RemoteEvent fires the server signal. The client connects "
                "to a BindableEvent. The script binds to ContextActionService. "
                "TweenService runs the animation. game:GetService handles modules."
            )
        ],
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
    )
    # Aliases only appear when the alias verb is in the sampled text AND
    # the target predicate is in the allowed schema.
    assert lens.relation_aliases.get("fires") == "uses"
    assert lens.relation_aliases.get("connects") == "uses"
    assert lens.relation_aliases.get("binds") == "depends_on"


def test_roblox_domain_only_aliases_surface_when_domain_triggers():
    """Aliases defined ONLY in the per-domain rule (not in the global
    _RELATION_ALIAS_TO_APPROVED map) must surface when the domain fires.
    `exposes -> implements`, `instances -> produces`, `tweens -> uses`
    are Roblox-local: they have no entry in the global alias map, and
    the bug being fixed was that the builder ignored
    rule['relation_aliases'] entirely."""
    lens = build_deterministic_schema_lens(
        corpus_id="c" * 36,
        filename="combat.luau",
        parents=[],
        children=[
            _child(
                "local TweenService = game:GetService('TweenService')\n"
                "function ModuleScript.run() end"
            )
        ],
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
    )
    # These aliases live ONLY in _DOMAIN_RULES['roblox']['relation_aliases'],
    # not in _RELATION_ALIAS_TO_APPROVED. They must appear because the
    # domain triggered — not because the phrase was in the sampled text.
    assert lens.relation_aliases.get("exposes") == "implements"
    assert lens.relation_aliases.get("instances") == "produces"
    assert lens.relation_aliases.get("tweens") == "uses"


def test_non_roblox_corpus_does_not_get_roblox_domain():
    """A pure prose corpus (e.g., a novel chapter, a recipe blog)
    must NOT match the Roblox trigger set. No pollution into
    unrelated corpora."""
    lens = build_deterministic_schema_lens(
        corpus_id="c" * 36,
        filename="spring_garden_recipes.md",
        parents=[],
        children=[
            _child(
                "Spring weather brings fresh asparagus to the table. "
                "The chef binds the herbs with twine before roasting. "
                "Serve with a glass of crisp white wine."
            )
        ],
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
    )
    assert "roblox" not in lens.corpus_domains
