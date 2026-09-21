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

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from model_launcher.core.model import Snapshot, parse_dump
from model_launcher.core.remote import remote_dir

#: Stubs that record their argv, so a test can assert what the scheduler invoked.
STUB_BINARIES = ("aws", "sbatch", "squeue", "scancel")

STUB_TEMPLATE = """\
#!/bin/bash
printf '%s\\n' "$* " >> "$STUB_CALL_LOG.{name}"
exit 0
"""

#: `aws` has to answer plausibly, not just record: `s3_list_libraries` refuses to
#: cache an empty result (a transient AWS failure must not blank the dropdown for
#: an hour), so a stub that printed nothing would re-call on every dump and the
#: warm-cache half of invariant 11 could never be observed.
AWS_STUB = """\
#!/bin/bash
printf '%s\\n' "$* " >> "$STUB_CALL_LOG.aws"
url="${@: -1}"
case "$url" in
    */input/)
        printf '%27s%s\\n' "PRE " "testlib/"
        ;;
    */input/*/)
        for i in 1 2 3; do
            printf '2026-01-01 00:00:00        100 testlib_chunk_00000%s.csv\\n' "$i"
        done
        ;;
esac
exit 0
"""


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
    _drivers: List[subprocess.Popen] = field(default_factory=list)

    # -- environment ------------------------------------------------------
    def env(self, **overrides: str) -> Dict[str, str]:
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
            SCHED_FAKE_DURATION="600",
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

    def write_status(self, rows: List[str]) -> None:
        """Write ``status.tsv`` directly, to seed a pre-existing verdict."""
        header = "#key\tstatus\tdone\ttotal\tstarted\tfinished\tlog\tnote"
        (self.log_dir / "status.tsv").write_text("\n".join([header, *rows]) + "\n")

    def read_queue(self) -> str:
        """Return the current queue-file contents."""
        return self.queue_file.read_text()

    def stub_calls(self, name: str) -> List[str]:
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

    def start_driver(self, *extra: str, **env: str) -> subprocess.Popen:
        """Start the driver in dry-run and return the process."""
        argv = [
            "bash",
            str(remote_dir() / "run-model-queue.sh"),
            str(self.queue_file),
            self.default_library,
            "1000",
            "cpu-queue",
            "--dry-run",
            *extra,
        ]
        # Pin the working directory: anything the scheduler writes to a relative
        # path lands in the test's own sandbox rather than in the repo.
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self.env(**env),
            cwd=self.root,
        )
        self._drivers.append(proc)
        return proc

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

    def sleep_children(self) -> List[int]:
        """PIDs of the fake orchestrators (``sleep 600``) this instance started."""
        proc = subprocess.run(
            ["pgrep", "-f", "sleep 600"], capture_output=True, text=True
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
def scheduler(tmp_path: Path) -> Scheduler:
    """A scheduler instance in a temporary directory, with recording stubs."""
    if shutil.which("bash") is None:  # pragma: no cover - environment guard
        pytest.skip("bash is required to exercise the scheduler")

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for name in STUB_BINARIES:
        stub = stub_bin / name
        stub.write_text(AWS_STUB if name == "aws" else STUB_TEMPLATE.format(name=name))
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
    snippet: str, env: Optional[Dict[str, str]] = None
) -> subprocess.CompletedProcess:
    """Source ``scheduler-lib.sh`` and run ``snippet`` against it.

    Lets a test assert on the library's own helpers — the parsing and locking
    primitives that the driver, the control CLI and the renderer all share.
    """
    script = f'source "{remote_dir() / "scheduler-lib.sh"}"\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=60,
    )
