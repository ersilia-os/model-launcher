#!/bin/bash
#SBATCH --job-name=ersilia-bisect
#SBATCH --partition=cpu-queue
#SBATCH --nodes=1
#SBATCH --cpus-per-task=8
#SBATCH --time=24:00:00
#SBATCH --output=/shared/logs/ersilia-bisect-%A_%a.out
#SBATCH --error=/shared/logs/ersilia-bisect-%A_%a.err
#
# Array-task worker for the end-of-run bisect (bisect.sh): runs the model on ONE
# piece of a chunk that kept failing. Same resources as run-ersilia-wave-job.sh.
#
#   sbatch --array=0-(N-1) run-ersilia-bisect-piece.sh <model_id> <task_list> <output_dir>
#
#   <task_list>  one "<piece_in> <piece_out>" per line (0-based array index = line - 1)
#
# Success is exit 0 AND an output with exactly as many lines as the input. Anything
# else leaves no output behind, so a failed attempt can never be merged as a result.
# Nothing is uploaded here: the orchestrator merges the pieces and uploads the chunk.

set -uo pipefail

MODEL_ID="${1:-}"
TASK_LIST="${2:-}"
ERSILIA_APPTAINER="${ERSILIA_APPTAINER:-/shared/python39/bin/ersilia_apptainer}"
SIF_DIR="${SIF_DIR:-/shared/sif-files}"

if [ -z "$MODEL_ID" ] || [ -z "$TASK_LIST" ] || [ -z "${SLURM_ARRAY_TASK_ID:-}" ]; then
    echo "ERROR: Usage: sbatch --array=0-N run-ersilia-bisect-piece.sh <model_id> <task_list> <output_dir>"
    exit 1
fi

read -r PIECE_IN PIECE_OUT <<< "$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" "$TASK_LIST")"
if [ -z "${PIECE_IN:-}" ] || [ -z "${PIECE_OUT:-}" ] || [ ! -f "$PIECE_IN" ]; then
    echo "ERROR: no piece at index $SLURM_ARRAY_TASK_ID in $TASK_LIST"
    exit 1
fi

echo "bisect piece: $MODEL_ID $PIECE_IN ($(( $(wc -l < "$PIECE_IN") - 1 )) molecules)"
rm -f "$PIECE_OUT"
LOCAL_INPUT="/tmp/$(basename "$PIECE_IN" .csv)_${SLURM_JOB_ID:-$$}_${SLURM_ARRAY_TASK_ID}.csv"
cp "$PIECE_IN" "$LOCAL_INPUT"

"$ERSILIA_APPTAINER" --sif "${SIF_DIR}/${MODEL_ID}.sif" \
    --input "$LOCAL_INPUT" --output "$PIECE_OUT" --verbose
RC=$?
rm -f "$LOCAL_INPUT"

if [ "$RC" -ne 0 ] || [ ! -f "$PIECE_OUT" ] \
   || [ "$(wc -l < "$PIECE_OUT")" -ne "$(wc -l < "$PIECE_IN")" ]; then
    echo "FAILED (rc=$RC): no usable output for $PIECE_IN"
    rm -f "$PIECE_OUT"
    exit 1
fi
echo "OK: $PIECE_OUT"
