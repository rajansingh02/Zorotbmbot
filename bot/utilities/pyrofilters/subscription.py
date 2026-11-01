import asyncio
import datetime
import logging
from typing import ClassVar

import tzlocal
from lru import LRU
from pyrogram import filters
from pyrogram.client import Client
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserNotParticipant
from pyrogram.types import Message

from bot.config import config
from bot.database import MongoDB

database = MongoDB()


class SubscriptionMessage(Message):
    def __init__(self) -> None:
        self.user_is_banned = False


class SubscriptionFilter:
    """
    Force-subscription filter.

    Successful subscription checks are cached for one hour.
    Failed checks are never cached.

    An active /mpost photo session bypasses FSub because the user
    has already been authorized to start the /mpost workflow.
    """

    logger = logging.getLogger(__name__)

    CACHE_USER_SECONDS: int = 3600

    _subs_cache: ClassVar[LRU] = LRU(10)

    @classmethod
    def clear_cache(cls) -> None:
        """Clear cached subscription results."""
        cls._subs_cache.clear()

    @classmethod
    async def _check_membership(
        cls,
        client: Client,
        user_id: int,
        channel_id: int,
    ) -> bool:
        """
        Check membership.

        Telegram may take a short time to update membership after a user
        joins, so retry several times before rejecting the user.
        """

        for attempt in range(3):
            try:
                member = await client.get_chat_member(
                    chat_id=channel_id,
                    user_id=user_id,
                )

                cls.logger.info(
                    "FSUB CHECK user=%s channel=%s status=%s is_member=%s",
                    user_id,
                    channel_id,
                    member.status,
                    getattr(member, "is_member", None),
                )

                if member.status in (
                    ChatMemberStatus.OWNER,
                    ChatMemberStatus.ADMINISTRATOR,
                    ChatMemberStatus.MEMBER,
                ):
                    return True

                if (
                    member.status == ChatMemberStatus.RESTRICTED
                    and getattr(member, "is_member", False)
                ):
                    return True

                cls.logger.warning(
                    "FSUB NOT MEMBER user=%s channel=%s status=%s",
                    user_id,
                    channel_id,
                    member.status,
                )

                return False

            except UserNotParticipant:
                cls.logger.warning(
                    "FSUB UserNotParticipant user=%s channel=%s attempt=%s",
                    user_id,
                    channel_id,
                    attempt + 1,
                )

                if attempt < 2:
                    await asyncio.sleep(2)
                    continue

                return False

            except Exception:
                cls.logger.exception(
                    "FSUB CHECK ERROR user=%s channel=%s",
                    user_id,
                    channel_id,
                )
                return False

        return False

    @classmethod
    def subscription(cls) -> filters.Filter:
        async def func(
            flt: None,
            client: Client,
            message: SubscriptionMessage,
        ) -> bool:  # noqa: ARG001

            if not message.from_user:
                return False

            user_id = message.from_user.id

            # ---------------------------------------------------------
            # /mpost photo bypass
            # ---------------------------------------------------------
            # Once /mpost has created a pending session, the next
            # private photo belongs to that workflow. Do not run the
            # normal force-subscription check on that photo.
            #
            # Import locally to avoid a circular import during startup.
            # ---------------------------------------------------------
            if (
                message.chat
                and message.chat.type == "private"
                and message.photo
            ):
                from bot.mpost import MPostCommand

                unique_id = message.chat.id + user_id

                if unique_id in MPostCommand.pending_posts:
                    cls.logger.info(
                        "FSUB BYPASS /mpost user=%s",
                        user_id,
                    )
                    return True

            if await database.is_user_banned(user_id):
                message.user_is_banned = True
                return False

            # Root admins bypass FSub.
            if user_id in config.ROOT_ADMINS_ID:
                return True

            # No FSub configured.
            if not config.channels_n_invite:
                return True

            # Successful checks are cached for one hour.
            if user_id in cls._subs_cache:
                user_cache_time = cls._subs_cache.get(user_id)

                current_time = datetime.datetime.now(
                    tz=tzlocal.get_localzone(),
                )

                if user_cache_time and (
                    current_time - user_cache_time
                ) <= datetime.timedelta(
                    seconds=cls.CACHE_USER_SECONDS,
                ):
                    cls.logger.info(
                        "FSUB CACHE HIT user=%s",
                        user_id,
                    )
                    return True

                cls._subs_cache.pop(user_id)

            # Check every configured channel.
            for channel_info in config.channels_n_invite.values():
                channel_id = channel_info["channel_id"]

                cls.logger.info(
                    "FSUB CHECKING user=%s channel=%s title=%s",
                    user_id,
                    channel_id,
                    channel_info.get("title"),
                )

                if await cls._check_membership(
                    client=client,
                    user_id=user_id,
                    channel_id=channel_id,
                ):
                    continue

                # Existing private join-request support.
                if config.PRIVATE_REQUEST:
                    try:
                        joined_request_channel = (
                            await database.user_requested_channels(
                                user_id,
                            )
                        )

                        if channel_id in joined_request_channel:
                            continue

                    except Exception:
                        cls.logger.exception(
                            "FSUB REQUEST CHECK ERROR user=%s channel=%s",
                            user_id,
                            channel_id,
                        )

                cls.logger.warning(
                    "FSUB FAILED user=%s channel=%s",
                    user_id,
                    channel_id,
                )

                return False

            # Only cache after ALL channels pass.
            cls._subs_cache[user_id] = datetime.datetime.now(
                tz=tzlocal.get_localzone(),
            )

            cls.logger.info(
                "FSUB PASSED user=%s",
                user_id,
            )

            return True

        return filters.create(
            func,
            "SubscriptionFilter",
        )
