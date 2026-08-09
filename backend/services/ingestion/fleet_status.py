"""Deterministic worker-fleet observability (owner-ordered, 2026-08-09).

The extraction ENGINE has a glass control plane (/api/health/engine: route,
pins, qualification, audit trail). The worker FLEET did not — an agent asking
"what is each worker doing right now?" needed raw Mongo queries, which reads
as a black box. This module is the missing pane: one call returns every live
lane owner (worker identity, heartbeat freshness), every running item (file,
stage, heartbeat), per-corpus queue depths, recent throughput, and the engine
route — all from durable state, no process introspection, no PID namespaces.

Worker identity: lane owners are ``batch:<batch_id>:<container_id>:<pid>``.
The container_id is the DOCKER CONTAINER's hostname — host PIDs are
meaningless here (operate containers with ``docker exec``/``docker top``,
never with host PIDs; inside its namespace a worker's main process is PID 1).

Fail-safe: every section degrades to a labeled error string, never raises.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from services.ingestion.job_leases import DEFAULT_LANE_ADOPT_STALE_SECONDS


def _age_seconds(now: datetime, value: Any) -> float | None:
    if not isinstance(value, datetime):
        return None
    return round(max(0.0, (now - value).total_seconds()), 1)


def _parse_owner(owner: str) -> dict[str, Any]:
    parts = owner.split(":")
    if len(parts) >= 4 and parts[0] == "batch":
        return {
            "kind": "batch_runner",
            "batch_id": parts[1],
            "container_id": parts[-2],
            "pid": parts[-1],
        }
    if len(parts) >= 2:
        return {"kind": parts[0], "detail": ":".join(parts[1:])}
    return {"kind": "unknown"}


async def fleet_status(db: Any) -> dict[str, Any]:
    now = datetime.utcnow()
    out: dict[str, Any] = {
        "schema_version": "polymath.fleet_status.v1",
        "generated_at": now.isoformat() + "Z",
    }

    corpus_names: dict[str, str] = {}
    try:
        async for c in db["corpora"].find({}, {"corpus_id": 1, "name": 1}):
            corpus_names[str(c.get("corpus_id"))] = str(c.get("name") or "?")
    except Exception as exc:  # noqa: BLE001
        out["corpora_error"] = f"{type(exc).__name__}: {exc}"[:160]

    # Live lane owners — one entry per (owner, corpus): the worker roster.
    workers: dict[tuple, dict[str, Any]] = {}
    try:
        async for l in db["ingest_lane_leases"].find(
            {"lease_until": {"$gt": now}},
            {"owner": 1, "lane": 1, "corpus_id": 1, "updated_at": 1},
        ):
            owner = str(l.get("owner") or "")
            corpus_id = str(l.get("corpus_id") or "")
            key = (owner, corpus_id)
            row = workers.setdefault(
                key,
                {
                    "owner": owner,
                    **_parse_owner(owner),
                    "corpus_id": corpus_id,
                    "corpus_name": corpus_names.get(corpus_id, "?"),
                    "lanes": [],
                    "heartbeat_seconds": None,
                },
            )
            row["lanes"].append(str(l.get("lane")))
            beat = _age_seconds(now, l.get("updated_at"))
            if beat is not None and (
                row["heartbeat_seconds"] is None or beat < row["heartbeat_seconds"]
            ):
                row["heartbeat_seconds"] = beat
        for row in workers.values():
            row["lanes"] = sorted(row["lanes"])
            beat = row["heartbeat_seconds"]
            # Same rule the scheduler uses: fresh beat = live owner; stale
            # beat = dead (or event-loop-blocked) — its lanes are adoptable.
            row["live"] = bool(
                beat is not None and beat < float(DEFAULT_LANE_ADOPT_STALE_SECONDS)
            )
        out["workers"] = sorted(
            workers.values(), key=lambda r: (not r["live"], r["corpus_name"])
        )
    except Exception as exc:  # noqa: BLE001
        out["workers_error"] = f"{type(exc).__name__}: {exc}"[:160]

    # Every running item, with heartbeat freshness — "what is being worked".
    try:
        running = []
        async for it in db["ingest_batch_items"].find(
            {"status": "running"},
            {
                "filename": 1,
                "corpus_id": 1,
                "stage": 1,
                "phase": 1,
                "attempts": 1,
                "last_heartbeat_at": 1,
                "started_at": 1,
            },
        ).limit(50):
            cid = str(it.get("corpus_id") or "")
            running.append(
                {
                    "filename": str(it.get("filename") or "?"),
                    "corpus_name": corpus_names.get(cid, "?"),
                    "stage": it.get("stage"),
                    "phase": it.get("phase"),
                    "attempts": it.get("attempts"),
                    "heartbeat_seconds": _age_seconds(now, it.get("last_heartbeat_at")),
                    "running_minutes": (
                        round((now - it["started_at"]).total_seconds() / 60, 1)
                        if isinstance(it.get("started_at"), datetime)
                        else None
                    ),
                }
            )
        out["running_items"] = running
    except Exception as exc:  # noqa: BLE001
        out["running_items_error"] = f"{type(exc).__name__}: {exc}"[:160]

    # Per-corpus queue depths.
    try:
        queues: dict[str, dict[str, int]] = {}
        async for row in db["ingest_batch_items"].aggregate(
            [{"$group": {"_id": {"c": "$corpus_id", "s": "$status"}, "n": {"$sum": 1}}}]
        ):
            cid = str((row.get("_id") or {}).get("c") or "")
            status = str((row.get("_id") or {}).get("s") or "?")
            name = corpus_names.get(cid, cid[:12] or "?")
            queues.setdefault(name, {})[status] = int(row.get("n") or 0)
        for counts in queues.values():
            counts["total"] = sum(counts.values())
        out["queues"] = queues
    except Exception as exc:  # noqa: BLE001
        out["queues_error"] = f"{type(exc).__name__}: {exc}"[:160]

    # Throughput: measured completions, not projections.
    try:
        throughput: dict[str, Any] = {}
        for label, delta in (("last_hour", 1), ("last_24h", 24)):
            throughput[f"done_{label}"] = await db["ingest_batch_items"].count_documents(
                {
                    "status": "done",
                    "completed_at": {"$gte": now - timedelta(hours=delta)},
                }
            )
        recent = []
        async for it in (
            db["ingest_batch_items"]
            .find(
                {"status": "done"},
                {"filename": 1, "started_at": 1, "completed_at": 1},
            )
            .sort("completed_at", -1)
            .limit(5)
        ):
            minutes = None
            if isinstance(it.get("completed_at"), datetime) and isinstance(
                it.get("started_at"), datetime
            ):
                minutes = round(
                    (it["completed_at"] - it["started_at"]).total_seconds() / 60, 1
                )
            recent.append(
                {"filename": str(it.get("filename") or "?"), "minutes": minutes}
            )
        throughput["recent_completions"] = recent
        out["throughput"] = throughput
    except Exception as exc:  # noqa: BLE001
        out["throughput_error"] = f"{type(exc).__name__}: {exc}"[:160]

    # Engine route summary (no live probe — /api/health/engine does that).
    try:
        from services.extraction.engine_routing import describe_route

        route = describe_route()
        out["engine"] = {
            "source": route.get("source"),
            "sidecar_url": route.get("sidecar_url"),
            "mode": route.get("mode"),
            "expected_release": route.get("expected_release"),
        }
    except Exception as exc:  # noqa: BLE001
        out["engine_error"] = f"{type(exc).__name__}: {exc}"[:160]

    return out
