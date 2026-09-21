#!/bin/bash
# =============================================================================
# Shared helpers for the wave scheduler.
# =============================================================================
# SOURCED (not executed) by run-model-queue.sh (driver), sched-ctl.sh (control
# CLI) and scheduler-status.sh (renderer). Keeping the per-mode S3-counting logic
# and the on-disk formats here means the three can never drift on how "done" is
# measured or how the queue/status files are laid out.
#
# The caller must have S3_BUCKET set before calling the s3_* helpers.
# write_state() additionally operates on the driver's Q_* arrays + STATE_FILE.
#
# On-disk layout under $LOG_DIR:
#   state.tsv      render view, queue-order (schema unchanged since v1)
#   status.tsv     durable status store, keyed model|mode|library
#   driver.info    key=value facts about the live driver (pid, queue path, ...)
#   control/       paused / stop-after-current flag files + one-shot messages
#   .queue.lock    flock target guarding every queue-file read/write
#   .lock/         atomic-mkdir single-driver lock (driver only)
# =============================================================================

# ISO-8601 UTC timestamp, e.g. 2026-07-23T09:01:22Z
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# All the statuses the scheduler can assign, in display order. Single source of
# truth for the summary lines in the driver, the renderer and the TUI.
SCHED_STATUSES="done running pending held failed cancelled missing-files skipped"

# --- fake-S3 test mode -------------------------------------------------------
# SCHED_FAKE_S3=1 makes the s3_count_* helpers read from a local fixture instead
# of calling `aws s3 ls`, so the whole scheduler can be exercised with no AWS
# credentials and no cluster. Fixture: $SCHED_FAKE_S3_FILE, one line per entry
#   input  <library>                 <count>
#   output <model> <library> <mode>  <count>
# A missing fixture or missing line counts as 0.
SCHED_FAKE_S3="${SCHED_FAKE_S3:-0}"
SCHED_FAKE_S3_FILE="${SCHED_FAKE_S3_FILE:-${LOG_DIR:-/tmp}/fake-s3.txt}"

_fake_s3_count() {  # $1 = kind (input|output), $2.. = key fields
    local kind="$1"; shift
    [ -f "$SCHED_FAKE_S3_FILE" ] || { echo 0; return 0; }
    local want="$kind $*" line k n
    while IFS= read -r line; do
        case "$line" in ''|'#'*) continue ;; esac
        # split the trailing count off the key so keys may not contain spaces
        n="${line##* }"; k="${line% *}"
        if [ "$k" = "$want" ]; then echo "${n:-0}"; return 0; fi
    done < "$SCHED_FAKE_S3_FILE"
    echo 0
}

# Count a library's input chunks in S3:  <lib>_chunk_<NNN>.csv
# Digit-count agnostic (3-digit small libs and 6-digit 1.4B both match).
#
# CACHED, because this is the single most expensive call in the whole scheduler and
# the answer is effectively static: a library's chunk count only changes when the
# library is re-ingested. Listing 13,639 objects is ~14 paged API calls plus AWS CLI
# startup, and a full recount would otherwise pay that for every row sharing the
# library. Set SCHED_INPUT_CACHE_TTL=0 to force a fresh count.
s3_count_input() {  # $1 = library
    if [ "$SCHED_FAKE_S3" = "1" ]; then _fake_s3_count input "$1"; return 0; fi

    local lib="$1" ttl="${SCHED_INPUT_CACHE_TTL:-3600}"
    local cache="${LOG_DIR:-/tmp}/.input-counts" now val ts key
    now="$(date -u +%s)"

    if [ "$ttl" -gt 0 ] && [ -f "$cache" ]; then
        while IFS=$'\t' read -r ts val key; do
            [ "$key" = "$lib" ] || continue
            if [ -n "$ts" ] && [ $((now - ts)) -lt "$ttl" ]; then
                echo "$val"; return 0
            fi
            break
        done < "$cache"
    fi

    val="$(aws s3 ls "s3://${S3_BUCKET}/input/${lib}/" 2>/dev/null \
           | grep -cP '_chunk_[0-9]+\.csv$' || true)"
    val="${val:-0}"

    # Only cache a real answer: caching a 0 from a transient AWS failure would make
    # every job look like it has no input until the TTL expired.
    if [ "$val" -gt 0 ] && [ -n "${LOG_DIR:-}" ] && [ -d "$LOG_DIR" ]; then
        {
            [ -f "$cache" ] && awk -F'\t' -v L="$lib" '$3 != L' "$cache"
            printf '%s\t%s\t%s\n' "$now" "$val" "$lib"
        } > "${cache}.tmp.$$" 2>/dev/null && mv -f "${cache}.tmp.$$" "$cache" 2>/dev/null
    fi
    echo "$val"
}

