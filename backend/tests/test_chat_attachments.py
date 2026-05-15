"""Phase 29 — per-turn chat attachment tests.

Validates the request-shape contract and the joint message/attachments
constraint. Does NOT exercise the orchestrator's multimodal injection
(that requires a full retrieval pipeline + LLM mock — covered by
integration tests later).

The structural pins here are what stops a future refactor from:
  - Re-introducing the "empty message → 400" rule that would block
    attachment-only turns
  - Removing per-attachment size caps
  - Removing the 4-attachment-per-turn cap
  - Letting a request through with neither message nor attachments
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from models.schemas import ChatAttachment, ChatRequest


# ─── ChatAttachment field validation ───────────────────────────────────────


def test_chat_attachment_image_minimal_fields():
    """Image attachment with the minimum required fields parses."""
    att = ChatAttachment(
        filename="screenshot.png",
        mime_type="image/png",
        size_bytes=12345,
        kind="image",
        content="aGVsbG8=",  # base64 "hello"
    )
    assert att.kind == "image"
    assert att.filename == "screenshot.png"


def test_chat_attachment_text_minimal_fields():
    """Text attachment parses with kind='text' and UTF-8 content."""
    att = ChatAttachment(
        filename="notes.md",
        mime_type="text/markdown",
        size_bytes=42,
        kind="text",
        content="# Hello\n\nSome notes.",
    )
    assert att.kind == "text"


def test_chat_attachment_rejects_unknown_kind():
    """kind must be 'image' or 'text' — no other values accepted."""
    with pytest.raises(ValidationError):
        ChatAttachment(
            filename="x.pdf",
            mime_type="application/pdf",
            size_bytes=100,
            kind="pdf",  # not allowed
            content="x",
        )


def test_chat_attachment_rejects_empty_content():
    """Empty content is rejected — attachments must carry payload."""
    with pytest.raises(ValidationError):
        ChatAttachment(
            filename="empty.png",
            mime_type="image/png",
            size_bytes=0,
            kind="image",
            content="",
        )


def test_chat_attachment_rejects_oversize_file():
    """size_bytes ceiling is 20MB. Anything larger is rejected at
    validation time so the request never enters the chat pipeline."""
    with pytest.raises(ValidationError):
        ChatAttachment(
            filename="huge.png",
            mime_type="image/png",
            size_bytes=30 * 1024 * 1024,  # 30MB
            kind="image",
            content="aGVsbG8=",
        )


def test_chat_attachment_rejects_oversize_content():
    """Content string ceiling is 28MB chars (allows ~20MB binary
    after base64 inflation). Beyond that the validator trips."""
    oversized = "a" * (29 * 1024 * 1024)
    with pytest.raises(ValidationError):
        ChatAttachment(
            filename="huge.png",
            mime_type="image/png",
            size_bytes=1024,
            kind="image",
            content=oversized,
        )


# ─── ChatRequest joint message+attachments constraint ─────────────────────


def _image_att() -> ChatAttachment:
    return ChatAttachment(
        filename="x.png",
        mime_type="image/png",
        size_bytes=10,
        kind="image",
        content="aGVsbG8=",
    )


def test_chat_request_message_only_works():
    req = ChatRequest(message="hi", attachments=None)
    assert req.message == "hi"
    assert req.attachments is None


def test_chat_request_attachment_only_works():
    """Empty message + an attachment is a valid request — the 'what's
    in this image?' case where the image IS the question."""
    req = ChatRequest(message="", attachments=[_image_att()])
    assert req.message == ""
    assert req.attachments is not None and len(req.attachments) == 1


def test_chat_request_both_works():
    req = ChatRequest(
        message="describe this",
        attachments=[_image_att()],
    )
    assert req.message == "describe this"
    assert req.attachments and len(req.attachments) == 1


def test_chat_request_rejects_both_empty():
    """Joint validator: no message AND no attachments → 422."""
    with pytest.raises(ValidationError) as exc:
        ChatRequest(message="", attachments=None)
    assert "non-empty message" in str(exc.value) or "attachment" in str(exc.value)


def test_chat_request_rejects_whitespace_message_no_attachments():
    """A message of just whitespace is treated as empty."""
    with pytest.raises(ValidationError):
        ChatRequest(message="   \n\t  ", attachments=None)


def test_chat_request_whitespace_message_with_attachment_works():
    """Whitespace message is OK if attachment is present."""
    req = ChatRequest(message="   ", attachments=[_image_att()])
    assert req.attachments is not None


# ─── Attachment count cap ─────────────────────────────────────────────────


def test_chat_request_rejects_more_than_4_attachments():
    """Hard cap at 4 attachments per turn — keeps request payload size
    and LLM context budget honest."""
    too_many = [_image_att() for _ in range(5)]
    with pytest.raises(ValidationError) as exc:
        ChatRequest(message="hi", attachments=too_many)
    assert "Maximum 4 attachments" in str(exc.value)


def test_chat_request_accepts_4_attachments():
    just_right = [_image_att() for _ in range(4)]
    req = ChatRequest(message="hi", attachments=just_right)
    assert len(req.attachments or []) == 4


# ─── Mixed-kind attachments ────────────────────────────────────────────────


def test_chat_request_accepts_image_and_text_mix():
    """Common case: a screenshot + a markdown file in the same turn."""
    text_att = ChatAttachment(
        filename="notes.md",
        mime_type="text/markdown",
        size_bytes=20,
        kind="text",
        content="# Header\nbody.",
    )
    req = ChatRequest(
        message="incorporate this design",
        attachments=[_image_att(), text_att],
    )
    assert len(req.attachments or []) == 2
    kinds = {a.kind for a in (req.attachments or [])}
    assert kinds == {"image", "text"}
