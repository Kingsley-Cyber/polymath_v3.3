"""Phase 29 production-readiness tests.

Pins the three gaps the production-readiness review identified:

  1. Text-file attachment injection MUST happen BEFORE
     build_augmented_prompt runs (so the inlined <attached_file>
     blocks land in the same context-management pass as RAG sources).
  2. Image attachments contribute to the token budget (so
     _trim_history sees the right total and doesn't silently overflow
     the model's context window).
  3. Vision-capability detector correctly identifies vision-capable
     models across providers and rejects non-vision models when
     image attachments are present.
"""

from __future__ import annotations

import pytest

from models.schemas import ChatAttachment
from services.vision_capabilities import (
    attachments_include_image,
    supports_vision,
    matched_label,
)
from utils.tokens import estimate_attachment_tokens


# ─── Gap 3: vision capability detector ────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    [
        # OpenAI GPT-4o family
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "openai/gpt-4o-2024-08-06",
        # OpenAI o-series with vision
        "openai/o1",
        "openai/o3-mini",
        "openai/o4-preview",
        # GPT-4 Turbo with vision
        "openai/gpt-4-turbo",
        "openai/gpt-4-vision-preview",
        # Anthropic Claude (all 3+ have vision)
        "anthropic/claude-3-5-sonnet",
        "anthropic/claude-3-opus",
        "anthropic/claude-sonnet-4-5",
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-opus-4",
        # Google Gemini 1.5+ / 2.x
        "gemini/gemini-1.5-pro",
        "gemini/gemini-1.5-flash",
        "gemini/gemini-2.0-flash",
        "gemini/gemini-2.5-flash",
        "gemini/gemini-2.5-pro",
        # GLM vision variants
        "zai/glm-4.5v",
        "zai/glm-5v-turbo",
        # Mistral Pixtral
        "mistral/pixtral-large-latest",
        "mistral/pixtral-12b",
        # Qwen-VL
        "qwen/qwen2-vl-72b-instruct",
        "qwen/qwen2.5-vl-7b",
        # Llama vision
        "meta/llama-3.2-90b-vision-instruct",
        "llama-4-maverick",
        "llama-4-scout",
    ],
)
def test_supports_vision_known_vision_models(model):
    """Every vision-capable model in our matrix must return True."""
    assert supports_vision(model), f"{model!r} should have vision support"


@pytest.mark.parametrize(
    "model",
    [
        # Older / non-vision OpenAI
        "openai/gpt-3.5-turbo",
        "openai/gpt-4",  # base GPT-4 without -turbo / -vision suffix
        # Pre-vision Gemini
        "gemini/gemini-1.0-pro",
        "gemini/gemini-pro",
        # GLM non-vision
        "zai/glm-5",
        "zai/glm-4.7",
        "zai/glm-4.5",
        "zai/glm-5.1",  # text only despite being newer
        # DeepSeek (no vision)
        "deepseek/deepseek-chat",
        "deepseek/deepseek-v4-pro",  # V4 doesn't have vision per our matrix
        "deepseek/deepseek-reasoner",
        # Non-pixtral Mistral
        "mistral/mistral-large-latest",
        "mistral/magistral-small-latest",
        "mistral/codestral-latest",
        # Ollama text-only
        "ollama/llama3.2:3b",
        "ollama/qwen2.5:7b",
        # Llama 3.0 / 3.1 base (no vision)
        "meta/llama-3.1-70b-instruct",
    ],
)
def test_supports_vision_known_text_only_models(model):
    """Known text-only models must return False."""
    assert not supports_vision(model), f"{model!r} should NOT have vision"


def test_supports_vision_handles_none_and_empty():
    assert supports_vision(None) is False
    assert supports_vision("") is False
    assert supports_vision("   ") is False


def test_supports_vision_rejects_unresolved_pool_refs():
    """Bare pool:<id> / profile:<id> shouldn't accidentally match
    something in their entry_id. Caller must resolve first."""
    assert supports_vision("pool:gpt-4o-style-id") is False
    assert supports_vision("profile:claude-id") is False


def test_matched_label_returns_provider_tag():
    """Diagnostic helper — returns the matching pattern's label."""
    assert matched_label("openai/gpt-4o") == "openai-gpt4o"
    assert matched_label("anthropic/claude-sonnet-4-5") == "anthropic-claude"
    assert matched_label("openai/gpt-3.5-turbo") is None


# ─── attachments_include_image helper ──────────────────────────────────────


def test_attachments_include_image_empty():
    assert attachments_include_image(None) is False
    assert attachments_include_image([]) is False


def test_attachments_include_image_text_only():
    text_only = [
        ChatAttachment(
            filename="x.md",
            mime_type="text/markdown",
            size_bytes=10,
            kind="text",
            content="hi",
        )
    ]
    assert attachments_include_image(text_only) is False


def test_attachments_include_image_with_image():
    mixed = [
        ChatAttachment(
            filename="x.md",
            mime_type="text/markdown",
            size_bytes=10,
            kind="text",
            content="hi",
        ),
        ChatAttachment(
            filename="y.png",
            mime_type="image/png",
            size_bytes=20,
            kind="image",
            content="aGVsbG8=",
        ),
    ]
    assert attachments_include_image(mixed) is True


