"""Phase 30 — tests for the MCP corpus / document lifecycle tools.

Pins the contract for the agent-facing write surface:

  • polymath_create_corpus — preset normalization, user_id stamping, return shape
  • polymath_ingest_from_url — URL safety (SSRF blocks), size cap streaming
  • polymath_upload_document — base64 decode + data-URL strip + size cap
  • polymath_get_ingest_status — write_state → status translation, neo4j gate
  • polymath_delete_document — pass-through + status mapping

All tests mock `ingestion_service` and the auth context. We do NOT spin up
Mongo/Qdrant/Neo4j here — that's the integration suite's job. The unit tests
just verify the MCP adapter's logic: input validation, status translation,
SSRF blocking, and faithful service-call wiring.
"""
from __future__ import annotations

import base64
import json
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


# Stub auth-side packages in environments where they aren't installed.
# polymath_mcp.tools transitively imports services.auth → {jose, passlib};
# the MCP write-tool logic under test never touches the JWT or password
# hashing path, so no-op stand-ins are safe. Real environments with
# python-jose / passlib installed skip the stubs entirely.
def _install_auth_stubs_if_missing() -> None:
    # python-jose ---------------------------------------------------------
    try:
        import jose  # noqa: F401
    except ImportError:
        jose_mod = ModuleType("jose")

        class JWTError(Exception):
            pass

        class _Jwt:
            @staticmethod
            def encode(*_a, **_kw):  # pragma: no cover — never invoked here
                raise RuntimeError("jose stub: encode not implemented")

            @staticmethod
            def decode(*_a, **_kw):  # pragma: no cover — never invoked here
                raise RuntimeError("jose stub: decode not implemented")

        jose_mod.JWTError = JWTError
        jose_mod.jwt = _Jwt()
        sys.modules["jose"] = jose_mod

    # passlib + passlib.context ------------------------------------------
    try:
        import passlib.context  # noqa: F401
    except ImportError:
        passlib_mod = ModuleType("passlib")
        ctx_mod = ModuleType("passlib.context")

        class _CryptContext:
            def __init__(self, *a, **kw):
                pass

            def hash(self, *_a, **_kw):  # pragma: no cover
                raise RuntimeError("passlib stub: hash not implemented")

            def verify(self, *_a, **_kw):  # pragma: no cover
                raise RuntimeError("passlib stub: verify not implemented")

        ctx_mod.CryptContext = _CryptContext
        passlib_mod.context = ctx_mod
        sys.modules["passlib"] = passlib_mod
        sys.modules["passlib.context"] = ctx_mod


_install_auth_stubs_if_missing()


from polymath_mcp import tools as mcp_tools  # noqa: E402
from polymath_mcp.auth import SYSTEM_USER_ID, _current_user_id  # noqa: E402
from models.schemas import GlobalIngestionSettings, GlobalIngestionSummarySettings  # noqa: E402
from services.settings import settings_service  # noqa: E402


@pytest.fixture
def system_user():
    """Authenticate as the system sentinel for the duration of the test."""
    token = _current_user_id.set(SYSTEM_USER_ID)
    try:
        yield SYSTEM_USER_ID
    finally:
        _current_user_id.reset(token)


@pytest.fixture
def real_user():
    """Authenticate as a real per-user identity."""
    token = _current_user_id.set("user-abc-123")
    try:
        yield "user-abc-123"
    finally:
        _current_user_id.reset(token)


@pytest.fixture
def anonymous():
    """No auth at all — write tools should refuse when MCP_REQUIRE_AUTH=True."""
    token = _current_user_id.set(None)
    try:
        yield None
    finally:
        _current_user_id.reset(token)


# ─── _safe_ingest_url — SSRF + scheme checks ──────────────────────────────


