"""The 14 scheduler invariants, as tests.

``HANDOFF.md`` §4 lists fourteen rules, each written after a production bug.
Until now they were prose: a future change could break one and nothing would
notice until a wave was lost on the cluster. Each test below is named for the
invariant it pins, and the docstring says what breaking it costs.

Numbering follows HANDOFF.md §4 exactly. Do not renumber.
"""

from __future__ import annotations

import os
import signal
import time

import pytest

from model_launcher.core.model import is_queue_flag as py_is_queue_flag

from .conftest import bash_eval


# --- 1. the queue file is the source of truth, line order IS priority --------


def test_inv01_line_order_is_priority(scheduler):
    """Reordering the queue file reorders the run order. `top` means "move the line up"."""
    scheduler.ctl("add", "eos_a", "ersilia", "testlib", check=True)
    scheduler.ctl("add", "eos_b", "ersilia", "testlib", check=True)
    scheduler.ctl("add", "eos_c", "ersilia", "testlib", check=True)
    assert [j.model for j in scheduler.dump().jobs] == ["eos_a", "eos_b", "eos_c"]

    scheduler.ctl("top", "eos_c", check=True)
    assert [j.model for j in scheduler.dump().jobs] == ["eos_c", "eos_a", "eos_b"]

    # The file itself moved, not just the view: position is stored, not computed.
    lines = [
        ln
        for ln in scheduler.read_queue().splitlines()
        if ln and not ln.startswith("#")
    ]
    assert lines[0].split()[0] == "eos_c"


def test_inv01_driver_rereads_queue_before_every_job(scheduler):
    """The driver re-reads the queue while idling, so a job added later is picked up.

    A driver that parsed once at startup would ignore every edit — the failure
    that `driver_is_legacy` exists to warn about.
    """
    scheduler.write_queue("# empty\n")
    scheduler.start_driver()
    scheduler.wait_for_driver_info()

    scheduler.ctl("add", "eos_late", "ersilia", "testlib", check=True)
    scheduler.wait_for_status("eos_late", "running")


# --- 2. `hold` outranks only `pending` --------------------------------------


def test_inv02_hold_does_not_mask_a_real_verdict(scheduler):
    """A cancelled-then-held job must read `cancelled`, not `held`.

    Showing "held" would hide the very thing the operator just did.
    """
    scheduler.write_queue("eos_x ersilia testlib hold")
    scheduler.write_status(
        ["eos_x|ersilia|testlib\tcancelled\t3\t100\t-\t-\t\tstopped"]
    )

    job = scheduler.dump().find("eos_x")
    assert job.status == "cancelled"
    assert job.hold is True  # still flagged in the queue file


def test_inv02_hold_does_outrank_pending(scheduler):
    """With no stored verdict, a held line reads `held` and is not dispatched."""
    scheduler.write_queue("eos_x ersilia testlib hold")
    assert scheduler.dump().find("eos_x").status == "held"


# --- 3. control messages are drained at startup, not just in the loop -------


def test_inv03_stale_shutdown_is_discarded_at_startup(scheduler):
    """A `shutdown` left over from a previous driver must not kill the next one.

    Without this, a click made hours ago makes a freshly started scheduler exit
    immediately, and the symptom reads as "the scheduler is broken".
    """
    control = scheduler.log_dir / "control"
    control.mkdir(exist_ok=True)
    (control / "1.1.shutdown").write_text("\n")

    scheduler.write_queue("eos_x ersilia testlib")
    driver = scheduler.start_driver()
    scheduler.wait_for_driver_info()

    # It survived, consumed the stale message, and went on to run the job.
    scheduler.wait_for_status("eos_x", "running")
    assert driver.poll() is None
    assert not (control / "1.1.shutdown").exists()


def test_inv03_paused_flag_survives_startup(scheduler):
    """`paused` is deliberately exempt: bringing a driver up idle is a real workflow."""
    control = scheduler.log_dir / "control"
    control.mkdir(exist_ok=True)
    (control / "paused").write_text("")

    scheduler.write_queue("eos_x ersilia testlib")
    scheduler.start_driver()
    scheduler.wait_for_driver_info()

    time.sleep(3)  # long enough that an unpaused driver would have dispatched
    assert (control / "paused").exists()
    assert scheduler.dump().paused is True
    assert scheduler.dump().find("eos_x").status == "pending"


