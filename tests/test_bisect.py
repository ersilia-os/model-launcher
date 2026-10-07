"""End-of-run bisect on SLURM: rescuing chunks that a few molecules break.

The pieces helper (split, ranges, merge) is tested on its own. The bisect loop is
then run for real — bisect.sh, the piece worker and the orchestrator's own
submit_and_wait — with a stub sbatch that runs every array task at once and a fake
ersilia_apptainer that fails on any piece holding a BAD molecule.
"""

from __future__ import annotations

import csv
import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

from model_launcher.core.remote import remote_dir

from .conftest import bash_eval

SLURM = remote_dir() / "slurm"
PIECES = SLURM / "bisect-pieces.py"


def _pieces(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PIECES), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _chunk(path: Path, smiles: list[str]) -> Path:
    path.write_text("smiles\n" + "".join(f"{s}\n" for s in smiles))
    return path


def _rows(path: Path) -> list[list[str]]:
    with open(path, newline="") as f:
        return list(csv.reader(f))


# --- the pieces helper ----------------------------------------------------------------


def test_ranges_cover_the_span_in_up_to_ten_pieces():
    out = _pieces("ranges", "0", "24").stdout.split("\n")
    ranges = [tuple(map(int, line.split())) for line in out if line]
    assert ranges[0] == (0, 2) and ranges[-1] == (24, 24)
    assert len(ranges) <= 10
    assert sum(e - s + 1 for s, e in ranges) == 25


def test_split_writes_the_header_and_the_rows_asked_for(tmp_path):
    chunk = _chunk(tmp_path / "lib_chunk_001.csv", [f"C{i}" for i in range(10)])
    _pieces("split", str(chunk), str(tmp_path / "w"), "3", "5")
    assert _rows(tmp_path / "w" / "piece_3_5.csv") == [
        ["smiles"],
        ["C3"],
        ["C4"],
        ["C5"],
    ]


def _result(work: Path, s: int, e: int, header=("key", "input", "value")) -> None:
    rows = [list(header)] + [["k", f"C{i}", "1"] for i in range(s, e + 1)]
    with open(work / f"result_{s}_{e}.csv", "w", newline="") as f:
        csv.writer(f).writerows(rows)


def test_merge_keeps_the_order_and_blanks_bad_molecules(tmp_path):
    chunk = _chunk(tmp_path / "c.csv", [f"C{i}" for i in range(5)])
    work = tmp_path / "w"
    work.mkdir()
    _result(work, 0, 1)
    _result(work, 3, 4)
    (work / "bad_2").touch()
    out, bad = tmp_path / "out.csv", tmp_path / "bad.csv"

    proc = _pieces("merge", str(chunk), str(work), str(out), str(bad))

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "1"
    rows = _rows(out)
    assert [r[1] for r in rows[1:]] == ["C0", "C1", "C2", "C3", "C4"]
    assert rows[3] == [hashlib.md5(b"C2").hexdigest(), "C2", ""]
    assert _rows(bad)[1:] == [["2", hashlib.md5(b"C2").hexdigest(), "C2"]]


def test_a_bad_row_follows_the_models_own_columns(tmp_path):
    """A singularity model need not write key,input; the empty row matches it."""
    chunk = _chunk(tmp_path / "c.csv", ["C0", "C1"])
    work = tmp_path / "w"
    work.mkdir()
    with open(work / "result_0_0.csv", "w", newline="") as f:
        csv.writer(f).writerows([["smiles", "score", "label"], ["C0", "0.5", "x"]])
    (work / "bad_1").touch()
    _pieces(
        "merge", str(chunk), str(work), str(tmp_path / "o.csv"), str(tmp_path / "b.csv")
    )
    assert _rows(tmp_path / "o.csv")[2] == ["C1", "", ""]


@pytest.mark.parametrize(
    "setup",
    ["missing", "overlap", "short"],
)
def test_merge_refuses_pieces_that_do_not_cover_the_chunk(tmp_path, setup):
    chunk = _chunk(tmp_path / "c.csv", [f"C{i}" for i in range(4)])
    work = tmp_path / "w"
    work.mkdir()
    _result(work, 0, 1)
    if setup == "overlap":
        _result(work, 1, 3)
    elif setup == "short":
        _result(work, 2, 3)
        lines = (work / "result_2_3.csv").read_text().splitlines()
        (work / "result_2_3.csv").write_text("\n".join(lines[:-1]) + "\n")
    proc = _pieces(
        "merge", str(chunk), str(work), str(tmp_path / "o.csv"), str(tmp_path / "b.csv")
    )
    assert proc.returncode == 1
    assert not (tmp_path / "o.csv").exists()


