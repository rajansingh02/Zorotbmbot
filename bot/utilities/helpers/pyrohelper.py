from typing import Any, TypedDict, cast

from pyrogram import raw
from pyrogram.client import Client
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserIsBlocked
from pyrogram.types import InlineKeyboardMarkup, Message

from bot.config import ChannelInfo, config


class NoInviteLinkError(Exception):
    def __init__(self, channel: int | str) -> None:
        super().__init__(f"{channel} has no invite link")


class BotNotAdminError(Exception):
    def __init__(self, channel: int | str) -> None:
        super().__init__(f"Bot is not an administrator in {channel}")


class CustomCaption(TypedDict):
    text: str | None
    inelinekeyboardmarkup: InlineKeyboardMarkup | None


class PyroHelper:
    """Helper class for additional Pyrogram functions."""

    @staticmethod
    async def get_channel_info(
        client: Client,
        channel_id: int | str,
    ) -> ChannelInfo:
        """
        Get channel information and a usable join link.

        The bot must be an administrator in the channel.
        Public channels use their public username as the join link.
        Private channels use an exported invite link.
        """
        channel = await client.get_chat(chat_id=channel_id)

        bot = await client.get_me()

        try:
            member = await client.get_chat_member(
                chat_id=channel.id,
                user_id=bot.id,
            )
        except Exception as e:
            raise BotNotAdminError(channel.title) from e

        if member.status not in (
            ChatMemberStatus.OWNER,
            ChatMemberStatus.ADMINISTRATOR,
        ):
            raise BotNotAdminError(channel.title)

        username = getattr(channel, "username", None)

        if username:
            invite_link = f"https://t.me/{username}"
            is_private = False
        else:
            try:
                get_link = await client.invoke(
                    raw.functions.messages.ExportChatInvite(
                        peer=await client.resolve_peer(
                            peer_id=channel.id,
                        ),
                        legacy_revoke_permanent=True,
                        request_needed=config.PRIVATE_REQUEST,
                    ),
                )
            except Exception as e:
                raise NoInviteLinkError(channel.id) from e

            if get_link is None:
                raise NoInviteLinkError(channel.id)

            invite_link = get_link.link
            is_private = True

        return ChannelInfo(
            title=channel.title,
            is_private=is_private,
            invite_link=invite_link,
            channel_id=channel.id,
        )

    @staticmethod
    async def get_channel_invites(
        client: Client,
        channels: list[int],
    ) -> dict[str, ChannelInfo]:
        """
        Get invite links for a list of channels.

        Parameters:
            client (Client):
                Pyrogram client instance.
            channels (list[int]):
                List of channel IDs to get invite links for.

        Returns:
            dict[str, ChannelInfo]:
                Dictionary with channel titles as keys and ChannelInfo.

        Raises:
            ValueError:
                If any channel in the list does not have an invite link.
        """
        if not channels:
            return {}

        channels_n_invite: dict[str, ChannelInfo] = {}

        for channel_id in channels:
            channel = await client.get_chat(chat_id=channel_id)
            get_link = await client.invoke(
                raw.functions.messages.ExportChatInvite(
                    peer=await client.resolve_peer(
                        peer_id=channel_id,
                    ),
                    legacy_revoke_permanent=True,
                    request_needed=config.PRIVATE_REQUEST,
                ),
            )

            if get_link is not None:
                channel_invite = get_link.link

                if channel.title not in channels_n_invite:
                    channels_n_invite[channel.title] = ChannelInfo(
                        title=channel.title,
                        is_private=bool(channel.username is None),
                        invite_link=channel_invite,
                        channel_id=channel_id,
                    )
            else:
                raise NoInviteLinkError(channel_id)

        return channels_n_invite

    @staticmethod
    async def option_message(
        client: Client,
        message: Message,
        option_key: str | int,
        **kwargs: Any,
    ) -> Message | None:
        if isinstance(option_key, int):
            message_origin = await client.get_messages(
                chat_id=config.BACKUP_CHANNEL,
                message_ids=option_key,
            )

            if message_origin:
                return cast(
                    "Message",
                    await message_origin.copy(
                        chat_id=message.chat.id,
                        **kwargs,
                    ),
                )

        try:
            return await message.reply(
                text=str(option_key),
                **kwargs,
            )
        except UserIsBlocked:
            return None

    @staticmethod
    async def custom_caption(
        client: Client,
        option_key: str | int,
    ) -> CustomCaption:

        if str(option_key).isdigit() and str(option_key) != "0":
            message_origin = await client.get_messages(
                chat_id=config.BACKUP_CHANNEL,
                message_ids=int(option_key),
            )

            message = (
                message_origin[0]
                if isinstance(message_origin, list)
                else message_origin
            )

            return CustomCaption(
                text=message.text.markdown if message.text else None,
                inelinekeyboardmarkup=message.reply_markup
                if isinstance(message.reply_markup, InlineKeyboardMarkup)
                else None,
            )

        caption = None if option_key == 0 else str(option_key)

        return CustomCaption(
            text=caption,
            inelinekeyboardmarkup=None,
        )