def test_inv03_a_command_posted_the_instant_driver_info_appears_is_never_discarded(
    scheduler,
):
    """``discard_stale_control`` must finish before ``driver.info`` exists.

    ``driver.info`` is the readiness signal an external client polls for. If
    the driver announced itself before running its own stale-message sweep,
    there would be a real window — not just a theoretical one — where a fast
    client sees a ready driver, posts a command, and then has that exact
    command discarded a moment later as if it predated startup. This starts
    the driver and posts the instant ``wait_for_driver_info`` returns, with no
    slack — a race would show up as a straight failure here, not a flake.
    """
    scheduler.write_queue("# empty\n")
    scheduler.start_driver()
    scheduler.wait_for_driver_info()

    scheduler.ctl("stop-after-current", check=True)
    assert scheduler.dump().stop_after_current is True


# --- 4. kill the orchestrator BEFORE scancel --------------------------------


def test_inv04_orchestrator_is_dead_before_scancel_runs(scheduler):
    """Reverse this order and cancelling a wave spawns a fresh one.

    A live orchestrator sees its array vanish from squeue, concludes the wave
    finished, verifies, finds it missing from S3 and fires its own
    "resubmit once" retry.
    """
    # A scancel stub that records how many fake orchestrators were still alive.
    scancel = scheduler.stub_bin / "scancel"
    scancel.write_text(
        "#!/bin/bash\n"
        'n=$(pgrep -c -f "sleep 600" 2>/dev/null || echo 0)\n'
        'printf "%s alive=%s\\n" "$*" "$n" >> "$STUB_CALL_LOG.scancel"\n'
    )
    scancel.chmod(0o755)

    scheduler.write_queue("eos_x ersilia testlib")
    scheduler.start_driver()
    scheduler.wait_for_driver_info()
    scheduler.wait_for_status("eos_x", "running")

    # The driver scrapes array ids from this job's log only.
    log = scheduler.log_dir / "eos_x_testlib.log"
    with log.open("a") as handle:
        handle.write("Submitted array job 424242\n")

    scheduler.ctl("cancel", "eos_x", check=True)
    scheduler.wait_for_status("eos_x", "cancelled", timeout=40)

    calls = scheduler.stub_calls("scancel")
    assert calls, "scancel was never invoked for the in-flight array"
    assert "424242" in calls[0]
    assert "alive=0" in calls[0], (
        f"orchestrator still alive at scancel time: {calls[0]}"
    )


# --- 5. the driver must kill its orchestrator on exit -----------------------


def test_inv05_driver_takes_the_orchestrator_down_with_it(scheduler):
    """An orphaned orchestrator keeps submitting waves.

    The next driver then runs a *second* model concurrently, both fighting for
    nodes and writing into /fsx.
    """
    scheduler.write_queue("eos_x ersilia testlib")
    driver = scheduler.start_driver()
    scheduler.wait_for_driver_info()
    scheduler.wait_for_status("eos_x", "running")

    children = scheduler.sleep_children()
    assert children, "no fake orchestrator was started"

    driver.send_signal(signal.SIGTERM)
    driver.wait(timeout=40)

    for pid in children:
        assert not _pid_alive(pid), f"orchestrator {pid} outlived the driver"
    assert scheduler.dump().find("eos_x").status == "cancelled"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


# --- 6. one lock, and it is not re-entrant by accident ----------------------


def test_inv06_nested_queue_locked_does_not_release_the_lock(scheduler, tmp_path):
    """Nesting must not re-run `exec 9>>`, which would silently release the outer flock.

    The caller would carry on believing it still held it, and two writers would
    interleave on the queue file.
    """
    lock_probe = tmp_path / "probe"
    snippet = f"""
        LOG_DIR={scheduler.log_dir}
        mkdir -p "$LOG_DIR"
        inner() {{
            # While the outer lock is held, an independent flock must fail.
            flock -n 8 2>/dev/null && echo "LOST" > {lock_probe} || echo "HELD" > {lock_probe}
        }} 8>>"$(queue_lock_file)"
        queue_locked queue_locked inner
        echo "depth_after=${{QUEUE_LOCK_DEPTH:-0}}"
    """
    result = bash_eval(snippet)
    assert result.returncode == 0, result.stderr
    assert lock_probe.read_text().strip() == "HELD", "the nested call released the lock"
    assert "depth_after=0" in result.stdout


def test_inv06_nested_queue_locked_runs_the_command_once(scheduler):
    """Re-entrancy is a pass-through, not a second execution."""
    snippet = f"""
        LOG_DIR={scheduler.log_dir}
        mkdir -p "$LOG_DIR"
        n=0
        bump() {{ echo tick; }}
        queue_locked queue_locked bump
    """
    result = bash_eval(snippet)
    assert result.stdout.count("tick") == 1


# --- 7. queue flags are recognised by shape, not position -------------------


