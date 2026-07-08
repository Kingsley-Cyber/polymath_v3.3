"""MCP auth bridge — JWT validation + static API key reused from services.auth.

Per Plan_V3_1.md Phase 8.4 + API key extension:
  - Extract `Authorization: Bearer <token>` from MCP request headers
  - Try MCP_API_KEY first (constant-time compare). On match → SYSTEM_USER_ID
    (sees all corpora; no per-user scoping).
  - Else verify as JWT via services.auth.verify_token. On match → real user_id
    (corpus_ids filtered to that user's owned set).
  - On both fail and MCP_REQUIRE_AUTH=True: middleware returns MCP "auth.unauthorized".
  - Tools then resolve user_id → allowed_corpus_ids via mongo_reader.list_corpora,
    silent-drop disallowed corpus_ids before calling the service.

Per-request user_id is propagated via a contextvars.ContextVar so tool functions
read it without explicit threading.
"""
from __future__ import annotations

import hmac
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from config import get_settings
from services.auth import auth_service
from services.conversation import conversation_service
from services.storage.mongo_reader import list_corpora

logger = logging.getLogger(__name__)

# Sentinel user_id for requests authenticated via the static MCP_API_KEY rather
# than a per-user JWT. System-level access — bypasses per-user corpus scoping.
SYSTEM_USER_ID = "__mcp_system__"

# Per-request authenticated user_id. None when MCP_REQUIRE_AUTH=False and request
# arrived without a token (single-user dev mode).
_current_user_id: ContextVar[Optional[str]] = ContextVar(
    "mcp_current_user_id", default=None
)
_current_scopes: ContextVar[frozenset[str] | None] = ContextVar(
    "mcp_current_scopes", default=None
)

ALL_MCP_SCOPES = frozenset({"read", "write", "admin"})
DEFAULT_USER_MCP_SCOPES = frozenset({"read", "write"})


@dataclass(frozen=True)
class MCPAuthContext:
    user_id: Optional[str]
    scopes: frozenset[str]


class AuthError(Exception):
    """Raised when an MCP request fails JWT validation. The transport layer
    converts this into an MCP-shaped error response."""


def _normalize_scopes(scopes: list[str] | tuple[str, ...] | set[str] | None) -> frozenset[str]:
    allowed = set(ALL_MCP_SCOPES)
    normalized = {
        str(scope or "").strip().lower()
        for scope in (scopes or DEFAULT_USER_MCP_SCOPES)
        if str(scope or "").strip().lower() in allowed
    }
    normalized.add("read")
    return frozenset(normalized or DEFAULT_USER_MCP_SCOPES)


def _default_scopes_for_user(user_id: Optional[str]) -> frozenset[str]:
    if user_id == SYSTEM_USER_ID:
        return ALL_MCP_SCOPES
    if user_id is None:
        return ALL_MCP_SCOPES
    return ALL_MCP_SCOPES


def set_current_user_id(user_id: Optional[str], scopes: list[str] | tuple[str, ...] | set[str] | None = None) -> None:
    """Stash the authenticated user_id for the duration of this request."""
    _current_user_id.set(user_id)
    _current_scopes.set(
        _normalize_scopes(scopes) if scopes is not None else _default_scopes_for_user(user_id)
    )


def set_current_auth_context(auth: MCPAuthContext | None) -> None:
    """Stash the authenticated subject and MCP scopes for this request."""
    if auth is None:
        set_current_user_id(None)
        return
    _current_user_id.set(auth.user_id)
    _current_scopes.set(auth.scopes)


def get_current_user_id() -> Optional[str]:
    """Read the user_id stashed by the auth middleware (or None when not set)."""
    return _current_user_id.get()


def get_current_scopes() -> frozenset[str]:
    """Read current MCP scopes, deriving legacy defaults for old test paths."""
    scopes = _current_scopes.get()
    if scopes is not None:
        return scopes
    return _default_scopes_for_user(get_current_user_id())


