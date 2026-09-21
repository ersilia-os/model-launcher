#!/bin/bash
# =============================================================================
# Wave orchestrator for billion-scale libraries — SINGULARITY worker
# =============================================================================
# Singularity counterpart of large_library_scripts/submit-ersilia-waves.sh, for
# models that cannot run under the ersilia apptainer wrapper and are invoked as a
# plain `singularity run <sif> <in> <out>`. Everything else — the S3-centric,
# FSx-bounded wave strategy — is identical.
#
# Strategy:
#   * Chunk manifest is built from S3 (`aws s3 ls`), not an `ls *.csv` glob — no
#     ARG_MAX risk, no FSx listing lag.
#   * Chunks already present in s3://<bucket>/output/<lib>/<model>/ are skipped, so
#     the run is fully RESUMABLE.
#   * Chunks are processed in SEQUENTIAL waves of <wave_size> (<=1000 = SLURM
#     MaxArraySize). Each wave is awaited before the next, so in-flight jobs never
#     exceed one wave — the wait IS the throttle.
#   * Per wave: submit array job -> wait -> verify row counts (+one resubmit of
#     failures) -> `aws s3 sync` (safety net) -> `rm` the wave's outputs from /fsx.
#     FSx therefore holds at most one wave's outputs regardless of model width.
#
# Output naming is `<model_id>_<chunk_num>.csv` (NO `_results_`), matching the rest
# of the singularity ecosystem (run-singularity-library-job.sh, the check/bisect/
# merge scripts). This lets submit-missing-bisect-singularity-large.sh and
# check-singularity-library-results.sh work against these outputs unchanged.
#
# Run it on the HEAD NODE inside tmux/nohup (a full 1.4B run can take days):
#   tmux new -s waves
#   S3_BUCKET=ai2050-ersilia-cluster \
#     /shared/scripts/large_library_scripts/submit-singularity-waves.sh <model_id> <library> 1000 cpu-queue
#
# Usage: submit-singularity-waves.sh <model_id> <library_name> [wave_size=1000] [queue=cpu-queue]
# =============================================================================

set -uo pipefail

MODEL_ID="${1:-}"
LIBRARY_NAME="${2:-}"
WAVE_SIZE="${3:-1000}"
QUEUE="${4:-cpu-queue}"
S3_BUCKET="${S3_BUCKET:-ai2050-ersilia-cluster}"
POLL_SECONDS="${POLL_SECONDS:-30}"
# Optional per-model packing override, normally set by the scheduler from a `cpus=N`
# queue flag. EMPTY MEANS "DO NOT PASS IT": the worker's own #SBATCH
# --cpus-per-task then stands, which is the only value that has actually been tuned
# against this partition. Passing a number here overrides that directive, so a
# default baked in on this side would silently re-pack every existing run.
CPUS_PER_TASK="${CPUS_PER_TASK:-}"

if [ -n "$CPUS_PER_TASK" ]; then
    if ! [[ "$CPUS_PER_TASK" =~ ^[0-9]+$ ]] || [ "$CPUS_PER_TASK" -lt 1 ] \
       || [ "$CPUS_PER_TASK" -gt 32 ]; then
        echo "ERROR: CPUS_PER_TASK must be between 1 and 32 (got '$CPUS_PER_TASK')."
        exit 1
    fi
fi

# Built as an array so the flag is absent — not empty — when there is no override.
# An empty "" argument to sbatch is a usage error, not a no-op.
SBATCH_CPUS=()
[ -n "$CPUS_PER_TASK" ] && SBATCH_CPUS=(--cpus-per-task="$CPUS_PER_TASK")

if [ -z "$MODEL_ID" ] || [ -z "$LIBRARY_NAME" ]; then
    echo "Usage: $0 <model_id> <library_name> [wave_size=1000] [queue=cpu-queue]"
    echo "Example: $0 mtb-public-models Enamine_Real_Sample_1.4B 1000 cpu-queue"
    exit 1
