"""Widgets for scheduler-tui.

Everything here is mouse-first: the table reports clicks and double-clicks, the
chips are buttons, the context menu opens on right-click, and the splitter is
draggable. Keyboard equivalents live in the app's BINDINGS — no action is
available by only one of the two routes.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.events import Click, MouseDown, MouseMove, MouseUp
from textual.message import Message
from textual.widgets import Button, DataTable, Static

from ..core.model import STATUS_ORDER, Job
from .theme import palette

BAR_WIDTH = 16
LIBRARY_WIDTH = 28

#: Leading-edge characters for sub-cell progress, 1/8th of a cell apiece.
_EIGHTHS = " ▏▎▍▌▋▊▉"

#: Which palette to render with. Set by the app when the theme changes; module
#: level because the render helpers are plain functions called per cell.
_MODE_DARK = True


def set_dark(dark: bool) -> None:
    global _MODE_DARK
    _MODE_DARK = bool(dark)


def _pal():
    return palette(_MODE_DARK)


def status_style(status: str):
    """(colour, glyph) for a status, in the current theme mode."""
    return _pal().status_style(status)


def status_glyph(status: str) -> str:
    return _pal().status_style(status)[1]


def elide(text: str, width: int) -> str:
    """Truncate with a visible marker. DataTable would clip silently, which reads
    as a real (wrong) library name rather than a shortened one."""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def status_text(status: str, held: bool = False) -> Text:
    """The status cell.

    `held` is shown as a trailing marker rather than by replacing the status,
    because a job can be both held and cancelled/failed and you need to see both:
    the verdict says what happened, the marker says it will not be retried.
    """
    pal = _pal()
    colour, glyph = pal.status_style(status)
    out = Text(f"{glyph} {status}", style=colour)
    if held and status != "held":
        out.append("  ‖", style=pal.status_style("held")[0])
    return out


def progress_bar(done: int, total: int, status: str, width: int = BAR_WIDTH) -> Text:
    """A meter with sub-cell resolution.

    At 13,639 chunks across 16 cells, one cell is ~850 chunks — so a whole wave of
    1,000 could complete without the bar visibly moving. The partial-block leading
    edge gives eight times the resolution, which on a job measured in days is real
    information rather than decoration: you can see it advance.
    """
    pal = _pal()
    colour, _ = pal.status_style(status)
    if total <= 0:
        # Nothing to measure yet: a hairline placeholder, not an empty bar, so the
        # column still aligns without implying "0% of a known total".
        return Text("─" * width + "      ", style=pal.track)

    fraction = max(0.0, min(1.0, done / total))
    pct = int(fraction * 100)
    # Never show a full bar for a partial job, nor an empty one for a started job.
    if 0 < fraction < 1:
        pct = max(1, min(99, pct))

    exact = fraction * width
    full = int(exact)
    remainder = int((exact - full) * 8)

    bar = Text()
    bar.append("█" * full, style=colour)
    if full < width and remainder:
        bar.append(_EIGHTHS[remainder], style=colour)
        full += 1
    bar.append("░" * max(0, width - full), style=pal.track)
    bar.append(f" {pct:>3}%", style=colour if fraction else pal.dim)
    return bar


def counts_text(done: int, total: int) -> Text:
    """done/total, right-aligned so digits line up down the column."""
    pal = _pal()
    if total <= 0:
        return Text(f"{done:>6} / {'?':<6}", style=pal.dim)
    out = Text()
    out.append(f"{done:>6}", style=pal.text if done else pal.dim)
    out.append(" / ", style=pal.track)
    out.append(f"{total:<6}", style=pal.dim)
    return out


class QueueTable(DataTable):
    """The queue, one row per job.

    Emits :class:`QueueTable.OpenLog` on double-click and
    :class:`QueueTable.ContextRequested` on right-click.
    """

    # Uppercase headers: in a table with no rules, case change is what separates
    # the header band from the data without spending another colour on it.
    COLUMNS = ("#", "MODEL", "MODE", "LIBRARY", "STATUS", "CHUNKS DONE", "PROGRESS")
    #: Shown only while some job carries a `cpus=N` override — see _columns_for().
    #: A permanent column would spend width on a field that is usually blank, and
    #: this table has already been bitten once by a clipped PROGRESS bar.
    CPUS_COLUMN = "CPUS"
    CPUS_WIDTH = 5

    class OpenLog(Message):
        def __init__(self, key: str) -> None:
            self.key = key
            super().__init__()

    class ContextRequested(Message):
        def __init__(self, key: str, x: int, y: int) -> None:
            self.key = key
            self.x = x
            self.y = y
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cursor_type = "row"
        # No zebra stripes: they add a second, meaningless rhythm on top of the
        # status colours, and the row cursor already shows where you are.
        self.zebra_stripes = False
        self._models: List[str] = []
        self._keys: List[str] = []
        self._jobs: List[Job] = []
        self._bar_width = BAR_WIDTH
        self._library_width = LIBRARY_WIDTH
        self._show_cpus = False

    #: Minimum width for each column, in order. LIBRARY absorbs slack because it is
    #: the only column whose content is genuinely variable-length.
    #: STATUS must fit the longest label plus the held marker — "△ missing-files  ‖"
    #: is 18 cells, and a clipped status ("missing-fil") is exactly the kind of thing
    #: someone misreads at a glance.
    MIN_WIDTHS = (3, 17, 11, 18, 18, 15, 12)
    #: Per-cell horizontal padding DataTable adds on each side.
    CELL_PAD = 2

    def on_mount(self) -> None:
        self._build_columns(self.size.width or 120)

    def on_resize(self) -> None:
        # Rebuild rather than overflow: a clipped progress bar is worse than a
        # narrow one, and the bar is the column people actually read.
        self._build_columns(self.size.width or 120)
        if self._jobs:
            self.render_jobs(self._jobs)

    def _columns_for(self, show_cpus: bool):
        """Labels + minimum widths for the current shape, CPUS folded in or not.

        Returned together, and the slack maths below indexes them BY NAME, because
        the optional column shifts every position after MODE.
        """
        labels = list(self.COLUMNS)
        widths = list(self.MIN_WIDTHS)
        if show_cpus:
            at = labels.index("MODE") + 1
            labels.insert(at, self.CPUS_COLUMN)
            widths.insert(at, self.CPUS_WIDTH)
        return labels, widths

    def _build_columns(self, available: int) -> None:
        labels, widths = self._columns_for(self._show_cpus)
        library_at = labels.index("LIBRARY")
        progress_at = labels.index("PROGRESS")
        fixed = sum(widths) + self.CELL_PAD * len(widths)
        slack = available - fixed - 2  # -2 for the vertical scrollbar gutter
        if slack > 0:
            # Give the library column up to its comfortable width first, then hand
            # anything still spare to the progress bar.
            grow_library = min(slack, LIBRARY_WIDTH - widths[library_at])
            widths[library_at] += max(0, grow_library)
            slack -= max(0, grow_library)
            widths[progress_at] += min(slack, BAR_WIDTH + 6 - widths[progress_at])
        self._bar_width = max(6, widths[progress_at] - 6)  # leave room for " 100%"
        self.clear(columns=True)
        for label, width in zip(labels, widths):
            self.add_column(label, width=width, key=label)
        self._library_width = widths[library_at] - 1

    # -- data ------------------------------------------------------------
    def render_jobs(self, jobs: List[Job]) -> None:
        """Replace the table's contents, keeping the cursor on the same JOB.

        Rows are keyed by `model|mode|library`, not by model id: the same model
        against two libraries is a legitimate queue (eos21dr_v4 on Liquid_Stock and
        on Molport), and keying by model made DataTable raise DuplicateKey and the
        app crash outright.

        Anchoring the cursor on that key (not the row index) means a refresh that
        lands just as the queue is reordered does not silently move your selection
        onto a different job — which matters a lot when the next keystroke is
        `cancel`.
        """
        pal = _pal()
        previous = self.selected_key
        self._jobs = jobs
        # The CPUS column appears and disappears with the data, so the column set has
        # to be rebuilt when that changes — add_row would otherwise supply a cell
        # count the table has no column for. Read the selection FIRST: rebuilding
        # clears the rows the cursor is anchored to.
        show_cpus = any(job.cpus for job in jobs)
        if show_cpus != self._show_cpus:
            self._show_cpus = show_cpus
            self._build_columns(self.size.width or 120)
        self.clear()
        self._models = []
        self._keys = []
        for job in jobs:
            self._models.append(job.model)
            self._keys.append(job.key)
            library = job.library or "<no default>"
            if job.library_is_default and job.library:
                library = f"{job.library} *"
            # Hierarchy inside the row: the model id is the identity and gets the
            # brightest text; mode is the least useful column and recedes furthest.
            model_style = pal.bright if job.is_running else pal.text
            if job.is_running:
                model_style = f"bold {model_style}"
            cells = [
                Text(f"{job.pos:>2}", style=pal.dim),
                Text(elide(job.model, 20), style=model_style),
                Text(job.mode or "?", style=pal.dim),
            ]
            if self._show_cpus:
                # An override is a deliberate act, so it reads at normal weight while
                # the inherited default recedes — same hierarchy as the other columns.
                cells.append(
                    Text(job.cpus, style=pal.text)
                    if job.cpus
                    else Text("-", style=pal.dim)
                )
            cells += [
                Text(elide(library, self._library_width), style=pal.text),
                status_text(job.status, job.hold),
                counts_text(job.done, job.total),
                progress_bar(job.done, job.total, job.status, self._bar_width),
            ]
            self.add_row(*cells, key=job.key)
        if previous and previous in self._keys:
            self.move_cursor(row=self._keys.index(previous))

    @property
    def _cursor_index(self) -> Optional[int]:
        if not self._models:
            return None
        row = self.cursor_row
        if row is None or row < 0 or row >= len(self._models):
            return None
        return row

    @property
    def selected_model(self) -> Optional[str]:
        i = self._cursor_index
        return None if i is None else self._models[i]

    @property
    def selected_key(self) -> Optional[str]:
        """`model|mode|library` — the row's real identity."""
        i = self._cursor_index
        return None if i is None else self._keys[i]

    def key_at_screen_y(self, y: int) -> Optional[str]:
        """Which job key is under this absolute screen row (for right-click)."""
        offset = y - self.region.y - 1  # -1 for the header row
        index = offset + self.scroll_offset.y
        if 0 <= index < len(self._keys):
            return self._keys[index]
        return None

    # -- mouse -----------------------------------------------------------
    def on_click(self, event: Click) -> None:
        # Right-click: Textual reports button 3 here. Open the verb menu for the
        # row under the pointer, selecting it first so keyboard and mouse agree.
        if event.button == 3:
            key = self.key_at_screen_y(event.screen_y)
            if key:
                if key in self._keys:
                    self.move_cursor(row=self._keys.index(key))
                self.post_message(
                    self.ContextRequested(key, event.screen_x, event.screen_y)
                )
                event.stop()
            return
        if event.chain == 2:  # double-click
            key = self.selected_key
            if key:
                self.post_message(self.OpenLog(key))
                event.stop()


