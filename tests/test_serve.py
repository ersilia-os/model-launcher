"""M5: ``DISPATCH=serve`` runs a model with the plain ersilia CLI.

``fetch``, ``serve``, one ``run`` per chunk, ``close`` — against a fake
``ersilia`` on PATH that records its calls. Covers the three things the loop
adds on top: ``close`` on every exit (cancel included), resuming from the chunks
already done, and never counting a result that was not fully written.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from model_launcher.core.remote import remote_dir

SERVE = remote_dir() / "serve" / "run-ersilia-serve.sh"
INSTALL = remote_dir() / "install-scheduler-service.sh"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """``lib1`` with three chunks of two molecules each."""
    root = tmp_path / "data"
    lib = root / "input" / "lib1"
    lib.mkdir(parents=True)
    for i in (1, 2, 3):
        (lib / f"lib1_chunk_{i:03d}.csv").write_text("smiles\nCCO\nCCN\n")
    return root


def _results(data_dir: Path) -> Path:
    return data_dir / "output" / "lib1" / "eos0aaa"


def _serve_env(data_dir: Path, **extra: str) -> dict[str, str]:
    return {"DISPATCH": "serve", "DATA_DIR": str(data_dir), **extra}


def _verbs(scheduler) -> list[str]:
    return [call.split()[0] for call in scheduler.stub_calls("ersilia")]


def _start(scheduler, data_dir: Path, **extra: str):
    scheduler.write_queue("eos0aaa ersilia lib1")
    return scheduler.start_driver(dry_run=False, **_serve_env(data_dir, **extra))


def test_a_serve_job_fetches_serves_runs_every_chunk_and_closes(scheduler, data_dir):
    _start(scheduler, data_dir)
    scheduler.wait_for_status("eos0aaa", "done")

    assert _verbs(scheduler) == ["fetch", "serve", "run", "run", "run", "close"]
    out = _results(data_dir)
    names = sorted(p.name for p in out.iterdir())
    assert names == [f"eos0aaa_results_{i:03d}.csv" for i in (1, 2, 3)]
    assert (out / "eos0aaa_results_001.csv").read_text().splitlines() == [
        "key,input,value",
        "k,CCO,1",
        "k,CCN,1",
    ]
    assert scheduler.dump().find("eos0aaa").done == 3


def test_a_serve_job_runs_only_the_chunks_without_a_result(scheduler, data_dir):
    out = _results(data_dir)
    out.mkdir(parents=True)
    (out / "eos0aaa_results_002.csv").write_text("key,input,value\n")

    _start(scheduler, data_dir)
    scheduler.wait_for_status("eos0aaa", "done")

    runs = [c for c in scheduler.stub_calls("ersilia") if c.startswith("run ")]
    assert [Path(c.split()[2]).name for c in runs] == [
        "lib1_chunk_001.csv",
        "lib1_chunk_003.csv",
    ]


def test_with_every_chunk_done_the_model_is_never_served(scheduler, data_dir):
    out = _results(data_dir)
    out.mkdir(parents=True)
    for i in (1, 2, 3):
        (out / f"eos0aaa_results_{i:03d}.csv").write_text("key,input,value\n")

    proc = subprocess.run(
        ["bash", str(SERVE), "eos0aaa", "lib1"],
        env=scheduler.env(**_serve_env(data_dir)),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout
    assert scheduler.stub_calls("ersilia") == []
    assert not (out / "_work").exists()


def test_a_failed_run_fails_the_job_closes_the_model_and_a_retry_resumes(
    scheduler, data_dir, tmp_path
):
    fail = tmp_path / "fail-on"
    fail.write_text("lib1_chunk_002.csv\n")
    _start(scheduler, data_dir, FAKE_ERSILIA_FAIL_FILE=str(fail))

    scheduler.wait_for_status("eos0aaa", "failed")
    assert _verbs(scheduler) == ["fetch", "serve", "run", "run", "close"]
    out = _results(data_dir)
    # Chunk 1 is kept; the failed chunk left nothing behind that could count.
    assert sorted(p.name for p in out.iterdir()) == ["eos0aaa_results_001.csv"]

    fail.unlink()
    scheduler.forget_stub_calls("ersilia")
    scheduler.ctl("retry", "eos0aaa", check=True)
    scheduler.wait_for_status("eos0aaa", "done")

    runs = [c for c in scheduler.stub_calls("ersilia") if c.startswith("run ")]
    assert [Path(c.split()[2]).name for c in runs] == [
        "lib1_chunk_002.csv",
        "lib1_chunk_003.csv",
    ]


def test_cancelling_a_serve_job_closes_the_model(scheduler, data_dir):
    _start(scheduler, data_dir, FAKE_ERSILIA_SLOW="600")
    scheduler.wait_for_status("eos0aaa", "running")
    assert _wait_for_run(scheduler)

    scheduler.ctl("cancel", "eos0aaa", check=True)
    scheduler.wait_for_status("eos0aaa", "cancelled")

    assert _verbs(scheduler)[-1] == "close"
    assert scheduler.sleep_children() == []
    out = _results(data_dir)
    assert not any(out.glob("*.csv"))
    assert not (out / "_work").exists()


def _wait_for_run(scheduler, timeout: float = 20.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "run" in _verbs(scheduler):
            return True
        time.sleep(0.05)
    return False


# --- singularity has no meaning without SLURM -------------------------------


def test_singularity_is_skipped_on_a_serve_machine(scheduler, data_dir):
    scheduler.write_queue("eos0aaa singularity lib1")
    scheduler.start_driver(**_serve_env(data_dir))

    scheduler.wait_for_status("eos0aaa", "skipped")
    # The reason itself is not visible anywhere yet (skipped rows carry no note;
    # see test_unknown_mode_is_skipped_with_a_reason), so pin the verdict only.
    assert scheduler.stub_calls("ersilia") == []


def test_ctl_add_refuses_singularity_on_a_serve_machine(scheduler, data_dir):
    proc = scheduler.ctl(
        "add", "eos0aaa", "singularity", "lib1", **_serve_env(data_dir)
    )

    assert proc.returncode != 0
    assert "DISPATCH=slurm" in proc.stderr


def test_a_serve_driver_needs_the_ersilia_cli(scheduler, data_dir):
    proc = subprocess.run(
        ["bash", str(remote_dir() / "run-model-queue.sh"), str(scheduler.queue_file)],
        capture_output=True,
        check=False,
        text=True,
        env=scheduler.env(**_serve_env(data_dir, ERSILIA_BIN="/nonexistent/ersilia")),
        cwd=scheduler.root,
        timeout=30,
    )
    assert proc.returncode != 0
    assert "ERSILIA_BIN" in proc.stderr


# --- the service unit on a machine without SLURM -----------------------------


def test_the_serve_unit_needs_no_slurm_and_carries_home(scheduler, data_dir, tmp_path):
    bin_dir = tmp_path / "only-ersilia"
    bin_dir.mkdir()
    (bin_dir / "ersilia").symlink_to(scheduler.stub_bin / "ersilia")
    proc = subprocess.run(
        ["bash", str(INSTALL), "--print", str(scheduler.queue_file), "lib1"],
        env=scheduler.env(
            **_serve_env(data_dir),
            PATH=f"{bin_dir}:/usr/bin:/bin",
            SCHED_SERVICE_OS="Linux",
        ),
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    unit = proc.stdout
    assert 'Environment="DISPATCH=serve"' in unit
    assert f'Environment="DATA_DIR={data_dir}"' in unit
    assert f'Environment="HOME={os.environ["HOME"]}"' in unit
    assert "docker.service" in unit


def test_the_add_panel_offers_only_ersilia_on_a_serve_target():
    from model_launcher.tui.overlays import CommandLine

    line = CommandLine([], {"library": "lib1"}, 32, modes=["ersilia"])
    line.text = "eos0aaa "
    assert [m for m, _ in line.completions()] == ["ersilia"]
    line.text = "eos0aaa singularity lib1"
    assert line.error() == "mode must be ersilia"


def test_a_conf_in_the_user_config_folder_is_found_without_being_named(
    scheduler, data_dir, tmp_path
):
    """A pip-installed copy keeps its conf in ~/.config/model-launcher, and
    sched-ctl.sh run over SSH must still see DISPATCH=serve there."""
    home = tmp_path / "home"
    conf = home / ".config" / "model-launcher" / "scheduler.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        f'DISPATCH="${{DISPATCH:-serve}}"\nDATA_DIR="${{DATA_DIR:-{data_dir}}}"\n'
    )
    env = scheduler.env(HOME=str(home))
    env.pop("SCHEDULER_CONF")
    proc = subprocess.run(
        [
            "bash",
            str(remote_dir() / "sched-ctl.sh"),
            "--log-dir",
            str(scheduler.log_dir),
            "-q",
            str(scheduler.queue_file),
            "dump",
        ],
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert "dispatch=serve" in proc.stdout
    assert f"data_dir={data_dir}" in proc.stdout