fi
if [ "$WAVE_SIZE" -lt 1 ] || [ "$WAVE_SIZE" -gt 1000 ]; then
    echo "ERROR: wave_size must be between 1 and 1000 (SLURM MaxArraySize)."
    exit 1
fi

INPUT_DIR="/fsx/input/${LIBRARY_NAME}"
OUTPUT_DIR="/fsx/output/${LIBRARY_NAME}/${MODEL_ID}"
S3_INPUT="s3://${S3_BUCKET}/input/${LIBRARY_NAME}/"
S3_OUTPUT="s3://${S3_BUCKET}/output/${LIBRARY_NAME}/${MODEL_ID}/"
WORK="${OUTPUT_DIR}/_wave_work"
FAILED_LOG="${OUTPUT_DIR}/_failed_chunks.txt"

# Locate the array-task worker (repo folder first, then the deployed /shared copy).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_JOB="${SCRIPT_DIR}/run-singularity-wave-job.sh"
[ -f "$RUN_JOB" ] || RUN_JOB="/shared/scripts/large_library_scripts/run-singularity-wave-job.sh"

if [ ! -f "/shared/sif-files/${MODEL_ID}.sif" ]; then
    echo "ERROR: Model SIF not found: /shared/sif-files/${MODEL_ID}.sif"
    echo "Build/copy the .sif into /shared/sif-files/ first."
    exit 1
fi
if [ ! -f "$RUN_JOB" ]; then
    echo "ERROR: worker script not found: $RUN_JOB (deploy run-singularity-wave-job.sh to /shared/scripts/large_library_scripts)"
    exit 1
fi

mkdir -p "$OUTPUT_DIR" "$WORK"

echo "=========================================="
echo "Singularity Wave Orchestrator"
echo "=========================================="
echo "Model      : $MODEL_ID"
echo "Library    : $LIBRARY_NAME"
echo "Wave size  : $WAVE_SIZE chunks/wave"
echo "Queue      : $QUEUE"
echo "Cpus/task  : ${CPUS_PER_TASK:-<worker default>}"
echo "Worker     : $RUN_JOB"
echo "S3 input   : $S3_INPUT"
echo "S3 output  : $S3_OUTPUT"
echo "=========================================="

# --- 1. Build the full chunk manifest from S3 (num<space>fsx_input_path) ---
MANIFEST="${WORK}/manifest.txt"
echo "Listing input chunks from S3 ..."
aws s3 ls "$S3_INPUT" \
  | awk '{print $NF}' \
  | grep -oP '.*_chunk_\d+\.csv$' \
  | while read -r fname; do
        num=$(echo "$fname" | grep -oP '(?<=_chunk_)\d+(?=\.csv$)')
        [ -n "$num" ] && echo "$num ${INPUT_DIR}/${fname}"
    done \
  | sort -n > "$MANIFEST"

TOTAL_CHUNKS=$(wc -l < "$MANIFEST" | tr -d ' ')
if [ "$TOTAL_CHUNKS" -eq 0 ]; then
    echo "ERROR: no *_chunk_*.csv found under $S3_INPUT"
    exit 1
fi

# --- 2. Determine already-done chunks (resume) ---
DONE="${WORK}/done.txt"
aws s3 ls "$S3_OUTPUT" 2>/dev/null \
  | awk '{print $NF}' \
  | grep -oP "${MODEL_ID}_\K\d+(?=\.csv$)" \
  | sort -n > "$DONE" || true
DONE_COUNT=$(wc -l < "$DONE" | tr -d ' ')

# --- 3. Remaining input paths (manifest order = numeric) ---
# NOTE: do NOT use a single awk 'NR==FNR' pass here — when DONE is empty (a fresh
# run) NR==FNR stays true for MANIFEST too, so everything looks "done". Branch on it.
REMAINING="${WORK}/remaining.txt"
if [ -s "$DONE" ]; then
    awk 'NR==FNR{d[$1]=1; next} !($1 in d){print $2}' "$DONE" "$MANIFEST" > "$REMAINING"
