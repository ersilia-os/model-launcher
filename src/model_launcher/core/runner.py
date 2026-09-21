"""Transport to ``sched-ctl.sh`` — locally or over SSH.

The TUI never talks to AWS, never parses SLURM and never edits the queue file
itself. It does exactly two things:

    * read one snapshot          -> ``sched-ctl.sh dump``
    * mutate the queue / driver  -> ``sched-ctl.sh <verb> ...``

Keeping both behind this one seam is what lets the same app run on the cluster
head node and on your laptop: only the way a command is spawned changes.

Over SSH every call reuses a single multiplexed connection (ControlMaster), so a
refresh tick costs a few milliseconds instead of a fresh TCP+auth handshake.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .remote import ctl_path

DEFAULT_REMOTE_CTL = "/shared/scripts/large_library_scripts/scheduler/sched-ctl.sh"

# Long enough to survive a slow shared filesystem, short enough that a hung
# connection surfaces as a "stale" banner rather than a frozen UI.
DEFAULT_TIMEOUT = 25

# S3 recounts are a different order of magnitude: one prefix listing per model, each
# walking every object, plus ~1s of AWS CLI startup per call. A seven-model queue
# over a 13,639-chunk library runs to minutes, not seconds.
LIVE_TIMEOUT = 120  # totals per library + the running row
LIVE_ALL_TIMEOUT = 600  # every row, on explicit request


class RunnerError(RuntimeError):
    """A ctl invocation could not be carried out at all (transport failure)."""


@dataclass
class Runner:
    """Base runner. Subclasses only implement :meth:`_argv`."""

    ctl: str
    log_dir: Optional[str] = None
    queue_file: Optional[str] = None
    s3_bucket: Optional[str] = None
    timeout: int = DEFAULT_TIMEOUT

    # -- description ------------------------------------------------------
    @property
    def location(self) -> str:
        return "local"

    def _argv(self, args: Sequence[str]) -> List[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _global_flags(self) -> List[str]:
        flags: List[str] = []
        if self.log_dir:
            flags += ["--log-dir", self.log_dir]
        if self.queue_file:
            flags += ["-q", self.queue_file]
        return flags

    # -- invocation -------------------------------------------------------
    def run(self, *args: str, timeout: Optional[int] = None) -> Tuple[int, str, str]:
        """Invoke ctl. Returns ``(returncode, stdout, stderr)``.

        A non-zero return code is a normal outcome (e.g. "no queue entry matches")
        and is handed back for the UI to show. Only a failure to *execute* raises.
        """
        argv = self._argv([*self._global_flags(), *args])
        env = dict(os.environ)
        if self.s3_bucket:
            env["S3_BUCKET"] = self.s3_bucket
        budget = timeout or self.timeout
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=budget,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RunnerError(f"cannot execute {argv[0]!r}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            # Report the budget actually used, not self.timeout: a recount runs
            # with a much larger one, and blaming the 25s default made a slow
            # --live-all look like a broken connection.
            raise RunnerError(
                f"timed out after {budget}s running: {' '.join(argv)}"
            ) from exc
        return proc.returncode, proc.stdout, proc.stderr

    def dump(self, log_path: Optional[str] = None, live: str = "") -> str:
        """One complete snapshot — the only read the TUI ever performs.

        ``live`` asks ctl to recount progress from S3: ``"running"`` for per-library
        totals plus the running row, ``"all"`` for every row. Empty means trust the
        counts the driver recorded.

        A recount needs a far bigger time budget than a plain dump: each prefix
        listing walks every object (a 13,639-chunk library is ~14 paged API calls)
        and the AWS CLI costs about a second just to start. At the plain-dump
        timeout a full recount reports as "cannot reach the scheduler", which reads
        as a broken connection rather than "this is slow".
        """
        args = ["dump"]
        if log_path:
            args += ["--log", log_path]
        timeout = self.timeout
        if live == "running":
            args.append("--live")
            timeout = max(timeout, LIVE_TIMEOUT)
        elif live == "all":
            args.append("--live-all")
            timeout = max(timeout, LIVE_ALL_TIMEOUT)
        rc, out, err = self.run(*args, timeout=timeout)
        if rc != 0 and not out.strip():
            raise RunnerError(err.strip() or f"dump failed (rc={rc})")
        return out


@dataclass
class LocalRunner(Runner):
    """Runs ctl as a child process on this machine."""

    def _argv(self, args: Sequence[str]) -> List[str]:
        return ["bash", self.ctl, *args]


@dataclass
class SshRunner(Runner):
    """Runs ctl on a remote host over a persisted SSH connection.

    ``ControlMaster``/``ControlPersist`` mean the first call pays for the
    handshake and every later call (each refresh tick, each keypress) rides the
    existing channel. Without this, a 2-second refresh interval would open a new
    SSH connection every 2 seconds.
    """

    host: str = ""
    ssh_opts: List[str] = field(default_factory=list)

    @property
    def location(self) -> str:
        return self.host

    def _ssh_argv(self) -> List[str]:
        control_path = os.path.expanduser("~/.ssh/cm-%r@%h:%p")
        return [
            "ssh",
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={control_path}",
            "-o",
            "ControlPersist=600",
            "-o",
            "BatchMode=yes",
            "-o",
            "ServerAliveInterval=15",
            *self.ssh_opts,
            self.host,
        ]

    def _argv(self, args: Sequence[str]) -> List[str]:
        # One quoted string: ssh concatenates its command words with spaces and
        # hands the result to the remote shell, so we must quote ourselves.
        remote = " ".join(shlex.quote(a) for a in ["bash", self.ctl, *args])
        env_prefix = ""
        if self.s3_bucket:
            env_prefix = f"S3_BUCKET={shlex.quote(self.s3_bucket)} "
        return [*self._ssh_argv(), "--", f"{env_prefix}{remote}"]


def build_runner(
    host: Optional[str] = None,
    ctl: Optional[str] = None,
    log_dir: Optional[str] = None,
    queue_file: Optional[str] = None,
    s3_bucket: Optional[str] = None,
    ssh_opts: Optional[Sequence[str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Runner:
    """Pick the transport from the arguments/environment.

    ``host`` (or ``$SCHEDULER_HOST``) selects SSH; otherwise everything runs
    locally. A local run falls back to the ctl script packaged with this client,
    so development and the test suite work with no configuration at all.
    """
    host = host or os.environ.get("SCHEDULER_HOST") or None
    ctl = ctl or os.environ.get("SCHEDULER_CTL") or None
    log_dir = log_dir or os.environ.get("LOG_DIR") or None
    queue_file = queue_file or os.environ.get("QUEUE_FILE") or None
    s3_bucket = s3_bucket or os.environ.get("S3_BUCKET") or None

    if host:
        return SshRunner(
            ctl=ctl or DEFAULT_REMOTE_CTL,
            host=host,
            log_dir=log_dir,
            queue_file=queue_file,
            s3_bucket=s3_bucket,
            ssh_opts=list(ssh_opts or []),
            timeout=timeout,
        )

    if not ctl:
        ctl = _find_local_ctl() or DEFAULT_REMOTE_CTL
    return LocalRunner(
        ctl=ctl,
        log_dir=log_dir,
        queue_file=queue_file,
        s3_bucket=s3_bucket,
        timeout=timeout,
    )


def _find_local_ctl() -> Optional[str]:
    """Locate sched-ctl.sh for a local run: deployed copy, then the packaged one.

    The packaged copy is always present, so a local run needs no configuration.
    A deployed copy still wins: on a machine that has one, that is the version
    the live driver is running, and the client must not talk to a different one.
    """
    candidates = [DEFAULT_REMOTE_CTL, str(ctl_path())]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None
