from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import config
from bot.database import MongoDB
from bot.options import options
from bot.utilities.helpers import (
    DataEncoder,
    DataValidationError,
    PyroHelper,
    RateLimiter,
)
from bot.utilities.pyrofilters import PyroFilters, SubscriptionMessage
from bot.utilities.pyrotools import FileResolverModel, HelpCmd, Pyrotools
from bot.utilities.schedule_manager import schedule_manager

database = MongoDB()


class FileSender:
    """Used to manage file sending functions between codexbotz and teleshare."""

    forward_limit_size = 100

    @staticmethod
    async def codexbotz(
        client: Client,
        codex_message_ids: list[int],
        chat_id: int,
        from_chat_id: int,
        protect_content: bool,
    ) -> list[Message]:
        all_sent_files = []

        if len(codex_message_ids) == 1:
            send_files = await client.copy_message(
                chat_id=chat_id,
                from_chat_id=from_chat_id,
                message_id=codex_message_ids[0],
                protect_content=protect_content,
            )

            all_sent_files.append(send_files)

        else:
            codex_message_ids_chunk = [
                codex_message_ids[i : i + FileSender.forward_limit_size]
                for i in range(
                    0,
                    len(codex_message_ids),
                    FileSender.forward_limit_size,
                )
            ]

            for codex_files in codex_message_ids_chunk:
                send_files = await client.forward_messages(
                    chat_id=chat_id,
                    from_chat_id=from_chat_id,
                    message_ids=codex_files,
                    hide_sender_name=True,
                    protect_content=protect_content,
                )

                if isinstance(send_files, list):
                    all_sent_files.extend(send_files)
                else:
                    all_sent_files.append(send_files)

        return all_sent_files

    @staticmethod
    async def teleshare(
        client: Client,
        chat_id: int,
        file_data: list[FileResolverModel],
        file_origin: int,
        protect_content: bool,
    ) -> list[Message]:
        all_sent_files = []

        if len(file_data) == 1:
            send_files = await Pyrotools.send_media(
                client=client,
                chat_id=chat_id,
                file_data=file_data[0],
                file_origin=file_origin,
                protect_content=protect_content,
            )
            all_sent_files.append(send_files)

        else:
            file_data_chunk = [
                file_data[i : i + FileSender.forward_limit_size]
                for i in range(
                    0,
                    len(file_data),
                    FileSender.forward_limit_size,
                )
            ]

            for i_file_data in file_data_chunk:
                send_files = await Pyrotools.send_media_manager(
                    client=client,
                    chat_id=chat_id,
                    file_data=i_file_data,
                    file_origin=file_origin,
                    protect_content=protect_content,
                )

                if isinstance(send_files, list):
                    all_sent_files.extend(send_files)
                else:
                    all_sent_files.append(send_files)

        return all_sent_files


