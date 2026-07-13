"""T-HOOK-2 capture-at-generation — temporal contract clamps.

Mirrors tests/test_summary_semantics_latent.py: the summary contract now also
carries ``temporal_class`` + ``time_expressions`` through the SAME seam as
``latent_concepts``, with deterministic Python clamping (strict enum, verbatim
source check, in-code char offsets, silent drops counted).
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from models.contracts import ParentSummaryRecord
from services.ghost_a import parse_tagged_summary_response
from services.ingestion.summary_backfill import summary_result_fields
from services.ingestion.summary_semantics import (
    MAX_TIME_EXPRESSIONS,
    SEMANTIC_SUMMARY_INSTRUCTION,
    TEMPORAL_CLASSES,
    TEMPORAL_PARSE_COUNTERS,
    canonical_parent_summary_fields,
    parse_semantic_summary,
    parse_temporal_semantics,
    repair_parent_summary_row,
)
from services.storage import qdrant_writer


def _counters() -> dict:
    return dict(TEMPORAL_PARSE_COUNTERS)


def test_instruction_declares_temporal_contract():
    assert "temporal_class" in SEMANTIC_SUMMARY_INSTRUCTION
    assert "time_expressions" in SEMANTIC_SUMMARY_INSTRUCTION
    assert "|".join(TEMPORAL_CLASSES) in SEMANTIC_SUMMARY_INSTRUCTION
    # latent contract stays byte-identical on the same seam
    assert "latent_concepts" in SEMANTIC_SUMMARY_INSTRUCTION
    assert "direct|inferred" in SEMANTIC_SUMMARY_INSTRUCTION


def test_parse_validates_enum_and_counts_invalid_class():
    before = _counters()
    out = parse_temporal_semantics({"temporal_class": "Versioned"})
    assert out["temporal_class"] == "versioned"

    out = parse_temporal_semantics({"temporal_class": "timeless_wisdom"})
    assert out["temporal_class"] is None
    assert (
        TEMPORAL_PARSE_COUNTERS["invalid_temporal_class"]
        == before["invalid_temporal_class"] + 1
    )

    # absent / empty is None WITHOUT an invalid-count (nothing was malformed)
    mid = _counters()
    assert parse_temporal_semantics({})["temporal_class"] is None
    assert parse_temporal_semantics({"temporal_class": ""})["temporal_class"] is None
    assert (
        TEMPORAL_PARSE_COUNTERS["invalid_temporal_class"]
        == mid["invalid_temporal_class"]
    )


def test_parse_clamps_verifies_and_drops_time_expressions():
    source = "TikTok updated its ad policy in March 2024, effective Q3 2024."
    before = _counters()
    obj = {
        "temporal_class": "versioned",
        "time_expressions": [
            {"text": "March 2024", "role": "event_time"},
            {"text": "march 2024", "role": "event_time"},  # not verbatim
            {"text": "Q3 2024", "role": "party_time"},  # bad role -> unknown
            {"text": "June 1999", "role": "event_time"},  # not in source
            {"text": "", "role": "event_time"},  # empty
            {"text": "x" * 90, "role": "event_time"},  # too long
            "not-a-dict",
        ],
    }
    out = parse_temporal_semantics(obj, source_text=source)
    assert out["temporal_class"] == "versioned"
    assert out["time_expressions"] == [
        {
            "text": "March 2024",
            "role": "event_time",
            "char_start": source.index("March 2024"),
            "char_end": source.index("March 2024") + len("March 2024"),
        },
        {
            "text": "Q3 2024",
            "role": "unknown",
            "char_start": source.index("Q3 2024"),
            "char_end": source.index("Q3 2024") + len("Q3 2024"),
        },
    ]
    assert (
        TEMPORAL_PARSE_COUNTERS["unverifiable_time_expression"]
        == before["unverifiable_time_expression"] + 2
    )
    assert (
        TEMPORAL_PARSE_COUNTERS["malformed_time_expression"]
        == before["malformed_time_expression"] + 3
    )


def test_parse_caps_time_expression_count():
    source = " ".join(f"year {1900 + i}" for i in range(30))
    obj = {
        "time_expressions": [
            {"text": f"year {1900 + i}", "role": "reference_time"}
            for i in range(30)
        ]
    }
    out = parse_temporal_semantics(obj, source_text=source)
    assert len(out["time_expressions"]) == MAX_TIME_EXPRESSIONS


def test_parse_is_idempotent_on_clamped_rows():
    source = "The FACS manual was revised in 2002."
    first = parse_temporal_semantics(
        {
            "temporal_class": "slowly_evolving",
            "time_expressions": [{"text": "2002", "role": "revision_time"}],
        },
        source_text=source,
    )
    mid = _counters()
    second = parse_temporal_semantics(first, source_text=source)
    assert second == first
    assert _counters() == mid  # no drops re-counted


def test_repeated_literal_gets_deterministic_occurrences_and_roles():
    source = "Published in 2024. Revised in 2024 after review."
    out = parse_temporal_semantics(
        {
            "temporal_class": "versioned",
            "time_expressions": [
                {"text": "2024", "role": "publication_time"},
                {"text": "2024", "role": "revision_time"},
            ],
        },
        source_text=source,
    )

    assert [row["role"] for row in out["time_expressions"]] == [
        "publication_time",
        "revision_time",
    ]
    assert [row["char_start"] for row in out["time_expressions"]] == [
        source.index("2024"),
        source.rindex("2024"),
    ]
    assert parse_temporal_semantics(out, source_text=source) == out


def test_parse_semantic_summary_carries_temporal_with_latent():
    source = (
        "TikTok updated its advertising policy in March 2024 to require "
        "authenticity disclosures on testimonial ads."
    )
    raw = (
        '{"summary": "This passage explains the March 2024 TikTok advertising '
        'policy change requiring authenticity disclosures on testimonials.", '
        '"domain": "marketing", "semantic_chunk_type": "claim", '
        '"key_terms": ["TikTok"], "mechanisms": ["platform_policy"], '
        '"central_claim": "TikTok requires authenticity disclosures.", '
        '"key_points": [], "concept_tags": ["ad policy"], "entity_hints": [], '
        '"retrieval_uses": ["claim"], "abstraction_level": "medium", '
        '"latent_concepts": [{"concept": "authenticity cues", '
        '"evidence_basis": "inferred", "aliases": ["feels real"]}], '
        '"temporal_class": "versioned", '
        '"time_expressions": [{"text": "March 2024", "role": "effective_time"}]}'
    )
    out = parse_semantic_summary(raw, source_child_ids=["c1"], source_text=source)
    assert out["temporal_class"] == "versioned"
    assert out["time_expressions"] == [
        {
            "text": "March 2024",
            "role": "effective_time",
            "char_start": source.index("March 2024"),
            "char_end": source.index("March 2024") + len("March 2024"),
        }
    ]
    # latent behavior unchanged, both families ride the same artifact
    assert out["latent_concepts"] == [
        {
            "concept": "authenticity_cues",
            "evidence_basis": "inferred",
            "aliases": ["feels real"],
        }
    ]


def test_missing_temporal_defaults_empty():
    out = parse_semantic_summary(
        '{"summary": "A passage about lighting setups for interviews and '
        'their contrast ratios."}',
        source_child_ids=[],
    )
    assert out["temporal_class"] == "unknown"
    assert out["time_expressions"] == []


def test_canonical_fields_carry_all_four_families():
    source = "Laban movement theory, formalized in the 1940s, remains standard."
    parsed = parse_semantic_summary(
        '{"summary": "Laban movement theory was formalized in the 1940s and '
        'remains the standard vocabulary for movement analysis in film '
        'performance and animation reference work today.", '
        '"domain": "movement", "semantic_chunk_type": "principle", '
        '"key_terms": ["Laban"], "mechanisms": [], '
        '"central_claim": "Laban theory remains standard.", "key_points": [], '
        '"concept_tags": ["movement analysis"], "entity_hints": [], '
        '"retrieval_uses": ["definition"], "abstraction_level": "high", '
        '"latent_concepts": [{"concept": "movement vocabulary", '
        '"evidence_basis": "direct", "aliases": ["how actors move"]}], '
        '"temporal_class": "evergreen", '
        '"time_expressions": [{"text": "the 1940s", "role": "event_time"}]}',
        source_child_ids=["c1"],
        source_text=source,
    )
    fields = canonical_parent_summary_fields(
        parsed,
        parent_id="p1",
        doc_id="d1",
        corpus_id="corpus1",
        source_text=source,
        source_child_ids=["c1"],
        summary_model="test/model",
    )
    assert fields["latent_concepts"][0]["concept"] == "movement_vocabulary"
    assert fields["latent_concepts"][0]["aliases"] == ["how actors move"]
    assert fields["temporal_class"] == "evergreen"
    assert fields["time_expressions"][0]["text"] == "the 1940s"
    assert fields["time_expressions"][0]["char_start"] == source.index("the 1940s")


def test_typed_writer_boundary_accepts_clamped_and_rejects_junk():
    record = ParentSummaryRecord(
        summary="A validated summary.",
        latent_concepts=[
            {"concept": "movement_vocabulary", "evidence_basis": "direct",
             "aliases": ["how actors move"]}
        ],
        temporal_class="evergreen",
        time_expressions=[
            {"text": "the 1940s", "role": "event_time",
             "char_start": 0, "char_end": 9}
        ],
    )
    dumped = record.model_dump()
    assert dumped["temporal_class"] == "evergreen"
    assert dumped["time_expressions"][0]["text"] == "the 1940s"
    assert dumped["latent_concepts"][0]["aliases"] == ["how actors move"]

    with pytest.raises(ValidationError):
        ParentSummaryRecord(summary="x", temporal_class="timeless_wisdom")
    with pytest.raises(ValidationError):
        ParentSummaryRecord(summary="x", time_expressions=[{"text": ""}])


@pytest.mark.parametrize(
    "fields",
    [
        {"summary": "   "},
        {
            "summary": "x",
            "time_expressions": [{"text": "2024", "role": "party_time"}],
        },
        {
            "summary": "x",
            "time_expressions": [
                {"text": "2024", "char_start": 3, "char_end": None}
            ],
        },
        {
            "summary": "x",
            "time_expressions": [
                {"text": "2024", "char_start": 8, "char_end": 4}
            ],
        },
        {
            "summary": "x",
            "time_expressions": [
                {"text": str(index), "role": "unknown"} for index in range(13)
            ],
        },
        {"summary": "x", "surprise": True},
        {
            "summary": "x",
            "time_expressions": [{"text": "2024", "surprise": True}],
        },
    ],
)
def test_typed_writer_boundary_rejects_malformed_records(fields):
    with pytest.raises(ValidationError):
        ParentSummaryRecord(**fields)


def test_typed_writer_boundary_defaults_temporal_explicitly():
    dumped = ParentSummaryRecord(summary="A usable summary.").model_dump()
    assert dumped["temporal_class"] == "unknown"
    assert dumped["time_expressions"] == []


def test_tagged_rescue_captures_latent_and_temporal_fields():
    source = (
        "In March 2024, TikTok revised its testimonial policy and required "
        "advertisers to disclose authenticity cues."
    )
    raw = """
