import re
import uuid
from inspect import cleandoc
from urllib.parse import parse_qs, urlparse

from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import Message

from bot.config import config
from bot.database import MongoDB
from bot.options import options
from bot.utilities.helpers import DataEncoder, RateLimiter
from bot.utilities.pyrofilters import PyroFilters
from bot.utilities.pyrotools import HelpCmd


database = MongoDB()


def extract_bot_start_link(value: str) -> str | None:
    """Extract the start payload from a bot start link."""
    try:
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        start_values = query.get("start")

        if start_values:
            return start_values[0] or None
    except ValueError:
        pass

    return None


def extract_channel_message_id(value: str) -> int | None:
    """Extract a Telegram channel message ID from a message link."""
    value = value.strip()

    # Public channel:
    # https://t.me/channelusername/12345
    # https://telegram.me/channelusername/12345
    public_match = re.match(
        r"^(?:https?://)?(?:t\.me|telegram\.me)/"
        r"[A-Za-z0-9_]+/(\d+)(?:\?.*)?$",
        value,
        re.IGNORECASE,
    )

    if public_match:
        return int(public_match.group(1))

    # Private channel:
    # https://t.me/c/1234567890/12345
    # https://telegram.me/c/1234567890/12345
    private_match = re.match(
        r"^(?:https?://)?(?:t\.me|telegram\.me)/"
        r"c/\d+/(\d+)(?:\?.*)?$",
        value,
        re.IGNORECASE,
    )

    if private_match:
        return int(private_match.group(1))

    return None


def get_argument_type(
    value: str,
) -> tuple[str, int | None]:
    """Identify the supplied getlink argument."""
    value = value.strip()

    if value.isdigit():
        return "message_id", int(value)

    if extract_bot_start_link(value):
        return "bot_link", None

    message_id = extract_channel_message_id(value)

    if message_id is not None:
        return "channel_link", message_id

    return "invalid", None


def generate_file_link() -> str:
    """Generate a new encoded file link."""
    unique_link = f"{uuid.uuid4().int}"
    return DataEncoder.encode_data(unique_link)


async def create_link_from_backup_message(
    message: Message,
) -> str | None:
    """Create a Files DB link from a verified backup-channel message."""
    file_type = (
        message.document
        or message.video
        or message.photo
        or message.audio
        or message.sticker
    )

    if not file_type:
        return None

    file_data = [
        {
            "caption": (
                message.caption.markdown
                if message.caption
                else None
            ),
            "file_id": file_type.file_id,
            "message_id": message.id,
            "media_group_id": message.media_group_id,
        },
    ]

    file_link = generate_file_link()

    added = await database.add_file(
        file_link=file_link,
        file_origin=config.BACKUP_CHANNEL,
        file_data=file_data,
    )

    if not added:
        return None

    return file_link


@Client.on_message(
    filters.private
    & PyroFilters.admin(allow_global=True)
    & filters.command("getlink"),
)
@RateLimiter.hybrid_limiter(func_count=1)
async def get_link(
    client: Client,
    message: Message,
) -> Message:
    """Get an existing link for a file in the backup channel.

    **Usage:**

    /getlink [backup message ID]

    /getlink [backup channel message link]

    /getlink [existing bot file link]

    Backup-channel messages that are not yet in the database
    will automatically receive a new link.
    """

    if not message.command[1:]:
        return await message.reply(
            text=cleandoc(get_link.__doc__ or ""),
            quote=True,
        )

    argument = message.text.split(maxsplit=1)[1].strip()

    argument_type, message_id = get_argument_type(argument)

    # ---------------------------------------------------------
    # Backup message ID
    # ---------------------------------------------------------
    if argument_type == "message_id":
        existing = await database.get_link_by_backup_message_id(
            message_id=message_id,
            backup_channel=config.BACKUP_CHANNEL,
        )

        if existing:
            file_link, _ = existing

        else:
            try:
                backup_message = await client.get_messages(
                    config.BACKUP_CHANNEL,
                    message_id,
                )
            except Exception:
                backup_message = None

            if not backup_message or not backup_message.id:
                return await message.reply(
                    text=(
                        "❌ That message does not exist in the "
                        "configured backup channel."
                    ),
                    quote=True,
                )

            file_link = await create_link_from_backup_message(
                backup_message,
            )

            if not file_link:
                return await message.reply(
                    text=(
                        "❌ That backup-channel message does not "
                        "contain a supported file."
                    ),
                    quote=True,
                )

    # ---------------------------------------------------------
    # Backup channel message link
    # ---------------------------------------------------------
    elif argument_type == "channel_link":
        existing = await database.get_link_by_backup_message_id(
            message_id=message_id,
            backup_channel=config.BACKUP_CHANNEL,
        )

        if existing:
            file_link, _ = existing

        else:
            # IMPORTANT:
            # Do not trust the channel ID contained in the URL.
            # Always fetch the message from the configured
            # BACKUP_CHANNEL.
            try:
                backup_message = await client.get_messages(
                    config.BACKUP_CHANNEL,
                    message_id,
                )
            except Exception:
                backup_message = None

            if not backup_message or not backup_message.id:
                return await message.reply(
                    text=(
                        "❌ That message does not exist in the "
                        "configured backup channel."
                    ),
                    quote=True,
                )

            file_link = await create_link_from_backup_message(
                backup_message,
            )

            if not file_link:
                return await message.reply(
                    text=(
                        "❌ That backup-channel message does not "
                        "contain a supported file."
                    ),
                    quote=True,
                )

    # ---------------------------------------------------------
    # Existing bot /start file link
    # ---------------------------------------------------------
    elif argument_type == "bot_link":
        file_link = extract_bot_start_link(argument)

        if not file_link:
            return await message.reply(
                text="❌ Invalid bot file link.",
                quote=True,
            )

        file_document = await database.get_link_document(
            base64_file_link=file_link,
        )

        if not file_document:
            return await message.reply(
                text=(
                    "❌ That file link does not exist or "
                    "has already been deleted."
                ),
                quote=True,
            )

        if file_document.get("file_origin") != config.BACKUP_CHANNEL:
            return await message.reply(
                text=(
                    "❌ That link is not associated with the "
                    "configured backup channel."
                ),
                quote=True,
            )

    # ---------------------------------------------------------
    # Invalid input
    # ---------------------------------------------------------
    else:
        return await message.reply(
            text=(
                "❌ Invalid input.\n\n"
                "Use one of these:\n"
                "`/getlink 12345`\n"
                "`/getlink https://t.me/c/1234567890/12345`\n"
                "`/getlink https://t.me/MyChannel/12345`\n"
                "`/getlink https://t.me/YourBot?start=...`"
            ),
            quote=True,
        )

    me = await client.get_me()

    if not me.username:
        return await message.reply(
            text="❌ Cannot determine the bot username.",
            quote=True,
        )

    link = f"https://t.me/{me.username}?start={file_link}"

    return await message.reply(
        text=f"🔗 Existing file link:\n{link}",
        quote=True,
        disable_web_page_preview=True,
    )


HelpCmd.set_help(
    command="getlink",
    description=get_link.__doc__,
    allow_global=True,
    allow_non_admin=False,
)
