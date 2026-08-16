"""Clipboard and paste-list helpers.

This is the bridge between the tool and the game, and it is deliberately a
one-way, manual one. There is no supported way to drive the EVE client
programmatically, and automating input into it violates the EULA. So the tool
computes numbers and hands them to your clipboard; you alt-tab and press
Ctrl+V yourself.

Two formats matter:

* **Multibuy** — EVE's Multibuy window accepts a pasted list of
  ``<item name><tab><quantity>`` lines and builds the whole basket at once.
  This is how you place a shopping list in Jita in one action.
* **A bare price** — the modify-order and create-order dialogs take a plain
  number. It must have no thousands separators or the client rejects it.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class ClipboardUnavailable(RuntimeError):
    """No system clipboard (headless session, or missing backend)."""


def multibuy_list(items: list[tuple[str, int]]) -> str:
    """Format ``[(item_name, qty)]`` for EVE's Multibuy window.

    Tab-separated because item names contain spaces and EVE's parser splits on
    the last whitespace run otherwise, which mangles names like
    'Small Shield Extender II'.
    """
    lines = []
    for name, qty in items:
        if qty <= 0:
            continue
        lines.append(f"{name}\t{qty}")
    return "\n".join(lines)


def format_price(price: float) -> str:
    """Format a price for pasting into an EVE order dialog.

    No thousands separators: the client will not parse '1,234.56'.
    """
    return f"{price:.2f}"


def copy(text: str) -> None:
    """Put text on the system clipboard.

    Raises :class:`ClipboardUnavailable` rather than failing silently — a
    price you think was copied but wasn't is worse than an error.
    """
    try:
        import pyperclip
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise ClipboardUnavailable(
            "pyperclip is not installed; run `pip install -e '.[clipboard]'`"
        ) from exc

    try:
        pyperclip.copy(text)
    except Exception as exc:
        raise ClipboardUnavailable(
            "no clipboard backend available. On Linux install xclip or xsel."
        ) from exc


def try_copy(text: str) -> bool:
    """Copy if possible; report whether it worked. Never raises."""
    try:
        copy(text)
        return True
    except ClipboardUnavailable as exc:
        log.debug("clipboard unavailable: %s", exc)
        return False
