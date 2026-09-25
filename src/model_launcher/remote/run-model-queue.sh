#!/bin/bash
# =============================================================================
# Wave scheduler — sequential multi-model driver (dynamic queue).
# =============================================================================
# Reads a queue of models and runs each one's wave orchestrator back-to-back so
# the cluster stays busy with minimal idle time. For each queue line it dispatches
# submit-ersilia-waves.sh or submit-singularity-waves.sh, captures the exit code,
# and maintains an atomic status file that scheduler-status.sh renders.
#
# The queue file is the LIVE SOURCE OF TRUTH: it is re-read before every job, so
# you can add / remove / reorder / hold models while the driver runs. Line order
# IS the priority. Use sched-ctl.sh (or the Textual TUI) to edit it safely, or
# hand-edit it — both take the same lock.
#
# The dispatched orchestrator runs in its OWN PROCESS GROUP in the background and
# is polled, so the driver stays responsive to control requests (pause, cancel the
# running model) instead of blocking for hours inside the child.
#
# Runs on the HEAD NODE inside tmux (a full queue can take days):
#   tmux new -s scheduler
#   S3_BUCKET=ai2050-ersilia-cluster ./run-model-queue.sh models.queue Enamine_Real_Sample_1.4B
# ...or launch it detached with start-scheduler-tmux.sh.
#
# Usage:
#   run-model-queue.sh <queue_file> [default_library] [default_wave_size] [default_queue]
#                      [--dry-run] [--exit-when-empty]
#
# Queue file: one job per line; blank lines and '#' comments (incl. indented) ignored;
# whitespace-separated:
#   <model_id> <mode> [library] [wave_size] [queue] [flags]   mode = ersilia | singularity
#   * library optional  -> default_library (alias-resolved; e.g. real -> Enamine_Real_Sample_10.4M)
#   * wave_size optional -> default_wave_size (1..1000)
#   * queue optional     -> default_queue
#   * flags optional     -> `hold` parks the job (driver skips it until unheld)
#
# Env:
#   S3_BUCKET        (default ai2050-ersilia-cluster)   passed through to the orchestrators
#   POLL_SECONDS     (default 30)                        passed through to the orchestrators
#   ON_FAIL          continue | halt   (default continue)
#   AUTO_FETCH_SIF   0 | 1             (default 0 — do NOT download; missing SIF => missing-files)
#   LOG_DIR          (default /shared/logs/scheduler)
#   STATE_FILE       (default $LOG_DIR/state.tsv)
#   STATUS_FILE      (default $LOG_DIR/status.tsv)
#   CTL_POLL         (default 15)   how often to check control requests while a job runs
#   IDLE_POLL        (default 30)   how often to re-read the queue when it has nothing runnable
#   REFRESH_SECONDS  (default 300)  how often to re-count S3 progress for the running job
#   EXIT_WHEN_EMPTY  0 | 1 (default 0 — idle and wait for queue changes instead of exiting)
#   SCHED_FAKE_RC    (dry-run only) space-separated fake exit codes by queue index, for testing
#   SCHED_FAKE_S3    (test only) 1 => count from a fixture instead of `aws s3 ls` (see the lib)
# =============================================================================

set -uo pipefail

usage() {
    sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'
}

# ---- args: separate flags from positionals ----
DRY_RUN=0
POS=()
for a in "$@"; do
    case "$a" in
        --dry-run)         DRY_RUN=1 ;;
        --exit-when-empty) EXIT_WHEN_EMPTY=1 ;;
        -h|--help)         usage; exit 0 ;;
        *)                 POS+=("$a") ;;
    esac
done
QUEUE_FILE="${POS[0]:-}"
DEFAULT_LIBRARY="${POS[1]:-}"
DEFAULT_WAVE_SIZE="${POS[2]:-1000}"

# ---- locate this script's own directory ----
# scheduler.conf and scheduler-lib.sh both live beside it, and the conf has to
# be sourced before ANY ${VAR:-default} below fixes a value it was meant to
# override — hence computing this before the env/config block, not after it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- scheduler.conf: per-machine defaults ----
# A DEFAULTS file, not an assignment file — see scheduler.conf.example.
# Precedence, high to low: CLI flag/positional > environment > this file >
# the built-in default hardcoded below.
SCHEDULER_CONF="${SCHEDULER_CONF:-${SCRIPT_DIR}/scheduler.conf}"
# shellcheck source=/dev/null
[ -f "$SCHEDULER_CONF" ] && source "$SCHEDULER_CONF"

DEFAULT_QUEUE="${POS[3]:-${DEFAULT_QUEUE:-cpu-queue}}"

# ---- env / config ----
S3_BUCKET="${S3_BUCKET:-ai2050-ersilia-cluster}"
POLL_SECONDS="${POLL_SECONDS:-30}"
ON_FAIL="${ON_FAIL:-continue}"
AUTO_FETCH_SIF="${AUTO_FETCH_SIF:-0}"
SIF_DIR="${SIF_DIR:-/shared/sif-files}"
DOWNLOAD_SCRIPT="${DOWNLOAD_SCRIPT:-/shared/scripts/download-ersilia-model.sh}"
LOG_DIR="${LOG_DIR:-/shared/logs/scheduler}"
# Exported so every child (ctl helpers, orchestrators) resolves the same value.
# NOT enough on its own for driver_pid_scan / client discovery, which read
# /proc/<pid>/environ: that is the environment the process was exec'd WITH, and
# an `export` here never shows up in it. A launcher has to set LOG_DIR before
# exec'ing this script — start-scheduler-tmux.sh and scheduler-service.sh both
# do. A driver started by hand without one is invisible to both scans.
export LOG_DIR
STATE_FILE="${STATE_FILE:-${LOG_DIR}/state.tsv}"
STATUS_FILE="${STATUS_FILE:-${LOG_DIR}/status.tsv}"
CTL_POLL="${CTL_POLL:-15}"
IDLE_POLL="${IDLE_POLL:-30}"
REFRESH_SECONDS="${REFRESH_SECONDS:-300}"
EXIT_WHEN_EMPTY="${EXIT_WHEN_EMPTY:-0}"
declare -a FAKE_RC=(${SCHED_FAKE_RC:-})   # dry-run test hook (empty in normal use)

