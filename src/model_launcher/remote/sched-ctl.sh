#!/bin/bash
# =============================================================================
# Wave scheduler — control CLI.
# =============================================================================
# Safely mutates a LIVE queue while run-model-queue.sh is running. The driver
# re-reads the queue file before every job, so edits made here take effect at the
# next job boundary (immediately, for pause/cancel).
#
# Line order in the queue file IS the priority — "prioritize" means "move up".
# Every read/write takes the same flock the driver uses, so nothing interleaves.
#
# Comment/blank lines are treated as belonging to the job line BELOW them, and
# move with it. Your annotations follow their job.
#
# Usage:
#   sched-ctl.sh [-q <queue_file>] [--log-dir <dir>] [--who <name>] <command> [args]
#
# Queue editing (takes effect at the next job boundary):
#   add <model> <mode> [library] [wave] [queue] [--cpus <n>] [--top|--after <n>]
#                                --cpus <n> pins SLURM cpus-per-task (1..32) for this
#                                model only, written as a `cpus=<n>` queue flag.
#                                Omit it to keep the worker's own tuned default.
#   rm       <sel>...            remove job(s)
#   top      <sel>...            move to the front of the queue
#   up       <sel>...            move one position earlier
#   down     <sel>...            move one position later
#   move     <sel> <pos>         move to an absolute 1-based position
#   hold     <sel>...            park a job (driver skips it)
#   unhold   <sel>...            un-park it
#   retry    <sel>...            forget a failed/cancelled verdict so it runs again
#
# Live control (takes effect within CTL_POLL seconds):
#   pause | resume               stop/start picking up new jobs
#   cancel <sel>                 scancel + kill the RUNNING model; hold a pending one
#   stop-after-current           finish the current model, then exit the driver
#   shutdown                     cancel the current model and exit the driver
#   refresh                      ask the driver to re-count S3 progress now
#
# Inspection:
#   list                         the queue with live statuses (no S3 calls)
#   status                       delegates to scheduler-status.sh (live S3 counts)
#   dump [--log <path>] [--live|--live-all]
#                                one machine-readable snapshot (used by the TUI).
#                                --live     recount totals + the running row from S3
#                                --live-all recount every row (slower; on demand)
#
# <sel> is a model id (eos12x7_v1) or a 1-based queue position (3).
#
# Env: LOG_DIR (default /shared/logs/scheduler), S3_BUCKET, QUEUE_FILE.
#      With a driver running, the queue file is discovered from driver.info.
#
# --who identifies the operator for the audit log and cancellation notes.
# Everyone on the cluster shares one unix account, so this has to come from
# outside — the Python client passes its own operator's local username. With
# neither --who nor $SCHED_WHO given, WHO is left EMPTY rather than falling
# back to `id -un`: on the shared account that would always say the same
# thing for everyone, which reads as an answer without being one. The audit
# log renders an empty WHO as the word "unknown"; a cancellation note simply
# omits "by ..." rather than naming the account instead of the person.
# =============================================================================

set -uo pipefail

usage() { sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'; }

# ---- global options ---------------------------------------------------------
CLI_QUEUE=""
CLI_LOG_DIR=""
CLI_WHO=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -q|--queue)   CLI_QUEUE="${2:-}"; shift 2 ;;
        --log-dir)    CLI_LOG_DIR="${2:-}"; shift 2 ;;
        --who)        CLI_WHO="${2:-}"; shift 2 ;;
        -h|--help)    usage; exit 0 ;;
        *)            break ;;
    esac
done
[ "$#" -ge 1 ] || { usage; exit 1; }
CMD="$1"; shift
WHO="${CLI_WHO:-${SCHED_WHO:-}}"

# ---- locate this script's own directory ----
# scheduler.conf and scheduler-lib.sh both live beside it, and the conf has to
# be sourced before ANY ${VAR:-default} below fixes a value it was meant to
# override.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- scheduler.conf: per-machine defaults ----
# A DEFAULTS file, not an assignment file — see scheduler.conf.example.
# Precedence, high to low: CLI flag > environment > this file > the built-in
# default hardcoded below. This must run before `--log-dir` is folded in, so
# an explicit flag still wins over anything the conf sets.
SCHEDULER_CONF="${SCHEDULER_CONF:-${SCRIPT_DIR}/scheduler.conf}"
# shellcheck source=/dev/null
[ -f "$SCHEDULER_CONF" ] && source "$SCHEDULER_CONF"

LOG_DIR="${CLI_LOG_DIR:-${LOG_DIR:-/shared/logs/scheduler}}"
S3_BUCKET="${S3_BUCKET:-ai2050-ersilia-cluster}"
SIF_DIR="${SIF_DIR:-/shared/sif-files}"
DISPATCH="${DISPATCH:-slurm}"

LIB="${SCRIPT_DIR}/scheduler-lib.sh"
[ -f "$LIB" ] || LIB="/shared/scripts/scheduler/scheduler-lib.sh"
# shellcheck source=/dev/null
source "$LIB" || { echo "ERROR: cannot source scheduler-lib.sh ($LIB)"; exit 1; }

STATE_FILE="${STATE_FILE:-${LOG_DIR}/state.tsv}"
STATUS_FILE="${STATUS_FILE:-${LOG_DIR}/status.tsv}"

