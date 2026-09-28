"""Smoke coverage for every `sched-ctl.sh` command.

Per the project guide, this is deliberately smoke-level — one representative
call per command, not exhaustive edge cases (those live in
``test_invariants.py`` and ``test_config.py`` where a specific behaviour is
being pinned). The point is that "test all commands" is a checked fact rather
than a TODO: before this file, 8 of 18 commands had zero coverage.
"""

from __future__ import annotations

import re

from model_launcher.core.remote import remote_dir


def _add3(scheduler):
    scheduler.ctl("add", "eos_a", "ersilia", "testlib", check=True)
    scheduler.ctl("add", "eos_b", "ersilia", "testlib", check=True)
    scheduler.ctl("add", "eos_c", "ersilia", "testlib", check=True)


def test_rm_removes_a_job(scheduler):
    _add3(scheduler)
    proc = scheduler.ctl("rm", "eos_b", check=True)
    assert "removed" in proc.stdout.lower()
    assert [j.model for j in scheduler.dump().jobs] == ["eos_a", "eos_c"]


def test_rm_unknown_selector_fails_clearly(scheduler):
    scheduler.write_queue("eos_a ersilia testlib")
    proc = scheduler.ctl("rm", "eos_nonexistent")
    assert proc.returncode != 0
    assert "no queue entry" in (proc.stderr + proc.stdout).lower()


def test_a_position_picks_that_row_when_a_model_repeats(scheduler):
    """The same model on two libraries: position 2 must act on row 2.

    apply_sel once re-resolved each selector through its model id, so `rm 2`
    removed row 1 — the TUI's selected job stayed and another one vanished.
    """
    scheduler.write_queue(
        "eos_a ersilia testlib\neos_a ersilia otherlib\neos_c ersilia testlib\n"
    )
    scheduler.ctl("hold", "2", check=True)
    held = {j.library: j.hold for j in scheduler.dump().jobs if j.model == "eos_a"}
    assert held == {"testlib": False, "otherlib": True}
    scheduler.ctl("rm", "2", check=True)
    assert [j.key for j in scheduler.dump().jobs] == [
        "eos_a|ersilia|testlib",
        "eos_c|ersilia|testlib",
    ]


def test_rm_keeps_the_other_rows_fields_in_step(scheduler):
    """Rows keep their own wave, partition and flags after a removal, blank
    fields included — the arrays mut_rm rebuilds must not fall out of step."""
    scheduler.write_queue(
        "eos_a ersilia testlib\n"
        "eos_b ersilia testlib 500 gpu-queue hold\n"
        "eos_c ersilia testlib cpus=4\n"
    )
    scheduler.ctl("rm", "eos_a", check=True)
    rows = [(j.model, j.wave, j.queue, j.hold, j.cpus) for j in scheduler.dump().jobs]
    assert rows == [
        ("eos_b", "500", "gpu-queue", True, ""),
        ("eos_c", "", "", False, "4"),
    ]
    scheduler.ctl("rm", "eos_b", check=True)
    scheduler.ctl("rm", "eos_c", check=True)  # down to an empty queue
    assert scheduler.dump().jobs == []


def test_no_array_copy_uses_the_bash42_unsafe_idiom():
    """bash 4.2 (the head node) drops EMPTY elements from
    ``"${arr[@]+"${arr[@]}"}"`` (fully quoted). It made `rm` die after printing
    "removed:", with the queue untouched. The suite runs a newer bash, where the
    idiom works, so guard the source. The unquoted-outer form
    ``${arr[@]+"${arr[@]}"}`` is fine and stays allowed.
    """
    offenders = []
    for path in sorted(remote_dir().rglob("*.sh")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if not line.lstrip().startswith("#") and re.search(
                r'"\$\{\w+\[@\]\+"', line
            ):
                offenders.append(f"{path.name}:{n}")
    assert not offenders, offenders


def test_up_moves_a_job_one_position_earlier(scheduler):
    _add3(scheduler)
    scheduler.ctl("up", "eos_c", check=True)
    assert [j.model for j in scheduler.dump().jobs] == ["eos_a", "eos_c", "eos_b"]


def test_up_at_the_front_is_a_no_op(scheduler):
    _add3(scheduler)
    scheduler.ctl("up", "eos_a", check=True)
    assert [j.model for j in scheduler.dump().jobs] == ["eos_a", "eos_b", "eos_c"]


def test_down_moves_a_job_one_position_later(scheduler):
    _add3(scheduler)
    scheduler.ctl("down", "eos_a", check=True)
    assert [j.model for j in scheduler.dump().jobs] == ["eos_b", "eos_a", "eos_c"]


def test_down_at_the_back_is_a_no_op(scheduler):
    _add3(scheduler)
    scheduler.ctl("down", "eos_c", check=True)
    assert [j.model for j in scheduler.dump().jobs] == ["eos_a", "eos_b", "eos_c"]


def test_move_to_an_absolute_position(scheduler):
    _add3(scheduler)
    scheduler.ctl("move", "eos_c", "1", check=True)
    assert [j.model for j in scheduler.dump().jobs] == ["eos_c", "eos_a", "eos_b"]


def test_move_rejects_a_non_numeric_position(scheduler):
    scheduler.write_queue("eos_a ersilia testlib")
    proc = scheduler.ctl("move", "eos_a", "first")
    assert proc.returncode != 0
    assert "1-based number" in (proc.stderr + proc.stdout)


def test_unhold_clears_a_held_job(scheduler):
    scheduler.write_queue("eos_a ersilia testlib hold")
    assert scheduler.dump().find("eos_a").status == "held"

    proc = scheduler.ctl("unhold", "eos_a", check=True)
    assert "unheld" in proc.stdout.lower()
    assert scheduler.dump().find("eos_a").status == "pending"


def test_stop_after_current_needs_a_live_driver(scheduler):
    """Posting a control message with no driver to consume it would ambush
    the next one that starts — ctl refuses it outright."""
    proc = scheduler.ctl("stop-after-current")
    assert proc.returncode != 0


def test_stop_after_current_arms_the_flag(running_scheduler):
    scheduler = running_scheduler
    proc = scheduler.ctl("stop-after-current", check=True)
    assert "armed" in proc.stdout.lower()
    assert scheduler.dump().stop_after_current is True


def test_shutdown_needs_a_live_driver(scheduler):
    proc = scheduler.ctl("shutdown")
    assert proc.returncode != 0


def test_shutdown_stops_the_driver(scheduler):
    scheduler.write_queue("# empty\n")
    driver = scheduler.start_driver()
    scheduler.wait_for_driver_info()

    proc = scheduler.ctl("shutdown", check=True)
    assert "shutdown requested" in proc.stdout.lower()
    driver.wait(timeout=30)
    assert scheduler.dump().driver_alive is False


def test_refresh_needs_a_live_driver(scheduler):
    proc = scheduler.ctl("refresh")
    assert proc.returncode != 0


def test_refresh_is_accepted_by_a_live_driver(running_scheduler):
    proc = running_scheduler.ctl("refresh", check=True)
    assert "recount" in proc.stdout.lower()