# Count a model's result files in S3, per run mode.
#   ersilia     -> <model>_results_<NNN>.csv
#   singularity -> <model>_<NNN>.csv         (NO _results_)
# The two patterns never cross-count: after "<model>_", ersilia has the letters
# "results", not a digit, so the singularity regex can't match an ersilia file.
s3_count_output() {  # $1 = model  $2 = library  $3 = mode
    if [ "$SCHED_FAKE_S3" = "1" ]; then _fake_s3_count output "$1" "$2" "$3"; return 0; fi
    local pat
    case "$3" in
        ersilia)     pat="${1}_results_[0-9]+\.csv$" ;;
        singularity) pat="${1}_[0-9]+\.csv$" ;;
        *)           echo 0; return 0 ;;
    esac
    aws s3 ls "s3://${S3_BUCKET}/output/${2}/${1}/" 2>/dev/null \
        | grep -cP "$pat" || true
}

# List the libraries that actually exist in S3, i.e. the prefixes under input/.
#
# This is the authoritative answer to "what can I run against?" — the alias table
# only knows the five hand-registered names, so anything ingested since (the h3d
# selections, the 44g subsets, the 1.4B) was invisible to the add dialog.
#
# Cheap: `aws s3 ls` on a prefix with a trailing slash returns COMMON PREFIXES
# (`PRE name/`), a dozen or so lines, not the millions of objects beneath them.
# Cached anyway (default 1 h, SCHED_LIB_CACHE_TTL=0 to force) because the plain
# dump the TUI polls every 2 s must never call AWS.
s3_list_libraries() {  # $1 = any non-empty value forces a refresh
    local force="${1:-}" cache="${LOG_DIR:-/tmp}/.libraries"
    local ttl="${SCHED_LIB_CACHE_TTL:-3600}" age=999999 tmp

    if [ "$SCHED_FAKE_S3" = "1" ]; then
        awk '$1 == "input" { print $2 }' "$SCHED_FAKE_S3_FILE" 2>/dev/null | sort -u
        return 0
    fi

    if [ -f "$cache" ]; then
        age=$(( $(date -u +%s) - $(stat -c %Y "$cache" 2>/dev/null || echo 0) ))
    fi
    if [ -n "$force" ] || [ ! -f "$cache" ] || [ "$age" -ge "$ttl" ]; then
        tmp="${cache}.tmp.$$"
        # Only replace the cache with a non-empty result: a transient AWS failure
        # must not blank the dropdown for the next hour.
        if aws s3 ls "s3://${S3_BUCKET}/input/" 2>/dev/null \
             | awk '$1 == "PRE" { sub(/\/$/, "", $2); print $2 }' \
             | sort -u > "$tmp" && [ -s "$tmp" ]; then
            mv -f "$tmp" "$cache" 2>/dev/null
        else
            rm -f "$tmp" 2>/dev/null
        fi
    fi
    [ -f "$cache" ] && cat "$cache"
    return 0
}

# Map a run mode to its wave-orchestrator script basename.
mode_script() {  # $1 = mode
    case "$1" in
        ersilia)     echo "submit-ersilia-waves.sh" ;;
        singularity) echo "submit-singularity-waves.sh" ;;
        *)           return 1 ;;
    esac
}

# Atomically (re)write the whole state TSV from the driver's Q_* arrays.
# Temp file in the SAME dir + mv -f == atomic rename, so the renderer never
# reads a half-written table.
# Globals: STATE_FILE, Q_MODEL Q_MODE Q_LIB Q_STATUS Q_DONE Q_TOTAL Q_START Q_FIN Q_LOG
write_state() {
    local tmp="${STATE_FILE}.tmp.$$" i
    {
        printf '#idx\tmodel\tmode\tlibrary\tstatus\tdone\ttotal\tstarted\tfinished\tlog\n'
        for i in "${!Q_MODEL[@]}"; do
            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                "$((i + 1))" "${Q_MODEL[i]}" "${Q_MODE[i]}" "${Q_LIB[i]}" \
                "${Q_STATUS[i]}" "${Q_DONE[i]}" "${Q_TOTAL[i]}" \
                "${Q_START[i]}" "${Q_FIN[i]}" "${Q_LOG[i]}"
        done
    } > "$tmp" && mv -f "$tmp" "$STATE_FILE"
}

