"""``scheduler.conf`` and the M1 fixes that came with it.

Covers: config precedence, the packaged ``slurm/`` layout resolving correctly,
``driver_pid_scan`` being scoped per ``LOG_DIR``, and the newly-published
``sif_dir``/``dispatch`` runtime fields.
"""

from __future__ import annotations

import subprocess
import time

from model_launcher.core.model import parse_dump
from model_launcher.core.remote import remote_dir


def _write_conf(scheduler, text: str) -> None:
    (scheduler.root / "scheduler.conf").write_text(text.strip("\n") + "\n")


def _dump_without_env(scheduler, unset=(), **overrides):
    """Run `sched-ctl.sh dump` with the given env vars removed before overrides
    are applied — for exercising precedence below whatever the test harness's
    own `Scheduler.env()` normally sets."""
    env = scheduler.env(**overrides)
    for key in unset:
        env.pop(key, None)
    argv = [
        "bash",
        str(remote_dir() / "sched-ctl.sh"),
        "--log-dir",
        str(scheduler.log_dir),
        "-q",
        str(scheduler.queue_file),
        "dump",
    ]
    proc = subprocess.run(
        argv, capture_output=True, text=True, env=env, cwd=scheduler.root, timeout=30
    )
    return parse_dump(proc.stdout)


# --- precedence: CLI flag > environment > scheduler.conf > built-in default -


def test_conf_default_is_used_when_nothing_else_is_set(scheduler):
    """With no flag, no conf, and only the harness's own env override, that
    env value stands — precedence below it is exercised by the tests below."""
    scheduler.ctl("add", "eos_x", "ersilia", "testlib", check=True)
    snap = scheduler.dump()
    assert snap.runtime["s3_bucket"] == "test-bucket"  # set by Scheduler.env()


def test_conf_overrides_the_built_in_default(scheduler):
    """A value only set in scheduler.conf beats the hardcoded fallback.

    Env is unset here (rather than the harness's usual "test-bucket") so this
    exercises conf-vs-built-in-default specifically.
    """
    _write_conf(scheduler, 'S3_BUCKET="${S3_BUCKET:-conf-bucket}"')
    snap = _dump_without_env(
        scheduler,
        unset=("S3_BUCKET",),
        SCHEDULER_CONF=str(scheduler.root / "scheduler.conf"),
    )
    assert snap.runtime["s3_bucket"] == "conf-bucket"


def test_env_beats_conf(scheduler):
    """An explicit environment variable wins over scheduler.conf."""
    _write_conf(scheduler, 'S3_BUCKET="${S3_BUCKET:-conf-bucket}"')
    snap = _dump_without_env(
        scheduler,
        SCHEDULER_CONF=str(scheduler.root / "scheduler.conf"),
        S3_BUCKET="env-bucket",
    )
    assert snap.runtime["s3_bucket"] == "env-bucket"


def test_cli_flag_beats_everything(scheduler):
    """--log-dir wins over scheduler.conf's LOG_DIR, over env, over default."""
    other_dir = scheduler.root / "other-logs"
    other_dir.mkdir()
    _write_conf(scheduler, f'LOG_DIR="${{LOG_DIR:-{scheduler.root / "conf-logs"}}}"')

    argv = [
        "bash",
        str(remote_dir() / "sched-ctl.sh"),
        "--log-dir",
        str(other_dir),
        "-q",
        str(scheduler.queue_file),
        "dump",
    ]
    env = scheduler.env(SCHEDULER_CONF=str(scheduler.root / "scheduler.conf"))
    proc = subprocess.run(
        argv, capture_output=True, text=True, env=env, cwd=scheduler.root, timeout=30
    )
    snap = parse_dump(proc.stdout)
    assert snap.runtime["log_dir"] == str(other_dir)


def test_a_plain_assignment_in_conf_would_be_wrong_and_this_conf_is_not_one():
    """scheduler.conf.example must be a defaults file, never a plain assignment.

    A plain ``VAR=value`` there would silently win over ``--log-dir`` and the
    ``SCHED_FAKE_S3``/``LOG_DIR=/tmp/...`` test harness. Every uncommented
    line must guard itself with the variable's own name, e.g. ``VAR="${VAR:-x}"``.
    """
    example = (remote_dir() / "scheduler.conf.example").read_text()
    for line in example.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        assert f"${{{name}:-" in value, f"not a defaults-guarded line: {line!r}"