def test_inv07_flag_after_library_is_not_a_positional(scheduler):
    """`eos1 ersilia mylib hold` must work — it is the obvious way to write it.

    Positional parsing would put `hold` in the wave_size slot and reject the
    line as "wave_size out of 1..1000".
    """
    scheduler.write_queue("eos_x ersilia testlib hold")
    job = scheduler.dump().find("eos_x")
    assert job.library == "testlib"
    assert job.status == "held"


@pytest.mark.parametrize(
    "token",
    [
        "hold",
        "cpus=8",
        "wave=3",
        "anything=",
        "=value",
        "eos1aaa",
        "1000",
        "cpu-queue",
        "",
    ],
)
def test_inv07_bash_and_python_agree_on_what_a_flag_is(token):
    """`scheduler-lib.sh:is_queue_flag` and `model.py:is_queue_flag` must stay in step.

    They are two implementations of one rule; a disagreement means the dashboard
    shows a different job than the cluster runs.
    """
    result = bash_eval(f"is_queue_flag {token!r} && echo yes || echo no")
    bash_says = result.stdout.strip() == "yes"
    assert bash_says == py_is_queue_flag(token), f"disagreement on {token!r}"


# --- 8. empty positional fields cannot be written ---------------------------


def test_inv08_refuses_to_write_a_blank_middle_field(scheduler):
    """Fields are whitespace-delimited, so a blank middle field is invisible on re-read.

    `model mode <blank> 500` comes back as library=500.
    """
    proc = scheduler.ctl("add", "eos_x", "ersilia", "", "500")
    assert proc.returncode != 0
    assert "library" in (proc.stderr + proc.stdout).lower()
    assert "eos_x" not in scheduler.read_queue()


def test_inv08_fields_left_of_a_given_value_are_materialised(running_scheduler):
    """Given a wave size, the library must be filled in from the driver's default."""
    scheduler = running_scheduler
    scheduler.ctl("add", "eos_x", "ersilia", "", "500", check=True)

    job = scheduler.dump().find("eos_x")
    assert job.library == scheduler.default_library
    assert job.wave == "500"


# --- 9. status.tsv is the authority; state.tsv is only a fallback -----------


def test_inv09_status_store_beats_state_file(scheduler):
    """Falling back per-job would resurrect verdicts that `retry` just cleared."""
    scheduler.write_queue("eos_x ersilia testlib")
    scheduler.write_status(["eos_x|ersilia|testlib\tdone\t100\t100\t-\t-\t\t"])
    (scheduler.log_dir / "state.tsv").write_text(
        "#idx\tmodel\tmode\tlibrary\tstatus\tdone\ttotal\tstarted\tfinished\tlog\n"
        "1\teos_x\tersilia\ttestlib\tpending\t0\t0\t-\t-\t\n"
    )
    assert scheduler.dump().find("eos_x").status == "done"


def test_inv09_retry_clears_the_stored_verdict(scheduler):
    """`retry` drops the key so the driver re-derives progress from S3."""
    scheduler.write_queue("eos_x ersilia testlib")
    scheduler.write_status(["eos_x|ersilia|testlib\tfailed\t3\t100\t-\t-\t\tboom"])
    assert scheduler.dump().find("eos_x").status == "failed"

    scheduler.ctl("retry", "eos_x", check=True)
    assert scheduler.dump().find("eos_x").status == "pending"


def test_inv09_empty_log_column_does_not_swallow_the_note(scheduler):
    """A stored note must not shift left into the log-path column.

    This is not cosmetic. ``merge_status`` assigns ``Q_LOG[i]`` from the stored
    log path, and the driver appends orchestrator output to it — so a shifted
    note would become a *relative* file named after its own text, created in
    whatever directory the driver happens to be running in. The real log is
    lost and the dashboard's "open log" points at nothing.

    ``reclaim_stale_running`` produces exactly this shape: empty log,
    non-empty note. Fixed by having ``status_write`` emit ``-`` for both
    columns when empty — the same placeholder ``started``/``finished`` already
    use — so a genuinely empty field is never adjacent to a non-empty one on
    the wire (bash's ``read`` collapses runs of tab-separated empty fields,
    since tab is IFS *whitespace* even when IFS is set to only a tab).
    ``status_load`` and the Python parser both translate ``-`` back to ``""``
    for these two columns, so nothing downstream ever sees the placeholder.

    This writes the row exactly as the fixed ``status_write`` would, rather
    than the old ambiguous shape (an empty field with no placeholder) — that
    shape is genuinely unparseable in bash regardless of the read side, so the
    guarantee is "writes are never ambiguous", not "any input can be recovered".
    """
    note = "reclaimed at startup"
    scheduler.write_status([f"eos_x|ersilia|testlib\tpending\t0\t100\t-\t-\t-\t{note}"])
    scheduler.write_queue("eos_x ersilia testlib")

    scheduler.start_driver()
    scheduler.wait_for_status("eos_x", "running")

    stray_note = scheduler.root / note
    stray_dash = scheduler.root / "-"
    assert not stray_note.exists(), f"driver wrote the job log to {stray_note}"
    assert not stray_dash.exists(), "the '-' placeholder leaked into the log path"
    assert (scheduler.log_dir / "eos_x_testlib.log").exists()