# ---- library-aliases (resolve_library) ----
# The driver resolves aliases BEFORE keying the status store, so `molport` in the
# queue is stored as Molport_Screening_Compounds_5.3M. We must resolve identically
# or every status lookup for an aliased entry silently misses and reads "pending".
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
    resolve_library() { echo "$1"; }
fi

# The library a job is actually keyed under: explicit field, else the driver's
# default, then alias-resolved — exactly the driver's own order of operations.
effective_library() {  # $1 = raw library field from the queue line
    local lib="$1"
    [ -n "$lib" ] || lib="${DI_default_library:-}"
    [ -n "$lib" ] || { echo ""; return 0; }
    resolve_library "$lib"
}

# ---- resolve the queue file -------------------------------------------------
# Priority: -q flag > $QUEUE_FILE > driver.info (the live driver's own queue).
resolve_queue_file() {
    if [ -n "$CLI_QUEUE" ]; then QUEUE_FILE="$CLI_QUEUE"; return 0; fi
    if [ -n "${QUEUE_FILE:-}" ]; then return 0; fi
    if read_driver_info && [ -n "${DI_queue_file:-}" ]; then
        QUEUE_FILE="$DI_queue_file"
        return 0
    fi
    return 1
}

need_queue() {
    resolve_queue_file || {
        echo "ERROR: no queue file. Pass -q <file>, set QUEUE_FILE, or start a driver." >&2
        exit 1
    }
    [ -f "$QUEUE_FILE" ] || { echo "ERROR: queue file not found: $QUEUE_FILE" >&2; exit 1; }
}

# =============================================================================
# Block model — a job line plus the comment/blank lines directly above it
# =============================================================================
# Reordering must not scramble a hand-annotated file, so the file is modelled as:
#
#   BLK_HEADER    the file's banner: leading comments/blanks up to and INCLUDING
#                 the last blank line before the first job. Always stays on top.
#   BLK_PRE[i]    the comments directly above job i (after that last blank line).
#                 Moves with the job — your per-job notes follow their job.
#   BLK_TAIL      comments/blanks after the last job. Always stays at EOF.
BLK_PRE=(); BLK_MODEL=(); BLK_MODE=(); BLK_LIB=(); BLK_WAVE=(); BLK_QUEUE=(); BLK_FLAGS=()
BLK_HEADER=""
BLK_TAIL=""

# Split the leading pending text into (header, job-attached comment) at the last
# blank line: a banner is separated from the first job's note by a blank line,
# which is exactly how these files are written by hand.
_split_header() {  # $1 = pending text ; sets SPLIT_HEADER / SPLIT_PRE
    local text="$1" line header="" pre="" seen_blank_at=""
    local -a lines=()
    while IFS= read -r line; do lines+=("$line"); done <<< "$text"
    # drop the trailing empty element `<<<` adds when text ends in a newline
    [ "${#lines[@]}" -gt 0 ] && [ -z "${lines[-1]}" ] && unset 'lines[-1]'
    local i
    for i in "${!lines[@]}"; do
        [[ "${lines[i]}" =~ ^[[:space:]]*$ ]] && seen_blank_at="$i"
    done
    for i in "${!lines[@]}"; do
        if [ -n "$seen_blank_at" ] && [ "$i" -le "$seen_blank_at" ]; then
            header+="${lines[i]}"$'\n'
        else
            pre+="${lines[i]}"$'\n'
        fi
    done
    SPLIT_HEADER="$header"; SPLIT_PRE="$pre"
}

