import asyncio
import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import ClassVar

from lru import LRU
from pyrogram.client import Client
from pyrogram.types import Message

from bot.config import config
from bot.database import MongoDB
from bot.options import options


class RateLimiter:
    """
    Rate limiters used by the bot.

    The normal hybrid limiter is kept unchanged.

    The daily file limiter is persistent and stores its data in the
    normal MongoDB cluster (MONGO_DB_URL), never in the Files cluster.

    All numeric limits below are read live from `options.settings` (configurable
    via /option, e.g. `/option RATE_LIMIT_PER_MINUTE 40`) so they can be tuned
    without a restart. The class vars are only used as a fallback default before
    settings are loaded from the database.
    """

    logger = logging.getLogger(__name__)

    MAX_EXECUTIONS_PER_MINUTE_SAME_CHAT: ClassVar[int] = 25

    DAILY_FILE_LIMIT: ClassVar[int] = 20
    DAILY_FILE_WINDOW_SECONDS: ClassVar[int] = 86400

    chat_execution_counts: ClassVar[LRU] = LRU(100)

    last_second_reset: ClassVar[float] = time.perf_counter()
    last_minute_reset: ClassVar[float] = time.perf_counter()

    database = MongoDB()

    @classmethod
    def cooldown_limiter(cls) -> None:
        """
        Used to update every minute with new time and chat_execution_counts
        per ID.

        Should only be run once during startup.
        """
        cls.logger.info("cooldown_limiter Started...")

        while True:
            # Re-read every cycle so a live /option change takes effect
            # without a restart.
            exec_per_min = options.settings.RATE_LIMIT_PER_MINUTE

            current_time = time.perf_counter()

            if current_time - cls.last_minute_reset >= 60:
                cls.last_minute_reset = current_time

                for key, value in cls.chat_execution_counts.items():
                    queue = value.get("queue", 0)

                    if queue <= exec_per_min:
                        execs = queue
                        new_queue = 0
                    else:
                        execs = exec_per_min
                        new_queue = queue - exec_per_min

                    if execs == 0 and new_queue == 0:
                        cls.chat_execution_counts.pop(key)
                    else:
                        cls.chat_execution_counts.update(
                            {
                                key: {
                                    "exec": execs,
                                    "queue": new_queue,
                                },
                            },
                        )

            time.sleep(2)

    @classmethod
    def hybrid_limiter(
        cls,
        func_count: int = 1,
    ) -> Callable[[Callable], Callable]:
        """
        Normal per-minute hybrid rate limiter.
        """

        def decorator(func: Callable) -> Callable:
            @wraps(func)
            async def wrapper(
                client: Client,
                message: Message,
                *args: tuple,
                **kwargs: dict,
            ) -> bool:
                if not config.RATE_LIMITER:
                    return await func(
                        client,
                        message,
                        *args,
                        **kwargs,
                    )

                chat_id = message.chat.id

                cls.chat_execution_counts.setdefault(
                    chat_id,
                    {
                        "exec": 0,
                        "queue": 0,
                    },
                )

                user_dict = cls.chat_execution_counts[chat_id]

                # Configurable via /option RATE_LIMIT_PER_MINUTE <n>.
                max_per_minute = options.settings.RATE_LIMIT_PER_MINUTE

                now = time.perf_counter()
                elapsed_time_minute = (
                    now - cls.last_minute_reset
                )

                if (
                    user_dict["exec"]
                    >= max_per_minute
                ):
                    cls.chat_execution_counts[chat_id]["queue"] += (
                        func_count
                    )

                    sleep_time = 60

                    sleep_queue = (
                        user_dict["queue"]
                        // max_per_minute
                    ) + 1

                    total_sleep = (
                        sleep_time * sleep_queue
                    ) - elapsed_time_minute

                    if total_sleep > 0:
                        cls.logger.info(
                            "Waiting for %d seconds... "
                            "before next execution, id: %d",
                            total_sleep,
                            chat_id,
                        )

                        await asyncio.sleep(total_sleep)

                cls.chat_execution_counts.setdefault(
                    chat_id,
                    {
                        "exec": 0,
                        "queue": 0,
                    },
                )["exec"] += func_count

                return await func(
                    client,
                    message,
                    *args,
                    **kwargs,
                )

            return wrapper

        return decorator

    @classmethod
    def daily_file_limiter(
        cls,
        func: Callable,
    ) -> Callable:
        """
        Persistent rolling 24-hour limit for file-link requests.

        Each user can request at most 20 file links during a
        rolling 24-hour window.

        The counter is stored in MongoDB Cluster 2.
        """

        @wraps(func)
        async def wrapper(
            client: Client,
            message: Message,
            *args: tuple,
            **kwargs: dict,
        ) -> bool:
            if not config.RATE_LIMITER:
                return await func(
                    client,
                    message,
                    *args,
                    **kwargs,
                )

            user_id = message.from_user.id

            if user_id in config.ROOT_ADMINS_ID:
                return await func(client, message, *args, **kwargs)

            # Configurable via /option DAILY_FILE_LIMIT <n> and
            # /option DAILY_FILE_LIMIT_WINDOW_SECONDS <n>.
            daily_limit = options.settings.DAILY_FILE_LIMIT
            window_seconds = options.settings.DAILY_FILE_LIMIT_WINDOW_SECONDS

            allowed, count, reset_at = (
                await cls.database.check_daily_file_limit(
                    user_id=user_id,
                    limit=daily_limit,
                    window_seconds=window_seconds,
                )
            )

            if not allowed:
                remaining_seconds = max(
                    0,
                    int(reset_at - time.time()),
                )

                remaining_hours = remaining_seconds // 3600
                remaining_minutes = (
                    remaining_seconds % 3600
                ) // 60

                if remaining_hours:
                    retry_text = (
                        f"{remaining_hours} hour(s)"
                        if remaining_minutes == 0
                        else (
                            f"{remaining_hours} hour(s) "
                            f"{remaining_minutes} minute(s)"
                        )
                    )
                else:
                    retry_text = (
                        f"{max(1, remaining_minutes)} minute(s)"
                    )

                window_hours = max(1, window_seconds // 3600)

                await message.reply(
                    "⚠️ **Daily file limit reached.**\n\n"
                    f"You can use up to **{daily_limit} file links "
                    f"every {window_hours} hour(s)**.\n\n"
                    f"Try again in approximately **{retry_text}**.",
                    quote=True,
                )

                return message.stop_propagation()

            cls.logger.debug(
                "Daily file limit: user=%d count=%d/%d",
                user_id,
                count,
                daily_limit,
            )

            return await func(
                client,
                message,
                *args,
                **kwargs,
            )

        return wrapper
