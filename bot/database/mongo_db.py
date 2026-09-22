import time
from datetime import datetime, timezone

import dns.resolver
from async_lru import alru_cache
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import ConfigurationError, DuplicateKeyError

from bot.config import config

from .listener import Listener
from .moderation import Moderation


class MongoDB(Moderation, Listener):
    """
    MongoDB connections.

    Cluster 1:
        Files collection only.

    Cluster 2:
        Everything else.
    """

    def __init__(self, name: str | None = None) -> None:
        """
        Initialize both MongoDB connections.

        Cluster 1 is used exclusively for Files.
        Cluster 2 is used for all other collections.
        """

        database_name = name or config.MONGO_DB_NAME
        files_database_name = config.MONGO_FILES_DB_NAME

        try:
            self.client = AsyncIOMotorClient(
                host=str(config.MONGO_DB_URL),
            )
            self.files_client = AsyncIOMotorClient(
                host=str(config.MONGO_FILES_DB_URL),
            )

        except ConfigurationError:
            dns.resolver.default_resolver = dns.resolver.Resolver(
                configure=False,
            )
            dns.resolver.default_resolver.nameservers = ["8.8.8.8"]

            self.client = AsyncIOMotorClient(
                host=str(config.MONGO_DB_URL),
            )
            self.files_client = AsyncIOMotorClient(
                host=str(config.MONGO_FILES_DB_URL),
            )

        # Cluster 2: everything except Files.
        self.db = self.client[database_name]

        # Cluster 1: Files only.
        self.files_db = self.files_client[files_database_name]

    @alru_cache(maxsize=10, ttl=10)
    async def add_user(self, user_id: int) -> bool:
        """Adds a user to Cluster 2."""
        collection = self.db["Users"]

        result = await collection.update_one(
            filter={"_id": user_id},
            update={"$set": {"_id": user_id}},
            upsert=True,
        )

        return result.acknowledged

    async def check_daily_file_limit(
        self,
        user_id: int,
        limit: int,
        window_seconds: int,
    ) -> tuple[bool, int, float]:
        """
        Atomically check and increment a user's rolling file-link limit.

        The data is stored in Cluster 2 through self.db.

        Each user gets `limit` requests during a rolling
        `window_seconds` period.
        """

        collection = self.db["RateLimits"]

        now = time.time()
        window_start = now - window_seconds

        expired_result = await collection.update_one(
            {
                "_id": user_id,
                "window_start": {"$lt": window_start},
            },
            {
                "$set": {
                    "count": 1,
                    "window_start": now,
                },
            },
        )

        if expired_result.modified_count:
            return True, 1, now + window_seconds

        active_result = await collection.update_one(
            {
                "_id": user_id,
                "window_start": {"$gte": window_start},
                "count": {"$lt": limit},
            },
            {
                "$inc": {
                    "count": 1,
                },
            },
        )

        if active_result.modified_count:
            document = await collection.find_one(
                {"_id": user_id},
                {"count": 1, "window_start": 1},
            )

            if document:
                count = int(document.get("count", 1))
                stored_window_start = float(
                    document.get("window_start", now),
                )

                return (
                    True,
                    count,
                    stored_window_start + window_seconds,
                )

        try:
            await collection.insert_one(
                {
                    "_id": user_id,
                    "count": 1,
                    "window_start": now,
                },
            )

            return True, 1, now + window_seconds

        except DuplicateKeyError:
            retry_result = await collection.update_one(
                {
                    "_id": user_id,
                    "window_start": {"$gte": window_start},
                    "count": {"$lt": limit},
                },
                {
                    "$inc": {
                        "count": 1,
                    },
                },
            )

            if retry_result.modified_count:
                document = await collection.find_one(
                    {"_id": user_id},
                    {"count": 1, "window_start": 1},
                )

                if document:
                    count = int(
                        document.get("count", 1),
                    )
                    stored_window_start = float(
                        document.get(
                            "window_start",
                            now,
                        ),
                    )

                    return (
                        True,
                        count,
                        stored_window_start + window_seconds,
                    )

        document = await collection.find_one(
            {"_id": user_id},
            {"count": 1, "window_start": 1},
        )

        if document:
            count = int(
                document.get("count", limit),
            )
            stored_window_start = float(
                document.get("window_start", now),
            )

            return (
                False,
                count,
                stored_window_start + window_seconds,
            )

        return False, limit, now + window_seconds

    async def reset_daily_file_limit(
        self,
        user_id: int | None = None,
    ) -> int:
        """
        Reset daily file-link rate limits from Cluster 2.

        If user_id is provided, only that user's limit is reset.
        If user_id is None, all users' limits are reset.

        Returns:
            int: Number of rate-limit records deleted.
        """

        collection = self.db["RateLimits"]

        if user_id is not None:
            result = await collection.delete_one(
                {"_id": user_id},
            )

            return result.deleted_count

        result = await collection.delete_many({})

        return result.deleted_count

    async def add_file(
        self,
        file_link: str,
        file_origin: int,
        file_data: list[dict[str, str | int]],
        temporary: bool = False,
        expires_at: datetime | None = None,
    ) -> bool:
        """Adds a file link to Cluster 1."""

        collection = self.files_db["Files"]

        file_document = {
            "file_origin": file_origin,
            "files": file_data,
        }

        if temporary and expires_at is not None:
            file_document["temporary"] = True
            file_document["expires_at"] = expires_at

        result = await collection.update_one(
            filter={"_id": file_link},
            update={"$set": file_document},
            upsert=True,
        )

        return result.acknowledged

    async def delete_link_document(
        self,
        base64_file_link: str,
    ) -> bool:
        """Deletes a file link from Cluster 1."""

        collection = self.files_db["Files"]

        result = await collection.delete_one(
            filter={"_id": base64_file_link},
        )

        return result.deleted_count > 0

    async def get_link_document(
        self,
        base64_file_link: str,
    ) -> dict | None:
        """Retrieves a file link from Cluster 1."""

        collection = self.files_db["Files"]

        pipeline = [
            {
                "$match": {
                    "_id": base64_file_link,
                },
            },
        ]

        result = await collection.aggregate(
            pipeline,
        ).to_list(length=None)

        return result[0] if result else None
        
    async def get_link_by_backup_message_id(
        self,
        message_id: int,
        backup_channel: int,
    ) -> tuple[str, dict] | None:
        """Find an existing link by backup-channel message ID."""
        collection = self.files_db["Files"]

        result = await collection.find_one(
            {
                "file_origin": backup_channel,
                "files": {
                    "$elemMatch": {
                        "message_id": message_id,
                    },
                },
            },
        )

        if not result:
            return None

        return str(result["_id"]), result

    async def get_expired_temporary_links(
        self,
        now: datetime | None = None,
    ) -> list[dict]:
        """
        Retrieve expired temporary links from Cluster 1.
        """

        collection = self.files_db["Files"]

        current_time = now or datetime.now(timezone.utc)

        return await collection.find(
            {
                "temporary": True,
                "expires_at": {
                    "$lte": current_time,
                },
            },
        ).to_list(length=100)

    async def add_fsub_channel(
        self,
        channel_info: dict,
    ) -> bool:
        """Add or update a force-subscription channel in Cluster 2."""

        collection = self.db["ForceSubChannels"]

        result = await collection.update_one(
            {"_id": channel_info["channel_id"]},
            {"$set": channel_info},
            upsert=True,
        )

        return result.acknowledged

    async def remove_fsub_channel(
        self,
        channel_id: int,
    ) -> bool:
        """Remove a force-subscription channel from Cluster 2."""

        collection = self.db["ForceSubChannels"]

        result = await collection.delete_one(
            {"_id": channel_id},
        )

        return result.deleted_count > 0

    async def get_fsub_channels(self) -> list[dict]:
        """Return all configured force-subscription channels."""

        collection = self.db["ForceSubChannels"]

        return await collection.find({}).to_list(length=None)

    async def get_fsub_channel(
        self,
        channel_id: int,
    ) -> dict | None:
        """Return one force-subscription channel."""

        collection = self.db["ForceSubChannels"]

        return await collection.find_one(
            {"_id": channel_id},
        )

    async def clear_fsub_channels(self) -> int:
        """Remove all force-subscription channels."""

        collection = self.db["ForceSubChannels"]

        result = await collection.delete_many({})

        return result.deleted_count

    async def add_ad_channel(
        self,
        channel_info: dict,
    ) -> bool:
        """Add or update a post/ads channel (used by /mpost) in Cluster 2."""

        collection = self.db["AdChannels"]

        result = await collection.update_one(
            {"_id": channel_info["channel_id"]},
            {"$set": channel_info},
            upsert=True,
        )

        return result.acknowledged

    async def remove_ad_channel(
        self,
        channel_id: int,
    ) -> bool:
        """Remove a post/ads channel from Cluster 2."""

        collection = self.db["AdChannels"]

        result = await collection.delete_one(
            {"_id": channel_id},
        )

        return result.deleted_count > 0

    async def get_ad_channels(self) -> list[dict]:
        """Return all configured post/ads channels."""

        collection = self.db["AdChannels"]

        return await collection.find({}).to_list(length=None)

    async def get_ad_channel(
        self,
        channel_id: int,
    ) -> dict | None:
        """Return one post/ads channel."""

        collection = self.db["AdChannels"]

        return await collection.find_one(
            {"_id": channel_id},
        )

    async def clear_ad_channels(self) -> int:
        """Remove all post/ads channels."""

        collection = self.db["AdChannels"]

        result = await collection.delete_many({})

        return result.deleted_count

    async def save_mpost_preset(
        self,
        name: str,
        template: str,
    ) -> bool:
        """Save (or overwrite) a named /mpost button-template preset."""

        collection = self.db["MPostPresets"]

        result = await collection.update_one(
            {"_id": name},
            {"$set": {"template": template}},
            upsert=True,
        )

        return result.acknowledged

    async def get_mpost_preset(
        self,
        name: str,
    ) -> dict | None:
        """Return one named /mpost button-template preset."""

        collection = self.db["MPostPresets"]

        return await collection.find_one({"_id": name})

    async def get_mpost_presets(self) -> list[dict]:
        """Return all saved /mpost button-template presets."""

        collection = self.db["MPostPresets"]

        return await collection.find({}).to_list(length=None)

    async def delete_mpost_preset(
        self,
        name: str,
    ) -> bool:
        """Delete a named /mpost button-template preset."""

        collection = self.db["MPostPresets"]

        result = await collection.delete_one({"_id": name})

        return result.deleted_count > 0

    async def get_user_ids(
        self,
    ) -> tuple[list[int], list[int]]:
        """
        Retrieves the IDs of all users from Cluster 2.

        Returns:
            tuple[list[int], list[int]]: Two lists of user IDs.
        """

        pipeline = [
            {"$project": {"_id": 1}},
            {
                "$group": {
                    "_id": None,
                    "user_ids": {"$addToSet": "$_id"},
                },
            },
            {"$project": {"_id": 0, "user_ids": 1}},
        ]

        users_collection = self.db["Users"]
        users_codex_collection = self.db["users"]

        user_ids_cursor = users_collection.aggregate(
            pipeline,
        )
        user_ids = await user_ids_cursor.to_list(
            length=None,
        )

        user_ids_codex_cursor = users_codex_collection.aggregate(
            pipeline,
        )
        user_ids_codex = await user_ids_codex_cursor.to_list(
            length=None,
        )

        main_ids = (
            user_ids[0]["user_ids"]
            if user_ids
            else []
        )

        codex_ids = (
            user_ids_codex[0]["user_ids"]
            if user_ids_codex
            else []
        )

        return (main_ids, codex_ids)

    async def stats(self) -> tuple[int, int]:
        """
        Retrieves link count from Cluster 1 and user count
        from Cluster 2.
        """

        link_count = await self.files_db["Files"].count_documents({})
        users_count = await self.db["Users"].count_documents({})

        return (link_count, users_count)

    async def cleanup_users(
        self,
        unsuccessful_ids: list,
        unsuccessful_ids_codex: list,
    ) -> None:
        """Cleans up users from Cluster 2."""

        if unsuccessful_ids:
            await self.db["Users"].delete_many(
                {"_id": {"$in": unsuccessful_ids}},
            )

        if unsuccessful_ids_codex:
            await self.db["users"].delete_many(
                {"_id": {"$in": unsuccessful_ids_codex}},
            )