load_blocks() {
    BLK_PRE=(); BLK_MODEL=(); BLK_MODE=(); BLK_LIB=(); BLK_WAVE=(); BLK_QUEUE=(); BLK_FLAGS=()
    BLK_HEADER=""; BLK_TAIL=""
    local raw pending="" n first=1
    while IFS= read -r raw || [ -n "$raw" ]; do
        if parse_queue_line "$raw"; then
            n=${#BLK_MODEL[@]}
            if [ "$first" -eq 1 ]; then
                first=0
                if [ -n "$pending" ]; then
                    _split_header "$pending"
                    BLK_HEADER="$SPLIT_HEADER"
                    BLK_PRE[n]="$SPLIT_PRE"
                else
                    BLK_PRE[n]=""
                fi
            else
                BLK_PRE[n]="$pending"
            fi
            BLK_MODEL[n]="$QL_MODEL"; BLK_MODE[n]="$QL_MODE"; BLK_LIB[n]="$QL_LIB"
            BLK_WAVE[n]="$QL_WAVE";   BLK_QUEUE[n]="$QL_QUEUE"; BLK_FLAGS[n]="$QL_FLAGS"
            pending=""
        else
            pending+="${raw}"$'\n'
        fi
    done < "$QUEUE_FILE"
    if [ "$first" -eq 1 ]; then
        BLK_HEADER="$pending"       # a queue with no jobs at all is all header
    else
        BLK_TAIL="$pending"         # comments/blanks after the last job stay at EOF
    fi
}

write_blocks() {
    local tmp="${QUEUE_FILE}.tmp.$$" i
    {
        printf '%s' "$BLK_HEADER"
        for i in "${!BLK_MODEL[@]}"; do
            printf '%s' "${BLK_PRE[i]}"
            format_queue_line "${BLK_MODEL[i]}" "${BLK_MODE[i]}" "${BLK_LIB[i]}" \
                              "${BLK_WAVE[i]}" "${BLK_QUEUE[i]}" "${BLK_FLAGS[i]}"
        done
        printf '%s' "$BLK_TAIL"
    } > "$tmp" && mv -f "$tmp" "$QUEUE_FILE"
}

# Move the block at $1 to position $2 (both 0-based), preserving everything else.
move_block() {
    local from="$1" to="$2" n=${#BLK_MODEL[@]} i
    [ "$to" -lt 0 ] && to=0
    [ "$to" -ge "$n" ] && to=$((n - 1))
    [ "$from" -eq "$to" ] && return 0
    local p="${BLK_PRE[from]}" m="${BLK_MODEL[from]}" md="${BLK_MODE[from]}"
    local l="${BLK_LIB[from]}" w="${BLK_WAVE[from]}" q="${BLK_QUEUE[from]}" f="${BLK_FLAGS[from]}"
    local NP=() NM=() NMD=() NL=() NW=() NQ=() NF=()
    local j=0
    for ((i = 0; i < n; i++)); do
        [ "$i" -eq "$from" ] && continue
        NP[j]="${BLK_PRE[i]}"; NM[j]="${BLK_MODEL[i]}"; NMD[j]="${BLK_MODE[i]}"
        NL[j]="${BLK_LIB[i]}"; NW[j]="${BLK_WAVE[i]}";  NQ[j]="${BLK_QUEUE[i]}"
        NF[j]="${BLK_FLAGS[i]}"
        j=$((j + 1))
    done
    BLK_PRE=(); BLK_MODEL=(); BLK_MODE=(); BLK_LIB=(); BLK_WAVE=(); BLK_QUEUE=(); BLK_FLAGS=()
    j=0
    for ((i = 0; i < n; i++)); do
        if [ "$i" -eq "$to" ]; then
            BLK_PRE+=("$p"); BLK_MODEL+=("$m"); BLK_MODE+=("$md")
            BLK_LIB+=("$l"); BLK_WAVE+=("$w"); BLK_QUEUE+=("$q"); BLK_FLAGS+=("$f")
            continue
        fi
        BLK_PRE+=("${NP[j]}"); BLK_MODEL+=("${NM[j]}"); BLK_MODE+=("${NMD[j]}")
        BLK_LIB+=("${NL[j]}"); BLK_WAVE+=("${NW[j]}"); BLK_QUEUE+=("${NQ[j]}")
        BLK_FLAGS+=("${NF[j]}")
        j=$((j + 1))
    done
}

# Resolve a selector (model id or 1-based position) to 0-based block indices.
# Echoes one index per line; empty output means "no match".
resolve_sel() {  # $1 = selector
    local sel="$1" i found=0 hits=()
    if [[ "$sel" =~ ^[0-9]+$ ]]; then
        i=$((sel - 1))
        if [ "$i" -ge 0 ] && [ "$i" -lt "${#BLK_MODEL[@]}" ]; then echo "$i"; return 0; fi
        return 1
    fi
    for i in "${!BLK_MODEL[@]}"; do
        if [ "${BLK_MODEL[i]}" = "$sel" ]; then echo "$i"; hits+=("$((i + 1))"); found=1; fi
    done
    # The same model can legitimately appear twice against different libraries.
    # Callers act on the first match, so say so rather than let the user believe
    # they held or cancelled the other one.
    if [ "${#hits[@]}" -gt 1 ]; then
        echo "warning: '$sel' matches queue positions ${hits[*]} — acting on ${hits[0]}." >&2
        echo "         Use the position number to pick a specific one." >&2
    fi
    [ "$found" -eq 1 ]
}

# Set or clear the `hold` flag on a block, leaving unknown flags untouched.
set_hold_flag() {  # $1 = index, $2 = 1|0
    local i="$1" want="$2" f out=""
    for f in ${BLK_FLAGS[i]}; do
        case "$f" in hold|hold=1|hold=true|hold=0|hold=false) continue ;; esac
        out="${out}${out:+ }${f}"
    done
    [ "$want" = "1" ] && out="${out}${out:+ }hold"
    BLK_FLAGS[i]="$out"
}

# =============================================================================
# Commands
# =============================================================================

cmd_add() {
    local model="" mode="" lib="" wave="" queue="" where="end" after="" cpus=""
    local pos=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --top)   where="top"; shift ;;
            --after) where="after"; after="${2:-}"; shift 2 ;;
            --cpus)  cpus="${2:-}"; shift 2 ;;
            *)       pos+=("$1"); shift ;;
        esac
    done
    model="${pos[0]:-}"; mode="${pos[1]:-}"; lib="${pos[2]:-}"
    wave="${pos[3]:-}";  queue="${pos[4]:-}"
    [ -n "$model" ] || { echo "ERROR: add needs <model> <mode> [library] [wave] [queue]" >&2; return 1; }
    [ -n "$mode" ]  || { echo "ERROR: add needs a mode (ersilia|singularity)" >&2; return 1; }
    case "$mode" in ersilia|singularity) ;; *)
        echo "ERROR: mode must be ersilia or singularity (got '$mode')" >&2; return 1 ;;
    esac
    if [ -n "$wave" ] && { ! [[ "$wave" =~ ^[0-9]+$ ]] || [ "$wave" -lt 1 ] || [ "$wave" -gt 1000 ]; }; then
        echo "ERROR: wave_size must be 1..1000 (got '$wave')" >&2; return 1
    fi
    # cpus rides as a FLAG, not a positional field. That is deliberate: a sixth
    # positional column would reintroduce exactly the bug invariant #7 describes —
    # a blank middle field is invisible on re-read and every later value shifts left.
    # As `cpus=N` it can sit anywhere after the model id and cannot be confused with
    # a wave size or a partition name.
    if [ -n "$cpus" ] && ! is_valid_cpus "$cpus"; then
        echo "ERROR: --cpus must be 1..${MAX_CPUS_PER_TASK} (got '$cpus')" >&2
        echo "       ${MAX_CPUS_PER_TASK} means one task per 32-vCPU node: slowest, but the" >&2
        echo "       most memory headroom. Omit --cpus to keep the worker's default." >&2
        return 1
    fi

    # Fill in skipped positional fields.
    #
    # Queue fields are whitespace-delimited, so an EMPTY middle field is invisible
    # once written: `model mode <blank> 500` re-reads as library=500, and
    # `model mode lib <blank> gpu-queue` re-reads as wave_size=gpu-queue. Either way
    # the job is silently mis-parsed later — wrong S3 prefix, or rejected as an
    # out-of-range wave. So anything to the left of a value you did give must be
    # materialised from the driver's defaults before the line is written.
    if [ -n "$queue" ] && [ -z "$wave" ]; then
        wave="${DI_default_wave_size:-1000}"
    fi
    if { [ -n "$wave" ] || [ -n "$queue" ]; } && [ -z "$lib" ]; then
        lib="${DI_default_library:-}"
        if [ -z "$lib" ]; then
            echo "ERROR: a wave_size or partition was given without a library, and no" >&2
            echo "       default_library is available (is a driver running?)." >&2
            echo "       Name the library explicitly:  add $model $mode <library> ${wave:-} ${queue:-}" >&2
            return 1
        fi
        echo "note: library not given — using the driver default '$lib'" >&2
    fi

    _do() {
        load_blocks
        # Compare RESOLVED libraries: `molport` and Molport_Screening_Compounds_5.3M
        # are the same job, and so are an explicit library and a blank one that falls
        # back to the same default. A duplicate would give two queue lines one shared
        # status key, so the second silently inherits the first's verdict.
        local i want; want="$(effective_library "$lib")"
        for i in "${!BLK_MODEL[@]}"; do
            if [ "${BLK_MODEL[i]}" = "$model" ] && [ "${BLK_MODE[i]}" = "$mode" ] \
               && [ "$(effective_library "${BLK_LIB[i]}")" = "$want" ]; then
                echo "already queued at position $((i + 1)): $model $mode ${want:-<default>}" >&2
                return 1
            fi
        done
        local n=${#BLK_MODEL[@]}
        BLK_PRE[n]=""; BLK_MODEL[n]="$model"; BLK_MODE[n]="$mode"; BLK_LIB[n]="$lib"
        BLK_WAVE[n]="$wave"; BLK_QUEUE[n]="$queue"
        BLK_FLAGS[n]="${cpus:+cpus=$cpus}"
        case "$where" in
            top)   move_block "$n" 0 ;;
            after) [[ "$after" =~ ^[0-9]+$ ]] && move_block "$n" "$after" ;;
        esac
        write_blocks
        echo "added: $model $mode ${lib:-<default library>}${cpus:+ cpus=$cpus} (${where})"
    }
    queue_locked _do
}

