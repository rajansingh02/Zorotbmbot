from pyrogram import filters
from pyrogram.client import Client
from pyrogram.errors import (
    ChannelInvalid,
    ChannelPrivate,
    ChatAdminRequired,
    RPCError,
)
from pyrogram.types import Message

from bot.database import MongoDB
from bot.utilities.helpers import (
    BotNotAdminError,
    NoInviteLinkError,
    PyroHelper,
)
from bot.utilities.pyrofilters import PyroFilters
from bot.utilities.pyrotools import HelpCmd

from .fsub import get_channel_id


class AdChannelCommand:
    """Management commands for channels /mpost can send posts to."""

    database = MongoDB()

    @classmethod
    async def add_channel(
        cls,
        client: Client,
        message: Message,
        channel_id: int | str,
    ) -> Message:
        """Add or update a post channel."""
        try:
            channel_info = await PyroHelper.get_channel_info(
                client=client,
                channel_id=channel_id,
            )
        except BotNotAdminError:
            return await message.reply(
                "❌ **Bot is not an admin in this channel.**\n\n"
                "Please add the bot as an administrator in the channel "
                "and try again.",
                quote=True,
            )
        except NoInviteLinkError:
            return await message.reply(
                "❌ **Couldn't get an invite link for this channel.**\n\n"
                "Make sure the bot has permission to manage the channel "
                "and create invite links.",
                quote=True,
            )
        except (
            ChannelInvalid,
            ChannelPrivate,
            ChatAdminRequired,
        ):
            return await message.reply(
                "❌ **The bot cannot access this channel.**\n\n"
                "Make sure the bot is a member and an administrator "
                "of the channel.",
                quote=True,
            )
        except RPCError as e:
            return await message.reply(
                "❌ **Telegram rejected the channel.**\n\n"
                f"`{e}`",
                quote=True,
            )
        except Exception:
            return await message.reply(
                "❌ **Couldn't add this channel.**\n\n"
                "Make sure the channel ID or username is correct and "
                "the bot is an administrator there.",
                quote=True,
            )

        await cls.database.add_ad_channel(channel_info)

        return await message.reply(
            "✅ **Post channel added.**\n\n"
            f"**Channel:** {channel_info['title']}\n"
            f"**ID:** `{channel_info['channel_id']}`\n\n"
            "It will now show up as a destination button in /mpost.",
            quote=True,
        )

    @classmethod
    async def list_channels(cls, message: Message) -> Message:
        """List configured post channels."""
        channels = await cls.database.get_ad_channels()

        if not channels:
            return await message.reply(
                "ℹ️ No post channels are configured.",
                quote=True,
            )

        lines = ["**Post channels:**\n"]

        for index, channel_info in enumerate(channels, start=1):
            lines.append(
                f"**{index}. {channel_info['title']}**\n"
                f"ID: `{channel_info['channel_id']}`\n",
            )

        return await message.reply(
            "\n".join(lines),
            quote=True,
        )

    @classmethod
    async def remove_channel(
        cls,
        message: Message,
        channel_id: int,
    ) -> Message:
        """Remove a post channel."""
        removed = await cls.database.remove_ad_channel(channel_id)

        if not removed:
            return await message.reply(
                "❌ That channel is not configured as a post channel.",
                quote=True,
            )

        return await message.reply(
            "✅ **Post channel removed.**\n\n"
            f"ID: `{channel_id}`",
            quote=True,
        )

    @classmethod
    async def clear_channels(cls, message: Message) -> Message:
        """Remove all post channels."""
        count = await cls.database.clear_ad_channels()

        if count == 0:
            return await message.reply(
                "ℹ️ No post channels were configured.",
                quote=True,
            )

        return await message.reply(
            f"✅ Removed **{count}** post channel(s).",
            quote=True,
        )


@Client.on_message(
    filters.private
    & filters.command("adpchannel")
    & PyroFilters.admin()
)
async def adpchannel_add_handler(
    client: Client,
    message: Message,
) -> Message:
    """Add a channel that /mpost can send posts to.

    **Usage:**
        `/adpchannel -1001234567890`
        `/adpchannel @publicchannel`

    Or reply to a message forwarded from the channel with `/adpchannel`.
    """
    channel_id = get_channel_id(message)

    if channel_id is None:
        return await message.reply(
            "Usage:\n"
            "`/adpchannel -1001234567890`\n"
            "`/adpchannel @publicchannel`\n\n"
            "Or reply to a forwarded channel post with `/adpchannel`.",
            quote=True,
        )

    return await AdChannelCommand.add_channel(
        client=client,
        message=message,
        channel_id=channel_id,
    )


@Client.on_message(
    filters.private
    & filters.command("adpchannel_list")
    & PyroFilters.admin()
)
async def adpchannel_list_handler(
    client: Client,  # noqa: ARG001
    message: Message,
) -> Message:
    """List channels /mpost can send posts to."""
    return await AdChannelCommand.list_channels(message)


@Client.on_message(
    filters.private
    & filters.command("adpchannel_remove")
    & PyroFilters.admin()
)
async def adpchannel_remove_handler(
    client: Client,  # noqa: ARG001
    message: Message,
) -> Message:
    """Remove a post channel.

    **Usage:** `/adpchannel_remove -1001234567890`
    """
    channel_id = get_channel_id(message)

    if not isinstance(channel_id, int):
        return await message.reply(
            "Usage:\n`/adpchannel_remove -1001234567890`",
            quote=True,
        )

    return await AdChannelCommand.remove_channel(
        message=message,
        channel_id=channel_id,
    )


@Client.on_message(
    filters.private
    & filters.command("adpchannel_clear")
    & PyroFilters.admin()
)
async def adpchannel_clear_handler(
    client: Client,  # noqa: ARG001
    message: Message,
) -> Message:
    """Remove all post channels."""
    if (
        len(message.command) < 2
        or message.command[1].lower() != "confirm"
    ):
        return await message.reply(
            "⚠️ This will remove **all** post channels.\n\n"
            "Use `/adpchannel_clear confirm` to continue.",
            quote=True,
        )

    return await AdChannelCommand.clear_channels(message)


HelpCmd.set_help(
    command="adpchannel",
    description=adpchannel_add_handler.__doc__,
    allow_global=True,
    allow_non_admin=False,
)

HelpCmd.set_help(
    command="adpchannel_remove",
    description=adpchannel_remove_handler.__doc__,
    allow_global=True,
    allow_non_admin=False,
)

HelpCmd.set_help(
    command="adpchannel_list",
    description=adpchannel_list_handler.__doc__,
    allow_global=True,
    allow_non_admin=False,
)

HelpCmd.set_help(
    command="adpchannel_clear",
    description=adpchannel_clear_handler.__doc__,
    allow_global=True,
    allow_non_admin=False,
)
