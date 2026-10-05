"""Harness for exercising the bash scheduler with no cluster and no AWS.

The scheduler ships its own test seams — ``SCHED_FAKE_S3`` swaps S3 counting for
a text fixture, ``--dry-run`` replaces the wave orchestrator with a real
``sleep`` so the dispatch/poll/cancel path still runs for real. This wraps them
in a fixture so the invariants in ``HANDOFF.md`` can be asserted from pytest
rather than re-verified by hand on the cluster.

Stub ``aws``/``sbatch``/``squeue``/``scancel`` go on ``PATH`` and record every
call, which is how "the client never calls AWS" becomes a test rather than a
convention.
"""

from __future__ import annotations

import itertools
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from model_launcher.core.model import Snapshot, parse_dump
from model_launcher.core.remote import remote_dir


def pytest_collection_modifyitems(config, items):
    """Skip ``linux_only`` tests off Linux.

    The scheduler itself runs on Linux and macOS alike. What stays Linux-only is
    what only Linux has, such as systemd.
    """
    if sys.platform.startswith("linux"):
        return
    skip = pytest.mark.skip(reason="needs Linux")
    for item in items:
        if "linux_only" in item.keywords:
            item.add_marker(skip)


#: Stubs that record their argv, so a test can assert what the scheduler invoked.
STUB_BINARIES = ("aws", "sbatch", "squeue", "scancel", "ersilia")

STUB_TEMPLATE = """\
#!/bin/bash
printf '%s\\n' "$* " >> "$STUB_CALL_LOG.{name}"
exit 0
"""

#: `aws` has to answer plausibly, not just record: `list_libraries` refuses to
#: cache an empty result (a transient AWS failure must not blank the dropdown for
#: an hour), so a stub that printed nothing would re-call on every dump and the
#: warm-cache half of invariant 11 could never be observed.
#:
#: Like the real CLI, a prefix with nothing under it exits 1 with a silent stderr;
#: ``AWS_STUB_FAIL=<message>`` makes every call fail the way missing credentials
#: do, with the message on stderr.
AWS_STUB = """\
#!/bin/bash
printf '%s\\n' "$* " >> "$STUB_CALL_LOG.aws"
if [ -n "${AWS_STUB_FAIL:-}" ]; then echo "$AWS_STUB_FAIL" >&2; exit 255; fi
url="${@: -1}"
case "$url" in
    */input/)
        printf '%27s%s\\n' "PRE " "testlib/"
        ;;
    */input/testlib/)
        for i in 1 2 3; do
            printf '2026-01-01 00:00:00        100 testlib_chunk_00000%s.csv\\n' "$i"
        done
        ;;
    *)
        exit 1
        ;;
esac
exit 0
"""


#: A fake `ersilia` CLI for DISPATCH=serve. It records every call, and `run`
#: writes one `key,input,value` row per input row, like the real one. A run on a
#: chunk named in the file $FAKE_ERSILIA_FAIL_FILE exits 1; FAKE_ERSILIA_SLOW
#: makes each run take that many seconds (a real child, so cancel is exercised).
ERSILIA_STUB = """\
#!/bin/bash
printf '%s\\n' "$* " >> "$STUB_CALL_LOG.ersilia"
case "$1" in
    run)
        shift
        while [ "$#" -gt 0 ]; do
            case "$1" in -i) in="$2"; shift ;; -o) out="$2"; shift ;; esac
            shift
        done
        if [ -n "${FAKE_ERSILIA_FAIL_FILE:-}" ] && [ -f "$FAKE_ERSILIA_FAIL_FILE" ] \\
           && grep -qxF "$(basename "$in")" "$FAKE_ERSILIA_FAIL_FILE"; then
            echo "fake ersilia: run failed" >&2; exit 1
        fi
        # Like the real CLI, Ctrl-C (SIGINT) ends a run; SIGTERM may be ignored.
        if [ "${FAKE_ERSILIA_SLOW:-0}" != 0 ]; then
            sleep "$FAKE_ERSILIA_SLOW" & nap=$!
            trap 'kill -KILL "$nap" 2>/dev/null; echo interrupted >&2; exit 130' INT
            wait "$nap"
        fi
        { echo "key,input,value"; tail -n +2 "$in" | sed 's/^/k,/; s/$/,1/'; } > "$out"
        ;;
esac
exit 0
"""


