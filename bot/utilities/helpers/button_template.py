"""Parsing helpers for `/mpost` button-template presets.

Template syntax (one row per line, buttons in a row separated by `|`):

    DOWNLOAD - [Link] | MEMBERSHIP - https://t.me/YourBot?start=abc
    JOIN - https://t.me/some_channel - style:red

`[Link]` (case-insensitive) is replaced with the freshly generated file
link, in both button URLs and the post caption. The optional trailing
`- style:xxx` on a button is accepted for compatibility with other
bots' preset syntax but is otherwise ignored: Telegram bot buttons do
not support custom colors.
"""

import re
from typing import NamedTuple

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_LINK_PLACEHOLDER = re.compile(r"\[link\]", re.IGNORECASE)
_STYLE_CELL = re.compile(r"^style:", re.IGNORECASE)
_VALID_SCHEMES = ("http://", "https://", "tg://")


class ParsedButtons(NamedTuple):
    """Result of parsing a button-template preset."""

    markup: InlineKeyboardMarkup | None
    warnings: list[str]


def apply_link_placeholder(text: str, link: str) -> str:
    """Replace every `[Link]` (case-insensitive) occurrence with the real link."""
    return _LINK_PLACEHOLDER.sub(link, text)


def parse_button_template(template: str, link: str) -> ParsedButtons:
    """
    Parse a button-template preset into an `InlineKeyboardMarkup`.

    Unparsable or invalid cells are skipped and reported back as
    warnings instead of raising, so a single bad line never blocks
    the rest of the post from going out.
    """
    rows: list[list[InlineKeyboardButton]] = []
    warnings: list[str] = []

    for line_no, raw_line in enumerate(template.splitlines(), start=1):
        line = raw_line.strip()

        if not line:
            continue

        buttons: list[InlineKeyboardButton] = []

        for raw_cell in line.split("|"):
            cell = raw_cell.strip()

            if not cell:
                continue

            parts = [part.strip() for part in cell.split(" - ")]

            if len(parts) >= 3 and _STYLE_CELL.match(parts[-1]):  # noqa: PLR2004
                parts = parts[:-1]

            if len(parts) < 2:  # noqa: PLR2004
                warnings.append(
                    f"Line {line_no}: couldn't parse `{cell}` "
                    "(expected `Label - URL`).",
                )
                continue

            label, url = parts[0], " - ".join(parts[1:])

            if not label:
                warnings.append(
                    f"Line {line_no}: empty button label — skipped.",
                )
                continue

            url = apply_link_placeholder(url, link)

            if not url.lower().startswith(_VALID_SCHEMES):
                warnings.append(
                    f"Line {line_no}: `{label}` has no valid URL "
                    f"(`{url}`) — skipped.",
                )
                continue

            buttons.append(InlineKeyboardButton(label, url=url))

        if buttons:
            rows.append(buttons)

    markup = InlineKeyboardMarkup(rows) if rows else None

    return ParsedButtons(markup=markup, warnings=warnings)