# Apply a per-block mutation to every selector given.
apply_sel() {  # $1 = mutator fn name, rest = selectors
    local fn="$1"; shift
    [ "$#" -ge 1 ] || { echo "ERROR: $CMD needs at least one <sel>" >&2; return 1; }
    local sels=("$@")
    _do() {
        load_blocks
        local sel idx rc=0
        # Resolve every selector to a MODEL first: indices shift as we mutate, but
        # a model id stays valid across the whole batch.
        local models=()
        for sel in "${sels[@]}"; do
            if ! idx="$(resolve_sel "$sel" | head -n 1)"; then
                echo "ERROR: no queue entry matches '$sel'" >&2; rc=1; continue
            fi
            models+=("${BLK_MODEL[idx]}")
        done
        for sel in "${models[@]}"; do
            idx="$(resolve_sel "$sel" | head -n 1)" || continue
            "$fn" "$idx" || rc=1
        done
        write_blocks
        return "$rc"
    }
    queue_locked _do
}

mut_rm() {
    local i="$1" n=${#BLK_MODEL[@]} j=0
    local NP=() NM=() NMD=() NL=() NW=() NQ=() NF=() k
    echo "removed: ${BLK_MODEL[i]} (was position $((i + 1)))"
    for ((k = 0; k < n; k++)); do
        [ "$k" -eq "$i" ] && continue
        NP[j]="${BLK_PRE[k]}"; NM[j]="${BLK_MODEL[k]}"; NMD[j]="${BLK_MODE[k]}"
        NL[j]="${BLK_LIB[k]}"; NW[j]="${BLK_WAVE[k]}";  NQ[j]="${BLK_QUEUE[k]}"
        NF[j]="${BLK_FLAGS[k]}"
        j=$((j + 1))
    done
    BLK_PRE=("${NP[@]+"${NP[@]}"}"); BLK_MODEL=("${NM[@]+"${NM[@]}"}")
    BLK_MODE=("${NMD[@]+"${NMD[@]}"}"); BLK_LIB=("${NL[@]+"${NL[@]}"}")
    BLK_WAVE=("${NW[@]+"${NW[@]}"}"); BLK_QUEUE=("${NQ[@]+"${NQ[@]}"}")
    BLK_FLAGS=("${NF[@]+"${NF[@]}"}")
}
mut_top()    { echo "moved ${BLK_MODEL[$1]} to position 1"; move_block "$1" 0; }
mut_up()     { local t=$(( $1 - 1 )); [ "$t" -lt 0 ] && t=0
               echo "moved ${BLK_MODEL[$1]} to position $((t + 1))"; move_block "$1" "$t"; }
mut_down()   { local t=$(( $1 + 1 ))
               echo "moved ${BLK_MODEL[$1]} to position $((t + 1))"; move_block "$1" "$t"; }
mut_hold()   { set_hold_flag "$1" 1; echo "held: ${BLK_MODEL[$1]}"; }
mut_unhold() { set_hold_flag "$1" 0; echo "unheld: ${BLK_MODEL[$1]}"; }

cmd_move() {
    local sel="${1:-}" pos="${2:-}"
    [ -n "$sel" ] && [ -n "$pos" ] || { echo "ERROR: move <sel> <pos>" >&2; return 1; }
    [[ "$pos" =~ ^[0-9]+$ ]] || { echo "ERROR: <pos> must be a 1-based number" >&2; return 1; }
    _do() {
        load_blocks
        local idx
        idx="$(resolve_sel "$sel" | head -n 1)" || {
            echo "ERROR: no queue entry matches '$sel'" >&2; return 1; }
        move_block "$idx" "$((pos - 1))"
        write_blocks
        echo "moved ${sel} to position ${pos}"
    }
    queue_locked _do
}

cmd_retry() {
    [ "$#" -ge 1 ] || { echo "ERROR: retry needs at least one <sel>" >&2; return 1; }
    local sels=("$@")
    # Read AND write inside one lock. Loading the blocks outside it and writing them
    # back later would silently discard any edit made in between — the driver could
    # be re-reading, or the TUI reordering, at exactly that moment.
    _do() {
        load_blocks
        status_load
        local sel idx key rc=0
        for sel in "${sels[@]}"; do
            idx="$(resolve_sel "$sel" | head -n 1)" || {
                echo "ERROR: no queue entry matches '$sel'" >&2; rc=1; continue; }
            # Reproduce the driver's key exactly: default applied, then alias-resolved.
            key="$(job_key "${BLK_MODEL[idx]}" "${BLK_MODE[idx]}" \
                           "$(effective_library "${BLK_LIB[idx]}")")"
            unset "ST_STATUS[$key]" "ST_DONE[$key]" "ST_TOTAL[$key]" \
                  "ST_START[$key]" "ST_FIN[$key]" "ST_LOG[$key]" "ST_NOTE[$key]"
            set_hold_flag "$idx" 0
            echo "retry: ${BLK_MODEL[idx]} (verdict cleared, unheld)"
        done
        status_write
        write_blocks
        return "$rc"
    }
    queue_locked _do
}

# Status of a model according to the render view (no S3 calls).
# Status of ONE job, identified by its full key.
#
# Never look this up by model id: the same model is legitimately queued against
# several libraries, and the first match is then the wrong row. Getting this wrong
# meant `cancel` on a running job read the status of a DIFFERENT, already-done row
# and quietly held that one instead of stopping the job in flight.
status_of_key() {  # $1 = model|mode|library
    local want="$1"
    status_load
    if [ -n "${ST_STATUS[$want]:-}" ]; then echo "${ST_STATUS[$want]}"; return 0; fi

    # No entry in the durable store (e.g. a pre-upgrade driver): fall back to
    # state.tsv, matched on the whole triple rather than just the model.
    [ -f "$STATE_FILE" ] || return 1
    local wm="${want%%|*}" rest="${want#*|}" wmode wlib
    wmode="${rest%%|*}"; wlib="${rest#*|}"
    local idx model mode lib status _r
    while IFS=$'\t' read -r idx model mode lib status _r; do
        case "$idx" in ''|'#'*) continue ;; esac
        if [ "$model" = "$wm" ] && [ "$mode" = "$wmode" ] && [ "$lib" = "$wlib" ]; then
            echo "$status"; return 0
        fi
    done < "$STATE_FILE"
    return 1
}

