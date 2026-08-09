"""Accountable extraction-organ contract for the canonical Graphify lane."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

ORGAN_CONTRACT_VERSION = "polymath.extraction_organs.v1"

# Lane identifiers, matching ExtractionResult.provider.
LANE_GRAPHIFY = "graphify_gliner2_cpu"
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
        produced_by=(LANE_GRAPHIFY,),
        field_path="entities",
        repair_route="rerun the canonical Graphify document pipeline",
        required=True,
        needs_model_pass=True,
        notes="Only organ that was never broken on either lane.",
    ),
    OrganSpec(
        organ=ORGAN_FACETS,
        produced_by=(),
        field_path="entities[].object_kind",
        repair_route="manual historical-artifact review; no live model route",
        required=False,
        needs_model_pass=False,
        notes=(
            "Optional historical field. No production model route owns it."
        ),
    ),
    OrganSpec(
        organ=ORGAN_RELATIONS,
        produced_by=(LANE_GRAPHIFY,),
        field_path="relations",
        repair_route="rerun the canonical Graphify document pipeline",
        required=True,
        needs_model_pass=False,
        notes=(
            "Graphify emits only deterministic, eligibility-filtered relations."
        ),
    ),
    OrganSpec(
        organ=ORGAN_FACTS,
        produced_by=(),
        field_path="facts",
        repair_route="backfill_facts.py (enrich Stage D; local recompute)",
        required=False,
        needs_model_pass=False,
        notes=(
            "Graphify does not claim a separate facts organ."
        ),
    ),
    OrganSpec(
        organ=ORGAN_CLAIMS,
        produced_by=(),
        field_path="claim_compilation.claims",
        repair_route="compile_claim_records_v1 over the spaCy bundle",
        required=False,
        needs_model_pass=False,
        notes=(
            "Claims are an optional downstream compilation stage."
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
    for lane in (LANE_GRAPHIFY,):
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