#: Each instance's fake orchestrators sleep for a different number of seconds, so
#: ``pgrep -f "sleep <n>"`` finds exactly that instance's children on any OS —
#: no /proc environment to read, and never another test's (or anyone's) sleep.
_FAKE_DURATIONS = itertools.count(600_001 + os.getpid() % 1000 * 1000)


def _wait_for(predicate, timeout: float = 20.0, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@dataclass
class Scheduler:
    """A throwaway scheduler instance rooted at one temporary ``LOG_DIR``."""

    root: Path
    log_dir: Path
    queue_file: Path
    stub_bin: Path
    call_log: Path
    fake_s3: bool = True
    default_library: str = "testlib"
    fake_duration: str = field(default_factory=lambda: str(next(_FAKE_DURATIONS)))
    _drivers: list[subprocess.Popen] = field(default_factory=list)

    # -- environment ------------------------------------------------------
    def env(self, **overrides: str) -> dict[str, str]:
        """Build the environment every scheduler process runs under."""
        env = dict(os.environ)
        env.update(
            PATH=f"{self.stub_bin}{os.pathsep}{os.environ['PATH']}",
            STUB_CALL_LOG=str(self.call_log),
            LOG_DIR=str(self.log_dir),
            S3_BUCKET="test-bucket",
            CTL_POLL="1",
            IDLE_POLL="1",
            REFRESH_SECONDS="1",
            SCHED_FAKE_S3="1" if self.fake_s3 else "0",
            SCHED_FAKE_S3_FILE=str(self.log_dir / "fake-s3.txt"),
            SCHED_FAKE_DURATION=self.fake_duration,
            # Explicit, so a developer's ~/.config/model-launcher/scheduler.conf
            # can never leak into a test. Tests that want a conf write this file.
            SCHEDULER_CONF=str(self.root / "scheduler.conf"),
            # Same reason: drivers started by tests register here, never in
            # the developer's ~/.config/model-launcher/drivers.
            SCHED_REGISTRY_DIR=str(self.root / "drivers"),
        )
        env.update(overrides)
        return env

    # -- fixtures on disk -------------------------------------------------
    def write_queue(self, text: str) -> None:
        """Replace the queue file with ``text`` (dedented, trailing newline added)."""
        self.queue_file.write_text(text.strip("\n") + "\n")

    def write_fake_s3(self, text: str) -> None:
        """Write the fake-S3 fixture: ``input <lib> <n>`` / ``output <model> <lib> <mode> <n>``."""
        (self.log_dir / "fake-s3.txt").write_text(text.strip("\n") + "\n")

    def write_status(self, rows: list[str]) -> None:
        """Write ``status.tsv`` directly, to seed a pre-existing verdict."""
        header = "#key\tstatus\tdone\ttotal\tstarted\tfinished\tlog\tnote"
        (self.log_dir / "status.tsv").write_text("\n".join([header, *rows]) + "\n")

    def read_queue(self) -> str:
        """Return the current queue-file contents."""
        return self.queue_file.read_text()

    def stub_calls(self, name: str) -> list[str]:
        """Return every recorded invocation of stub ``name``."""
        path = Path(f"{self.call_log}.{name}")
        if not path.exists():
            return []
        return [line for line in path.read_text().splitlines() if line.strip()]

    def forget_stub_calls(self, name: str) -> None:
        """Drop the recorded history for stub ``name``."""
        Path(f"{self.call_log}.{name}").unlink(missing_ok=True)

    # -- invoking ---------------------------------------------------------
    def ctl(
        self, *args: str, check: bool = False, **env: str
    ) -> subprocess.CompletedProcess:
        """Run ``sched-ctl.sh`` against this instance."""
        argv = [
            "bash",
            str(remote_dir() / "sched-ctl.sh"),
            "--log-dir",
            str(self.log_dir),
            "-q",
            str(self.queue_file),
            *args,
        ]
        proc = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            text=True,
            env=self.env(**env),
            cwd=self.root,
            timeout=60,
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"ctl {' '.join(args)} failed rc={proc.returncode}\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        return proc

    def dump(self, *args: str) -> Snapshot:
        """Return a parsed snapshot — the same call and parser the TUI uses."""
        proc = self.ctl("dump", *args)
        return parse_dump(proc.stdout)

    def start_driver(
        self, *extra: str, dry_run: bool = True, **env: str
    ) -> subprocess.Popen:
        """Start the driver (in dry-run unless told otherwise) and return the process.

        Combined stdout/stderr goes to ``driver.log`` in ``log_dir``, matching
        the ``tee -a $LOG_DIR/driver.log`` a real deployment uses — a test can
        grep it with :meth:`driver_log` rather than draining a live pipe
        (which would deadlock the driver once it wrote past the OS pipe
        buffer, since nothing was ever reading the other end).
        """
        argv = [
            "bash",
            str(remote_dir() / "run-model-queue.sh"),
            str(self.queue_file),
            self.default_library,
            "1000",
            "cpu-queue",
            *(["--dry-run"] if dry_run else []),
            *extra,
        ]
        # Pin the working directory: anything the scheduler writes to a relative
        # path lands in the test's own sandbox rather than in the repo.
        with open(self.log_dir / "driver.log", "a") as log_file:
            proc = subprocess.Popen(
                argv,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=self.env(**env),
                cwd=self.root,
            )
        self._drivers.append(proc)
        return proc

    def driver_log(self) -> str:
        """Return the driver's combined stdout/stderr so far."""
        path = self.log_dir / "driver.log"
        return path.read_text() if path.exists() else ""

    # -- waiting ----------------------------------------------------------
    def wait_for_status(self, model: str, status: str, timeout: float = 20.0) -> None:
        """Block until ``model`` reaches ``status`` in the snapshot, or fail."""

        def reached() -> bool:
            job = self.dump().find(model)
            return job is not None and job.status == status

        if not reached() and not _wait_for(reached, timeout):
            job = self.dump().find(model)
            actual = job.status if job else "<absent>"
            raise AssertionError(
                f"{model} did not reach {status!r} within {timeout}s (now {actual!r})"
            )

    def wait_for_driver_info(self, timeout: float = 20.0) -> None:
        """Block until the driver has published ``driver.info``."""
        info = self.log_dir / "driver.info"
        if not _wait_for(info.exists, timeout):
            raise AssertionError("driver never wrote driver.info")

    def sleep_children(self) -> list[int]:
        """PIDs of the fake orchestrators (``sleep <fake_duration>``) this instance started."""
        proc = subprocess.run(
            ["pgrep", "-f", f"sleep {self.fake_duration}"],
            capture_output=True,
            check=False,
            text=True,
        )
        return [int(p) for p in proc.stdout.split()]

    # -- teardown ---------------------------------------------------------
    def stop_all(self) -> None:
        """Terminate every driver started here, and its fake orchestrator."""
        for proc in self._drivers:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)


