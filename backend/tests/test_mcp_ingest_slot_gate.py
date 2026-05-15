"""MCP ingest slot-gate tests — second half of the Bug #1 fix.

`routers/ingestion.py` was patched in commit 0a47b8f to acquire an
ingest slot BEFORE reading the file body so a 500-file simultaneous
upload couldn't materialize all body bytes in RAM just to be 429'd.

That fix only covered the HTTP multipart path. The MCP write surface
(`polymath_mcp.tools._ingest_bytes` used by `polymath_upload_document`
and `polymath_ingest_from_url`) called `ingestion_service.ingest()`
directly — completely bypassing the slot gate. An agent looping over
500 documents through MCP could trigger the exact same OOM /
worker-starvation pattern the HTTP fix prevented.

The fix:
  • Extract slot primitives to `services/ingestion/admission.py`
    (shared with HTTP).
  • Make `_ingest_bytes` acquire the slot before calling
    `ingestion_service.ingest()`, and release it in `finally`.
  • If the slot pool is saturated, raise `ValueError` with a
    "queue full" message (MCP doesn't have HTTP status codes, so
    we surface the rejection as the standard exception shape MCP
    tools use for refusal).

These tests pin that contract.
"""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest


# ── Auth-package stubs (mirror test_ingest_slot_ordering.py) ────────
def _install_stubs_if_missing() -> None:
    if "jose" not in sys.modules:
        try:
            import jose  # noqa: F401
        except ImportError:
            jose_mod = ModuleType("jose")

            class JWTError(Exception):
                pass

            class _Jwt:
                @staticmethod
                def encode(*_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

                @staticmethod
                def decode(*_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

            jose_mod.JWTError = JWTError
            jose_mod.jwt = _Jwt()
            sys.modules["jose"] = jose_mod

    if "passlib.context" not in sys.modules:
        try:
            import passlib.context  # noqa: F401
        except ImportError:
            passlib_mod = ModuleType("passlib")
            ctx_mod = ModuleType("passlib.context")

            class _CryptContext:
                def __init__(self, *a, **kw):
                    pass

                def hash(self, *_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

                def verify(self, *_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

            ctx_mod.CryptContext = _CryptContext
            passlib_mod.context = ctx_mod
            sys.modules["passlib"] = passlib_mod
            sys.modules["passlib.context"] = ctx_mod

    if "slowapi" not in sys.modules:
        try:
            import slowapi  # noqa: F401
        except ImportError:
            slowapi_mod = ModuleType("slowapi")
            util_mod = ModuleType("slowapi.util")

            class _Limiter:
                def __init__(self, *a, **kw):
                    pass

                def limit(self, *_a, **_kw):
                    def _decorator(fn):
                        return fn
                    return _decorator

            def _get_remote_address(_request):  # pragma: no cover
                return "0.0.0.0"

            slowapi_mod.Limiter = _Limiter
            util_mod.get_remote_address = _get_remote_address
            sys.modules["slowapi"] = slowapi_mod
            sys.modules["slowapi.util"] = util_mod


_install_stubs_if_missing()


from polymath_mcp import tools as mcp_tools  # noqa: E402
from services.ingestion import admission as _admission  # noqa: E402


# ── Fake IngestJobResponse so we don't have to import the real one
# (it lives in `services.ingestion.worker` which imports docling and
# other heavy modules — keeping the test light).
class _FakeJobResult:
    def __init__(self, doc_id: str = "doc-1") -> None:
        self.doc_id = doc_id
        self.job_id = "job-1"
        self.filename = "f.pdf"
        self.source_tier = "qdrant_only"


@pytest.fixture(autouse=True)
def reset_slot_counter():
    """Reset the slot counter between tests so they don't bleed state."""
    original_count = _admission._ingest_active_count
    original_limit = _admission.INGEST_ACTIVE_LIMIT
    _admission._ingest_active_count = 0
    yield
    _admission._ingest_active_count = original_count
    _admission.INGEST_ACTIVE_LIMIT = original_limit


@pytest.fixture
def mock_ingestion_service(monkeypatch):
    """Stub `ingestion_service._get_corpus_raw` and `.ingest` so the
    test never touches Mongo / Qdrant / Neo4j / docling."""
    # Minimal corpus row — the only fields _ingest_bytes reads are
    # default_ingestion_config and existence.
    fake_corpus = {
        "corpus_id": "corp-A",
        "default_ingestion_config": {},
    }
    monkeypatch.setattr(
        mcp_tools.ingestion_service,
        "_get_corpus_raw",
        AsyncMock(return_value=fake_corpus),
    )
    ingest_mock = AsyncMock(return_value=_FakeJobResult())
    monkeypatch.setattr(mcp_tools.ingestion_service, "ingest", ingest_mock)
    return ingest_mock


# ── Slot acquisition / release contract ─────────────────────────────


@pytest.mark.asyncio
async def test_successful_ingest_acquires_and_releases_slot(
    mock_ingestion_service,
):
    """The happy path: acquire → ingest → release. Net slot count
    returns to 0 after the call."""
    assert _admission._ingest_active_count == 0
    result = await mcp_tools._ingest_bytes(
        data=b"hello world",
        filename="x.txt",
        corpus_id="corp-A",
        user_id="user-A",
    )
    # Slot was released.
    assert _admission._ingest_active_count == 0
    assert result["status"] == "queued"
    assert result["doc_id"] == "doc-1"
    mock_ingestion_service.assert_awaited_once()


@pytest.mark.asyncio
async def test_ingest_exception_still_releases_slot(monkeypatch):
    """If `ingestion_service.ingest` raises, the slot MUST still get
    released — otherwise a single ingest failure burns a slot for the
    life of the process."""
    monkeypatch.setattr(
        mcp_tools.ingestion_service,
        "_get_corpus_raw",
        AsyncMock(return_value={"corpus_id": "corp-A", "default_ingestion_config": {}}),
    )
    monkeypatch.setattr(
        mcp_tools.ingestion_service,
        "ingest",
        AsyncMock(side_effect=RuntimeError("docling exploded")),
    )

    with pytest.raises(RuntimeError, match="docling exploded"):
        await mcp_tools._ingest_bytes(
            data=b"hello",
            filename="x.txt",
            corpus_id="corp-A",
            user_id="user-A",
        )
    # Slot released despite the exception.
    assert _admission._ingest_active_count == 0


@pytest.mark.asyncio
async def test_slot_full_rejects_with_value_error(mock_ingestion_service):
    """When the slot pool is saturated, _ingest_bytes raises ValueError
    BEFORE delegating to ingestion_service. The ingest() mock should
    never get called."""
    _admission.INGEST_ACTIVE_LIMIT = 2
    # Pre-saturate.
    assert await _admission.try_acquire_ingest_slot() is True
    assert await _admission.try_acquire_ingest_slot() is True
    assert _admission._ingest_active_count == 2

    with pytest.raises(ValueError, match="Ingest queue is full"):
        await mcp_tools._ingest_bytes(
            data=b"hello",
            filename="x.txt",
            corpus_id="corp-A",
            user_id="user-A",
        )
    # Mock was not invoked.
    mock_ingestion_service.assert_not_awaited()
    # Counter unchanged — we didn't accidentally consume a third slot.
    assert _admission._ingest_active_count == 2


# ── Early-validation paths must NOT touch the slot pool ─────────────


@pytest.mark.asyncio
async def test_empty_body_rejects_before_slot_acquire(mock_ingestion_service):
    """An empty body should error BEFORE touching the slot pool. If we
    accidentally consumed a slot here, an attacker spamming empty
    uploads could DoS the slot pool without paying any real upload
    cost."""
    assert _admission._ingest_active_count == 0
    with pytest.raises(ValueError, match="empty"):
        await mcp_tools._ingest_bytes(
            data=b"",
            filename="x.txt",
            corpus_id="corp-A",
            user_id="user-A",
        )
    # No slot consumed.
    assert _admission._ingest_active_count == 0
    mock_ingestion_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversize_body_rejects_before_slot_acquire(
    mock_ingestion_service, monkeypatch,
):
    """Same logic as the empty-body test but for the size cap. The
    MCP_INGEST_MAX_BYTES check must fire BEFORE the slot acquire so
    oversize submissions can't burn slots while being rejected."""
    # Force the cap to a small number for the test.
    settings = mcp_tools.get_settings()
    monkeypatch.setattr(settings, "MCP_INGEST_MAX_BYTES", 16, raising=False)

    assert _admission._ingest_active_count == 0
    with pytest.raises(ValueError, match="bytes"):
        await mcp_tools._ingest_bytes(
            data=b"x" * 100,
            filename="x.txt",
            corpus_id="corp-A",
            user_id="user-A",
        )
    # No slot consumed.
    assert _admission._ingest_active_count == 0
    mock_ingestion_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_corpus_rejects_before_slot_acquire(monkeypatch):
    """If the corpus lookup returns None (deleted / never existed),
    we should fail BEFORE consuming a slot. Otherwise a malicious
    user could probe random corpus_ids and burn slots on each miss."""
    monkeypatch.setattr(
        mcp_tools.ingestion_service,
        "_get_corpus_raw",
        AsyncMock(return_value=None),
    )
    ingest_mock = AsyncMock(return_value=_FakeJobResult())
    monkeypatch.setattr(mcp_tools.ingestion_service, "ingest", ingest_mock)

    with pytest.raises(ValueError, match="not found"):
        await mcp_tools._ingest_bytes(
            data=b"hello",
            filename="x.txt",
            corpus_id="bogus",
            user_id="user-A",
        )
    assert _admission._ingest_active_count == 0
    ingest_mock.assert_not_awaited()


# ── Burst: MCP shares the gate with HTTP ─────────────────────────────


@pytest.mark.asyncio
async def test_mcp_and_http_share_same_slot_pool():
    """The whole point of extracting admission.py — calls through
    routers.ingestion's slot primitives drain the SAME pool as calls
    through polymath_mcp.tools's slot primitives. Pre-acquiring via
    the router's helper saturates the gate for the MCP path too."""
    from routers import ingestion as ing
    _admission.INGEST_ACTIVE_LIMIT = 3

    # Take 3 slots via the router's re-export.
    assert await ing._try_acquire_ingest_slot() is True
    assert await ing._try_acquire_ingest_slot() is True
    assert await ing._try_acquire_ingest_slot() is True
    assert _admission._ingest_active_count == 3

    # MCP can't get a slot — proves the pool is shared.
    assert await _admission.try_acquire_ingest_slot() is False
    assert _admission._ingest_active_count == 3


# ── Source-pin: confirm the gate is in the right place ──────────────


def test_slot_acquire_is_before_ingest_call_in_mcp_source():
    """Source-pin: in `_ingest_bytes`, `_try_acquire_ingest_slot()`
    must appear BEFORE `ingestion_service.ingest(`. A future refactor
    that moves the slot acquire AFTER the ingest call (or removes it
    entirely) fails this test. Mirrors the equivalent pin in
    test_ingest_slot_ordering.py for the HTTP router."""
    from pathlib import Path

    tools_path = Path(mcp_tools.__file__).resolve()
    source = tools_path.read_text(encoding="utf-8")

    func_pos = source.find("async def _ingest_bytes(")
    assert func_pos != -1, "_ingest_bytes definition missing"
    # Bound the inspection to just this function's body — the next
    # `async def` or `def ` at column 0 ends it.
    body = source[func_pos:]
    next_def_pos = -1
    for marker in ("\n\nasync def ", "\n\ndef "):
        p = body.find(marker, 10)
        if p > 0 and (next_def_pos == -1 or p < next_def_pos):
            next_def_pos = p
    if next_def_pos > 0:
        body = body[:next_def_pos]

    slot_pos = body.find("_try_acquire_ingest_slot()")
    ingest_pos = body.find("ingestion_service.ingest(")
    release_pos = body.find("_release_ingest_slot()")

    assert slot_pos != -1, (
        "Bug #1 regression — _ingest_bytes lost its slot acquire call. "
        "The MCP write path now bypasses the INGEST_MAX_ACTIVE_JOBS gate "
        "again."
    )
    assert ingest_pos != -1, "ingestion_service.ingest() call missing"
    assert release_pos != -1, "_release_ingest_slot() call missing from finally"
    assert slot_pos < ingest_pos, (
        "Bug #1 regression — _try_acquire_ingest_slot() must come BEFORE "
        "ingestion_service.ingest(...) so a 500-doc MCP burst can't bypass "
        "the slot gate."
    )
    assert ingest_pos < release_pos, (
        "_release_ingest_slot() must be in the finally block AFTER the "
        "ingest call, so the slot is held for the full ingest duration."
    )
