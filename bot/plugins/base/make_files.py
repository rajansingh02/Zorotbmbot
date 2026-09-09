import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, TypedDict

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import config
from bot.database import MongoDB
from bot.options import options
from bot.utilities.helpers import DataEncoder, RateLimiter
from bot.utilities.pyrofilters import ConvoMessage, PyroFilters
from bot.utilities.pyrotools import HelpCmd


class CacheEntry(TypedDict):
    """Cache entry for files."""

    counter: int
    files: list[dict]
    mode: str


class MakeFilesCommand:
    """Make files command class."""

    database = MongoDB()
    files_cache: ClassVar[dict[int, CacheEntry]] = {}

    TEMP_EXPIRY_SECONDS: ClassVar[dict[str, int]] = {
        "1": 60 * 60,
        "6": 6 * 60 * 60,
        "12": 12 * 60 * 60,
    }

    @staticmethod
    @RateLimiter.hybrid_limiter(func_count=1)
    async def message_reply(
        client: Client,
        message: Message,
        **kwargs: Any,
    ) -> Message:  # noqa: ANN401, ARG004
        """
        Replies to a message with rate limiter.

        Parameters:
            client (Client): The client instance.
            message (Message): The message to reply to.
            **kwargs (Any): Additional keyword arguments for the reply.

        Returns:
            Message: The replied message.
        """
        return await message.reply(**kwargs)

    @classmethod
    async def handle_convo_start(
        cls,
        client: Client,
        message: ConvoMessage,
    ) -> Message:
        """
        Handle conversation start.

        Parameters:
            client (Client): The client instance.
            message (ConvoMessage): The conversation message.

        Returns:
            Message: The replied message.
        """
        unique_id = message.chat.id + message.from_user.id

        text = message.text or message.caption or ""
        is_temp = text == "/templink"

        cls.files_cache[unique_id] = {
            "files": [],
            "counter": 0,
            "mode": "temporary" if is_temp else "permanent",
        }

        if is_temp:
            reply_text = (
                "Send your files.\n\n"
                "When you're finished, use /temp_link."
            )
        else:
            reply_text = "Send your files."

        return await cls.message_reply(
            client=client,
            message=message,
            text=reply_text,
            quote=True,
        )

    @classmethod
    async def handle_conversation(
        cls,
        client: Client,
        message: ConvoMessage,
    ) -> Message | None:
        """
        Handle conversations and file uploads. Maintain file cache for optimization.
        Process burst files, responding only when complete.

        Parameters:
            client (Client): The client instance.
            message (ConvoMessage): The conversation message.

        Returns:
            Message or None: The replied message or None if burst is triggered.
        """
        unique_id = message.chat.id + message.from_user.id

        if unique_id not in cls.files_cache:
            return None

        file_type = (
            message.document
            or message.video
            or message.photo
            or message.audio
            or message.sticker
        )

        if not file_type:
            return await cls.message_reply(
                client=client,
                message=message,
                text="> Only send support files!",
                quote=True,
            )

        cls.files_cache[unique_id]["counter"] += 1
        cls.files_cache[unique_id]["files"].append(
            {
                "caption": message.caption.markdown if message.caption else None,
                "file_id": file_type.file_id,
                "file_name": (
                    getattr(file_type, "file_name", file_type.file_unique_id)
                    or file_type.file_unique_id
                ),
                "message_id": message.id,
                "media_group_id": message.media_group_id,
            },
        )

        current_files_count = cls.files_cache[unique_id]["counter"]

        await asyncio.sleep(0.1)

        if unique_id not in cls.files_cache:
            return None

        if cls.files_cache[unique_id]["counter"] != current_files_count:
            return None

        file_names = "\n".join(
            i["file_name"]
            for i in cls.files_cache[unique_id]["files"]
        )

        if cls.files_cache[unique_id]["mode"] == "temporary":
            extra_message = (
                "> Send more files to continue.\n"
                "> Use /temp_link when finished."
            )
        else:
            extra_message = (
                ">File list truncated.\n"
                "- Send more files to continue.\n"
                "- Use /make_link for a shareable link."
            )

        return await cls.message_reply(
            client=client,
            message=message,
            text=f"```\nFile(s):\n{file_names[-3000:]}\n```\n{extra_message}",
            quote=True,
        )

    @classmethod
    async def _store_files(
        cls,
        client: Client,
        message: Message,
        unique_id: int,
        expires_at: datetime | None = None,
    ) -> str | None:
        """Forward and store the cached files."""
        forward_limit_size = 100

        if unique_id not in cls.files_cache:
            return None

        cached_files = cls.files_cache[unique_id]["files"]

        user_cache_chunk = [
            [
                file["message_id"]
                for file in cached_files[i : i + forward_limit_size]
            ]
            for i in range(0, len(cached_files), forward_limit_size)
        ]

        if not user_cache_chunk:
            return None

        files_to_store = []

        if options.settings.BACKUP_FILES:
            for user_cache in user_cache_chunk:
                forwarded_messages = await client.forward_messages(
                    chat_id=config.BACKUP_CHANNEL,
                    from_chat_id=message.chat.id,
                    message_ids=user_cache,
                    hide_sender_name=True,
                )

                messages = (
                    forwarded_messages
                    if isinstance(forwarded_messages, list)
                    else [forwarded_messages]
                )

                for msg in messages:
                    file_type = (
                        msg.document
                        or msg.video
                        or msg.photo
                        or msg.audio
                        or msg.sticker
                    )

                    if not file_type:
                        continue

                    files_to_store.append(
                        {
                            "caption": msg.caption.markdown
                            if msg.caption
                            else None,
                            "file_id": file_type.file_id,
                            "message_id": msg.id,
                            "media_group_id": message.media_group_id,
                        },
                    )
        else:
            files_to_store = [
                {
                    k: v
                    for k, v in file
                    .items()
                    if k != "file_name"
                }
                for file in cached_files
            ]

        unique_link = f"{uuid.uuid4().int}"
        file_link = DataEncoder.encode_data(unique_link)

        file_origin = (
            config.BACKUP_CHANNEL
            if options.settings.BACKUP_FILES
            else message.chat.id
        )

        add_file = await cls.database.add_file(
            file_link=file_link,
            file_origin=file_origin,
            file_data=files_to_store,
            temporary=expires_at is not None,
            expires_at=expires_at,
        )

        if not add_file:
            return None

        cls.files_cache.pop(unique_id, None)

        return file_link

    @classmethod
    async def handle_convo_stop(
        cls,
        client: Client,
        message: ConvoMessage,
    ) -> Message:
        """
        Handle the end of a permanent-link conversation.

        Parameters:
            client (Client): The client instance.
            message (ConvoMessage): The conversation message.

        Returns:
            Message: The replied message.
        """
        unique_id = message.chat.id + message.from_user.id

        if unique_id not in cls.files_cache:
            return await cls.message_reply(
                client=client,
                message=message,
                text="No active file session found.",
                quote=True,
            )

        # A temporary session must be finalized through /temp_link.
        if cls.files_cache[unique_id]["mode"] == "temporary":
            return await cls.show_temp_expiry_buttons(
                client=client,
                message=message,
            )

        file_link = await cls._store_files(
            client=client,
            message=message,
            unique_id=unique_id,
        )

        if not file_link:
            cls.files_cache.pop(unique_id, None)

            return await cls.message_reply(
                client=client,
                message=message,
                text="No file inputs, stopping task.",
                quote=True,
            )

        link = (
            f"https://t.me/{client.me.username}?start={file_link}"
        )  # type: ignore[reportOptionalMemberAccess]

        reply_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Share URL",
                        url=f"https://t.me/share/url?url={link}",
                    ),
                ],
            ],
        )

        return await cls.message_reply(
            client=client,
            message=message,
            text=f"Here is your link:\n>{link}",
            quote=True,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )

    @classmethod
    async def show_temp_expiry_buttons(
        cls,
        client: Client,
        message: Message,
    ) -> Message:
        """Show expiry choices for a temporary link."""
        unique_id = message.chat.id + message.from_user.id

        if unique_id not in cls.files_cache:
            return await cls.message_reply(
                client=client,
                message=message,
                text="No active temporary-link session found.",
                quote=True,
            )

        if not cls.files_cache[unique_id]["files"]:
            cls.files_cache.pop(unique_id, None)

            return await cls.message_reply(
                client=client,
                message=message,
                text="No file inputs, stopping task.",
                quote=True,
            )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "1 Hour",
                        callback_data=f"temp_link:1:{message.from_user.id}",
                    ),
                    InlineKeyboardButton(
                        "6 Hours",
                        callback_data=f"temp_link:6:{message.from_user.id}",
                    ),
                    InlineKeyboardButton(
                        "12 Hours",
                        callback_data=f"temp_link:12:{message.from_user.id}",
                    ),
                ],
            ],
        )

        return await cls.message_reply(
            client=client,
            message=message,
            text="Choose how long the temporary link should remain active:",
            quote=True,
            reply_markup=keyboard,
        )

    @classmethod
    async def create_temp_link(
        cls,
        client: Client,
        callback_query: CallbackQuery,
        duration_key: str,
        user_id: int,
    ) -> None:
        """Create a temporary link after the expiry duration is selected."""
        unique_id = callback_query.message.chat.id + user_id

        if unique_id not in cls.files_cache:
            await callback_query.answer(
                "This temporary-link session has expired.",
                show_alert=True,
            )
            return

        if callback_query.from_user.id != user_id:
            await callback_query.answer(
                "This button belongs to another user.",
                show_alert=True,
            )
            return

        seconds = cls.TEMP_EXPIRY_SECONDS.get(duration_key)

        if seconds is None:
            await callback_query.answer(
                "Invalid expiry duration.",
                show_alert=True,
            )
            return

        await callback_query.answer("Creating temporary link...")

        try:
            await callback_query.message.edit_text(
                "⏳ Creating temporary link...",
            )
        except Exception:
            pass

        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=seconds,
        )

        file_link = await cls._store_files(
            client=client,
            message=callback_query.message,
            unique_id=unique_id,
            expires_at=expires_at,
        )

        if not file_link:
            cls.files_cache.pop(unique_id, None)

            try:
                await callback_query.message.edit_text(
                    "❌ Couldn't create temporary link.",
                )
            except Exception:
                await callback_query.message.reply(
                    "❌ Couldn't create temporary link.",
                )

            return

        link = (
            f"https://t.me/{client.me.username}?start={file_link}"
        )  # type: ignore[reportOptionalMemberAccess]

        hours = {
            "1": "1 hour",
            "6": "6 hours",
            "12": "12 hours",
        }[duration_key]

        reply_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Share URL",
                        url=f"https://t.me/share/url?url={link}",
                    ),
                ],
            ],
        )

        text = (
            "✅ **Temporary link created.**\n\n"
            f"Expires in: **{hours}**\n"
            f"Expires at: **{expires_at.strftime('%Y-%m-%d %H:%M UTC')}**\n\n"
            f"Here is your link:\n>{link}"
        )

        try:
            await callback_query.message.edit_text(
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except Exception:
            await callback_query.message.reply(
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )


@Client.on_message(
    filters.private
    & PyroFilters.admin(allow_global=True)
    & PyroFilters.subscription()
    & PyroFilters.create_conversation_filter(
        convo_start=[
            "/make_files",
            "/batch",
            "/batch_files",
            "/templink",
        ],
        convo_stop=[
            "/make_link",
            "/batch_link",
            "/temp_link",
        ],
    ),
)
async def make_files_command_handler(
    client: Client,
    message: ConvoMessage,
) -> Message | None:
    """Handles permanent and temporary file-link conversations.

    **Usage:**
        /make_files: initiate a permanent-link conversation.
        /make_link: finish a permanent-link conversation.
        /templink: initiate a temporary-link conversation.
        /temp_link: finish a temporary-link conversation and choose expiry.
    """
    if message.convo_start:
        return await MakeFilesCommand.handle_convo_start(
            client=client,
            message=message,
        )

    if message.conversation:
        return await MakeFilesCommand.handle_conversation(
            client=client,
            message=message,
        )

    if message.convo_stop:
        return await MakeFilesCommand.handle_convo_stop(
            client=client,
            message=message,
        )

    return None


@Client.on_callback_query(
    filters.regex(r"^temp_link:(1|6|12):\d+$"),
)
async def temporary_link_callback(
    client: Client,
    callback_query: CallbackQuery,
) -> None:
    """Handle temporary-link expiry selection."""
    try:
        _, duration_key, user_id_text = callback_query.data.split(":", 2)
        user_id = int(user_id_text)
    except (AttributeError, ValueError):
        await callback_query.answer(
            "Invalid temporary-link request.",
            show_alert=True,
        )
        return

    await MakeFilesCommand.create_temp_link(
        client=client,
        callback_query=callback_query,
        duration_key=duration_key,
        user_id=user_id,
    )


HelpCmd.set_help(
    command="make_files",
    description=make_files_command_handler.__doc__,
    allow_global=True,
    allow_non_admin=False,
    alias=["/batch", "/batch_files"],
)

HelpCmd.set_help(
    command="templink",
    description=make_files_command_handler.__doc__,
    allow_global=True,
    allow_non_admin=False,
)
