"""Phase 7 — tombstone read-path resolution.

A merged entity's node is replaced by:
  (:Entity {entity_id:'tombstone:'+orig, original_entity_id:orig,
            merged_into:<survivor>, tombstone:true})
so a by-id lookup of the ORIGINAL id finds nothing. Fresh queries seed entity
ids from live traversal (survivors only — tombstones are edgeless islands), so
this matters mainly for STALE references: cached graph sessions, pre-merge
anchors, Qdrant payloads. resolve_entity_ids() maps any merged id to its
survivor so those follow through instead of silently dropping the entity.

Merges are single-level by design (a survivor is never itself a dup in the same
run), so one hop suffices; we loop-guard anyway in case of chained runs.
"""
from typing import Iterable


async def resolve_entity_ids(session, ids: Iterable[str], *, max_hops: int = 3) -> dict[str, str]:
    """Return {original_id: survivor_id} for any id that has been merged away.
    Ids that are still live (no tombstone) are omitted, so callers can do
    `mapping.get(i, i)`."""
    pending = [i for i in dict.fromkeys(ids) if i]
    if not pending:
        return {}
    mapping: dict[str, str] = {}
    q = ("UNWIND $ids AS i "
         "MATCH (t:Entity {entity_id: 'tombstone:' + i}) "
         "WHERE t.merged_into IS NOT NULL "
         "RETURN i AS orig, t.merged_into AS sur")
    for _ in range(max_hops):
        res = await session.run(q, ids=pending)
        hop: dict[str, str] = {}
        async for r in res:
            row = dict(r)
            orig = row.get("orig")
            sur = row.get("sur")
            if orig and sur:
                hop[orig] = sur
        if not hop:
            break
        # compose: anything that previously mapped to an id now-merged again
        for orig, sur in list(mapping.items()):
            if sur in hop:
                mapping[orig] = hop[sur]
        for orig, sur in hop.items():
            mapping[orig] = sur
        pending = list({sur for sur in hop.values()})
    return mapping


def redirect(ids: Iterable[str], mapping: dict[str, str]) -> list[str]:
    """Rewrite ids through the merged->survivor mapping, de-duplicated, order-
    preserving."""
    return list(dict.fromkeys(mapping.get(i, i) for i in ids))
