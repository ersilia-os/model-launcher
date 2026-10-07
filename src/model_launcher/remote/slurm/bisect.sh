#!/bin/bash
# =============================================================================
# End-of-run bisect: rescue chunks that keep failing because of a few molecules.
# =============================================================================
# SOURCED by submit-ersilia-waves.sh and submit-singularity-waves.sh, after their
# wave loop. One bad SMILES fails a whole chunk (the model crashes, or writes the
# wrong number of rows), and the wave retry cannot help with that. So each chunk
# still failing is split into 10 pieces; the pieces run as ONE SLURM array; every
# piece that fails is split into 10 again, down to single molecules. A molecule
# that fails on its own, twice, becomes an empty row. The pieces are then merged
# back in the original order and the chunk is uploaded like any other result,
# with its failing molecules listed in _bad_smiles_<N>.csv beside it.
#
# Every sbatch goes through the orchestrator's submit_and_wait, which logs
# "Submitted array job N" to the job log: a cancel finds and scancels these
# arrays too (invariant 4). No worker ever submits anything itself.
#
# Needs from the orchestrator: MODEL_ID, OUTPUT_DIR, S3_OUTPUT, RESULT_PREFIX,
# BISECT_JOB (the piece worker) and submit_and_wait <list> [job_script].
# BISECT=0 turns it off. Written for bash 4.2 (the head node).
# =============================================================================

BISECT="${BISECT:-1}"
BISECT_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bisect-pieces.py"

_bisect_num() {  # $1 = chunk input path -> its zero-padded chunk number
    basename "$1" .csv | sed -n 's/.*_\([0-9][0-9]*\)$/\1/p'
}

# Rescue what it can of the chunks listed in $1 (one input path per line), then
# rewrite $1 to list only the chunks still failing. Prints a final
# "BISECT: rescued K chunk(s); B molecule(s) written as empty rows" line.
# Returns 1 only if a submission failed; $1 is then left as it was.
bisect_failed() {  # $1 = failed-chunk list
    local failed="$1"
    [ "$BISECT" = "1" ] || return 0
    [ -s "$failed" ] || return 0
    if ! command -v python3 >/dev/null 2>&1; then
        echo "  bisect skipped: python3 is not available on this machine"
        return 0
    fi

    local root="${OUTPUT_DIR}/_bisect"
    local pending="${root}/pending.txt"
    local inpath num n dir s e tries level=0 tasks next
    rm -rf "$root"
    mkdir -p "$root" || return 1
    : > "$pending"

    echo ""
    echo "----- Bisect: $(grep -c . "$failed") chunk(s) still failing; isolating the molecules that break them -----"
    while read -r inpath; do
        [ -n "$inpath" ] || continue
        num="$(_bisect_num "$inpath")"
        dir="${root}/${num}"
        mkdir -p "$dir"
        echo "$inpath" > "${dir}/input"
        n="$(python3 "$BISECT_PY" count "$inpath")" || continue
        [ "$n" -gt 0 ] || continue
        python3 "$BISECT_PY" ranges 0 $((n - 1)) \
            | while read -r s e; do echo "$num $s $e 0"; done >> "$pending"
    done < "$failed"

    # One level per pass: every pending range of every chunk becomes a piece, and
    # all of them run as one array. Ranges only shrink, and a lone molecule gets
    # two tries, so this always ends.
    while [ -s "$pending" ]; do
        level=$((level + 1))
        tasks="${root}/level_${level}.tasks"
        : > "$tasks"
        while read -r num s e tries; do
            dir="${root}/${num}"
            python3 "$BISECT_PY" split "$(cat "${dir}/input")" "$dir" "$s" "$e" >/dev/null || return 1
            echo "${dir}/piece_${s}_${e}.csv ${dir}/result_${s}_${e}.csv" >> "$tasks"
        done < "$pending"
        echo "  level ${level}: $(grep -c . "$tasks") piece(s)"
        submit_and_wait "$tasks" "$BISECT_JOB" >/dev/null || return 1

        next="${root}/level_${level}.next"
        : > "$next"
        while read -r num s e tries; do
            dir="${root}/${num}"
            rm -f "${dir}/piece_${s}_${e}.csv"
            [ -f "${dir}/result_${s}_${e}.csv" ] && continue
            if [ "$s" -lt "$e" ]; then
                python3 "$BISECT_PY" ranges "$s" "$e" \
                    | while read -r a b; do echo "$num $a $b 0"; done >> "$next"
            elif [ "$tries" -lt 1 ]; then
                # A transient failure (spot loss, timeout) must not blank a good molecule.
                echo "$num $s $e $((tries + 1))" >> "$next"
            else
                : > "${dir}/bad_${s}"
            fi
        done < "$pending"
        mv -f "$next" "$pending"
    done

    local still="${root}/still_failing.txt" rescued=0 blanks=0 nbad out bad
    : > "$still"
    while read -r inpath; do
        [ -n "$inpath" ] || continue
        num="$(_bisect_num "$inpath")"
        dir="${root}/${num}"
        out="${OUTPUT_DIR}/${RESULT_PREFIX}${num}.csv"
        bad="${OUTPUT_DIR}/_bad_smiles_${num}.csv"
        if nbad="$(python3 "$BISECT_PY" merge "$inpath" "$dir" "$out" "$bad")" \
           && aws s3 cp "$out" "${S3_OUTPUT}$(basename "$out")" >/dev/null \
           && aws s3 cp "$bad" "${S3_OUTPUT}$(basename "$bad")" >/dev/null; then
            rescued=$((rescued + 1))
            blanks=$((blanks + nbad))
            echo "  chunk ${num}: rescued; ${nbad} molecule(s) written as empty rows"
            rm -f "$out" "$bad"
            rm -rf "$dir"
        else
            echo "  chunk ${num}: could not be rescued (workspace kept: ${dir})"
            echo "$inpath" >> "$still"
            rm -f "$out"
        fi
    done < "$failed"

    cp -f "$still" "$failed"
    [ -s "$still" ] || rm -rf "$root"
    echo "BISECT: rescued ${rescued} chunk(s); ${blanks} molecule(s) written as empty rows"
    return 0
}