if [ -z "$QUEUE_FILE" ]; then usage; exit 1; fi
[ -f "$QUEUE_FILE" ] || { echo "ERROR: queue file not found: $QUEUE_FILE"; exit 1; }
QUEUE_FILE="$(cd "$(dirname "$QUEUE_FILE")" && pwd)/$(basename "$QUEUE_FILE")"

# ---- source the shared lib ----
LIB="${SCRIPT_DIR}/scheduler-lib.sh"
[ -f "$LIB" ] || LIB="/shared/scripts/scheduler/scheduler-lib.sh"
# shellcheck source=/dev/null
source "$LIB" || { echo "ERROR: cannot source scheduler-lib.sh ($LIB)"; exit 1; }

# ---- library-aliases (resolve_library); passthrough if not deployed ----
# The packaged copy beside this script is checked first; the /shared paths are
# kept for a deployment that predates the scripts moving into this package.
for cand in "${SCRIPT_DIR}/library-aliases.sh" \
            /shared/scripts/library-aliases.sh \
            "${SCRIPT_DIR}/../../AWS_templates/library-aliases.sh" \
            /shared/scripts/AWS_templates/library-aliases.sh; do
    # shellcheck source=/dev/null
    [ -f "$cand" ] && { source "$cand"; break; }
done
if ! declare -F resolve_library >/dev/null; then
    resolve_library() { echo "$1"; }   # no alias table -> pass names through unchanged
fi

# ---- locate the wave orchestrators ----
# Packaged layout puts them at slurm/ beside this script. A deployment that
# predates the move (or a nonstandard layout via scheduler.conf's WAVES_DIR)
# falls back to the old one-directory-up location, then the /shared default.
WAVES_DIR="${WAVES_DIR:-${SCRIPT_DIR}/slurm}"
[ -f "${WAVES_DIR}/submit-ersilia-waves.sh" ] || WAVES_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
[ -f "${WAVES_DIR}/submit-ersilia-waves.sh" ] || WAVES_DIR="/shared/scripts/scheduler/slurm"

mkdir -p "$LOG_DIR" "$(control_dir)"

# ---- single-driver lock ----
#
# A flock on fd 8 (fd 9 belongs to queue_locked). The kernel releases it however
# the driver dies — including SIGKILL, which no trap can see. The mkdir lock it
# replaces could not: a SIGKILLed driver left the directory behind, every restart
# then exited "another driver holds the lock", and a supervisor restarting on
# failure looped until it gave up — while the interrupted job stayed `running`.
#
# Lock held => exit 75 (EX_TEMPFAIL). The systemd unit lists 75 as
# RestartPreventExitStatus: a conflict should fail once and loudly, not loop.
LOCK_FILE="${LOG_DIR}/.driver.lock"
LEGACY_LOCK="${LOG_DIR}/.lock"
LOCK_HELD_RC=75
DRIVER_LOCK=""

# Is $1 this driver or one of its own subshells? Those share its argv and
# environment, and the scan below runs inside $(...) — so its helpers are
# grandchildren, not just children, of $$.
is_self_or_descendant() {  # $1 = pid
    local p="$1" n=0
    while [ -n "$p" ] && [ "$p" -gt 1 ] && [ "$n" -lt 16 ]; do
        [ "$p" = "$$" ] && return 0
        p="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')"
        n=$((n + 1))
    done
    return 1
}

# Another live driver on this LOG_DIR, if any. driver_pid_scan cannot answer this
# from inside a driver: this process and its subshells match its pattern too.
# Conservative on purpose: a candidate with NO LOG_DIR in its environment counts,
# since its built-in default may well be ours.
other_driver_pid() {
    local pid env_dir
    for pid in $(pgrep -u "$(id -u)" -f 'run-model-queue\.sh' 2>/dev/null); do
        is_self_or_descendant "$pid" && continue
        is_driver_process "$pid" || continue
        # Gone already (a short-lived subshell): nothing to compare against.
        [ -r "/proc/${pid}/environ" ] || continue
        env_dir="$(tr '\0' '\n' < "/proc/${pid}/environ" 2>/dev/null | sed -n 's/^LOG_DIR=//p' | head -n 1)"
        [ -n "$env_dir" ] && [ "$env_dir" != "$LOG_DIR" ] && continue
        kill -0 "$pid" 2>/dev/null || continue
        printf '%s\n' "$pid"
        return 0
    done
}

# Upgrade guard: a pre-flock driver holds only the .lock DIRECTORY and would never
# see our flock. Refuse while it lives; clear the directory once it is stale.
if [ -d "$LEGACY_LOCK" ]; then
    other="$(other_driver_pid)"
    if [ -n "$other" ]; then
        echo "ERROR: an older driver (pid ${other}) holds ${LEGACY_LOCK} — stop it first."
        exit "$LOCK_HELD_RC"
    fi
    rmdir "$LEGACY_LOCK" 2>/dev/null \
        && echo "[$(now_iso)] removed stale lock directory ${LEGACY_LOCK} (no driver was holding it)"
