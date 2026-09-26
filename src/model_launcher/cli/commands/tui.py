"""Open the terminal dashboard."""

from __future__ import annotations

import os

import click
from rich_click import RichCommand

from ..target import resolve_target


@click.command(cls=RichCommand)
@click.option(
    "--refresh",
    type=float,
    default=None,
    metavar="SECONDS",
    help="Refresh interval (default: 2 locally, 5 over SSH).",
)
@click.pass_context
def tui(ctx, refresh):
    """Watch and steer the queue in a full-screen dashboard.

    With no --host (or --ctl), it first asks which machine to drive; press Ctrl+O
    inside it to switch.
    """
    obj = ctx.obj
    named = (
        obj.get("host")
        or os.environ.get("SCHEDULER_HOST")
        or obj.get("ctl")
        or os.environ.get("SCHEDULER_CTL")
    )
    runner = resolve_target(obj).runner if named else None

    if refresh is None:
        # Over SSH each tick is a round-trip; locally it is a fork. Pace accordingly.
        # The picker usually lands on a remote host, so it gets the SSH pace.
        local = runner is not None and runner.location == "local"
        refresh = 2.0 if local else 5.0

    # Imported here so --help and `check` work even where Textual is missing.
    from ...tui.app import SchedulerTUI

    SchedulerTUI(runner, refresh_interval=refresh, options=obj).run()
