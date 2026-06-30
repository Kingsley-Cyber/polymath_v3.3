"""Asserting tests for the streaming-LLM connection retry (burst blank-answer fix).

The retry must:
  * fire on a TRANSIENT connect error (DNS getaddrinfo) before the first token,
  * NOT retry once a token has streamed (no duplicate output),
  * NOT retry a non-transient error (bad model, HTTP status),
  * give up after LLM_STREAM_MAX_RETRIES.

Run inside the backend container:
    docker exec -i polymath_v33-backend-1 python /app/tests/test_stream_retry.py
"""

from __future__ import annotations

import asyncio
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import httpx  # noqa: E402

from services.llm import LLMService, _is_transient_stream_error  # noqa: E402


# ── transient classifier ───────────────────────────────────────────────────
def test_dns_errno_is_transient():
    assert _is_transient_stream_error(RuntimeError("[Errno -2] Name or service not known"))
    assert _is_transient_stream_error(Exception("Temporary failure in name resolution"))


def test_httpx_connect_error_is_transient():
    assert _is_transient_stream_error(httpx.ConnectError("connection refused"))


def test_http_status_error_is_not_transient():
    req = httpx.Request("POST", "http://litellm:4000/x")
    resp = httpx.Response(500, request=req)
    assert _is_transient_stream_error(httpx.HTTPStatusError("500", request=req, response=resp)) is False


def test_plain_value_error_is_not_transient():
    assert _is_transient_stream_error(ValueError("unknown model 'foo'")) is False


# ── retry wrapper ──────────────────────────────────────────────────────────
class _ScriptedService(LLMService):
    """LLMService whose _stream_chat_once is scripted per attempt."""

    def __init__(self, script):
        super().__init__()
        self._script = script
        self.calls = 0
        # keep the test fast — no real backoff
        import config

        config.get_settings().LLM_STREAM_RETRY_BACKOFF_SECONDS = 0.0

    async def _stream_chat_once(self, *a, **k):
        behavior = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if behavior == "dns_fail":
            raise RuntimeError("[Errno -2] Name or service not known")
        if behavior == "hard_fail":
            raise ValueError("unknown model 'foo'")
        if behavior == "token_then_fail":
            yield {"content": "partial"}
            raise RuntimeError("[Errno -2] Name or service not known")
        if behavior == "ok":
            yield {"content": "hello"}
            return
        raise AssertionError(f"bad behavior {behavior}")


async def _collect(svc):
    out = []
    async for item in svc.stream_chat(messages=[{"role": "user", "content": "hi"}]):
        out.append(item)
    return out


def test_retries_transient_then_succeeds():
    svc = _ScriptedService(["dns_fail", "dns_fail", "ok"])
    out = asyncio.run(_collect(svc))
    assert out == [{"content": "hello"}]
    assert svc.calls == 3                      # two retries, then success


def test_no_retry_after_a_token_streamed():
    svc = _ScriptedService(["token_then_fail"])
    raised = False
    try:
        asyncio.run(_collect(svc))
    except RuntimeError:
        raised = True
    assert raised
    assert svc.calls == 1                       # must NOT retry — would duplicate output


def test_no_retry_on_non_transient():
    svc = _ScriptedService(["hard_fail"])
    raised = False
    try:
        asyncio.run(_collect(svc))
    except ValueError:
        raised = True
    assert raised
    assert svc.calls == 1                       # bad model is not a connection blip


def test_gives_up_after_max_retries():
    svc = _ScriptedService(["dns_fail"])         # always fails
    raised = False
    try:
        asyncio.run(_collect(svc))
    except RuntimeError:
        raised = True
    assert raised
    # default LLM_STREAM_MAX_RETRIES=2 → 1 initial + 2 retries = 3 attempts
    assert svc.calls == 3


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
