"""macOS: the driver as a LaunchAgent.

launchd lacks three systemd settings the driver relies on (SuccessExitStatus,
RestartPreventExitStatus, ExecStopPost), so ``scheduler-service.sh launchd``
supplies them as the driver's parent. The plist itself is rendered anywhere with
``SCHED_SERVICE_OS=Darwin``; the crash clean-up needs a real Mac.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time

import pytest

from model_launcher.core.remote import remote_dir

INSTALL = remote_dir() / "install-scheduler-service.sh"
SERVICE = remote_dir() / "scheduler-service.sh"


def _render(scheduler, tmp_path, **env: str) -> subprocess.CompletedProcess:
    data_dir = tmp_path / "data"
    defaults = {
        "SCHED_SERVICE_OS": "Darwin",
        "DISPATCH": "serve",
        "DATA_DIR": str(data_dir),
        "ERSILIA_BIN": str(scheduler.stub_bin / "ersilia"),
    }
    return subprocess.run(
        ["bash", str(INSTALL), "--print", str(scheduler.queue_file), "lib1"],
        env=scheduler.env(**{**defaults, **env}),
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )


def test_the_agent_runs_the_wrapper_with_the_hosts_settings(scheduler, tmp_path):
    proc = _render(scheduler, tmp_path)
    assert proc.returncode == 0, proc.stderr
    agent = plistlib.loads(proc.stdout.encode())

    assert agent["Label"] == "io.ersilia.model-launcher"
    assert agent["ProgramArguments"][1:] == [
        str(SERVICE),
        "launchd",
        str(scheduler.queue_file),
        "lib1",
    ]
    env = agent["EnvironmentVariables"]
    assert env["DISPATCH"] == "serve"
    assert env["DATA_DIR"] == str(tmp_path / "data")
    assert env["LOG_DIR"] == str(scheduler.log_dir)
    assert env["HOME"] == os.environ["HOME"]
    # Restarted after a crash only; a stop lets the driver close its model.
    assert agent["RunAtLoad"] is True
    assert agent["KeepAlive"] == {"SuccessfulExit": False}
    assert agent["ExitTimeOut"] == 180
    assert agent["StandardErrorPath"] == str(scheduler.log_dir / "driver.log")


def test_plist_values_are_escaped(scheduler, tmp_path):
    proc = _render(scheduler, tmp_path, DATA_DIR=str(tmp_path / "R&D<1>"))
    agent = plistlib.loads(proc.stdout.encode())
    assert agent["EnvironmentVariables"]["DATA_DIR"] == str(tmp_path / "R&D<1>")


def test_a_mac_host_must_be_a_serve_host(scheduler, tmp_path):
    env = scheduler.env(SCHED_SERVICE_OS="Darwin")
    env.pop("DISPATCH", None)
    proc = subprocess.run(
        ["bash", str(INSTALL), "--print", str(scheduler.queue_file)],
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1
    assert "DISPATCH=serve" in proc.stderr


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS-only")
def test_the_agent_passes_plutil_lint(scheduler, tmp_path):
    agent = tmp_path / "agent.plist"
    agent.write_text(_render(scheduler, tmp_path).stdout)
    proc = subprocess.run(
        ["plutil", "-lint", str(agent)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- the wrapper: what launchd itself cannot express ----------------------------


def _wrapper(scheduler, **popen) -> subprocess.Popen:
    """``scheduler-service.sh launchd``, as the LaunchAgent runs it."""
    proc = subprocess.Popen(
        [
            "bash",
            str(SERVICE),
            "launchd",
            str(scheduler.queue_file),
            "testlib",
            "--dry-run",
        ],
        env=scheduler.env(),
        cwd=scheduler.root,
        **popen,
    )
    scheduler._drivers.append(proc)
    return proc


def _driver_pid(scheduler) -> int:
    for line in (scheduler.log_dir / "driver.info").read_text().splitlines():
        if line.startswith("pid="):
            return int(line[4:])
    raise AssertionError("no pid in driver.info")


def test_a_stop_exits_0_so_launchd_does_not_restart_it(scheduler):
    scheduler.write_queue("eos_x ersilia testlib")
    wrapper = _wrapper(scheduler)
    scheduler.wait_for_status("eos_x", "running")

    wrapper.send_signal(signal.SIGTERM)

    assert wrapper.wait(timeout=60) == 0
    assert scheduler.dump().find("eos_x").status == "cancelled"
    assert "starting under launchd" in scheduler.driver_log()


def test_a_held_lock_exits_0_so_launchd_does_not_loop(running_scheduler):
    wrapper = _wrapper(running_scheduler)

    assert wrapper.wait(timeout=60) == 0
    assert "not restarting" in running_scheduler.driver_log()


def test_a_crashed_driver_hands_its_exit_code_back_to_launchd(scheduler):
    scheduler.write_queue("eos_x ersilia testlib")
    # launchd makes each job the leader of its own process group.
    wrapper = _wrapper(scheduler, start_new_session=True)
    scheduler.wait_for_status("eos_x", "running")
    orchestrators = scheduler.sleep_children()
    assert orchestrators

    try:
        os.kill(_driver_pid(scheduler), signal.SIGKILL)
        assert wrapper.wait(timeout=90) == 128 + signal.SIGKILL
        assert "without cleaning up" in scheduler.driver_log()

        if sys.platform == "darwin":
            # There the orchestrator shares the wrapper's process group, and the
            # wrapper stops it (its trap would close the ersilia model).
            deadline = time.monotonic() + 30
            while scheduler.sleep_children() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert scheduler.sleep_children() == []
            assert "stopping what it left running" in scheduler.driver_log()
    finally:
        for pid in scheduler.sleep_children():
            os.kill(pid, signal.SIGKILL)
