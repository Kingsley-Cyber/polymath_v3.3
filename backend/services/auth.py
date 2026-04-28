# backend/services/auth.py
# Authentication service: JWT tokens, password hashing, zero-user bootstrap
# All functions are async. Import: from services.auth import auth_service

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId
from config import get_settings
from jose import JWTError, jwt
from models.schemas import TokenData, UserPublic
from passlib.context import CryptContext

logger = logging.getLogger(__name__)

# Password hashing context — bcrypt via passlib (already in requirements.txt)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class AuthService:
    """
    Handles authentication: JWT tokens, password hashing, user management.

    Usage:
        auth_service.connect(db)    # during app lifespan startup
        auth_service.bootstrap()    # creates default admin if no users exist
    """

    def __init__(self) -> None:
        self._db = None
        self._users_collection = None

    async def connect(self, db: Any) -> None:
        """
        Initialize with MongoDB database reference.

        Args:
            db: AsyncIOMotorDatabase instance from Motor client
        """
        self._db = db
        self._users_collection = db["users"]
        logger.info("AuthService connected to MongoDB")

    async def disconnect(self) -> None:
        """Cleanup database references."""
        self._db = None
        self._users_collection = None

    # ------------------------------------------------------------------
    # Zero-User Bootstrapper
    # ------------------------------------------------------------------

    async def bootstrap(self) -> None:
        """
        Zero-user bootstrapper: if the users collection is empty,
        automatically create a default admin account from .env settings.

        Called once during application startup (lifespan).
        Safe to call on every restart — skips if users already exist.
        """
        if self._users_collection is None:
            logger.error("AuthService not connected — call connect() first")
            return

        user_count = await self._users_collection.count_documents({})

        if user_count == 0:
            settings = get_settings()
            hashed_password = pwd_context.hash(settings.DEFAULT_ADMIN_PASSWORD)

            admin_doc = {
                "username": settings.DEFAULT_ADMIN_USERNAME,
                "hashed_password": hashed_password,
                "created_at": datetime.now(timezone.utc),
            }

            result = await self._users_collection.insert_one(admin_doc)
            logger.info(
                f"Zero-user bootstrap: created default admin "
                f"'{settings.DEFAULT_ADMIN_USERNAME}' (id={result.inserted_id})"
            )
        else:
            logger.info(
                f"Users collection has {user_count} document(s) — skipping bootstrap"
            )

    # ------------------------------------------------------------------
    # JWT Token Operations
    # ------------------------------------------------------------------

    def create_access_token(self, user_id: str, username: str) -> str:
        """
        Create a signed JWT access token.

        Args:
            user_id: MongoDB user document _id as string
            username: Username to encode in token payload

        Returns:
            Encoded JWT string
        """
        settings = get_settings()
        expires = datetime.now(timezone.utc) + timedelta(
            days=settings.AUTH_TOKEN_EXPIRE_DAYS
        )

        payload = {
            "sub": user_id,
            "username": username,
            "exp": expires,
        }

        return jwt.encode(
            payload, settings.AUTH_SECRET_KEY, algorithm=settings.AUTH_ALGORITHM
        )

    def verify_token(self, token: str) -> TokenData | None:
        """
        Verify and decode a JWT token.

        Args:
            token: JWT token string (without 'Bearer ' prefix)

        Returns:
            TokenData if valid, None if expired or malformed
        """
        settings = get_settings()
        try:
            payload = jwt.decode(
                token,
                settings.AUTH_SECRET_KEY,
                algorithms=[settings.AUTH_ALGORITHM],
            )
            user_id: str | None = payload.get("sub")
            username: str | None = payload.get("username")

            if user_id is None or username is None:
                return None

            exp_timestamp = payload.get("exp")
            return TokenData(
                user_id=user_id,
                username=username,
                exp=(
                    datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)
                    if exp_timestamp
                    else None
                ),
            )
        except JWTError:
            return None

    # ------------------------------------------------------------------
    # User Authentication
    # ------------------------------------------------------------------

    async def authenticate_user(
        self, username: str, password: str
    ) -> dict[str, Any] | None:
        """
        Authenticate a user by username and password.

        Args:
            username: Username to look up
            password: Plain-text password to verify against hash

        Returns:
            User document dict if authenticated, None if not found or wrong password
        """
        if self._users_collection is None:
            return None

        user = await self._users_collection.find_one({"username": username})
        if user is None:
            return None

        if not pwd_context.verify(password, user["hashed_password"]):
            return None

        return user

    async def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        """
        Fetch a user document by its MongoDB _id.

        Args:
            user_id: User ID as string

        Returns:
            User document dict or None if not found / invalid ID
        """
        if self._users_collection is None:
            return None

        if not ObjectId.is_valid(user_id):
            return None

        return await self._users_collection.find_one({"_id": ObjectId(user_id)})

    # ------------------------------------------------------------------
    # Credential Updates
    # ------------------------------------------------------------------

    async def update_credentials(
        self,
        user_id: str,
        current_password: str,
        new_username: str | None = None,
        new_password: str | None = None,
    ) -> dict[str, Any]:
        """
        Update user credentials. Requires current password verification.

        Args:
            user_id: User ID string
            current_password: Current password for verification
            new_username: Optional new username (skipped if None/empty)
            new_password: Optional new password (skipped if None)

        Returns:
            Updated user document dict

        Raises:
            ValueError: If current password is wrong, username is taken, or nothing to update
        """
        if self._users_collection is None:
            raise ValueError("AuthService not connected")

        if not ObjectId.is_valid(user_id):
            raise ValueError("Invalid user ID")

        user = await self._users_collection.find_one({"_id": ObjectId(user_id)})
        if user is None:
            raise ValueError("User not found")

        # Verify current password
        if not pwd_context.verify(current_password, user["hashed_password"]):
            raise ValueError("Current password is incorrect")

        # Build update fields
        update_fields: dict[str, Any] = {}

        if new_username is not None and new_username.strip():
            # Check if username is taken by another user
            existing = await self._users_collection.find_one(
                {"username": new_username.strip(), "_id": {"$ne": ObjectId(user_id)}}
            )
            if existing is not None:
                raise ValueError(f"Username '{new_username.strip()}' is already taken")
            update_fields["username"] = new_username.strip()

        if new_password is not None:
            update_fields["hashed_password"] = pwd_context.hash(new_password)

        if not update_fields:
            raise ValueError(
                "No changes provided — supply new_username or new_password"
            )

        # Perform atomic update
        await self._users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_fields},
        )

        # Return fresh document
        updated = await self._users_collection.find_one({"_id": ObjectId(user_id)})
        logger.info(f"Credentials updated for user {user_id}")
        return updated

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def user_to_public(self, user_doc: dict[str, Any]) -> UserPublic:
        """
        Convert a raw MongoDB user document to a UserPublic response model.
        Strips hashed_password and other internal fields.

        Args:
            user_doc: Raw MongoDB document from users collection

        Returns:
            UserPublic Pydantic model
        """
        return UserPublic(
            id=str(user_doc["_id"]),
            username=user_doc["username"],
            created_at=user_doc["created_at"],
        )


# Singleton instance — imported everywhere as: from services.auth import auth_service
auth_service = AuthService()