def test_attachments_include_image_accepts_dicts():
    """Duck-typed — should work with raw dicts (e.g., pre-validation
    request payloads)."""
    raw = [{"kind": "image", "content": "x"}]
    assert attachments_include_image(raw) is True
    assert attachments_include_image([{"kind": "text", "content": "x"}]) is False


# ─── Gap 2: attachment token budget ───────────────────────────────────────


def test_estimate_attachment_tokens_empty():
    assert estimate_attachment_tokens(None) == 0
    assert estimate_attachment_tokens([]) == 0


def test_estimate_attachment_tokens_image_openai():
    """OpenAI vision: ~1200 tokens per image."""
    img = ChatAttachment(
        filename="x.png",
        mime_type="image/png",
        size_bytes=10,
        kind="image",
        content="aGVsbG8=",
    )
    assert estimate_attachment_tokens([img], "openai/gpt-4o") == 1200


def test_estimate_attachment_tokens_image_anthropic_higher():
    """Anthropic Claude uses ~1600 tokens per image — higher than OpenAI."""
    img = ChatAttachment(
        filename="x.png",
        mime_type="image/png",
        size_bytes=10,
        kind="image",
        content="aGVsbG8=",
    )
    assert estimate_attachment_tokens([img], "anthropic/claude-sonnet-4-5") == 1600


def test_estimate_attachment_tokens_image_gemini_lower():
    """Gemini uses fewer tokens per image (~400)."""
    img = ChatAttachment(
        filename="x.png",
        mime_type="image/png",
        size_bytes=10,
        kind="image",
        content="aGVsbG8=",
    )
    assert estimate_attachment_tokens([img], "gemini/gemini-2.5-flash") == 400


def test_estimate_attachment_tokens_unknown_provider_uses_default():
    """Unknown provider → conservative default (1600, matching the
    highest known per-image cost)."""
    img = ChatAttachment(
        filename="x.png",
        mime_type="image/png",
        size_bytes=10,
        kind="image",
        content="aGVsbG8=",
    )
    assert estimate_attachment_tokens([img], "unknown-vendor/some-model") == 1600


def test_estimate_attachment_tokens_text_uses_tiktoken():
    """Text content tokens are counted via tiktoken (real count, not
    a flat estimate) plus a wrapper-tag overhead."""
    text = ChatAttachment(
        filename="doc.md",
        mime_type="text/markdown",
        size_bytes=100,
        kind="text",
        content="The quick brown fox jumps over the lazy dog.",
    )
    # "The quick brown fox jumps over the lazy dog." is 9 tokens in
    # cl100k_base. Plus 20-token wrapper overhead = 29 total.
    estimate = estimate_attachment_tokens([text], "openai/gpt-4o")
    assert 20 <= estimate <= 50


def test_estimate_attachment_tokens_4_images_dominates():
    """Realistic case — 4 image attachments add up. Pin so the trimmer
    sees enough budget pressure to evict history accordingly."""
    images = [
        ChatAttachment(
            filename=f"x{i}.png",
            mime_type="image/png",
            size_bytes=10,
            kind="image",
            content="aGVsbG8=",
        )
        for i in range(4)
    ]
    # 4 × 1200 (OpenAI) = 4800 tokens. That's ~4% of GPT-4o's 128K
    # context — small but enough that ignoring it causes overflow
    # in long conversations.
    assert estimate_attachment_tokens(images, "openai/gpt-4o") == 4800


def test_estimate_attachment_tokens_mixed():
    img = ChatAttachment(
        filename="x.png",
        mime_type="image/png",
        size_bytes=10,
        kind="image",
        content="aGVsbG8=",
    )
    text = ChatAttachment(
        filename="x.md",
        mime_type="text/markdown",
        size_bytes=10,
        kind="text",
        content="hello world",
    )
    total = estimate_attachment_tokens([img, text], "openai/gpt-4o")
    # 1200 (image) + ~22 (text body + wrapper)
    assert 1200 < total < 1300


# ─── Gap 1: injection order is BEFORE build_augmented_prompt ──────────────


def test_injection_order_is_locked_in_source():
    """Pin via source-code introspection. The text-attachment inline
    block in chat_orchestrator.py MUST appear BEFORE the
    build_augmented_prompt call site.

    If a future refactor moves the attachment block AFTER the prompt
    builder, this test fails — preventing a regression where
    <attached_file> blocks bypass the system-prompt context manager
    and get appended in a way the model can't parse uniformly.
    """
    from pathlib import Path

    orchestrator_path = (
        Path(__file__).resolve().parent.parent
        / "services"
        / "chat_orchestrator.py"
    )
    source = orchestrator_path.read_text(encoding="utf-8")

    # Find both anchor strings. They MUST both exist; if the
    # implementation changes the comment wording, update this test too.
    attachment_anchor = (
        "inline text-file attachments into the user message"
    )
    augment_anchor = "_build_budgeted_augmented_prompt("
    att_pos = source.find(attachment_anchor)
    aug_pos = source.find(augment_anchor)
    assert att_pos != -1, "attachment anchor missing — implementation changed"
    aug_pos = source.find(augment_anchor, att_pos)
    assert aug_pos != -1, "build_augmented_prompt call after attachment block missing"
    assert att_pos < aug_pos, (
        "Text-attachment inlining MUST run BEFORE build_augmented_prompt. "
        "If you moved the augmentation call, also move the attachment "
        "inline block above it."
    )