# =============================================================================
# Queue file
# =============================================================================

# Path of the flock target guarding the queue file. Lives beside the other
# scheduler state so a single LOG_DIR fully describes one scheduler instance.
queue_lock_file() { echo "${LOG_DIR}/.queue.lock"; }

# Run a command while holding an exclusive lock on the queue file.
#   queue_locked <command...>
# Uses fd 9. flock is in util-linux and present on AL2/AL2023 and on macOS only
# via util-linux, so fall back to a bounded mkdir spin when it is missing.
queue_locked() {
    # Re-entrancy guard. Nesting would run `exec 9>>` a second time, which replaces
    # fd 9's open file description and RELEASES the outer flock — the caller would
    # carry on believing it still held the lock. Running the inner command directly
    # is correct: we already hold it.
    if [ "${QUEUE_LOCK_DEPTH:-0}" -gt 0 ]; then
        "$@"
        return $?
    fi

    mkdir -p "$LOG_DIR"
    local lf; lf="$(queue_lock_file)"
    if command -v flock >/dev/null 2>&1; then
        exec 9>>"$lf" || { echo "ERROR: cannot open queue lock $lf" >&2; return 1; }
        if ! flock -w 30 9; then
            echo "ERROR: timed out waiting for queue lock $lf" >&2
            exec 9>&-
            return 1
        fi
        QUEUE_LOCK_DEPTH=1
        "$@"; local rc=$?
        QUEUE_LOCK_DEPTH=0
        exec 9>&-
        return "$rc"
    fi
    local spin=0 d="${lf}.d"
    until mkdir "$d" 2>/dev/null; do
        spin=$((spin + 1))
        [ "$spin" -gt 300 ] && { echo "ERROR: timed out waiting for queue lock $d" >&2; return 1; }
        sleep 0.1
    done
    QUEUE_LOCK_DEPTH=1
    "$@"; local rc=$?
    QUEUE_LOCK_DEPTH=0
    rmdir "$d" 2>/dev/null
    return "$rc"
}

# Is this token a flag rather than a positional field?
# Flags are `key=value` pairs or a small set of bare words. Recognising them by
# SHAPE rather than by position is what lets `eos1 ersilia mylib hold` work: the
# obvious way to write it by hand would otherwise put `hold` in the wave_size
# slot and the whole line would be rejected as "wave_size out of 1..1000".
is_queue_flag() {  # $1 = token
    case "$1" in
        hold)  return 0 ;;
        *=*)   return 0 ;;
        *)     return 1 ;;
    esac
}

# Upper bound for a per-job cpus-per-task override. The cpu-queue instance types
# are 32-vCPU, so 32 means "one task per node" — the least dense, most memory-safe
# packing available. Anything higher would make every task unschedulable.
MAX_CPUS_PER_TASK="${MAX_CPUS_PER_TASK:-32}"

# Value of a `key=value` queue flag, or empty with rc=1 when absent.
# One implementation, because the driver reads these to build an sbatch line and
# ctl reads them to render the same job — a disagreement would mean the dashboard
# shows a packing the cluster is not using.
queue_flag_value() {  # $1 = flags string, $2 = key
    local tok
    for tok in $1; do
        case "$tok" in
            "$2"=*) printf '%s' "${tok#*=}"; return 0 ;;
        esac
    done
    return 1
}

# Is this a usable cpus-per-task override?
#
# Validated in two places on purpose: ctl rejects bad input at write time, and the
# driver re-checks at read time because the queue file is hand-editable and a bad
# value must degrade to a `skipped` verdict rather than an sbatch that fails 1000
# times in an array.
is_valid_cpus() {  # $1 = candidate
    [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le "$MAX_CPUS_PER_TASK" ]
}

