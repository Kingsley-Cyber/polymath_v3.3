"""polymath_extraction_engine — the MCP execution-plane window."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# Reuse the auth stubs installed by the ingest-tools suite when running solo.
from tests.test_polymath_mcp_ingest_tools import _install_auth_stubs_if_missing

_install_auth_stubs_if_missing()

from polymath_mcp import tools as mcp_tools  # noqa: E402


class _FakeItems:
    def __init__(self, running=2, queued=5):
        self._counts = {"running": running, "queued": queued}

    async def count_documents(self, query):
        return self._counts.get(query.get("status"), 0)


class _FakeDb(dict):
    def __init__(self, items):
        super().__init__()
        self["ingest_batch_items"] = items

    def __getitem__(self, key):
        return super().__getitem__(key)


@pytest.mark.asyncio
async def test_engine_tool_reports_pins_load_and_seam(monkeypatch):
    monkeypatch.setenv("RELEX_SIDECAR_URL", "http://gpu-box:8737")
    monkeypatch.setenv("RELEX_EXPECT_RELEASE", "relex-large-cuda-sidecar-v1")

    health = {
        "release": "relex-large-cuda-sidecar-v1",
        "contract": "relex-infer-v1",
        "device": "cuda",
        "model": {"id": "knowledgator/gliner-relex-large-v1.0"},
        "thresholds": {"entity": 0.3, "relation": 0.3},
    }

    class _Resp:
        def read(self):
            import json
            return json.dumps(health).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake_db = _FakeDb(_FakeItems())
    with patch("urllib.request.urlopen", return_value=_Resp()), \
         patch.object(type(mcp_tools.ingestion_service), "db",
                      property(lambda self: fake_db)):
        out = await mcp_tools.polymath_extraction_engine()

    assert out["contract_version"] == "polymath.extraction_engine.v1"
    assert out["reachable"] is True
    assert out["release"] == "relex-large-cuda-sidecar-v1"
    assert out["device"] == "cuda"
    assert out["release_pin_enforced"] is True
    assert out["busy"] is True
    assert out["engine_load"] == {"running_items": 2, "queued_items": 5}
    assert "RELEX_SIDECAR_URL" in out["deployment_seam"]["switch"]


@pytest.mark.asyncio
async def test_engine_tool_unreachable_is_a_datum_not_an_error(monkeypatch):
    monkeypatch.setenv("RELEX_SIDECAR_URL", "http://nowhere:9")
    monkeypatch.delenv("RELEX_EXPECT_RELEASE", raising=False)

    fake_db = _FakeDb(_FakeItems(0, 0))
    with patch("urllib.request.urlopen", side_effect=OSError("no route")), \
         patch.object(type(mcp_tools.ingestion_service), "db",
                      property(lambda self: fake_db)):
        out = await mcp_tools.polymath_extraction_engine()

    assert out["reachable"] is False
    assert out["busy"] is False
    assert out["release_pin_enforced"] is False


def test_engine_tool_is_registered():
    assert mcp_tools.polymath_extraction_engine in mcp_tools.ALL_TOOLS


@pytest.mark.asyncio
async def test_switch_refuses_pin_mismatch(monkeypatch, system_user=None):
    from tests.test_polymath_mcp_ingest_tools import _install_auth_stubs_if_missing
    _install_auth_stubs_if_missing()

    health = {
        "release": "some-other-build",
        "contract": "relex-infer-v1",
        "device": "cuda",
        "model": {"id": "someone/else-model", "revision": "x", "weights_sha256": "y"},
    }

    class _Resp:
        def read(self):
            import json
            return json.dumps(health).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    from polymath_mcp.auth import SYSTEM_USER_ID, _current_user_id
    token = _current_user_id.set(SYSTEM_USER_ID)
    try:
        with patch("urllib.request.urlopen", return_value=_Resp()):
            out = await mcp_tools.polymath_set_extraction_engine(
                "http://gpu-box:8737", mode="qualification")
    finally:
        _current_user_id.reset(token)
    assert out["status"] == "refused"
    assert out["routed"] is False
    assert any("id:" in m for m in out["mismatches"])


@pytest.mark.asyncio
async def test_switch_refuses_unqualified_production_but_allows_qualification(monkeypatch):
    from services.extraction import engine_routing

    health = {
        "release": "relex-large-cuda-sidecar-v1",
        "contract": "relex-infer-v1",
        "device": "cuda",
        "model": dict(engine_routing.PINNED_MODEL),
    }

    class _Resp:
        def read(self):
            import json
            return json.dumps(health).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Routing:
        def __init__(self):
            self.rows = {}

        async def find_one(self, q):
            return self.rows.get(q.get("_id"))

        async def update_one(self, q, update, upsert=False):
            row = self.rows.setdefault(q["_id"], {"_id": q["_id"]})
            row.update(update.get("$set") or {})
            return SimpleNamespace(modified_count=1)

    class _Db(dict):
        def __missing__(self, key):
            value = _Routing()
            self[key] = value
            return value

    fake_db = _Db()
    from polymath_mcp.auth import SYSTEM_USER_ID, _current_user_id
    token = _current_user_id.set(SYSTEM_USER_ID)
    try:
        with patch("urllib.request.urlopen", return_value=_Resp()), \
             patch.object(type(mcp_tools.ingestion_service), "db",
                          property(lambda self: fake_db)):
            prod = await mcp_tools.polymath_set_extraction_engine(
                "http://gpu-box:8737", mode="production")
            qual = await mcp_tools.polymath_set_extraction_engine(
                "http://gpu-box:8737", mode="qualification")
    finally:
        _current_user_id.reset(token)
    assert prod["status"] == "refused" and "not production-qualified" in prod["reason"]
    assert qual["status"] == "routed" and qual["route"]["mode"] == "qualification"
    assert qual["route"]["expected_release"] == "relex-large-cuda-sidecar-v1"
