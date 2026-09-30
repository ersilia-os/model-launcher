"""``model-launcher setup``, without the screen: the conf it writes, what it finds.

The conf is bash that every scheduler script sources, so the tests check it the
way the scripts will read it, not only the way Python writes it.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from model_launcher.core import hostconf
from model_launcher.core.model import parse_dump
from model_launcher.core.remote import remote_dir


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh home directory, with no ersilia anywhere on PATH."""
    home = tmp_path / "home"
    home.mkdir()
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{empty}:/usr/bin:/bin")
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    return home


def _config(home: Path, **changes: str) -> hostconf.HostConfig:
    root = home / "ml"
    fields = {
        "data_dir": str(root / "data"),
        "ersilia_bin": "/usr/bin/true",
        "log_dir": str(root / "logs"),
        "queue_file": str(root / "models.queue"),
        "default_library": "",
        **changes,
    }
    return hostconf.HostConfig(**fields)


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# --- the conf ---------------------------------------------------------------------


def test_what_is_saved_is_what_is_loaded(home):
    cfg = _config(home, default_library="lib1")
    hostconf.save(cfg)
    assert hostconf.load() == cfg


def test_other_lines_of_an_existing_conf_are_kept(home):
    conf = hostconf.conf_path()
    conf.parent.mkdir(parents=True)
    conf.write_text(
        '# my own note\nSIF_DIR="${SIF_DIR:-/opt/sif}"\nDATA_DIR="${DATA_DIR:-/old}"\n'
    )
    hostconf.save(_config(home))
    text = conf.read_text()

    assert "# my own note" in text
    assert 'SIF_DIR="${SIF_DIR:-/opt/sif}"' in text
    assert "/old" not in text
    assert text.count("DATA_DIR=") == 1


def test_every_written_line_keeps_the_defaults_form(home):
    """A plain VAR=value would override the environment and --log-dir."""
    text = hostconf.render(_config(home), 'SIF_DIR="${SIF_DIR:-/opt/sif}"\n')
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, _, value = line.partition("=")
        assert f"${{{name}:-" in value, line


def test_the_scheduler_reads_what_setup_writes(scheduler, home):
    cfg = _config(home)
    hostconf.save(cfg)
    proc = scheduler.ctl("dump", SCHEDULER_CONF=str(hostconf.conf_path()))
    snap = parse_dump(proc.stdout)
    assert snap.dispatch == "serve"
    assert snap.data_dir == cfg.data_dir


def test_saving_creates_the_folders_and_an_empty_queue(home):
    cfg = _config(home)
    done = hostconf.save(cfg)

    for folder in ("input", "output"):
        assert (Path(cfg.data_dir) / folder).is_dir()
    assert Path(cfg.log_dir).is_dir()
    queue = Path(cfg.queue_file).read_text()
    assert all(line.startswith("#") or not line.strip() for line in queue.splitlines())
    assert any("wrote" in line for line in done)


def test_an_existing_queue_is_left_alone(home):
    cfg = _config(home)
    Path(cfg.queue_file).parent.mkdir(parents=True)
    Path(cfg.queue_file).write_text("eos3b5e ersilia lib1\n")
    hostconf.save(cfg)
    assert Path(cfg.queue_file).read_text() == "eos3b5e ersilia lib1\n"


# --- finding ersilia ----------------------------------------------------------------


def test_ersilia_is_found_in_conda_envs_the_ersilia_env_first(home):
    other = _executable(home / "miniconda3" / "envs" / "chem" / "bin" / "ersilia")
    named = _executable(home / "miniconda3" / "envs" / "ersilia" / "bin" / "ersilia")
    found = [p for p in hostconf.ersilia_candidates() if p.startswith(str(home))]
    assert found == [str(named), str(other)]
    assert hostconf.defaults().ersilia_bin == str(named)


def test_ersilia_on_path_comes_before_any_env(home, tmp_path):
    _executable(home / "miniconda3" / "envs" / "ersilia" / "bin" / "ersilia")
    on_path = _executable(tmp_path / "empty-bin" / "ersilia")
    assert hostconf.ersilia_candidates()[0] == str(on_path)


# --- checks ---------------------------------------------------------------------------


def _levels(checks: list[hostconf.Check], prefix: str) -> list[str]:
    return [c.level for c in checks if c.text.startswith(prefix)]


def test_a_relative_or_unsafe_path_blocks_saving(home):
    assert _levels(
        hostconf.quick_checks(_config(home, data_dir="data")), "data folder"
    ) == ["bad"]
    spaced = str(home / "my data")
    assert _levels(
        hostconf.quick_checks(_config(home, data_dir=spaced)), "data folder"
    ) == ["bad"]


def test_a_missing_data_folder_will_be_created(home):
    assert _levels(hostconf.quick_checks(_config(home)), "data folder") == ["warn"]


def test_existing_libraries_are_counted(home):
    cfg = _config(home, default_library="lib1")
    (Path(cfg.data_dir) / "input" / "lib1").mkdir(parents=True)
    checks = hostconf.quick_checks(cfg)
    assert any(c.text == "data folder: 1 library in input/" for c in checks)
    assert _levels(checks, "default library") == ["ok"]


def test_ersilia_must_be_an_executable(home):
    assert _levels(hostconf.quick_checks(_config(home, ersilia_bin="")), "ersilia") == [
        "bad"
    ]
    missing = str(home / "nope" / "ersilia")
    assert _levels(
        hostconf.quick_checks(_config(home, ersilia_bin=missing)), "ersilia"
    ) == ["bad"]


def test_a_default_library_not_there_yet_is_a_warning(home):
    checks = hostconf.quick_checks(_config(home, default_library="lib9"))
    assert _levels(checks, "default library") == ["warn"]


# --- starting it ------------------------------------------------------------------------


def test_the_service_command_names_the_queue_and_the_conf(home):
    cfg = _config(home, default_library="lib1")
    argv, env = hostconf.service_command(cfg, print_only=True)
    assert argv[1].endswith("install-scheduler-service.sh")
    assert argv[2:] == ["--print", cfg.queue_file, "lib1"]
    assert env == {"SCHEDULER_CONF": str(hostconf.conf_path())}


def test_the_tmux_launcher_uses_the_confs_log_dir(scheduler, home, tmp_path):
    """It used to inline /shared/logs/scheduler whatever the conf said."""
    cfg = _config(home)
    hostconf.save(cfg)
    record = tmp_path / "tmux-args"
    tmux = scheduler.stub_bin / "tmux"
    tmux.write_text(
        '#!/bin/bash\n[ "$1" = has-session ] && exit 1\n'
        f'printf "%s\\n" "$@" > "{record}"\n'
    )
    tmux.chmod(0o755)
    env = scheduler.env(SCHEDULER_CONF=str(hostconf.conf_path()), HOME=str(home))
    env.pop("LOG_DIR")
    subprocess.run(
        ["bash", str(remote_dir() / "start-scheduler-tmux.sh"), cfg.queue_file],
        env={**env, "SCHEDULER_UNIT": "no-such-unit"},
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    command = record.read_text()
    assert f"LOG_DIR={cfg.log_dir}" in command
    assert "DISPATCH=serve" in command
    assert os.path.isdir(cfg.log_dir)