def test_merge_with_no_good_piece_cannot_write_a_result(tmp_path):
    chunk = _chunk(tmp_path / "c.csv", ["C0"])
    work = tmp_path / "w"
    work.mkdir()
    (work / "bad_0").touch()
    proc = _pieces(
        "merge", str(chunk), str(work), str(tmp_path / "o.csv"), str(tmp_path / "b.csv")
    )
    assert proc.returncode == 2


# --- the bisect loop, end to end -------------------------------------------------------

FAKE_APPTAINER = r"""#!/bin/bash
# Fails on a piece holding BAD; fails a FLAKY one twice, then succeeds.
while [ $# -gt 0 ]; do
    case "$1" in --input) in="$2"; shift ;; --output) out="$2"; shift ;; esac
    shift
done
grep -q BAD "$in" && exit 1
if grep -q FLAKY "$in"; then
    n=$(cat "$FLAKY_COUNT" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$FLAKY_COUNT"
    [ "$n" -lt 2 ] && exit 1
fi
{ echo "key,input,value"; tail -n +2 "$in" | sed 's/^/k,/; s/$/,1/'; } > "$out"
"""

SBATCH = r"""#!/bin/bash
# Runs every array task at once, then reports like sbatch does.
for arg in "$@"; do
    case "$arg" in --array=0-*) last="${arg#--array=0-}" ;; --*) ;; *) rest+=("$arg") ;; esac
done
id=$((RANDOM + 1000))
echo "$id" >> "$SBATCH_LOG"
for i in $(seq 0 "$last"); do
    SLURM_ARRAY_TASK_ID=$i SLURM_JOB_ID=$id bash "${rest[@]}" >/dev/null 2>&1
done
echo "Submitted batch job $id"
"""


def _submit_and_wait() -> str:
    """The orchestrator's own submit_and_wait, lifted out of the script."""
    text = (SLURM / "submit-ersilia-waves.sh").read_text()
    match = re.search(
        r"^submit_and_wait\(\) \{.*?^\}\n", text, re.DOTALL | re.MULTILINE
    )
    assert match
    return match.group(0)


def _stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def cluster(tmp_path, scheduler):
    """Stub sbatch, squeue and aws, and a fake ersilia_apptainer."""
    bin_dir = scheduler.stub_bin
    _stub(bin_dir / "sbatch", SBATCH)
    _stub(bin_dir / "squeue", "#!/bin/bash\nexit 0\n")
    # Accepts every upload (the shared stub answers "not found" for unknown paths).
    _stub(
        bin_dir / "aws", '#!/bin/bash\nprintf "%s\\n" "$* " >> "$STUB_CALL_LOG.aws"\n'
    )
    apptainer = tmp_path / "ersilia_apptainer"
    _stub(apptainer, FAKE_APPTAINER)
    out = tmp_path / "out"
    out.mkdir()
    env = scheduler.env(
        ERSILIA_APPTAINER=str(apptainer),
        FLAKY_COUNT=str(tmp_path / "flaky"),
        SBATCH_LOG=str(tmp_path / "sbatch-ids"),
    )
    return tmp_path, out, env