fi

if command -v flock >/dev/null 2>&1; then
    exec 8>>"$LOCK_FILE" || { echo "ERROR: cannot open driver lock $LOCK_FILE"; exit 1; }
    if ! flock -n 8; then
        echo "ERROR: another driver holds the lock: $LOCK_FILE (pid $(cat "$LOCK_FILE" 2>/dev/null || echo '?'))"
        exit "$LOCK_HELD_RC"
    fi
    # The content is only for the message above; the lock is the flock itself.
    # Never delete this file: a process could open the old inode and "win" a lock
    # nobody else can see.
    echo "$$" > "$LOCK_FILE"
    DRIVER_LOCK=flock
else
    # No flock (e.g. macOS without util-linux): the old behaviour, stale-on-SIGKILL.
    if ! mkdir "$LEGACY_LOCK" 2>/dev/null; then
        echo "ERROR: another driver holds the lock: $LEGACY_LOCK"
        echo "       If no scheduler is running, remove it:  rmdir $LEGACY_LOCK"
        exit "$LOCK_HELD_RC"
    fi
    DRIVER_LOCK=mkdir
fi
cleanup() {
    # Take the orchestrator down with us.
    #
    # It runs under `setsid` in its own process group so that `cancel` can kill the
    # whole tree — but that also means it does NOT die when this driver is stopped.
    # Leaving it behind is actively dangerous: it keeps submitting waves, and the
    # next driver you start will run a SECOND model concurrently, both fighting for
    # nodes and writing into /fsx.
    if [ -n "${CHILD_PID:-}" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
        echo "[$(now_iso)] driver exiting — stopping the orchestrator it launched"
        cancel_child "${CURRENT_LOG:-}"
        if [ -n "${CURRENT_KEY:-}" ]; then
            status_update "$CURRENT_KEY" cancelled "stopped because the driver exited"
        fi
    fi
    [ "$DRIVER_LOCK" = mkdir ] && rmdir "$LEGACY_LOCK" 2>/dev/null
    rm -f "${STATE_FILE}.tmp.$$" "$(status_file).tmp.$$" 2>/dev/null
    rm -f "$(driver_info)" 2>/dev/null
}
trap cleanup EXIT
# Without these, Ctrl-C and `kill` bypass the EXIT trap's cleanup in some shells.
trap 'exit 130' INT
trap 'exit 143' TERM

# ---- state arrays (index-aligned, rebuilt on every queue re-read) ----
Q_MODEL=(); Q_MODE=(); Q_LIB=(); Q_WAVE=(); Q_QUEUE=(); Q_KEY=(); Q_HOLD=(); Q_CPUS=()
Q_STATUS=(); Q_DONE=(); Q_TOTAL=(); Q_START=(); Q_FIN=(); Q_LOG=(); Q_NOTE=()

# ---- control state ----
CANCEL_KEYS=""          # space-separated keys with a pending cancel request
declare -A CANCEL_WHO=()  # want-token (key or model) -> operator who asked, if known
STOP_AFTER=0            # finish the current job, then exit
SHUTDOWN=0              # exit as soon as the current job settles

log_line() { echo "[$(now_iso)] $*"; }

# Our own process group, captured once. cancel_child refuses to signal it.
SELF_PGID="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')"
: "${SELF_PGID:=$$}"

# =============================================================================
# Queue parsing (re-run before every job)
# =============================================================================

add_job() {  # model mode library wave queue hold status note [cpus]
    local i=${#Q_MODEL[@]}
    Q_MODEL[i]="$1"; Q_MODE[i]="$2"; Q_LIB[i]="$3"; Q_WAVE[i]="$4"; Q_QUEUE[i]="$5"
    Q_HOLD[i]="$6"; Q_STATUS[i]="$7"; Q_NOTE[i]="$8"
    # Empty means "no override": the worker's own #SBATCH --cpus-per-task stands.
    # That default differs per mode (ersilia 8, singularity 4) and the deployed
    # copies have been re-tuned by hand, so the driver must not invent a number.
    Q_CPUS[i]="${9:-}"
    Q_KEY[i]="$(job_key "$1" "$2" "$3")"
    Q_DONE[i]=0; Q_TOTAL[i]=0; Q_START[i]="-"; Q_FIN[i]="-"
    # Log name is derived from the job identity, not its queue position, so it is
    # stable across reordering (positions change; the log must not follow).
    Q_LOG[i]="${LOG_DIR}/${1}_${3:-NA}.log"
}

_parse_queue_unlocked() {
    Q_MODEL=(); Q_MODE=(); Q_LIB=(); Q_WAVE=(); Q_QUEUE=(); Q_KEY=(); Q_HOLD=(); Q_CPUS=()
    Q_STATUS=(); Q_DONE=(); Q_TOTAL=(); Q_START=(); Q_FIN=(); Q_LOG=(); Q_NOTE=()
    local raw lib wave queue cpus
    while IFS= read -r raw || [ -n "$raw" ]; do
        parse_queue_line "$raw" || continue

        lib="${QL_LIB:-$DEFAULT_LIBRARY}"
        wave="${QL_WAVE:-$DEFAULT_WAVE_SIZE}"
        queue="${QL_QUEUE:-$DEFAULT_QUEUE}"
        cpus="$QL_CPUS"

        if [ "$QL_MODE" != "ersilia" ] && [ "$QL_MODE" != "singularity" ]; then
            add_job "$QL_MODEL" "${QL_MODE:-?}" "${lib:-NA}" "$wave" "$queue" 0 \
                    skipped "unknown mode '${QL_MODE:-}' (want ersilia|singularity)" "$cpus"
            continue
        fi
        if [ -z "$lib" ]; then
            add_job "$QL_MODEL" "$QL_MODE" "NA" "$wave" "$queue" 0 \
                    skipped "no library and no default_library given" "$cpus"
            continue
        fi
        lib="$(resolve_library "$lib")"
        if ! [[ "$wave" =~ ^[0-9]+$ ]] || [ "$wave" -lt 1 ] || [ "$wave" -gt 1000 ]; then
            add_job "$QL_MODEL" "$QL_MODE" "$lib" "$wave" "$queue" 0 \
                    skipped "wave_size '$wave' out of 1..1000" "$cpus"
            continue
        fi
        # A bad cpus= must not reach sbatch. Rejected here it costs one skipped row;
        # passed through it would be an --cpus-per-task the controller refuses, once
        # per array task, for every wave of the run.
        if [ -n "$cpus" ] && ! is_valid_cpus "$cpus"; then
            add_job "$QL_MODEL" "$QL_MODE" "$lib" "$wave" "$queue" 0 \
                    skipped "cpus '$cpus' out of 1..${MAX_CPUS_PER_TASK}" "$cpus"
            continue
        fi
        if [ "$QL_HOLD" = "1" ]; then
            add_job "$QL_MODEL" "$QL_MODE" "$lib" "$wave" "$queue" 1 held "held in queue file" "$cpus"
        else
            add_job "$QL_MODEL" "$QL_MODE" "$lib" "$wave" "$queue" 0 pending "" "$cpus"
        fi
    done < "$QUEUE_FILE"
}

parse_queue() { queue_locked _parse_queue_unlocked; }

# Overlay the durable status store onto the freshly-parsed queue. Queue-file
# verdicts (skipped, held) win over the store, since they describe the line as it
# reads right now; everything else comes from the store.
merge_status() {
    status_load
    local i k st
    for i in "${!Q_MODEL[@]}"; do
        k="${Q_KEY[i]}"
        st="${ST_STATUS[$k]:-}"
        [ -n "$st" ] || continue
        Q_DONE[i]="${ST_DONE[$k]:-0}";   Q_TOTAL[i]="${ST_TOTAL[$k]:-0}"
        Q_START[i]="${ST_START[$k]:--}"; Q_FIN[i]="${ST_FIN[$k]:--}"
        [ -n "${ST_LOG[$k]:-}" ] && Q_LOG[i]="${ST_LOG[$k]}"
        case "${Q_STATUS[i]}" in
            skipped) continue ;;                                  # bad line: queue file wins
            held)
                # `hold` outranks only `pending`. It stops a job being STARTED; it
                # does not stop one already in flight, and it must not mask a real
                # verdict — a cancelled-then-held job reading as "held" hides the
                # very thing you just did.
                [ "$st" = "pending" ] || Q_STATUS[i]="$st"
                continue ;;
        esac
        Q_STATUS[i]="$st"
        Q_NOTE[i]="${ST_NOTE[$k]:-}"
    done
}