@Client.on_message(
    filters.command("start") & filters.private & PyroFilters.subscription(),
    group=0,
)
@RateLimiter.daily_file_limiter
async def file_start(
    client: Client,
    message: Message,
) -> Message:
    """
    Handle start command, it returns files if a link is included otherwise sends the user a request.

    **Usage:**
        /start [optional file_link]
    """
    if not message.command[1:]:
        await PyroHelper.option_message(
            client=client,
            message=message,
            option_key=options.settings.START_MESSAGE,
        )
        return message.stop_propagation()

    await database.add_user(user_id=message.from_user.id)

    base64_file_link = message.text.split(maxsplit=1)[1]
    file_document = await database.get_link_document(
        base64_file_link=base64_file_link,
    )

    if not file_document:
        try:
            codex_message_ids = DataEncoder.codex_decode(
                base64_string=base64_file_link,
                backup_channel=config.BACKUP_CHANNEL,
            )
        except (DataValidationError, IndexError):
            await PyroHelper.option_message(
                client=client,
                message=message,
                option_key=options.settings.INVALID_LINK_MESSAGE,
            )
            return message.stop_propagation()

        send_files = await FileSender.codexbotz(
            client=client,
            codex_message_ids=codex_message_ids,
            chat_id=message.chat.id,
            from_chat_id=config.BACKUP_CHANNEL,
            protect_content=config.PROTECT_CONTENT,
        )

        if not send_files:
            await PyroHelper.option_message(
                client=client,
                message=message,
                option_key=options.settings.FILE_DOES_NOT_EXIST,
            )
            return message.stop_propagation()

    else:
        file_origin = file_document["file_origin"]
        file_data = [
            FileResolverModel(**file)
            for file in file_document["files"]
        ]

        send_files = await FileSender.teleshare(
            client=client,
            chat_id=message.chat.id,
            file_data=file_data,
            file_origin=file_origin,
            protect_content=config.PROTECT_CONTENT,
        )

    delete_n_seconds = options.settings.AUTO_DELETE_SECONDS

    additional_message = None

    if options.settings.ADDITIONAL_MESSAGE != 0:
        additional_message = await PyroHelper.option_message(
            client=client,
            message=message,
            option_key=options.settings.ADDITIONAL_MESSAGE,
        )

    if delete_n_seconds != 0:
        schedule_delete_message = [msg.id for msg in send_files]

        auto_delete_message = (
            options.settings.AUTO_DELETE_MESSAGE.format(
                int(delete_n_seconds / 60),
            )
            if not isinstance(options.settings.AUTO_DELETE_MESSAGE, int)
            else options.settings.AUTO_DELETE_MESSAGE
        )

        auto_delete_message_reply = await PyroHelper.option_message(
            client=client,
            message=message,
            option_key=auto_delete_message,
        )

        if auto_delete_message_reply:
            schedule_delete_message.append(auto_delete_message_reply.id)

        if additional_message:
            schedule_delete_message.append(additional_message.id)

        await schedule_manager.schedule_delete(
            client=client,
            chat_id=message.chat.id,
            message_ids=schedule_delete_message,
            delete_n_seconds=delete_n_seconds,
            base64_file_link=base64_file_link,
        )

    return message.stop_propagation()


@Client.on_message(
    filters.command("start") & filters.private,
    group=69,
)
@RateLimiter.hybrid_limiter(func_count=1)
async def return_start(
    client: Client,
    message: SubscriptionMessage,
) -> Message | None:
    """
    Handle start command without files or not subscribed.
    """

    if hasattr(message, "user_is_banned") and message.user_is_banned:
        return await PyroHelper.option_message(
            client=client,
            message=message,
            option_key=options.settings.BANNED_USER_MESSAGE,
        )

    channels_n_invite = config.channels_n_invite
    buttons = []

    for channel_info in channels_n_invite.values():
        title = channel_info["title"]

        # Keep the complete button text within 25 visible characters.
        if len(title) > 22:
            title = f"{title[:22]}..."

        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"📢 {title}",
                    url=channel_info["invite_link"],
                ),
            ],
        )

    # Extract the original /start payload.
    start_parameter = None

    if message.text:
        parts = message.text.split(maxsplit=1)

        if len(parts) == 2:
            start_parameter = parts[1].strip()

    # IMPORTANT:
    # Always add Try Again AFTER all channel buttons.
    if start_parameter:
        bot_username = client.me.username

        if bot_username:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="✅ Try Again",
                        url=(
                            f"https://t.me/{bot_username}"
                            f"?start={start_parameter}"
                        ),
                    ),
                ],
            )

    reply_markup = (
        InlineKeyboardMarkup(buttons)
        if buttons
        else None
    )

    force_sub_message = options.settings.FORCE_SUB_MESSAGE

    # Send the force-sub message directly instead of passing the
    # keyboard through PyroHelper.option_message().
    if isinstance(force_sub_message, int):
        message_origin = await client.get_messages(
            chat_id=config.BACKUP_CHANNEL,
            message_ids=force_sub_message,
        )

        if message_origin:
            return await message_origin.copy(
                chat_id=message.chat.id,
                reply_markup=reply_markup,
            )

    try:
        return await message.reply(
            text=str(force_sub_message),
            reply_markup=reply_markup,
            quote=True,
        )
    except Exception:
        return None


HelpCmd.set_help(
    command="start",
    description=file_start.__doc__,
    allow_global=True,
    allow_non_admin=True,
)
