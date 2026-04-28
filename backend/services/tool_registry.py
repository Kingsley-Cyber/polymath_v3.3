import builtins
import logging
import multiprocessing
import queue
from typing import Any

from bson import ObjectId
from models.schemas import Tool, ToolCreate, ToolUpdate
from services.conversation import conversation_service

logger = logging.getLogger(__name__)


def _worker(code: str, func_name: str, kwargs: dict, q: multiprocessing.Queue) -> None:
    try:
        b = builtins.__dict__.copy()
        for dangerous in ["open", "eval", "exec", "compile", "input", "exit", "quit"]:
            b.pop(dangerous, None)

        original_import = b.get("__import__")

        def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
            allowed = {"math", "json", "re", "datetime", "time", "urllib", "random"}
            if name.split(".")[0] not in allowed:
                raise ImportError(
                    f"Importing '{name}' is not allowed in sandboxed tools"
                )
            return original_import(name, globals, locals, fromlist, level)

        b["__import__"] = safe_import
        global_env = {"__builtins__": b}
        local_env = {}

        exec(code, global_env, local_env)

        if func_name not in local_env or not callable(local_env[func_name]):
            q.put(
                (
                    "error",
                    f"Error: function '{func_name}' not defined in the tool script.",
                )
            )
            return

        result = local_env[func_name](**kwargs)
        q.put(("success", result))
    except Exception as e:
        q.put(("error", str(e)))


class ToolRegistry:
    """Service for managing and executing custom Python tools."""

    @property
    def collection(self):
        """Get the tools MongoDB collection."""
        if conversation_service._db is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return conversation_service._db["tools"]

    async def list_tools(self) -> list[Tool]:
        """List all registered tools."""
        try:
            cursor = self.collection.find({})
            tools = []
            async for doc in cursor:
                doc["_id"] = str(doc["_id"])
                tools.append(Tool.model_validate(doc))
            return tools
        except Exception as e:
            logger.error(f"Failed to list tools: {e}")
            return []

    async def get_tool(self, tool_id: str) -> Tool | None:
        """Get a specific tool by ID."""
        try:
            doc = await self.collection.find_one({"_id": ObjectId(tool_id)})
            if doc:
                doc["_id"] = str(doc["_id"])
                return Tool.model_validate(doc)
            return None
        except Exception as e:
            logger.error(f"Failed to get tool {tool_id}: {e}")
            return None

    async def get_tools_by_ids(self, tool_ids: list[str]) -> list[Tool]:
        """Get multiple tools by their IDs."""
        if not tool_ids:
            return []
        try:
            object_ids = [ObjectId(tid) for tid in tool_ids if ObjectId.is_valid(tid)]
            cursor = self.collection.find({"_id": {"$in": object_ids}, "enabled": True})
            tools = []
            async for doc in cursor:
                doc["_id"] = str(doc["_id"])
                tools.append(Tool.model_validate(doc))
            return tools
        except Exception as e:
            logger.error(f"Failed to get tools by IDs: {e}")
            return []

    @property
    def _skills_collection(self):
        if conversation_service._db is None:
            raise RuntimeError("Database not connected.")
        return conversation_service._db["skills"]

    async def _check_slash_unique(
        self, slash: str | None, exclude_tool_id: str | None = None
    ) -> None:
        """Phase 24 — slash uniqueness across tools + skills."""
        if slash is None:
            return
        from services.skills_registry import SlashCommandConflict

        tool_q: dict = {"slash_command": slash}
        if exclude_tool_id:
            tool_q["_id"] = {"$ne": ObjectId(exclude_tool_id)}
        if await self.collection.find_one(tool_q):
            raise SlashCommandConflict(
                f"Slash command '{slash}' is already used by another tool."
            )
        if await self._skills_collection.find_one({"slash_command": slash}):
            raise SlashCommandConflict(
                f"Slash command '{slash}' is already used by a skill."
            )

    async def create_tool(self, tool: ToolCreate) -> Tool:
        """Create a new custom tool."""
        try:
            from services.skills_registry import normalize_slash, validate_slash

            doc = tool.model_dump()
            doc["slash_command"] = normalize_slash(doc.get("slash_command"))
            validate_slash(doc["slash_command"])
            await self._check_slash_unique(doc["slash_command"])
            result = await self.collection.insert_one(doc)
            doc["_id"] = str(result.inserted_id)
            return Tool.model_validate(doc)
        except Exception as e:
            logger.error(f"Failed to create tool: {e}")
            raise

    async def update_tool(self, tool_id: str, updates: ToolUpdate) -> Tool | None:
        """Update an existing tool."""
        try:
            from services.skills_registry import normalize_slash, validate_slash

            update_data = {
                k: v for k, v in updates.model_dump().items() if v is not None
            }
            if "slash_command" in update_data:
                update_data["slash_command"] = normalize_slash(update_data["slash_command"])
                validate_slash(update_data["slash_command"])
                await self._check_slash_unique(
                    update_data["slash_command"], exclude_tool_id=tool_id
                )
            if not update_data:
                return await self.get_tool(tool_id)

            await self.collection.update_one(
                {"_id": ObjectId(tool_id)}, {"$set": update_data}
            )
            return await self.get_tool(tool_id)
        except Exception as e:
            logger.error(f"Failed to update tool {tool_id}: {e}")
            raise

    async def delete_tool(self, tool_id: str) -> bool:
        """Delete a tool."""
        try:
            result = await self.collection.delete_one({"_id": ObjectId(tool_id)})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Failed to delete tool {tool_id}: {e}")
            return False

    def execute_tool(self, code: str, func_name: str, kwargs: dict) -> Any:
        """
        Execute a custom Python tool safely in a sandboxed child process.
        """
        q = multiprocessing.Queue()
        p = multiprocessing.Process(target=_worker, args=(code, func_name, kwargs, q))

        try:
            p.start()

            result_tuple = None
            try:
                # Wait for up to 10 seconds for a result on the queue
                result_tuple = q.get(timeout=10.0)
            except queue.Empty:
                pass

            p.join(timeout=0.1)

            if p.is_alive():
                p.terminate()
                p.join()
                return f"Tool execution failed: Timeout after 10.0 seconds."

            if result_tuple:
                status, result = result_tuple
                if status == "error":
                    logger.error(f"Tool execution failed for '{func_name}': {result}")
                    return f"Tool execution failed: {result}"
                return result
            else:
                return "Tool execution failed: Worker process crashed or returned no result."
        except Exception as e:
            logger.error(f"Tool execution encountered an error: {e}")
            return f"Tool execution failed: {str(e)}"


# Singleton instance
tool_registry = ToolRegistry()
