"""Turn connection options into a runner pointed at a real scheduler.

With a host and no explicit ctl, the client asks the target which scheduler
drivers are running and takes the ctl path — and, if not given, the
``LOG_DIR`` — from the one it finds. See :mod:`model_launcher.core.discover`.

Pure: nothing here prompts or prints. When the answer needs a person (several
drivers, none chosen), :func:`resolve` says so and the caller asks in whatever
way suits it — a terminal prompt in the CLI, a list in the dashboard.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass, field
from typing import List, Optional

from .discover import Driver, discover_drivers
from .runner import Runner, RunnerError, SshRunner, build_runner


@dataclass
class Resolution:
    """The outcome of resolving connection options.

    Attributes
    ----------
    runner : Runner or None
        Transport to the chosen scheduler; None only when ``choices`` is set.
    source : str or None
        One line on where ``ctl`` came from; None when it was given explicitly
        or the run is local.
    hint : str or None
        Advice to show if talking to ctl then fails.
    warning : str or None
        Something the user should see now, e.g. a probably-mistyped log dir.
    choices : list of Driver
        Set when several drivers are running and none was picked: call
        :func:`choose` with one of them.
    host : str
        The SSH host, or "" for this machine.
    """

    runner: Optional[Runner]
    source: Optional[str] = None
    hint: Optional[str] = None
    warning: Optional[str] = None
    choices: List[Driver] = field(default_factory=list)
    host: str = ""


def resolve(obj: dict) -> Resolution:
    """Build the runner for these options, discovering the scheduler if needed.

    Parameters
    ----------
    obj : dict
        Connection options, as ``build_runner`` takes them (the CLI's ``ctx.obj``).

    Returns
    -------
    Resolution
        A runner, or ``choices`` when several drivers run and none was chosen.
    """
    runner = build_runner(**obj)
    explicit_ctl = obj.get("ctl") or os.environ.get("SCHEDULER_CTL")
    if not isinstance(runner, SshRunner) or explicit_ctl:
        return Resolution(runner, host=getattr(runner, "host", ""))

    host = runner.host
    try:
        drivers = discover_drivers(runner)
    except RunnerError:
        # Unreachable host: the ctl call that follows reports it properly.
        return Resolution(runner, host=host)

    if runner.log_dir:
        want = posixpath.normpath(runner.log_dir)
        for driver in drivers:
            if driver.log_dir == want:
                return choose(obj, driver, host)
        if not drivers:
            return _nothing_running(runner, host)
        # Probably a mistyped --log-dir. Still honour it (it may be a stopped
        # instance's history), but with ctl from the running deployment.
        warning = f"no running driver on {host} uses --log-dir {want}; running: " + (
            ", ".join(d.log_dir for d in drivers)
        )
        ctls = {d.ctl for d in drivers}
        if len(ctls) == 1:
            ctl = ctls.pop()
            return Resolution(
                build_runner(**{**obj, "ctl": ctl}),
                source=f"{ctl} (from the running driver)",
                warning=warning,
                host=host,
            )
        resolution = _nothing_running(runner, host)
        resolution.warning = warning
        return resolution

    if not drivers:
        return _nothing_running(runner, host)
    if len(drivers) == 1:
        return choose(obj, drivers[0], host)
    return Resolution(None, choices=drivers, host=host)


def choose(obj: dict, driver: Driver, host: str = "") -> Resolution:
    """Resolve to one specific discovered driver.

    Parameters
    ----------
    obj : dict
        The same connection options passed to :func:`resolve`.
    driver : Driver
        The driver to talk to — one of a previous resolution's ``choices``.
    host : str
        The host it runs on, for display.

    Returns
    -------
    Resolution
        A runner using that driver's ctl and ``LOG_DIR``.
    """
    runner = build_runner(**{**obj, "ctl": driver.ctl, "log_dir": driver.log_dir})
    return Resolution(
        runner,
        source=f"running driver pid {driver.pid} (discovered)",
        host=host or getattr(runner, "host", ""),
    )


def _nothing_running(runner: Runner, host: str) -> Resolution:
    return Resolution(
        runner,
        source="no running driver found; using the default path",
        hint=f"no running driver found on {host}, and the default path is wrong "
        "for this machine — pass --ctl /path/to/sched-ctl.sh",
        host=host,
    )
