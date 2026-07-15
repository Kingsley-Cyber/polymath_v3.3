"""Pure gates for the zero-provider atomic B4 preflight."""

from pathlib import Path

from models.hash_taxonomy import namespace_hash
from models.semantic_digest_atomic_selection import load_atomic_b4_selection_recipe
from models.semantic_validator import ClaimScope, SemanticValidationContext
from scripts.semantic_gateway_mark_atomic_preflight import (
    RECIPE_PATH,
    AtomicPopulationRow,
    _band_for_basis_points,
    _cost_upper_bound,
    _packet_population_set_hash,
    _stratify_and_select,
)
from scripts.semantic_gateway_mark_paid_pass import CanaryPacket, PlannedPacket


def _row(index: int, packet_bytes: int) -> AtomicPopulationRow:
    parent_id = f"parent:{index:03d}"
    item = CanaryPacket(
        packet={"parent_id": parent_id},
        context=SemanticValidationContext.from_owner_registries(
            parent_id=parent_id,
            claims=(ClaimScope(f"claim:{index:03d}", parent_id),),
        ),
        parent_id=parent_id,
        doc_id=f"doc:{index:03d}",
        entity_count=1,
        source_child_count=1,
    )
    return AtomicPopulationRow(
        planned=PlannedPacket(
            item=item,
            ordinal=index,
            job_id=f"job:{index:03d}",
            cache_key=f"cache:{index:03d}",
            input_hash=f"input:{index:03d}",
            packet_bytes=packet_bytes,
        ),
        packet_hash=f"sha256:{index:064x}",
    )


def test_atomic_b4_recipe_is_frozen_and_top_decile_is_explicit() -> None:
    recipe = load_atomic_b4_selection_recipe(RECIPE_PATH)

    assert recipe.target_count == 10
    assert [band.selection_count for band in recipe.bands] == [2, 2, 2, 2, 2]
    assert recipe.bands[-1].band_id == "top_decile_q90_q100"
    assert recipe.bands[-1].lower_basis_points_inclusive == 9000
    assert recipe.summary_faithfulness_review.unsupported_synthesis_allowed is False
    assert namespace_hash("registry", recipe.model_dump(mode="python")).startswith(
        "sha256:"
    )
    assert Path(RECIPE_PATH).is_file()


def test_basis_point_boundaries_resolve_exactly_once() -> None:
    recipe = load_atomic_b4_selection_recipe(RECIPE_PATH)

    assert _band_for_basis_points(recipe, 0).band_id == "q00_q25"
    assert _band_for_basis_points(recipe, 2499).band_id == "q00_q25"
    assert _band_for_basis_points(recipe, 2500).band_id == "q25_q50"
    assert _band_for_basis_points(recipe, 8999).band_id == "q75_q90"
    assert _band_for_basis_points(recipe, 9000).band_id == "top_decile_q90_q100"
    assert _band_for_basis_points(recipe, 9999).band_id == "top_decile_q90_q100"


def test_selection_is_deterministic_stratified_and_document_unique() -> None:
    recipe = load_atomic_b4_selection_recipe(RECIPE_PATH)
    recipe_hash = namespace_hash("registry", recipe.model_dump(mode="python"))
    population = [_row(index, 10_000 + index) for index in range(100)]

    first, bands = _stratify_and_select(
        population,
        recipe=recipe,
        recipe_hash=recipe_hash,
    )
    replay, replay_bands = _stratify_and_select(
        list(reversed(population)),
        recipe=recipe,
        recipe_hash=recipe_hash,
    )

    assert [item.row.parent_id for item in first] == [
        item.row.parent_id for item in replay
    ]
    assert bands == replay_bands
    assert [row["population_count"] for row in bands] == [25, 25, 25, 15, 10]
    assert [row["selection_count"] for row in bands] == [2, 2, 2, 2, 2]
    assert len({item.row.doc_id for item in first}) == 10


def test_cost_authority_uses_packet_bytes_and_route_output_cap() -> None:
    rows = [_row(0, 10_000), _row(1, 20_000)]

    ceiling = _cost_upper_bound(
        rows,
        uncached_input_rate=0.75,
        output_rate=2.95,
        price_unit=1_000_000,
        max_output_tokens=8192,
    )

    expected_before_margin = (
        (10_000 * 0.75 + 8192 * 2.95) + (20_000 * 0.75 + 8192 * 2.95)
    ) / 1_000_000
    assert ceiling == round(expected_before_margin * 2 * 1.10, 8)


def test_packet_set_hash_uses_certified_raw_digest_recipe() -> None:
    population = [_row(0, 10_000), _row(1, 20_000)]

    assert _packet_population_set_hash(population) == namespace_hash(
        "input-set",
        frozenset({f"{0:064x}", f"{1:064x}"}),
    )
