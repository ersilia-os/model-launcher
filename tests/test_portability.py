"""Linux and macOS: the bash floor, and finding drivers without /proc.

macOS runs /bin/bash 3.2, which silently mangles the scheduler's associative
arrays, and has no /proc to find a running driver by. The floor re-runs every
entry script under a bash >= 4; the registry lets drivers be found anywhere.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from model_launcher.core.discover import PROBE
from model_launcher.core.remote import remote_dir

from .conftest import bash_eval

ENTRY_SCRIPTS = (
    "sched-ctl.sh",
    "run-model-queue.sh",
    "scheduler-status.sh",
    "scheduler-service.sh",
    "start-scheduler-tmux.sh",
    "install-scheduler-service.sh",
)

#: Syntax bash 3.2 does not have (or silently gets wrong).
BASH4 = re.compile(r"declare -[a-zA-Z]*A|,,}|\^\^}|\[-1\]|\bmapfile\b|\breadarray\b")


@pytest.mark.parametrize("name", ENTRY_SCRIPTS)
def test_every_entry_script_reaches_the_floor_before_any_bash4_syntax(name):
    lines = (remote_dir() / name).read_text().splitlines()
    floor = next(i for i, line in enumerate(lines) if "bash-floor.sh" in line)
    early = [line for line in lines[:floor] if not line.lstrip().startswith("#")]
    assert not [line for line in early if BASH4.search(line)]


def test_the_floor_and_the_probe_run_on_bash32():
    """Both run before any re-exec could help: they must be 3.2 themselves."""
    for text in ((remote_dir() / "bash-floor.sh").read_text(), PROBE):
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        assert not BASH4.search(code)


@pytest.mark.skipif(sys.platform != "darwin", reason="/bin/bash is 3.2 only on macOS")
def test_bin_bash_32_re_runs_ctl_under_a_newer_bash(scheduler):
    proc = subprocess.run(
        [
            "/bin/bash",
            str(remote_dir() / "sched-ctl.sh"),
            "--log-dir",
            str(scheduler.log_dir),
            "-q",
            str(scheduler.queue_file),
            "dump",
        ],
        env=scheduler.env(),
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "---8<--- runtime" in proc.stdout


# --- the driver registry -------------------------------------------------------


def _registry(scheduler) -> Path:
    return scheduler.root / "drivers"


def test_a_running_driver_registers_itself_and_unregisters_on_exit(scheduler):
    driver = scheduler.start_driver()
    scheduler.wait_for_driver_info()

    entry = _registry(scheduler) / str(driver.pid)
    assert f"log_dir={scheduler.log_dir}" in entry.read_text().splitlines()

    scheduler.ctl("shutdown", check=True)
    driver.wait(timeout=30)
    assert not entry.exists()


def test_a_stale_registry_entry_is_ignored_and_dropped(scheduler):
    registry = _registry(scheduler)
    registry.mkdir()
    dead = subprocess.Popen(["true"])
    dead.wait()
    stale = registry / str(dead.pid)
    stale.write_text(f"log_dir={scheduler.log_dir}\nscript_dir=/nowhere\n")

    proc = bash_eval("registry_list", scheduler.env())
    assert proc.stdout == ""
    assert not stale.exists()


def test_driver_pid_scan_finds_a_driver_through_the_registry(running_scheduler):
    proc = bash_eval("driver_pid_scan", running_scheduler.env())
    info = (running_scheduler.log_dir / "driver.info").read_text()
    assert f"pid={proc.stdout.strip()}" in info.splitlines()


def test_the_probe_finds_a_driver_from_the_registry_alone(running_scheduler):
    """Only the registry half of the probe — what a Mac, with no /proc, runs."""
    registry_half = PROBE.split("[ -d /proc/self ]")[0] + "\nexit 0\n"
    proc = subprocess.run(
        ["bash", "-s"],
        input=registry_half,
        env=running_scheduler.env(),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    _pid, log_dir, script_dir = proc.stdout.strip().split("\t")
    assert log_dir == str(running_scheduler.log_dir)
    assert script_dir == str(remote_dir())
