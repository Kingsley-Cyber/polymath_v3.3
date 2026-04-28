# backend/routers/auth.py
# Thin auth router — validates input, calls auth_service, returns response.
# No business logic here. All logic lives in services/auth.py.

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from models.schemas import (
    LoginRequest,
    LoginResponse,
    UpdateCredentialsRequest,
    UpdateCredentialsResponse,
    UserMeResponse,
    UserPublic,
)
from services.auth import auth_service
from slowapi import Limiter
from slowapi.util import get_remote_address

logger = logging.getLogger(__name__)

# Phase 17 W1.3 — per-IP rate limiter on auth endpoints.
# Backend: in-memory by default (upgrade to `storage_uri=settings.REDIS_URL`
# for multi-worker deployments once Redis is wired everywhere).
limiter = Limiter(key_func=get_remote_address)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Bearer token extractor — auto-parses Authorization header
security = HTTPBearer(auto_error=True)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict[str, str]:
    """
    FastAPI dependency that extracts and validates the JWT from the
    Authorization: Bearer <token> header.

    Returns:
        dict with user_id and username if token is valid.

    Raises:
        HTTPException 401 if token is missing, malformed, or expired.
    """
    token = credentials.credentials
    payload = auth_service.verify_token(token)

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return {"user_id": payload.user_id, "username": payload.username}


# ────────────────────────────────────────────────────────────────────────────
# POST /api/auth/login
# ────────────────────────────────────────────────────────────────────────────


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Authenticate user and receive JWT token",
)
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest):
    """
    Authenticate with username and password.

    Rate limited (Phase 17 W1.3): 5 attempts per IP per minute. Exceeding the
    limit returns HTTP 429. `request: Request` is required by slowapi to read
    the client IP; `body: LoginRequest` is the actual JSON payload.

    Returns a JWT access token valid for the configured expiration period
    (default 7 days). The token must be sent as `Authorization: Bearer <token>`
    on all protected endpoints.
    """
    user_doc = await auth_service.authenticate_user(
        username=body.username, password=body.password
    )

    if user_doc is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_service.create_access_token(
        user_id=str(user_doc["_id"]),
        username=user_doc["username"],
    )

    logger.info(f"User logged in: {user_doc['username']}")

    return LoginResponse(
        access_token=token,
        token_type="bearer",
        user=UserPublic(
            id=str(user_doc["_id"]),
            username=user_doc["username"],
            created_at=user_doc["created_at"],
        ),
    )


# ────────────────────────────────────────────────────────────────────────────
# GET /api/auth/me
# ────────────────────────────────────────────────────────────────────────────


@router.get(
    "/me",
    response_model=UserMeResponse,
    summary="Get current authenticated user info",
)
async def get_me(current_user: dict = Depends(get_current_user)):
    """
    Verify the current token is valid and return user info.

    Requires a valid Bearer token in the Authorization header.
    Used by the frontend to validate stored tokens on app load.
    """
    user_doc = await auth_service.get_user_by_id(current_user["user_id"])

    if user_doc is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return UserMeResponse(
        id=str(user_doc["_id"]),
        username=user_doc["username"],
        created_at=user_doc["created_at"],
    )


# ────────────────────────────────────────────────────────────────────────────
# PATCH /api/auth/update
# ────────────────────────────────────────────────────────────────────────────


@router.patch(
    "/update",
    response_model=UpdateCredentialsResponse,
    summary="Update username and/or password",
)
async def update_credentials(
    request: UpdateCredentialsRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Update the authenticated user's username and/or password.

    Requires the current password for verification.
    Returns a fresh JWT token (the old one is still valid until expiry,
    but the frontend should replace it immediately).
    """
    # Verify current password
    user_doc = await auth_service.authenticate_user(
        username=current_user["username"],
        password=request.current_password,
    )

    if user_doc is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )

    # Ensure at least one field is being updated
    if request.new_username is None and request.new_password is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide new_username and/or new_password",
        )

    # Apply updates — service raises ValueError on bad input
    try:
        updated_user = await auth_service.update_credentials(
            user_id=current_user["user_id"],
            current_password=request.current_password,
            new_username=request.new_username,
            new_password=request.new_password,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    # Issue fresh token with updated username
    fresh_token = auth_service.create_access_token(
        user_id=str(updated_user["_id"]),
        username=updated_user["username"],
    )

    logger.info(f"Credentials updated for user: {updated_user['username']}")

    return UpdateCredentialsResponse(
        success=True,
        access_token=fresh_token,
        user=UserPublic(
            id=str(updated_user["_id"]),
            username=updated_user["username"],
            created_at=updated_user["created_at"],
        ),
    )
