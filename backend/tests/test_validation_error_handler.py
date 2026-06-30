"""Asserting test: the RequestValidationError handler must not crash when an
error payload carries a non-JSON-serializable object (Pydantic v2 puts a raw
ValueError in `ctx`). Before the fix it raised
`TypeError: Object of type ValueError is not JSON serializable`, masking the 422.

Run inside the backend container:
    docker exec -i polymath_v33-backend-1 python /app/tests/test_validation_error_handler.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.exceptions import RequestValidationError  # noqa: E402

from main import _log_validation_error  # noqa: E402


class _URL:
    path = "/api/chat"


class _Req:
    method = "POST"
    url = _URL()


def _make_exc():
    # Mirror the real shape: a value_error whose ctx holds a raw exception.
    return RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("body", "message"),
                "msg": "Value error, boom",
                "input": {"message": ""},
                "ctx": {"error": ValueError("boom")},  # <- non-serializable
            }
        ]
    )


def test_handler_does_not_crash_on_nonserializable_ctx():
    resp = asyncio.run(_log_validation_error(_Req(), _make_exc()))
    assert resp.status_code == 422
    # The body must be valid JSON (proves render/json.dumps succeeded).
    body = json.loads(bytes(resp.body))
    assert "detail" in body
    assert isinstance(body["detail"], list)


def test_handler_preserves_error_fields():
    resp = asyncio.run(_log_validation_error(_Req(), _make_exc()))
    detail = json.loads(bytes(resp.body))["detail"][0]
    # Core diagnostic fields survive the sanitization.
    assert detail.get("type") == "value_error"
    assert "message" in detail.get("loc", [])
    assert "boom" in str(detail.get("msg", ""))


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