# Persist one job's row back into the durable store, then refresh state.tsv.
persist_job() {  # $1 = index
    local i="$1" k="${Q_KEY[$1]}"
    _do() {
        status_load
        ST_STATUS["$k"]="${Q_STATUS[i]}"; ST_DONE["$k"]="${Q_DONE[i]}"
        ST_TOTAL["$k"]="${Q_TOTAL[i]}";   ST_START["$k"]="${Q_START[i]}"
        ST_FIN["$k"]="${Q_FIN[i]}";       ST_LOG["$k"]="${Q_LOG[i]}"
        ST_NOTE["$k"]="${Q_NOTE[i]}"
        status_write
    }
    queue_locked _do
    write_state
}

# Reset leftover `running` verdicts to pending at startup.
#
# We only reach here holding the exclusive single-driver lock, so by definition no
# other driver is alive — any row still marked `running` was left behind by one that
# died mid-job (Spot preemption, a killed tmux, SIGKILL). Without this the job is
# neither running nor pending: it gets skipped forever while the driver idles, which
# is exactly the failure this scheduler exists to survive.
#
# Its finished chunks are already in S3, so re-dispatching resumes rather than redoing.
reclaim_stale_running() {
    _do() {
        status_load
        local key changed=0
        for key in "${!ST_STATUS[@]}"; do
            [ "${ST_STATUS[$key]}" = "running" ] || continue
            ST_STATUS["$key"]="pending"
            ST_FIN["$key"]="-"
            ST_NOTE["$key"]="reclaimed at startup (previous driver died mid-job)"
            changed=1
            log_line "reclaimed interrupted job: ${key%%|*} (was 'running' — will resume)"
        done
        [ "$changed" -eq 1 ] && status_write
        return 0
    }
    queue_locked _do
}

set_status() {  # $1=index $2=status [$3=note]
    Q_STATUS[$1]="$2"
    [ "$#" -ge 3 ] && Q_NOTE[$1]="$3"
    persist_job "$1"
}

# =============================================================================
# Control channel
# =============================================================================