class StatChips(Horizontal):
    """Clickable status counters that double as filters.

    Only statuses actually present are shown: a bar of ten mostly-zero chips is
    noise, and a chip for a status with no jobs would filter to an empty table.
    """

    class Toggled(Message):
        def __init__(self, status: Optional[str]) -> None:
            self.status = status
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.active: Optional[str] = None
        self._counts: Dict[str, int] = {}

    def compose(self):
        # Note the " 0": `width: auto` is measured from the initial label, so a chip
        # that starts without a digit renders its count clipped off.
        yield Button("all 0", id="chip-all", classes="chip -active")
        for status in STATUS_ORDER:
            yield Button(
                f"{status_glyph(status)} {status} 0",
                id=f"chip-{status.replace('-', '_')}",
                classes="chip -hidden",
            )

    def update_counts(self, counts: Dict[str, int]) -> None:
        self._counts = counts
        total = sum(counts.values())
        all_btn = self.query_one("#chip-all", Button)
        all_btn.label = f"all {total}"
        for status in STATUS_ORDER:
            btn = self.query_one(f"#chip-{status.replace('-', '_')}", Button)
            count = counts.get(status, 0)
            btn.label = f"{status_glyph(status)} {status} {count}"
            # Keep a chip visible while it is the active filter, even if its count
            # drops to zero — it would otherwise vanish under the user's cursor.
            btn.set_class(count == 0 and self.active != status, "-hidden")
        # Labels just changed length (9 -> 10 jobs adds a digit); `width: auto` only
        # re-measures on a layout pass, so ask for one or the count gets clipped.
        self.refresh(layout=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id or ""
        if button_id == "chip-all":
            new_active = None
        else:
            status = button_id[len("chip-") :].replace("_", "-")
            new_active = None if self.active == status else status
        self.active = new_active
        for btn in self.query(Button):
            is_active = (
                btn.id == "chip-all" and new_active is None
            ) or btn.id == f"chip-{(new_active or '').replace('-', '_')}"
            btn.set_class(is_active, "-active")
        self.post_message(self.Toggled(new_active))


class Splitter(Static):
    """A one-row grip you can drag to resize the log pane."""

    class Dragged(Message):
        def __init__(self, delta: int) -> None:
            self.delta = delta
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__("─" * 200, **kwargs)
        self._dragging = False
        self._last_y = 0

    def on_mouse_down(self, event: MouseDown) -> None:
        self._dragging = True
        self._last_y = event.screen_y
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: MouseMove) -> None:
        if not self._dragging:
            return
        delta = event.screen_y - self._last_y
        if delta:
            self._last_y = event.screen_y
            self.post_message(self.Dragged(delta))
        event.stop()

    def on_mouse_up(self, event: MouseUp) -> None:
        if self._dragging:
            self._dragging = False
            self.release_mouse()
            event.stop()


class ContextMenu(Vertical):
    """Right-click verb menu. Same verbs as the toolbar and the keymap."""

    class Chosen(Message):
        def __init__(self, verb: str, model: str) -> None:
            self.verb = verb
            self.model = model
            super().__init__()

    ITEMS = [
        ("top", "Run next (move to top)", ""),
        ("up", "Move up", ""),
        ("down", "Move down", ""),
        ("hold", "Hold / unhold", ""),
        ("retry", "Retry (clear verdict)", ""),
        ("log", "Show log", ""),
        ("cancel", "Cancel", "-danger"),
        ("rm", "Remove from queue", "-danger"),
    ]

    def __init__(self, model: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.model = model

    def compose(self):
        for verb, label, extra in self.ITEMS:
            yield Button(label, id=f"ctx-{verb}", classes=extra)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        verb = (event.button.id or "")[len("ctx-") :]
        self.post_message(self.Chosen(verb, self.model))
