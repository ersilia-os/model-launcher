"""Widgets for the dashboard.

Each widget is a thin shell around one ``draw.py`` function: it holds the data
for its region, repaints only when that data changes, and turns mouse events
into messages. The drawing itself — every column offset and colour — lives in
``draw.py``, so the layout can be checked without an app running.

Mouse and keyboard reach the same actions: click selects a row, double-click
opens its log, right-click opens the verb menu, clicking a status in the
summary line filters, and the log drawer's top edge drags to resize.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import (
    Click,
    MouseDown,
    MouseMove,
    MouseScrollDown,
    MouseScrollUp,
    MouseUp,
)
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button

from ..core.model import Job, Snapshot
from . import draw
from .theme import Tokens


def _tokens(widget: Widget) -> Tokens:
    return widget.app.tokens  # type: ignore[attr-defined]


def _join(lines: Sequence[Text]) -> Text:
    return Text("\n", end="").join(lines)


class Painted(Widget):
    """A region drawn by one ``draw.py`` function.

    Subclasses implement :meth:`paint`. :meth:`show` stores new data and
    repaints only if it differs from what is on screen, so a poll that changed
    nothing redraws nothing.
    """

    DEFAULT_CSS = "Painted { height: 1; }"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.data: tuple = ()

    def show(self, *data) -> None:
        if data != self.data:
            self.data = data
            self.refresh()

    def paint(self, width: int, t: Tokens) -> List[Text]:  # pragma: no cover - abstract
        raise NotImplementedError

    def render(self) -> Text:
        if not self.data:
            return Text("")
        return _join(self.paint(self.size.width or 120, _tokens(self)))


class Band(Painted):
    """Row 0: brand band. Data: (host, snapshot) or (right-hand text,)."""

    def paint(self, width: int, t: Tokens) -> List[Text]:
        if len(self.data) == 1:
            return [draw.band(width, t, right=self.data[0])]
        host, snap = self.data
        return [draw.band(width, t, host=host, snap=snap)]


class ContextLine(Painted):
    """Row 1: ``queue … lib … wave … partition …``. Data: (pairs,)."""

    def paint(self, width: int, t: Tokens) -> List[Text]:
        return [draw.context_line(width, t, self.data[0])]


class KeyFooter(Painted):
    """The key hints on the last row. Data: (groups,)."""

    def paint(self, width: int, t: Tokens) -> List[Text]:
        return [draw.footer(width, t, self.data[0])]


class Banner(Painted):
    """An inset notice under the context line: a warning, or a transport error.

    Always one row tall when empty, so the grid below it does not shift when a
    banner comes and goes on a flaky connection.
    """

    DEFAULT_CSS = "Banner { height: auto; min-height: 1; }"

    def paint(self, width: int, t: Tokens) -> List[Text]:
        message, error = self.data
        if not message:
            return [Text("")]
        colour = t.err if error else t.warn
        text = Text(no_wrap=False, end="")
        text.append(" ▌ ", Style(color=colour, bgcolor=t.surface))
        text.append(message, Style(color=colour, bgcolor=t.surface))
        return [text]


class RunningCard(Painted):
    """The NOW RUNNING card. Data: a tuple of what it shows, then the snapshot."""

    DEFAULT_CSS = "RunningCard { height: 5; }"

    def paint(self, width: int, t: Tokens) -> List[Text]:
        return draw.running_card(width, t, self.data[-1])

    def show_snapshot(self, snap: Snapshot) -> None:
        # Compare only what the card draws; the snapshot's log text and the
        # rest of the queue change on every tick and must not repaint it.
        job = snap.running_job()
        key = (
            job,
            len(snap.jobs),
            snap.driver_alive,
            snap.paused,
            sum(1 for j in snap.jobs if j.status == "pending" and not j.hold),
            snap.driver_info.get("default_wave_size"),
            snap.driver_info.get("default_queue"),
        )
        if key != self.data[:-1]:
            self.data = key + (snap,)
            self.refresh()


class StatusSummary(Painted):
    """``all N`` then one count per status; click one to filter the queue."""

    class Toggled(Message):
        def __init__(self, status: Optional[str]) -> None:
            self.status = status
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._spans: List[Tuple[int, int, Optional[str]]] = []

    def paint(self, width: int, t: Tokens) -> List[Text]:
        counts, active = self.data
        line, self._spans = draw.summary_line(width, t, counts, active)
        return [line]

    def on_click(self, event: Click) -> None:
        if not self.data:
            return
        active = self.data[1]
        for start, end, status in self._spans:
            if start <= event.x < end:
                # Clicking the active filter again clears it.
                self.post_message(self.Toggled(None if status == active else status))
                event.stop()
                return


class QueueHeader(Painted):
    """Column headings and the rule under them. Data: (Columns,)."""

    DEFAULT_CSS = "QueueHeader { height: 2; }"

    def paint(self, width: int, t: Tokens) -> List[Text]:
        return draw.table_header(self.data[0], t)


class QueueView(ScrollView, can_focus=True):
    """The queue, one line per job, with a cursor anchored on the job's key.

    Emits :class:`QueueView.OpenLog` on double-click and
    :class:`QueueView.ContextRequested` on right-click.
    """

    BINDINGS = [
        Binding("up", "cursor(-1)", "up", show=False),
        Binding("down", "cursor(1)", "down", show=False),
        Binding("pageup", "cursor(-10)", show=False),
        Binding("pagedown", "cursor(10)", show=False),
        Binding("home", "cursor(-100000)", show=False),
        Binding("end", "cursor(100000)", show=False),
    ]

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

    class Resized(Message):
        """The table's width changed, so the column layout did too."""

        def __init__(self, cols: draw.Columns) -> None:
            self.cols = cols
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._jobs: List[Job] = []
        self._keys: List[str] = []
        self._cursor = 0
        self._seeded = False
        self.cols = draw.columns(120)

    # -- data ------------------------------------------------------------
    def render_jobs(self, jobs: List[Job]) -> None:
        """Replace the rows, keeping the cursor on the same JOB.

        Anchored on ``model|mode|library`` rather than the row index, so a
        refresh landing just as the queue is reordered does not move the
        selection onto another job — which matters when the next key is
        ``cancel``. On the first load the running job is selected.
        """
        previous = self.selected_key
        changed = jobs != self._jobs
        self._jobs = list(jobs)
        self._keys = [job.key for job in jobs]
        if not self._seeded and jobs:
            self._seeded = True
            running = next((i for i, j in enumerate(jobs) if j.is_running), 0)
            self._cursor = running
        elif previous in self._keys:
            self._cursor = self._keys.index(previous)
        self._cursor = max(0, min(self._cursor, len(jobs) - 1))
        self.virtual_size = Size(self.cols.width, len(jobs))
        if changed:
            self.refresh()
        self._keep_cursor_visible()

    def reset(self) -> None:
        """Forget the selection, as after switching host."""
        self._seeded = False
        self._cursor = 0
        self.render_jobs([])

    @property
    def selected_key(self) -> Optional[str]:
        """``model|mode|library`` of the selected row — its real identity."""
        if not self._keys:
            return None
        return self._keys[self._cursor]

    # -- drawing ---------------------------------------------------------
    def on_resize(self) -> None:
        cols = draw.columns(self.size.width or 120)
        if cols != self.cols:
            self.cols = cols
            self.virtual_size = Size(cols.width, len(self._jobs))
            self.post_message(self.Resized(cols))
        self.refresh()

    def render_line(self, y: int) -> Strip:
        t = _tokens(self)
        row = y + self.scroll_offset.y
        width = self.size.width
        if 0 <= row < len(self._jobs):
            text = draw.job_row(self._jobs[row], row == self._cursor, self.cols, t)
            segments = list(text.render(self.app.console))
            return Strip(segments).crop_extend(0, width, Style(bgcolor=t.bg))
        return Strip([Segment(" " * width, Style(bgcolor=t.bg))], width)

    # -- cursor ----------------------------------------------------------
    def action_cursor(self, delta: int) -> None:
        if not self._jobs:
            return
        self._move_to(self._cursor + delta)

    def _move_to(self, index: int) -> None:
        index = max(0, min(index, len(self._jobs) - 1))
        if index != self._cursor:
            self._cursor = index
            self.refresh()
        self._keep_cursor_visible()

    def _keep_cursor_visible(self) -> None:
        height = self.size.height
        if not height:
            return
        top = self.scroll_offset.y
        if self._cursor < top:
            self.scroll_to(y=self._cursor, animate=False)
        elif self._cursor >= top + height:
            self.scroll_to(y=self._cursor - height + 1, animate=False)

    def key_at(self, y: int) -> Optional[str]:
        """Which job key is on this row of the widget."""
        index = y + self.scroll_offset.y
        if 0 <= index < len(self._keys):
            return self._keys[index]
        return None

    # -- mouse -----------------------------------------------------------
    def on_click(self, event: Click) -> None:
        key = self.key_at(event.y)
        if key is None:
            return
        self._move_to(self._keys.index(key))
        if event.button == 3:
            self.post_message(
                self.ContextRequested(key, event.screen_x, event.screen_y)
            )
            event.stop()
        elif event.chain == 2:
            self.post_message(self.OpenLog(key))
            event.stop()