# Consume every pending control message and update the flags. Cheap enough to
# call on each poll tick.
drain_control() {
    local d f verb payload who
    d="$(control_dir)"
    [ -d "$d" ] || return 0

    STOP_AFTER=0
    [ -f "$(stopafter_flag)" ] && STOP_AFTER=1

    for f in "$d"/*.cancel "$d"/*.shutdown "$d"/*.refresh; do
        [ -f "$f" ] || continue
        verb="${f##*.}"
        # `who` is a second line, appended after the payload by newer clients.
        # An older message simply has nothing on line 2, which reads as empty
        # here — additive, not a format break.
        payload="$(sed -n '1p' "$f" 2>/dev/null)"
        who="$(sed -n '2p' "$f" 2>/dev/null)"
        rm -f "$f"
        case "$verb" in
            cancel)
                CANCEL_KEYS="${CANCEL_KEYS} ${payload}"
                [ -n "$who" ] && CANCEL_WHO["$payload"]="$who"
                log_line "control: cancel requested for '${payload}'${who:+ by ${who}}"
                ;;
            shutdown)
                SHUTDOWN=1
                log_line "control: shutdown requested${who:+ by ${who}}"
                ;;
            refresh)
                FORCE_REFRESH=1
                log_line "control: S3 recount requested${who:+ by ${who}}"
                ;;
        esac
    done
}

# Drop control messages that were posted BEFORE this driver started.
#
# drain_control runs on the first tick of the main loop, so anything already sitting
# in the control dir gets consumed by US — even though it was aimed at a driver that
# is long gone. Every one of those is worse than a no-op:
#   * a stale `shutdown` makes a freshly started driver exit immediately
#   * a stale `cancel` kills the named model the moment the queue reaches it
#   * a stale `stop-after-current` makes this driver run exactly one job and quit
# The click that caused it may be hours old, so the symptom reads as "the scheduler
# is broken" rather than as a consequence. sched-ctl.sh refuses these verbs when no
# driver is alive; this is the other half of the guard, because ctl is a CLI and
# anything could have written here.
#
# The `paused` flag is deliberately NOT cleared: pausing before starting the driver
# is a legitimate way to bring it up idle while you stage the queue, and the state is
# plainly visible in the header and in `sched-ctl.sh list`.
discard_stale_control() {
    local d f n=0
    d="$(control_dir)"
    [ -d "$d" ] || return 0
    for f in "$d"/*.cancel "$d"/*.shutdown "$d"/*.refresh; do
        [ -f "$f" ] || continue
        log_line "discarding stale control message posted before startup: ${f##*/}"
        rm -f "$f"
        n=$((n + 1))
    done
    if [ -f "$(stopafter_flag)" ]; then
        log_line "discarding stale stop-after-current flag posted before startup"
        rm -f "$(stopafter_flag)"
        n=$((n + 1))
    fi
    [ "$n" -gt 0 ] && log_line "discarded ${n} stale control message(s) — they predate this driver"
    return 0
}

is_paused() { [ -f "$(paused_flag)" ]; }

# Does a cancel request name this job? Matches the full key or just the model id,
# so `sched-ctl.sh cancel eos12x7_v1` works without spelling out mode/library.
cancel_wanted() {  # $1 = key
    local key="$1" model="${1%%|*}" want
    for want in $CANCEL_KEYS; do
        [ "$want" = "$key" ] && return 0
        [ "$want" = "$model" ] && return 0
    done
    return 1
}

cancel_clear() {  # $1 = key — drop satisfied requests
    local key="$1" model="${1%%|*}" want keep=""
    for want in $CANCEL_KEYS; do
        [ "$want" = "$key" ] && continue
        [ "$want" = "$model" ] && continue
        keep="${keep} ${want}"
    done
    CANCEL_KEYS="$keep"
    unset "CANCEL_WHO[$key]" "CANCEL_WHO[$model]" 2>/dev/null
}

# Who asked for this job's cancellation, if the request said. Matches the same
# two forms cancel_wanted/cancel_clear do (full key or bare model id). Empty
# for a SHUTDOWN-triggered cancellation, which never named a job specifically.
cancel_who() {  # $1 = key
    local key="$1" model="${1%%|*}"
    [ -n "${CANCEL_WHO[$key]:-}" ] && { printf '%s' "${CANCEL_WHO[$key]}"; return 0; }
    [ -n "${CANCEL_WHO[$model]:-}" ] && { printf '%s' "${CANCEL_WHO[$model]}"; return 0; }
    return 1
}

# =============================================================================
# Dispatch
# =============================================================================

ensure_sif() {  # $1=model $2=logfile ; 0 if present (or fetched), 1 otherwise
    local m="$1" logf="$2"
    [ -f "${SIF_DIR}/${m}.sif" ] && return 0
    if [ "$AUTO_FETCH_SIF" = "1" ]; then
        if [ -x "$DOWNLOAD_SCRIPT" ]; then
            "$DOWNLOAD_SCRIPT" "$m" >>"$logf" 2>&1 && return 0
        else
            aws s3 cp "s3://${S3_BUCKET}/sif-files/${m}.sif" \
                "${SIF_DIR}/${m}.sif" >>"$logf" 2>&1 && return 0
        fi
    fi
    return 1
}