# Parse one queue line into the QL_* globals. Returns 1 for blank/comment lines.
#   <model_id> <mode> [library] [wave_size] [queue] [flags...]
# Flags may appear anywhere after the model id; non-flag tokens fill the
# positional fields in order. Unknown flags are preserved verbatim in QL_FLAGS so
# a rewrite never silently drops one.
#
# `cpus=N` is lifted into QL_CPUS *and* left in QL_FLAGS, exactly like `hold` is
# lifted into QL_HOLD: the flags string is what gets written back, so removing the
# token here would silently drop the override on the next rewrite.
# Globals set: QL_MODEL QL_MODE QL_LIB QL_WAVE QL_QUEUE QL_FLAGS QL_HOLD QL_CPUS
parse_queue_line() {  # $1 = raw line
    local raw="$1" trimmed tok
    trimmed="${raw#"${raw%%[![:space:]]*}"}"            # left-trim whitespace
    case "$trimmed" in ''|'#'*) return 1 ;; esac        # blank / comment
    QL_MODEL=""; QL_MODE=""; QL_LIB=""; QL_WAVE=""; QL_QUEUE=""; QL_FLAGS=""; QL_HOLD=0
    QL_CPUS=""

    local -a positional=() flags=()
    for tok in $trimmed; do
        if [ -z "$QL_MODEL" ]; then
            QL_MODEL="$tok"                             # first token is always the model
        elif is_queue_flag "$tok"; then
            flags+=("$tok")
        elif [ "${#positional[@]}" -lt 4 ]; then
            positional+=("$tok")
        else
            flags+=("$tok")                             # overflow: keep, don't lose
        fi
    done
    [ -n "$QL_MODEL" ] || return 1

    QL_MODE="${positional[0]:-}"; QL_LIB="${positional[1]:-}"
    QL_WAVE="${positional[2]:-}"; QL_QUEUE="${positional[3]:-}"
    QL_FLAGS="${flags[*]:-}"
    for tok in ${QL_FLAGS}; do
        case "$tok" in hold|hold=1|hold=true) QL_HOLD=1 ;; esac
    done
    QL_CPUS="$(queue_flag_value "$QL_FLAGS" cpus)" || QL_CPUS=""
    return 0
}

# Compose a queue line from fields. Keeps the columns aligned like example.queue
# so a hand-edited file and a ctl-edited file look the same. When there are flags
# the positional columns stay padded, so the flags line up in their own column
# even for entries that left wave/queue at the default.
format_queue_line() {  # model mode library wave queue flags
    local line
    line=$(printf '%-19s %-12s %-26s %-5s %-10s' "$1" "$2" "${3:-}" "${4:-}" "${5:-}")
    if [ -n "${6:-}" ]; then
        line="${line} ${6}"
    else
        line="${line%"${line##*[![:space:]]}"}"         # right-trim
    fi
    printf '%s\n' "$line"
}

# The durable key for a job. Must match between driver, ctl and TUI.
job_key() { printf '%s|%s|%s\n' "$1" "$2" "$3"; }   # model mode library

# =============================================================================
# Status store  ($LOG_DIR/status.tsv)
# =============================================================================
# Keyed by model|mode|library so it survives queue reordering, queue edits and
# driver restarts. state.tsv is a render view of (queue order x this store).
#
# Loaded into the ST_* associative arrays; the caller must have declared them
# (status_load does it if they are missing).

status_file() { echo "${STATUS_FILE:-${LOG_DIR}/status.tsv}"; }

# Populate ST_STATUS/ST_DONE/ST_TOTAL/ST_START/ST_FIN/ST_LOG/ST_NOTE by key.
status_load() {
    declare -gA ST_STATUS=() ST_DONE=() ST_TOTAL=() ST_START=() ST_FIN=() ST_LOG=() ST_NOTE=()
    local f; f="$(status_file)"
    [ -f "$f" ] || return 0
    local key st dn tt sa fi lg nt
    while IFS=$'\t' read -r key st dn tt sa fi lg nt; do
        case "$key" in ''|'#'*) continue ;; esac
        ST_STATUS["$key"]="$st"; ST_DONE["$key"]="${dn:-0}"; ST_TOTAL["$key"]="${tt:-0}"
        ST_START["$key"]="${sa:--}"; ST_FIN["$key"]="${fi:--}"
        ST_LOG["$key"]="${lg:-}";   ST_NOTE["$key"]="${nt:-}"
    done < "$f"
}

