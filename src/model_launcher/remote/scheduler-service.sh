#!/bin/bash
# =============================================================================
# Service entry point for the scheduler driver (see install-scheduler-service.sh):
# systemd on Linux, launchd on macOS.
# =============================================================================
# systemd uses two verbs, one per unit hook; launchd uses the third:
#
#   start <queue_file> [default_library] [default_wave_size] [default_queue]
#       ExecStart. Becomes the driver via `exec`, so the driver IS the unit's
#       main PID: systemd's SIGTERM reaches its trap directly (KillMode=mixed
#       signals only the main PID first), and no `| tee` shell sits in between.
#       Output is appended to $LOG_DIR/driver.log — the same file the tmux
#       launcher writes — because systemd 219 has no StandardOutput=append:.
#
#   stop-post
#       ExecStopPost. The crash safety net. After a clean stop the driver's own
#       cleanup has already cancelled the orchestrator and its SLURM array and
#       marked the job `cancelled`, so this finds nothing to do. After a crash
#       (SIGKILL, OOM, stop timeout) the job is still `running` and its array is
#       still on SLURM with nobody watching it; the restarted driver would
#       resume the same model beside it. This cancels that array, using only
#       ids from that job's own log. systemd runs ExecStopPost only once every
#       process in the unit is gone, so the orchestrator is already dead —
#       invariant 4 (orchestrator dead BEFORE scancel) holds by construction.
#       The row is left `running` on purpose: the next driver's
#       reclaim_stale_running resumes it (invariant 14).
#
#   launchd <queue_file> [default_library] [default_wave_size] [default_queue]
#       The LaunchAgent's program (macOS). launchd has no SuccessExitStatus=,
#       no RestartPreventExitStatus= and no ExecStopPost=, so instead of
#       becoming the driver this stays its parent and supplies all three:
#         * TERM/INT (launchctl bootout) is passed on to the driver, whose
#           trap stops the job as usual; its 130/143 then exits 0, so
#           KeepAlive (SuccessfulExit=false) does not restart a deliberate stop.
#         * 75 (another driver holds the lock) exits 0 too: logged, not looped.
#         * Any other exit is a crash. The job's orchestrator is still running
#           — on macOS it shares the driver's process group — so it is stopped
#           here (its trap closes the ersilia model), then the exit code goes
#           back to launchd, which restarts the driver to resume the job.
#
# Env: the same as run-model-queue.sh; the unit sets PATH, LOG_DIR and the rest.
# =============================================================================

set -uo pipefail

# A bash >= 4 and, on macOS, Homebrew's tools, before anything else runs.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bash-floor.sh" || exit 1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Same defaults, in the same order, as the driver itself — so this script and the
# driver it becomes can never disagree about which LOG_DIR they mean.
# Beside the scripts; else, for a pip-installed copy (whose folder `pip install -U`
# replaces), ~/.config/model-launcher/scheduler.conf. An explicit SCHEDULER_CONF wins.
if [ -z "${SCHEDULER_CONF:-}" ]; then
    SCHEDULER_CONF="${SCRIPT_DIR}/scheduler.conf"
    [ -f "$SCHEDULER_CONF" ] || SCHEDULER_CONF="${HOME:-}/.config/model-launcher/scheduler.conf"
fi
# shellcheck source=/dev/null
[ -f "$SCHEDULER_CONF" ] && source "$SCHEDULER_CONF"
LOG_DIR="${LOG_DIR:-/shared/logs/scheduler}"
# Exported BEFORE exec, which is what puts it into the driver's
# /proc/<pid>/environ — the only place driver_pid_scan and client discovery can
# read it from. An `export` inside the driver would come too late.
export LOG_DIR

verb="${1:-}"
shift || true

case "$verb" in
    start)
        [ "$#" -ge 1 ] || { echo "Usage: $0 start <queue_file> [lib] [wave] [queue]" >&2; exit 2; }
        mkdir -p "$LOG_DIR" || exit 1
        exec >>"${LOG_DIR}/driver.log" 2>&1
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting under systemd (pid $$)"
        exec bash "${SCRIPT_DIR}/run-model-queue.sh" "$@"
        ;;
    launchd)
        [ "$#" -ge 1 ] || { echo "Usage: $0 launchd <queue_file> [lib] [wave] [queue]" >&2; exit 2; }
        mkdir -p "$LOG_DIR" || exit 1
        exec >>"${LOG_DIR}/driver.log" 2>&1
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting under launchd (wrapper pid $$)"
        bash "${SCRIPT_DIR}/run-model-queue.sh" "$@" &
        driver=$!
        trap 'kill -TERM "$driver" 2>/dev/null' TERM INT
        rc=0
        wait "$driver" || rc=$?
        # A trapped signal ends `wait` early; wait for the driver's real exit.
        while kill -0 "$driver" 2>/dev/null; do
            rc=0
            wait "$driver" || rc=$?
        done
        case "$rc" in
            0|130|143)
                exit 0 ;;
            75)
                echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] another driver holds the lock for ${LOG_DIR}; not restarting"
                exit 0 ;;
        esac
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] driver exited rc=${rc} without cleaning up"
        # Only as launchd runs it: the leader of our own process group. Anywhere
        # else the group is someone else's, and must not be touched.
        # What the dead driver left is now an orphan: still in our group, with
        # pid 1 (launchd) as its parent. Its own children follow its trap.
        if [ "$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')" = "$$" ]; then
            left="$(ps -A -o pid=,ppid=,pgid= 2>/dev/null \
                    | awk -v g="$$" '$3 == g && $2 == 1 { printf "%s ", $1 }')"
            if [ -n "$left" ]; then
                echo "  stopping what it left running: ${left}"
                # shellcheck disable=SC2086
                kill -TERM $left 2>/dev/null
                waited=0
                while [ "$waited" -lt "${SERVE_STOP_SECONDS:-60}" ]; do
                    alive=0
                    for pid in $left; do kill -0 "$pid" 2>/dev/null && alive=1; done
                    [ "$alive" -eq 1 ] || break
                    sleep 1; waited=$((waited + 1))
                done
            fi
        fi
        exit "$rc"
        ;;
    stop-post)
        # shellcheck source=/dev/null
        source "${SCRIPT_DIR}/scheduler-lib.sh" || exit 1
        exec >>"${LOG_DIR}/driver.log" 2>&1
        status_load
        for key in "${!ST_STATUS[@]}"; do
            [ "${ST_STATUS[$key]}" = "running" ] || continue
            ids="$(log_submitted_ids "${ST_LOG[$key]}")"
            echo "[$(now_iso)] driver stopped without cleaning up ${key%%|*}" \
                 "— cancelling its SLURM jobs: ${ids:-none recorded}"
            for aid in $ids; do
                scancel "$aid" 2>/dev/null
            done
        done
        exit 0
        ;;
    *)
        echo "Usage: $0 start|launchd <queue_file> [lib] [wave] [queue] | stop-post" >&2
        exit 2
        ;;
esac
