"""
Phase 24 — Skills registry. Mirrors tool_registry shape; stores skills in
db["skills"] alongside db["tools"]. Skills inject their `instructions` markdown
as a <skills_active> context block on the user turn (see chat_orchestrator).

A skill is *not* a tool — it has no executable code. It's a behavior modifier:
prose instructions that shape how the LLM responds for that turn.

Slash-command uniqueness is enforced across BOTH skills and tools at write time
so the chat-input slash popover can't return ambiguous results.
"""

import logging
import re

from bson import ObjectId
from models.schemas import Skill, SkillCreate, SkillUpdate
from services.conversation import conversation_service

logger = logging.getLogger(__name__)


# Reserved slash commands — built-in chat affordances. Never hand to a skill or tool.
RESERVED_SLASH_COMMANDS = {"/help", "/clear", "/new", "/compact", "/reset"}

# slash_command must start with /, contain only [a-z0-9_-], and be 2-32 chars total.
_SLASH_RE = re.compile(r"^/[a-z0-9_-]{1,31}$")


class SlashCommandConflict(ValueError):
    """Raised when a slash_command collides with another tool or skill."""


def normalize_slash(value: str | None) -> str | None:
    """Strip + lowercase + return None for empty. No coercion of bad shapes."""
    if value is None:
        return None
    v = value.strip().lower()
    return v or None


def validate_slash(value: str | None) -> None:
    """Raise ValueError if value is set but not a valid slash command shape."""
    if value is None:
        return
    if value in RESERVED_SLASH_COMMANDS:
        raise ValueError(f"'{value}' is reserved and cannot be used.")
    if not _SLASH_RE.match(value):
        raise ValueError(
            f"'{value}' is not a valid slash command. Use /lowercase-letters-digits-only, 2-32 chars."
        )


class SkillsRegistry:
    """Service for managing custom skills."""

    @property
    def collection(self):
        if conversation_service._db is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return conversation_service._db["skills"]

    @property
    def _tools_collection(self):
        if conversation_service._db is None:
            raise RuntimeError("Database not connected.")
        return conversation_service._db["tools"]

    async def _check_slash_unique(
        self, slash: str | None, exclude_skill_id: str | None = None
    ) -> None:
        """Ensure slash command is unique across BOTH skills and tools collections."""
        if slash is None:
            return
        # Same-collection collision
        skill_q: dict = {"slash_command": slash}
        if exclude_skill_id:
            skill_q["_id"] = {"$ne": ObjectId(exclude_skill_id)}
        if await self.collection.find_one(skill_q):
            raise SlashCommandConflict(
                f"Slash command '{slash}' is already used by another skill."
            )
        # Cross-collection collision (tools)
        if await self._tools_collection.find_one({"slash_command": slash}):
            raise SlashCommandConflict(
                f"Slash command '{slash}' is already used by a tool."
            )

    async def list_skills(self) -> list[Skill]:
        try:
            cursor = self.collection.find({})
            out = []
            async for doc in cursor:
                doc["_id"] = str(doc["_id"])
                out.append(Skill.model_validate(doc))
            return out
        except Exception as e:
            logger.error(f"Failed to list skills: {e}")
            return []

    async def get_skill(self, skill_id: str) -> Skill | None:
        try:
            doc = await self.collection.find_one({"_id": ObjectId(skill_id)})
            if doc:
                doc["_id"] = str(doc["_id"])
                return Skill.model_validate(doc)
            return None
        except Exception as e:
            logger.error(f"Failed to get skill {skill_id}: {e}")
            return None

    async def get_skills_by_ids(self, skill_ids: list[str]) -> list[Skill]:
        if not skill_ids:
            return []
        try:
            object_ids = [ObjectId(sid) for sid in skill_ids if ObjectId.is_valid(sid)]
            cursor = self.collection.find(
                {"_id": {"$in": object_ids}, "enabled": True}
            )
            out = []
            async for doc in cursor:
                doc["_id"] = str(doc["_id"])
                out.append(Skill.model_validate(doc))
            return out
        except Exception as e:
            logger.error(f"Failed to get skills by ids: {e}")
            return []

    async def create_skill(self, skill: SkillCreate) -> Skill:
        try:
            data = skill.model_dump()
            data["slash_command"] = normalize_slash(data.get("slash_command"))
            validate_slash(data["slash_command"])
            await self._check_slash_unique(data["slash_command"])
            result = await self.collection.insert_one(data)
            data["_id"] = str(result.inserted_id)
            return Skill.model_validate(data)
        except Exception:
            raise

    async def update_skill(
        self, skill_id: str, updates: SkillUpdate
    ) -> Skill | None:
        try:
            patch = {
                k: v for k, v in updates.model_dump().items() if v is not None
            }
            if "slash_command" in patch:
                patch["slash_command"] = normalize_slash(patch["slash_command"])
                validate_slash(patch["slash_command"])
                await self._check_slash_unique(
                    patch["slash_command"], exclude_skill_id=skill_id
                )
            if not patch:
                return await self.get_skill(skill_id)
            await self.collection.update_one(
                {"_id": ObjectId(skill_id)}, {"$set": patch}
            )
            return await self.get_skill(skill_id)
        except Exception as e:
            logger.error(f"Failed to update skill {skill_id}: {e}")
            raise

    async def delete_skill(self, skill_id: str) -> bool:
        try:
            res = await self.collection.delete_one({"_id": ObjectId(skill_id)})
            return res.deleted_count > 0
        except Exception as e:
            logger.error(f"Failed to delete skill {skill_id}: {e}")
            return False


skills_registry = SkillsRegistry()
