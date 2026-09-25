"""Open the terminal dashboard."""

from __future__ import annotations

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
    """Watch and steer the queue in a full-screen dashboard."""
    runner = resolve_target(ctx.obj).runner

    if refresh is None:
        # Over SSH each tick is a round-trip; locally it is a fork. Pace accordingly.
        refresh = 5.0 if runner.location != "local" else 2.0

    # Imported here so --help and `check` work even where Textual is missing.
    from ...tui.app import SchedulerTUI

    SchedulerTUI(runner, refresh_interval=refresh).run()