else
    cut -d' ' -f2- "$MANIFEST" > "$REMAINING"   # nothing done yet -> all chunks remain
fi
REMAINING_COUNT=$(wc -l < "$REMAINING" | tr -d ' ')

echo "Total chunks : $TOTAL_CHUNKS"
echo "Already done : $DONE_COUNT"
echo "Remaining    : $REMAINING_COUNT"
if [ "$REMAINING_COUNT" -eq 0 ]; then
    echo "Nothing to do — all chunks already present in S3 output."
    exit 0
fi
NUM_WAVES=$(( (REMAINING_COUNT + WAVE_SIZE - 1) / WAVE_SIZE ))
echo "Waves        : $NUM_WAVES (of up to $WAVE_SIZE chunks each)"
echo ""

# --- helpers ---------------------------------------------------------------

submit_and_wait() {
    # $1 = chunk-list file ; echoes the array job id
    local list="$1" w
    w=$(wc -l < "$list" | tr -d ' ')
    local aid
    # ${arr[@]+"${arr[@]}"} — expanding an EMPTY array as "${arr[@]}" is an unbound
    # variable error under `set -u` on bash 4.2 (the AL2 head node). This form
    # expands to nothing at all when there is no override.
    aid=$(sbatch --partition="$QUEUE" ${SBATCH_CPUS[@]+"${SBATCH_CPUS[@]}"} \
            --array=0-$((w - 1)) \
            "$RUN_JOB" "$MODEL_ID" "$list" "$OUTPUT_DIR" 2>&1 \
          | grep -oP 'Submitted batch job \K\d+')
    if [ -z "$aid" ]; then
        echo "  ERROR: sbatch submission failed for $list" >&2
        return 1
    fi
    echo "  Submitted array job $aid ($w tasks); waiting ..." >&2
    # Robust wait: only declare the array done after squeue reports NO active tasks
    # (pending/running/completing/...) on 3 CONSECUTIVE polls. A single empty or failed
    # squeue must NOT end the wait — on a busy/scaling controller squeue can transiently
    # return nothing right after submission, which would otherwise trigger a premature
    # verify (all outputs "missing") and a spurious duplicate resubmit.
    sleep "$POLL_SECONDS"
    local empties=0 out rc n
    while :; do
        out=$(squeue -h -j "$aid" -t PENDING,RUNNING,COMPLETING,CONFIGURING,SUSPENDED 2>&1)
        rc=$?
        if [ "$rc" -eq 0 ]; then
            n=$(printf '%s' "$out" | grep -c .)             # count active tasks (0 if none)
        elif printf '%s' "$out" | grep -qi 'invalid job id'; then
            n=0                                             # job purged from controller = DONE
        else
            n=1                                             # transient squeue failure -> keep waiting
        fi
        if [ "$n" -gt 0 ]; then
            empties=0
        else
            empties=$((empties + 1))
            [ "$empties" -ge 3 ] && break
        fi
        sleep "$POLL_SECONDS"
    done
    echo "$aid"
}

verify_wave() {
    # $1 = chunk-list file ; $2 = output file for failures
    local list="$1" fails="$2" inpath num out in_rows out_rows
    : > "$fails"
    while read -r inpath; do
        num=$(basename "$inpath" .csv | grep -oP '\d+$')
        out="${OUTPUT_DIR}/${MODEL_ID}_${num}.csv"
        if [ ! -f "$out" ]; then
            echo "$inpath" >> "$fails"; continue
        fi
        in_rows=$(( $(wc -l < "$inpath") - 1 ))
        out_rows=$(( $(wc -l < "$out") - 1 ))
        [ "$in_rows" -ne "$out_rows" ] && echo "$inpath" >> "$fails"
    done < "$list"
}