@pytest.mark.asyncio
async def test_mcp_status_reports_toolsets_and_masks_summary_keys(monkeypatch, system_user):
    async def fake_runtime_settings(user_id: str | None = None):
        return GlobalIngestionSettings(
            summary=GlobalIngestionSummarySettings(
                enabled=True,
                max_summary_tokens=256,
                max_concurrent=4,
                summary_models=[
                    {
                        "provider_preset": "siliconflow",
                        "model": "openai/tencent/Hy3-preview",
                        "base_url": "https://api.siliconflow.cn/v1",
                        "api_key": "unit-secret-key",
                        "max_concurrent": 4,
                        "extra_params": {},
                    }
                ],
            )
        )

    monkeypatch.setattr(
        settings_service,
        "get_runtime_ingestion_settings",
        fake_runtime_settings,
    )

    result = await mcp_tools.polymath_mcp_status(detail="full")
    toolset_names = {toolset["name"] for toolset in result["toolsets"]}
    payload = json.dumps(result)

    assert result["auth"]["mode"] == "system_api_key"
    assert {"context", "retrieval", "graph", "ingestion"} <= toolset_names
    assert "polymath_plan_ingestion" in result["registered_tools"]
    assert result["ingestion"]["summary_defaults"]["enabled"] is True
    assert result["ingestion"]["summary_defaults"]["models"][0]["api_key_configured"] is True
    assert "unit-secret-key" not in payload


@pytest.mark.asyncio
async def test_mcp_status_treats_masked_summary_key_as_configured(monkeypatch, system_user):
    async def fake_runtime_settings(user_id: str | None = None):
        return GlobalIngestionSettings(
            summary=GlobalIngestionSummarySettings(
                enabled=True,
                summary_models=[
                    {
                        "provider_preset": "siliconflow",
                        "model": "openai/tencent/Hy3-preview",
                        "base_url": "https://api.siliconflow.cn/v1",
                        "api_key": "[set]",
                        "max_concurrent": 4,
                        "extra_params": {},
                    }
                ],
            )
        )

    monkeypatch.setattr(
        settings_service,
        "get_runtime_ingestion_settings",
        fake_runtime_settings,
    )

    result = await mcp_tools.polymath_mcp_status()

    assert result["ingestion"]["summary_defaults"]["models"][0]["api_key_configured"] is True


@pytest.mark.asyncio
async def test_plan_ingestion_transcript_defaults_to_deep_summary():
    result = await mcp_tools.polymath_plan_ingestion(
        filename="shopify_video_transcript.txt",
        source_url="https://example.com/shopify_video_transcript.txt",
        content_type="text/plain",
        summary_required="auto",
    )

    assert result["status"] == "ok"
    assert result["profile"] == "transcript"
    assert result["summary_required"] is True
    assert result["ingest_tool"] == "polymath_ingest_from_url"
    assert result["corpus_action"]["action"] == "create_corpus"
    assert result["corpus_action"]["args"]["preset"] == "deep"
    assert "polymath_get_ingest_status until complete" in result["call_sequence"]
    assert result["verification"]["negative_query"]


@pytest.mark.asyncio
async def test_plan_ingestion_existing_balanced_corpus_adds_summary_backfill(system_user):
    with (
        patch.object(mcp_tools, "assert_corpus_allowed", new=AsyncMock()),
        patch.object(
            mcp_tools.ingestion_service,
            "get_corpus",
            new=AsyncMock(
                return_value={
                    "corpus_id": "c1",
                    "default_ingestion_config": {
                        "preset": "balanced",
                        "chunk_summarization": False,
                    },
                }
            ),
        ),
    ):
        result = await mcp_tools.polymath_plan_ingestion(
            filename="metrics.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            summary_required="yes",
            existing_corpus_id="c1",
        )

    assert result["profile"] == "table_or_data"
    assert result["corpus_action"]["action"] == "use_existing_corpus"
    assert result["post_ingest_actions"][0]["tool"] == "polymath_backfill_summaries"
    assert "polymath_backfill_summaries" in result["call_sequence"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file.pdf",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "gopher://example.com/",
    ],
)
def test_safe_ingest_url_rejects_non_http(url):
    allowed, reason = mcp_tools._safe_ingest_url(url)
    assert allowed is False
    assert "scheme" in reason


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/file.pdf",
        "http://127.0.0.1/file.pdf",
        "http://10.0.0.5/file.pdf",
        "http://172.16.0.1/file.pdf",
        "http://192.168.1.1/file.pdf",
        "http://169.254.169.254/latest/meta-data/",  # AWS IMDS
        "http://[::1]/file.pdf",
        "http://metadata.google.internal/computeMetadata/v1/",
    ],
)
def test_safe_ingest_url_blocks_private_and_metadata(url):
    allowed, reason = mcp_tools._safe_ingest_url(url)
    assert allowed is False, f"{url} should be blocked"
    assert reason  # non-empty reason


