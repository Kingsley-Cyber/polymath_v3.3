"""Vision-capability detector for chat models.

Phase 29 follow-up: when the user attaches images, we must reject the
request with a clear error if the selected model can't process them.
Without this check, LiteLLM forwards the multimodal request to a
non-vision endpoint and the user gets a generic 4xx that looks like
a transport bug.

Mirrors the frontend `supportsVision()` heuristic in
`frontend/src/lib/modelCapabilities.ts`. Keep both lists in sync.

Conservative bias — false positives (claiming vision support a model
doesn't have) are worse than false negatives. When in doubt, return
False and let the user pick a known-vision model explicitly.
"""

from __future__ import annotations

import re
from typing import Iterable


# Pattern table. Each entry: (regex against lowercased model name,
# short label for logs). Substring/regex matches are cheap; we walk
# the list on every attachment-bearing request, so keep them simple.
#
# Curated against published vision-capable models as of 2026-05-15.
# When a new vision model lands, add an entry here AND in the
# frontend supportsVision() heuristic. The two must stay in sync.
_VISION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # OpenAI — GPT-4o family is the main vision line. The older
    # gpt-4-vision-preview is still around; the o-series (o1/o3/o4)
    # supports images natively as of recent updates.
    (re.compile(r"(^|/)gpt-4o(\b|-)"), "openai-gpt4o"),
    (re.compile(r"(^|/)gpt-4-turbo"), "openai-gpt4-turbo"),
    (re.compile(r"(^|/)gpt-4-vision"), "openai-gpt4-vision"),
    (re.compile(r"(^|/)o[134](-|\b)"), "openai-o-series"),

    # Anthropic — every Claude 3+ model supports vision.
    (re.compile(r"claude"), "anthropic-claude"),

    # Google Gemini — 1.5 Pro/Flash and all 2.x lines.
    (re.compile(r"gemini-(1\.5|2\.\d|2-)"), "gemini-vision"),

    # GLM (Z.AI) — vision-specific variants carry a "v" suffix in the
    # version (glm-4.5v, glm-5v-turbo).
    (re.compile(r"glm-(4\.5v|5v)"), "glm-vision"),

    # Mistral — pixtral is the vision line; non-pixtral Mistrals are
    # text-only.
    (re.compile(r"pixtral"), "mistral-pixtral"),

    # Qwen vision (qwen-vl, qwen2-vl, qwen2.5-vl, …).
    (re.compile(r"qwen[\d.]*-vl"), "qwen-vl"),

    # Llama vision (llama-3.2-vision, llama-3.2-90b-vision-instruct,
    # llama-4-maverick, llama-4-scout). Character class allows
    # alphanumerics + dots + hyphens between the family marker and
    # the vision/maverick/scout suffix — e.g., the "90b" parameter
    # tag in llama-3.2-90b-vision.
    (re.compile(r"llama[\w.\-]*(vision|maverick|scout)"), "llama-vision"),
]


def supports_vision(model: str | None) -> bool:
    """True if the model name matches any known vision-capable pattern.

    Strips `pool:` and `profile:` references should they reach us bare;
    callers should already have resolved those upstream (the chat
    orchestrator resolves model_used before this check fires).
    """
    if not model:
        return False
    m = model.lower().strip()
    # Defensive — if someone passes a bare reference, treat as unknown.
    if m.startswith("pool:") or m.startswith("profile:"):
        return False
    for pattern, _label in _VISION_PATTERNS:
        if pattern.search(m):
            return True
    return False


def vision_capable_models_hint() -> str:
    """User-facing hint used in error messages. Short list of common
    picks across providers so the user has somewhere to go."""
    return (
        "Pick a vision-capable model: GPT-4o, Claude 3.5 Sonnet / "
        "Claude 4, Gemini 1.5/2.x, Qwen2-VL, GLM-5V, or Pixtral."
    )


def matched_label(model: str | None) -> str | None:
    """Diagnostic — return the label of the first matching pattern,
    or None when the model has no vision support. Used in logs."""
    if not model:
        return None
    m = model.lower().strip()
    for pattern, label in _VISION_PATTERNS:
        if pattern.search(m):
            return label
    return None


def attachments_include_image(attachments: Iterable | None) -> bool:
    """Helper — True iff at least one attachment is an image. Duck-typed
    so it works with ChatAttachment objects or dicts."""
    if not attachments:
        return False
    for att in attachments:
        kind = getattr(att, "kind", None) or (
            att.get("kind") if isinstance(att, dict) else None
        )
        if kind == "image":
            return True
    return False
