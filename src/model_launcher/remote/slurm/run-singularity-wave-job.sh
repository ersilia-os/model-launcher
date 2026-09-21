#!/bin/bash
#SBATCH --job-name=singularity-wave
#SBATCH --partition=cpu-queue
#SBATCH --nodes=1
#SBATCH --cpus-per-task=4
#SBATCH --time=24:00:00
#SBATCH --output=/shared/logs/singularity-wave-%A_%a.out
#SBATCH --error=/shared/logs/singularity-wave-%A_%a.err
#
# Array-task worker for the billion-scale wave orchestrator (submit-singularity-waves.sh).
#
# Singularity counterpart of run-ersilia-wave-job.sh: identical wave/S3 mechanics,
# but the model is invoked as a plain `singularity run <sif> <in> <out>` (these are
# the models that cannot run under the ersilia apptainer wrapper). Differences from
# run-ersilia-wave-job.sh:
#   1. Model call is `singularity run --bind /fsx --bind /shared <sif> <in> <out>`
#      instead of `ersilia_apptainer --sif ... --input ... --output ...`.
#   2. Output naming is `<model_id>_<chunk_num>.csv` (NO `_results_`), matching the
#      rest of the singularity ecosystem (run-singularity-library-job.sh,
#      check-singularity-library-results.sh, the bisect/merge chain). The orchestrator
#      and submit-missing-bisect-singularity-large.sh use the SAME pattern.
#   3. --cpus-per-task=4 packs 8 jobs on a 32-vCPU node. cpu-queue is CR_CPU (no memory
#      accounting), so cpus-per-task is the only packing lever. This value was chosen
#      from the packing test (testing_singularity/run-singularity-packing-test.sh) on
#      h3d-mtb: 8 concurrent jobs finished a 100k chunk each in ~27 min (~17.8 chunks/
#      hr/node) vs 4 jobs in ~20 min (~12 chunks/hr/node) — 8/node wins on throughput
#      with no OOM. Re-tune per model: HEAVIER models may need --cpus-per-task=8
#      (4 jobs/node) or --exclusive (1/node); LIGHTER ones may pack more. Redeploy to
#      take effect on the next wave.
#   4. S3_BUCKET is DEFAULTED so a job that did not inherit the login environment
#      still uploads its result.
#
# Called (per wave) as:
#   sbatch --array=0-(W-1) run-singularity-wave-job.sh <model_id> <wave_chunk_list> <output_dir>
#
#   <wave_chunk_list>  one input path per line (1-based lines map to array indices)
#   <output_dir>       /fsx/output/<library>/<model_id>
#
# Output: <output_dir>/<model_id>_<chunk_num>.csv, also uploaded to
#         s3://$S3_BUCKET/output/<library>/<model_id>/. The FSx copy is left in place;
#         the orchestrator evicts it at wave end (after verify + sync).

set -uo pipefail

MODEL_ID="${1:-}"
CHUNK_LIST="${2:-}"
OUTPUT_DIR="${3:-}"
S3_BUCKET="${S3_BUCKET:-ai2050-ersilia-cluster}"

if [ -z "$MODEL_ID" ] || [ -z "$CHUNK_LIST" ] || [ -z "$OUTPUT_DIR" ]; then
    echo "ERROR: Usage: sbatch --array=0-N run-singularity-wave-job.sh <model_id> <chunk_list> <output_dir>"
    exit 1
fi

if [ -z "${SLURM_ARRAY_TASK_ID:-}" ]; then
    echo "ERROR: SLURM_ARRAY_TASK_ID is not set — this script must run as an array job."
    exit 1
fi

# Pick this task's input by 1-based line number in the wave chunk list.
INPUT_FILE=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" "$CHUNK_LIST")
if [ -z "$INPUT_FILE" ]; then
    echo "ERROR: No input at index $SLURM_ARRAY_TASK_ID in $CHUNK_LIST"
    exit 1
fi

# Zero-padded chunk number preserved from the input filename (…_chunk_NNNNNN.csv).
CHUNK_NUM=$(basename "$INPUT_FILE" .csv | grep -oP '\d+$')
OUTPUT_FILE="${OUTPUT_DIR}/${MODEL_ID}_${CHUNK_NUM}.csv"

echo "=========================================="
echo "Singularity Wave Job"
echo "  Job ID     : ${SLURM_JOB_ID:-N/A}   Array task: ${SLURM_ARRAY_TASK_ID}"
echo "  Node       : $(hostname)"
echo "  Date       : $(date)"
echo "  Model      : $MODEL_ID"
echo "  Input      : $INPUT_FILE"
echo "  Output     : $OUTPUT_FILE"
echo "  S3 bucket  : $S3_BUCKET"
echo "=========================================="

SIF_FILE="/shared/sif-files/${MODEL_ID}.sif"
if [ ! -f "$SIF_FILE" ]; then
    echo "ERROR: SIF file not found: $SIF_FILE"
    exit 1
fi

if [ ! -f "$INPUT_FILE" ]; then
    echo "ERROR: Input file not found: $INPUT_FILE"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

echo "Processing $(( $(wc -l < "$INPUT_FILE") - 1 )) molecules..."

singularity run --bind /fsx:/fsx --bind /shared:/shared "$SIF_FILE" "$INPUT_FILE" "$OUTPUT_FILE"
RC=$?

if [ $RC -ne 0 ] || [ ! -f "$OUTPUT_FILE" ]; then
    echo "ERROR: singularity run failed (rc=$RC) or output not created: $OUTPUT_FILE"
    exit 1
fi

echo "SUCCESS: $OUTPUT_FILE ($(( $(wc -l < "$OUTPUT_FILE") - 1 )) rows)"

# Upload to S3 (per-chunk). The orchestrator's wave-end `aws s3 sync` is a safety net.
S3_OUTPUT="s3://${S3_BUCKET}/output/${OUTPUT_FILE#/fsx/output/}"
if aws s3 cp "$OUTPUT_FILE" "$S3_OUTPUT"; then
    echo "Uploaded -> $S3_OUTPUT"
else
    echo "WARNING: S3 upload failed for $OUTPUT_FILE (wave-end sync will retry)."
fi

echo "Job completed: $(date)"
