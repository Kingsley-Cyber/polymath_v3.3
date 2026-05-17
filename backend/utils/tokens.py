# backend/utils/tokens.py
# Token counting only. No business logic.
# Uses tiktoken for cloud models, fallback char/4 estimate for local models.

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Cache for tiktoken encoders to avoid repeated lookups
_encoder_cache: dict[str, object] = {}


def prewarm() -> None:
    """Phase 24 perf — exercise count_tokens at startup so the FIRST chat
    turn doesn't pay the 1-4s tiktoken cold-start when count_tokens runs
    on the assistant message. Calls through the natural path so the
    `_encoder_cache` keys match what subsequent runtime calls look up.
    Idempotent and non-fatal."""
    try:
        # Touch one cl100k model so the encoder loads + seeds the cache.
        # Subsequent count_tokens(..., "gpt-4o") / "gpt-3.5-turbo" / etc.
        # will hit the cache. For non-cl100k models (Mistral, Claude) the
        # function falls back to the char/4 estimator — also exercised here.
        count_tokens("warmup", "gpt-4o")
        count_tokens("warmup", "mistral-large")
        logger.info("tiktoken pre-warmed via natural path")
    except Exception as exc:
        logger.warning("tiktoken prewarm skipped: %s", exc)

# Models that use cl100k_base encoding (GPT-4, GPT-3.5-turbo, etc.)
CL100K_MODELS = [
    "gpt-4",
    "gpt-4-turbo",
    "gpt-3.5-turbo",
    "gpt-4o",
    "gpt-4o-mini",
]

# Models that use p50k_base encoding (GPT-3, Codex, etc.)
P50K_MODELS = [
    "text-davinci-003",
    "text-davinci-002",
    "code-davinci-002",
]


def _get_encoder(model: str) -> Optional[object]:
    """
    Get the appropriate tiktoken encoder for a model.

    Args:
        model: Model name (e.g., "gpt-4", "ollama/llama3.2:3b")

    Returns:
        tiktoken encoder or None if model is not supported
    """
    try:
        import tiktoken
    except ImportError:
        logger.warning("tiktoken not installed, using fallback token counting")
        return None

    # Strip provider prefix for matching
    model_name = model.split("/")[-1] if "/" in model else model

    # Check cache first
    if model_name in _encoder_cache:
        return _encoder_cache[model_name]

    # Try to match model to encoding
    encoder = None

    # Check for cl100k_base models (GPT-4, GPT-3.5-turbo)
    for prefix in CL100K_MODELS:
        if model_name.startswith(prefix):
            try:
                encoder = tiktoken.get_encoding("cl100k_base")
                break
            except Exception as e:
                logger.debug(f"Failed to get cl100k_base encoding: {e}")

    # Check for p50k_base models (GPT-3, Codex)
    if encoder is None:
        for prefix in P50K_MODELS:
            if model_name.startswith(prefix):
                try:
                    encoder = tiktoken.get_encoding("p50k_base")
                    break
                except Exception as e:
                    logger.debug(f"Failed to get p50k_base encoding: {e}")

    # Try to get encoding directly from model name
    if encoder is None:
        try:
            encoder = tiktoken.encoding_for_model(model_name)
        except Exception:
            # Model not recognized by tiktoken, use cl100k_base as default
            try:
                encoder = tiktoken.get_encoding("cl100k_base")
            except Exception as e:
                logger.debug(f"Failed to get default encoding: {e}")

    # Cache the encoder (even if None)
    _encoder_cache[model_name] = encoder
    return encoder