# Block index whose resolved identity equals this key. Survives a reload, which a
# raw index does not.
resolve_key() {  # $1 = model|mode|library
    local want="$1" i
    for i in "${!BLK_MODEL[@]}"; do
        if [ "$(job_key "${BLK_MODEL[i]}" "${BLK_MODE[i]}" \
                        "$(effective_library "${BLK_LIB[i]}")")" = "$want" ]; then
            echo "$i"; return 0
        fi
    done
    return 1
}

# Refuse a control-plane verb when there is no driver to receive it.
#
# These verbs are one-shot messages that only a RUNNING driver consumes. With none
# alive the message simply sits in the control dir until the NEXT driver drains it on
# its first tick — so a `shutdown` posted today makes tomorrow's driver exit on
# startup, hours after the click that caused it. The driver now discards pre-startup
# messages for exactly that reason; refusing here is the other half, because printing
# "shutdown requested" for something that will never happen is worse than an error.
#
# Note what is NOT guarded: add / rm / top / up / down / move / hold / unhold / retry
# all edit the queue file or the status store, and the driver re-reads the queue
# before every job — so they apply the moment one starts. Staging a queue against a
# stopped driver is a supported workflow, not a mistake. `pause` is also left alone:
# its flag survives a restart on purpose, so you can bring a driver up idle.
require_driver() {  # $1 = verb name, for the message
    driver_alive && return 0
    echo "ERROR: no driver is running — '$1' has nothing to act on." >&2
    echo "       Start one: sudo systemctl start ersilia-scheduler  (if installed as a" >&2
    echo "       service — see install-scheduler-service.sh), else start-scheduler-tmux.sh." >&2
    echo "       Queue edits (add/rm/top/up/down/hold/retry) do not need a driver:" >&2
    echo "       they are written to the queue and apply as soon as one starts." >&2
    return 1
}