@pytest.mark.parametrize(
    "url",
    [
        "https://arxiv.org/pdf/2401.12345",
        "https://raw.githubusercontent.com/foo/bar/main/README.md",
        "http://example.com/file.pdf",
    ],
)
def test_safe_ingest_url_allows_public(url):
    allowed, reason = mcp_tools._safe_ingest_url(url)
    assert allowed is True, f"{url} should be allowed: {reason!r}"


def test_safe_ingest_url_unparseable():
    allowed, reason = mcp_tools._safe_ingest_url("not a url")
    assert allowed is False


def test_safe_ingest_url_can_be_unlocked_via_setting():
    """When MCP_INGEST_URL_ALLOW_PRIVATE=True, intranet URLs are accepted."""
    from config import get_settings

    settings = get_settings()
    original = settings.MCP_INGEST_URL_ALLOW_PRIVATE
    try:
        settings.MCP_INGEST_URL_ALLOW_PRIVATE = True
        allowed, reason = mcp_tools._safe_ingest_url("http://10.0.0.5/x.pdf")
        assert allowed is True, reason
    finally:
        settings.MCP_INGEST_URL_ALLOW_PRIVATE = original


# ─── _summarize_write_state — status translation ─────────────────────────


def test_summarize_write_state_queued():
    doc = {"write_state": {}, "ingestion_config": {"use_neo4j": False}}
    assert mcp_tools._summarize_write_state(doc) == "queued"


def test_summarize_write_state_processing_partial():
    doc = {
        "write_state": {"mongo_written": True, "qdrant_written": False, "neo4j_written": False},
        "ingestion_config": {"use_neo4j": True},
    }
    assert mcp_tools._summarize_write_state(doc) == "processing"


def test_summarize_write_state_complete_without_neo4j():
    """A corpus configured with use_neo4j=False completes when only mongo +
    qdrant are written — the trimmer must not wait for a graph write that
    will never happen."""
    doc = {
        "write_state": {
            "mongo_written": True,
            "qdrant_written": True,
            "neo4j_written": False,
        },
        "ingestion_config": {"use_neo4j": False},
    }
    assert mcp_tools._summarize_write_state(doc) == "complete"


def test_summarize_write_state_waits_for_required_summaries():
    doc = {
        "write_state": {
            "mongo_written": True,
            "qdrant_written": True,
            "summaries_indexed": False,
            "neo4j_written": False,
        },
        "ingestion_config": {
            "use_neo4j": False,
            "chunk_summarization": True,
            "target_qdrant_collections": ["naive", "hrag"],
        },
    }
    assert mcp_tools._summarize_write_state(doc) == "processing"


def test_summarize_write_state_complete_with_neo4j():
    doc = {
        "write_state": {
            "mongo_written": True,
            "qdrant_written": True,
            "neo4j_written": True,
        },
        "ingestion_config": {"use_neo4j": True},
    }
    assert mcp_tools._summarize_write_state(doc) == "complete"


def test_summarize_write_state_failed_on_error():
    doc = {
        "error": "docling parse blew up",
        "write_state": {"mongo_written": True, "qdrant_written": True, "neo4j_written": True},
        "ingestion_config": {"use_neo4j": True},
    }
    assert mcp_tools._summarize_write_state(doc) == "failed"