# --- WAVES_DIR resolves the packaged slurm/ layout --------------------------


def test_dispatch_resolves_the_packaged_slurm_directory(scheduler):
    """The orchestrator path in the dispatch log must point at the packaged
    slurm/ directory, not fall through to the old one-directory-up layout."""
    scheduler.write_queue("eos_x ersilia testlib")
    scheduler.start_driver()
    scheduler.wait_for_status("eos_x", "running")

    log = scheduler.driver_log()
    assert "slurm/submit-ersilia-waves.sh" in log, log


def test_waves_dir_is_conf_overridable(scheduler, tmp_path):
    """A nonstandard layout can point WAVES_DIR elsewhere via scheduler.conf."""
    custom = tmp_path / "custom-waves"
    custom.mkdir()
    (custom / "submit-ersilia-waves.sh").write_text("#!/bin/bash\nexit 0\n")
    (custom / "submit-ersilia-waves.sh").chmod(0o755)
    _write_conf(scheduler, f'WAVES_DIR="${{WAVES_DIR:-{custom}}}"')

    scheduler.write_queue("eos_x ersilia testlib")
    scheduler.start_driver(SCHEDULER_CONF=str(scheduler.root / "scheduler.conf"))
    scheduler.wait_for_status("eos_x", "running")

    assert str(custom) in scheduler.driver_log()


# --- driver_pid_scan is scoped per LOG_DIR -----------------------------------


def test_driver_pid_scan_does_not_cross_log_dirs(scheduler, tmp_path):
    """A driver against a different LOG_DIR must not be mistaken for ours.

    Everyone on the cluster shares one unix account, so scoping only by uid
    would make a colleague's throwaway test instance look like our production
    driver going "legacy" (alive, but no driver.info) the moment it starts.
    """
    other_log_dir = tmp_path / "someone-elses-instance"
    other_log_dir.mkdir()
    other_queue = tmp_path / "someone-elses.queue"
    other_queue.write_text("# a different scheduler entirely\n")

    other_env = scheduler.env(LOG_DIR=str(other_log_dir))
    other_proc = subprocess.Popen(
        [
            "bash",
            str(remote_dir() / "run-model-queue.sh"),
            str(other_queue),
            "testlib",
            "1000",
            "cpu-queue",
            "--dry-run",
        ],
        env=other_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        cwd=scheduler.root,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not (other_log_dir / "driver.info").exists():
            time.sleep(0.1)
        assert (other_log_dir / "driver.info").exists(), (
            "the other driver never started"
        )

        # Our own LOG_DIR has no driver at all — it must read as stopped, not
        # as a legacy driver borrowed from someone else's instance.
        snap = scheduler.dump()
        assert snap.driver_alive is False
        assert snap.driver_legacy is False
    finally:
        other_proc.terminate()
        other_proc.wait(timeout=30)


# --- newly-published runtime fields ------------------------------------------


def test_dispatch_defaults_to_slurm(scheduler):
    assert scheduler.dump().dispatch == "slurm"


def test_dispatch_is_conf_overridable(scheduler):
    _write_conf(scheduler, 'DISPATCH="${DISPATCH:-serve}"')
    proc = scheduler.ctl("dump", SCHEDULER_CONF=str(scheduler.root / "scheduler.conf"))

    assert parse_dump(proc.stdout).dispatch == "serve"


def test_sif_dir_is_published(scheduler):
    assert scheduler.dump().sif_dir == "/shared/sif-files"


def test_sif_dir_is_conf_overridable(scheduler):
    _write_conf(scheduler, 'SIF_DIR="${SIF_DIR:-/opt/sif}"')
    proc = scheduler.ctl("dump", SCHEDULER_CONF=str(scheduler.root / "scheduler.conf"))

    assert parse_dump(proc.stdout).sif_dir == "/opt/sif"


# --- library-aliases.sh is now vendored --------------------------------------


def test_library_aliases_is_packaged():
    aliases = remote_dir() / "library-aliases.sh"
    assert aliases.is_file()
    assert "resolve_library" in aliases.read_text()


def test_packaged_library_aliases_resolves_known_names(scheduler):
    """The packaged copy is picked up ahead of any /shared fallback."""
    scheduler.write_queue("eos_x ersilia molport")
    job = scheduler.dump().find("eos_x")
    assert job.library == "Molport_Screening_Compounds_5.3M"
