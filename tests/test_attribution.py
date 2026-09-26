"""Attribution: the audit log and "cancelled by X" status notes.

Everyone on the cluster shares one unix account, so `id -un` identifies no
one. WHO comes from outside instead — normally the Python client, reading its
own operator's local username — and is threaded through two independent
mechanisms: an append-only audit log for every mutating command, and a
`who` field carried on cancel control messages so the eventual status note
can say who asked for it.
"""

from __future__ import annotations

import subprocess

import pytest

from model_launcher.core.remote import remote_dir
from model_launcher.core.runner import LocalRunner, SshRunner, build_runner


def _audit_lines(scheduler):
    path = scheduler.log_dir / "audit.log"
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line.strip()]


# --- the audit log -----------------------------------------------------------


def test_mutating_commands_are_audited(scheduler):
    scheduler.ctl("add", "eos_x", "ersilia", "testlib", SCHED_WHO="ana", check=True)
    lines = _audit_lines(scheduler)
    assert len(lines) == 1
    _ts, who, verb, args = lines[0].split("\t")
    assert who == "ana"
    assert verb == "add"
    assert args == "eos_x ersilia testlib"


def test_read_only_commands_are_not_audited(scheduler):
    scheduler.write_queue("eos_x ersilia testlib")
    scheduler.ctl("list", SCHED_WHO="ana")
    scheduler.ctl("status", SCHED_WHO="ana")
    scheduler.ctl("dump", SCHED_WHO="ana")
    scheduler.ctl("queue-file", SCHED_WHO="ana")
    assert _audit_lines(scheduler) == []


def test_a_failed_mutation_is_still_recorded(scheduler):
    """A rejected attempt ('cpus out of range') is still something someone
    did, and may be exactly what an operator is trying to find later."""
    proc = scheduler.ctl("add", "eos_x", "ersilia", "", "500", SCHED_WHO="ana")
    assert proc.returncode != 0
    lines = _audit_lines(scheduler)
    assert len(lines) == 1
    assert "ana" in lines[0]
    assert "\tadd\t" in lines[0]


def test_who_falls_back_to_unknown_rather_than_the_shared_account_name(scheduler):
    """With no --who and no $SCHED_WHO, the audit log says "unknown" rather
    than `id -un` — on the shared account that would say the same thing for
    every single person, which reads as an answer without being one."""
    scheduler.ctl("pause", check=True)
    lines = _audit_lines(scheduler)
    assert len(lines) == 1
    _, who, verb, _ = lines[0].split("\t")
    assert who == "unknown"
    assert verb == "pause"


def test_sched_who_env_var_is_honoured(scheduler):
    scheduler.ctl("resume", SCHED_WHO="ana", check=True)
    lines = _audit_lines(scheduler)
    assert lines[0].split("\t")[1] == "ana"


def test_cli_who_flag_beats_sched_who_env(scheduler):
    proc = subprocess.run(
        [
            "bash",
            str(remote_dir() / "sched-ctl.sh"),
            "--log-dir",
            str(scheduler.log_dir),
            "-q",
            str(scheduler.queue_file),
            "--who",
            "flag-wins",
            "pause",
        ],
        capture_output=True,
        check=False,
        text=True,
        env=scheduler.env(SCHED_WHO="env-loses"),
        cwd=scheduler.root,
        timeout=30,
    )
    assert proc.returncode == 0
    assert _audit_lines(scheduler)[0].split("\t")[1] == "flag-wins"


def test_multiple_mutations_append_rather_than_overwrite(scheduler):
    scheduler.ctl("add", "eos_a", "ersilia", "testlib", SCHED_WHO="ana", check=True)
    scheduler.ctl("add", "eos_b", "ersilia", "testlib", SCHED_WHO="marina", check=True)
    scheduler.ctl("hold", "eos_a", SCHED_WHO="ana", check=True)
    lines = _audit_lines(scheduler)
    assert len(lines) == 3
    verbs = [line.split("\t")[2] for line in lines]
    assert verbs == ["add", "add", "hold"]


# --- "cancelled by X" ---------------------------------------------------------


@pytest.mark.linux_only
def test_cancel_note_names_who_asked(scheduler):
    scheduler.write_queue("eos_x ersilia testlib")
    scheduler.start_driver()
    scheduler.wait_for_status("eos_x", "running")

    scheduler.ctl("cancel", "eos_x", SCHED_WHO="ana", check=True)
    scheduler.wait_for_status("eos_x", "cancelled", timeout=40)

    note = scheduler.dump().find("eos_x").note
    assert "ana" in note


@pytest.mark.linux_only
def test_cancel_note_has_no_dangling_by_when_who_is_unknown(scheduler):
    """A hand-typed `sched-ctl.sh cancel` with no --who must not produce a note
    like 'cancelled by  at ...' — the placeholder is omitted, not blank."""
    scheduler.write_queue("eos_x ersilia testlib")
    scheduler.start_driver()
    scheduler.wait_for_status("eos_x", "running")

    proc = subprocess.run(
        [
            "bash",
            str(remote_dir() / "sched-ctl.sh"),
            "--log-dir",
            str(scheduler.log_dir),
            "-q",
            str(scheduler.queue_file),
            "cancel",
            "eos_x",
        ],
        capture_output=True,
        check=False,
        text=True,
        env={**scheduler.env(), "SCHED_WHO": ""},
        cwd=scheduler.root,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    scheduler.wait_for_status("eos_x", "cancelled", timeout=40)

    note = scheduler.dump().find("eos_x").note
    assert "by " not in note, note
    assert note.startswith(("cancelled at ", "cancelled by"))


@pytest.mark.linux_only
def test_audit_log_records_the_cancel_request_too(scheduler):
    """The cancel note names who asked; the audit log independently confirms
    the command was actually run, at what time."""
    scheduler.write_queue("eos_x ersilia testlib")
    scheduler.start_driver()
    scheduler.wait_for_status("eos_x", "running")

    scheduler.ctl("cancel", "eos_x", SCHED_WHO="ana", check=True)
    scheduler.wait_for_status("eos_x", "cancelled", timeout=40)

    lines = _audit_lines(scheduler)
    assert any(line.split("\t")[1:3] == ["ana", "cancel"] for line in lines)


# --- Python side: where `who` comes from -------------------------------------


def test_build_runner_reads_who_from_the_operators_own_machine(monkeypatch):
    monkeypatch.setenv("USER", "ana")
    monkeypatch.delenv("SCHEDULER_WHO", raising=False)
    runner = build_runner(ctl="/bin/true")
    assert runner.who == "ana"


def test_build_runner_honours_explicit_scheduler_who(monkeypatch):
    monkeypatch.setenv("USER", "ana")
    monkeypatch.setenv("SCHEDULER_WHO", "marina")
    runner = build_runner(ctl="/bin/true")
    assert runner.who == "marina"


def test_who_argument_beats_environment(monkeypatch):
    monkeypatch.setenv("SCHEDULER_WHO", "marina")
    runner = build_runner(ctl="/bin/true", who="explicit")
    assert runner.who == "explicit"


def test_who_rides_on_every_ctl_invocation():
    runner = LocalRunner(ctl="/bin/true", who="ana")
    assert "--who" in runner._argv(runner._global_flags())
    assert "ana" in runner._argv(runner._global_flags())


def test_who_rides_over_ssh_too():
    runner = SshRunner(ctl="/bin/true", host="somehost", who="ana")
    argv = runner._argv(runner._global_flags())
    assert any("--who" in part and "ana" in part for part in argv)
