from typing import ClassVar

from pyrogram import StopPropagation, filters
from pyrogram.client import Client
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.database import MongoDB
from bot.options import options
from bot.utilities.helpers import parse_button_template
from bot.utilities.pyrofilters import PyroFilters
from bot.utilities.pyrotools import HelpCmd
from bot.utilities.pyrofilters.conversation import ConversationFilter


class MPostCommand:
    """
    Create a channel-ready post from an active /make_files session.

    Flow:
        /make_files -> send files -> /mpost
        -> send one photo with a caption
        -> preview
        -> choose a channel

    The photo caption is kept exactly as sent.

    [Link] is replaced only inside the saved button preset.
    """

    database = MongoDB()

    pending_posts: ClassVar[dict[int, dict]] = {}
    ready_posts: ClassVar[dict[int, dict]] = {}

    @classmethod
    async def _get_preset(cls) -> dict | None:
        """
        Get the configured /mpost preset.

        Rules:
        1. If MPOST_DEFAULT_PRESET is set, use that preset.
        2. If no default is set and exactly one preset exists, use it.
        3. Otherwise return None.
        """

        presets = await cls.database.get_mpost_presets()

        default_name = str(
            options.settings.MPOST_DEFAULT_PRESET or ""
        ).strip().lower()

        if default_name and default_name != "0":
            for preset in presets:
                saved_name = str(
                    preset.get("_id", "")
                ).strip().lower()

                if saved_name == default_name:
                    return preset

            return None

        if len(presets) == 1:
            return presets[0]

        return None

    @classmethod
    async def begin(
        cls,
        client: Client,
        message: Message,
        unique_id: int,
        file_link: str,
    ) -> Message:
        """Start the /mpost photo step."""

        username = client.me.username

        link = f"https://t.me/{username}?start={file_link}"

        preset = await cls._get_preset()

        cls.pending_posts[unique_id] = {
            "link": link,
            "preset": preset,
        }

        # Work out why there is no preset.
        note = ""

        if not preset:
            presets = await cls.database.get_mpost_presets()

            if not presets:
                note = (
                    "\n\n⚠️ No button preset saved.\n"
                    "Reply to your button template with:\n"
                    "`/preset_save movie`"
                )
            elif (
                options.settings.MPOST_DEFAULT_PRESET
                and str(options.settings.MPOST_DEFAULT_PRESET) != "0"
            ):
                default_name = str(
                    options.settings.MPOST_DEFAULT_PRESET
                ).strip()

                note = (
                    f"\n\n⚠️ Preset `{default_name}` was not found."
                )
            else:
                note = (
                    "\n\n⚠️ More than one preset is saved.\n"
                    "Set the one to use with:\n"
                    "`/option MPOST_DEFAULT_PRESET <name>`"
                )

        text = (
            "✅ **Link created.**\n\n"
            f"> {link}\n\n"
            "📮 Now send **one photo with a caption**.\n"
            "The caption will be posted exactly as you send it."
            f"{note}\n\n"
            "Use /cancel to stop."
        )

        try:
            return await message.edit_text(
                text,
                disable_web_page_preview=True,
            )
        except Exception:
            return await message.reply(
                text,
                quote=True,
                disable_web_page_preview=True,
            )

    @classmethod
    async def handle_post_image(
        cls,
        client: Client,
        message: Message,
    ) -> Message | None:
        """Handle the photo sent after /mpost."""

        if not message.from_user:
            return None

        unique_id = message.chat.id + message.from_user.id

        pending = cls.pending_posts.get(unique_id)

        if not pending:
            return None

        if not message.photo:
            return await message.reply(
                "❌ Send one photo with a caption, or /cancel.",
                quote=True,
            )

        if not message.caption:
            return await message.reply(
                "❌ Please include a caption with the photo.",
                quote=True,
            )

        link = pending["link"]
        preset = pending.get("preset")

        # Keep the user's caption exactly as sent.
        caption = message.caption.markdown

        markup = None
        warnings: list[str] = []

        # [Link] replacement happens ONLY in the preset.
        if preset:
            template = preset.get("template")

            if template:
                markup, warnings = parse_button_template(
                    template=template,
                    link=link,
                )
            else:
                warnings.append(
                    "The selected preset has an empty template."
                )

        # Remove pending state after the photo has been accepted.
        cls.pending_posts.pop(unique_id, None)

        # Save ready post.
        cls.ready_posts[unique_id] = {
            "file_id": message.photo.file_id,
            "caption": caption,
            "markup": markup,
        }

        warning_text = ""

        if warnings:
            warning_text = (
                "\n\n⚠️ " + "\n⚠️ ".join(warnings)
            )

        # Preview.
        await message.reply_photo(
            photo=message.photo.file_id,
            caption=caption,
            reply_markup=markup,
            quote=True,
        )

        # Get configured post channels.
        ad_channels = await cls.database.get_ad_channels()

        if not ad_channels:
            return await message.reply(
                text=(
                    "ℹ️ That's the post preview."
                    f"{warning_text}\n\n"
                    "No post channels configured."
                ),
                quote=True,
            )

        buttons = []

        for channel in ad_channels:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"📤 Send to {channel['title']}",
                        callback_data=(
                            f"mpost_ch:"
                            f"{channel['channel_id']}:"
                            f"{message.from_user.id}"
                        ),
                    )
                ]
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=(
                        f"mpost_cancel:{message.from_user.id}"
                    ),
                )
            ]
        )

        return await message.reply(
            text=f"📮 Send this post to:{warning_text}",
            quote=True,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


    @classmethod
    async def send_to_channel(
        cls,
        client: Client,
        callback_query: CallbackQuery,
        channel_id: int,
        user_id: int,
    ) -> None:
        """Send the ready post to the selected channel."""

        if not callback_query.message:
            await callback_query.answer(
                "This post preview has expired.",
                show_alert=True,
            )
            return

        unique_id = callback_query.message.chat.id + user_id

        ready = cls.ready_posts.get(unique_id)

        if not ready:
            await callback_query.answer(
                "This post preview has expired.",
                show_alert=True,
            )
            return

        await callback_query.answer("Posting...")

        try:
            await client.send_photo(
                chat_id=channel_id,
                photo=ready["file_id"],
                caption=ready["caption"],
                reply_markup=ready["markup"],
            )

        except Exception as e:
            try:
                await callback_query.message.edit_text(
                    f"❌ Couldn't post to that channel:\n{e}"
                )
            except Exception:
                pass

            return

        cls.ready_posts.pop(unique_id, None)
        ConversationFilter._convo_cache.discard(unique_id)

        try:
            await callback_query.message.edit_text(
                "✅ Post sent."
            )
        except Exception:
            pass


@Client.on_message(
    filters.private & filters.photo,
    group=-100,
)
async def mpost_image_handler(
    client: Client,
    message: Message,
) -> Message | None:
    """
    Collect the photo for an active /mpost session.

    This handler runs before normal handlers and stops propagation
    once it has claimed an active /mpost photo.
    """

    if not message.from_user:
        return None

    unique_id = message.chat.id + message.from_user.id

    if unique_id not in MPostCommand.pending_posts:
        return None

    try:
        result = await MPostCommand.handle_post_image(
            client=client,
            message=message,
        )
    finally:
        # Prevent FSub and other later handlers from processing
        # an active /mpost photo.
        raise StopPropagation

    return result


@Client.on_message(
    filters.private
    & filters.command("mpost")
    & PyroFilters.admin(allow_global=True)
    & PyroFilters.user_not_in_conversation(),
)
async def mpost_standalone_handler(
    client: Client,
    message: Message,
) -> Message:
    """Explain how to use /mpost."""

    return await message.reply(
        "ℹ️ Start with `/make_files`, send your file(s), "
        "then use `/mpost`.",
        quote=True,
    )


@Client.on_callback_query(
    filters.regex(r"^mpost_ch:-?\d+:\d+$"),
)
async def mpost_channel_callback(
    client: Client,
    callback_query: CallbackQuery,
) -> None:
    """Handle channel selection."""

    try:
        _, channel_id_text, user_id_text = callback_query.data.split(
            ":",
            2,
        )

        channel_id = int(channel_id_text)
        user_id = int(user_id_text)

    except (AttributeError, ValueError):
        await callback_query.answer(
            "Invalid request.",
            show_alert=True,
        )
        return

    if callback_query.from_user.id != user_id:
        await callback_query.answer(
            "This button belongs to another user.",
            show_alert=True,
        )
        return

    await MPostCommand.send_to_channel(
        client=client,
        callback_query=callback_query,
        channel_id=channel_id,
        user_id=user_id,
    )


@Client.on_callback_query(
    filters.regex(r"^mpost_cancel:\d+$"),
)
async def mpost_cancel_callback(
    client: Client,
    callback_query: CallbackQuery,
) -> None:
    """Cancel the ready post."""

    try:
        _, user_id_text = callback_query.data.split(":", 1)
        user_id = int(user_id_text)

    except (AttributeError, ValueError):
        await callback_query.answer(
            "Invalid request.",
            show_alert=True,
        )
        return

    if callback_query.from_user.id != user_id:
        await callback_query.answer(
            "This button belongs to another user.",
            show_alert=True,
        )
        return

    if not callback_query.message:
        await callback_query.answer(
            "This post preview has expired.",
            show_alert=True,
        )
        return

    unique_id = callback_query.message.chat.id + user_id

    MPostCommand.ready_posts.pop(
        unique_id,
        None,
    )

    await callback_query.answer("Cancelled.")

    try:
        await callback_query.message.edit_text(
            "❌ Post cancelled."
        )
    except Exception:
        pass


HelpCmd.set_help(
    command="mpost",
    description=(
        "Create a post from an active /make_files session.\n\n"
        "**Usage:**\n"
        "1. Use /make_files\n"
        "2. Send your file(s)\n"
        "3. Use /mpost\n"
        "4. Send one photo with a caption\n"
        "5. Choose the destination channel\n\n"
        "The caption is kept exactly as sent.\n"
        "`[Link]` is replaced only inside the saved button preset."
    ),
    allow_global=True,
    allow_non_admin=False,
)