cmd_cancel() {
    local sel="${1:-}"
    [ -n "$sel" ] || { echo "ERROR: cancel <sel>" >&2; return 1; }
    load_blocks
    local idx model st
    idx="$(resolve_sel "$sel" | head -n 1)" || {
        echo "ERROR: no queue entry matches '$sel'" >&2; return 1; }
    model="${BLK_MODEL[idx]}"
    local lib key label
    lib="$(effective_library "${BLK_LIB[idx]}")"
    key="$(job_key "$model" "${BLK_MODE[idx]}" "$lib")"
    label="${model} on ${lib:-<no library>}"
    st="$(status_of_key "$key" || echo unknown)"
    # A `running` row with no live driver is a leftover: the driver died mid-job and
    # never wrote a verdict (reclaim_stale_running repairs it at the next startup).
    # Taking the cancel path for it would be wrong twice over — there is no
    # orchestrator or SLURM array to stop, and the posted message would ambush the
    # next driver instead. Fall through to hold, which is what the caller actually
    # wants: do not let this job start.
    if [ "$st" = "running" ] && ! driver_alive; then
        echo "note: ${label} is recorded as running but no driver is alive, so that row" >&2
        echo "      is stale — left behind by a driver that died mid-job." >&2
        st="stale"
    fi
    if [ "$st" = "running" ]; then
        # Post the KEY, not the model: the driver matches either, and the key cannot
        # name the wrong library. WHO travels as a second line so the eventual
        # "cancelled" status note can say who asked for it.
        control_post cancel "$key" "$WHO"
        echo "cancel requested for RUNNING ${label} — the driver will scancel its"
        echo "in-flight SLURM array and move on (within ${CTL_POLL:-15}s)."
    else
        _do() { load_blocks; local i; i="$(resolve_key "$key")" || return 1
                set_hold_flag "$i" 1; write_blocks; }
        queue_locked _do
        echo "${label} is not running (status: ${st}) — held instead, so it will not start."
        echo "Use 'rm ${sel}' to drop it from the queue entirely."
    fi
}

cmd_pause()  { mkdir -p "$(control_dir)"; : > "$(paused_flag)"; echo "paused — the driver will not start new jobs"; }
cmd_resume() { rm -f "$(paused_flag)"; echo "resumed"; }
cmd_stop_after() {
    require_driver stop-after-current || return 1
    mkdir -p "$(control_dir)"; : > "$(stopafter_flag)"
    echo "stop-after-current armed — the driver exits when the current model finishes"
}
cmd_shutdown() {
    require_driver shutdown || return 1
    control_post shutdown "" "$WHO"
    echo "shutdown requested — current model will be cancelled"
}
# Only the driver's own bookkeeping needs this. A client-side recount does not:
# `dump --live-all` counts S3 directly and works with no driver at all.
cmd_refresh() {
    require_driver refresh || return 1
    control_post refresh "" "$WHO"
    echo "S3 recount requested"
}

cmd_list() {
    load_blocks
    status_load
    printf '%-4s %-19s %-12s %-34s %-13s %14s  %s\n' \
        "#" "model" "mode" "library" "status" "done/total" "flags"
    printf -- "-%.0s" {1..108}; echo ""
    local i lib key st dn tt
    for i in "${!BLK_MODEL[@]}"; do
        lib="$(effective_library "${BLK_LIB[i]}")"
        key="$(job_key "${BLK_MODEL[i]}" "${BLK_MODE[i]}" "$lib")"
        st="${ST_STATUS[$key]:-pending}"
        dn="${ST_DONE[$key]:-0}"; tt="${ST_TOTAL[$key]:-0}"
        # `hold` outranks only `pending`. A real verdict — running, cancelled,
        # failed — is what you need to see, and the flag is still shown in the
        # flags column. Overriding unconditionally made a cancelled job read as
        # "held", which hides the very thing you just did.
        case " ${BLK_FLAGS[i]} " in
            *" hold "*) [ "$st" = "pending" ] && st="held" ;;
        esac
        printf '%-4s %-19s %-12s %-34s %-13s %6s/%-7s %s\n' \
            "$((i + 1))" "${BLK_MODEL[i]}" "${BLK_MODE[i]}" "${lib:-<no default>}" \
            "$st" "$dn" "$tt" "${BLK_FLAGS[i]}"
    done
    printf -- '-%.0s' {1..108}; echo ""
    if driver_alive; then
        if [ -f "$(paused_flag)" ]; then echo "  driver: PAUSED (pid ${DI_pid})"
        else echo "  driver: running (pid ${DI_pid})"; fi
    else
        echo "  driver: not running"
    fi
    echo "  queue : $QUEUE_FILE"
}

