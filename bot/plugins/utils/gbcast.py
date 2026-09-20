import asyncio
import re
import time
from inspect import cleandoc
from typing import Any

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.errors import FloodWait
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.options import options
from bot.utilities.helpers import RateLimiter
from bot.utilities.pyrofilters import PyroFilters
from bot.utilities.pyrotools import HelpCmd

# The KurimuzonAkuma pyrogram fork this bot uses added support for Telegram's
# "copy text" button (Bot API 7.11+). If the installed version doesn't have
# it yet, we fall back to a plain callback button that pops the text in an
# alert instead -- either way nothing here requires inline mode.
try:
    from pyrogram.types import CopyTextButton

    _HAS_COPY_TEXT_BUTTON = True
except ImportError:
    _HAS_COPY_TEXT_BUTTON = False

DEEP_LINK_REGEX = re.compile(r"https?://t\.me/\w+\?start=\S+")

CAPTIONABLE_ATTRS = ("document", "video", "photo", "audio", "animation", "voice")
MEDIA_ATTRS = (*CAPTIONABLE_ATTRS, "sticker", "video_note")

# Admin user id -> last /gbcast timestamp. Small and short-lived enough that
# it doesn't need any TTL pruning of its own -- nothing here touches the
# database, this is just an in-memory spam guard.
_last_gbcast: dict[int, float] = {}


def _quote(text: str) -> str:
    """Render text as a Telegram blockquote (one leading '>' per line)."""
    return "\n".join(f">{line}" if line else ">" for line in text.splitlines())


def _build_copy_button(movie_name: str) -> InlineKeyboardButton:
    """A button that hands the viewer the exact `Movie name` command to send."""
    command_text = f"Movie {movie_name}"

    if _HAS_COPY_TEXT_BUTTON:
        return InlineKeyboardButton(
            "📋 Copy Command",
            copy_text=CopyTextButton(text=command_text),
        )

    # Fallback: callback_data is capped at 64 bytes, keep the name short.
    safe_name = command_text.encode()[:50].decode("utf-8", errors="ignore")
    return InlineKeyboardButton(
        "📋 Show Command",
        callback_data=f"gbcast_show:{safe_name}",
    )


async def _resend_on_floodwait(send: Any) -> Message:  # noqa: ANN401
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
    """Broadcast a replied-to message into the configured announcement group.

    **Usage:**
        Reply to an uploaded file (or its link message) with:
            `/gbcast`
        to copy it into the group as-is, quoted, or:
            `/gbcast Movie Name`
        to post it dressed up with the announcement preset instead -- every
        `[]` in GBCAST_TEMPLATE is replaced with the name you type, and a
        button is attached so people can grab the `Movie Name` command to
        paste elsewhere.

    Nothing from /gbcast is written to the database.

    **Configure via /option:**
        GBCAST_GROUP_ID, GBCAST_TEMPLATE, GBCAST_COOLDOWN_SECONDS
    """
    if not message.reply_to_message:
        return await message.reply(
            text=cleandoc(gbcast.__doc__ or ""),
            quote=True,
        )

    group_id = options.settings.GBCAST_GROUP_ID

    if not group_id:
        return await message.reply(
            text=(
                "❌ No announcement group configured yet.\n\n"
                "Set one with:\n`/option GBCAST_GROUP_ID -100xxxxxxxxxx`"
            ),
            quote=True,
        )

    admin_id = message.from_user.id
    cooldown = options.settings.GBCAST_COOLDOWN_SECONDS
    elapsed = time.time() - _last_gbcast.get(admin_id, 0.0)

    if cooldown and elapsed < cooldown:
        return await message.reply(
            text=f"⏳ Please wait {int(cooldown - elapsed)}s before using /gbcast again.",
            quote=True,
        )

    movie_name = (
        message.text.split(maxsplit=1)[1].strip()
        if message.command[1:]
        else ""
    )
    reply_to = message.reply_to_message

    reply_markup = None

    if movie_name:
        preset = options.settings.GBCAST_TEMPLATE.replace("[]", movie_name)
        final_text = _quote(preset)

        source_text = None

        if reply_to.text:
            source_text = reply_to.text.markdown
        elif reply_to.caption:
            source_text = reply_to.caption.markdown

        deep_link = None

        if source_text:
            found = DEEP_LINK_REGEX.search(source_text)
            if found:
                deep_link = found.group(0)

        buttons = [[_build_copy_button(movie_name)]]

        if deep_link:
            label = movie_name if len(movie_name) <= 30 else f"{movie_name[:30]}..."  # noqa: PLR2004
            buttons.append(
                [InlineKeyboardButton(f'🎬 Get "{label}"', url=deep_link)],
            )

        reply_markup = InlineKeyboardMarkup(buttons)
    else:
        original = None

        if reply_to.caption:
            original = reply_to.caption.markdown
        elif reply_to.text:
            original = reply_to.text.markdown

        header = _quote("📢 Broadcast")
        final_text = f"{header}\n\n{original}" if original else header

    captionable = any(getattr(reply_to, attr, None) for attr in CAPTIONABLE_ATTRS)
    has_media = captionable or any(getattr(reply_to, attr, None) for attr in MEDIA_ATTRS)

    try:
        if has_media:
            if captionable:
                await _resend_on_floodwait(
                    lambda: reply_to.copy(
                        chat_id=group_id,
                        caption=final_text,
                        reply_markup=reply_markup,
                    ),
                )
            else:
                # Stickers/video notes can't carry a caption -- send the
                # media, then the announcement as its own message.
                await _resend_on_floodwait(lambda: reply_to.copy(chat_id=group_id))
                await _resend_on_floodwait(
                    lambda: client.send_message(
                        chat_id=group_id,
                        text=final_text,
                        reply_markup=reply_markup,
                    ),
                )
        else:
            await _resend_on_floodwait(
                lambda: client.send_message(
                    chat_id=group_id,
                    text=final_text,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                ),
            )
    except Exception as e:  # noqa: BLE001
        return await message.reply(
            text=f"❌ Couldn't broadcast to the group: {e}",
            quote=True,
        )

    _last_gbcast[admin_id] = time.time()

    return await message.reply(text="✅ Broadcasted to the group.", quote=True)


@Client.on_callback_query(filters.regex(r"^gbcast_show:"))
async def gbcast_show_callback(client: Client, callback_query: CallbackQuery) -> None:  # noqa: ARG001
    """Fallback popup for pyrogram builds without CopyTextButton support."""
    command_text = callback_query.data.split(":", 1)[1]
    await callback_query.answer(command_text, show_alert=True)


HelpCmd.set_help(
    command="gbcast",
    description=gbcast.__doc__,
    allow_global=False,
    allow_non_admin=False,
)