sizing_warning() {
    # Best-effort: warn if one wave's outputs risk overrunning FSx.
    local sample bytes est_gb free_gb
    sample=$(ls "$OUTPUT_DIR"/${MODEL_ID}_*.csv 2>/dev/null | head -1) || return 0
    [ -n "$sample" ] || return 0
    bytes=$(stat -c%s "$sample" 2>/dev/null) || return 0
    est_gb=$(( bytes * WAVE_SIZE / 1000000000 ))
    free_gb=$(df -B1G --output=avail /fsx 2>/dev/null | tail -1 | tr -d ' ') || return 0
    echo "  [sizing] ~$(( bytes / 1000000 )) MB/output-chunk -> ~${est_gb} GB per wave; /fsx free ~${free_gb} GB"
    if [ -n "$free_gb" ] && [ "$est_gb" -gt $(( free_gb * 6 / 10 )) ]; then
        echo "  [sizing] WARNING: a wave may exceed 60% of free FSx. Consider a smaller wave_size."
    fi
}

# --- 4. Process waves ------------------------------------------------------
: > "$FAILED_LOG"
WAVE=0
START=1
while [ "$START" -le "$REMAINING_COUNT" ]; do
    END=$(( START + WAVE_SIZE - 1 ))
    [ "$END" -gt "$REMAINING_COUNT" ] && END=$REMAINING_COUNT
    WAVE_LIST="${WORK}/wave_$(printf '%04d' "$WAVE").txt"
    sed -n "${START},${END}p" "$REMAINING" > "$WAVE_LIST"

    echo "----- Wave $((WAVE + 1))/$NUM_WAVES : chunks $START-$END of $REMAINING_COUNT remaining -----"
    submit_and_wait "$WAVE_LIST" >/dev/null || exit 1

    FAILS="${WORK}/wave_$(printf '%04d' "$WAVE")_fails.txt"
    verify_wave "$WAVE_LIST" "$FAILS"
    NF=$(wc -l < "$FAILS" | tr -d ' ')
    if [ "$NF" -gt 0 ]; then
        echo "  $NF chunk(s) missing/mismatched — resubmitting once ..."
        submit_and_wait "$FAILS" >/dev/null || true
        verify_wave "$FAILS" "${FAILS}.2"
        NF2=$(wc -l < "${FAILS}.2" | tr -d ' ')
        if [ "$NF2" -gt 0 ]; then
            echo "  $NF2 chunk(s) still failing after retry — logged to $FAILED_LOG (continuing)."
            cat "${FAILS}.2" >> "$FAILED_LOG"
        fi
    fi

    [ "$WAVE" -eq 0 ] && sizing_warning

    # Safety-net sync of anything the per-chunk upload missed, then evict from FSx.
    aws s3 sync "$OUTPUT_DIR/" "$S3_OUTPUT" \
        --exclude "*" --include "${MODEL_ID}_*.csv" >/dev/null
    while read -r inpath; do
        num=$(basename "$inpath" .csv | grep -oP '\d+$')
        rm -f "${OUTPUT_DIR}/${MODEL_ID}_${num}.csv"
    done < "$WAVE_LIST"

    echo "  Wave $((WAVE + 1)) done, synced to S3, evicted from /fsx."
    START=$(( END + 1 ))
    WAVE=$(( WAVE + 1 ))
done

# --- 5. Summary ------------------------------------------------------------
TOTAL_FAILED=$(wc -l < "$FAILED_LOG" | tr -d ' ')
echo ""
echo "=========================================="
echo "All waves complete for $LIBRARY_NAME / $MODEL_ID"
echo "  Chunks processed this run : $REMAINING_COUNT"
echo "  Persistent failures       : $TOTAL_FAILED"
echo "=========================================="
if [ "$TOTAL_FAILED" -gt 0 ]; then
    echo "Failed chunk inputs are listed in: $FAILED_LOG"
    echo "Isolate bad molecules with the bisect orchestrator:"
    echo "  S3_BUCKET=$S3_BUCKET /shared/scripts/large_library_scripts/submit-missing-bisect-singularity-large.sh $MODEL_ID $LIBRARY_NAME $QUEUE"
    exit 1
fi
echo "Verify in S3:"
echo "  aws s3 ls $S3_OUTPUT | wc -l   # expect $TOTAL_CHUNKS"
