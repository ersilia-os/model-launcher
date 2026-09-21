"""Turning results into something pleasant to read in a terminal.

Kept apart from the modules that produce the data, so discovery and transport
stay testable without a console attached.
"""

from __future__ import annotations

from typing import List

from rich.box import SIMPLE_HEAD
from rich.table import Table
from rich.text import Text

from ..core.hosts import Target, ssh_config_path
from ..core.model import STATUS_ORDER, Snapshot

#: How a scheduler status is coloured. Mirrors the dashboard: mint is settled,
#: blue is the one live row, warm hues mean a human is needed.
STATUS_STYLES = {
    "done": "ok",
    "running": "live",
    "pending": "muted",
    "held": "warn",
    "failed": "bad",
    "cancelled": "bad",
    "missing-files": "alert",
    "skipped": "gone",
    "stale": "warn",
}


def _via_style(via: str) -> str:
    return "key" if via.startswith("ssh") else "ok"


def summary_table() -> Table:
    """An unboxed label/value table for a short block of facts.

    A table rather than padded f-strings so a long path is aligned rather than
    wrapped under its own label.
    """
    table = Table(box=None, show_header=False, pad_edge=False, show_edge=False)
    table.add_column(style="muted", no_wrap=True)
    table.add_column(overflow="fold")
    return table


def targets_table(targets: List[Target]) -> Table:
    """Render discovered machines as a table."""
    table = Table(
        box=SIMPLE_HEAD,
        header_style="heading",
        expand=False,
        pad_edge=False,
        show_edge=False,
    )
    table.add_column("HOST", no_wrap=True)
    table.add_column("VIA", no_wrap=True)
    table.add_column("DETAIL", overflow="fold")
    table.add_column("STATUS", no_wrap=True)

    for target in targets:
        if target.status == "this machine":
            status = Text(target.status, style="live")
        elif target.status == "offline":
            status = Text(target.status, style="gone")
        elif target.status == "online":
            status = Text(target.status, style="ok")
        else:
            status = Text(target.status or "—", style="muted")
        name_style = "" if target.reachable else "gone"
        table.add_row(
            Text(target.name, style=name_style),
            Text(target.via, style=_via_style(target.via)),
            Text(target.detail, style="muted"),
            status,
        )
    return table


def no_targets_message() -> Text:
    """Explain what to do when nothing was discovered."""
    return Text.from_markup(
        f"[warn]No machines found.[/warn]\n"
        f"Add a Host entry to [key]{ssh_config_path()}[/key], or join the "
        f"tailnet with [key]tailscale up --ssh[/key], then pass the name with "
        f"[key]--host[/key]."
    )


def counts_text(snapshot: Snapshot) -> Text:
    """Render the per-status tally, each count in its own status colour.

    ``Snapshot.counts()`` returns a dict; printing that raw shows Python
    punctuation to someone who just wants to know how the queue is doing.
    """
    counts = snapshot.counts()
    if not counts:
        return Text("no jobs", style="muted")
    text = Text()
    for index, status in enumerate(STATUS_ORDER):
        if status not in counts:
            continue
        if index and len(text):
            text.append("  ")
        text.append(f"{status} ", style="muted")
        text.append(str(counts[status]), style=STATUS_STYLES.get(status, "muted"))
    return text


def snapshot_table(snapshot: Snapshot) -> Table:
    """Render the queue from one scheduler snapshot."""
    table = Table(
        box=SIMPLE_HEAD,
        header_style="heading",
        expand=False,
        pad_edge=False,
        show_edge=False,
    )
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("MODEL", no_wrap=True)
    table.add_column("STATUS", no_wrap=True)
    table.add_column("PROGRESS", no_wrap=True)
    table.add_column("LIBRARY", overflow="fold")

    for job in snapshot.jobs:
        style = STATUS_STYLES.get(job.status, "muted")
        table.add_row(
            Text(str(job.pos), style="muted"),
            Text(job.model),
            Text(job.status, style=style),
            Text(f"{job.done}/{job.total} ({job.pct}%)", style=style),
            Text(job.library, style="muted"),
        )
    return table
