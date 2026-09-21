#!/bin/bash
# =============================================================================
# Launch the wave scheduler driver in a DETACHED tmux session.
# =============================================================================
# Starts run-model-queue.sh inside a tmux session named 'scheduler' (distinct from
# the manual 'waves' orchestrator session) so it survives disconnect. Refuses to
# double-start. All positional args are forwarded to the driver; the relevant env
# vars are INLINED into the launched command (a detached tmux shell does not
# reliably inherit the caller's exports).
#
# Usage: start-scheduler-tmux.sh <queue_file> [default_library] [default_wave_size] [default_queue] [--dry-run]
# Env forwarded: S3_BUCKET POLL_SECONDS ON_FAIL AUTO_FETCH_SIF LOG_DIR STATE_FILE
# =============================================================================

set -uo pipefail

SESSION="${SCHEDULER_TMUX:-scheduler}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRIVER="${SCRIPT_DIR}/run-model-queue.sh"
[ -f "$DRIVER" ] || DRIVER="/shared/scripts/large_library_scripts/scheduler/run-model-queue.sh"

if [ "$#" -lt 1 ] || [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    echo "Usage: $0 <queue_file> [default_library] [default_wave_size] [default_queue] [--dry-run]"
    exit 1
fi

command -v tmux >/dev/null 2>&1 || { echo "ERROR: tmux is not installed."; exit 1; }
[ -f "$DRIVER" ] || { echo "ERROR: driver not found: $DRIVER"; exit 1; }

QUEUE_FILE="$1"
[ -f "$QUEUE_FILE" ] || { echo "ERROR: queue file not found: $QUEUE_FILE"; exit 1; }

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "ERROR: tmux session '$SESSION' already exists (a scheduler is running?)."
    echo "       Attach:  tmux attach -t $SESSION"
    exit 1
fi

LOG_DIR="${LOG_DIR:-/shared/logs/scheduler}"
mkdir -p "$LOG_DIR"

# Absolute paths — a detached tmux session starts in $HOME, not the current dir.
DRIVER="$(cd "$(dirname "$DRIVER")" && pwd)/$(basename "$DRIVER")"
ARGS=("$@")
ARGS[0]="$(cd "$(dirname "$QUEUE_FILE")" && pwd)/$(basename "$QUEUE_FILE")"   # absolutize queue path

# Inline env + driver call + tee to a driver log. %q keeps everything shell-safe.
ENVS=$(printf 'S3_BUCKET=%q POLL_SECONDS=%q ON_FAIL=%q AUTO_FETCH_SIF=%q LOG_DIR=%q' \
    "${S3_BUCKET:-ai2050-ersilia-cluster}" "${POLL_SECONDS:-30}" \
    "${ON_FAIL:-continue}" "${AUTO_FETCH_SIF:-0}" "$LOG_DIR")
[ -n "${STATE_FILE:-}" ] && ENVS="$ENVS STATE_FILE=$(printf '%q' "$STATE_FILE")"

CMD=$(printf '%q ' "$DRIVER" "${ARGS[@]}")
FULL="${ENVS} ${CMD}2>&1 | tee -a ${LOG_DIR}/driver.log"

tmux new-session -d -s "$SESSION" "$FULL"

echo "Detached scheduler started in tmux session '$SESSION'."
echo "  attach : tmux attach -t $SESSION      (detach again: Ctrl-b d)"
echo "  status : watch -n 30 ${SCRIPT_DIR}/scheduler-status.sh"
echo "  driver log : ${LOG_DIR}/driver.log"
