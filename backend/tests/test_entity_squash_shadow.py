from services.graph.entity_dedup.dryrun import BOTH_HIGH_MENTIONS
from services.graph.entity_dedup.squash_shadow import (
    build_shadow_report,
    shadow_entity_id,
)


def _entity(
    entity_id: str,
    normalized_name: str,
    entity_type: str = "Concept",
    *,
    mentions: int = 0,
    relations: int = 0,
    facts: int = 0,
) -> dict:
    return {
        "entity_id": entity_id,
        "normalized_name": normalized_name,
        "canonical_name": normalized_name,
        "display_name": normalized_name,
        "primary_entity_type": entity_type,
        "canonical_family": "",
        "mentions": mentions,
        "relations": relations,
        "facts": facts,
    }


def test_shadow_entity_id_collapses_space_underscore_and_hyphen() -> None:
    assert shadow_entity_id("flame_audio") == "entity:flameaudio"
    assert shadow_entity_id("flame audio") == "entity:flameaudio"
    assert shadow_entity_id("flame-audio") == "entity:flameaudio"


def test_shadow_report_classifies_collision_review_lanes() -> None:
    live_entities = [
        _entity("entity:flame_audio", "flame_audio", "Software", mentions=4),
        _entity("entity:flameaudio", "flameaudio", "Software", mentions=7),
        _entity("entity:video_games", "video games", "Concept", mentions=3),
        _entity("entity:videogames", "videogames", "Product", mentions=2),
        _entity(
            "entity:note_book",
            "note book",
            "Concept",
            mentions=BOTH_HIGH_MENTIONS,
        ),
        _entity(
            "entity:notebook",
            "notebook",
            "Concept",
            mentions=BOTH_HIGH_MENTIONS + 1,
        ),
    ]

    report = build_shadow_report(live_entities, [], examples=20)
    groups = {group["target_id"]: group for group in report["top_collision_groups"]}

    assert report["mutated_graph"] is False
    assert report["stats"]["collision_groups"] == 3
    assert report["stats"]["collision_groups_auto_same_type"] == 1
    assert report["stats"]["collision_groups_review_cross_type"] == 1
    assert report["stats"]["collision_groups_review_high_mention"] == 1
    assert groups["entity:flameaudio"]["decision"] == "auto_same_type"
    assert groups["entity:videogames"]["decision"] == "review_cross_type"
    assert groups["entity:notebook"]["decision"] == "review_high_mention"


def test_shadow_report_flags_tombstone_target_conflicts() -> None:
    live_entities = [
        _entity("entity:legacy_space", "legacy space", mentions=3),
        _entity("entity:legacykey_alias", "legacykey alias"),
        _entity("entity:legacy-keyalias", "legacykeyalias"),
    ]
    tombstones = [
        {
            "original": "entity:legacyspace",
            "survivor": "entity:legacy-space",
            "tombstone_id": "entity:legacyspace",
        },
        {
            "original": "entity:legacykeyalias",
            "survivor": "entity:legacy-key-alias",
            "tombstone_id": "entity:legacykeyalias",
        },
    ]

    report = build_shadow_report(live_entities, tombstones, examples=20)
    groups = {group["target_id"]: group for group in report["top_collision_groups"]}

    assert report["stats"]["entities_targeting_tombstoned_originals"] == 3
    assert report["stats"]["shadow_targets_that_are_tombstoned_originals"] == 2
    assert groups["entity:legacykeyalias"]["decision"] == "review_tombstone_conflict"
    assert report["top_tombstone_target_conflicts"][0]["target_id"] == "entity:legacyspace"