cmd_status() {
    local s="${SCRIPT_DIR}/scheduler-status.sh"
    [ -x "$s" ] || s="/shared/scripts/scheduler/scheduler-status.sh"
    S3_BUCKET="$S3_BUCKET" LOG_DIR="$LOG_DIR" "$s" "$STATE_FILE"
}

# Recount progress from S3, the same way scheduler-status.sh does.
#
# Cost control matters here: an `aws s3 ls` over a 13,644-object prefix takes about
# a second, so the two halves are counted differently.
#   * TOTALS are per LIBRARY, not per job — one listing serves every row sharing a
#     library, so they are always counted (usually 1-2 calls for a whole queue).
#   * DONE counts are per model, so by default only the RUNNING row is recounted
#     (one call); `--all` recounts every row, for an explicit user-requested refresh.
# An empty done field means "not recounted — keep whatever was recorded".
emit_counts() {  # $1 = scope: running | all
    local scope="$1"
    load_blocks
    status_load
    declare -A INPUT_CACHE=()
    local i lib key st dn tt
    for i in "${!BLK_MODEL[@]}"; do
        lib="$(effective_library "${BLK_LIB[i]}")"
        [ -n "$lib" ] || continue
        key="$(job_key "${BLK_MODEL[i]}" "${BLK_MODE[i]}" "$lib")"
        if [ -z "${INPUT_CACHE[$lib]+x}" ]; then
            INPUT_CACHE[$lib]="$(s3_count_input "$lib")"
        fi
        tt="${INPUT_CACHE[$lib]}"
        st="${ST_STATUS[$key]:-}"
        # No status store means a pre-upgrade driver: fall back to its state.tsv so
        # we still know which row is the running one.
        [ -n "$st" ] || st="$(status_of_key "$key" 2>/dev/null || echo pending)"
        dn=""
        if [ "$scope" = "all" ] || [ "$st" = "running" ]; then
            dn="$(s3_count_output "${BLK_MODEL[i]}" "$lib" "${BLK_MODE[i]}")"
        fi
        printf '%s\t%s\t%s\n' "$key" "$dn" "$tt"
    done
}

# One machine-readable snapshot. The TUI's only read primitive: a single call
# (and over SSH, a single round-trip) returns everything it needs to render.
cmd_dump() {
    local logpath="" live=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --log)      logpath="${2:-}"; shift 2 ;;
            --live)     live="running"; shift ;;
            --live-all) live="all"; shift ;;
            *)          shift ;;
        esac
    done
    local alive=0 paused=0 stopafter=0 legacy=0 legacy_pid=""
    driver_alive && alive=1
    if driver_is_legacy; then legacy=1; legacy_pid="$(driver_pid_scan)"; fi
    [ -f "$(paused_flag)" ] && paused=1
    [ -f "$(stopafter_flag)" ] && stopafter=1

    echo "---8<--- runtime"
    echo "schema=1"
    echo "driver_alive=${alive}"
    echo "driver_legacy=${legacy}"
    echo "legacy_pid=${legacy_pid}"
    echo "paused=${paused}"
    echo "stop_after_current=${stopafter}"
    echo "log_dir=${LOG_DIR}"
    echo "queue_file=${QUEUE_FILE:-}"
    echo "state_file=${STATE_FILE}"
    echo "status_file=${STATUS_FILE}"
    echo "s3_bucket=${S3_BUCKET}"
    # Published so the add dialog validates against the cluster's real ceiling rather
    # than a number hardcoded in the client, which would drift the day the partition
    # gets bigger instance types.
    echo "max_cpus_per_task=${MAX_CPUS_PER_TASK}"
    echo "sif_dir=${SIF_DIR}"
    # How this target runs models: `slurm` today; `serve` once a non-SLURM
    # backend exists. A client that predates this key sees nothing here, which
    # is fine — the field is additive, like every other line in this section.
    echo "dispatch=${DISPATCH}"
    echo "now=$(now_iso)"

    echo "---8<--- driver.info"
    [ -f "$(driver_info)" ] && cat "$(driver_info)"

    echo "---8<--- queue"
    [ -n "${QUEUE_FILE:-}" ] && [ -f "$QUEUE_FILE" ] && cat "$QUEUE_FILE"

    # The authoritative parsed view: queue order with libraries already resolved
    # the way the driver resolves them. Clients read THIS rather than re-parsing
    # the raw queue above, so alias handling has exactly one implementation.
    # `cpus` is APPENDED as a ninth column, never inserted. A client that predates it
    # slices the first eight fields and is unaffected; a current client pads short
    # rows. Inserting mid-row instead would silently shift lib_is_default into the
    # cpus slot for every un-upgraded TUI still pointed at this cluster.
    echo "---8<--- jobs"
    if [ -n "${QUEUE_FILE:-}" ] && [ -f "$QUEUE_FILE" ]; then
        printf '#pos\tmodel\tmode\tlibrary\twave\tqueue\tflags\tlib_is_default\tcpus\n'
        load_blocks
        local i lib jcpus
        for i in "${!BLK_MODEL[@]}"; do
            lib="$(effective_library "${BLK_LIB[i]}")"
            jcpus="$(queue_flag_value "${BLK_FLAGS[i]}" cpus)" || jcpus=""
            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                "$((i + 1))" "${BLK_MODEL[i]}" "${BLK_MODE[i]}" "$lib" \
                "${BLK_WAVE[i]}" "${BLK_QUEUE[i]}" "${BLK_FLAGS[i]}" \
                "$([ -z "${BLK_LIB[i]}" ] && echo 1 || echo 0)" \
                "$jcpus"
        done
    fi

    echo "---8<--- state.tsv"
    [ -f "$STATE_FILE" ] && cat "$STATE_FILE"

    echo "---8<--- status.tsv"
    [ -f "$STATUS_FILE" ] && cat "$STATUS_FILE"

    echo "---8<--- libraries"
    # Library names for the add-dialog's dropdown. THREE sources, unioned:
    #
    #   1. what actually exists in S3 under input/  <- the authoritative list
    #   2. the alias table's canonical names (the `echo "Name"` arms), so a library
    #      that is aliased but not yet ingested still offers itself
    #   3. anything already referenced by this queue or status store
    #
    # (1) is the one that matters and was missing: the alias table knows only five
    # names, so every library ingested since — the h3d selections, the 44g subsets —
    # was absent from the dropdown unless it happened to be in the queue already.
    # Refreshed from S3 on a --live dump, served from cache otherwise.
    {
        s3_list_libraries ${live:+refresh}
        for cand in "${SCRIPT_DIR}/library-aliases.sh" \
                    /shared/scripts/library-aliases.sh \
                    "${SCRIPT_DIR}/../../AWS_templates/library-aliases.sh" \
                    /shared/scripts/AWS_templates/library-aliases.sh; do
            [ -f "$cand" ] || continue
            grep -oP 'echo\s+"\K[A-Za-z][A-Za-z0-9_.]*(?=")' "$cand" 2>/dev/null
            break
        done
        [ -f "$STATE_FILE" ] && awk -F'\t' '$1 !~ /^#/ && $4 != "" {print $4}' "$STATE_FILE"
        [ -f "$STATUS_FILE" ] && awk -F'\t' '$1 !~ /^#/ {n=split($1,a,"|"); if (n==3) print a[3]}' "$STATUS_FILE"
    } 2>/dev/null | grep -vx 'NA' | sort -u

    echo "---8<--- counts ${live}"
    if [ -n "$live" ] && [ -n "${QUEUE_FILE:-}" ] && [ -f "$QUEUE_FILE" ]; then
        emit_counts "$live"
    fi

    echo "---8<--- log ${logpath}"
    [ -n "$logpath" ] && [ -f "$logpath" ] && tail -n "${DUMP_LOG_LINES:-300}" "$logpath"

    echo "---8<--- end"
}