# Cancel the orchestrator we launched, plus whatever it has in flight on SLURM.
# Array job ids are scraped from THIS JOB'S LOG ONLY, so we can never scancel a
# job the scheduler did not start.
# Every descendant of a pid, deepest first.
#
# Walking DOWN from the orchestrator is the whole point: a process-group kill was
# the only thing in this script that could reach the driver itself, and on the
# cluster it did exactly that twice — killing the driver, its tmux pane and the tmux
# server, untrappably (no cleanup, stale lock, orphaned orchestrator). A tree walk
# rooted at the child cannot reach its own parent by construction, and the manual
# `kill <orchestrator pid>` recovery has proved sufficient in practice.
descendants_of() {  # $1 = pid ; echoes children before parents
    local pid="$1" child
    for child in $(pgrep -P "$pid" 2>/dev/null); do
        descendants_of "$child"
    done
    printf '%s\n' "$pid"
}

# Signal a pid and everything under it. Never signals a process group, so it can
# never reach this driver.
kill_tree() {  # $1 = signal, $2 = root pid
    local sig="$1" root="$2" pid
    for pid in $(descendants_of "$root"); do
        [ "$pid" = "$$" ] && continue          # belt and braces: never ourselves
        kill "-${sig}" "$pid" 2>/dev/null
    done
}

cancel_child() {  # $1 = logfile
    local logf="$1" aid

    # ORDER MATTERS, and it is the opposite of the obvious one.
    #
    # Kill the orchestrator FIRST, scancel second. If the array is cancelled while
    # the orchestrator still lives, it sees the job vanish from squeue, concludes the
    # wave finished, runs its verify step, finds the whole wave missing from S3 — and
    # fires its own "resubmit once" retry. You cancel a wave and a fresh one appears.
    # A dead orchestrator cannot react to anything.
    local waited=0
    if [ -n "${CHILD_PID:-}" ]; then
        log_line "  stopping orchestrator pid ${CHILD_PID} and its children" \
                 "(driver pid $$, pgid ${SELF_PGID})"
        kill_tree TERM "$CHILD_PID"
        while kill -0 "$CHILD_PID" 2>/dev/null && [ "$waited" -lt 10 ]; do
            sleep 1; waited=$((waited + 1))
        done
        if kill -0 "$CHILD_PID" 2>/dev/null; then
            log_line "  did not exit on TERM after ${waited}s — sending KILL"
            kill_tree KILL "$CHILD_PID"
            sleep 1
        fi
        if kill -0 "$CHILD_PID" 2>/dev/null; then
            log_line "  WARNING: orchestrator pid ${CHILD_PID} is still alive."
            log_line "           Check by hand: pgrep -af 'submit-(ersilia|singularity)-waves'"
        else
            log_line "  orchestrator stopped"
        fi
    fi

    # Now that nothing can resubmit, drop whatever it left on the queue. Ids come
    # from THIS job's log only, so we can never scancel something we did not start.
    for aid in $(log_submitted_ids "$logf"); do
        log_line "  scancel ${aid}"
        scancel "$aid" 2>/dev/null
    done
}

