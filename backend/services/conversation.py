# backend/services/conversation.py
# Conversation + message CRUD via async MongoDB (Motor).
#
# Collections:
#   conversations  — metadata only (title, created_at, updated_at, model_config)
#   messages       — all messages with conversation_id FK
#
# All functions are async. Import: from services.conversation import conversation_service

import logging
from datetime import datetime
from typing import Optional

from bson import ObjectId
from config import get_settings
from models.schemas import ChatMessage, Conversation, ConversationListItem, ModelConfig
from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
)

logger = logging.getLogger(__name__)
settings = get_settings()


class ConversationService:
    """Async MongoDB service for conversation + message CRUD (separate collections)."""

    def __init__(self) -> None:
        self._client: Optional[AsyncIOMotorClient] = None
        self._db: Optional[AsyncIOMotorDatabase] = None
        self._conversations: Optional[AsyncIOMotorCollection] = None
        self._messages: Optional[AsyncIOMotorCollection] = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        """Connect to MongoDB. Call on app startup."""
        if self._client is not None:
            logger.warning("MongoDB client already connected")
            return

        try:
            self._client = AsyncIOMotorClient(settings.MONGODB_URI)
            self._db = self._client[settings.MONGODB_DATABASE]
            self._conversations = self._db["conversations"]
            self._messages = self._db["messages"]

            # Indexes are created centrally via db/indexes.py on startup,
            # but we still verify connection here.
            await self._client.admin.command("ping")
            logger.info("Connected to MongoDB successfully")
        except Exception as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            raise

    async def disconnect(self) -> None:
        """Disconnect from MongoDB. Call on app shutdown."""
        if self._client is not None:
            self._client.close()
            self._client = None
            self._db = None
            self._conversations = None
            self._messages = None
            logger.info("Disconnected from MongoDB")

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    @property
    def collection(self) -> AsyncIOMotorCollection:
        """conversations collection — raises if not connected."""
        if self._conversations is None:
            raise RuntimeError("MongoDB not connected. Call connect() first.")
        return self._conversations

    @property
    def messages_collection(self) -> AsyncIOMotorCollection:
        """messages collection — raises if not connected."""
        if self._messages is None:
            raise RuntimeError("MongoDB not connected. Call connect() first.")
        return self._messages

    # ------------------------------------------------------------------ #
    # Conversation CRUD                                                    #
    # ------------------------------------------------------------------ #

    async def create_conversation(
        self,
        title: str = "New Conversation",
        model_config: Optional[ModelConfig] = None,
    ) -> str:
        """
        Create a new conversation document (no messages embedded).

        Returns:
            String ID of the created conversation.
        """
        if model_config is None:
            model_config = ModelConfig()

        now = datetime.utcnow()
        doc = {
            "title": title,
            "created_at": now,
            "updated_at": now,
            "model_config": model_config.model_dump(),
        }

        result = await self.collection.insert_one(doc)
        conversation_id = str(result.inserted_id)
        logger.info(f"Created conversation: {conversation_id}")
        return conversation_id

    async def create_conversation_with_message(
        self,
        title: str,
        message: ChatMessage,
        model_config: Optional[ModelConfig] = None,
    ) -> str:
        """
        Create a conversation and persist its first user message.

        Sequential (no transaction) — compatible with standalone MongoDB.
        If the message insert fails, the conversation is orphaned but recoverable.

        Returns:
            String ID of the created conversation.
        """
        conversation_id = await self.create_conversation(title, model_config)
        await self.append_message(conversation_id, message)
        return conversation_id

    async def get_conversation(self, conversation_id: str) -> Optional[Conversation]:
        """
        Get a conversation by ID, with messages populated from the messages collection.

        Returns:
            Conversation object (messages populated) or None if not found.
        """
        try:
            doc = await self.collection.find_one({"_id": ObjectId(conversation_id)})
        except Exception as e:
            logger.error(f"Invalid conversation ID: {conversation_id} — {e}")
            return None

        if doc is None:
            logger.debug(f"Conversation not found: {conversation_id}")
            return None

        doc["_id"] = str(doc["_id"])

        # Populate messages from the separate collection
        messages = await self.get_messages(conversation_id)
        doc["messages"] = [msg.model_dump() for msg in messages]

        return Conversation.model_validate(doc)

    async def list_conversations(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConversationListItem]:
        """
        List conversations with pagination, sorted by most recent.

        Uses aggregation to fetch message count + last message preview
        in a single round-trip.
        """
        pipeline = [
            {"$sort": {"updated_at": -1}},
            {"$skip": offset},
            {"$limit": limit},
            # Last message (for preview)
            {
                "$lookup": {
                    "from": "messages",
                    "let": {"cid": "$_id"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {"$eq": ["$conversation_id", "$$cid"]}
                            }
                        },
                        {"$sort": {"created_at": -1}},
                        {"$limit": 1},
                        {"$project": {"content": 1, "_id": 0}},
                    ],
                    "as": "last_msg",
                }
            },
            # Message count only (no full docs loaded)
            {
                "$lookup": {
                    "from": "messages",
                    "let": {"cid": "$_id"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {"$eq": ["$conversation_id", "$$cid"]}
                            }
                        },
                        {"$count": "n"},
                    ],
                    "as": "msg_count",
                }
            },
            {
                "$project": {
                    "_id": 1,
                    "title": 1,
                    "created_at": 1,
                    "updated_at": 1,
                    "last_msg": 1,
                    "msg_count": 1,
                }
            },
        ]

        conversations = []
        async for doc in self.collection.aggregate(pipeline):
            # Extract message count
            count_arr = doc.get("msg_count", [])
            message_count = count_arr[0]["n"] if count_arr else 0

            # Extract last message preview
            last_msgs = doc.get("last_msg", [])
            last_message_preview: Optional[str] = None
            if last_msgs:
                content = last_msgs[0].get("content", "")
                last_message_preview = (
                    content[:100] + "..." if len(content) > 100 else content
                )

            conversations.append(
                ConversationListItem(
                    _id=str(doc["_id"]),
                    title=doc.get("title", "Untitled"),
                    created_at=doc.get("created_at", datetime.utcnow()),
                    updated_at=doc.get("updated_at", datetime.utcnow()),
                    message_count=message_count,
                    last_message_preview=last_message_preview,
                )
            )

        return conversations

    async def update_conversation_title(
        self,
        conversation_id: str,
        title: str,
    ) -> bool:
        """Update conversation title. Returns True if updated."""
        try:
            result = await self.collection.update_one(
                {"_id": ObjectId(conversation_id)},
                {"$set": {"title": title, "updated_at": datetime.utcnow()}},
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Failed to update title for {conversation_id}: {e}")
            return False

    async def update_model_config(
        self,
        conversation_id: str,
        model_config: ModelConfig,
    ) -> bool:
        """Update conversation model configuration. Returns True if updated."""
        try:
            result = await self.collection.update_one(
                {"_id": ObjectId(conversation_id)},
                {
                    "$set": {
                        "model_config": model_config.model_dump(),
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Failed to update model config for {conversation_id}: {e}")
            return False

    async def delete_conversation(self, conversation_id: str) -> bool:
        """
        Delete a conversation and cascade-delete all its messages.

        Returns:
            True if the conversation was deleted, False if not found.
        """
        try:
            obj_id = ObjectId(conversation_id)

            # Cascade: remove all messages for this conversation first
            deleted_msgs = await self.messages_collection.delete_many(
                {"conversation_id": obj_id}
            )
            logger.debug(
                f"Cascade-deleted {deleted_msgs.deleted_count} messages "
                f"for conversation {conversation_id}"
            )

            result = await self.collection.delete_one({"_id": obj_id})
            deleted = result.deleted_count > 0
            if deleted:
                logger.info(f"Deleted conversation: {conversation_id}")
            return deleted
        except Exception as e:
            logger.error(f"Failed to delete conversation {conversation_id}: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Message CRUD (messages collection)                                   #
    # ------------------------------------------------------------------ #

    async def append_message(
        self,
        conversation_id: str,
        message: ChatMessage,
        session=None,  # kept for call-site compatibility; not used (standalone MongoDB)
    ) -> bool:
        """
        Insert a single message into the messages collection and
        bump conversation.updated_at.

        Returns:
            True on success, False on error.
        """
        try:
            obj_id = ObjectId(conversation_id)
            msg_doc = message.model_dump()
            msg_doc["conversation_id"] = obj_id

            await self.messages_collection.insert_one(msg_doc)
            await self.collection.update_one(
                {"_id": obj_id},
                {"$set": {"updated_at": datetime.utcnow()}},
            )
            return True
        except Exception as e:
            logger.error(f"Failed to append message to {conversation_id}: {e}")
            return False

    async def append_messages(
        self,
        conversation_id: str,
        messages: list[ChatMessage],
    ) -> bool:
        """
        Bulk-insert multiple messages into the messages collection.

        Returns:
            True on success, False on error.
        """
        if not messages:
            return True

        try:
            obj_id = ObjectId(conversation_id)
            docs = []
            for msg in messages:
                doc = msg.model_dump()
                doc["conversation_id"] = obj_id
                docs.append(doc)

            await self.messages_collection.insert_many(docs)
            await self.collection.update_one(
                {"_id": obj_id},
                {"$set": {"updated_at": datetime.utcnow()}},
            )
            return True
        except Exception as e:
            logger.error(f"Failed to bulk-append messages to {conversation_id}: {e}")
            return False

    async def append_messages_batch(
        self,
        conversation_id: str,
        messages: list[ChatMessage],
    ) -> bool:
        """Alias for append_messages — kept for call-site compatibility."""
        return await self.append_messages(conversation_id, messages)

    async def get_messages(
        self,
        conversation_id: str,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[ChatMessage]:
        """
        Retrieve messages for a conversation, ordered by created_at ASC.

        Args:
            conversation_id: Conversation ObjectId as string.
            limit:  Optional cap on number of messages returned.
            offset: Number of messages to skip (for pagination).

        Returns:
            List of ChatMessage objects.
        """
        try:
            obj_id = ObjectId(conversation_id)
            cursor = (
                self.messages_collection.find({"conversation_id": obj_id})
                .sort("created_at", 1)
                .skip(offset)
            )
            if limit is not None:
                cursor = cursor.limit(limit)

            result: list[ChatMessage] = []
            async for doc in cursor:
                doc.pop("_id", None)
                doc.pop("conversation_id", None)
                result.append(ChatMessage(**doc))
            return result
        except Exception as e:
            logger.error(f"Failed to get messages for {conversation_id}: {e}")
            return []

    async def get_message_count(self, conversation_id: str) -> int:
        """Return the number of messages in a conversation."""
        try:
            return await self.messages_collection.count_documents(
                {"conversation_id": ObjectId(conversation_id)}
            )
        except Exception as e:
            logger.error(f"Failed to count messages for {conversation_id}: {e}")
            return 0

    # ------------------------------------------------------------------ #
    # Health                                                               #
    # ------------------------------------------------------------------ #

    async def health_check(self) -> bool:
        """Check MongoDB connection health."""
        try:
            if self._client is None:
                return False
            await self._client.admin.command("ping")
            return True
        except Exception as e:
            logger.error(f"MongoDB health check failed: {e}")
            return False


# Singleton instance for app-wide use
conversation_service = ConversationService()
