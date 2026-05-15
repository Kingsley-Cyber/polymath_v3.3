"""Ingest slot-acquire ordering tests — Bug #1 from the 500-file audit.

Pre-fix:
  routers/ingestion.py:ingest_document read the full file body into
  RAM (`data = await file.read()`) BEFORE acquiring an ingest slot.
  A 500-file simultaneous upload would materialize ~5GB of body bytes
  in RAM (UploadFile's SpooledTemporaryFile rollover to a single
  bytes() object), even though only INGEST_MAX_ACTIVE_JOBS=16 of
  them could run. The other 484 paid the RAM cost just to get 429'd.

Post-fix:
  Slot acquire happens BEFORE file.read(). If the slot 429s, the
  spooled body stays unmaterialized. The early-validation paths
  (empty body, read error) explicitly release the slot they just
  acquired so the next request can use it.

These tests cover the slot accounting primitives (_try_acquire,
_release) and the new release-on-early-failure contract. Driving
the actual FastAPI endpoint requires multipart + mongo + qdrant
fixtures; the contract test below validates the slot semantics
that the endpoint relies on.
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
    """The post-fix endpoint acquires the slot, then runs file.read().
    If the read fails or the body is empty, the explicit release in
    the except / if-not-data branches keeps the active count
    accurate. This test simulates that pattern manually."""
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


# ── Source-code introspection: confirm the reorder is in place ──────


def test_slot_acquire_is_before_file_read_in_source():
    """Source-pin: byte-position of '_try_acquire_ingest_slot()' must
    be BEFORE the byte-position of 'data = await file.read()' in
    ingest_document. A future refactor that moves them back to the
    pre-fix order fails this test.

    Mirrors the Phase 29 injection-order pin pattern from
    test_chat_attachments_production.py."""
    from pathlib import Path

    router_path = (
        Path(ing.__file__).resolve().parent
        / "ingestion.py"
    )
    source = router_path.read_text(encoding="utf-8")

    # We anchor on the in-function lines (not the helper definitions
    # at the top of the file). The function body is the one between
    # the @router.post("/corpora/{corpus_id}/ingest") decorator and
    # the next @router decorator.
    decorator_pos = source.find('@router.post("/corpora/{corpus_id}/ingest"')
    assert decorator_pos != -1, "ingest endpoint decorator missing"
    body = source[decorator_pos:]

    # Find the next router decorator to bound the function body.
    next_decorator_pos = body.find("@router.", 10)
    if next_decorator_pos > 0:
        body = body[:next_decorator_pos]

    slot_pos = body.find("_try_acquire_ingest_slot()")
    read_pos = body.find("data = await file.read()")
    assert slot_pos != -1, "slot acquire call missing from ingest endpoint"
    assert read_pos != -1, "file.read() call missing from ingest endpoint"
    assert slot_pos < read_pos, (
        "Bug #1 regression — _try_acquire_ingest_slot() must come BEFORE "
        "`data = await file.read()` so 500-file uploads don't materialize "
        "all bodies into RAM before the slot rejection runs."
    )
