from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import Message

from bot.database import MongoDB
from bot.options import options
from bot.utilities.helpers import RateLimiter
from bot.utilities.pyrofilters import PyroFilters
from bot.utilities.pyrotools import HelpCmd

database = MongoDB()


def _preset_name(message: Message) -> str | None:
    """Extract and normalize the preset name argument."""
    if len(message.command) < 2:
        return None

    return message.command[1].strip().lower()


@Client.on_message(
    filters.private & PyroFilters.admin() & filters.command("preset_save"),
)
@RateLimiter.hybrid_limiter(func_count=1)
async def preset_save_cmd(client: Client, message: Message) -> Message:  # noqa: ARG001
    """Save a named /mpost button-template preset.

    **Usage:**
        Reply to a message containing the button template with:
        `/preset_save <name>`

    **Template syntax** (one row per line, buttons separated by `|`):
        `Label - URL`
        `Label - URL - style:red`

    "Use `[Link]` in a button URL and it will be replaced with the
    generated file link when the post is created."

    **Example template:**
        DOWNLOAD - [Link] | MEMBERSHIP - https://t.me/YourBot?start=abc
        ☠️ JOIN - https://t.me/some_channel - style:red
    """
    name = _preset_name(message)

    if not name:
        return await message.reply(
            text=f"missing arguments:\n{preset_save_cmd.__doc__}",
            quote=True,
        )

    replied = message.reply_to_message

    if not replied or not replied.text:
        return await message.reply(
            text=(
                "Reply to a **text message** containing the button "
                "template with `/preset_save <name>`."
            ),
            quote=True,
        )

    template = replied.text.markdown

    await database.save_mpost_preset(name=name, template=template)

    return await message.reply(
        text=(
            f"✅ Preset `{name}` saved.\n\n"
            "Use `/mpost` after `/make_files` to use this preset."
        ),
        quote=True,
    )


@Client.on_message(
    filters.private & PyroFilters.admin() & filters.command("preset_list"),
)
async def preset_list_cmd(client: Client, message: Message) -> Message:  # noqa: ARG001
    """List saved /mpost button-template presets."""
    presets = await database.get_mpost_presets()

    if not presets:
        return await message.reply(
            "ℹ️ No presets saved yet. Use `/preset_save <name>`.",
            quote=True,
        )

    default_name = str(options.settings.MPOST_DEFAULT_PRESET)

    lines = ["**Saved /mpost presets:**\n"]

    for preset in presets:
        name = preset["_id"]
        marker = " (default)" if name == default_name else ""
        lines.append(f"- `{name}`{marker}")

    return await message.reply(
        "\n".join(lines),
        quote=True,
    )


@Client.on_message(
    filters.private & PyroFilters.admin() & filters.command("preset_view"),
)
async def preset_view_cmd(client: Client, message: Message) -> Message:  # noqa: ARG001
    """View a saved /mpost preset's raw template.

    **Usage:** `/preset_view <name>`
    """
    name = _preset_name(message)

    if not name:
        return await message.reply(
            text=f"missing arguments:\n{preset_view_cmd.__doc__}",
            quote=True,
        )

    preset = await database.get_mpost_preset(name)

    if not preset:
        return await message.reply(
            f"❌ No preset named `{name}`.",
            quote=True,
        )

    return await message.reply(
        f"**Preset `{name}`:**\n```\n{preset['template']}\n```",
        quote=True,
    )


@Client.on_message(
    filters.private & PyroFilters.admin() & filters.command("preset_delete"),
)
async def preset_delete_cmd(client: Client, message: Message) -> Message:  # noqa: ARG001
    """Delete a saved /mpost preset.

    **Usage:** `/preset_delete <name>`
    """
    name = _preset_name(message)

    if not name:
        return await message.reply(
            text=f"missing arguments:\n{preset_delete_cmd.__doc__}",
            quote=True,
        )

    removed = await database.delete_mpost_preset(name)

    if not removed:
        return await message.reply(
            f"❌ No preset named `{name}`.",
            quote=True,
        )

    return await message.reply(
        f"✅ Preset `{name}` deleted.",
        quote=True,
    )


HelpCmd.set_help(
    command="preset_save",
    description=preset_save_cmd.__doc__,
    allow_global=False,
    allow_non_admin=False,
)

HelpCmd.set_help(
    command="preset_list",
    description=preset_list_cmd.__doc__,
    allow_global=False,
    allow_non_admin=False,
)

HelpCmd.set_help(
    command="preset_view",
    description=preset_view_cmd.__doc__,
    allow_global=False,
    allow_non_admin=False,
)

HelpCmd.set_help(
    command="preset_delete",
    description=preset_delete_cmd.__doc__,
    allow_global=False,
    allow_non_admin=False,
)