def count_tokens(text: str, model: str = "gpt-4") -> int:
    """
    Count the number of tokens in a text string.

    Args:
        text: The text to count tokens for
        model: Model name to determine encoding

    Returns:
        Number of tokens

    Examples:
        >>> count_tokens("Hello, world!", "gpt-4")
        4
        >>> count_tokens("Hello, world!", "ollama/llama3.2:3b")
        5  # Fallback: char/4 estimate
    """
    if not text:
        return 0

    encoder = _get_encoder(model)

    if encoder is not None:
        try:
            return len(encoder.encode(text))
        except Exception as e:
            logger.warning(f"tiktoken encode failed, using fallback: {e}")

    # Fallback: estimate tokens as characters / 4
    # This is a rough approximation for local models
    return max(1, len(text) // 4)


def count_tokens_messages(messages: list[dict], model: str = "gpt-4") -> int:
    """
    Count tokens in a list of messages (OpenAI format).

    Accounts for message formatting overhead (role, separators, etc.)

    Args:
        messages: List of message dicts with 'role' and 'content' keys
        model: Model name to determine encoding

    Returns:
        Total token count including formatting overhead
    """
    if not messages:
        return 0

    # Tokens per message overhead (role, separators)
    # Based on OpenAI's token counting guide
    TOKENS_PER_MESSAGE = (
        4  # Every message follows <|start|>{role/name}\n{content}<|end|>\n
    )
    TOKENS_PER_REPLY = 3  # Every reply is primed with <|start|>assistant<|message|>

    total = 0

    for message in messages:
        total += TOKENS_PER_MESSAGE

        # Count role tokens
        role = message.get("role", "")
        total += count_tokens(role, model)

        # Count content tokens
        content = message.get("content", "")
        total += count_tokens(content, model)

    # Add reply priming tokens
    total += TOKENS_PER_REPLY

    return total


def estimate_max_messages(
    messages: list[dict],
    max_tokens: int,
    model: str = "gpt-4",
    reserve_tokens: int = 500,
) -> int:
    """
    Estimate how many messages from the end of the list fit within token budget.

    Args:
        messages: List of message dicts (oldest to newest)
        max_tokens: Maximum context window size
        model: Model name for token counting
        reserve_tokens: Tokens to reserve for response

    Returns:
        Number of messages that fit (counting from the end)
    """
    if not messages:
        return 0

    available_tokens = max_tokens - reserve_tokens
    used_tokens = 0
    messages_fit = 0

    # Count from newest to oldest
    for message in reversed(messages):
        message_tokens = count_tokens_messages([message], model)
        if used_tokens + message_tokens > available_tokens:
            break
        used_tokens += message_tokens
        messages_fit += 1

    return messages_fit


# Phase 29 — per-provider image-token costs. These are upper-bound
# estimates published by each provider (so we under-budget rather than
# over-budget the text). When the model is unknown, fall back to the
# Claude figure since it's the highest — better to trim text aggressively
# than overflow the context window.
#
# Sources:
#   OpenAI GPT-4o/Vision: ~85 base + 170 per 512x512 tile, capped per image
#                         at ~1k for low-res mode and ~2k for high-res
#   Anthropic Claude:     flat ~1600 tokens per image
#   Google Gemini 1.5+:   ~258 tokens per image (low) up to ~512 (high)
#   DeepSeek V4 vision:   unspecified; budget conservatively at 1200
#   GLM (Z.AI) vision:    unspecified; budget conservatively at 1200
_PROVIDER_IMAGE_TOKEN_COST: dict[str, int] = {
    "openai":    1200,
    "anthropic": 1600,
    "google":    400,
    "gemini":    400,
    "deepseek":  1200,
    "zai":       1200,
}
_DEFAULT_IMAGE_TOKEN_COST = 1600  # safest conservative default


def estimate_attachment_tokens(
    attachments: list | None,
    model: str = "gpt-4",
) -> int:
    """Estimate the per-turn LLM-token cost of chat attachments.

    Text attachments contribute their actual UTF-8 content token count
    (since they get inlined into the augmented prompt and tiktoken can
    count them directly). Image attachments contribute a provider-aware
    flat estimate — image tokens vary with resolution + provider but
    we don't know the rendered resolution at request-build time, so
    we pick an upper-bound per provider.

    Returns 0 when `attachments` is None or empty.

    The chat orchestrator adds this to user_message.token_count BEFORE
    invoking the history trimmer, so the trimmer sees the right total
    budget and trims older messages to make room.
    """
    if not attachments:
        return 0
    provider = ""
    if "/" in model:
        provider = model.split("/", 1)[0].lower()
    image_cost_per = _PROVIDER_IMAGE_TOKEN_COST.get(
        provider, _DEFAULT_IMAGE_TOKEN_COST
    )
    total = 0
    for att in attachments:
        # Duck-typed: works with both Pydantic ChatAttachment objects
        # and dicts (so callers can use this with raw request payloads
        # for pre-flight estimates without instantiating the model).
        kind = getattr(att, "kind", None) or (
            att.get("kind") if isinstance(att, dict) else None
        )
        if kind == "image":
            total += image_cost_per
        elif kind == "text":
            content = getattr(att, "content", None) or (
                att.get("content") if isinstance(att, dict) else ""
            )
            # Text files use real tiktoken counting — the body lands
            # inside the augmented prompt verbatim.
            total += count_tokens(content or "", model)
            # Also account for the <attached_file name="…"> wrapper
            # tag overhead. ~20 tokens is a safe over-estimate.
            total += 20
    return total


def _normalized_model_name(model: str) -> str:
    return (model or "").split("/")[-1].lower()


def _lookup_model_limit(model: str, limits: dict[str, int], default: int) -> int:
    model_name = _normalized_model_name(model)

    if model_name in limits:
        return limits[model_name]

    # Match provider-prefixed or provider-suffixed aliases conservatively.
    for known_model, limit in limits.items():
        known = known_model.lower()
        if model_name.startswith(known) or known in model_name:
            return limit

    return default


def get_model_context_limit(model: str) -> int:
    """
    Get the context window limit for a known model.

    Args:
        model: Model name (e.g., "gpt-4", "ollama/llama3.2:3b")

    Returns:
        Context window size in tokens, or default 4096 if unknown
    """
    # Known model context limits
    CONTEXT_LIMITS = {
        # DeepSeek
        # User/provider advertised capability for the active v4 flash profile.
        "deepseek-v4-flash": 1_000_000,
        # OpenAI models
        "gpt-4": 8192,
        "gpt-4-32k": 32768,
        "gpt-4-turbo": 128000,
        "gpt-4o": 128000,
        "gpt-4o-mini": 128000,
        "gpt-3.5-turbo": 16385,
        "gpt-3.5-turbo-16k": 16385,
        # Anthropic models
        "claude-3-opus": 200000,
        "claude-3-sonnet": 200000,
        "claude-3-haiku": 200000,
        # Ollama models (common defaults)
        "llama3.2:1b": 131072,
        "llama3.2:3b": 131072,
        "llama3.1:8b": 131072,
        "llama3.1:70b": 131072,
        "qwen2.5:0.5b": 32768,
        "qwen2.5:1.5b": 32768,
        "qwen2.5:3b": 32768,
        "qwen2.5:7b": 131072,
        "mistral:7b": 32768,
        "gemma:2b": 8192,
        # Embedding models (typically not used for chat)
        "nomic-embed-text": 8192,
    }

    return _lookup_model_limit(model, CONTEXT_LIMITS, 4096)


def get_model_output_limit(model: str) -> int:
    """Return the provider-advertised maximum output tokens when known."""

    OUTPUT_LIMITS = {
        # DeepSeek
        # User/provider advertised maximum for the active v4 flash profile.
        "deepseek-v4-flash": 384_000,
        # Common OpenAI caps. These are intentionally broad guardrails only;
        # providers remain the source of truth and may enforce stricter limits.
        "gpt-4": 8192,
        "gpt-4-32k": 8192,
        "gpt-4-turbo": 4096,
        "gpt-4o": 16_384,
        "gpt-4o-mini": 16_384,
        "gpt-3.5-turbo": 4096,
        "gpt-3.5-turbo-16k": 4096,
    }

    return _lookup_model_limit(model, OUTPUT_LIMITS, 32_000)
