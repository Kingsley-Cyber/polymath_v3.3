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


def get_model_context_limit(model: str) -> int:
    """
    Get the context window limit for a known model.

    Args:
        model: Model name (e.g., "gpt-4", "ollama/llama3.2:3b")

    Returns:
        Context window size in tokens, or default 4096 if unknown
    """
    model_name = model.split("/")[-1] if "/" in model else model

    # Known model context limits
    CONTEXT_LIMITS = {
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
        # Local LFM2 fine-tunes served by vLLM at 12288 tokens (matches the
        # --max-model-len Polymath ships in compose). Frontend chip-add no
        # longer asks for ctx — auto-detection from this registry handles it.
        "lfm2-extract": 12288,
        "lfm2-rag": 12288,
        "lfm2-1.2B-Extract": 12288,
        "lfm2-1.2B-RAG": 12288,
        "lfm2-1.2B-Instruct": 12288,
        "gemma4-e4b": 8192,
        # Embedding models (typically not used for chat)
        "nomic-embed-text": 8192,
    }

    # LiteLLM provider prefixes (for example openai/lfm2-extract) are routing
    # hints, not model context identifiers.
    normalized_model_name = model_name.split("/", 1)[1] if "/" in model_name else model_name

    # Try exact match first
    if normalized_model_name in CONTEXT_LIMITS:
        return CONTEXT_LIMITS[normalized_model_name]

    # Try prefix match
    for known_model, limit in CONTEXT_LIMITS.items():
        if normalized_model_name.startswith(known_model.split(":")[0]):
            return limit

    # Default fallback
    return 4096
