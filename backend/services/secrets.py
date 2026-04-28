"""
Secrets service — Fernet-encrypted storage for user-provided API keys.

Phase 19.2 — closes PLAN_UNIFIED.md §2.4 "API Key Security Flow".

Keys are persisted as ciphertext in the MongoDB `settings` collection under
`api_keys.<provider>`. At read time, the router returns MASKED values only
(`sk-...abc4` → last 4 chars). Decryption happens in-process at LLM call
time and the plaintext is immediately passed to LiteLLM as `api_key` param.
Never logged, never round-tripped to the frontend.

Encryption key precedence:
  1. APP_ENCRYPTION_KEY env var — if set, used directly (must be a valid
     urlsafe base64-encoded 32-byte Fernet key)
  2. Derived from AUTH_SECRET_KEY — SHA-256(auth_secret) → urlsafe_b64encode
     (32 bytes). Stable across restarts as long as AUTH_SECRET_KEY doesn't
     change. Rotating AUTH_SECRET_KEY invalidates all stored API keys.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Known providers whose keys we manage. Extend as needed — the storage is
# schemaless, but having an allowlist keeps typos from creating orphan fields.
# Phase 19.3 — added mistral, kimi, minimax, mimo (and their *-coding variants),
# z_ai (for glm-coding/*). Match each entry to the LiteLLM route prefix exactly
# so `_provider_for_model` in llm.py finds the right user key.
KNOWN_PROVIDERS = frozenset(
    {
        "openai",
        "deepseek",
        "anthropic",
        "gemini",
        "openrouter",
        "modal",
        "siliconflow",
        # Phase 19.3 providers (match config.yaml wildcard route prefixes)
        "mistral",
        "kimi",
        "minimax",
        "mimo",
        "mimo-coding",
        "glm-coding",
        # Sprint 3 — QueryModelPoolEntry.provider coverage. Added so
        # `use_shared_key` entries can look up by provider name.
        # (Per-entry Fernet keys bypass this allowlist.)
        "moonshot",
        "groq",
        "together",
        "custom",
        "ollama",
        # Modal CLI auth — stored like API keys, used only by the verify-token
        # route (modal token info). Never injected into LiteLLM routing.
        "modal_token_id",
        "modal_token_secret",
    }
)


def _derive_key_from_auth_secret() -> bytes:
    """
    Deterministic Fernet key derived from AUTH_SECRET_KEY.

    Uses SHA-256 of the secret, then urlsafe-base64-encodes the 32-byte
    digest to produce a valid Fernet key. Stable across restarts so keys
    written yesterday still decrypt today.
    """
    auth_secret = os.getenv("AUTH_SECRET_KEY", "").strip()
    if not auth_secret:
        raise RuntimeError(
            "Cannot derive encryption key: AUTH_SECRET_KEY is not set. "
            "Set APP_ENCRYPTION_KEY directly, or ensure AUTH_SECRET_KEY is configured."
        )
    digest = hashlib.sha256(auth_secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _get_fernet() -> Fernet:
    """
    Lazy-build the Fernet cipher. Resolves the key on every call because
    APP_ENCRYPTION_KEY may change between test runs; in production the cost
    is one hash + one base64.
    """
    explicit = os.getenv("APP_ENCRYPTION_KEY", "").strip()
    if explicit:
        try:
            return Fernet(explicit.encode("utf-8"))
        except (ValueError, Exception) as exc:
            raise RuntimeError(
                f"APP_ENCRYPTION_KEY is not a valid Fernet key: {exc}. "
                "Generate one with `python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`."
            )
    return Fernet(_derive_key_from_auth_secret())


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext secret. Empty string passes through as empty."""
    if not plaintext:
        return ""
    token = _get_fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt(ciphertext: str) -> str | None:
    """
    Decrypt. Returns None on any failure (wrong key, corrupted, missing).
    Callers fall back to env-var keys on None.
    """
    if not ciphertext:
        return None
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.warning(
            "API key ciphertext failed to decrypt — likely AUTH_SECRET_KEY rotated "
            "since the key was stored. Re-save the key from Settings."
        )
        return None
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("Unexpected decrypt failure: %s", exc)
        return None


def mask(plaintext: str | None) -> str:
    """
    Frontend-safe masked display of a secret. Returns 'sk-...XXXX' showing
    provider prefix and last 4 chars, or '[not set]' when missing.
    """
    if not plaintext:
        return "[not set]"
    if len(plaintext) <= 8:
        return "••••" + plaintext[-2:]
    # Preserve short prefix up to the first '-' if present (sk-, sk-ant-, etc.)
    head_end = plaintext.find("-", 2) + 1 if plaintext.startswith("sk") else 3
    head_end = max(head_end, 3)
    head_end = min(head_end, 8)
    return f"{plaintext[:head_end]}••••{plaintext[-4:]}"


def validate_provider(name: str) -> None:
    """Raises ValueError if provider isn't one we manage."""
    if name not in KNOWN_PROVIDERS:
        raise ValueError(
            f"Unknown provider '{name}'. Valid: {sorted(KNOWN_PROVIDERS)}"
        )


def decrypt_all(stored: dict[str, Any] | None) -> dict[str, str]:
    """
    Decrypt every provider's stored ciphertext. Returns dict of provider →
    plaintext (only for providers whose decrypt succeeded). Used by the
    LLM client wrapper to pick the right key at request time.
    """
    out: dict[str, str] = {}
    if not stored:
        return out
    for provider, ciphertext in stored.items():
        if provider not in KNOWN_PROVIDERS:
            continue
        if not isinstance(ciphertext, str) or not ciphertext:
            continue
        plaintext = decrypt(ciphertext)
        if plaintext:
            out[provider] = plaintext
    return out
