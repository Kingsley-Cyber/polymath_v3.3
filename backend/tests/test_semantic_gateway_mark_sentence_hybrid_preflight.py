"""Pure gates for the sentence-hybrid v3 zero-provider preflight."""

from decimal import Decimal

from models.hash_taxonomy import namespace_hash
from models.semantic_digest_sentence_selection import (
    load_sentence_hybrid_canary_selection_recipe,
)
from models.semantic_validator import ClaimScope, SemanticValidationContext
from scripts.semantic_gateway_mark_paid_pass import CanaryPacket, PlannedPacket
from scripts.semantic_gateway_mark_sentence_hybrid_preflight import (
    RECIPE_PATH,
    SentenceHybridPopulationRow,
    _affordable_prefix,
    _band_for_basis_points,
    _cost_upper_bound,
    _packet_population_set_hash,
    _reservation_sum,
    _stratify_and_select,
)


def _row(index: int, packet_bytes: int) -> SentenceHybridPopulationRow:
    parent_id = f"parent:{index:03d}"
    item = CanaryPacket(
        packet={"parent_id": parent_id},
        context=SemanticValidationContext.from_owner_registries(
            parent_id=parent_id,
            claims=(ClaimScope(f"evidence:{index:03d}", parent_id),),
        ),
        parent_id=parent_id,
        doc_id=f"doc:{index:03d}",
        entity_count=1,
        source_child_count=1,
    )
    return SentenceHybridPopulationRow(
        planned=PlannedPacket(
            item=item,
            ordinal=index,
            job_id=f"job:{index:03d}",
            cache_key=f"cache:{index:03d}",
            input_hash=f"input:{index:03d}",
            packet_bytes=packet_bytes,
        ),
        packet_hash=f"sha256:{index:064x}",
        source_sentence_count=2,
        mapped_sentence_count=1,
        context_only_sentence_count=1,
    )


def _route() -> dict:
    return {
        "parameters": {"max_tokens": 8192},
        "price": {
            "uncached_input_usd": 0.75,
            "output_usd": 2.95,
            "price_unit_tokens": 1_000_000,
        },
    }


def test_sentence_hybrid_recipe_is_frozen_and_requires_long_stratum() -> None:
    recipe = load_sentence_hybrid_canary_selection_recipe(RECIPE_PATH)

    assert recipe.target_count == 10
    assert recipe.packet_schema_version == "semantic_parent_packet.sentence_hybrid.v3"
    assert [band.selection_count for band in recipe.bands] == [2, 2, 2, 2, 2]
    assert recipe.long_packet_threshold_bytes_exclusive == 20_000
    assert recipe.minimum_long_packet_selection_count == 1
    assert recipe.unique_document_across_selection is True
    assert namespace_hash("registry", recipe.model_dump(mode="python")).startswith(
        "sha256:"
    )


def test_sentence_hybrid_band_boundaries_close_exactly_once() -> None:
    recipe = load_sentence_hybrid_canary_selection_recipe(RECIPE_PATH)

    assert _band_for_basis_points(recipe, 0).band_id == "q00_q25"
    assert _band_for_basis_points(recipe, 2499).band_id == "q00_q25"
    assert _band_for_basis_points(recipe, 2500).band_id == "q25_q50"
    assert _band_for_basis_points(recipe, 8999).band_id == "q75_q90"
    assert _band_for_basis_points(recipe, 9000).band_id == "top_decile_q90_q100"
    assert _band_for_basis_points(recipe, 9999).band_id == "top_decile_q90_q100"


def test_selection_is_deterministic_unique_stratified_and_reserves_long_packet() -> (
    None
):
    recipe = load_sentence_hybrid_canary_selection_recipe(RECIPE_PATH)
    recipe_hash = namespace_hash("registry", recipe.model_dump(mode="python"))
    population = [_row(index, 10_000 + index) for index in range(100)]
    population[-1] = _row(99, 25_000)

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
    assert sum(item.row.packet_bytes > 20_000 for item in first) >= 1
    reserved = [item for item in first if item.long_packet_reserved]
    assert len(reserved) == 1
    assert reserved[0].row.parent_id == "parent:099"


def test_two_attempt_authority_matches_sum_reservation_boundary() -> None:
    rows = [_row(0, 10_000), _row(1, 20_000)]
    route = _route()

    batch = _cost_upper_bound(rows, route=route)
    reservations = _reservation_sum(rows, route=route)

    expected = Decimal("0.15583216")
    assert batch == expected
    assert reservations == expected


def test_affordable_prefix_stops_before_reservation_breach() -> None:
    rows = [_row(0, 10_000), _row(1, 20_000), _row(2, 30_000)]
    route = _route()
    first_two = _reservation_sum(rows[:2], route=route)

    exact = _affordable_prefix(
        rows,
        route=route,
        remaining_umbrella_usd=first_two,
    )
    inside = _affordable_prefix(
        rows,
        route=route,
        remaining_umbrella_usd=first_two - Decimal("0.00000001"),
    )

    assert exact["affordable_prefix_count"] == 2
    assert exact["remaining_after_prefix_usd"] == "0E-8"
    assert inside["affordable_prefix_count"] == 1


def test_packet_population_set_hash_uses_raw_digest_recipe() -> None:
    population = [_row(0, 10_000), _row(1, 20_000)]

    assert _packet_population_set_hash(population) == namespace_hash(
        "input-set",
        frozenset({f"{0:064x}", f"{1:064x}"}),
    )
