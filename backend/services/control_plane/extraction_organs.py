"""Extraction ORGANS as first-class, accountable control-plane stages.

THE DEFECT THIS FIXES
    `compile_document_contract` declares extraction as ONE boolean:
        {"extraction_required": True, ...}
    So the control plane can certify a document as extracted while three of the
    four extraction organs produced nothing. That is exactly what happened:
    `relations=[]` and `facts=[]` were hardcoded and the facet pass never ran,
    across 362,142 chunks, for months, with every health signal green.

    The control plane did not know its own process. It knew a step named
    "extraction"; it did not know that step has four sub-stages, which lane owns
    each, or whether each actually fired.

WHAT THIS ADDS
    Each organ becomes a declared stage with:
      - an OWNER LANE — who is accountable for producing it,
      - a REQUIREMENT rule — whether this document is supposed to have it,
      - an OBSERVATION — what was actually produced,
      - a REPAIR ROUTE — the concrete job that closes the gap.

    A stage that cannot name its repair route is not a stage, it is a wish. So
    `repair_route` is mandatory and asserted by tests.

THE LANE ASYMMETRY THIS EXPOSED
    Neither lane runs all four organs:
      pod lane  (runpod_local_extraction) : entities, relations, facts, claims — NO facets
      local lane (ghost_b_local)          : entities, facets, relations, facts — NO claims
    Encoding the owner lane per organ is what makes that asymmetry visible
    instead of folklore.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

ORGAN_CONTRACT_VERSION = "polymath.extraction_organs.v1"

# Lane identifiers, matching ExtractionResult.provider.
LANE_POD = "runpod_local_extraction"
LANE_LOCAL = "ghost_b_local"
LANE_ANY = "*"

ORGAN_ENTITIES = "entities"
ORGAN_FACETS = "facets"
ORGAN_RELATIONS = "relations"
ORGAN_FACTS = "facts"
ORGAN_CLAIMS = "claims"


@dataclass(frozen=True)
class OrganSpec:
    """One accountable extraction sub-stage."""

    organ: str
    #: Which lanes are SUPPOSED to produce this organ today.
    produced_by: tuple[str, ...]
    #: Where the value lives on an ExtractionResult / stored row.
    field_path: str
    #: The concrete job that closes a gap. A stage with no repair route is a
    #: wish, not a stage.
    repair_route: str
    #: True when a document with chunks must have this organ. False = optional.
    required: bool
    #: Costs a model forward pass to repair (vs a free local recompute).
    needs_model_pass: bool
    notes: str = ""

    def to_doc(self) -> dict[str, Any]:
        return asdict(self)


ORGAN_SPECS: tuple[OrganSpec, ...] = (
    OrganSpec(
        organ=ORGAN_ENTITIES,
        produced_by=(LANE_POD, LANE_LOCAL),
        field_path="entities",
        repair_route="re-extract chunk (GLiNER pass-1); no cheaper route exists",
        required=True,
        needs_model_pass=True,
        notes="Only organ that was never broken on either lane.",
    ),
    OrganSpec(
        organ=ORGAN_FACETS,
        produced_by=(LANE_LOCAL,),
        field_path="entities[].object_kind",
        repair_route="facet_tagger.tag_facets over unique canonical_names",
        required=False,
        needs_model_pass=True,
        notes=(
            "GLiNER pass-2. NOT produced by the pod lane at all — the single "
            "largest remaining gap. Deduped per unique canonical_name (6:1 vs "
            "mentions), so repair is ~467k forwards, not 2.8M."
        ),
    ),
    OrganSpec(
        organ=ORGAN_RELATIONS,
        produced_by=(LANE_POD, LANE_LOCAL),
        field_path="relations",
        repair_route="backfill_relations.py (frame extractor; local recompute)",
        required=True,
        needs_model_pass=False,
        notes=(
            "Was hardcoded [] on the pod lane. Repaired 2026-07-30. Free to "
            "recompute: a pure function of stored text + entities."
        ),
    ),
    OrganSpec(
        organ=ORGAN_FACTS,
        produced_by=(LANE_POD, LANE_LOCAL),
        field_path="facts",
        repair_route="backfill_facts.py (enrich Stage D; local recompute)",
        required=True,
        needs_model_pass=False,
        notes="Was hardcoded [] on the pod lane. Repaired 2026-07-31.",
    ),
    OrganSpec(
        organ=ORGAN_CLAIMS,
        produced_by=(LANE_POD,),
        field_path="claim_compilation.claims",
        repair_route="compile_claim_records_v1 over the spaCy bundle",
        required=False,
        needs_model_pass=False,
        notes=(
            "MIRROR of the facet gap: produced by the pod lane, NOT by the "
            "local lane (cpcs_local measures 0.000). Neither lane runs all "
            "four organs."
        ),
    ),
)

ORGAN_BY_NAME: dict[str, OrganSpec] = {s.organ: s for s in ORGAN_SPECS}


def organs_expected_for_lane(lane: str) -> tuple[str, ...]:
    """Which organs a given lane is accountable for producing."""
    return tuple(
        s.organ for s in ORGAN_SPECS
        if LANE_ANY in s.produced_by or lane in s.produced_by
    )


def lane_coverage_gaps() -> dict[str, tuple[str, ...]]:
    """Organs each lane does NOT produce. The asymmetry, made explicit."""
    all_organs = tuple(s.organ for s in ORGAN_SPECS)
    out: dict[str, tuple[str, ...]] = {}
    for lane in (LANE_POD, LANE_LOCAL):
        produced = set(organs_expected_for_lane(lane))
        out[lane] = tuple(o for o in all_organs if o not in produced)
    return out


def compile_organ_contract(lane: str) -> dict[str, Any]:
    """Per-organ contract for a document extracted by `lane`.

    Replaces the single `extraction_required` boolean with a declaration the
    reconciler can actually check organ by organ.
    """
    expected = set(organs_expected_for_lane(lane))
    return {
        "organ_contract_version": ORGAN_CONTRACT_VERSION,
        "lane": lane,
        "organs": {
            s.organ: {
                "expected": s.organ in expected,
                "required": s.required and s.organ in expected,
                "repair_route": s.repair_route,
                "needs_model_pass": s.needs_model_pass,
                "field_path": s.field_path,
            }
            for s in ORGAN_SPECS
        },
        "not_produced_by_this_lane": sorted(
            s.organ for s in ORGAN_SPECS if s.organ not in expected
        ),
    }
