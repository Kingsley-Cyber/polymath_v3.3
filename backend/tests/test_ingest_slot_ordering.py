"""Ingest admission tests and browser-upload disablement pins.

Large corpus ingest is now backend-folder only. The legacy browser multipart
endpoint must reject before declaring UploadFile/File parameters or reading a
body, while the shared admission primitives remain tested for MCP/internal
ingest surfaces that still use the same slot gate.

These tests cover the slot accounting primitives (_try_acquire,
_release) and pin the disabled browser endpoint so it cannot silently become
a request-owned batch runner again.
"""
from __future__ import annotations

import sys
from types import ModuleType

import pytest


# ── Auth-package stubs ───────────────────────────────────────────────
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


from routers import ingestion as ing  # noqa: E402
from services.ingestion import admission as _admission  # noqa: E402


@pytest.fixture(autouse=True)
def reset_slot_counter():
    """Tests share module-level _INGEST_ACTIVE_COUNT; reset between
    runs so they don't bleed state. We also restore the original
    limit at the end in case a test shrinks it."""
    original_count = _admission._ingest_active_count
    original_limit = _admission.INGEST_ACTIVE_LIMIT
    _admission._ingest_active_count = 0
    yield
    _admission._ingest_active_count = original_count
    _admission.INGEST_ACTIVE_LIMIT = original_limit


# ── Slot primitives ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slot_acquire_increments_count():
    """_try_acquire_ingest_slot returns True + bumps the active counter."""
    assert _admission._ingest_active_count == 0
    ok = await ing._try_acquire_ingest_slot()
    assert ok is True
    assert _admission._ingest_active_count == 1


@pytest.mark.asyncio
async def test_slot_release_decrements_count():
    """_release_ingest_slot decrements but never goes below 0."""
    await ing._try_acquire_ingest_slot()
    assert _admission._ingest_active_count == 1
    await ing._release_ingest_slot()
    assert _admission._ingest_active_count == 0
    # Double-release is safe (defensive on early-failure paths that
    # might race with the _run() task's own release).
    await ing._release_ingest_slot()
    assert _admission._ingest_active_count == 0


@pytest.mark.asyncio
async def test_slot_acquire_returns_false_at_limit():
    """Once active count hits the limit, further acquires fail."""
    _admission.INGEST_ACTIVE_LIMIT = 3
    for _ in range(3):
        assert await ing._try_acquire_ingest_slot() is True
    assert _admission._ingest_active_count == 3
    # Fourth acquire fails — this is the 429 path.
    ok = await ing._try_acquire_ingest_slot()
    assert ok is False
    # Counter unchanged.
    assert _admission._ingest_active_count == 3


@pytest.mark.asyncio
async def test_release_frees_slot_for_next_acquire():
    """A successful release allows the next acquire to succeed."""
    _admission.INGEST_ACTIVE_LIMIT = 1
    assert await ing._try_acquire_ingest_slot() is True
    assert await ing._try_acquire_ingest_slot() is False  # full
    await ing._release_ingest_slot()
    # Slot is now free again.
    assert await ing._try_acquire_ingest_slot() is True


# ── Bug #1 contract: 500-file simulation ────────────────────────────


@pytest.mark.asyncio
async def test_burst_of_acquires_respects_limit():
    """Simulate 500 simultaneous ingest requests competing for slots.
    Only INGEST_MAX_ACTIVE_JOBS get through; the rest get the 429
    path WITHOUT having read their file bodies into RAM (which is
    the entire point of the reorder fix)."""
    import asyncio
    _admission.INGEST_ACTIVE_LIMIT = 16

    async def attempt() -> bool:
        return await ing._try_acquire_ingest_slot()

    results = await asyncio.gather(*[attempt() for _ in range(500)])
    successes = sum(1 for r in results if r is True)
    failures = sum(1 for r in results if r is False)
    assert successes == 16
    assert failures == 484
    assert _admission._ingest_active_count == 16


# ── Early-failure release contract ──────────────────────────────────


@pytest.mark.asyncio
async def test_early_failure_release_pattern_keeps_accounting_honest():
    """Internal callers that acquire then fail before enqueue must release."""
    _admission.INGEST_ACTIVE_LIMIT = 2
    # Successful slot acquire.
    assert await ing._try_acquire_ingest_slot() is True
    assert _admission._ingest_active_count == 1

    # Simulate the "empty body" branch.
    empty_body = b""
    if not empty_body:
        await ing._release_ingest_slot()
    assert _admission._ingest_active_count == 0

    # Simulate the "read raised" branch.
    assert await ing._try_acquire_ingest_slot() is True
    try:
        raise OSError("network drop mid-upload")
    except OSError:
        await ing._release_ingest_slot()
    assert _admission._ingest_active_count == 0

    # The slot is genuinely free — a new request gets it.
    assert await ing._try_acquire_ingest_slot() is True
    assert _admission._ingest_active_count == 1


# ── Browser ingest endpoint is intentionally disabled ───────────────


@pytest.mark.asyncio
async def test_browser_ingest_endpoint_rejects_with_backend_batch_hint(monkeypatch):
    async def fake_get_corpus(corpus_id):
        return {"corpus_id": corpus_id}

    monkeypatch.setattr(ing.ingestion_service, "get_corpus", fake_get_corpus)

    with pytest.raises(ing.HTTPException) as exc_info:
        await ing.ingest_document(
            corpus_id="corpus-1",
            current_user={"user_id": "user-1"},
        )

    assert exc_info.value.status_code == 410
    assert "ingest-batches/local" in str(exc_info.value.detail)


def test_browser_ingest_endpoint_does_not_read_multipart_body():
    """Source-pin: browser ingest must reject before body parsing/slot work."""
    from pathlib import Path

    router_path = (
        Path(ing.__file__).resolve().parent
        / "ingestion.py"
    )
    source = router_path.read_text(encoding="utf-8")

    decorator_pos = source.find('@router.post("/corpora/{corpus_id}/ingest"')
    assert decorator_pos != -1, "ingest endpoint decorator missing"
    body = source[decorator_pos:]

    # Find the next router decorator to bound the function body.
    next_decorator_pos = body.find("@router.", 10)
    if next_decorator_pos > 0:
        body = body[:next_decorator_pos]

    assert "UploadFile" not in body
    assert "File(" not in body
    assert "Form(" not in body
    assert "file.read()" not in body
    assert "_try_acquire_ingest_slot()" not in body
    assert "ingest-batches/local" in body
