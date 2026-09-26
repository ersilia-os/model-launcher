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
from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ..core.discover import Driver, HostStatus, probe_host
from ..core.hosts import LOCAL, available_targets, load_last_host
from ..core.target import Resolution, choose, resolve


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


class HostScreen(ModalScreen[Resolution | None]):
    """Pick a machine. Returns a :class:`Resolution`, or None if cancelled.

    Parameters
    ----------
    options : dict
        Connection options for :func:`~model_launcher.core.target.resolve`;
        ``host`` is filled in from the chosen row.
    current : str or None
        Key of the host already connected, marked in the list.
    """

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss_none", "Cancel")]

    def __init__(self, options: dict, current: str | None = None) -> None:
        super().__init__()
        self.options = options
        self.current = current
        self.rows: dict[str, HostRow] = {}
        self.status: dict[str, str] = {}
        self._drivers: list[Driver] = []
        self._driver_host: HostRow | None = None
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="host-box"):
            yield Static("Which machine?", id="host-title")
            yield Static("looking for machines…", id="host-hint")
            yield OptionList(id="host-list")

    def on_mount(self) -> None:
        self.query_one("#host-list", OptionList).focus()
        self.run_worker(self._load_rows, thread=True, group="hosts")

    # -- building the list ---------------------------------------------------
    def _load_rows(self) -> None:
        rows = host_rows()
        self.app.call_from_thread(self._show_rows, rows)

    def _show_rows(self, rows: list[HostRow]) -> None:
        self.rows = {row.key: row for row in rows}
        for row in rows:
            self.status[row.key] = "checking…" if row.reachable else "offline"
        options = self.query_one("#host-list", OptionList)
        options.set_options(Option(self._prompt(row), id=row.key) for row in rows)
        preferred = self.current or load_last_host()
        keys = [row.key for row in rows]
        options.highlighted = keys.index(preferred) if preferred in keys else 0
        self._hint("Enter to connect · Esc to cancel")
        for row in rows:
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
        self.query_one("#host-list", OptionList).replace_option_prompt(
            key, self._prompt(self.rows[key])
        )

    def _prompt(self, row: HostRow) -> Text:
        status = self.status.get(row.key, "")
        text = Text(no_wrap=True, overflow="ellipsis")
        name = row.name + ("  (current)" if row.key == self.current else "")
        text.append(f"{name:<26}", style="bold" if row.reachable else "dim")
        text.append(f"{row.via:<15}", style="dim")
        text.append(f"{status:<22}", style="bold" if "RUNNING" in status else "dim")
        text.append(row.detail, style="dim")
        return text

    # -- choosing --------------------------------------------------------------
    @on(OptionList.OptionSelected, "#host-list")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if self._busy:
            return
        if self._drivers:
            driver = self._drivers[event.option_index]
            row = self._driver_host
            host = (row.host or "") if row else ""
            self.dismiss(choose(self._options_for(row), driver, host))
            return
        row = self.rows.get(event.option.id or "")
        if row is None:
            return
        if not row.reachable:
            self.notify(f"{row.name} is offline.", severity="warning")
            return
        self._busy = True
        self._hint(f"connecting to {row.name}…")
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
        self.query_one("#host-title", Static).update(
            f"{len(self._drivers)} schedulers on {row.name} — which one?"
        )
        self._hint("Enter to connect · Esc to cancel")
        options = self.query_one("#host-list", OptionList)
        options.set_options(
            Option(f"{driver.log_dir}   pid {driver.pid}") for driver in self._drivers
        )
        options.highlighted = 0

    def _hint(self, message: str) -> None:
        self.query_one("#host-hint", Static).update(message)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)