# Atomically write the ST_* arrays back out.
status_write() {
    local f tmp key
    f="$(status_file)"; tmp="${f}.tmp.$$"
    mkdir -p "$(dirname "$f")"
    {
        printf '#key\tstatus\tdone\ttotal\tstarted\tfinished\tlog\tnote\n'
        for key in "${!ST_STATUS[@]}"; do
            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                "$key" "${ST_STATUS[$key]}" "${ST_DONE[$key]:-0}" "${ST_TOTAL[$key]:-0}" \
                "${ST_START[$key]:--}" "${ST_FIN[$key]:--}" \
                "${ST_LOG[$key]:-}" "${ST_NOTE[$key]:-}"
        done | sort
    } > "$tmp" && mv -f "$tmp" "$f"
}

# Read-modify-write one key under the queue lock. Safe to call from ctl while the
# driver runs: both go through queue_locked, so writes never interleave.
#   status_update <key> <status> [note]
status_update() {
    local key="$1" st="$2" note="${3:-}"
    _do() {
        status_load
        ST_STATUS["$key"]="$st"
        ST_NOTE["$key"]="$note"
        : "${ST_DONE[$key]:=0}"; : "${ST_TOTAL[$key]:=0}"
        : "${ST_START[$key]:=-}"; : "${ST_FIN[$key]:=-}"; : "${ST_LOG[$key]:=}"
        status_write
    }
    queue_locked _do
}

# Drop one key entirely (used by `retry`, so the driver re-derives from S3).
status_forget() {
    local key="$1"
    _do() {
        status_load
        unset "ST_STATUS[$key]" "ST_DONE[$key]" "ST_TOTAL[$key]" \
              "ST_START[$key]" "ST_FIN[$key]" "ST_LOG[$key]" "ST_NOTE[$key]"
        status_write
    }
    queue_locked _do
}

# =============================================================================
# Control channel + driver.info
# =============================================================================

control_dir()   { echo "${LOG_DIR}/control"; }
driver_info()   { echo "${LOG_DIR}/driver.info"; }
paused_flag()   { echo "$(control_dir)/paused"; }
stopafter_flag(){ echo "$(control_dir)/stop-after-current"; }

# Write driver.info atomically. Called by the driver at startup and on changes.
write_driver_info() {  # key=value pairs on stdin
    local f tmp
    f="$(driver_info)"; tmp="${f}.tmp.$$"
    mkdir -p "$(dirname "$f")"
    cat > "$tmp" && mv -f "$tmp" "$f"
}

# Read driver.info into DI_* variables (DI_pid, DI_queue_file, ...).
read_driver_info() {
    local f k v
    f="$(driver_info)"
    [ -f "$f" ] || return 1
    while IFS='=' read -r k v; do
        case "$k" in ''|'#'*) continue ;; esac
        # only accept identifier-shaped keys — driver.info is ours, but this file
        # is read by the TUI's transport too and eval deserves a guard
        [[ "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        printf -v "DI_${k}" '%s' "$v"
    done < "$f"
    return 0
}

# PID of a running driver process owned by this user, if any.
# Used as a fallback when driver.info is absent — which is exactly the case while a
# PRE-UPGRADE driver is still running: it never wrote driver.info, and reporting it
# as stopped would show its in-flight model as a phantom.
driver_pid_scan() {
    pgrep -u "$(id -u)" -f 'run-model-queue\.sh' 2>/dev/null | head -n 1
}

# Is a driver actually alive?
driver_alive() {
    local pid
    if read_driver_info && [ -n "${DI_pid:-}" ]; then
        kill -0 "$DI_pid" 2>/dev/null && return 0
    fi
    pid="$(driver_pid_scan)"
    [ -n "$pid" ]
}

# Is the live driver a pre-upgrade one (running, but no driver.info)?
# It matters a lot for the UI: an old driver parses the queue ONCE at startup, so
# queue edits will appear to do nothing until it is restarted on the new version.
driver_is_legacy() {
    read_driver_info && [ -n "${DI_pid:-}" ] && kill -0 "$DI_pid" 2>/dev/null && return 1
    [ -n "$(driver_pid_scan)" ]
}

# Post a one-shot control message for the driver to consume.
#   control_post <verb> [payload]
control_post() {
    local d; d="$(control_dir)"
    mkdir -p "$d"
    local f="${d}/$(date -u +%s%N).$$.$1"
    printf '%s\n' "${2:-}" > "${f}.tmp" && mv -f "${f}.tmp" "$f"
}
