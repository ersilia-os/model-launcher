"""Finding the scheduler on a target by looking for its running driver.

Guessing where the scripts were deployed is fragile: the answer differs per
machine, and a wrong guess fails with a bare "No such file or directory". A
running driver already knows both things the client needs — which copy of the
scripts it is running from, and its ``LOG_DIR`` — so over SSH the client asks
it, and only falls back to a default path when no driver is running.

This runs *before* ``sched-ctl.sh`` (whose path is the thing being found), so
it is a small self-contained bash probe rather than a ctl verb. It is not part
of the deployed payload and needs nothing on the target beyond a POSIX-ish
userland and any bash, 3.2 included.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from .model import Snapshot, parse_dump
from .remote import CTL_NAME
from .runner import LocalRunner, Runner, RunnerError, SshRunner

#: One line per scheduler instance: ``pid<TAB>log_dir<TAB>script_dir``.
#:
#: Two sources, deduplicated by ``LOG_DIR`` (one LOG_DIR is one instance):
#:
#: * **The registry** every driver writes while it runs
#:   (``~/.config/model-launcher/drivers/<pid>``, see ``registry_add`` in
#:   scheduler-lib.sh). It works on every OS, and it is the only source on
#:   macOS, which has no ``/proc``.
#: * **/proc** (Linux), for a driver started before the registry existed.
#:   Candidates come from ``pgrep``, which also matches processes that merely
#:   *mention* the driver — the tmux server and the ``sh -c "... | tee"``
#:   wrapper start-scheduler-tmux.sh launches. Those do not carry ``LOG_DIR``
#:   in their own environment, so a candidate without one is skipped.
#:   ``script_dir`` comes from ``driver.info`` when its pid is alive, else from
#:   the script path in the process's own argv.
#:
#: Over SSH this runs as ``bash -s`` — on a Mac that is /bin/bash 3.2, so it
#: must not use anything newer (no associative arrays).
PROBE = r"""
seen='
'
emit() {
    case "$seen" in *"
$2
"*) return 0 ;; esac
    seen="${seen}$2
"
    printf '%s\t%s\t%s\n' "$1" "$2" "$3"
}

reg="${SCHED_REGISTRY_DIR:-${HOME:-/tmp}/.config/model-launcher/drivers}"
for f in "$reg"/*; do
    [ -f "$f" ] || continue
    pid="${f##*/}"
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    kill -0 "$pid" 2>/dev/null || continue
    ps -o command= -p "$pid" 2>/dev/null | grep -q 'run-model-queue\.sh' || continue
    log_dir="$(sed -n 's/^log_dir=//p' "$f" | head -n 1)"
    script_dir="$(sed -n 's/^script_dir=//p' "$f" | head -n 1)"
    [ -n "$log_dir" ] && [ -n "$script_dir" ] || continue
    emit "$pid" "$log_dir" "$script_dir"
done