def require_mcp_scope(scope: str) -> None:
    """Raise AuthError when the current MCP key lacks a required scope."""
    required = str(scope or "").strip().lower()
    if required and required not in get_current_scopes():
        raise AuthError(f"MCP key lacks required scope: {required}")


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Parse 'Authorization: Bearer <token>' header. Returns None when missing
    or malformed."""
    if not authorization_header:
        return None
    parts = authorization_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def validate_api_key(token: str | None) -> bool:
    """Constant-time compare against MCP_API_KEY (when configured).

    Returns True only when both:
      - MCP_API_KEY is set in env / config
      - `token` matches it byte-for-byte
    Uses hmac.compare_digest to prevent timing-based key recovery.
    """
    if not token:
        return False
    settings = get_settings()
    expected = settings.MCP_API_KEY
    if not expected:
        return False
    # Encode to bytes; compare_digest requires equal-length operands but is
    # safe against partial-match early-exit timing attacks.
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


def validate_token(token: str | None) -> Optional[str]:
    """Validate the bearer token against API key first, then JWT.

    Returns:
        - SYSTEM_USER_ID when the token matches MCP_API_KEY (system-level access)
        - real user_id string when the token is a valid JWT
        - None when both checks fail or no token was provided

    Honors settings.MCP_REQUIRE_AUTH:
      - True (default): missing/invalid token → returns None (caller should reject)
      - False: missing token allowed → returns None but caller should NOT reject;
              tools will see user_id=None and skip the per-user corpus filter
    """
    if not token:
        return None
    if validate_api_key(token):
        return SYSTEM_USER_ID
    token_data = auth_service.verify_token(token)
    if token_data is None:
        return None
    return token_data.user_id


async def validate_token_async(token: str | None) -> Optional[str]:
    """Validate bearer token, including database-backed user MCP keys.

    The sync `validate_token` path handles the legacy static MCP_API_KEY and
    normal JWTs. User-generated MCP keys live in MongoDB, so the HTTP
    middleware calls this async wrapper.
    """
    auth = await validate_token_context_async(token)
    return auth.user_id if auth else None


async def validate_token_context_async(token: str | None) -> MCPAuthContext | None:
    """Validate bearer token and return the authenticated subject + scopes."""
    if not token:
        return None
    if validate_api_key(token):
        return MCPAuthContext(SYSTEM_USER_ID, ALL_MCP_SCOPES)
    token_data = auth_service.verify_token(token)
    if token_data is not None:
        # JWTs are first-party app sessions, not shareable MCP keys. Preserve
        # historical full capability for the interactive user.
        return MCPAuthContext(token_data.user_id, ALL_MCP_SCOPES)
    if not token:
        return None
    db = conversation_service._db
    if db is None:
        logger.warning("MCP auth: MongoDB not connected; cannot validate user key")
        return None
    try:
        from .key_store import validate_user_mcp_key_details

        details = await validate_user_mcp_key_details(db, token)
        if not details:
            return None
        return MCPAuthContext(
            str(details["user_id"]),
            _normalize_scopes(details.get("scopes")),
        )
    except Exception as exc:
        logger.warning("MCP auth: user key validation failed: %s", exc)
        return None


async def allowed_corpus_ids(user_id: Optional[str]) -> set[str]:
    """Resolve user_id → set of allowed corpus_ids.

    Three modes:
      - user_id == SYSTEM_USER_ID → return ALL corpus_ids (API-key auth path).
      - user_id == None           → return ALL corpus_ids ONLY when MCP_REQUIRE_AUTH
                                    is False (trusted-network single-user dev mode).
                                    Otherwise the middleware would have rejected
                                    the request before reaching this point.
      - real user_id              → return only corpora owned by that user.
    """
    db = conversation_service._db
    if db is None:
        logger.warning("MCP auth: MongoDB not connected; cannot resolve corpora")
        return set()
    if user_id == SYSTEM_USER_ID or user_id is None:
        # System / dev mode: list all corpora across all owners.
        all_corpora = await list_corpora(db, user_id=None)
        return {c["corpus_id"] for c in all_corpora}
    user_corpora = await list_corpora(db, user_id=user_id)
    return {c["corpus_id"] for c in user_corpora}


def filter_corpus_ids(
    requested: list[str] | None, allowed: set[str]
) -> list[str]:
    """Silent-drop filter: return only the corpus_ids in both `requested` and
    `allowed`. Per Phase 8.4: silent drop, not error."""
    if not requested:
        return []
    filtered = [cid for cid in requested if cid in allowed]
    dropped = [cid for cid in requested if cid not in allowed]
    if dropped:
        logger.info("MCP auth: silent-dropped %d disallowed corpus_ids", len(dropped))
    return filtered


async def resolve_request_scope(requested_corpus_ids: list[str] | None) -> list[str]:
    """One-shot helper for tool functions: pull current user_id, resolve allowed
    corpora, filter the requested list, return the safe subset."""
    settings = get_settings()
    user_id = get_current_user_id()
    if user_id is None and settings.MCP_REQUIRE_AUTH:
        # Tool was invoked without prior auth — should not happen if middleware
        # is wired correctly, but fail-closed.
        raise AuthError("MCP request missing valid authentication")
    allowed = await allowed_corpus_ids(user_id)
    return filter_corpus_ids(requested_corpus_ids, allowed)


async def assert_corpus_allowed(corpus_id: str) -> None:
    """For single-corpus tools: raise AuthError when corpus is outside the
    user's allowed set. Caller should let the exception bubble to the MCP
    error response."""
    settings = get_settings()
    user_id = get_current_user_id()
    if user_id is None and settings.MCP_REQUIRE_AUTH:
        raise AuthError("MCP request missing valid authentication")
    allowed = await allowed_corpus_ids(user_id)
    if corpus_id not in allowed:
        raise AuthError(f"corpus_id {corpus_id!r} is not accessible to this user")
