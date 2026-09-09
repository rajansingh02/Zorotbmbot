import asyncio
import logging
import threading
from datetime import datetime, timezone

from pyrogram.client import Client
from pyrogram.sync import idle
from rich.logging import RichHandler
from rich.traceback import install

from bot.config import config
from bot.database import MongoDB
from bot.options import options
from bot.utilities.helpers import RateLimiter
from bot.utilities.http_server import HTTPServer
from bot.utilities.schedule_manager import schedule_manager

install(show_locals=True)

logging.basicConfig(
    level="INFO",
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler()],
)

logger = logging.getLogger(__name__)

try:
    import uvloop  # type: ignore[reportMissingImports]

    uvloop.install()
    logger.info("Using UVLoop for enhanced performance")
except ImportError:
    logger.warning("UVLoop not installed. Falling back to asyncio")

background_tasks = set()


async def cleanup_expired_links(
    bot_client: Client,
    database: MongoDB,
) -> None:
    """
    Delete expired temporary links and their backup-channel files.
    """

    while True:
        try:
            expired_links = await database.get_expired_temporary_links(
                now=datetime.now(timezone.utc),
            )

            for file_document in expired_links:
                file_link = file_document["_id"]
                file_origin = file_document.get("file_origin")
                files = file_document.get("files", [])

                # Delete the backup-channel messages first.
                # This keeps the database document available for a
                # later cleanup attempt if Telegram temporarily fails.
                if file_origin == config.BACKUP_CHANNEL and files:
                    message_ids = [
                        file["message_id"]
                        for file in files
                        if file.get("message_id") is not None
                    ]

                    if message_ids:
                        try:
                            await bot_client.delete_messages(
                                chat_id=file_origin,
                                message_ids=message_ids,
                            )
                        except Exception:
                            logger.exception(
                                "Failed deleting expired backup files "
                                "for link %s",
                                file_link,
                            )
                            continue

                deleted = await database.delete_link_document(
                    base64_file_link=file_link,
                )

                if deleted:
                    logger.info(
                        "Expired temporary link deleted: %s",
                        file_link,
                    )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Temporary-link cleanup failed",
            )

        # Check once every minute.
        await asyncio.sleep(60)


async def main() -> None:
    bot_client = Client(
        name=config.BOT_SESSION,
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        workers=config.BOT_WORKER,
        plugins={
            "root": f"{__package__}/plugins"
            if __package__
            else "plugins",
        },
        max_message_cache_size=config.BOT_MAX_MESSAGE_CACHE_SIZE,
    )

    # Load database settings.
    await options.load_settings()
    await bot_client.start()

    # Load force-subscription channels from MongoDB.
    database = MongoDB()
    fsub_channels = await database.get_fsub_channels()

    config.channels_n_invite = {
        str(channel["channel_id"]): channel
        for channel in fsub_channels
    }

    await schedule_manager.start()

    # Start temporary-link expiry cleanup.
    expiry_task = asyncio.create_task(
        cleanup_expired_links(
            bot_client=bot_client,
            database=database,
        ),
    )
    background_tasks.add(expiry_task)

    task = None

    if config.HTTP_SERVER:
        http_server = HTTPServer(
            host=config.HOSTNAME,
            port=config.PORT,
        )
        task = asyncio.create_task(
            http_server.run_server(),
        )
        background_tasks.add(task)

    if config.RATE_LIMITER:
        thread = threading.Thread(
            target=RateLimiter.cooldown_limiter,
        )
        thread.daemon = True
        thread.start()

    try:
        await idle()
    finally:
        expiry_task.cancel()

        try:
            await expiry_task
        except asyncio.CancelledError:
            pass

        background_tasks.discard(expiry_task)

        if task:
            task.cancel()
            background_tasks.discard(task)

        await bot_client.stop()


asyncio.run(main())
