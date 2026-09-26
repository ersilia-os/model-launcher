"""M2: keeping the driver alive under systemd.

systemd itself is not available to the tests, so these cover the three things
the service relies on, against the real bash:

* the driver lock survives the way drivers actually die (SIGKILL included),
  so a restart is never blocked by a stale lock;
* ``scheduler-service.sh`` is a transparent entry point (``exec``, same pid,
  discoverable) and its post-stop step cancels only what a crash left behind;
* the rendered unit carries every setting the roadmap's invariants need.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from model_launcher.core.discover import discover_drivers
from model_launcher.core.remote import remote_dir
from model_launcher.core.runner import LocalRunner

SERVICE = remote_dir() / "scheduler-service.sh"
INSTALL = remote_dir() / "install-scheduler-service.sh"


def _wait(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _info_pid(scheduler) -> str:
    info = scheduler.log_dir / "driver.info"
    if not info.exists():
        return ""
    for line in info.read_text().splitlines():
        if line.startswith("pid="):
            return line[4:]
    return ""


def _kill_orphans(log_dir: Path) -> None:
    """SIGKILL the fake orchestrators a killed driver left behind (ours only)."""
    proc = subprocess.run(
        ["pgrep", "-f", "sleep 600"], capture_output=True, check=False, text=True
    )
    for pid in proc.stdout.split():
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if f"LOG_DIR={log_dir}".encode() in env:
            os.kill(int(pid), signal.SIGKILL)


# --- the lock ---------------------------------------------------------------


def test_a_second_driver_on_the_same_log_dir_exits_75(running_scheduler):
    """75 is what the unit's RestartPreventExitStatus= keys on: fail once, don't loop."""
    second = running_scheduler.start_driver()
    assert second.wait(timeout=30) == 75
    assert "another driver holds the lock" in running_scheduler.driver_log()


@pytest.mark.linux_only
def test_a_sigkilled_driver_does_not_block_the_next_one(scheduler):
    """The old mkdir lock outlived SIGKILL; every restart then failed forever."""
    scheduler.write_queue("eos_x ersilia testlib")
    first = scheduler.start_driver()
    scheduler.wait_for_status("eos_x", "running")
    try:
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=10)

        second = scheduler.start_driver()
        assert _wait(lambda: _info_pid(scheduler) == str(second.pid)), (
            f"restart never came up:\n{scheduler.driver_log()}"
        )
        assert second.poll() is None
        assert "reclaimed interrupted job: eos_x" in scheduler.driver_log()
    finally:
        _kill_orphans(scheduler.log_dir)


def test_a_stale_legacy_lock_directory_is_cleared(scheduler):
    """Upgrading over a SIGKILLed pre-flock driver must not need a manual rmdir."""
    (scheduler.log_dir / ".lock").mkdir()
    scheduler.start_driver()
    scheduler.wait_for_driver_info()
    assert not (scheduler.log_dir / ".lock").exists()
    assert "removed stale lock directory" in scheduler.driver_log()


def test_a_process_that_only_mentions_the_driver_is_not_a_driver(scheduler):
    """The tmux server's argv is the whole launch command as one word.

    It outlives the scheduler session while any other session exists and has no
    LOG_DIR of its own, so a scan that trusted `pgrep -f` would take it for a
    live pre-flock driver and refuse to start forever.
    """
    env = {k: v for k, v in os.environ.items() if k != "LOG_DIR"}
    lookalike = subprocess.Popen(
        ["sh", "-c", "sleep 60; : bash /x/run-model-queue.sh models.queue"], env=env
    )
    try:
        (scheduler.log_dir / ".lock").mkdir()
        scheduler.start_driver()
        scheduler.wait_for_driver_info()
        assert not (scheduler.log_dir / ".lock").exists()
    finally:
        lookalike.kill()
        lookalike.wait(timeout=10)


@pytest.mark.linux_only
def test_a_live_legacy_lock_holder_is_respected(running_scheduler):
    """A pre-flock driver never sees our flock, so its directory must still count."""
    (running_scheduler.log_dir / ".lock").mkdir()
    second = running_scheduler.start_driver()
    assert second.wait(timeout=30) == 75
    assert "an older driver" in running_scheduler.driver_log()


# --- scheduler-service.sh ---------------------------------------------------


@pytest.mark.linux_only
def test_service_start_becomes_the_driver_and_is_discoverable(scheduler, tmp_path):
    """LOG_DIR comes only from scheduler.conf here, as it would under systemd.

    Discovery reads /proc/<pid>/environ, which only holds what the process was
    exec'd with — so this passes only if the entry script exports LOG_DIR
    before exec, not if the driver exports it afterwards.
    """
    conf = tmp_path / "scheduler.conf"
    conf.write_text(f'LOG_DIR="${{LOG_DIR:-{scheduler.log_dir}}}"\n')
    env = scheduler.env(SCHEDULER_CONF=str(conf))
    del env["LOG_DIR"]

    proc = subprocess.Popen(
        [
            "bash",
            str(SERVICE),
            "start",
            str(scheduler.queue_file),
            "testlib",
            "--dry-run",
        ],
        env=env,
        cwd=scheduler.root,
    )
    scheduler._drivers.append(proc)
    scheduler.wait_for_driver_info()

    assert _info_pid(scheduler) == str(proc.pid), "exec must keep the unit's main pid"
    assert "starting under systemd" in scheduler.driver_log()
    found = {d.log_dir: d for d in discover_drivers(LocalRunner(ctl="unused"))}
    assert found[str(scheduler.log_dir)].pid == proc.pid


def _stop_post(scheduler) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SERVICE), "stop-post"],
        env=scheduler.env(),
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )


@pytest.mark.linux_only
def test_stop_post_cancels_what_a_crashed_driver_left_on_slurm(scheduler):
    job_log = scheduler.log_dir / "eos_x_testlib.log"
    job_log.write_text("Submitted array job 4242\nSubmitted batch job 4243\n")
    scheduler.write_status(
        [
            f"eos_x|ersilia|testlib\trunning\t1\t100\t2026-01-01T00:00:00Z\t-\t{job_log}\t-"
        ]
    )

    assert _stop_post(scheduler).returncode == 0

    calls = scheduler.stub_calls("scancel")
    assert calls == ["4242 ", "4243 "]
    # Left `running` on purpose: the restarted driver reclaims and resumes it.
    assert "\trunning\t" in (scheduler.log_dir / "status.tsv").read_text()


def test_stop_post_does_nothing_after_a_clean_stop(scheduler):
    """A clean stop has already cancelled and marked the job; nothing is left to do."""
    job_log = scheduler.log_dir / "eos_x_testlib.log"
    job_log.write_text("Submitted array job 4242\n")
    scheduler.write_status(
        [
            f"eos_x|ersilia|testlib\tcancelled\t1\t100\t2026-01-01T00:00:00Z\t-\t{job_log}\t-"
        ]
    )
    assert _stop_post(scheduler).returncode == 0
    assert scheduler.stub_calls("scancel") == []


# --- the unit ---------------------------------------------------------------


def _render(scheduler, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(INSTALL), "--print", str(scheduler.queue_file), *args],
        env=scheduler.env(**env),
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )


def test_the_unit_carries_every_setting_the_invariants_need(scheduler):
    proc = _render(scheduler, "testlib")
    assert proc.returncode == 0, proc.stderr
    # The template is an unquoted heredoc: anything on stderr means some of its
    # text was run as a command instead of written out.
    assert proc.stderr == ""
    unit = proc.stdout
    assert '"systemctl stop" would count as a failure' in unit
    for setting in (
        "KillMode=mixed",
        "TimeoutStopSec=180",
        "SuccessExitStatus=130 143",
        "Restart=on-failure",
        "RestartPreventExitStatus=75",
        f'Environment="LOG_DIR={scheduler.log_dir}"',
        f"ExecStart={remote_dir()}/scheduler-service.sh start {scheduler.queue_file} testlib",
        f"ExecStopPost={remote_dir()}/scheduler-service.sh stop-post",
    ):
        assert setting in unit, setting
    path_line = next(line for line in unit.splitlines() if "PATH=" in line)
    assert str(scheduler.stub_bin) in path_line
    assert "StandardOutput=append" not in unit  # does not exist on systemd 219


def test_install_refuses_a_path_without_slurm(scheduler):
    bare = "/usr/bin:/bin"
    if shutil.which("squeue", path=bare):  # pragma: no cover - a real SLURM host
        pytest.skip("SLURM is installed system-wide here")
    proc = _render(scheduler, PATH=bare)
    assert proc.returncode == 1
    assert "not on PATH" in proc.stderr


def test_install_refuses_arguments_systemd_would_reinterpret(scheduler):
    proc = _render(scheduler, "lib%with%specifiers")
    assert proc.returncode == 1


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None, reason="systemd-analyze not available"
)
def test_the_unit_passes_systemd_analyze_verify(scheduler, tmp_path):
    unit = tmp_path / "ersilia-scheduler.service"
    unit.write_text(_render(scheduler, "testlib").stdout)
    proc = subprocess.run(
        ["systemd-analyze", "verify", str(unit)],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    # verify also reports on unrelated units already on this machine; only
    # complaints about ours count.
    ours = [line for line in proc.stderr.splitlines() if unit.name in line]
    assert not ours, "\n".join(ours)
