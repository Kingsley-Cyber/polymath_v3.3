"""P2.2 capture-at-generation — latent-concept contract clamps."""

from services.ingestion.summary_semantics import (
    MAX_LATENT_CONCEPTS,
    SEMANTIC_SUMMARY_INSTRUCTION,
    parse_latent_concepts,
    parse_semantic_summary,
)


def test_instruction_declares_latent_contract():
    assert "latent_concepts" in SEMANTIC_SUMMARY_INSTRUCTION
    assert "direct|inferred" in SEMANTIC_SUMMARY_INSTRUCTION


def test_parse_clamps_normalizes_and_drops_invalid():
    obj = {
        "latent_concepts": [
            {"concept": "Source Credibility", "evidence_basis": "direct",
             "aliases": ["ads that feel real", "ADS THAT FEEL REAL", "trust", "extra4"]},
            {"concept": "source credibility", "evidence_basis": "direct"},  # dupe
            {"concept": "speculation", "evidence_basis": "speculative"},  # basis dropped
            {"concept": "", "evidence_basis": "direct"},  # empty
            "not-a-dict",
            {"concept": "x" * 90, "evidence_basis": "inferred"},  # too long
        ]
        + [
            {"concept": f"concept_{i}", "evidence_basis": "inferred"}
            for i in range(20)
        ]
    }
    rows = parse_latent_concepts(obj)
    assert len(rows) == MAX_LATENT_CONCEPTS
    first = rows[0]
    assert first["concept"] == "source_credibility"
    assert first["evidence_basis"] == "direct"
    assert first["aliases"] == ["ads that feel real", "trust", "extra4"]
    assert all(r["evidence_basis"] in {"direct", "inferred"} for r in rows)


def test_parse_semantic_summary_carries_latent_concepts():
    raw = (
        '{"summary": "This passage explains social proof in advertising using '
        'user testimonials and reviews.", "domain": "marketing", '
        '"semantic_chunk_type": "concept", "key_terms": ["social proof"], '
        '"mechanisms": ["social_proof"], "central_claim": "Testimonials build '
        'trust.", "key_points": [], "concept_tags": ["social proof"], '
        '"entity_hints": [], "retrieval_uses": ["claim"], '
        '"abstraction_level": "medium", '
        '"latent_concepts": [{"concept": "authenticity cues", '
        '"evidence_basis": "inferred", "aliases": ["feels real"]}]}'
    )
    out = parse_semantic_summary(raw, source_child_ids=["c1"])
    assert out["latent_concepts"] == [
        {
            "concept": "authenticity_cues",
            "evidence_basis": "inferred",
            "aliases": ["feels real"],
        }
    ]


def test_missing_latent_concepts_defaults_empty():
    out = parse_semantic_summary(
        '{"summary": "A passage about lighting setups for interviews and '
        'their contrast ratios."}',
        source_child_ids=[],
    )
    assert out["latent_concepts"] == []
