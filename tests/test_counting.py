"""M4: progress counting follows ``DISPATCH`` — S3 on ``slurm``, ``DATA_DIR`` on ``serve``.

Listing is pluggable; the patterns that turn a listing into a count are shared,
so both stores must agree on what a chunk and a result are. The rest covers the
input pre-flight: a count of 0 has several causes, and ``missing-files`` is
never retried, so the note has to name the real one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import bash_eval


def _touch(folder: Path, *names: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_text("smiles\n")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A local store: 3 input chunks for ``lib1`` and mixed results for two modes."""
    root = tmp_path / "data"
    _touch(root / "input" / "lib1", *(f"lib1_chunk_{i:03d}.csv" for i in (1, 2, 3)))
    _touch(root / "input" / "lib1", "README.txt")
    (root / "input" / "empty").mkdir(parents=True)
    _touch(
        root / "output" / "lib1" / "eos0aaa",
        "eos0aaa_results_001.csv",
        "eos0aaa_results_002.csv",
        "eos0aaa_003.csv",
    )
    return root


def _serve_env(data_dir: Path) -> dict[str, str]:
    return {"DISPATCH": "serve", "DATA_DIR": str(data_dir), "SCHED_FAKE_S3": "1"}


def _eval(snippet: str, env: dict[str, str]) -> str:
    proc = bash_eval(snippet, env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


# --- local counting -----------------------------------------------------------


def test_local_store_counts_chunks_and_results_per_mode(data_dir):
    # SCHED_FAKE_S3=1 is set on purpose: the fake fakes S3 only, never a folder.
    env = _serve_env(data_dir)

    assert _eval("count_input lib1", env) == "3"
    assert _eval("count_output eos0aaa lib1 ersilia", env) == "2"
    assert _eval("count_output eos0aaa lib1 singularity", env) == "1"
    assert _eval("count_output eos0aaa nolib ersilia", env) == "0"


def test_local_store_lists_its_library_folders(data_dir):
    assert _eval("list_libraries", _serve_env(data_dir)).split() == ["empty", "lib1"]


def test_input_problem_names_the_cause_on_a_local_store(data_dir):
    env = _serve_env(data_dir)

    assert _eval("input_problem lib1", env) == ""
    assert _eval("input_problem empty", env) == (
        f"no input chunks in {data_dir}/input/empty/"
    )
    assert _eval("input_problem typo", env) == (
        f"input dir does not exist: {data_dir}/input/typo/"
    )


# --- S3: an unreachable bucket must not read as an empty library ----------------


def test_input_problem_separates_empty_s3_from_unreachable_s3(scheduler):
    env = scheduler.env(SCHED_FAKE_S3="0", SCHED_INPUT_CACHE_TTL="0")

    assert _eval("input_problem testlib", env) == ""
    assert _eval("input_problem nolib", env) == (
        "no input chunks in s3://test-bucket/input/nolib/"
    )
    failing = {**env, "AWS_STUB_FAIL": "Unable to locate credentials."}
    assert _eval("input_problem testlib", failing) == (
        "cannot list s3://test-bucket/input/testlib/: Unable to locate credentials."
    )


# --- the driver's pre-flight, end to end ----------------------------------------


def test_serve_driver_marks_a_mistyped_library_missing_files_with_the_cause(
    scheduler, data_dir
):
    scheduler.write_queue("eos0aaa ersilia typo")
    scheduler.start_driver(**_serve_env(data_dir))

    scheduler.wait_for_status("eos0aaa", "missing-files")
    job = scheduler.dump().find("eos0aaa")
    assert job.note == f"input dir does not exist: {data_dir}/input/typo/"


def test_serve_driver_skips_a_job_whose_results_are_all_on_disk(scheduler, data_dir):
    _touch(
        data_dir / "output" / "lib1" / "eos0bbb",
        *(f"eos0bbb_results_{i:03d}.csv" for i in (1, 2, 3)),
    )
    scheduler.write_queue("eos0bbb ersilia lib1")
    scheduler.start_driver(**_serve_env(data_dir))

    scheduler.wait_for_status("eos0bbb", "done")
    assert scheduler.dump().find("eos0bbb").note == "already complete"


# --- configuration that would otherwise fail silently ---------------------------


def test_ctl_refuses_serve_without_a_data_dir(scheduler):
    proc = scheduler.ctl("dump", DISPATCH="serve")

    assert proc.returncode != 0
    assert "DATA_DIR" in proc.stderr


def test_ctl_refuses_an_unknown_dispatch(scheduler):
    proc = scheduler.ctl("dump", DISPATCH="lsf")

    assert proc.returncode != 0
    assert "slurm or serve" in proc.stderr
