"""The host picker: which machine should the dashboard drive?

Lists this machine plus everything ``--list-hosts`` knows about, then probes
each reachable one in the background so its row can say whether a scheduler
is actually running there. Choosing a row resolves it exactly the way
``--host`` would (see :mod:`model_launcher.core.target`); when that finds
several drivers, the same screen asks which one.

Everything slow — the tailnet lookup, each SSH probe, the resolution — runs in
a worker thread, so a dead host never freezes the list.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.events import Click, Key
from textual.screen import Screen

from ..core.discover import Driver, HostStatus, probe_host
from ..core.hosts import LOCAL, available_targets, load_last_host
from ..core.target import Resolution, choose, resolve
from . import draw
from .theme import Tokens
from .widgets import Band, HintClicked, KeyFooter, Painted


@dataclass(frozen=True)
class HostRow:
    """One machine in the picker."""

    key: str
    host: str | None  # SSH alias; None = this machine
    name: str
    via: str
    detail: str = ""
    reachable: bool = True


def host_rows() -> list[HostRow]:
    """This machine first, then every ``--list-hosts`` target.

    The tailnet lists this machine too; that row is dropped so it does not
    appear twice under two names.
    """
    rows = [HostRow(LOCAL, None, "this machine", "local", socket.gethostname())]
    for target in available_targets():
        if target.status == "this machine":
            continue
        rows.append(
            HostRow(
                key=target.name,
                host=target.name,
                name=target.name,
                via=target.via,
                detail=target.detail,
                reachable=target.reachable,
            )
        )
    return rows


class HostsView(Painted):
    """Title, header, host slots and legend. Data: the arguments of
    ``draw.hosts_view`` after the size and tokens."""

    DEFAULT_CSS = "HostsView { height: 1fr; }"
    ALLOW_SELECT = False  # a double-click connects; it must not select text

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.slot_rows: list[tuple[int, int]] = []

    def paint(self, width: int, t: Tokens) -> list[Text]:
        lines, self.slot_rows = draw.hosts_view(
            width, self.size.height or 32, t, *self.data
        )
        return lines

    def slot_at(self, y: int) -> int | None:
        return next((i for row, i in self.slot_rows if row == y), None)


class HostScreen(Screen[Resolution | None]):
    """All hosts, and which one the dashboard should drive.

    Lists this machine plus everything ``--list-hosts`` knows about, probing
    each reachable one in the background. Returns a :class:`Resolution`, or
    None if left with ``esc``. When the chosen host runs several schedulers,
    the same list becomes a list of them.

    Parameters
    ----------
    options : dict
        Connection options for :func:`~model_launcher.core.target.resolve`;
        ``host`` is filled in from the chosen row.
    current : str or None
        Key of the host already connected; its queue and running job are shown.
    """

    ALLOW_SELECT = False  # a double-click connects; it must not select text

    def __init__(self, options: dict, current: str | None = None) -> None:
        super().__init__()
        self.options = options
        self.current = current
        self.rows: dict[str, HostRow] = {}
        self.status: dict[str, str] = {}
        self._probed: dict[str, HostStatus] = {}
        self._drivers: list[Driver] = []
        self._driver_host: HostRow | None = None
        self._busy = False
        self._selected = 0
        self._top = 0
        self._filter = ""
        self._filtering = False
        self._note = "looking for machines…"

    def compose(self) -> ComposeResult:
        yield Band()
        yield HostsView()
        yield KeyFooter()

    def on_mount(self) -> None:
        self.query_one(KeyFooter).show(tuple(tuple(g) for g in draw.HOST_KEYS))
        self._redraw()
        self.run_worker(self._load_rows, thread=True, group="hosts")

    # -- building the list ---------------------------------------------------
    def _load_rows(self) -> None:
        rows = host_rows()
        self.app.call_from_thread(self._show_rows, rows)

    def _show_rows(self, rows: list[HostRow]) -> None:
        self.rows = {row.key: row for row in rows}
        self._probed = {}
        for row in rows:
            self.status[row.key] = "checking…" if row.reachable else "offline"
        preferred = self.current or load_last_host()
        keys = [row.key for row in rows]
        self._selected = keys.index(preferred) if preferred in keys else 0
        self._note = "probing…"
        self._redraw()
        for row in rows:
            self._start_probe(row)

    def _start_probe(self, row: HostRow) -> None:
        if row.reachable:
            self.run_worker(
                lambda row=row: self._probe(row), thread=True, group="probe"
            )

    def _probe(self, row: HostRow) -> None:
        status = probe_host(row.host)
        self.app.call_from_thread(self._show_status, row.key, status)

    def _show_status(self, key: str, status: HostStatus) -> None:
        if not self.is_mounted or self._drivers or key not in self.rows:
            return
        self.status[key] = status.label
        self._probed[key] = status
        self._note = f"probed {time.strftime('%H:%M:%S')}"
        self._redraw()

    # -- drawing ---------------------------------------------------------------
    def _visible(self) -> list[HostRow]:
        low = self._filter.lower()
        return [row for row in self.rows.values() if low in row.name.lower()]

    def _slot(self, row: HostRow) -> draw.HostSlot:
        probed = self._probed.get(row.key)
        if not row.reachable:
            state = "offline"
        elif probed is None:
            state = "checking"
        else:
            state = {"running": "running", "none": "none"}.get(probed.state, "offline")
        current = row.key == self.current
        strip = running = None
        if current:
            snap = self.app.snapshot  # type: ignore[attr-defined]
            strip = tuple(job.status for job in snap.jobs)
            if snap.driver_alive and snap.paused:
                state = "paused"
            job = snap.running_job()
            if job:
                running = (job.model, job.done, job.total)
        return draw.HostSlot(
            name=row.name,
            detail=row.detail,
            via=row.via,
            state=state,
            status=self.status.get(row.key, ""),
            current=current,
            strip=strip,
            running=running,
        )

    def _slots(self) -> tuple[str, list[draw.HostSlot]]:
        if self._drivers:
            row = self._driver_host
            title = f"{len(self._drivers)} schedulers on {row.name if row else '?'}"
            via = row.via if row else ""
            return title, [
                draw.HostSlot(
                    name=f"pid {driver.pid}",
                    detail=driver.log_dir,
                    via=via,
                    state="running",
                    status="RUNNING",
                )
                for driver in self._drivers
            ]
        return "All hosts", [self._slot(row) for row in self._visible()]

    def _redraw(self) -> None:
        title, slots = self._slots()
        self._selected = max(0, min(self._selected, len(slots) - 1))
        shown = draw.host_slots_visible(self.query_one(HostsView).size.height or 32)
        if self._selected < self._top:
            self._top = self._selected
        elif self._selected >= self._top + shown:
            self._top = self._selected - shown + 1
        note = self._note
        if self._filtering or self._filter:
            note = f"filter: {self._filter}{'▌' if self._filtering else ''}"
        schedulers = sum(len(p.drivers) for p in self._probed.values())
        self.query_one(Band).show(
            f"{len(self.rows)} machines · {schedulers} schedulers "
        )
        self.query_one(HostsView).show(
            title, note, tuple(slots), self._selected, self._top
        )

    # -- choosing --------------------------------------------------------------
    def _connect(self) -> None:
        if self._busy:
            return
        if self._drivers:
            driver = self._drivers[self._selected]
            row = self._driver_host
            host = (row.host or "") if row else ""
            self.dismiss(choose(self._options_for(row), driver, host))
            return
        visible = self._visible()
        if not visible:
            return
        row = visible[self._selected]
        if not row.reachable:
            self.notify(f"{row.name} is offline.", severity="warning")
            return
        self._busy = True
        self._note = f"connecting to {row.name}…"
        self._redraw()
        self.run_worker(lambda: self._resolve(row), thread=True, group="resolve")

    def _options_for(self, row: HostRow | None) -> dict:
        return {**self.options, "host": (row.host if row else None) or ""}

    def _resolve(self, row: HostRow) -> None:
        resolution = resolve(self._options_for(row))
        self.app.call_from_thread(self._resolved, row, resolution)

    def _resolved(self, row: HostRow, resolution: Resolution) -> None:
        self._busy = False
        if not resolution.choices:
            self.dismiss(resolution)
            return
        # Several schedulers on one machine: the same list becomes a list of them.
        self._drivers = list(resolution.choices)
        self._driver_host = row
        self._selected = self._top = 0
        self._note = "which one?"
        self._redraw()

    def _recheck(self) -> None:
        """``c``: probe the selected host again."""
        visible = self._visible()
        if self._drivers or not visible:
            return
        row = visible[self._selected]
        if row.reachable:
            self.status[row.key] = "checking…"
            self._probed.pop(row.key, None)
            self._redraw()
            self._start_probe(row)

    def _back(self) -> None:
        """``esc``: out of the filter, then out of a driver choice, then away."""
        if self._filtering or self._filter:
            self._filter, self._filtering = "", False
        elif self._drivers:
            self._drivers, self._driver_host = [], None
            self._note = "probing…"
            self._selected = self._top = 0
        else:
            self.dismiss(None)
            return
        self._redraw()

    # -- input -----------------------------------------------------------------
    def on_key(self, event: Key) -> None:
        # Every key stops here, so the dashboard's bindings cannot act on a
        # queue that is not on screen.
        event.stop()
        event.prevent_default()
        key = event.key
        if self._filtering:
            if key == "enter":
                self._filtering = False
            elif key == "escape":
                self._back()
                return
            elif key == "backspace":
                self._filter = self._filter[:-1]
            elif event.character and event.character.isprintable():
                self._filter += event.character
                self._selected = 0
            self._redraw()
            return
        if key in ("up", "down"):
            self._selected += -1 if key == "up" else 1
            self._redraw()
        elif key == "enter":
            self._connect()
        elif key == "escape":
            self._back()
        elif key == "c":
            self._recheck()
        elif key == "r" and not self._drivers:
            self._note = "looking for machines…"
            self.run_worker(self._load_rows, thread=True, group="hosts")
        elif key == "slash" and not self._drivers:
            self._filtering = True
            self._redraw()

    @on(HintClicked)
    def _on_hint(self, event: HintClicked) -> None:
        event.stop()
        action = {"⏎": self._connect, "c": self._recheck, "esc": self._back}.get(
            event.key
        )
        if event.key == "/":
            self._filtering = True
            self._redraw()
        elif event.key == "r":
            self.run_worker(self._load_rows, thread=True, group="hosts")
        elif action:
            action()

    def on_click(self, event: Click) -> None:
        view = self.query_one(HostsView)
        offset = event.get_content_offset(view)
        if offset is None:
            return
        index = view.slot_at(offset.y)
        if index is None:
            return
        self._selected = index
        self._redraw()
        if event.chain == 2:
            self._connect()