@pytest.fixture
def scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scheduler:
    """A scheduler instance in a temporary directory, with recording stubs."""
    if shutil.which("bash") is None:  # pragma: no cover - environment guard
        pytest.skip("bash is required to exercise the scheduler")
    # Discovery run from the test process itself (probe_host, discover_drivers
    # through a LocalRunner) must read the registry this instance's drivers
    # write. Linux would still find them through /proc; macOS only has this.
    monkeypatch.setenv("SCHED_REGISTRY_DIR", str(tmp_path / "drivers"))
    # Likewise ctl run from the test process must never read this machine's own
    # ~/.config/model-launcher/scheduler.conf.
    monkeypatch.setenv("SCHEDULER_CONF", str(tmp_path / "scheduler.conf"))

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for name in STUB_BINARIES:
        stub = stub_bin / name
        special = {"aws": AWS_STUB, "ersilia": ERSILIA_STUB}
        stub.write_text(special.get(name) or STUB_TEMPLATE.format(name=name))
        stub.chmod(0o755)

    instance = Scheduler(
        root=tmp_path,
        log_dir=log_dir,
        queue_file=tmp_path / "models.queue",
        stub_bin=stub_bin,
        call_log=tmp_path / "stub-calls",
    )
    instance.write_queue("# test queue\n")
    instance.write_fake_s3("input testlib 100")
    try:
        yield instance
    finally:
        instance.stop_all()


@pytest.fixture
def running_scheduler(scheduler: Scheduler) -> Scheduler:
    """A scheduler whose driver is up and has published ``driver.info``."""
    scheduler.start_driver()
    scheduler.wait_for_driver_info()
    return scheduler


def bash_eval(
    snippet: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Source ``scheduler-lib.sh`` and run ``snippet`` against it.

    Lets a test assert on the library's own helpers — the parsing and locking
    primitives that the driver, the control CLI and the renderer all share.
    """
    script = f'source "{remote_dir() / "scheduler-lib.sh"}"\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=60,
    )
