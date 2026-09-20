from pyrogram import filters
from pyrogram.client import Client
from pyrogram.errors import (
    ChannelInvalid,
    ChannelPrivate,
    ChatAdminRequired,
    RPCError,
)
from pyrogram.types import Message

from bot.config import config
from bot.database import MongoDB
from bot.utilities.helpers import (
    BotNotAdminError,
    NoInviteLinkError,
    PyroHelper,
)
from bot.utilities.pyrofilters import PyroFilters
from bot.utilities.pyrofilters.subscription import SubscriptionFilter
from bot.utilities.pyrotools import HelpCmd


class ForceSubCommand:
    """Force-subscription management commands."""

    database = MongoDB()

    @staticmethod
    async def add_channel(
        client: Client,
        message: Message,
        channel_id: int | str,
    ) -> Message:
        """Add or update a force-subscription channel."""
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

        await ForceSubCommand.database.add_fsub_channel(channel_info)

        channels = dict(config.channels_n_invite)
        channels[str(channel_info["channel_id"])] = channel_info
        config.channels_n_invite = channels

        SubscriptionFilter.clear_cache()

        return await message.reply(
            "✅ **Force subscription channel added.**\n\n"
            f"**Channel:** {channel_info['title']}\n"
            f"**ID:** `{channel_info['channel_id']}`\n"
            f"**Type:** "
            f"{'Private' if channel_info['is_private'] else 'Public'}",
            quote=True,
        )

    @classmethod
    async def list_channels(cls, message: Message) -> Message:
        """List configured force-subscription channels."""
        channels = config.channels_n_invite

        if not channels:
            return await message.reply(
                "ℹ️ No force-subscription channels are configured.",
                quote=True,
            )

        lines = ["**Force-subscription channels:**\n"]

        for index, channel_info in enumerate(
            channels.values(),
            start=1,
        ):
            channel_type = (
                "Private"
                if channel_info["is_private"]
                else "Public"
            )

            lines.append(
                f"**{index}. {channel_info['title']}**\n"
                f"ID: `{channel_info['channel_id']}`\n"
                f"Type: {channel_type}\n",
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
        """Remove a force-subscription channel."""
        removed = await cls.database.remove_fsub_channel(channel_id)

        if not removed:
            return await message.reply(
                "❌ That channel is not configured for force subscription.",
                quote=True,
            )

        channels = dict(config.channels_n_invite)
        channels.pop(str(channel_id), None)
        config.channels_n_invite = channels

        SubscriptionFilter.clear_cache()

        return await message.reply(
            "✅ **Force-subscription channel removed.**\n\n"
            f"ID: `{channel_id}`",
            quote=True,
        )

    @classmethod
    async def clear_channels(cls, message: Message) -> Message:
        """Remove all force-subscription channels."""
        count = await cls.database.clear_fsub_channels()

        config.channels_n_invite = {}

        SubscriptionFilter.clear_cache()

        if count == 0:
            return await message.reply(
                "ℹ️ No force-subscription channels were configured.",
                quote=True,
            )

        return await message.reply(
            f"✅ Removed **{count}** force-subscription channel(s).",
            quote=True,
        )


def get_channel_id(message: Message) -> int | str | None:
    """Get a channel ID or public username."""
    if len(message.command) > 1:
        value = message.command[1].strip()

        if value.startswith("@"):
            return value

        try:
            return int(value)
        except ValueError:
            return None

    replied = message.reply_to_message

    if replied:
        forwarded_chat = getattr(
            replied,
            "forward_from_chat",
            None,
        )

        if forwarded_chat:
            return forwarded_chat.id

        sender_chat = getattr(
            replied,
            "sender_chat",
            None,
        )

        if sender_chat:
            return sender_chat.id

    return None


@Client.on_message(
    filters.private
    & filters.command("fsub_add")
    & PyroFilters.admin()
)
async def fsub_add_handler(
    client: Client,
    message: Message,
) -> Message:
    """Add a force-subscription channel."""
    channel_id = get_channel_id(message)

    if channel_id is None:
        return await message.reply(
            "Usage:\n"
            "`/fsub_add -1001234567890`\n"
            "`/fsub_add @publicchannel`\n\n"
            "Or reply to a forwarded channel post with `/fsub_add`.",
            quote=True,
        )

    return await ForceSubCommand.add_channel(
        client=client,
        message=message,
        channel_id=channel_id,
    )


@Client.on_message(
    filters.private
    & filters.command("fsub_list")
    & PyroFilters.admin()
)
async def fsub_list_handler(
    client: Client,
    message: Message,
) -> Message:
    """List force-subscription channels."""
    return await ForceSubCommand.list_channels(message)


@Client.on_message(
    filters.private
    & filters.command("fsub_remove")
    & PyroFilters.admin()
)
async def fsub_remove_handler(
    client: Client,
    message: Message,
) -> Message:
    """Remove a force-subscription channel."""
    channel_id = get_channel_id(message)

    if not isinstance(channel_id, int):
        return await message.reply(
            "Usage:\n`/fsub_remove -1001234567890`",
            quote=True,
        )

    return await ForceSubCommand.remove_channel(
        message=message,
        channel_id=channel_id,
    )


@Client.on_message(
    filters.private
    & filters.command("fsub_clear")
    & PyroFilters.admin()
)
async def fsub_clear_handler(
    client: Client,
    message: Message,
) -> Message:
    """Remove all force-subscription channels."""
    if (
        len(message.command) < 2
        or message.command[1].lower() != "confirm"
    ):
        return await message.reply(
            "⚠️ This will remove **all** force-subscription channels.\n\n"
            "Use `/fsub_clear confirm` to continue.",
            quote=True,
        )

    return await ForceSubCommand.clear_channels(message)


HelpCmd.set_help(
    command="fsub_add",
    description=fsub_add_handler.__doc__,
    allow_global=True,
    allow_non_admin=False,
)

HelpCmd.set_help(
    command="fsub_remove",
    description=fsub_remove_handler.__doc__,
    allow_global=True,
    allow_non_admin=False,
)

HelpCmd.set_help(
    command="fsub_list",
    description=fsub_list_handler.__doc__,
    allow_global=True,
    allow_non_admin=False,
)