def _bisect(
    tmp_path: Path, out: Path, env: dict, chunk: Path
) -> subprocess.CompletedProcess:
    failed = tmp_path / "failed.txt"
    failed.write_text(f"{chunk}\n")
    script = f"""
set -uo pipefail
MODEL_ID=eosx QUEUE=cpu-queue POLL_SECONDS=0 SBATCH_CPUS=()
OUTPUT_DIR={out} S3_OUTPUT=s3://bucket/output/lib/eosx/
RESULT_PREFIX=eosx_results_ RUN_JOB=/nonexistent
BISECT_JOB={SLURM / "run-ersilia-bisect-piece.sh"}
source {SLURM / "bisect.sh"}
{_submit_and_wait()}
bisect_failed {failed}
"""
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.mark.linux_only  # SLURM scripts use GNU grep -P; the cluster is Linux
def test_bisect_rescues_a_chunk_with_two_bad_molecules(cluster, scheduler):
    tmp_path, out, env = cluster
    smiles = [f"C{i}" for i in range(25)]
    smiles[4], smiles[17], smiles[9] = "BAD1", "BAD2", "FLAKY"
    chunk = _chunk(tmp_path / "lib_chunk_007.csv", smiles)

    proc = _bisect(tmp_path, out, env, chunk)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (
        "BISECT: rescued 1 chunk(s); 2 molecule(s) written as empty rows" in proc.stdout
    )
    assert (tmp_path / "failed.txt").read_text() == ""
    # Uploaded, then cleaned off /fsx.
    uploads = [c for c in scheduler.stub_calls("aws") if c.startswith("s3 cp")]
    assert any("eosx_results_007.csv" in c for c in uploads)
    assert any("_bad_smiles_007.csv" in c for c in uploads)
    # Every array the bisect submitted is in the log, where a cancel looks for it.
    ids = (tmp_path / "sbatch-ids").read_text().split()
    for aid in ids:
        assert f"Submitted array job {aid}" in proc.stderr


@pytest.mark.linux_only
def test_bisect_merges_in_order_and_retries_a_lone_failure(
    cluster, scheduler, monkeypatch
):
    tmp_path, out, env = cluster
    smiles = [f"C{i}" for i in range(25)]
    smiles[4], smiles[9] = "BAD1", "FLAKY"
    chunk = _chunk(tmp_path / "lib_chunk_008.csv", smiles)
    # Keep the merged files: make the upload fail after the merge.
    _stub(scheduler.stub_bin / "aws", "#!/bin/bash\nexit 1\n")

    proc = _bisect(tmp_path, out, env, chunk)

    assert "chunk 008: could not be rescued" in proc.stdout  # upload failed on purpose
    # The pieces and the merge itself were fine: re-merge from the kept workspace.
    work = out / "_bisect" / "008"
    merged, bad = tmp_path / "m.csv", tmp_path / "b.csv"
    assert (
        _pieces("merge", str(chunk), str(work), str(merged), str(bad)).returncode == 0
    )
    rows = _rows(merged)
    assert [r[1] for r in rows[1:]] == smiles
    assert rows[5][2] == ""  # BAD1, blanked
    assert rows[10][2] == "1"  # FLAKY failed twice, then passed its retry: kept
    assert [r[2] for r in _rows(bad)[1:]] == ["BAD1"]


@pytest.mark.linux_only
def test_a_chunk_where_nothing_runs_stays_failed(cluster):
    tmp_path, out, env = cluster
    chunk = _chunk(tmp_path / "lib_chunk_009.csv", ["BAD1", "BAD2", "BAD3"])

    proc = _bisect(tmp_path, out, env, chunk)

    assert "chunk 009: could not be rescued" in proc.stdout
    assert (tmp_path / "failed.txt").read_text().strip() == str(chunk)


# --- the driver's note ------------------------------------------------------------------


def test_the_note_comes_from_this_runs_bisect_only(tmp_path):
    log = tmp_path / "job.log"
    log.write_text("BISECT: rescued 1 chunk(s); 5 molecule(s) written as empty rows\n")
    start = log.stat().st_size
    with log.open("a") as f:
        f.write("All waves complete\n")

    assert bash_eval(f'bisect_note "{log}" {start}').stdout == ""
    with log.open("a") as f:
        f.write("BISECT: rescued 2 chunk(s); 3 molecule(s) written as empty rows\n")
    note = bash_eval(f'bisect_note "{log}" {start}').stdout.strip()
    assert note == "3 molecule(s) failed and were left empty; see _bad_smiles_*.csv"


def test_pieces_and_results_use_plain_newlines(tmp_path):
    """csv's default \\r\\n would hand a model "C0\\r" as its SMILES."""
    chunk = _chunk(tmp_path / "c.csv", ["C0", "C1"])
    _pieces("split", str(chunk), str(tmp_path / "w"), "0", "1")
    assert b"\r" not in (tmp_path / "w" / "piece_0_1.csv").read_bytes()