# --- 10. comment blocks move with their job ---------------------------------


def test_inv10_comments_travel_with_their_job(scheduler):
    """The queue file is header / per-job `pre` / tail. Hand annotations must survive."""
    # The blank line is load-bearing: `_split_header` splits the leading text at
    # the LAST blank line, which is how these files are written by hand.
    scheduler.write_queue(
        "# ===== top-of-file banner =====\n"
        "\n"
        "eos_a ersilia testlib\n"
        "# why eos_b matters\n"
        "eos_b ersilia testlib\n"
    )
    scheduler.ctl("top", "eos_b", check=True)

    lines = [ln.rstrip() for ln in scheduler.read_queue().splitlines() if ln.strip()]
    assert lines[0].startswith("# ===== top-of-file banner"), "banner must stay on top"
    assert lines[1] == "# why eos_b matters", "comment did not move with its job"
    assert lines[2].split()[0] == "eos_b"


# --- 11. the client never calls AWS -----------------------------------------


def test_inv11_plain_dump_never_counts_progress_from_s3(scheduler):
    """The 2s refresh tick must not recount progress, or the dashboard costs money and time.

    Progress is paced in three tiers: cached (plain dump), `--live` (per-library
    totals plus the running row), and `--live-all` on explicit request.

    Note the exact shape of the guarantee. A plain dump makes *no counting*
    calls, but on a cold cache it does make one cheap `s3 ls input/` listing to
    populate the library dropdown — that returns a dozen common prefixes, not
    millions of objects, and is cached for an hour. Once warm, a plain dump
    touches AWS not at all, which is the steady state the 2s tick runs in.
    """
    scheduler.fake_s3 = False  # let the stub `aws` record real call attempts
    scheduler.write_queue("eos_x ersilia testlib")

    scheduler.dump()
    cold = scheduler.stub_calls("aws")
    assert all("/input/" in call for call in cold), (
        f"a plain dump counted progress from S3: {cold}"
    )
    assert not any("/output/" in call for call in cold)

    # Warm cache: the tick the TUI actually runs on must be free.
    scheduler.forget_stub_calls("aws")
    scheduler.dump()
    assert scheduler.stub_calls("aws") == [], "a warm plain dump still called AWS"

    scheduler.dump("--live-all")
    assert any("/output/" in call for call in scheduler.stub_calls("aws")), (
        "--live-all should recount every row from S3"
    )


# --- 12. a recount must not be cancelled by a refresh -----------------------


def test_inv12_dump_worker_is_exclusive():
    """The 2s tick must stand aside while a recount is in flight.

    Otherwise the cheap tick discards the very numbers the user asked for. This
    is enforced by Textual's `exclusive=True` on the dump worker group.
    """
    import inspect

    from model_launcher.tui.app import SchedulerTUI

    bound = inspect.getclosurevars(SchedulerTUI.refresh_snapshot).nonlocals
    assert bound["exclusive"] is True, "refresh_snapshot must stay an exclusive worker"
    assert bound["thread"] is True, "the dump must not block the UI thread"
    assert bound["group"] == "dump"


# --- 13. per-job `cpus` is a queue flag, and empty means "do not pass it" ----


def test_inv13_cpus_survives_a_queue_rewrite(scheduler):
    """`cpus=N` is lifted into its own field but LEFT in the flags string.

    The flags string is what gets written back, so consuming the token would
    silently drop the override on the next rewrite.
    """
    scheduler.write_queue(
        "eos_a ersilia testlib\neos_b ersilia testlib 1000 cpu-queue cpus=8"
    )
    assert scheduler.dump().find("eos_b").cpus == "8"

    scheduler.ctl("top", "eos_b", check=True)
    assert "cpus=8" in scheduler.read_queue()
    assert scheduler.dump().find("eos_b").cpus == "8"


def test_inv13_absent_cpus_stays_absent(scheduler):
    """Empty means "pass nothing", leaving the worker's own #SBATCH in charge.

    Those defaults differ per mode and were tuned by hand, so inventing a
    number here would silently re-pack every existing run.
    """
    scheduler.write_queue("eos_x ersilia testlib")
    assert scheduler.dump().find("eos_x").cpus == ""