# SCHED_DISCOVER_NO_PROC=1 (tests) looks only at the registry, as macOS does.
[ -d /proc/self ] && [ -z "${SCHED_DISCOVER_NO_PROC:-}" ] || exit 0
for pid in $(pgrep -u "$(id -u)" -f 'run-model-queue\.sh' 2>/dev/null); do
    log_dir="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^LOG_DIR=//p' | head -n 1)"
    [ -n "$log_dir" ] || continue
    script_dir=
    info="$log_dir/driver.info"
    if [ -f "$info" ]; then
        info_pid="$(sed -n 's/^pid=//p' "$info" | head -n 1)"
        if [ -n "$info_pid" ] && kill -0 "$info_pid" 2>/dev/null; then
            pid="$info_pid"
            script_dir="$(sed -n 's/^script_dir=//p' "$info" | head -n 1)"
        fi
    fi
    if [ -z "$script_dir" ]; then
        script=
        while IFS= read -r -d '' arg; do
            case "$arg" in */run-model-queue.sh|run-model-queue.sh) script="$arg"; break ;; esac
        done < "/proc/$pid/cmdline"
        [ -n "$script" ] || continue
        case "$script" in /*) ;; *) script="$(readlink "/proc/$pid/cwd")/$script" ;; esac
        script_dir="$(cd "$(dirname "$script")" 2>/dev/null && pwd)" || continue
    fi
    emit "$pid" "$log_dir" "$script_dir"
done
exit 0
"""


@dataclass(frozen=True)
class Driver:
    """A scheduler driver found running on the target.

    Attributes
    ----------
    pid : int
        The driver's process id on the target.
    log_dir : str
        The ``LOG_DIR`` it runs against — what identifies the instance.
    ctl : str
        The ``sched-ctl.sh`` beside the driver's own script.
    """

    pid: int
    log_dir: str
    ctl: str


def discover_drivers(runner: Runner) -> list[Driver]:
    """List the scheduler drivers running on the runner's target.

    Parameters
    ----------
    runner : Runner
        Transport to the target. Its ``ctl`` is not used.

    Returns
    -------
    list of Driver
        One entry per running instance (per ``LOG_DIR``); empty if none.

    Raises
    ------
    RunnerError
        If the target could not be reached at all.
    """
    rc, out, err = runner.run_script(PROBE)
    if rc != 0:
        raise RunnerError(err.strip() or f"driver discovery failed (rc={rc})")
    drivers = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        pid, log_dir, script_dir = parts
        drivers.append(
            Driver(
                pid=int(pid),
                log_dir=posixpath.normpath(log_dir),
                ctl=posixpath.join(script_dir, CTL_NAME),
            )
        )
    return drivers


#: A probe is a quick "is anything there?" for a list of hosts, so it must not
#: wait on a dead machine for as long as a real ctl call would.
PROBE_TIMEOUT = 10
PROBE_SSH_OPTS = ("-o", "ConnectTimeout=5")


@dataclass(frozen=True)
class HostStatus:
    """What a quick probe found on one host.

    Attributes
    ----------
    state : str
        ``running`` (one or more drivers), ``none`` or ``unreachable``.
    drivers : tuple of Driver
        The drivers found, when ``running``.
    detail : str
        The transport error, when ``unreachable``.
    """

    state: str
    drivers: tuple[Driver, ...] = ()
    detail: str = ""

    @property
    def label(self) -> str:
        """One short phrase for a host list, e.g. ``RUNNING · pid 29886``."""
        if self.state == "running":
            if len(self.drivers) == 1:
                return f"RUNNING · pid {self.drivers[0].pid}"
            return f"RUNNING · {len(self.drivers)} drivers"
        return "no driver" if self.state == "none" else "unreachable"


def probe_host(host: str | None) -> HostStatus:
    """Check which scheduler drivers are running on one host, quickly.

    Parameters
    ----------
    host : str or None
        SSH alias to probe, or None for this machine.

    Returns
    -------
    HostStatus
        Never raises: an unreachable host is a status, not an error.
    """
    if host:
        runner: Runner = SshRunner(
            ctl="unused",
            host=host,
            ssh_opts=list(PROBE_SSH_OPTS),
            timeout=PROBE_TIMEOUT,
        )
    else:
        runner = LocalRunner(ctl="unused", timeout=PROBE_TIMEOUT)
    try:
        drivers = discover_drivers(runner)
    except RunnerError as exc:
        return HostStatus("unreachable", detail=str(exc))
    if not drivers:
        return HostStatus("none")
    return HostStatus("running", drivers=tuple(drivers))


def peek_queue(
    host: str | None, driver: Driver, s3_bucket: str | None = None
) -> Snapshot | None:
    """Read one driver's queue, quickly, for a host list.

    A plain ``dump``: the jobs, their statuses and the counts the driver last
    recorded. It never counts from S3 (invariant 11), so it is cheap enough to
    run for every host a list shows; the counts can be a recount interval old.

    Parameters
    ----------
    host : str or None
        SSH alias of the driver's machine, or None for this machine.
    driver : Driver
        The driver to read, as :func:`probe_host` found it.
    s3_bucket : str, optional
        Passed on to ctl, as for any other call.

    Returns
    -------
    Snapshot or None
        The snapshot, or None if it could not be read. Never raises.
    """
    if host:
        runner: Runner = SshRunner(
            ctl=driver.ctl,
            log_dir=driver.log_dir,
            s3_bucket=s3_bucket,
            host=host,
            ssh_opts=list(PROBE_SSH_OPTS),
            timeout=PROBE_TIMEOUT,
        )
    else:
        runner = LocalRunner(
            ctl=driver.ctl,
            log_dir=driver.log_dir,
            s3_bucket=s3_bucket,
            timeout=PROBE_TIMEOUT,
        )
    try:
        snap = parse_dump(runner.dump())
    except RunnerError:
        return None
    return None if snap.error else snap
