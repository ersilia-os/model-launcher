#!/bin/bash
# =============================================================================
# Serve orchestrator: run one Ersilia model over one library with the plain CLI
# =============================================================================
# The non-SLURM counterpart of slurm/submit-ersilia-waves.sh, for a machine with
# `ersilia` installed and DISPATCH=serve:
#
#   ersilia fetch <model>
#   ersilia serve <model>
#   for each chunk: ersilia run -i <chunk> -o <result>
#   ersilia close
#
#   * Input:   $DATA_DIR/input/<lib>/<lib>_chunk_<N>.csv
#   * Results: $DATA_DIR/output/<lib>/<model>/<model>_results_<N>.csv — the same
#     names the SLURM worker writes, so progress counting is shared.
#   * RESUMABLE: a chunk whose result already exists is skipped.
#   * A result is written under _work/ and moved into place only once `ersilia run`
#     has succeeded, so a run killed mid-write never counts as a finished chunk.
#   * The first failure stops the job (exit 1); a retry resumes where it stopped.
#   * `ersilia close` runs on every exit, including cancel (SIGTERM): a model served
#     from Docker lives outside this process tree, so killing the tree alone would
#     leave its container running.
#
# Every ersilia call is a direct child of this script, so the ersilia session is
# keyed to THIS script's pid and serve/run/close all share it. If this script is
# ever SIGKILLed, ersilia's own orphan cleanup stops the model on its next
# invocation.
#
# A cancel reaches ersilia as Ctrl-C, not as the SIGTERM the driver sends. Killed
# by SIGTERM, a `serve` still starting has launched its container but not yet
# recorded it, so `ersilia close` cannot find it and the container runs forever.
# Interrupted, ersilia cleans up after itself. See ersilia_cmd.
#
# Usage: run-ersilia-serve.sh <model_id> <library_name>
# Env:   DATA_DIR (required), ERSILIA_BIN (default: ersilia)
# =============================================================================

set -uo pipefail

MODEL_ID="${1:-}"
LIBRARY_NAME="${2:-}"
ERSILIA_BIN="${ERSILIA_BIN:-ersilia}"
DATA_DIR="${DATA_DIR:-}"

if [ -z "$MODEL_ID" ] || [ -z "$LIBRARY_NAME" ]; then
    echo "Usage: $0 <model_id> <library_name>   (env: DATA_DIR, ERSILIA_BIN)"
    exit 1
fi
[ -n "$DATA_DIR" ] || { echo "ERROR: DATA_DIR is not set."; exit 1; }

INPUT_DIR="${DATA_DIR%/}/input/${LIBRARY_NAME}"
OUTPUT_DIR="${DATA_DIR%/}/output/${LIBRARY_NAME}/${MODEL_ID}"
WORK="${OUTPUT_DIR}/_work"

[ -d "$INPUT_DIR" ] || { echo "ERROR: input dir does not exist: ${INPUT_DIR}"; exit 1; }
mkdir -p "$OUTPUT_DIR" || { echo "ERROR: cannot create ${OUTPUT_DIR}"; exit 1; }
# Whatever is in _work/ was left by a run that never finished: never a result.
rm -rf "$WORK"
mkdir -p "$WORK" || { echo "ERROR: cannot create ${WORK}"; exit 1; }

# ---- chunks still to do, in chunk-number order ----
REMAINING=()
while IFS= read -r name; do
    num="${name%.csv}"; num="${num##*_chunk_}"
    [ -f "${OUTPUT_DIR}/${MODEL_ID}_results_${num}.csv" ] || REMAINING+=("$name")
done < <(find "$INPUT_DIR" -mindepth 1 -maxdepth 1 -type f -name '*_chunk_*.csv' 2>/dev/null \
            | sed 's#.*/##' \
            | sed -nE 's/^(.*_chunk_([0-9]+)\.csv)$/\2 \1/p' \
            | sort -n | cut -d' ' -f2-)

TOTAL=${#REMAINING[@]}
echo "model=${MODEL_ID} library=${LIBRARY_NAME} chunks to run=${TOTAL}"
echo "input:  ${INPUT_DIR}"
echo "output: ${OUTPUT_DIR}"
if [ "$TOTAL" -eq 0 ]; then
    rmdir "$WORK" 2>/dev/null
    echo "Nothing to do: every chunk already has a result."
    exit 0
fi

# ---- run ersilia so that a cancel interrupts it cleanly ----
# The child ignores SIGTERM (the driver's kill_tree signals every descendant), and
# on TERM this script sends it SIGINT instead and waits for it to finish.
#
# SIGINT has to be handed back first. The driver starts this script in the
# background, and a background job starts with SIGINT ignored, which bash can
# never undo ("signals ignored upon entry cannot be trapped or reset"), so every
# child would inherit it and ersilia would never see the interrupt. perl, which
# macOS and Linux both ship, resets it just before exec. Without perl the child
# keeps SIGTERM, which still stops it, only less gracefully.
ERSILIA_PID=""
ersilia_cmd() {
    if command -v perl >/dev/null 2>&1; then
        ( trap '' TERM
          exec perl -e '$SIG{INT} = "DEFAULT"; exec { $ARGV[0] } @ARGV or die "cannot run $ARGV[0]: $!\n"' \
              "$ERSILIA_BIN" "$@" ) &
    else
        ( exec "$ERSILIA_BIN" "$@" ) &
    fi
    ERSILIA_PID=$!
    local rc=0
    wait "$ERSILIA_PID" || rc=$?
    ERSILIA_PID=""
    return "$rc"
}
on_signal() {  # $1 = exit code
    if [ -n "$ERSILIA_PID" ] && kill -0 "$ERSILIA_PID" 2>/dev/null; then
        echo "Interrupting ersilia (pid ${ERSILIA_PID})"
        kill -INT "$ERSILIA_PID" 2>/dev/null
        wait "$ERSILIA_PID" 2>/dev/null
        ERSILIA_PID=""
    fi
    exit "$1"
}

# ---- close the model however this script ends ----
SERVED=0
close_model() {
    if [ "$SERVED" -eq 1 ]; then
        SERVED=0
        echo "Closing ${MODEL_ID}"
        "$ERSILIA_BIN" close
    fi
    rm -rf "$WORK"
}
trap close_model EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

ersilia_cmd fetch "$MODEL_ID" || { echo "ERROR: ersilia fetch ${MODEL_ID} failed."; exit 1; }
# Set before serving: a serve interrupted halfway may already have started a server.
SERVED=1
ersilia_cmd serve "$MODEL_ID" || { echo "ERROR: ersilia serve ${MODEL_ID} failed."; exit 1; }

i=0
for name in "${REMAINING[@]}"; do
    i=$((i + 1))
    num="${name%.csv}"; num="${num##*_chunk_}"
    result="${MODEL_ID}_results_${num}.csv"
    echo "[${i}/${TOTAL}] ${name}"
    ersilia_cmd run -i "${INPUT_DIR}/${name}" -o "${WORK}/${result}" \
        || { echo "ERROR: ersilia run failed on ${name} (rc=$?)."; exit 1; }
    [ -f "${WORK}/${result}" ] || { echo "ERROR: ersilia run wrote no output for ${name}."; exit 1; }
    mv -f "${WORK}/${result}" "${OUTPUT_DIR}/${result}" \
        || { echo "ERROR: cannot move ${result} into ${OUTPUT_DIR}."; exit 1; }
done

echo "All ${TOTAL} chunk(s) done."
exit 0
