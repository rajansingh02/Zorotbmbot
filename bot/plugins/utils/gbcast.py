import asyncio
import time
from inspect import cleandoc
from typing import Any

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from bot.options import options
from bot.utilities.helpers import RateLimiter
from bot.utilities.pyrofilters import PyroFilters
from bot.utilities.pyrotools import HelpCmd


_last_gbcast: dict[int, float] = {}


def _quote(text: str) -> str:
    """Format every line as a Telegram quote."""
    return "\n".join(
        f">{line}" if line else ">"
        for line in text.splitlines()
    )


async def _resend_on_floodwait(send: Any) -> Message:
    """Retry the operation after Telegram asks us to wait."""
    try:
        return await send()
    except FloodWait as e:
        await asyncio.sleep(float(e.value))
        return await send()


@Client.on_message(
    PyroFilters.admin() & filters.command("gbcast"),
)
@RateLimiter.hybrid_limiter(func_count=1)
async def gbcast(client: Client, message: Message) -> Message:
    """
    Broadcast a movie announcement to the configured announcement group.

    Usage:
        /gbcast Movie Name

    The movie name is inserted into GBCAST_TEMPLATE and the
    Movie command is displayed as copyable monospace text.
    """

    group_id = options.settings.GBCAST_GROUP_ID

    if not group_id:
        return await message.reply(
            text=(
                "❌ No announcement group configured yet.\n\n"
                "Set one with:\n"
                "`/option GBCAST_GROUP_ID -100xxxxxxxxxx`"
            ),
            quote=True,
        )

    admin_id = message.from_user.id

    cooldown = options.settings.GBCAST_COOLDOWN_SECONDS
    elapsed = time.time() - _last_gbcast.get(admin_id, 0.0)

    if cooldown and elapsed < cooldown:
        return await message.reply(
            text=(
                f"⏳ Please wait "
                f"{int(cooldown - elapsed)}s before using /gbcast again."
            ),
            quote=True,
        )

    movie_name = (
        message.text.split(maxsplit=1)[1].strip()
        if message.command[1:]
        else ""
    )

    if not movie_name:
        return await message.reply(
            text=(
                "❌ Please provide a movie name.\n\n"
                "Example:\n"
                "`/gbcast Interstellar`"
            ),
            quote=True,
        )

    preset = options.settings.GBCAST_TEMPLATE.replace(
        "[]",
        movie_name,
    )

    final_text = (
        _quote(preset)
        + f'\n\n`Movie {movie_name}`\n👆 Tap to copy'
    )

    try:
        await _resend_on_floodwait(
            lambda: client.send_message(
                chat_id=group_id,
                text=final_text,
                disable_web_page_preview=True,
            ),
        )

    except Exception as e:
        return await message.reply(
            text=f"❌ Couldn't broadcast to the group: {e}",
            quote=True,
        )

    _last_gbcast[admin_id] = time.time()

    return await message.reply(
        text="✅ Broadcasted to the group.",
        quote=True,
    )


HelpCmd.set_help(
    command="gbcast",
    description=gbcast.__doc__,
    allow_global=False,
    allow_non_admin=False,
)