# Run one queue entry to completion (or cancellation). Returns the child's rc,
# or 130 if it was cancelled.
run_job() {  # $1 = index
    local i="$1"
    local model="${Q_MODEL[i]}" mode="${Q_MODE[i]}" lib="${Q_LIB[i]}"
    local wave="${Q_WAVE[i]}" queue="${Q_QUEUE[i]}" key="${Q_KEY[i]}"
    local cpus="${Q_CPUS[i]:-}"
    local script rc cancelled=0 last_refresh=0 nowsec cancelled_by=""

    log_line "----- ${model} (${mode}) on ${lib}  [queue pos $((i + 1))/${#Q_MODEL[@]}]${cpus:+  cpus=${cpus}} -----"

    # resume fast-skip: already complete in S3?
    Q_TOTAL[i]="$(s3_count_input "$lib")"
    Q_DONE[i]="$(s3_count_output "$model" "$lib" "$mode")"
    if [ "${Q_TOTAL[i]}" -gt 0 ] && [ "${Q_DONE[i]}" -ge "${Q_TOTAL[i]}" ]; then
        Q_FIN[i]="$(now_iso)"
        set_status "$i" done "already complete in S3"
        log_line "  already complete in S3 (${Q_DONE[i]}/${Q_TOTAL[i]}) — skipping dispatch"
        return 0
    fi

    Q_START[i]="$(now_iso)"; Q_FIN[i]="-"
    set_status "$i" running ""

    # pre-flight: SIF present (no download unless AUTO_FETCH_SIF=1).
    # Skipped under --dry-run: a dry run must not depend on $SIF_DIR, so the
    # dispatch/poll/cancel path stays testable off-cluster.
    if [ "$DRY_RUN" -eq 1 ]; then
        log_line "  [dry-run] skipping SIF pre-flight for ${model}"
    elif ! ensure_sif "$model" "${Q_LOG[i]}"; then
        Q_FIN[i]="$(now_iso)"
        set_status "$i" missing-files "SIF not found: ${SIF_DIR}/${model}.sif"
        log_line "  SIF not found: ${SIF_DIR}/${model}.sif — missing-files, continuing"
        return 0
    fi
    # pre-flight: input library must have chunks in S3
    if [ "${Q_TOTAL[i]}" -le 0 ]; then
        Q_FIN[i]="$(now_iso)"
        set_status "$i" missing-files "no input chunks in s3://${S3_BUCKET}/input/${lib}/"
        log_line "  no input chunks in s3://${S3_BUCKET}/input/${lib}/ — missing-files, continuing"
        return 0
    fi

    script="${WAVES_DIR}/$(mode_script "$mode")"

    # Launch in its own process group (setsid) so a cancel can take down the whole
    # tree, and in the background so this driver keeps servicing control requests.
    # Output goes to the per-job log; `tee` would break pid/pgid tracking.
    # cpus travels as an ENV var, not a 5th positional. The orchestrators take
    # positionals in a fixed order and an older deployed copy would read a 5th one as
    # nothing at all; an env var it does not know about is simply ignored, so a
    # partially-synced /shared degrades to "no override" instead of to a wrong
    # partition. Empty means "pass nothing", leaving the worker's #SBATCH in charge.
    if [ "$DRY_RUN" -eq 1 ]; then
        log_line "  [dry-run] S3_BUCKET=$S3_BUCKET POLL_SECONDS=$POLL_SECONDS ${cpus:+CPUS_PER_TASK=$cpus }$script $model $lib $wave $queue"
        # A real sleeping child, so the poll/cancel path is genuinely exercised.
        setsid sleep "${SCHED_FAKE_DURATION:-30}" >>"${Q_LOG[i]}" 2>&1 8>&- &
    else
        log_line "  dispatch: ${cpus:+CPUS_PER_TASK=$cpus }$script $model $lib $wave $queue  (log: ${Q_LOG[i]})"
        # 8>&- : the orchestrator must not inherit the driver lock. A flock lives
        # as long as ANY copy of its fd, so an orphan outliving a killed driver
        # would otherwise keep every new driver from starting for hours.
        S3_BUCKET="$S3_BUCKET" POLL_SECONDS="$POLL_SECONDS" CPUS_PER_TASK="$cpus" \
            setsid "$script" "$model" "$lib" "$wave" "$queue" >>"${Q_LOG[i]}" 2>&1 8>&- &
    fi
    CHILD_PID=$!
    CHILD_PGID="$(ps -o pgid= -p "$CHILD_PID" 2>/dev/null | tr -d ' ')"
    : "${CHILD_PGID:=$CHILD_PID}"
    # The EXIT trap has no access to $i, so publish what it needs to clean up.
    CURRENT_LOG="${Q_LOG[i]}"
    CURRENT_KEY="$key"
    log_line "  orchestrator pid=${CHILD_PID} pgid=${CHILD_PGID:-<unknown>} (driver pgid=${SELF_PGID})"

    while kill -0 "$CHILD_PID" 2>/dev/null; do
        drain_control
        if [ "$SHUTDOWN" -eq 1 ] || cancel_wanted "$key"; then
            cancelled=1
            cancelled_by="$(cancel_who "$key" 2>/dev/null || true)"
            cancel_child "${Q_LOG[i]}"
            cancel_clear "$key"
            break
        fi
        nowsec="$(date -u +%s)"
        if [ -n "${FORCE_REFRESH:-}" ] || [ $((nowsec - last_refresh)) -ge "$REFRESH_SECONDS" ]; then
            FORCE_REFRESH=""
            last_refresh="$nowsec"
            Q_DONE[i]="$(s3_count_output "$model" "$lib" "$mode")"
            persist_job "$i"
        fi
        sleep "$CTL_POLL"
    done

    wait "$CHILD_PID" 2>/dev/null; rc=$?
    [ "$DRY_RUN" -eq 1 ] && [ "$cancelled" -eq 0 ] && rc="${FAKE_RC[i]:-0}"
    CHILD_PID=""; CHILD_PGID=""; CURRENT_LOG=""; CURRENT_KEY=""

    Q_FIN[i]="$(now_iso)"
    Q_DONE[i]="$(s3_count_output "$model" "$lib" "$mode")"

    if [ "$cancelled" -eq 1 ]; then
        set_status "$i" cancelled \
            "cancelled${cancelled_by:+ by ${cancelled_by}} at ${Q_FIN[i]}"
        log_line "  CANCELLED (${Q_DONE[i]}/${Q_TOTAL[i]})${cancelled_by:+ — requested by ${cancelled_by}}"
        return 130
    fi
    if [ "$rc" -eq 0 ]; then
        set_status "$i" done ""
        log_line "  done (${Q_DONE[i]}/${Q_TOTAL[i]})"
    else
        set_status "$i" failed "orchestrator exited rc=$rc"
        log_line "  FAILED rc=$rc (${Q_DONE[i]}/${Q_TOTAL[i]})"
    fi
    return "$rc"
}

# =============================================================================
# Main loop
# =============================================================================

# First index whose status is pending and which is not held. Echoes nothing when
# there is no runnable job.
next_runnable() {
    local i
    for i in "${!Q_MODEL[@]}"; do
        [ "${Q_HOLD[i]}" = "1" ] && continue
        [ "${Q_STATUS[i]}" = "pending" ] && { echo "$i"; return 0; }
    done
    return 1
}

summary() {
    local i s
    declare -A c=()
    for i in "${!Q_MODEL[@]}"; do s="${Q_STATUS[i]}"; c[$s]=$(( ${c[$s]:-0} + 1 )); done
    local line=""
    for s in $SCHED_STATUSES; do
        [ -n "${c[$s]:-}" ] && line+="${s}=${c[$s]}  "
    done
    echo "=========================================="
    echo "Scheduler summary: ${line:-(no jobs)}"
    echo "  State file : $STATE_FILE"
    echo "  Live table : ${SCRIPT_DIR}/scheduler-status.sh $STATE_FILE"
    echo "=========================================="
}

RC=0
FAILS=0
IDLE_ANNOUNCED=0

