#!/bin/bash
# =============================================================================
# systemd entry point for the scheduler driver (see install-scheduler-service.sh).
# =============================================================================
# Two verbs, one per unit hook:
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
# Env: the same as run-model-queue.sh; the unit sets PATH, LOG_DIR and the rest.
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Same defaults, in the same order, as the driver itself — so this script and the
# driver it becomes can never disagree about which LOG_DIR they mean.
SCHEDULER_CONF="${SCHEDULER_CONF:-${SCRIPT_DIR}/scheduler.conf}"
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
        echo "Usage: $0 start <queue_file> [lib] [wave] [queue] | stop-post" >&2
        exit 2
        ;;
esac
