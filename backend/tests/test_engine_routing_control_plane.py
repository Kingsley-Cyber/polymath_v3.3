"""Control-plane hardening: deterministic routing, audit trail, safe fallbacks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.extraction import engine_routing


class _Coll:
    def __init__(self):
        self.doc = None

    async def find_one(self, q):
        return dict(self.doc) if self.doc else None

    async def update_one(self, q, update, upsert=False):
        if self.doc is None and upsert:
            self.doc = {"_id": q["_id"]}
        for k, v in (update.get("$set") or {}).items():
            self.doc[k] = v
        for k, spec in (update.get("$push") or {}).items():
            arr = self.doc.setdefault(k, [])
            if isinstance(spec, dict) and "$each" in spec:
                arr.extend(spec["$each"])
                cap = spec.get("$slice")
                if cap and cap < 0:
                    self.doc[k] = arr[cap:]
            else:
                arr.append(spec)
        for k, v in (update.get("$addToSet") or {}).items():
            arr = self.doc.setdefault(k, [])
            if v not in arr:
                arr.append(v)
        return SimpleNamespace(modified_count=1)

    async def delete_one(self, q):
        self.doc = None
        return SimpleNamespace(deleted_count=1)


class _Db(dict):
    def __missing__(self, key):
        v = _Coll(); self[key] = v; return v


@pytest.mark.asyncio
async def test_write_route_appends_capped_audit_history():
    db = _Db()
    for i in range(55):
        await engine_routing.write_route(
            db, sidecar_url=f"http://engine-{i}:8737",
            expected_release="relex-large-cuda-sidecar-v1",
            mode="production", updated_by=f"agent-{i}", note=f"flip {i}")
    doc = db[engine_routing.ROUTING_COLLECTION].doc
    assert doc["schema_version"] == "polymath.engine_routing.v1"
    hist = doc["route_history"]
    assert len(hist) == 50  # capped
    assert hist[-1]["sidecar_url"] == "http://engine-54:8737"
    assert hist[-1]["replaced_url"] == "http://engine-53:8737"  # reconstructible


@pytest.mark.asyncio
async def test_write_route_preserves_qualifications_and_wake():
    db = _Db()
    await engine_routing.record_qualified_release(
        db, release="relex-large-cuda-sidecar-v1", evidence="battery pass")
    db[engine_routing.ROUTING_COLLECTION].doc["wake"] = {"mac_address": "AA:BB"}
    await engine_routing.write_route(
        db, sidecar_url="http://x:8737", expected_release="r", mode="production",
        updated_by="ops")
    doc = db[engine_routing.ROUTING_COLLECTION].doc
    assert "relex-large-cuda-sidecar-v1" in doc["qualified_releases"]
    assert doc["wake"] == {"mac_address": "AA:BB"}


def test_describe_route_fail_safe_states(monkeypatch):
    # no doc, no env -> default source, never raises
    monkeypatch.setattr(engine_routing, "active_route", lambda **kw: None)
    monkeypatch.delenv("RELEX_SIDECAR_URL", raising=False)
    out = engine_routing.describe_route()
    assert out["source"] == "default" and out["sidecar_url"] is None
    assert out["qualified_releases"] == list(engine_routing.BASELINE_QUALIFIED_RELEASES)
    # env layer
    monkeypatch.setenv("RELEX_SIDECAR_URL", "http://mps:8737")
    assert engine_routing.describe_route()["source"] == "env"
    # routing doc wins over env
    monkeypatch.setattr(engine_routing, "active_route", lambda **kw: {
        "sidecar_url": "http://gpu:8737", "mode": "production",
        "expected_release": "relex-large-cuda-sidecar-v1",
        "wake": {"mac_address": "AA:BB"},
        "route_history": [{"at": "t", "by": "ops", "mode": "production",
                           "note": "", "sidecar_url": "http://gpu:8737",
                           "replaced_url": None}],
    })
    out = engine_routing.describe_route()
    assert out["source"] == "routing_doc"
    assert out["wake_configured"] is True
    assert out["last_change"]["by"] == "ops"