# A SIGKILLed driver cannot run its trap, so an orchestrator can still be orphaned.
# Starting a second model alongside it is the worst outcome, so say so loudly — the
# operator can then kill it, or let it finish before resuming.
warn_orphan_orchestrators() {
    local pids
    pids="$(pgrep -u "$(id -u)" -f 'submit-(ersilia|singularity)-waves\.sh' 2>/dev/null | tr '\n' ' ')"
    [ -n "$pids" ] || return 0
    echo "=========================================="
    echo "WARNING: a wave orchestrator is ALREADY RUNNING (pid(s): ${pids})"
    echo "  A previous driver was killed without cleaning up, or someone started one"
    echo "  by hand. If you let this driver proceed, TWO models will run at once and"
    echo "  fight over nodes and /fsx."
    echo "  To stop the stray one:  kill ${pids}"
    echo "  Then check for its SLURM jobs:  squeue -u \$USER"
    echo "=========================================="
}
warn_orphan_orchestrators

# Startup reconciliation, in this order: throw away messages meant for the dead
# driver, THEN adopt the jobs it left behind. Reversed, a stale `cancel` naming a
# just-reclaimed job would cancel it on the first tick.
discard_stale_control
reclaim_stale_running

# ---- announce ourselves so sched-ctl.sh / the TUI can find this instance ----
#
# This MUST be the last thing that happens before the main loop, not the first.
# driver.info is the signal an external client polls for "the driver is up, go
# ahead and post a command" — write it any earlier and there is a real window,
# not just a theoretical one, where a client sees a ready driver, posts (say)
# `stop-after-current`, and then discard_stale_control — which has not run yet
# from this driver's own perspective — throws it away as if it predated
# startup. A client fast enough to act within a few bash statements of
# driver.info appearing hits this reliably, not just occasionally.
write_driver_info <<EOF
pid=$$
queue_file=$QUEUE_FILE
log_dir=$LOG_DIR
state_file=$STATE_FILE
status_file=$STATUS_FILE
script_dir=$SCRIPT_DIR
s3_bucket=$S3_BUCKET
default_library=$DEFAULT_LIBRARY
default_wave_size=$DEFAULT_WAVE_SIZE
default_queue=$DEFAULT_QUEUE
on_fail=$ON_FAIL
dry_run=$DRY_RUN
exit_when_empty=$EXIT_WHEN_EMPTY
tmux_session=${SCHEDULER_TMUX:-}
started=$(now_iso)
EOF

echo "=========================================="
echo "Wave scheduler (driver)"
echo "  Queue      : $QUEUE_FILE   (re-read before every job)"
echo "  Default lib: ${DEFAULT_LIBRARY:-<none>}   wave=$DEFAULT_WAVE_SIZE   queue=$DEFAULT_QUEUE"
echo "  Orchestr.  : $WAVES_DIR/submit-{ersilia,singularity}-waves.sh"
echo "  On fail    : $ON_FAIL     Auto-fetch SIF: $AUTO_FETCH_SIF     Dry-run: $DRY_RUN"
echo "  State file : $STATE_FILE"
echo "  Control    : ${SCRIPT_DIR}/sched-ctl.sh   (add / rm / top / hold / cancel / pause)"
echo "  Idle mode  : $([ "$EXIT_WHEN_EMPTY" = "1" ] && echo 'exit when queue empty' || echo 'stay up and wait for queue changes')"
echo "=========================================="

while :; do
    drain_control
    if [ "$SHUTDOWN" -eq 1 ]; then
        log_line "shutdown requested — exiting."
        break
    fi

    if is_paused; then
        if [ "$IDLE_ANNOUNCED" != "paused" ]; then
            log_line "PAUSED (sched-ctl.sh resume to continue)"
            IDLE_ANNOUNCED=paused
        fi
        sleep "$CTL_POLL"
        continue
    fi

    parse_queue
    merge_status
    write_state

    if [ "${#Q_MODEL[@]}" -eq 0 ]; then
        if [ "$EXIT_WHEN_EMPTY" = "1" ]; then
            echo "Queue is empty (all blank/comment lines) — nothing to do."
            break
        fi
        if [ "$IDLE_ANNOUNCED" != "empty" ]; then
            log_line "queue file has no jobs — waiting for additions (sched-ctl.sh add ...)"
            IDLE_ANNOUNCED=empty
        fi
        sleep "$IDLE_POLL"
        continue
    fi

    if ! IDX="$(next_runnable)"; then
        if [ "$STOP_AFTER" -eq 1 ]; then
            log_line "stop-after-current satisfied and nothing runnable — exiting."
            rm -f "$(stopafter_flag)"
            break
        fi
        if [ "$EXIT_WHEN_EMPTY" = "1" ]; then
            break
        fi
        if [ "$IDLE_ANNOUNCED" != "idle" ]; then
            log_line "nothing runnable (all done/failed/held) — waiting for queue changes"
            IDLE_ANNOUNCED=idle
        fi
        sleep "$IDLE_POLL"
        continue
    fi
    IDLE_ANNOUNCED=0

    run_job "$IDX"; JOB_RC=$?
    if [ "$JOB_RC" -ne 0 ] && [ "$JOB_RC" -ne 130 ]; then
        FAILS=$((FAILS + 1))
        if [ "$ON_FAIL" = "halt" ]; then
            log_line "ON_FAIL=halt — stopping the queue."
            RC=1
            break
        fi
    fi

    if [ "$STOP_AFTER" -eq 1 ]; then
        log_line "stop-after-current — exiting after ${Q_MODEL[IDX]}."
        rm -f "$(stopafter_flag)"
        break
    fi
done

[ "$FAILS" -gt 0 ] && RC=1
summary
exit "$RC"
