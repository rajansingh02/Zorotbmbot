from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import Message

from bot.database import MongoDB
from bot.utilities.helpers import RateLimiter
from bot.utilities.pyrofilters import PyroFilters
from bot.utilities.pyrotools import HelpCmd

database = MongoDB()


@Client.on_message(
    filters.private & PyroFilters.admin() & filters.command("stats"),
)
@RateLimiter.hybrid_limiter(func_count=1)
async def stats(_: Client, message: Message) -> Message:
    """A command to display links and users count.:

    **Usage:**
        /stats
    """

    link_count, users_count = await database.stats()

    return await message.reply(
        f">STATS:\n**Users Count:** `{users_count}`\n"
        f"**Links Count:** `{link_count}`",
    )


HelpCmd.set_help(
    command="stats",
    description=stats.__doc__,
    allow_global=False,
    allow_non_admin=False,
)


@Client.on_message(
    filters.private & PyroFilters.admin() & filters.command("resetlimit"),
)
@RateLimiter.hybrid_limiter(func_count=1)
async def resetlimit(_: Client, message: Message) -> Message:
    """Reset the 20-file rolling 24-hour limit.

    **Usage:**
        /resetlimit <user_id>
        /resetlimit all
    """

    if len(message.command) < 2:
        return await message.reply(
            "Usage:\n"
            "`/resetlimit <user_id>`\n"
            "`/resetlimit all`",
            quote=True,
        )

    target = message.command[1].strip().lower()

    if target == "all":
        deleted_count = await database.reset_daily_file_limit()

        return await message.reply(
            "✅ **Rate limits reset.**\n\n"
            f"Reset **{deleted_count}** user(s).",
            quote=True,
        )

    try:
        user_id = int(target)
    except ValueError:
        return await message.reply(
            "❌ Invalid user ID.\n\n"
            "Use:\n"
            "`/resetlimit <user_id>`\n"
            "`/resetlimit all`",
            quote=True,
        )

    deleted_count = await database.reset_daily_file_limit(
        user_id=user_id,
    )

    if deleted_count:
        return await message.reply(
            "✅ **Rate limit reset.**\n\n"
            f"User: `{user_id}`",
            quote=True,
        )

    return await message.reply(
        "ℹ️ No active rate limit was found for that user.\n\n"
        f"User: `{user_id}`",
        quote=True,
    )


HelpCmd.set_help(
    command="resetlimit",
    description=resetlimit.__doc__,
    allow_global=False,
    allow_non_admin=False,
)
