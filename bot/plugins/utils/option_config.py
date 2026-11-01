from inspect import cleandoc
from urllib.parse import parse_qs, urlparse

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import Message

from bot.config import config
from bot.database import MongoDB
from bot.options import InvalidValueError, options
from bot.utilities.helpers import RateLimiter
from bot.utilities.pyrofilters import PyroFilters
from bot.utilities.pyrotools import HelpCmd

MISSING_ARGUMENT = 2
BOOLEN_CONVERT = {"true": True, "false": False}

database = MongoDB()


def extract_file_link(value: str) -> str | None:
    """Extract the start payload from a Telegram bot file link."""
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return None

    if parsed.scheme not in {"http", "https"}:
        return None

    hostname = (parsed.hostname or "").lower()

    if hostname not in {
        "t.me",
        "telegram.me",
        "www.t.me",
        "www.telegram.me",
    }:
        return None

    start = parse_qs(parsed.query).get("start")

    if not start or not start[0]:
        return None

    return start[0]


async def resolve_backup_file_link(
    client: Client,
    value: str,
) -> tuple[int | None, str | None]:
    """
    Resolve a bot file link to an existing backup-channel message ID.

    Returns:
        A tuple containing the message ID and an optional error message.
    """
    file_link = extract_file_link(value)

    if file_link is None:
        return None, None

    document = await database.get_link_document(
        base64_file_link=file_link,
    )

    if not document:
        return None, "That file link does not exist or has been deleted."

    if document.get("file_origin") != config.BACKUP_CHANNEL:
        return None, "That file link is not associated with the backup channel."

    files = document.get("files") or []

    if len(files) != 1:
        return (
            None,
            "That file link contains multiple messages. "
            "Please use a link containing exactly one backup-channel message.",
        )

    message_id = files[0].get("message_id")

    if message_id is None:
        return None, "That file link does not contain a valid backup message."

    try:
        message_id = int(message_id)
    except (TypeError, ValueError):
        return None, "That file link does not contain a valid backup message."

    # Make sure the referenced backup message still exists.
    backup_message = await client.get_messages(
        chat_id=config.BACKUP_CHANNEL,
        message_ids=message_id,
    )

    if not backup_message:
        return (
            None,
            "The backup-channel message for that file link no longer exists.",
        )

    return message_id, None


# Only options that represent messages are allowed to resolve
# Telegram bot file links.
#
# IMPORTANT:
# MPOST_DEFAULT_PRESET is intentionally NOT here.
#
# /option MPOST_DEFAULT_PRESET test
# must save the literal string "test".
MESSAGE_LINK_OPTIONS = {
    "FORCE_SUB_MESSAGE",
    "START_MESSAGE",
    "ADDITIONAL_MESSAGE",
    "USER_REPLY_TEXT",
    "CUSTOM_CAPTION",
    "AUTO_DELETE_MESSAGE",
    "INVALID_LINK_MESSAGE",
    "FILE_DOES_NOT_EXIST",
    "BANNED_USER_MESSAGE",
}


@Client.on_message(
    filters.private
    & PyroFilters.admin()
    & filters.command(["option", "settings"]),
)
@RateLimiter.hybrid_limiter(func_count=1)
async def option_config_cmd(
    client: Client,
    message: Message,
) -> Message | None:
    """Use to configure database options.

    **Usage:**
        /option key new_value
        /option key [reply to a message]

    **Example:**
        /option AUTO_DELETE_SECONDS 600
        /option FORCE_SUB_MESSAGE: reply to a message.
        /option START_MESSAGE https://t.me/YourBot?start=ABC123
    """

    cmd = message.command

    # /option
    if not cmd[1:]:
        options_configs = options.settings.model_dump()

        format_options = "\n".join(
            f"**{key}** ```\n{value}```"
            for key, value in options_configs.items()
        )

        func_doc = option_config_cmd.__doc__

        return await message.reply(
            text=(
                f"{format_options}\n\n"
                f"{cleandoc(func_doc) if func_doc else ''}"
            ),
            quote=True,
        )

    key = cmd[1].upper()

    # /option KEY without a value or reply.
    if len(cmd) == MISSING_ARGUMENT and not message.reply_to_message:
        return await message.reply(
            text=f"missing arguments:\n{option_config_cmd.__doc__}",
            quote=True,
        )

    # Validate option name.
    if key not in options.settings.__fields__:
        return await message.reply(
            "Please use a valid key to edit",
        )

    # ---------------------------------------------------------
    # Get the supplied value
    # ---------------------------------------------------------

    if message.reply_to_message:
        values = (
            message.reply_to_message.text.markdown
            if message.reply_to_message.text is not None
            else None
        )

        # If the replied message does not contain a numeric value,
        # copy it to the backup channel and store its message ID.
        if not values or not values.isdigit():
            copied_message = await message.reply_to_message.copy(
                chat_id=config.BACKUP_CHANNEL,
            )

            values = str(
                copied_message.id
                if isinstance(copied_message, Message)
                else values
            )

    else:
        # Everything after /option KEY is the value.
        values = message.text.markdown.split(
            maxsplit=2,
        )[2].lstrip()

    # ---------------------------------------------------------
    # Resolve existing Telegram file links
    # ---------------------------------------------------------
    #
    # This MUST NOT happen for every string setting.
    #
    # For example:
    #
    # /option MPOST_DEFAULT_PRESET test
    #
    # must remain:
    #
    # MPOST_DEFAULT_PRESET = "test"
    #
    # Only message-related options are allowed to resolve
    # Telegram bot file links.
    # ---------------------------------------------------------

    if key in MESSAGE_LINK_OPTIONS:
        message_id, error = await resolve_backup_file_link(
            client=client,
            value=values,
        )

        if error:
            return await message.reply(
                text=error,
                quote=True,
            )

        if message_id is not None:
            values = str(message_id)

    # ---------------------------------------------------------
    # Convert value to the appropriate basic type
    # ---------------------------------------------------------

    try:
        # Recognise both positive and negative integers.
        #
        # Examples:
        #   123
        #   -1001234567890
        #
        # Do not treat "" or "-" as integers.
        is_int_like = (
            values.lstrip("-").isdigit()
            and values not in {"", "-"}
        )

        change_value = (
            int(values)
            if is_int_like
            else BOOLEN_CONVERT.get(values.lower(), values)
        )

        update = await options.update_settings(
            key=key,
            value=change_value,
        )

        options_configs = update.model_dump()

        format_options = "\n".join(
            f"**{option_key}** ```\n{value}```"
            for option_key, value in options_configs.items()
        )

        final_message = await message.reply(
            text=(
                f"Updated:\n{format_options}\n\n"
                "__Note: if you see number instead of text it means it "
                "set a message to copy (this happens if you use reply to "
                "a message while setting the option key)__"
            ),
            quote=True,
        )

    except InvalidValueError:
        final_message = await message.reply(
            text=(
                "Please provide an existing key with int or digit for "
                "int value and str for str values"
            ),
            quote=True,
        )

    return final_message


HelpCmd.set_help(
    command="option",
    description=option_config_cmd.__doc__,
    allow_global=False,
    allow_non_admin=False,
    alias=["settings"],
)
