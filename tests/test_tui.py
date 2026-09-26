"""Smoke tests for the dashboard's host picker and host switching.

Driven with Textual's own test harness (``run_test``) against the real bash
scheduler from the fixtures; only the host list and the SSH probe are stubbed,
since there is no second machine to reach.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from model_launcher.core.discover import HostStatus, probe_host
from model_launcher.core.hosts import Target, load_last_host, save_last_host
from model_launcher.core.model import Job, Snapshot
from model_launcher.core.remote import ctl_path
from model_launcher.core.runner import LocalRunner
from model_launcher.core.target import Resolution
from model_launcher.tui import hosts as hosts_mod
from model_launcher.tui.app import SchedulerTUI
from model_launcher.tui.hosts import HostScreen


async def _until(pilot, predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await pilot.pause(0.05)


@pytest.fixture
def environment(scheduler, monkeypatch, tmp_path):
    """Run the app under the scheduler's stub environment, with a clean config."""
    for key, value in scheduler.env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for var in ("SCHEDULER_HOST", "SCHEDULER_CTL"):
        monkeypatch.delenv(var, raising=False)
    return scheduler


def _local_runner(scheduler) -> LocalRunner:
    return LocalRunner(
        ctl=str(ctl_path()),
        log_dir=str(scheduler.log_dir),
        queue_file=str(scheduler.queue_file),
    )


def test_the_picker_connects_to_the_chosen_host(environment, monkeypatch):
    scheduler = environment
    scheduler.start_driver()
    scheduler.wait_for_driver_info()
    monkeypatch.setattr(
        hosts_mod,
        "available_targets",
        lambda: [Target(name="ai2050cluster", via="ssh", detail="ec2-user@x")],
    )
    monkeypatch.setattr(
        hosts_mod,
        "probe_host",
        lambda host: HostStatus("none" if host is None else "unreachable"),
    )
    options = {
        "log_dir": str(scheduler.log_dir),
        "queue_file": str(scheduler.queue_file),
    }

    async def scenario() -> None:
        app = SchedulerTUI(None, refresh_interval=0.2, live_interval=0, options=options)
        async with app.run_test() as pilot:
            await _until(pilot, lambda: isinstance(app.screen, HostScreen))
            await _until(pilot, lambda: "ai2050cluster" in app.screen.rows)
            await _until(
                pilot, lambda: app.screen.status["ai2050cluster"] == "unreachable"
            )
            await pilot.press(
                "enter"
            )  # nothing remembered yet: "this machine" is first
            await _until(pilot, lambda: app.snapshot.driver_alive)
            assert app._host_key == "local"
            assert app.runner.location == "local"

    asyncio.run(scenario())
    assert load_last_host() == "local"


def test_switching_host_forgets_the_old_hosts_counts(environment):
    """The roadmap's M3 bug: counts are keyed by job, which says nothing of the host."""
    scheduler = environment
    first = _local_runner(scheduler)
    second = _local_runner(scheduler)

    async def scenario() -> None:
        app = SchedulerTUI(first, refresh_interval=60, live_interval=0)
        async with app.run_test() as pilot:
            await _until(pilot, lambda: app.snapshot.runtime)
            app._count_cache = {"eos_x|ersilia|testlib": {"done": 7, "total": 9}}

            app._connect(Resolution(second, host="other"))
            assert app._count_cache == {}
            assert app.runner is second

            # A recount from the old host that was still in flight lands late.
            stale = Snapshot(
                jobs=[
                    Job(
                        pos=1,
                        model="eos_x",
                        mode="ersilia",
                        library="testlib",
                        done=7,
                        total=9,
                        done_is_live=True,
                    )
                ],
                counts_are_live=True,
            )
            app._on_snapshot(stale, first)
            assert app.snapshot is not stale
            assert app._count_cache == {}

    asyncio.run(scenario())


def test_probe_reports_a_running_driver(running_scheduler, monkeypatch):
    status = probe_host(None)
    mine = [d for d in status.drivers if d.log_dir == str(running_scheduler.log_dir)]
    assert status.state == "running" and len(mine) == 1
    assert "RUNNING" in status.label


def test_probe_reports_an_unreachable_host():
    status = probe_host("no-such-host.invalid")
    assert status.state == "unreachable"
    assert status.label == "unreachable"


def test_last_host_round_trip_survives_a_broken_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert load_last_host() is None
    save_last_host("ai2050cluster")
    assert load_last_host() == "ai2050cluster"
    save_last_host(None)
    assert load_last_host() == "local"

    path = tmp_path / "model-launcher" / "last-host"
    path.unlink()
    path.mkdir()  # unreadable as a file
    assert load_last_host() is None
    save_last_host("x")  # must not raise
