"""``model-launcher setup``: what happens after the screen closes.

The screen is replaced by its result; the service installer really runs, against
recording stubs for sudo, systemctl and launchctl, so nothing is installed.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from model_launcher.cli import create_cli
from model_launcher.cli.commands import setup as setup_cmd
from model_launcher.core import hostconf
from model_launcher.tui import setup as setup_screen

STUB = """#!/bin/bash
printf '%s\\n' "$(basename "$0") $*" >> "{log}"
case "$(basename "$0") $1" in
    "systemctl is-active"|"launchctl print") exit 1 ;;
esac
exit 0
"""


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A tmp home, and stubs for everything that would change the system."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    for name in ("sudo", "systemctl", "launchctl", "ersilia"):
        stub = bin_dir / name
        stub.write_text(STUB.format(log=log))
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("SCHED_REGISTRY_DIR", str(tmp_path / "drivers"))
    monkeypatch.setattr(setup_cmd, "START_WAIT", 0.1)
    root = home / "ml"
    cfg = hostconf.HostConfig(
        data_dir=str(root / "data"),
        ersilia_bin=str(bin_dir / "ersilia"),
        log_dir=str(root / "logs"),
        queue_file=str(root / "models.queue"),
    )
    return cfg, log


def _run(monkeypatch, result):
    monkeypatch.setattr(setup_screen.SetupApp, "run", lambda self: result)
    return CliRunner().invoke(create_cli.cli, ["setup"], catch_exceptions=False)


def _calls(log: Path) -> str:
    return log.read_text() if log.exists() else ""


def test_saving_and_installing_the_service(machine, monkeypatch):
    cfg, log = machine
    proc = _run(monkeypatch, (cfg, "service"))

    assert proc.exit_code == 0, proc.output
    conf = hostconf.conf_path().read_text()
    assert 'DISPATCH="${DISPATCH:-serve}"' in conf
    assert Path(cfg.queue_file).is_file()
    calls = _calls(log)
    assert ("sudo install" in calls) or ("launchctl bootstrap" in calls)
    assert "serve" in proc.output  # check's summary: dispatch serve


def test_dont_start_prints_the_command_and_runs_nothing(machine, monkeypatch):
    cfg, log = machine
    proc = _run(monkeypatch, (cfg, "none"))

    assert proc.exit_code == 0, proc.output
    assert "install-scheduler-service.sh" in proc.output
    assert _calls(log) == ""
    assert hostconf.conf_path().is_file()


def test_leaving_the_screen_writes_nothing(machine, monkeypatch):
    proc = _run(monkeypatch, None)

    assert proc.exit_code == 0
    assert "Nothing written" in proc.output
    assert not hostconf.conf_path().exists()


def test_setup_is_listed_in_help():
    proc = CliRunner().invoke(create_cli.cli, ["--help"])
    assert "setup" in proc.output
