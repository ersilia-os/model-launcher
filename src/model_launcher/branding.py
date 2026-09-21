"""The Ersilia palette, shared by the dashboard and the command line.

These nine values are the official brand colours and the single source of truth
for both surfaces — ``tui/theme.py`` builds its Textual themes from them and the
CLI builds its Rich theme from them, so the two can never drift.

One constraint shapes how they are used on the command line. The secondary
palette is uniformly *light* (every channel 130+), and unlike the dashboard — a
full-screen app that paints its own plum background — a CLI writes onto a
terminal whose background we do not control. Mint on white is unreadable. So
colour is used for **accents and status only**; anything the reader must be able
to read stays the terminal's own foreground colour, and emphasis comes from
bold/dim, which works on any background.
"""

from __future__ import annotations

from rich.console import Console
from rich.theme import Theme

# --- the official Ersilia palette -------------------------------------------
# Primary
PLUM = "#50285A"
MINT = "#BEE6B4"
WHITE = "#FFFFFF"
# Secondary
GRAY = "#D2D2D0"
YELLOW = "#FAD782"
BLUE = "#8CC8FA"
PINK = "#DCA0DC"
ORANGE = "#FAA08C"
PURPLE = "#AA96FA"

#: Semantic names, so call sites say what they mean rather than naming a colour.
#: The meanings match the dashboard's: mint is settled, blue is live, warm hues
#: mean a human is needed.
CLI_THEME = Theme(
    {
        "brand": f"bold {MINT}",
        "heading": f"bold {MINT}",
        "rule": PLUM,
        "muted": "dim",
        "key": PURPLE,
        "ok": MINT,
        "live": f"bold {BLUE}",
        "warn": YELLOW,
        "bad": ORANGE,
        "alert": PINK,
        "gone": "dim",
    }
)


def console(**kwargs) -> Console:
    """Build a Rich console using the Ersilia theme.

    Rich drops colour automatically when output is not a terminal, so the same
    call is safe in a pipe, in CI and in the tests.
    """
    return Console(theme=CLI_THEME, **kwargs)


def style_help() -> None:
    """Configure ``rich_click`` to render ``--help`` in the Ersilia palette.

    Click's own help formatter has no styling seam worth extending — getting
    colour, wrapping and grouping right by hand would mean reimplementing a
    good chunk of it. ``rich_click`` already does that well and exposes every
    piece as a module-level style string, so this just points those at the same
    brand constants the dashboard and the rest of the CLI use.

    Call once, before the CLI runs (``create_cli`` does this at import time).
    """
    import rich_click.rich_click as rc

    rc.USE_RICH_MARKUP = True
    rc.STYLE_HEADER_TEXT = f"bold {MINT}"
    rc.STYLE_USAGE = f"bold {PURPLE}"
    rc.STYLE_USAGE_COMMAND = f"bold {MINT}"
    rc.STYLE_HELPTEXT_FIRST_LINE = "bold"
    rc.STYLE_HELPTEXT = ""
    rc.STYLE_OPTION = PURPLE
    rc.STYLE_ARGUMENT = PURPLE
    rc.STYLE_SWITCH = BLUE
    rc.STYLE_METAVAR = "dim"
    rc.STYLE_OPTION_DEFAULT = "dim"
    rc.STYLE_OPTION_HELP = ""
    rc.STYLE_COMMAND = f"bold {MINT}"
    rc.STYLE_COMMAND_HELP = ""
    rc.STYLE_OPTIONS_PANEL_BORDER = PLUM
    rc.STYLE_COMMANDS_PANEL_BORDER = PLUM
    rc.STYLE_ERRORS_PANEL_BORDER = ORANGE
    rc.STYLE_ABORTED = ORANGE