class LogDrawer(Painted):
    """The log drawer over the bottom of the dashboard.

    Data: (model, follow, lines, placeholder). Its height is set by the app;
    dragging the top edge posts :class:`LogDrawer.Dragged`. The mouse wheel
    scrolls back through the tail and freezes the view while it does.
    """

    DEFAULT_CSS = "LogDrawer { height: 14; }"

    class Dragged(Message):
        def __init__(self, delta: int) -> None:
            self.delta = delta
            super().__init__()

    class Scrolled(Message):
        """The wheel moved the view away from the live end."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.back = 0  # lines scrolled back from the end
        self._dragging = False
        self._last_y = 0

    def paint(self, width: int, t: Tokens) -> List[Text]:
        model, follow, lines, placeholder = self.data
        height = self.size.height or 14
        room = max(0, height - 4)
        self.back = max(0, min(self.back, len(lines) - room))
        end = len(lines) - self.back
        return draw.log_drawer(
            width, height, t, model, follow, lines[:end], placeholder
        )

    def on_mouse_down(self, event: MouseDown) -> None:
        if event.y == 0:
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

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        self.back += 3
        self.post_message(self.Scrolled())
        self.refresh()
        event.stop()

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        self.back = max(0, self.back - 3)
        self.refresh()
        event.stop()


class ContextMenu(Vertical):
    """Right-click verb menu. Same verbs as the keymap."""

    class Chosen(Message):
        def __init__(self, verb: str, key: str) -> None:
            self.verb = verb
            self.key = key
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

    def __init__(self, key: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.key = key

    def compose(self):
        for verb, label, extra in self.ITEMS:
            yield Button(label, id=f"ctx-{verb}", classes=extra)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        verb = (event.button.id or "")[len("ctx-") :]
        self.post_message(self.Chosen(verb, self.key))
