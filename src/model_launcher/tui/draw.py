"""Character-grid rendering for the dashboard.

Everything on screen is drawn here as Rich ``Text``, cell by cell, following the
design handoff (``design_handoff_model_launcher_tui/gen-tui.js``). That generator
places strings at exact column offsets on a 120-column grid; :class:`Canvas` is
its Python twin, so a coordinate in the spec can be copied across unchanged.

These are pure functions of (data, width, tokens). No Textual here: widgets call
them from ``render`` and hand the result to the screen, which keeps the layout
testable without an app running.

At 120 columns the layout is exactly the spec's. Wider terminals stretch LIBRARY
and the progress bars (see :func:`columns`); narrower ones shrink them first.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from rich.style import Style
from rich.text import Text

from ..branding import BLUE, MINT, WHITE, YELLOW
from ..core.model import STATUS_ORDER, Job, Snapshot
from .theme import Tokens, mix

#: Leading-edge characters for sub-cell progress, 1/8th of a cell apiece.
EIGHTHS = " ▏▎▍▌▋▊▉"

#: Rounded for cards, modals and the log drawer; square for the completion popup.
BORDERS = {"round": "╭╮╰╯─│", "square": "┌┐└┘─│"}

#: The order statuses appear in the summary line. The spec's seven first, then any
#: others the scheduler reports, so an unusual status is never silently uncounted.
_SPEC_ORDER = [
    "running",
    "pending",
    "held",
    "missing-files",
    "failed",
    "cancelled",
    "done",
]
SUMMARY_ORDER = _SPEC_ORDER + [s for s in STATUS_ORDER if s not in _SPEC_ORDER]

#: Footer key groups on the dashboard: (key, description) pairs, groups split by │.
DASHBOARD_KEYS = [
    [("a", "add"), ("x", "remove")],
    [("t", "run next"), ("K/J", "move")],
    [("h", "hold"), ("r", "retry"), ("c", "cancel")],
    [("p", "pause"), ("l", "log"), ("^o", "hosts")],
    [("D", "theme"), ("q", "quit")],
]

_STATE_GLYPH = {
    "RUNNING": "●",
    "RUNNING (old driver)": "●",
    "PAUSED": "‖",
    "STOPPING": "◐",
    "STOPPED": "○",
}

_LOG_STAMP = re.compile(r"^\[(?:\d{4}-\d\d-\d\d[T ])?(\d\d:\d\d:\d\d)Z?\]\s?")


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------
def elide(text: str, width: int) -> str:
    """Truncate with a visible ``…``; a silent clip reads as a real (wrong) name."""
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


def num(value: int) -> str:
    """Thousands separators, as in ``13,639``."""
    return f"{value:,}"


def pct(done: int, total: int) -> str:
    """Whole percent, floored, never 0% or 100% for a job that is partway.

    A job one chunk short of finishing must not read 100%, and one that has
    started must not read 0%: both are exactly the misreadings that matter.
    """
    if total <= 0:
        return "—"
    value = math.floor(done * 100 / total)
    if 0 < done < total:
        value = max(1, min(99, value))
    return f"{value}%"


def pct_fine(done: int, total: int) -> str:
    """One-decimal percent for the NOW RUNNING card, same clamping as :func:`pct`."""
    if total <= 0:
        return "—"
    value = math.floor(done * 1000 / total) / 10
    if 0 < done < total:
        value = max(0.1, min(99.9, value))
    return f"{value:.1f}%"


def split_log_line(line: str) -> tuple[str, str]:
    """``[2026-09-25T14:02:11Z] msg`` → ``("14:02:11", "msg")``; ``("", line)`` otherwise."""
    match = _LOG_STAMP.match(line)
    if not match:
        return "", line
    return match.group(1), line[match.end() :]


def clock(iso: str) -> str:
    """``2026-09-25T14:32:07Z`` → ``14:32:07``; anything else passes through."""
    match = re.search(r"(\d\d:\d\d:\d\d)", iso or "")
    return match.group(1) if match else (iso or "")


# ---------------------------------------------------------------------------
# the canvas
# ---------------------------------------------------------------------------
@lru_cache(maxsize=4096)
def _style(fg: str, bg: str, bold: bool) -> Style:
    return Style(color=fg, bgcolor=bg, bold=bold)


class Canvas:
    """A grid of cells, each with a character, colours and a bold flag.

    Mirrors the generator's ``G`` class: :meth:`put` writes a string from a
    column onward and returns the column after it, anything off the grid is
    dropped, and :meth:`lines` turns the grid into one ``Text`` per row.
    """

    def __init__(self, width: int, height: int, tokens: Tokens, bg: str = "") -> None:
        self.w = max(0, width)
        self.h = max(0, height)
        self.t = tokens
        base = bg or tokens.bg
        self.cells = [
            [[" ", tokens.fg, base, False] for _ in range(self.w)]
            for _ in range(self.h)
        ]

    def put(
        self,
        x: int,
        y: int,
        text: str,
        fg: str = "",
        bg: str = "",
        bold: bool = False,
    ) -> int:
        """Write ``text`` at (x, y); return the column just past it."""
        text = str(text)
        if not 0 <= y < self.h:
            return x + len(text)
        row = self.cells[y]
        for ch in text:
            if 0 <= x < self.w:
                cell = row[x]
                cell[0] = ch
                if fg:
                    cell[1] = fg
                if bg:
                    cell[2] = bg
                cell[3] = bold
            x += 1
        return x

    def rput(
        self, right: int, y: int, text: str, fg: str = "", bold: bool = False
    ) -> int:
        """Write ``text`` so that it ends just before column ``right``."""
        return self.put(right - len(text), y, text, fg, bold=bold)

    def fill(self, x: int, y: int, w: int, h: int, bg: str) -> None:
        """Blank a rectangle to background ``bg``."""
        for j in range(max(0, y), min(self.h, y + h)):
            for i in range(max(0, x), min(self.w, x + w)):
                cell = self.cells[j][i]
                cell[0], cell[2], cell[3] = " ", bg, False

    def box(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        fg: str,
        bg: str = "",
        kind: str = "round",
        title: str = "",
        title_fg: str = "",
    ) -> None:
        """Draw a bordered box, optionally filled and titled on its top edge."""
        tl, tr, bl, br, hz, vt = BORDERS[kind]
        if bg:
            self.fill(x, y, w, h, bg)
        self.put(x, y, tl + hz * (w - 2) + tr, fg, bg)
        for j in range(1, h - 1):
            self.put(x, y + j, vt, fg, bg)
            self.put(x + w - 1, y + j, vt, fg, bg)
        self.put(x, y + h - 1, bl + hz * (w - 2) + br, fg, bg)
        if title:
            self.put(x + 2, y, title, title_fg or fg, bg, bold=True)

    def lines(self) -> list[Text]:
        """One ``Text`` per row, with runs of identical style merged."""
        out = []
        for row in self.cells:
            text = Text(no_wrap=True, overflow="crop", end="")
            run, key = "", None
            for ch, fg, bg, bold in row:
                # A space's foreground is invisible, so it may join any run.
                this = (fg, bg, bold)
                if key is not None and (
                    this == key or (ch == " " and bg == key[1] and bold == key[2])
                ):
                    run += ch
                    continue
                if run:
                    text.append(run, _style(*key))
                run, key = ch, this
            if run:
                text.append(run, _style(*key))
            out.append(text)
        return out


def bar(
    cv: Canvas,
    x: int,
    y: int,
    width: int,
    done: int,
    total: int,
    colour: str,
    full: str = "█",
    track: str = "░",
    eighths: bool = True,
    none: str = "─",
) -> int:
    """A meter from column ``x``; returns the column after it.

    With ``eighths`` the leading edge uses partial blocks, so a 13,639-chunk job
    on a 104-cell bar visibly advances every ~16 chunks rather than every ~130.
    """
    t = cv.t
    if total <= 0:
        cv.put(x, y, none * width, t.track)
        return x + width
    exact = min(1.0, max(0.0, done / total)) * width
    filled = math.floor(exact)
    rem = math.floor((exact - filled) * 8)
    cv.put(x, y, full * filled, colour)
    n = filled
    if n < width and rem and eighths:
        cv.put(x + n, y, EIGHTHS[rem], colour)
        n += 1
    cv.put(x + n, y, track * (width - n), t.track)
    return x + width


# ---------------------------------------------------------------------------
# shared chrome
# ---------------------------------------------------------------------------
def band(
    width: int,
    t: Tokens,
    host: str = "",
    snap: Snapshot | None = None,
    right: str = "",
) -> Text:
    """Row 0: the plum brand band. ``right`` replaces the scheduler summary."""
    cv = Canvas(width, 1, t, bg=t.band)
    cv.put(1, 0, "ersilia", MINT, bold=True)
    cv.put(9, 0, "model-launcher", t.band_muted)
    if right:
        cv.rput(width - 1, 0, right, t.band_muted)
    elif snap is not None:
        state = snap.driver_state
        glyph = _STATE_GLYPH.get(state, "●" if snap.driver_alive else "○")
        colour = BLUE if snap.driver_alive and not snap.paused else YELLOW
        if not snap.driver_alive:
            colour = t.band_muted
        parts: list[tuple[str, str, bool]] = [(host, WHITE, True)]
        parts.append((f"{glyph} {state}", colour, True))
        if snap.runtime.get("dry_run") == "1" or snap.driver_info.get("dry_run") == "1":
            parts.append(("DRY-RUN", YELLOW, True))
        tail = []
        if snap.driver_pid and snap.driver_alive:
            tail.append(f"pid {snap.driver_pid}")
        stamp = clock(snap.runtime.get("now", ""))
        if stamp:
            tail.append(stamp)
        if tail:
            parts.append(("   ".join(tail), t.band_muted, False))
        parts = [p for p in parts if p[0]]
        length = sum(len(p[0]) for p in parts) + 3 * (len(parts) - 1) + 1
        x = width - length
        for i, (text, colour, bold) in enumerate(parts):
            if i:
                x = cv.put(x, 0, "   ")
            x = cv.put(x, 0, text, colour, bold=bold)
    return cv.lines()[0]


def context_line(width: int, t: Tokens, pairs: Sequence[tuple[str, str]]) -> Text:
    """Row 1: ``key value`` groups, muted key and plain value, three spaces apart."""
    cv = Canvas(width, 1, t)
    x = 1
    for key, value in pairs:
        x = cv.put(x, 0, key + " ", t.muted)
        x = cv.put(x, 0, value, t.fg)
        x = cv.put(x, 0, "   ")
    return cv.lines()[0]


#: (start column, end column, key) of each clickable key hint on one row.
Spans = list[tuple[int, int, str]]


def key_hints(
    cv: Canvas, x: int, y: int, items: Sequence[tuple[str, str]], gap: str
) -> tuple[int, Spans]:
    """Draw ``key desc`` pairs from column ``x``; return the next column and spans.

    Each span covers a key and its description, not the gap after it. A
    combined key such as ``K/J`` gets one span per key, the last one running
    on through the description, so each half can be clicked on its own.
    """
    spans: Spans = []
    for key, desc in items:
        start = x
        x = cv.put(x, y, key, cv.t.primary, bold=True)
        x = cv.put(x, y, f" {desc}{gap}", cv.t.muted)
        parts = key.split("/")
        for i, part in enumerate(parts):
            end = start + len(part) if i < len(parts) - 1 else x - len(gap)
            spans.append((start, end, part))
            start = end + 1
    return x, spans


def footer(
    width: int, t: Tokens, groups: Sequence[Sequence[tuple[str, str]]]
) -> tuple[Text, Spans]:
    """The last row (bold keys, muted descriptions, groups split by ``│``) and
    the column span of each key for click-to-run."""
    cv = Canvas(width, 1, t, bg=t.panel)
    x = 1
    spans: Spans = []
    for gi, group in enumerate(groups):
        x, group_spans = key_hints(cv, x, 0, group, "  ")
        spans += group_spans
        if gi < len(groups) - 1:
            x = cv.put(x, 0, "│  ", t.track)
    return cv.lines()[0], spans


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------
#: Row of the running card that carries its key hints.
CARD_HINT_ROW = 3


def running_card(width: int, t: Tokens, snap: Snapshot) -> tuple[list[Text], Spans]:
    """The five-row NOW RUNNING card (or an idle card when nothing runs), and
    the spans of the key hints on its :data:`CARD_HINT_ROW`."""
    cv = Canvas(width, 5, t)
    job = snap.running_job()
    if job is None:
        cv.box(1, 0, width - 2, 5, t.panel, title=" IDLE ", title_fg=t.muted)
        waiting = sum(1 for j in snap.jobs if j.status == "pending" and not j.hold)
        state = "no driver running" if not snap.driver_alive else "nothing running"
        if snap.driver_alive and snap.paused:
            state = "queue paused"
        x = cv.put(4, 1, state.capitalize(), t.fg, bold=True)
        cv.put(x + 2, 1, f"{waiting} pending", t.muted)
        bar(cv, 4, 2, width - 16, 0, 0, t.live, none="░")
        hints = (("a", "add"), ("t", "run next"), ("p", "pause"))
        _, spans = key_hints(cv, width - 40, CARD_HINT_ROW, hints, "   ")
        return cv.lines(), spans

    cv.box(
        1,
        0,
        width - 2,
        5,
        mix(t.bg, t.live, 0.45),
        title=" NOW RUNNING ",
        title_fg=t.live,
    )
    wave = job.wave or snap.driver_info.get("default_wave_size", "?")
    queue = job.queue or snap.driver_info.get("default_queue", "?")
    detail = f"{job.mode}  ·  {job.library or '?'}  ·  wave {wave}  ·  {queue}"
    position = f"position {job.pos} / {len(snap.jobs)}"
    x = cv.put(4, 1, job.model, t.bright, bold=True)
    cv.put(x + 2, 1, elide(detail, width - 19 - (x + 2) - 2), t.muted)
    cv.put(width - 17, 1, position, t.muted)

    bw = width - 16
    bar(cv, 4, 2, bw, job.done, job.total, t.live)
    cv.put(4 + bw + 2, 2, pct_fine(job.done, job.total), t.live, bold=True)

    if job.total > 0:
        x = cv.put(4, 3, num(job.done), t.fg, bold=True)
        x = cv.put(x, 3, f" / {num(job.total)} chunks", t.muted)
        x = cv.put(x, 3, "   ·   ", t.track)
        cv.put(x, 3, f"{num(max(0, job.total - job.done))} remaining", t.muted)
    else:
        cv.put(4, 3, "counting chunks…", t.muted)
    hints = (("l", "log"), ("c", "cancel"), ("s", "stop after"))
    _, spans = key_hints(cv, width - 40, CARD_HINT_ROW, hints, "   ")
    return cv.lines(), spans


def summary_line(
    width: int, t: Tokens, counts: dict[str, int], active: str | None
) -> tuple[Text, list[tuple[int, int, str | None]]]:
    """The status summary, and the column span of each entry for click-to-filter.

    ``active`` is the status currently filtered on (None = all); it is the one
    drawn as the inverted pill.
    """
    cv = Canvas(width, 1, t)
    spans: list[tuple[int, int, str | None]] = []
    label = f" all {sum(counts.values())} "
    if active is None:
        end = cv.put(2, 0, label, t.bg, t.primary, bold=True)
    else:
        end = cv.put(2, 0, label, t.muted)
    spans.append((2, end, None))
    x = end + 2
    for status in SUMMARY_ORDER:
        count = counts.get(status, 0)
        if not count and status != active:
            continue
        colour, glyph = t.status_style(status)
        start = x
        if status == active:
            x = cv.put(x, 0, f" {glyph} {status} {count} ", t.bg, t.primary, bold=True)
        else:
            x = cv.put(x, 0, glyph, colour)
            x = cv.put(x, 0, f" {status} ", t.muted)
            x = cv.put(x, 0, str(count), t.fg, bold=True)
        spans.append((start, x, status))
        x += 3
    return cv.lines()[0], spans


@dataclass(frozen=True)
class Columns:
    """Column offsets of the queue table for one terminal width.

    At 120 columns these are the spec's: # @2, MODEL @5, MODE @25, LIBRARY @38,
    STATUS @65, CHUNKS @82, PROGRESS @100 with the percent at @114.
    """

    width: int
    library_width: int
    bar_width: int

    pos: int = 2
    model: int = 5
    mode: int = 25
    library: int = 38

    @property
    def status(self) -> int:
        return self.library + self.library_width + 2

    @property
    def chunks(self) -> int:
        return self.status + 17

    @property
    def progress(self) -> int:
        return self.chunks + 18

    @property
    def percent(self) -> int:
        return self.progress + self.bar_width + 1


def columns(width: int) -> Columns:
    """Lay the table out for ``width`` columns.

    Slack goes to LIBRARY first (up to 10 more cells: the longest real names are
    ~32 characters), then to the bar. A narrow terminal gives up LIBRARY width
    before bar width, because the bar is the column people actually read.
    """
    library, bars = 25, 13
    extra = width - 120
    if extra > 0:
        grow = min(extra // 2, 10)
        library += grow
        bars += min(extra - grow, 40)
    elif extra < 0:
        shrink = min(-extra, library - 12)
        library -= shrink
        bars = max(6, bars - (-extra - shrink))
    return Columns(width=width, library_width=library, bar_width=bars)


def table_header(cols: Columns, t: Tokens) -> list[Text]:
    """The column headings and the rule under them."""
    cv = Canvas(cols.width, 2, t)
    for x, label in (
        (cols.pos, "#"),
        (cols.model, "MODEL"),
        (cols.mode, "MODE"),
        (cols.library, "LIBRARY"),
        (cols.status, "STATUS"),
        (cols.chunks, "CHUNKS"),
        (cols.progress, "PROGRESS"),
    ):
        cv.put(x, 0, label, t.muted)
    cv.put(0, 1, "─" * cols.width, t.panel)
    return cv.lines()


def job_row(job: Job, selected: bool, cols: Columns, t: Tokens) -> Text:
    """One queue row."""
    cv = Canvas(cols.width, 1, t)
    colour, glyph = t.status_style(job.status)
    settled = job.status == "done"
    if selected:
        cv.fill(0, 0, cols.width, 1, t.cursor)
        cv.put(0, 0, "▌", t.primary)

    cv.put(cols.pos, 0, f"{job.pos:>2}", t.muted)
    model_fg = t.muted if settled else (t.bright if selected else t.fg)
    cv.put(
        cols.model, 0, elide(job.model, 19), model_fg, bold=selected or job.is_running
    )

    room = cols.library - cols.mode - 1
    suffix = f" {job.cpus}c" if job.cpus else ""
    x = cv.put(cols.mode, 0, elide(job.mode or "?", room - len(suffix)), t.muted)
    if suffix:
        cv.put(x + 1, 0, suffix.strip(), t.fg)

    library = job.library or "<no default>"
    x = cv.put(
        cols.library,
        0,
        elide(library, cols.library_width),
        t.muted if settled else t.fg,
    )
    if job.library_is_default and job.library:
        cv.put(x + 1, 0, "*", t.primary)

    x = cv.put(cols.status, 0, f"{glyph} {job.status}", colour)
    if job.hold and job.status != "held":
        cv.put(x + 1, 0, "‖", t.status_style("held")[0])

    if job.total > 0:
        x = cv.put(cols.chunks, 0, f"{num(job.done):>6}", t.fg if job.done else t.muted)
        cv.put(x, 0, f" / {num(job.total)}", t.muted)
    else:
        cv.put(cols.chunks, 0, "     — / ?", t.muted)

    bar(
        cv,
        cols.progress,
        0,
        cols.bar_width,
        job.done,
        job.total,
        colour,
        full="━",
        track="─",
        eighths=False,
        none="┄",
    )
    label = pct(job.done, job.total) if job.total > 0 else ""
    cv.put(cols.percent, 0, f"{label:>4}", colour if job.done else t.muted)
    return cv.lines()[0]


def log_drawer(
    width: int,
    height: int,
    t: Tokens,
    model: str,
    follow: bool,
    lines: Sequence[str],
    placeholder: str = "",
) -> tuple[list[Text], Spans]:
    """The log drawer: a rounded box with timestamp and message columns, and
    the span of the follow toggle on its top row.

    ``lines`` are the ones to show, already scrolled; at most ``height - 4`` fit.
    """
    cv = Canvas(width, height, t)
    cv.box(
        1,
        0,
        width - 2,
        height,
        t.panel,
        title=f" log · {model or '—'} ",
        title_fg=t.primary,
    )
    start = x = width - 24
    if follow:
        x = cv.put(x, 0, " ● ", t.live)
        cv.put(x, 0, "following · f ", t.muted)
    else:
        x = cv.put(x, 0, " ○ ", t.muted)
        cv.put(x, 0, "frozen · f    ", t.muted)
    spans: Spans = [(start, start + len(" ● following · f "), "f")]

    stamp_fg = mix(t.muted, t.bg, 0.35)
    room = height - 4
    shown = list(lines)[-room:] if room > 0 else []
    if not shown and placeholder:
        cv.put(4, 2, elide(placeholder, width - 8), t.muted)
    for i, raw in enumerate(shown):
        y = 2 + i
        stamp, message = split_log_line(raw.expandtabs(4).rstrip())
        fg = t.fg if message.startswith("s3") else t.muted
        if stamp:
            cv.put(4, y, stamp, stamp_fg)
            cv.put(14, y, elide(message, width - 17), fg)
        else:
            cv.put(4, y, elide(message, width - 7), fg)
    cv.put(
        4,
        height - 2,
        "drag ─ or  +/-  to resize   ·   esc close",
        mix(t.muted, t.bg, 0.3),
    )
    return cv.lines(), spans
