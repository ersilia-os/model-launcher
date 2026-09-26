"""Smoke tests for the dashboard, its host picker and host switching.

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
from model_launcher.tui import draw
from model_launcher.tui import hosts as hosts_mod
from model_launcher.tui.app import SchedulerTUI
from model_launcher.tui.hosts import HostScreen
from model_launcher.tui.theme import tokens
from model_launcher.tui.widgets import KeyFooter, LogDrawer, QueueView, StatusSummary


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


def test_percent_never_reads_finished_or_unstarted_while_partway():
    assert draw.pct(13638, 13639) == "99%"
    assert draw.pct(1, 13639) == "1%"
    assert draw.pct(13639, 13639) == "100%"
    assert draw.pct(0, 0) == "—"
    assert draw.pct_fine(8412, 13639) == "61.6%"
    assert draw.columns(120).percent == 114  # the spec's grid, exactly


@pytest.mark.linux_only
def test_dashboard_selects_the_running_job_and_filters(environment):
    scheduler = environment
    scheduler.write_queue("eos_run ersilia testlib\neos_wait ersilia testlib\n")
    scheduler.start_driver()
    scheduler.wait_for_status("eos_run", "running")

    async def scenario() -> None:
        app = SchedulerTUI(
            _local_runner(scheduler), refresh_interval=0.2, live_interval=0
        )
        async with app.run_test(size=(120, 34)) as pilot:
            table = app.query_one("#table", QueueView)
            await _until(pilot, lambda: len(app.snapshot.jobs) == 2)
            await pilot.pause(0.1)
            assert table.selected_key == "eos_run|ersilia|testlib"

            app.query_one("#summary", StatusSummary).post_message(
                StatusSummary.Toggled("pending")
            )
            await _until(pilot, lambda: app.filter_status == "pending")
            assert [j.model for j in app.visible_jobs()] == ["eos_wait"]

            drawer = app.query_one("#log", LogDrawer)
            await pilot.press("l")
            assert drawer.display
            await pilot.press("escape")
            assert not drawer.display

            # The footer hints are buttons too: click "l log", then "f".
            footer = app.query_one("#footer", KeyFooter)
            log_x = next(s for _, s, _, k in footer.hints if k == "l")
            await pilot.click("#footer", offset=(log_x, 0))
            await _until(pilot, lambda: drawer.display)
            await pilot.pause(0.1)
            follow_x = next(s for _, s, _, k in drawer.hints if k == "f")
            await pilot.click("#log", offset=(follow_x, 0))
            assert app.follow_log is False

    asyncio.run(scenario())


def test_every_drawn_key_hint_runs_an_action():
    t = tokens(True)
    _, spans = draw.footer(120, t, draw.DASHBOARD_KEYS)
    for snap in (
        Snapshot(),
        Snapshot(jobs=[Job(1, "m", "ersilia", "lib", status="running")]),
    ):
        spans += draw.running_card(120, t, snap)[1]
    spans += draw.log_drawer(120, 14, t, "m", True, [])[1]
    keys = {key for _, _, key in spans}
    assert {"K", "J", "^o", "f"} <= keys  # K/J split in two
    assert keys <= set(SchedulerTUI.HINT_ACTIONS)


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


@pytest.mark.linux_only
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