def test_summarize_write_state_failed_verify():
    doc = {
        "write_state": {
            "mongo_written": True,
            "qdrant_written": True,
            "neo4j_written": True,
            "verified": False,
            "verify_errors": ["chunk_count_mismatch"],
        },
        "ingestion_config": {"use_neo4j": True},
    }
    assert mcp_tools._summarize_write_state(doc) == "failed_verify"


# ─── _require_user_id_for_write ───────────────────────────────────────────


def test_require_user_id_for_write_real_user(real_user):
    assert mcp_tools._require_user_id_for_write() == "user-abc-123"


def test_require_user_id_for_write_system(system_user):
    assert mcp_tools._require_user_id_for_write() == SYSTEM_USER_ID


def test_require_user_id_for_write_rejects_anonymous_when_auth_required(anonymous):
    from config import get_settings

    settings = get_settings()
    original = settings.MCP_REQUIRE_AUTH
    try:
        settings.MCP_REQUIRE_AUTH = True
        with pytest.raises(mcp_tools.AuthError):
            mcp_tools._require_user_id_for_write()
    finally:
        settings.MCP_REQUIRE_AUTH = original


def test_require_user_id_for_write_anonymous_falls_back_in_dev(anonymous):
    """When MCP_REQUIRE_AUTH=False, anonymous writes are accepted and the
    corpus is stamped with the system sentinel so it isn't orphaned."""
    from config import get_settings

    settings = get_settings()
    original = settings.MCP_REQUIRE_AUTH
    try:
        settings.MCP_REQUIRE_AUTH = False
        uid = mcp_tools._require_user_id_for_write()
        assert uid == SYSTEM_USER_ID
    finally:
        settings.MCP_REQUIRE_AUTH = original


# ─── polymath_create_corpus ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_corpus_passes_user_id_and_preset(real_user):
    from datetime import datetime

    fake_doc = {
        "corpus_id": "corp-xyz",
        "name": "Quantum EC",
        "description": "Research drop",
        "user_id": real_user,
        "default_ingestion_config": {
            "preset": "balanced",
            "use_neo4j": True,
            "chunk_summarization": False,
        },
        "embedding_model_id": "qwen3-embedding-0.6b-v1",
        "created_at": datetime(2026, 5, 15, 10, 30, 0),
    }
    with patch.object(
        mcp_tools.ingestion_service,
        "create_corpus",
        new=AsyncMock(return_value=fake_doc),
    ) as create_mock:
        result = await mcp_tools.polymath_create_corpus(
            name="Quantum EC",
            description="Research drop",
            preset="balanced",
        )
    assert result["status"] == "created"
    assert result["corpus_id"] == "corp-xyz"
    assert result["preset"] == "balanced"
    assert result["use_neo4j"] is True
    create_mock.assert_awaited_once()
    kwargs = create_mock.await_args.kwargs
    assert kwargs["name"] == "Quantum EC"
    assert kwargs["user_id"] == real_user


@pytest.mark.asyncio
async def test_create_corpus_rejects_empty_name(real_user):
    with pytest.raises(ValueError):
        await mcp_tools.polymath_create_corpus(name="   ")


@pytest.mark.asyncio
async def test_create_corpus_rejects_anonymous_when_auth_required(anonymous):
    from config import get_settings

    settings = get_settings()
    original = settings.MCP_REQUIRE_AUTH
    try:
        settings.MCP_REQUIRE_AUTH = True
        with pytest.raises(mcp_tools.AuthError):
            await mcp_tools.polymath_create_corpus(name="test")
    finally:
        settings.MCP_REQUIRE_AUTH = original


