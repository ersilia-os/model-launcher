"""Smoke tests for the documented entry points.

These run the installed console script rather than importing it, so they also
cover packaging: a missing ``package-data`` entry or a broken entry point shows
up here and nowhere else.
"""

from __future__ import annotations

import subprocess

from model_launcher.core.remote import ctl_path, payload_files


def _run(scheduler, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["model-launcher", *args],
        capture_output=True,
        check=False,
        text=True,
        env=scheduler.env(),
        timeout=60,
    )


def test_help_lists_the_commands(scheduler):
    proc = _run(scheduler, "--help")
    assert proc.returncode == 0
    assert "check" in proc.stdout
    assert "tui" in proc.stdout


def test_version_is_reported(scheduler):
    proc = _run(scheduler, "--version")
    assert proc.returncode == 0
    assert "model-launcher" in proc.stdout


def test_check_reports_a_live_driver(running_scheduler):
    """`check` is the fastest end-to-end signal that a target is reachable."""
    scheduler = running_scheduler
    scheduler.ctl("add", "eos_x", "ersilia", "testlib", check=True)

    proc = _run(
        scheduler,
        "--log-dir",
        str(scheduler.log_dir),
        "--queue-file",
        str(scheduler.queue_file),
        "check",
    )
    assert proc.returncode == 0, proc.stderr
    assert "transport" in proc.stdout and "local" in proc.stdout
    assert "eos_x" in proc.stdout
    assert "driver" in proc.stdout


def test_check_fails_clearly_when_the_target_is_unreachable(scheduler):
    """A broken transport must exit non-zero, not print an empty table."""
    proc = _run(scheduler, "--ctl", "/nonexistent/sched-ctl.sh", "check")
    assert proc.returncode == 1
    assert "FAILED" in proc.stderr


def test_packaged_bash_layer_is_complete(scheduler):
    """The scripts ship with the client; deployment must not need a second source."""
    names = {path.name for path in payload_files()}
    assert ctl_path().is_file()
    assert {
        "sched-ctl.sh",
        "run-model-queue.sh",
        "scheduler-lib.sh",
        "submit-ersilia-waves.sh",
        "run-ersilia-wave-job.sh",
    } <= names