SUMMARY: TikTok revised its testimonial advertising policy in March 2024. The policy requires advertisers to disclose authenticity cues so audiences can recognize sponsored endorsements and understand how testimonial content is presented.
CLAIM: TikTok requires authenticity disclosures for testimonial advertising.
POINT: The policy applies to testimonial advertisements.
TAGS: TikTok | testimonial policy | authenticity disclosure
MECHANISM: disclosure requirement
ABSTRACTION: medium
LATENT: inferred | perceived authenticity | feels genuine; trust cues
TEMPORAL_CLASS: versioned
TIME: revision_time | March 2024
"""

    parsed = parse_tagged_summary_response(
        raw,
        source_child_ids=["c1"],
        source_text=source,
    )

    assert parsed["latent_concepts"] == [
        {
            "concept": "perceived_authenticity",
            "evidence_basis": "inferred",
            "aliases": ["feels genuine", "trust cues"],
        }
    ]
    assert parsed["temporal_class"] == "versioned"
    assert parsed["time_expressions"] == [
        {
            "text": "March 2024",
            "role": "revision_time",
            "char_start": source.index("March 2024"),
            "char_end": source.index("March 2024") + len("March 2024"),
        }
    ]


def test_repair_preserves_latent_aliases_and_temporal_capture():
    source = (
        "In March 2024, TikTok revised testimonial advertising rules and "
        "required authenticity disclosures."
    )
    fixed = repair_parent_summary_row(
        {
            "parent_id": "p1",
            "doc_id": "d1",
            "corpus_id": "c1",
            "text": source,
            "summary": (
                "TikTok revised its testimonial advertising rules in March "
                "2024 and required advertisers to provide authenticity "
                "disclosures for testimonial content."
            ),
            "summary_model": "unit/model",
            "latent_concepts": [
                {
                    "concept": "perceived_authenticity",
                    "evidence_basis": "inferred",
                    "aliases": ["feels genuine", "trust cues"],
                }
            ],
            "temporal_class": "versioned",
            "time_expressions": [
                {"text": "March 2024", "role": "revision_time"}
            ],
        }
    )

    assert fixed["latent_concepts"][0]["aliases"] == [
        "feels genuine",
        "trust cues",
    ]
    assert fixed["temporal_class"] == "versioned"
    assert fixed["time_expressions"][0]["role"] == "revision_time"


def test_summary_result_fields_normalizes_missing_temporal_values():
    fields = summary_result_fields(
        SimpleNamespace(
            summary="A typed persistence summary.",
            latent_concepts=None,
            temporal_class=None,
            time_expressions=None,
        ),
        updated_at=datetime.now(timezone.utc),
    )

    assert fields["latent_concepts"] == []
    assert fields["temporal_class"] == "unknown"
    assert fields["time_expressions"] == []


@pytest.mark.asyncio
async def test_qdrant_summary_projection_keeps_latent_and_temporal(monkeypatch):
    captured = {}

    async def _owner(*_args, **_kwargs):
        return None

    async def _layout(*_args, **_kwargs):
        return True, False

    async def _capture(_client, *, collection_name, points, point_label):
        captured["payload"] = points[0].payload

    monkeypatch.setattr(qdrant_writer, "_assert_collection_owner", _owner)
    monkeypatch.setattr(qdrant_writer, "_collection_layout", _layout)
    monkeypatch.setattr(qdrant_writer, "_upsert_points_batched", _capture)

    await qdrant_writer.upsert_summaries(
        object(),
        "abcdef1234567890",
        [
            {
                "corpus_id": "abcdef1234567890",
                "doc_id": "d1",
                "parent_id": "p1",
                "source_tier": "tier_a",
                "summary": "A projected summary.",
                "summary_model": "unit/model",
                "latent_concepts": [
                    {
                        "concept": "perceived_authenticity",
                        "evidence_basis": "inferred",
                        "aliases": ["trust cues"],
                    }
                ],
                "temporal_class": "versioned",
                "time_expressions": [
                    {
                        "text": "March 2024",
                        "role": "revision_time",
                        "char_start": 3,
                        "char_end": 13,
                    }
                ],
            }
        ],
        [[0.1, 0.2]],
        target_kinds=["hrag"],
    )

    assert captured["payload"]["latent_concepts"][0]["aliases"] == ["trust cues"]
    assert captured["payload"]["temporal_class"] == "versioned"
    assert captured["payload"]["time_expressions"][0]["role"] == "revision_time"
