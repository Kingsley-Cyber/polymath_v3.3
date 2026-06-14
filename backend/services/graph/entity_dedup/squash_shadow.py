"""Part 2A shadow report: future squash-canonical entity ids.

Read-only. Scans live Entity nodes, computes the id that `entity_id_from_name()`
would emit if it were changed to the squash form, and reports collision/risk
classes before any migration or writer change.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_settings
from services.graph.entity_dedup.dryrun import BOTH_HIGH_MENTIONS, squash_key

ENTITY_PREFIX = "entity"


def _settings_attr(s: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = getattr(s, name, None)
        if value:
            return value
    return default


def shadow_entity_id(normalized_name: str) -> str:
    key = squash_key(normalized_name)
    return f"{ENTITY_PREFIX}:{key}" if key else ""


def _compact_entity(row: dict[str, Any], *, include_counts: bool = True) -> dict[str, Any]:
    out = {
        "entity_id": row.get("entity_id", ""),
        "target_id": row.get("target_id", ""),
        "canonical_name": row.get("canonical_name", ""),
        "normalized_name": row.get("normalized_name", ""),
        "display_name": row.get("display_name", ""),
        "primary_entity_type": row.get("primary_entity_type", ""),
        "canonical_family": row.get("canonical_family", ""),
    }
    if include_counts:
        out.update({
            "mentions": int(row.get("mentions") or 0),
            "relations": int(row.get("relations") or 0),
            "facts": int(row.get("facts") or 0),
        })
    return out


def _top_entities(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    ranked = sorted(
        rows,
        key=lambda r: (
            int(r.get("mentions") or 0),
            int(r.get("relations") or 0),
            int(r.get("facts") or 0),
            str(r.get("entity_id") or ""),
        ),
        reverse=True,
    )
    return [_compact_entity(row) for row in ranked[:limit]]


def build_shadow_report(
    live_entities: list[dict[str, Any]],
    tombstones: list[dict[str, Any]],
    *,
    examples: int = 12,
) -> dict[str, Any]:
    tombstone_by_original = {
        str(row.get("original") or ""): str(row.get("survivor") or "")
        for row in tombstones
        if row.get("original") and row.get("survivor")
    }
    tombstone_survivors = {sur for sur in tombstone_by_original.values() if sur}

    rows: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []
    for raw in live_entities:
        normalized = str(
            raw.get("normalized_name")
            or raw.get("canonical_name")
            or raw.get("display_name")
            or ""
        ).strip()
        target_id = shadow_entity_id(normalized)
        row = dict(raw)
        row["normalized_name"] = normalized
        row["target_id"] = target_id
        if target_id:
            rows.append(row)
        else:
            invalid_rows.append(row)

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["target_id"]].append(row)

    collision_groups = {target: group for target, group in groups.items() if len(group) > 1}
    unique_rows = [group[0] for group in groups.values() if len(group) == 1]
    unchanged_rows = [row for row in rows if row.get("entity_id") == row.get("target_id")]
    changed_rows = [row for row in rows if row.get("entity_id") != row.get("target_id")]
    id_change_no_collision = [
        row for row in unique_rows if row.get("entity_id") != row.get("target_id")
    ]

    group_reports: list[dict[str, Any]] = []
    type_counter: Counter[str] = Counter()
    for target_id, group in collision_groups.items():
        types = sorted({str(row.get("primary_entity_type") or "") for row in group})
        populated_types = [t for t in types if t]
        high_members = [
            row for row in group
            if int(row.get("mentions") or 0) >= BOTH_HIGH_MENTIONS
        ]
        target_is_tombstoned_original = target_id in tombstone_by_original
        target_redirect_survivor = tombstone_by_original.get(target_id, "")
        target_is_existing_survivor = target_id in tombstone_survivors
        cross_type = len(set(populated_types)) > 1
        high_mention_review = len(high_members) >= 2
        tombstone_conflict = bool(target_is_tombstoned_original)
        decision = "auto_same_type"
        if cross_type:
            decision = "review_cross_type"
        if high_mention_review:
            decision = "review_high_mention"
        if tombstone_conflict:
            decision = "review_tombstone_conflict"
        type_counter[decision] += 1
        group_reports.append({
            "target_id": target_id,
            "squash_key": target_id.split(":", 1)[1] if ":" in target_id else target_id,
            "size": len(group),
            "decision": decision,
            "types": populated_types,
            "changed_members": sum(1 for row in group if row.get("entity_id") != target_id),
            "unchanged_members": sum(1 for row in group if row.get("entity_id") == target_id),
            "mentions_total": sum(int(row.get("mentions") or 0) for row in group),
            "relations_total": sum(int(row.get("relations") or 0) for row in group),
            "facts_total": sum(int(row.get("facts") or 0) for row in group),
            "high_mention_members": len(high_members),
            "target_is_tombstoned_original": target_is_tombstoned_original,
            "target_redirect_survivor": target_redirect_survivor,
            "target_is_existing_survivor": target_is_existing_survivor,
            "examples": _top_entities(group, examples),
        })

    group_reports.sort(
        key=lambda g: (
            g["decision"] != "review_tombstone_conflict",
            g["decision"] != "review_cross_type",
            g["decision"] != "review_high_mention",
            -g["size"],
            -g["mentions_total"],
            g["target_id"],
        )
    )

    tombstone_target_conflicts = [
        _compact_entity(row)
        for row in changed_rows
        if row.get("target_id") in tombstone_by_original
    ]
    tombstone_target_conflicts.sort(
        key=lambda r: (r["mentions"], r["relations"], r["facts"], r["entity_id"]),
        reverse=True,
    )

    stats = {
        "live_entities_scanned": len(live_entities),
        "valid_shadow_ids": len(rows),
        "invalid_shadow_ids": len(invalid_rows),
        "unchanged_entity_ids": len(unchanged_rows),
        "would_change_entity_ids": len(changed_rows),
        "id_change_no_collision": len(id_change_no_collision),
        "collision_groups": len(group_reports),
        "collision_entities": sum(group["size"] for group in group_reports),
        "collision_groups_auto_same_type": type_counter["auto_same_type"],
        "collision_groups_review_cross_type": type_counter["review_cross_type"],
        "collision_groups_review_high_mention": type_counter["review_high_mention"],
        "collision_groups_review_tombstone_conflict": type_counter["review_tombstone_conflict"],
        "tombstones_scanned": len(tombstones),
        "shadow_targets_that_are_tombstoned_originals": len({
            row.get("target_id") for row in changed_rows
            if row.get("target_id") in tombstone_by_original
        }),
        "entities_targeting_tombstoned_originals": len(tombstone_target_conflicts),
    }

    return {
        "kind": "entity_squash_shadow_report",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mutated_graph": False,
        "squash_rule": r"entity_id = 'entity:' + re.sub(r'[\s_\-]+', '', normalized_name.lower())",
        "stats": stats,
        "top_collision_groups": group_reports[:examples],
        "top_id_change_no_collision": _top_entities(id_change_no_collision, examples),
        "top_tombstone_target_conflicts": tombstone_target_conflicts[:examples],
        "invalid_shadow_id_examples": [_compact_entity(row, include_counts=False) for row in invalid_rows[:examples]],
    }


async def _load_live_entities(session) -> list[dict[str, Any]]:
    query = """
    MATCH (e:Entity)
    WHERE coalesce(e.tombstone, false) = false
      AND e.entity_id IS NOT NULL
    RETURN e.entity_id AS entity_id,
           coalesce(e.normalized_name, e.canonical_name, e.display_name, '') AS normalized_name,
           coalesce(e.canonical_name, e.normalized_name, e.display_name, '') AS canonical_name,
           coalesce(e.display_name, e.canonical_name, e.normalized_name, '') AS display_name,
           coalesce(e.primary_entity_type, e.entity_type, '') AS primary_entity_type,
           coalesce(e.canonical_family, '') AS canonical_family
    """
    rows: list[dict[str, Any]] = []
    result = await session.run(query)
    async for row in result:
        rows.append(dict(row))
    return rows


async def _load_tombstones(session) -> list[dict[str, Any]]:
    query = """
    MATCH (t:Entity)
    WHERE coalesce(t.tombstone, false) = true
    RETURN t.original_entity_id AS original,
           t.merged_into AS survivor,
           t.entity_id AS tombstone_id
    """
    rows: list[dict[str, Any]] = []
    result = await session.run(query)
    async for row in result:
        rows.append(dict(row))
    return rows


async def _load_counts(session, rows: list[dict[str, Any]], *, batch_size: int = 5000) -> None:
    ids = [str(row.get("entity_id") or "") for row in rows if row.get("entity_id")]
    by_id = {str(row.get("entity_id")): row for row in rows if row.get("entity_id")}
    query = """
    UNWIND $ids AS id
    MATCH (e:Entity {entity_id: id})
    RETURN e.entity_id AS entity_id,
           size([(e)<-[:MENTIONS]-() | 1]) AS mentions,
           size([(e)-[:RELATES_TO]-() | 1]) + size([(e)<-[:RELATES_TO]-() | 1]) AS relations,
           size([(e)-[:HAS_FACT]->() | 1]) AS facts
    """
    for i in range(0, len(ids), batch_size):
        result = await session.run(query, ids=ids[i:i + batch_size])
        async for row in result:
            target = by_id.get(row["entity_id"])
            if target is None:
                continue
            target["mentions"] = int(row["mentions"] or 0)
            target["relations"] = int(row["relations"] or 0)
            target["facts"] = int(row["facts"] or 0)


async def run_shadow_report(*, examples: int = 12) -> dict[str, Any]:
    settings = get_settings()
    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(
        _settings_attr(settings, "NEO4J_URI", "NEO4J_URL"),
        auth=(
            _settings_attr(settings, "NEO4J_USER", "NEO4J_USERNAME", default="neo4j"),
            _settings_attr(settings, "NEO4J_PASSWORD", "NEO4J_PASS"),
        ),
    )
    try:
        async with driver.session() as session:
            live_entities = await _load_live_entities(session)
            tombstones = await _load_tombstones(session)
            tombstone_originals = {
                str(row.get("original") or "")
                for row in tombstones
                if row.get("original")
            }
            target_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in live_entities:
                normalized = str(
                    row.get("normalized_name")
                    or row.get("canonical_name")
                    or row.get("display_name")
                    or ""
                ).strip()
                target_id = shadow_entity_id(normalized)
                if target_id:
                    row["normalized_name"] = normalized
                    row["target_id"] = target_id
                    target_groups[target_id].append(row)

            count_ids: set[str] = set()
            for target_id, group in target_groups.items():
                if len(group) > 1 or target_id in tombstone_originals:
                    count_ids.update(
                        str(row.get("entity_id") or "")
                        for row in group
                        if row.get("entity_id")
                    )
            rows_for_counts = [
                row for row in live_entities
                if row.get("entity_id") in count_ids
            ]
            await _load_counts(session, rows_for_counts)
            return build_shadow_report(live_entities, tombstones, examples=examples)
    finally:
        await driver.close()


def _print_report(report: dict[str, Any]) -> None:
    stats = report["stats"]
    print("\nENTITY SQUASH-ID SHADOW REPORT")
    print("=" * 72)
    print(f"mutated graph                      : {report['mutated_graph']}")
    print(f"live entities scanned              : {stats['live_entities_scanned']:,}")
    print(f"unchanged entity_ids               : {stats['unchanged_entity_ids']:,}")
    print(f"would change entity_ids            : {stats['would_change_entity_ids']:,}")
    print(f"id changes with no collision        : {stats['id_change_no_collision']:,}")
    print(f"collision groups                   : {stats['collision_groups']:,}")
    print(f"collision entities                 : {stats['collision_entities']:,}")
    print(f"  auto same-type groups            : {stats['collision_groups_auto_same_type']:,}")
    print(f"  review cross-type groups         : {stats['collision_groups_review_cross_type']:,}")
    print(f"  review high-mention groups       : {stats['collision_groups_review_high_mention']:,}")
    print(f"  review tombstone-conflict groups : {stats['collision_groups_review_tombstone_conflict']:,}")
    print(f"entities targeting tombstoned ids  : {stats['entities_targeting_tombstoned_originals']:,}")
    print("-" * 72)
    for group in report["top_collision_groups"]:
        print(
            f"{group['decision']:27} {group['target_id']} "
            f"size={group['size']} mentions={group['mentions_total']}"
        )
        for entity in group["examples"][:4]:
            print(
                f"  - {entity['entity_id']} | {entity['canonical_name']!r} "
                f"type={entity['primary_entity_type']} mentions={entity.get('mentions', 0)}"
            )
    if report["top_id_change_no_collision"]:
        print("\nTop no-collision id changes:")
        for entity in report["top_id_change_no_collision"][:8]:
            print(f"  - {entity['entity_id']} -> {entity['target_id']} | {entity['canonical_name']!r}")
    if report["top_tombstone_target_conflicts"]:
        print("\nTop tombstone-target conflicts:")
        for entity in report["top_tombstone_target_conflicts"][:8]:
            print(f"  - {entity['entity_id']} -> {entity['target_id']} | {entity['canonical_name']!r}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only squash-ID shadow report")
    parser.add_argument("--examples", type=int, default=12)
    parser.add_argument("--out", help="Optional JSON output path")
    args = parser.parse_args()

    report = asyncio.run(run_shadow_report(examples=args.examples))
    _print_report(report)
    if args.out:
        path = Path(args.out)
        path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
