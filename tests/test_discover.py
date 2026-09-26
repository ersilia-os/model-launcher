"""Finding the scheduler from its running driver, rather than a guessed path.

The first real deploy to a new directory failed because the client fell back
to a path hardcoded to the old layout. These cover the replacement: the probe
must find a real driver (and only the instance asked about), and the CLI must
pick one sensibly when there are zero, one or several.
"""

from __future__ import annotations

import io
import time
from typing import ClassVar

import click
import pytest

from model_launcher.cli.target import resolve_target
from model_launcher.core import target as target_mod
from model_launcher.core.discover import Driver, discover_drivers
from model_launcher.core.remote import ctl_path
from model_launcher.core.runner import LocalRunner


@pytest.mark.linux_only
def test_probe_finds_each_running_driver_by_its_log_dir(running_scheduler, tmp_path):
    """Two instances on one machine are two results, each with its own LOG_DIR."""
    scheduler = running_scheduler
    other_log_dir = tmp_path / "other-logs"
    scheduler.start_driver(LOG_DIR=str(other_log_dir))
    info = other_log_dir / "driver.info"
    deadline = time.monotonic() + 20
    while not info.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert info.exists(), "second driver never wrote driver.info"

    found = {d.log_dir: d for d in discover_drivers(LocalRunner(ctl="unused"))}

    mine = found[str(scheduler.log_dir)]
    assert mine.pid == scheduler._drivers[0].pid
    assert mine.ctl == str(ctl_path())
    assert found[str(other_log_dir)].pid == scheduler._drivers[1].pid


OBJ = {
    "host": "fakehost",
    "ctl": None,
    "log_dir": None,
    "queue_file": None,
    "s3_bucket": None,
    "ssh_opts": [],
    "who": "tester",
}
PROD = Driver(pid=11, log_dir="/shared/logs/scheduler", ctl="/srv/a/sched-ctl.sh")
TEST = Driver(pid=22, log_dir="/tmp/schedtest", ctl="/srv/a/sched-ctl.sh")


@pytest.fixture
def drivers(monkeypatch):
    """Stub discovery: set ``drivers.running`` to what the target reports."""
    for var in ("SCHEDULER_CTL", "SCHEDULER_HOST", "LOG_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO())  # no terminal to prompt on

    class Running:
        running: ClassVar[list] = []

    monkeypatch.setattr(target_mod, "discover_drivers", lambda _r: Running.running)
    return Running


def test_a_single_driver_supplies_ctl_and_log_dir(drivers):
    drivers.running = [PROD]
    target = resolve_target(OBJ)
    assert target.runner.ctl == PROD.ctl
    assert target.runner.log_dir == PROD.log_dir
    assert "discovered" in target.source


def test_log_dir_selects_among_several(drivers):
    drivers.running = [PROD, TEST]
    target = resolve_target({**OBJ, "log_dir": "/tmp/schedtest/"})
    assert target.runner.log_dir == "/tmp/schedtest"
    assert "pid 22" in target.source


def test_several_drivers_without_a_terminal_list_the_choices(drivers):
    drivers.running = [PROD, TEST]
    with pytest.raises(click.ClickException) as exc:
        resolve_target(OBJ)
    assert "--log-dir /shared/logs/scheduler" in exc.value.message
    assert "--log-dir /tmp/schedtest" in exc.value.message


def test_a_mistyped_log_dir_still_uses_the_running_deployment(drivers, capsys):
    """The scheduler-v2 mistake: warn, but keep the ctl that actually exists."""
    drivers.running = [PROD]
    target = resolve_target({**OBJ, "log_dir": "/shared/logs/scheduler-v2"})
    assert target.runner.ctl == PROD.ctl
    assert target.runner.log_dir == "/shared/logs/scheduler-v2"
    assert "/shared/logs/scheduler" in capsys.readouterr().err


def test_no_driver_falls_back_with_a_hint(drivers):
    drivers.running = []
    target = resolve_target(OBJ)
    assert target.runner.ctl == "/shared/scripts/scheduler/sched-ctl.sh"
    assert "--ctl" in target.hint


def test_an_explicit_ctl_skips_discovery(drivers):
    drivers.running = [PROD]
    target = resolve_target({**OBJ, "ctl": "/elsewhere/sched-ctl.sh"})
    assert target.runner.ctl == "/elsewhere/sched-ctl.sh"
    assert target.source is None
