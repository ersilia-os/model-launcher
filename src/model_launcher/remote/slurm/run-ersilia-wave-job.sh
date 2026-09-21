#!/bin/bash
#SBATCH --job-name=ersilia-wave
#SBATCH --partition=cpu-queue
#SBATCH --nodes=1
#SBATCH --cpus-per-task=10
#SBATCH --time=24:00:00
#SBATCH --output=/shared/logs/ersilia-wave-%A_%a.out
#SBATCH --error=/shared/logs/ersilia-wave-%A_%a.err
#
# Array-task worker for the billion-scale wave orchestrator (submit-ersilia-waves.sh).
#
# Mirrors the cluster's generated /shared/scripts/run-ersilia-job.sh, with three
# robustness fixes for large runs:
#   1. S3_BUCKET is DEFAULTED (the generated worker silently skips the S3 upload when
#      $S3_BUCKET is empty, e.g. a job that did not inherit the login environment).
#   2. Longer wall time (24:00:00) — a 100k-molecule chunk is 10x the old 10k chunk.
#   3. cpus-per-task=10 caps packing at ~3 jobs per 32-vCPU node (cpu-queue is CR_CPU
#      = no memory accounting, so cpus-per-task is the only lever to prevent OOM from
#      overpacking). 32 / 10 = 3 jobs/node, each with ~1/3 of node RAM.
#      NOTE: assumes SLURM sees 32 CPUs/node. Verify with
#        sudo /opt/slurm/bin/scontrol show node <node> | grep -oP 'CPUTot=\K\d+'
#      If CPUTot=16 (hyperthreads not counted), use cpus-per-task=5 for 3 jobs/node.
#
# Called (per wave) as:
#   sbatch --array=0-(W-1) run-ersilia-wave-job.sh <model_id> <wave_chunk_list> <output_dir>
#
#   <wave_chunk_list>  one input path per line (1-based lines map to array indices)
#   <output_dir>       /fsx/output/<library>/<model_id>
#
# Output: <output_dir>/<model_id>_results_NNNNNN.csv, also uploaded to
#         s3://$S3_BUCKET/output/<library>/<model_id>/. The FSx copy is left in place;
#         the orchestrator evicts it at wave end (after verify + sync).

set -uo pipefail

MODEL_ID="${1:-}"
CHUNK_LIST="${2:-}"
OUTPUT_DIR="${3:-}"
S3_BUCKET="${S3_BUCKET:-ai2050-ersilia-cluster}"

if [ -z "$MODEL_ID" ] || [ -z "$CHUNK_LIST" ] || [ -z "$OUTPUT_DIR" ]; then
    echo "ERROR: Usage: sbatch --array=0-N run-ersilia-wave-job.sh <model_id> <chunk_list> <output_dir>"
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
OUTPUT_FILE="${OUTPUT_DIR}/${MODEL_ID}_results_${CHUNK_NUM}.csv"

echo "=========================================="
echo "Ersilia Wave Job"
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
    echo "Download it first: /shared/scripts/download-ersilia-model.sh $MODEL_ID"
    exit 1
fi

if [ ! -f "$INPUT_FILE" ]; then
    echo "ERROR: Input file not found: $INPUT_FILE"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

# Copy input to node-local /tmp to avoid FSx Lustre visibility issues in post-processing.
LOCAL_INPUT="/tmp/$(basename "$INPUT_FILE")"
cp "$INPUT_FILE" "$LOCAL_INPUT"

echo "Processing $(( $(wc -l < "$LOCAL_INPUT") - 1 )) molecules..."

/shared/python39/bin/ersilia_apptainer \
    --sif "$SIF_FILE" \
    --input "$LOCAL_INPUT" \
    --output "$OUTPUT_FILE" --verbose
RC=$?

rm -f "$LOCAL_INPUT"

if [ $RC -ne 0 ] || [ ! -f "$OUTPUT_FILE" ]; then
    echo "ERROR: ersilia_apptainer failed (rc=$RC) or output not created: $OUTPUT_FILE"
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
