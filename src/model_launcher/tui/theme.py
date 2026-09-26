"""Palette and themes — built from the official Ersilia colours.

Exactly two themes are registered: `ersilia-dark` and `ersilia-light`. `D` toggles
between them and nothing else; Textual's built-ins are not offered.

Design principle, unchanged, and the reason the colours are arranged this way:

    The running row is the only luminous thing on screen, and warmth always
    means attention.

A wave-scheduler queue is mostly *waiting* — a 1.4B-molecule library takes days,
so at any moment one row is working and the rest are idle or finished. A palette
that gives every status an equally bright hue turns that into a wall of confetti
and buries the one row you actually care about.

How the brand palette maps onto that:

  * the PRIMARY palette is the page. Plum shades make the dark ground, White is
    the text, Mint is the structural accent (headings, selection, focus). Plum
    with Mint is the pairing Ersilia is recognised by.
  * the SECONDARY palette carries the status semantics — which is what a secondary
    palette is for. Blue is the one bright cool, so the live row is where the eye
    lands. Mint means finished. Yellow / Orange / Pink mean a human is needed.

Every value below is a literal brand colour except those marked SHADE or TINT,
which are darkened or lightened versions of one. Two reasons those are
unavoidable: the secondary palette is uniformly light (every channel 130+), so on
a WHITE ground none of it reaches readable contrast; and there are nine statuses
to tell apart using six secondary colours.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Tuple

from textual.theme import Theme

# ---------------------------------------------------------------------------
# The official Ersilia palette, imported so the dashboard and the CLI cannot
# drift apart. See ``model_launcher.branding`` for the values themselves.
# ---------------------------------------------------------------------------
from ..branding import (  # noqa: E402
    BLUE,
    MINT,
    ORANGE,
    PINK,
    PLUM,
    PURPLE,
    WHITE,
    YELLOW,
)

# ---------------------------------------------------------------------------
# themes  (these also drive Textual's own widgets: footer, toasts, modals,
# Select overlays — so the whole app sits in one palette)
# ---------------------------------------------------------------------------

ERSILIA_DARK = Theme(
    name="ersilia-dark",
    dark=True,
    background="#160F1B",  # SHADE of Plum — near-black, but still plum
    surface="#1E1526",  # SHADE of Plum
    panel="#2A1D33",  # SHADE of Plum
    foreground="#F2ECF4",  # White, faintly plum-tinted so it settles on the ground
    primary=MINT,  # structure: headings, selection, focus
    secondary=BLUE,  # the live signal
    success=MINT,
    warning=YELLOW,
    error=ORANGE,
    accent=PURPLE,
    variables={
        "block-cursor-background": "#3A2846",  # SHADE of Plum: the selected row
        "block-cursor-foreground": WHITE,
        "block-cursor-text-style": "bold",
        "footer-key-foreground": MINT,
        "footer-description-foreground": "#9A8FA0",
    },
)

ERSILIA_LIGHT = Theme(
    name="ersilia-light",
    dark=False,
    background="#FAF8FB",  # White, a hair off so panels can read against it
    surface=WHITE,
    panel="#F1EBF4",  # TINT of Plum
    foreground="#2A1730",  # SHADE of Plum — body text
    primary=PLUM,  # on white, Plum itself is the accent
    secondary="#2F7FC4",  # SHADE of Blue (brand Blue is illegible on white)
    success="#4E8C42",  # SHADE of Mint
    warning="#A87A1E",  # SHADE of Yellow
    error="#B03A28",  # SHADE of Orange
    accent="#6A55C4",  # SHADE of Purple
    variables={
        "block-cursor-background": "#E4D8EA",  # TINT of Plum
        "block-cursor-foreground": "#2A1730",
        "block-cursor-text-style": "bold",
        "footer-key-foreground": PLUM,
        "footer-description-foreground": "#6B5A72",
    },
)

#: The only two themes the app registers.
THEMES = (ERSILIA_DARK, ERSILIA_LIGHT)
DARK_THEME = ERSILIA_DARK.name
LIGHT_THEME = ERSILIA_LIGHT.name

# ---------------------------------------------------------------------------
# status palette
# ---------------------------------------------------------------------------
# (colour, glyph). The glyph carries the meaning where colour cannot: a
# monochrome terminal, a colour-blind reader, or a screenshot pasted into chat.
#
# Glyphs come from the geometric/dingbat ranges that monospace terminal fonts
# reliably ship. Deliberately NO emoji: an emoji-presentation codepoint renders
# double-width in some terminals and single in others, which silently shifts
# every column after it out of line.

_DARK_STATUS: Dict[str, Tuple[str, str]] = {
    "running": (BLUE, "●"),  # the one bright cool — draws the eye
    "pending": ("#9A9A98", "○"),  # SHADE of Gray: waiting is not news
    "done": (MINT, "✓"),  # finished
    "skipped": ("#5F5A66", "·"),  # SHADE of Gray: ignored entirely
    "held": (YELLOW, "‖"),  # warm from here down = wants a human
    "missing-files": (ORANGE, "△"),
    "cancelled": (PINK, "⊘"),
    "failed": ("#E8705A", "✕"),  # SHADE of Orange: the strongest alarm
    "stale": (PURPLE, "?"),  # claims to be running, but no live driver
}

_LIGHT_STATUS: Dict[str, Tuple[str, str]] = {
    # All shades: the brand secondaries are far too light to read on white.
    "running": ("#2F7FC4", "●"),  # SHADE of Blue
    "pending": ("#77776F", "○"),  # SHADE of Gray
    "done": ("#4E8C42", "✓"),  # SHADE of Mint
    "skipped": ("#A5A5A0", "·"),  # SHADE of Gray
    "held": ("#A87A1E", "‖"),  # SHADE of Yellow
    "missing-files": ("#C4643E", "△"),  # SHADE of Orange
    "cancelled": ("#A0559E", "⊘"),  # SHADE of Pink
    "failed": ("#B03A28", "✕"),  # deeper SHADE of Orange
    "stale": ("#6A55C4", "?"),  # SHADE of Purple
}

_FALLBACK = ("#9A8FA0", "·")


# ---------------------------------------------------------------------------
# cell tokens — the design handoff's token table, used by ``draw.py``
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tokens:
    """Every colour the character-grid renderer needs, for one theme mode.

    The names are the design handoff's (``design_handoff_model_launcher_tui``),
    so a value can be checked against the spec by name.
    """

    dark: bool
    bg: str
    surface: str
    panel: str
    fg: str
    bright: str
    primary: str
    muted: str
    cursor: str
    track: str
    warn: str
    err: str
    live: str
    band: str
    band_muted: str
    status: Dict[str, Tuple[str, str]] = field(default_factory=dict)

    def status_style(self, status: str) -> Tuple[str, str]:
        """(colour, glyph) for a job status."""
        return self.status.get(status, _FALLBACK)


DARK_TOKENS = Tokens(
    dark=True,
    bg="#160F1B",
    surface="#1E1526",
    panel="#2A1D33",
    fg="#F2ECF4",
    bright=WHITE,
    primary=MINT,
    muted="#9A8FA0",
    cursor="#3A2846",
    track="#33263C",
    warn=YELLOW,
    err=ORANGE,
    live=BLUE,
    band=PLUM,
    band_muted="#CDB9D3",
    status=_DARK_STATUS,
)

LIGHT_TOKENS = Tokens(
    dark=False,
    bg="#FAF8FB",
    surface=WHITE,
    panel="#F1EBF4",
    fg="#2A1730",
    bright="#1A0E20",
    primary=PLUM,
    muted="#7C6B84",
    cursor="#E4D8EA",
    track="#E2DAE6",
    warn="#A87A1E",
    err="#B03A28",
    live="#2F7FC4",
    band=PLUM,
    band_muted="#DCCBE1",
    status=_LIGHT_STATUS,
)


def tokens(dark: bool = True) -> Tokens:
    """The cell tokens for one theme mode."""
    return DARK_TOKENS if dark else LIGHT_TOKENS


def mix(a: str, b: str, t: float) -> str:
    """Blend colour ``a`` toward ``b`` by ``t`` (0 = a, 1 = b), as ``#RRGGBB``."""
    ca = [int(a[i : i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i : i + 2], 16) for i in (1, 3, 5)]
    # floor(v + 0.5) rather than round(): JavaScript's Math.round, which the design
    # generator used, so blended colours match the mockups exactly.
    return "#" + "".join(
        f"{math.floor(x + (y - x) * t + 0.5):02X}" for x, y in zip(ca, cb)
    )
