"""Headless transport probe.

The fastest answer to "can this machine reach the scheduler, and what does it
think is going on?" — no UI, so it also works over a dumb pipe and in CI.
"""

from __future__ import annotations

import sys

import click
from rich.text import Text
from rich_click import RichCommand

from ...branding import console
from ...core.model import parse_dump
from ...core.runner import RunnerError
from ..render import counts_text, snapshot_table, summary_table
from ..target import resolve_target


@click.command(cls=RichCommand)
@click.pass_context
def check(ctx):
    """Verify the transport and print one snapshot summary, then exit."""
    target = resolve_target(ctx.obj)
    runner = target.runner
    out = console()
    err = console(stderr=True)

    facts = summary_table()
    facts.add_row("transport", Text(runner.location, style="key"))
    facts.add_row("ctl", Text(runner.ctl, style="muted"))
    facts.add_row("log dir", Text(runner.log_dir or "(ctl default)", style="muted"))
    if target.source:
        facts.add_row("found", Text(target.source, style="muted"))

    try:
        text = runner.dump()
    except RunnerError as exc:
        out.print(facts)
        err.print(f"[bad]FAILED[/bad] {exc}")
        if target.hint:
            err.print(f"       {target.hint}")
        sys.exit(1)

    snap = parse_dump(text)
    if snap.error:
        out.print(facts)
        err.print(f"[bad]FAILED[/bad] {snap.error}")
        if target.hint:
            err.print(f"       {target.hint}")
        sys.exit(1)

    state = Text(snap.driver_state, style="live" if snap.driver_alive else "warn")
    if snap.driver_alive and snap.driver_pid:
        state.append(f" (pid {snap.driver_pid})", style="muted")
    facts.add_row("driver", state)
    facts.add_row("queue", Text(snap.queue_file or "—", style="muted"))
    facts.add_row("jobs", Text(str(len(snap.jobs))))
    facts.add_row("", counts_text(snap))
    facts.add_row("libraries", Text(str(len(snap.libraries))))
    out.print(facts)

    if snap.jobs:
        out.print()
        out.print(snapshot_table(snap))