# ─── polymath_upload_document ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_document_decodes_base64_and_queues(system_user):
    payload = b"hello world contents of a tiny file"
    b64 = base64.b64encode(payload).decode("ascii")
    fake_corpus = {
        "corpus_id": "c1",
        "default_ingestion_config": {
            "preset": "balanced",
            "use_neo4j": True,
            "chunk_summarization": False,
        },
    }
    fake_response = SimpleNamespace(
        job_id="j1",
        doc_id="d1",
        filename="hello.txt",
        source_tier="text_native",
    )
    with (
        patch.object(
            mcp_tools,
            "assert_corpus_allowed",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            mcp_tools.ingestion_service,
            "_get_corpus_raw",
            new=AsyncMock(return_value=fake_corpus),
        ),
        patch.object(
            mcp_tools.ingestion_service,
            "ingest",
            new=AsyncMock(return_value=fake_response),
        ) as ingest_mock,
    ):
        result = await mcp_tools.polymath_upload_document(
            corpus_id="c1",
            filename="hello.txt",
            content_base64=b64,
        )

    assert result["status"] == "queued"
    assert result["doc_id"] == "d1"
    assert result["size_bytes"] == len(payload)
    ingest_mock.assert_awaited_once()
    call_kwargs = ingest_mock.await_args.kwargs
    assert call_kwargs["data"] == payload
    assert call_kwargs["filename"] == "hello.txt"
    assert call_kwargs["corpus_id"] == "c1"
    assert call_kwargs["model"] == ""  # signals "use corpus default pools"


@pytest.mark.asyncio
async def test_upload_document_strips_data_url_prefix(system_user):
    payload = b"abc"
    b64 = base64.b64encode(payload).decode("ascii")
    data_url = f"data:text/plain;base64,{b64}"
    fake_corpus = {"corpus_id": "c1", "default_ingestion_config": {}}
    fake_response = SimpleNamespace(
        job_id="j", doc_id="d", filename="x.txt", source_tier=None
    )
    with (
        patch.object(mcp_tools, "assert_corpus_allowed", new=AsyncMock()),
        patch.object(
            mcp_tools.ingestion_service,
            "_get_corpus_raw",
            new=AsyncMock(return_value=fake_corpus),
        ),
        patch.object(
            mcp_tools.ingestion_service,
            "ingest",
            new=AsyncMock(return_value=fake_response),
        ) as ingest_mock,
    ):
        result = await mcp_tools.polymath_upload_document(
            corpus_id="c1",
            filename="x.txt",
            content_base64=data_url,
        )
    assert result["status"] == "queued"
    assert ingest_mock.await_args.kwargs["data"] == payload


@pytest.mark.asyncio
async def test_upload_document_rejects_oversize(system_user):
    """Payload bigger than MCP_INGEST_MAX_BYTES is rejected before the
    ingest call fires — protect the worker from being asked to chew on
    a multi-GB blob that would have died downstream anyway."""
    from config import get_settings

    settings = get_settings()
    original = settings.MCP_INGEST_MAX_BYTES
    try:
        # Shrink the cap so the test stays fast.
        settings.MCP_INGEST_MAX_BYTES = 16
        b64 = base64.b64encode(b"x" * 64).decode("ascii")
        with (
            patch.object(mcp_tools, "assert_corpus_allowed", new=AsyncMock()),
            patch.object(
                mcp_tools.ingestion_service,
                "_get_corpus_raw",
                new=AsyncMock(return_value={"corpus_id": "c1"}),
            ),
            patch.object(
                mcp_tools.ingestion_service, "ingest", new=AsyncMock()
            ) as ingest_mock,
        ):
            with pytest.raises(ValueError, match="cap"):
                await mcp_tools.polymath_upload_document(
                    corpus_id="c1",
                    filename="big.txt",
                    content_base64=b64,
                )
            ingest_mock.assert_not_awaited()
    finally:
        settings.MCP_INGEST_MAX_BYTES = original


@pytest.mark.asyncio
async def test_upload_document_rejects_invalid_base64(system_user):
    with (
        patch.object(mcp_tools, "assert_corpus_allowed", new=AsyncMock()),
    ):
        with pytest.raises(ValueError, match="base64"):
            await mcp_tools.polymath_upload_document(
                corpus_id="c1",
                filename="x.txt",
                content_base64="not base64 !!!",
            )