def test_inv13_invalid_cpus_is_skipped_not_dispatched(scheduler):
    """A bad value must cost one skipped row, not an sbatch that fails 1000 times.

    The verdict is the driver's: the queue file is hand-editable, so the driver
    re-validates at read time rather than trusting what ctl let through.
    """
    scheduler.write_queue("eos_x ersilia testlib 1000 cpu-queue cpus=999")
    scheduler.start_driver()
    scheduler.wait_for_driver_info()
    scheduler.wait_for_status("eos_x", "skipped")

    # The override is preserved verbatim in the queue file rather than silently
    # dropped, so the operator can see and correct the value they typed.
    assert scheduler.dump().find("eos_x").cpus == "999"
    assert scheduler.stub_calls("sbatch") == []


# --- 14. stale `running` rows get reclaimed at startup ----------------------


def test_inv14_interrupted_job_is_reclaimed(scheduler):
    """A driver that died mid-job leaves a row marked `running`.

    Without reclaiming it the job is neither running nor pending: it is skipped
    forever while the driver idles — exactly the failure this scheduler exists
    to survive. Its finished chunks are already in S3, so re-dispatching
    resumes rather than redoes.
    """
    scheduler.write_queue("eos_x ersilia testlib")
    scheduler.write_status(
        ["eos_x|ersilia|testlib\trunning\t42\t100\t2026-01-01T00:00:00Z\t-\t\t"]
    )

    scheduler.start_driver()
    scheduler.wait_for_driver_info()
    # It comes back as runnable, and is dispatched again rather than stranded.
    scheduler.wait_for_status("eos_x", "running")

    stored = (scheduler.log_dir / "status.tsv").read_text()
    assert "eos_x" in stored


def test_inv14_reclaim_is_announced_in_the_driver_log(scheduler):
    """The operator must be able to see why a job restarted."""
    scheduler.write_queue("# empty\n")
    scheduler.write_status(
        ["eos_gone|ersilia|testlib\trunning\t1\t100\t2026-01-01T00:00:00Z\t-\t\t"]
    )
    driver = scheduler.start_driver()
    scheduler.wait_for_driver_info()

    def reclaimed() -> bool:
        status = (scheduler.log_dir / "status.tsv").read_text()
        return "reclaimed at startup" in status

    deadline = time.time() + 20
    while time.time() < deadline and not reclaimed():
        time.sleep(0.1)
    assert reclaimed(), "stale running row was never reclaimed"
    assert driver.poll() is None


# --- the fake-cluster harness itself ----------------------------------------


def test_dry_run_needs_no_cluster_and_no_credentials(scheduler):
    """`--dry-run` must skip the SIF pre-flight, so the dispatch path stays testable.

    This is what makes every test above possible without /shared or AWS.
    """
    scheduler.write_queue("eos_x ersilia testlib")
    scheduler.start_driver()
    scheduler.wait_for_status("eos_x", "running")

    assert scheduler.stub_calls("sbatch") == []
    assert scheduler.stub_calls("aws") == []


def test_unknown_mode_is_skipped_with_a_reason(scheduler):
    """An unrecognised mode must degrade to a `skipped` verdict, never a dispatch.

    This is the validator a third mode (`ersilia serve`) will have to be
    admitted to; the test pins the current behaviour so that change is visible.

    Known gap, recorded here rather than fixed (M0 vendors the bash unchanged):
    the *reason* for a skip reaches the driver log but no further. ``state.tsv``
    has no note column and skipped rows never enter ``status.tsv``, so the
    dashboard shows "skipped" with no explanation.
    """
    scheduler.write_queue("eos_x wobble testlib")
    scheduler.start_driver()
    scheduler.wait_for_driver_info()
    scheduler.wait_for_status("eos_x", "skipped")

    assert scheduler.stub_calls("sbatch") == []
    assert scheduler.dump().find("eos_x").note == ""  # see docstring


def test_pause_stops_new_jobs_starting(scheduler):
    """`pause` is a sticky flag: the driver finishes nothing new while it is set."""
    scheduler.write_queue("eos_x ersilia testlib")
    scheduler.ctl("pause", check=True)
    scheduler.start_driver()
    scheduler.wait_for_driver_info()

    time.sleep(3)
    assert scheduler.dump().paused is True
    assert scheduler.dump().find("eos_x").status == "pending"

    scheduler.ctl("resume", check=True)
    scheduler.wait_for_status("eos_x", "running")
