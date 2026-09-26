"""Resolve the group's connection options for a terminal command.

The logic lives in :mod:`model_launcher.core.target`; this adds the two parts
that need a terminal: printing the warning, and asking which driver to use
when several are running.
"""

from __future__ import annotations

import sys
from typing import List

import click

from ..branding import console
from ..core.discover import Driver
from ..core.target import Resolution, choose, resolve


def resolve_target(obj: dict) -> Resolution:
    """Build the runner for this invocation, asking on the terminal if needed.

    Parameters
    ----------
    obj : dict
        The group's connection options (``ctx.obj``).

    Returns
    -------
    Resolution
        The runner, and a note on how its ctl path was chosen.

    Raises
    ------
    click.ClickException
        If several drivers are running, none was chosen with ``--log-dir``,
        and there is no terminal to ask on.
    """
    resolution = resolve(obj)
    if resolution.warning:
        console(stderr=True).print(f"[warn]warning[/warn] {resolution.warning}")
    if resolution.choices:
        driver = _choose(resolution.host, resolution.choices)
        resolution = choose(obj, driver, resolution.host)
    return resolution


def _choose(host: str, drivers: List[Driver]) -> Driver:
    listing = "\n".join(
        f"  {i}. {d.log_dir}  (pid {d.pid})" for i, d in enumerate(drivers, 1)
    )
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        lines = "\n".join(f"  --log-dir {d.log_dir}" for d in drivers)
        raise click.ClickException(
            f"{len(drivers)} scheduler drivers are running on {host}; "
            f"pick one with:\n{lines}"
        )
    click.echo(f"{len(drivers)} scheduler drivers are running on {host}:\n{listing}")
    choice = click.prompt("Which one", type=click.IntRange(1, len(drivers)), default=1)
    return drivers[choice - 1]