# =============================================================================
# Dispatch
# =============================================================================
# Every MUTATING command is recorded in $LOG_DIR/audit.log before it runs — the
# attempt, not just a success, since a rejected mutation ("cpus out of range")
# is still something a person did and may want to find later. Read-only
# commands (list, status, dump, queue-file) are not logged: with everyone on
# one shared unix account, the audit log exists to answer "who changed
# something", not to record every look at the dashboard.
case "$CMD" in
    add)                need_queue; read_driver_info >/dev/null 2>&1
                        audit_log "$WHO" add "$@"; cmd_add "$@" ;;
    rm|remove)          need_queue; audit_log "$WHO" rm "$@"; apply_sel mut_rm "$@" ;;
    top)                need_queue; audit_log "$WHO" top "$@"; apply_sel mut_top "$@" ;;
    up)                 need_queue; audit_log "$WHO" up "$@"; apply_sel mut_up "$@" ;;
    down)               need_queue; audit_log "$WHO" down "$@"; apply_sel mut_down "$@" ;;
    hold)               need_queue; audit_log "$WHO" hold "$@"; apply_sel mut_hold "$@" ;;
    unhold)             need_queue; audit_log "$WHO" unhold "$@"; apply_sel mut_unhold "$@" ;;
    move)               need_queue; audit_log "$WHO" move "$@"; cmd_move "$@" ;;
    retry)              need_queue; read_driver_info >/dev/null 2>&1
                        audit_log "$WHO" retry "$@"; cmd_retry "$@" ;;
    cancel)             need_queue; audit_log "$WHO" cancel "$@"; cmd_cancel "$@" ;;
    pause)              audit_log "$WHO" pause; cmd_pause ;;
    resume)             audit_log "$WHO" resume; cmd_resume ;;
    stop-after-current) audit_log "$WHO" stop-after-current; cmd_stop_after ;;
    shutdown)           audit_log "$WHO" shutdown; cmd_shutdown ;;
    refresh)            audit_log "$WHO" refresh; cmd_refresh ;;
    list|ls)            need_queue; read_driver_info >/dev/null 2>&1; cmd_list ;;
    status)             cmd_status ;;
    dump)               resolve_queue_file >/dev/null 2>&1 || true; cmd_dump "$@" ;;
    queue-file)         need_queue; echo "$QUEUE_FILE" ;;
    -h|--help|help)     usage ;;
    *)                  echo "ERROR: unknown command '$CMD'" >&2; echo ""; usage; exit 1 ;;
esac
