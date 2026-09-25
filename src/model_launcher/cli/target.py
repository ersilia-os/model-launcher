"""Turn the group's connection options into a runner pointed at a real scheduler.

With ``--host`` and no ``--ctl``, the client asks the target which scheduler
drivers are running and takes the ctl path — and, if not given, the
``LOG_DIR`` — from the one it finds. See :mod:`model_launcher.core.discover`.
"""

from __future__ import annotations

import os
import posixpath
import sys
from dataclasses import dataclass
from typing import List, Optional

import click

from ..branding import console
from ..core.discover import Driver, discover_drivers
from ..core.runner import Runner, RunnerError, SshRunner, build_runner


@dataclass
class Target:
    """A runner plus how its ctl path was chosen, for the user to see.

    Attributes
    ----------
    runner : Runner
        Transport to the chosen scheduler.
    source : str or None
        One line on where ``ctl`` came from; None when it was given explicitly
        or the run is local.
    hint : str or None
        Extra advice to print if talking to ctl then fails.
    """

    runner: Runner
    source: Optional[str] = None
    hint: Optional[str] = None


def resolve_target(obj: dict) -> Target:
    """Build the runner for this invocation, discovering the scheduler if needed.

    Parameters
    ----------
    obj : dict
        The group's connection options (``ctx.obj``).

    Returns
    -------
    Target
        The runner, and a note on how its ctl path was chosen.

    Raises
    ------
    click.ClickException
        If several drivers are running, none was chosen with ``--log-dir``,
        and there is no terminal to ask on.
    """
    runner = build_runner(**obj)
    explicit_ctl = obj.get("ctl") or os.environ.get("SCHEDULER_CTL")
    if not isinstance(runner, SshRunner) or explicit_ctl:
        return Target(runner)

    try:
        drivers = discover_drivers(runner)
    except RunnerError:
        # Unreachable host: the ctl call that follows reports it properly.
        return Target(runner)

    host = runner.host
    if runner.log_dir:
        want = posixpath.normpath(runner.log_dir)
        for driver in drivers:
            if driver.log_dir == want:
                return Target(_rebuild(obj, driver), _found(driver))
        if not drivers:
            return Target(runner, *_nothing_running(host))
        # Probably a mistyped --log-dir. Still honour it (it may be a stopped
        # instance's history), but with ctl from the running deployment.
        _warn(
            f"no running driver on {host} uses --log-dir {want}; running: "
            + ", ".join(d.log_dir for d in drivers)
        )
        ctls = {d.ctl for d in drivers}
        if len(ctls) == 1:
            ctl = ctls.pop()
            runner = build_runner(**{**obj, "ctl": ctl})
            return Target(runner, f"{ctl} (from the running driver)")
        return Target(runner, *_nothing_running(host))

    if not drivers:
        return Target(runner, *_nothing_running(host))
    if len(drivers) == 1:
        return Target(_rebuild(obj, drivers[0]), _found(drivers[0]))
    driver = _choose(host, drivers)
    return Target(_rebuild(obj, driver), _found(driver))


def _rebuild(obj: dict, driver: Driver) -> Runner:
    return build_runner(**{**obj, "ctl": driver.ctl, "log_dir": driver.log_dir})


def _found(driver: Driver) -> str:
    return f"running driver pid {driver.pid} (discovered)"


def _nothing_running(host: str) -> tuple:
    return (
        "no running driver found; using the default path",
        f"no running driver found on {host}, and the default path is wrong "
        "for this machine — pass --ctl /path/to/sched-ctl.sh",
    )


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


def _warn(message: str) -> None:
    console(stderr=True).print(f"[warn]warning[/warn] {message}")
