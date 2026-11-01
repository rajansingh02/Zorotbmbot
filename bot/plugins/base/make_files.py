import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, TypedDict
from urllib.parse import parse_qs, urlparse

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.errors import RPCError
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pymongo.errors import PyMongoError

from bot.config import config
from bot.database import MongoDB
from bot.options import options
from bot.utilities.helpers import DataEncoder, RateLimiter
from bot.utilities.pyrofilters import ConvoMessage, PyroFilters
from bot.utilities.pyrofilters.conversation import ConversationFilter
from bot.utilities.pyrotools import HelpCmd


class CacheEntry(TypedDict):
    """Cache entry for files."""

    counter: int
    files: list[dict]
    mode: str


class EditEntry(TypedDict):
    """Cached data needed to edit an existing file link."""

    link: str
    file_origin: int
    original_files: list[dict]
    removed_original: set[int]
    revision: int


class EditLinkError(RuntimeError):
    """Raised when an edit cannot produce valid file records."""

    def __init__(self) -> None:
        super().__init__("Telegram could not prepare the added files.")


class MakeFilesCommand:
    """Make files command class."""

    database = MongoDB()
    files_cache: ClassVar[dict[int, CacheEntry]] = {}
    edit_cache: ClassVar[dict[int, EditEntry]] = {}
    logger = logging.getLogger(__name__)

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
        if text == "/edit_link" and message.from_user.id not in config.ROOT_ADMINS_ID:
            ConversationFilter.deactivate_conversation(unique_id)
            cls.files_cache.pop(unique_id, None)
            cls.edit_cache.pop(unique_id, None)
            return await cls.message_reply(
                client=client,
                message=message,
                text="Only bot admins can edit existing links.",
                quote=True,
            )

        is_temp = text == "/templink"
        cls.edit_cache.pop(unique_id, None)

        cls.files_cache[unique_id] = {
            "files": [],
            "counter": 0,
            "mode": (
                "temporary"
                if is_temp
                else "edit_link_input"
                if text == "/edit_link"
                else "permanent"
            ),
        }

        if is_temp:
            reply_text = (
                "Send your files.\n\n"
                "When you're finished, use /temp_link."
            )
        elif text == "/edit_link":
            reply_text = (
                "Send the bot link you want to edit. "
                "You can then add files, use /remove_file [number] "
                "to remove files, and /preview_edit to review changes."
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

        if cls.files_cache[unique_id]["mode"] == "edit_link_input":
            return await cls.begin_edit_link(
                client=client,
                message=message,
                unique_id=unique_id,
            )

        if cls.files_cache[unique_id]["mode"] == "edit":
            text = message.text or ""
            if text:
                return await cls.handle_edit_text(
                    client=client,
                    message=message,
                    unique_id=unique_id,
                )

        return await cls.handle_file_message(
            client=client,
            message=message,
            unique_id=unique_id,
        )

    @classmethod
    async def handle_file_message(
        cls,
        client: Client,
        message: ConvoMessage,
        unique_id: int,
    ) -> Message | None:
        """Store a supported file sent during a file conversation."""
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

        if cls.files_cache[unique_id]["mode"] == "edit":
            cls.edit_cache[unique_id]["revision"] += 1
            return await cls.message_reply(
                client=client,
                message=message,
                text=(
                    f"Added `{getattr(file_type, 'file_name', None) or 'file'}`. "
                    "Send more files, remove files with /remove_file [number], "
                    "or use /preview_edit."
                ),
                quote=True,
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

        reply_markup = None

        if cls.files_cache[unique_id]["mode"] == "temporary":
            extra_message = (
                "> Send more files to continue.\n"
                "> Use /temp_link when finished."
            )
        else:
            extra_message = (
                ">File list truncated.\n"
                "- Send more files to continue.\n"
                "- Use /make_link for a shareable link, or /mpost to "
                "also turn it into a channel post.\n"
                "- Or just tap a button below."
            )
            reply_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔗 Get Link",
                            callback_data=f"mf_get_link:{message.from_user.id}",
                        ),
                        InlineKeyboardButton(
                            "🖼 Make Post",
                            callback_data=f"mf_make_post:{message.from_user.id}",
                        ),
                    ],
                ],
            )

        return await cls.message_reply(
            client=client,
            message=message,
            text=f"```\nFile(s):\n{file_names[-3000:]}\n```\n{extra_message}",
            quote=True,
            reply_markup=reply_markup,
        )

    @classmethod
    async def begin_edit_link(
        cls,
        client: Client,
        message: Message,
        unique_id: int,
    ) -> Message:
        """Load the selected link and prepare its edit session."""
        link = (message.text or "").strip()
        if not link:
            return await cls.message_reply(
                client=client,
                message=message,
                text="Please send the existing bot link or its start token.",
                quote=True,
            )

        if "://" in link:
            try:
                link = parse_qs(urlparse(link).query).get("start", [""])[0]
            except ValueError:
                link = ""

        file_document = (
            await cls.database.get_link_document(base64_file_link=link)
            if link
            else None
        )
        if not file_document:
            return await cls.message_reply(
                client=client,
                message=message,
                text="I couldn't find that link. Send a valid bot link or start token.",
                quote=True,
            )

        original_files = file_document.get("files", [])
        if not original_files:
            return await cls.message_reply(
                client=client,
                message=message,
                text="That link has no files to edit.",
                quote=True,
            )

        cls.edit_cache[unique_id] = {
            "link": link,
            "file_origin": file_document["file_origin"],
            "original_files": original_files,
            "removed_original": set(),
            "revision": 0,
        }
        cls.files_cache[unique_id]["mode"] = "edit"
        backup_warning = (
            "\n\n⚠️ This link doesn't use the configured backup channel. "
            "Because backups are currently enabled, you can remove files "
            "but cannot add files to this link."
            if options.settings.BACKUP_FILES
            and file_document["file_origin"] != config.BACKUP_CHANNEL
            else ""
        )
        return await cls.message_reply(
            client=client,
            message=message,
            text=(
                f"Editing link `{link}` with {len(original_files)} file(s). "
                "Send files to add, use /remove_file [number] to remove one, "
                "or /preview_edit to review and confirm.\n\n"
                f"{cls.format_edit_file_list(unique_id)}"
                f"{backup_warning}"
            ),
            quote=True,
        )

    @classmethod
    async def handle_edit_text(
        cls,
        client: Client,
        message: Message,
        unique_id: int,
    ) -> Message:
        """Handle text commands during an edit session."""
        command = (message.text or "").split(maxsplit=1)[0]
        if command == "/remove_file":
            return await cls.remove_edit_file(
                client=client,
                message=message,
                unique_id=unique_id,
            )

        return await cls.message_reply(
            client=client,
            message=message,
            text=(
                "Send files to add, use /remove_file [number] "
                "to remove one, or /preview_edit to review changes."
            ),
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
                    for k, v in file.items()
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
    async def finalize_link(
        cls,
        client: Client,
        message: Message,
        unique_id: int,
    ) -> tuple[str | None, Message | None]:
        """
        Finalize a permanent-mode session into a stored file link.

        Shared by the /make_link text command and the "Get Link" /
        "Make Post" buttons, so both paths behave identically.

        Parameters:
            client (Client): The client instance.
            message (Message): A message in the same chat as the session
                (used only for its chat ID and for replying on failure).
            unique_id (int): The session's cache key, computed by the caller.

        Returns:
            A tuple of (file_link, error_reply). Exactly one is None:
            on success file_link is set; on failure error_reply is the
            message already sent to the user explaining what happened.
        """
        if unique_id not in cls.files_cache:
            return None, await cls.message_reply(
                client=client,
                message=message,
                text="No active file session found.",
                quote=True,
            )

        if cls.files_cache[unique_id]["mode"] == "edit":
            return await cls.show_edit_preview(
                client=client,
                message=message,
                unique_id=unique_id,
            )

        if cls.files_cache[unique_id]["mode"] == "edit_link_input":
            ConversationFilter.activate_conversation(unique_id)
            return await cls.message_reply(
                client=client,
                message=message,
                text="Send the existing bot link or its start token before previewing.",
                quote=True,
            )

        # A temporary session must be finalized through /temp_link.
        if cls.files_cache[unique_id]["mode"] == "temporary":
            return None, await cls.show_temp_expiry_buttons(
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
            cls.edit_cache.pop(unique_id, None)

            return None, await cls.message_reply(
                client=client,
                message=message,
                text="No file inputs, stopping task.",
                quote=True,
            )

        return file_link, None

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

        file_link, error = await cls.finalize_link(
            client=client,
            message=message,
            unique_id=unique_id,
        )

        if error is not None:
            return error

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
    async def handle_mpost_stop(
        cls,
        client: Client,
        message: ConvoMessage,
    ) -> Message:
        """
        Finalize the session via /mpost and hand off to the post-composer.

        Parameters:
            client (Client): The client instance.
            message (ConvoMessage): The conversation message.

        Returns:
            Message: The replied message.
        """
        unique_id = message.chat.id + message.from_user.id

        file_link, error = await cls.finalize_link(
            client=client,
            message=message,
            unique_id=unique_id,
        )

        if error is not None:
            return error

        from bot.plugins.base.mpost import MPostCommand  # noqa: PLC0415

        return await MPostCommand.begin(
            client=client,
            message=message,
            unique_id=unique_id,
            file_link=file_link,  # type: ignore[reportArgumentType]
        )

    @classmethod
    async def handle_cancel(
        cls,
        client: Client,
        message: ConvoMessage,
    ) -> Message:
        """Cancel the active file-link conversation."""
        unique_id = message.chat.id + message.from_user.id

        if unique_id not in cls.files_cache:
            return await cls.message_reply(
                client=client,
                message=message,
                text="No active file session found.",
                quote=True,
            )

        cls.files_cache.pop(unique_id, None)
        cls.edit_cache.pop(unique_id, None)

        return await cls.message_reply(
            client=client,
            message=message,
            text="❌ File operation cancelled.",
            quote=True,
        )

    @classmethod
    def get_edit_file_count(cls, unique_id: int) -> int:
        """Return the number of files currently retained in an edit session."""
        edit_entry = cls.edit_cache[unique_id]
        retained = len(edit_entry["original_files"]) - len(
            edit_entry["removed_original"],
        )
        return retained + len(cls.files_cache[unique_id]["files"])

    @classmethod
    def format_edit_file_list(cls, unique_id: int) -> str:
        """Format the current file order used by /remove_file."""
        edit_entry = cls.edit_cache[unique_id]
        lines = []
        number = 1
        for index in range(len(edit_entry["original_files"])):
            if index not in edit_entry["removed_original"]:
                lines.append(f"{number}. Existing file #{index + 1}")
                number += 1
        for index, _ in enumerate(cls.files_cache[unique_id]["files"], start=1):
            lines.append(f"{number}. Added file #{index}")
            number += 1
        return "Current file order (/remove_file [number]):\n" + "\n".join(lines)

    @classmethod
    async def remove_edit_file(
        cls,
        client: Client,
        message: Message,
        unique_id: int,
    ) -> Message:
        """Remove one file from the pending edit by its displayed number."""
        arguments = (message.text or "").split(maxsplit=1)[1:]
        if not arguments or not arguments[0].isdigit():
            return await cls.message_reply(
                client=client,
                message=message,
                text="Usage: `/remove_file [number]`",
                quote=True,
            )

        file_number = int(arguments[0])
        edit_entry = cls.edit_cache[unique_id]
        retained_originals = [
            index
            for index in range(len(edit_entry["original_files"]))
            if index not in edit_entry["removed_original"]
        ]
        added_files = cls.files_cache[unique_id]["files"]
        if file_number < 1 or file_number > len(retained_originals) + len(added_files):
            return await cls.message_reply(
                client=client,
                message=message,
                text=f"Choose a file number from 1 to {len(retained_originals) + len(added_files)}.",
                quote=True,
            )

        file_index = file_number - 1
        if file_index < len(retained_originals):
            edit_entry["removed_original"].add(retained_originals[file_index])
        else:
            added_files.pop(file_index - len(retained_originals))
        edit_entry["revision"] += 1

        return await cls.message_reply(
            client=client,
            message=message,
            text=(
                f"Removed file {file_number}. "
                f"{cls.get_edit_file_count(unique_id)} file(s) remain.\n\n"
                f"{cls.format_edit_file_list(unique_id)}"
            ),
            quote=True,
        )

    @classmethod
    async def show_edit_preview(
        cls,
        client: Client,
        message: Message,
        unique_id: int,
    ) -> Message:
        """Show the pending changes and ask for explicit confirmation."""
        edit_entry = cls.edit_cache.get(unique_id)
        if not edit_entry:
            ConversationFilter.activate_conversation(unique_id)
            return await cls.message_reply(
                client=client,
                message=message,
                text="This edit session has expired.",
                quote=True,
            )

        retained_originals = [
            file
            for index, file in enumerate(edit_entry["original_files"])
            if index not in edit_entry["removed_original"]
        ]
        additions = cls.files_cache[unique_id]["files"]
        if not edit_entry["removed_original"] and not additions:
            ConversationFilter.activate_conversation(unique_id)
            return await cls.message_reply(
                client=client,
                message=message,
                text="No changes to save. Add a file or remove one before previewing.",
                quote=True,
            )

        preview_files = retained_originals + additions
        if not preview_files:
            ConversationFilter.activate_conversation(unique_id)
            return await cls.message_reply(
                client=client,
                message=message,
                text="An edited link must contain at least one file. Add a file before saving.",
                quote=True,
            )

        ConversationFilter.activate_conversation(unique_id)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Save changes",
                        callback_data=(
                            f"edit_link:confirm:{message.from_user.id}:"
                            f"{edit_entry['revision']}"
                        ),
                    ),
                    InlineKeyboardButton(
                        "Continue editing",
                        callback_data=(
                            f"edit_link:continue:{message.from_user.id}:"
                            f"{edit_entry['revision']}"
                        ),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "Cancel",
                        callback_data=(
                            f"edit_link:cancel:{message.from_user.id}:"
                            f"{edit_entry['revision']}"
                        ),
                    ),
                ],
            ],
        )
        removed_numbers = sorted(
            index + 1 for index in edit_entry["removed_original"]
        )
        removed_summary = (
            "None"
            if not removed_numbers
            else ", ".join(f"#{number}" for number in removed_numbers)
        )
        return await cls.message_reply(
            client=client,
            message=message,
            text=(
                f"**Review edit for `{edit_entry['link']}`**\n"
                f"Files kept: {len(retained_originals)}\n"
                f"Files added: {len(additions)}\n"
                f"Original files removed: {removed_summary}\n"
                f"Total after saving: {len(preview_files)}\n\n"
                f"{cls.format_edit_file_list(unique_id)}\n\n"
                "The link URL will stay the same. Save only if this looks right."
            ),
            quote=True,
            reply_markup=keyboard,
        )

    @classmethod
    async def finish_edit(
        cls,
        client: Client,
        callback_query: CallbackQuery,
        action: str,
        user_id: int,
        revision: int,
    ) -> None:
        """Handle edit preview actions and persist confirmed edits."""
        message = callback_query.message
        unique_id = message.chat.id + user_id
        if callback_query.from_user.id != user_id:
            await callback_query.answer(
                "This button belongs to another user.",
                show_alert=True,
            )
            return

        edit_entry = cls.edit_cache.get(unique_id)
        if not edit_entry:
            await callback_query.answer(
                "This edit session has expired.",
                show_alert=True,
            )
            return

        if edit_entry["revision"] != revision:
            await callback_query.answer(
                "The edit changed after this preview. Run /preview_edit again.",
                show_alert=True,
            )
            return

        if action == "cancel":
            cls.edit_cache.pop(unique_id, None)
            cls.files_cache.pop(unique_id, None)
            ConversationFilter.deactivate_conversation(unique_id)
            await callback_query.answer("Edit cancelled.")
            await message.edit_text("❌ Edit cancelled. The link was not changed.")
            return

        if action == "continue":
            ConversationFilter.activate_conversation(unique_id)
            await callback_query.answer("You can continue editing.")
            await message.edit_text(
                "Continue editing: send files to add, use /remove_file [number], "
                "or use /preview_edit when ready.",
            )
            return

        if action != "confirm":
            await callback_query.answer(
                "Invalid edit action.",
                show_alert=True,
            )
            return

        await cls.save_edit(
            client=client,
            callback_query=callback_query,
            unique_id=unique_id,
            edit_entry=edit_entry,
        )

    @classmethod
    async def save_edit(
        cls,
        client: Client,
        callback_query: CallbackQuery,
        unique_id: int,
        edit_entry: EditEntry,
    ) -> None:
        """Persist the edit and clean up removed backup messages."""
        message = callback_query.message
        retained_originals = [
            file
            for index, file in enumerate(edit_entry["original_files"])
            if index not in edit_entry["removed_original"]
        ]
        additions = cls.files_cache[unique_id]["files"]
        if not edit_entry["removed_original"] and not additions:
            await callback_query.answer(
                "There are no changes to save.",
                show_alert=True,
            )
            return

        if (
            additions
            and options.settings.BACKUP_FILES
            and edit_entry["file_origin"] != config.BACKUP_CHANNEL
        ):
            await callback_query.answer(
                "This older link does not use the configured backup channel. "
                "Adding files is blocked to avoid mixing incompatible file sources.",
                show_alert=True,
            )
            return

        await callback_query.answer("Saving changes...")
        added_file_data, added_backup_message_ids = await cls.prepare_added_files(
            client=client,
            message=message,
            additions=additions,
            file_origin=edit_entry["file_origin"],
        )

        updated_files = retained_originals + added_file_data
        if not updated_files:
            await callback_query.answer(
                "An edited link must contain at least one file.",
                show_alert=True,
            )
            return

        try:
            updated = await cls.database.update_link_document(
                base64_file_link=edit_entry["link"],
                file_data=updated_files,
                file_origin=edit_entry["file_origin"],
                expected_files=edit_entry["original_files"],
            )
        except PyMongoError:
            await cls.cleanup_backup_messages(
                client=client,
                message_ids=added_backup_message_ids,
                link=edit_entry["link"],
                reason="unsaved",
            )
            raise
        if not updated:
            cleanup_failed = await cls.cleanup_backup_messages(
                client=client,
                message_ids=added_backup_message_ids,
                link=edit_entry["link"],
                reason="uncommitted",
            )
            cls.edit_cache.pop(unique_id, None)
            cls.files_cache.pop(unique_id, None)
            ConversationFilter.deactivate_conversation(unique_id)
            text = (
                "⚠️ This link changed or was deleted while you were editing. "
                "Your changes were not saved; start again to avoid overwriting newer edits."
            )
            if cleanup_failed:
                text += "\nSome uncommitted backup files could not be removed."
            await message.edit_text(text)
            return

        removed_message_ids = [
            file["message_id"]
            for index, file in enumerate(edit_entry["original_files"])
            if index in edit_entry["removed_original"]
            and file.get("message_id") is not None
        ]
        cleanup_failed = (
            await cls.cleanup_backup_messages(
                client=client,
                message_ids=removed_message_ids,
                link=edit_entry["link"],
                reason="replaced",
            )
            if edit_entry["file_origin"] == config.BACKUP_CHANNEL
            else False
        )

        cls.edit_cache.pop(unique_id, None)
        cls.files_cache.pop(unique_id, None)
        ConversationFilter.deactivate_conversation(unique_id)
        link = f"https://t.me/{client.me.username}?start={edit_entry['link']}"  # type: ignore[reportOptionalMemberAccess]
        result_text = f"✅ Changes saved. The existing link still works:\n{link}"
        if cleanup_failed:
            result_text += (
                "\n⚠️ The link was updated, but some removed backup files "
                "could not be deleted."
            )
        await message.edit_text(
            result_text,
            disable_web_page_preview=True,
        )

    @classmethod
    async def prepare_added_files(
        cls,
        client: Client,
        message: Message,
        additions: list[dict],
        file_origin: int,
    ) -> tuple[list[dict], list[int]]:
        """Prepare new file records, backing them up when the link uses backups."""
        if not additions or file_origin != config.BACKUP_CHANNEL:
            return (
                [
                    {
                        key: value
                        for key, value in file.items()
                        if key != "file_name"
                    }
                    for file in additions
                ],
                [],
            )

        added_file_data = []
        added_backup_message_ids = []
        for start in range(0, len(additions), 100):
            chunk = additions[start : start + 100]
            forwarded = await client.forward_messages(
                chat_id=config.BACKUP_CHANNEL,
                from_chat_id=message.chat.id,
                message_ids=[file["message_id"] for file in chunk],
                hide_sender_name=True,
            )
            forwarded_messages = forwarded if isinstance(forwarded, list) else [forwarded]
            if len(forwarded_messages) != len(chunk) or any(
                not forwarded_message for forwarded_message in forwarded_messages
            ):
                raise EditLinkError

            for source_file, forwarded_message in zip(
                chunk,
                forwarded_messages,
                strict=True,
            ):
                file_type = (
                    forwarded_message.document
                    or forwarded_message.video
                    or forwarded_message.photo
                    or forwarded_message.audio
                    or forwarded_message.sticker
                )
                if not file_type:
                    raise EditLinkError

                added_file_data.append(
                    {
                        "caption": (
                            forwarded_message.caption.markdown
                            if forwarded_message.caption
                            else None
                        ),
                        "file_id": file_type.file_id,
                        "message_id": forwarded_message.id,
                        "media_group_id": (
                            forwarded_message.media_group_id
                            or source_file.get("media_group_id")
                        ),
                    },
                )
                added_backup_message_ids.append(forwarded_message.id)

        return added_file_data, added_backup_message_ids

    @classmethod
    async def cleanup_backup_messages(
        cls,
        client: Client,
        message_ids: list[int],
        link: str,
        reason: str,
    ) -> bool:
        """Delete backup messages and report cleanup failures."""
        if not message_ids:
            return False

        try:
            await client.delete_messages(
                chat_id=config.BACKUP_CHANNEL,
                message_ids=message_ids,
            )
        except RPCError:
            cls.logger.exception(
                "Failed to clean up %s backup files for link %s",
                reason,
                link,
            )
            return True

        return False

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
    & PyroFilters.create_conversation_filter(
        convo_start=[
            "/make_files",
            "/batch",
            "/batch_files",
            "/templink",
            "/edit_link",
        ],
        convo_stop=[
            "/make_link",
            "/batch_link",
            "/temp_link",
            "/mpost",
            "/preview_edit",
        ],
        convo_cancel="/cancel",
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
        /mpost: finish a permanent-link conversation and compose a
            channel-ready post (photo + caption + buttons) with it.
        /templink: initiate a temporary-link conversation and choose expiry.
        /edit_link: edit files behind an existing link.
        /preview_edit: review pending changes and save them.
        /cancel: cancel the active file-link conversation.
    """
    if message.convo_start:
        return await MakeFilesCommand.handle_convo_start(
            client=client,
            message=message,
        )

    if message.convo_cancel:
        return await MakeFilesCommand.handle_cancel(
            client=client,
            message=message,
        )

    if message.conversation:
        return await MakeFilesCommand.handle_conversation(
            client=client,
            message=message,
        )

    if message.convo_stop:
        stop_text = message.text or message.caption or ""

        if stop_text.strip() == "/mpost":
            return await MakeFilesCommand.handle_mpost_stop(
                client=client,
                message=message,
            )

        return await MakeFilesCommand.handle_convo_stop(
            client=client,
            message=message,
        )

    return None


@Client.on_callback_query(
    filters.regex(r"^mf_get_link:\d+$"),
)
async def mf_get_link_callback(
    client: Client,
    callback_query: CallbackQuery,
) -> None:
    """Handle the "Get Link" button shown while sending files."""
    try:
        _, user_id_text = callback_query.data.split(":", 1)
        user_id = int(user_id_text)
    except (AttributeError, ValueError):
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    if callback_query.from_user.id != user_id:
        await callback_query.answer(
            "This button belongs to another user.",
            show_alert=True,
        )
        return

    unique_id = callback_query.message.chat.id + user_id

    await callback_query.answer("Creating link...")

    file_link, error = await MakeFilesCommand.finalize_link(
        client=client,
        message=callback_query.message,
        unique_id=unique_id,
    )

    if error is not None:
        return

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

    text = f"Here is your link:\n>{link}"

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


@Client.on_callback_query(
    filters.regex(r"^mf_make_post:\d+$"),
)
async def mf_make_post_callback(
    client: Client,
    callback_query: CallbackQuery,
) -> None:
    """Handle the "Make Post" button shown while sending files."""
    try:
        _, user_id_text = callback_query.data.split(":", 1)
        user_id = int(user_id_text)
    except (AttributeError, ValueError):
        await callback_query.answer("Invalid request.", show_alert=True)
        return

    if callback_query.from_user.id != user_id:
        await callback_query.answer(
            "This button belongs to another user.",
            show_alert=True,
        )
        return

    unique_id = callback_query.message.chat.id + user_id

    await callback_query.answer("Creating link...")

    file_link, error = await MakeFilesCommand.finalize_link(
        client=client,
        message=callback_query.message,
        unique_id=unique_id,
    )

    if error is not None:
        return

    from bot.plugins.base.mpost import MPostCommand  # noqa: PLC0415

    await MPostCommand.begin(
        client=client,
        message=callback_query.message,
        unique_id=unique_id,
        file_link=file_link,  # type: ignore[reportArgumentType]
    )


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


@Client.on_callback_query(
    filters.regex(r"^edit_link:(confirm|continue|cancel):\d+:\d+$"),
)
async def edit_link_callback(
    client: Client,
    callback_query: CallbackQuery,
) -> None:
    """Handle preview, confirmation, and cancellation of link edits."""
    data = callback_query.data
    if not isinstance(data, str):
        await callback_query.answer(
            "Invalid edit request.",
            show_alert=True,
        )
        return

    try:
        _, action, user_id_text, revision_text = data.split(":", 3)
        user_id = int(user_id_text)
        revision = int(revision_text)
    except (AttributeError, ValueError):
        await callback_query.answer(
            "Invalid edit request.",
            show_alert=True,
        )
        return

    await MakeFilesCommand.finish_edit(
        client=client,
        callback_query=callback_query,
        action=action,
        user_id=user_id,
        revision=revision,
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

HelpCmd.set_help(
    command="edit_link",
    description=(
        "Edit files behind an existing link without changing its URL.\n\n"
        "Use /edit_link, send the bot link or start token, then send files to add. "
        "Use /remove_file [number] to remove files and /preview_edit to review "
        "and confirm the changes."
    ),
    allow_global=False,
    allow_non_admin=False,
)