# ─── polymath_ingest_from_url ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_from_url_blocks_private_ip(system_user):
    """The SSRF safety check must fire BEFORE we attempt an HTTP fetch."""
    with patch.object(mcp_tools, "assert_corpus_allowed", new=AsyncMock()):
        with pytest.raises(ValueError, match="private"):
            await mcp_tools.polymath_ingest_from_url(
                corpus_id="c1",
                url="http://10.0.0.5/secret.pdf",
            )


@pytest.mark.asyncio
async def test_ingest_from_url_blocks_non_http(system_user):
    with patch.object(mcp_tools, "assert_corpus_allowed", new=AsyncMock()):
        with pytest.raises(ValueError, match="scheme"):
            await mcp_tools.polymath_ingest_from_url(
                corpus_id="c1",
                url="file:///etc/passwd",
            )


# ─── polymath_get_ingest_status ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_ingest_status_not_found(system_user):
    with patch.object(
        mcp_tools.ingestion_service,
        "get_job_status",
        new=AsyncMock(return_value=None),
    ):
        result = await mcp_tools.polymath_get_ingest_status(doc_id="missing")
    assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_get_ingest_status_translates_complete(system_user):
    doc = {
        "doc_id": "d1",
        "corpus_id": "c1",
        "filename": "paper.pdf",
        "write_state": {
            "mongo_written": True,
            "qdrant_written": True,
            "neo4j_written": True,
            "verified": True,
            "warnings": [],
            "verify_errors": [],
        },
        "chunk_count": 42,
        "parent_chunks": [{"i": i} for i in range(5)],
        "ingestion_config": {"use_neo4j": True},
        "source_tier": "pdf_native",
        "error": None,
        "ingested_at": None,
    }
    with patch.object(
        mcp_tools.ingestion_service,
        "get_job_status",
        new=AsyncMock(return_value=doc),
    ):
        result = await mcp_tools.polymath_get_ingest_status(doc_id="d1")
    assert result["status"] == "complete"
    assert result["chunk_count"] == 42
    assert result["parent_count"] == 5
    assert result["write_state"]["mongo_written"] is True


@pytest.mark.asyncio
async def test_get_ingest_status_translates_failed(system_user):
    doc = {
        "doc_id": "d1",
        "error": "OOM during embedding",
        "write_state": {"mongo_written": True, "qdrant_written": False, "neo4j_written": False},
        "ingestion_config": {"use_neo4j": True},
    }
    with patch.object(
        mcp_tools.ingestion_service,
        "get_job_status",
        new=AsyncMock(return_value=doc),
    ):
        result = await mcp_tools.polymath_get_ingest_status(doc_id="d1")
    assert result["status"] == "failed"
    assert result["error"] == "OOM during embedding"


# ─── polymath_delete_document ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_document_success(system_user):
    with (
        patch.object(mcp_tools, "assert_corpus_allowed", new=AsyncMock()),
        patch.object(
            mcp_tools.ingestion_service,
            "delete_document",
            new=AsyncMock(return_value=True),
        ) as del_mock,
    ):
        result = await mcp_tools.polymath_delete_document(
            corpus_id="c1", doc_id="d1"
        )
    assert result["status"] == "deleted"
    del_mock.assert_awaited_once_with("c1", "d1")


@pytest.mark.asyncio
async def test_delete_document_not_found(system_user):
    with (
        patch.object(mcp_tools, "assert_corpus_allowed", new=AsyncMock()),
        patch.object(
            mcp_tools.ingestion_service,
            "delete_document",
            new=AsyncMock(return_value=False),
        ),
    ):
        result = await mcp_tools.polymath_delete_document(
            corpus_id="c1", doc_id="missing"
        )
    assert result["status"] == "not_found"


# ─── Registry — new tools are wired in ────────────────────────────────────


def test_new_tools_in_registry():
    names = {fn.__name__ for fn in mcp_tools.ALL_TOOLS}
    assert "polymath_mcp_status" in names
    assert "polymath_plan_ingestion" in names
    assert "polymath_create_corpus" in names
    assert "polymath_ingest_from_url" in names
    assert "polymath_upload_document" in names
    assert "polymath_get_ingest_status" in names
    assert "polymath_delete_document" in names
