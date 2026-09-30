"""The setup screen: editing rules, and the screen driven with Textual's pilot."""

from __future__ import annotations

import asyncio
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from model_launcher.core import hostconf
from model_launcher.tui import draw
from model_launcher.tui.setup import FIELDS, SetupApp, SetupForm, SetupScreen


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home with two ersilia envs, and no ersilia on PATH."""
    home = tmp_path / "home"
    for env in ("ersilia", "chem"):
        cli = home / "miniconda3" / "envs" / env / "bin" / "ersilia"
        cli.parent.mkdir(parents=True)
        cli.write_text("#!/bin/sh\n")
        cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{empty}:/usr/bin:/bin")
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    return home


def _env(home: Path, name: str) -> str:
    return str(home / "miniconda3" / "envs" / name / "bin" / "ersilia")


# --- the form, without Textual -----------------------------------------------------


def test_the_form_starts_from_the_defaults_with_the_ersilia_env(home):
    form = SetupForm(hostconf.defaults(), "systemd")
    assert form.config.ersilia_bin == _env(home, "ersilia")
    assert form.field() == "data_dir"
    assert form.start_choice == "service"


def test_typing_edits_only_the_focused_field(home):
    form = SetupForm(hostconf.defaults(), "systemd")
    form.clear()
    form.type("/data/ml")
    form.backspace()
    assert form.config.data_dir == "/data/m"
    assert form.config.log_dir == hostconf.defaults().log_dir


def test_tab_moves_on_without_replacing_a_value(home):
    """The popup lists the other ersilia; tab alone must not pick it."""
    form = SetupForm(hostconf.defaults(), "systemd")
    form.tab()
    assert form.field() == "ersilia_bin"
    assert form.completions() == [_env(home, "chem")]
    form.tab()
    assert form.config.ersilia_bin == _env(home, "ersilia")


def test_down_enters_the_popup_and_tab_takes_it(home):
    form = SetupForm(hostconf.defaults(), "systemd")
    form.tab()
    form.move(1)
    assert form.highlighted == 0
    form.tab()
    assert form.config.ersilia_bin == _env(home, "chem")
    assert form.field() == "log_dir"


def test_focus_wraps_round_through_the_start_choice(home):
    form = SetupForm(hostconf.defaults(), "launchd")
    form.move_focus(-1)
    assert form.on_start
    form.choose_start(1)
    assert form.start_choice == "none"
    assert form.start_labels()[0] == "install the launchd service"


def test_every_drawn_hint_does_something():
    drawn = {key for group in draw.SETUP_KEYS for key, _ in group}
    keys = {part for key in drawn for part in key.split("/")}
    assert keys <= set(SetupScreen.HINTS)


# --- the screen ------------------------------------------------------------------------


def _run(cfg, keys):
    """Drive the app with ``keys`` and return what it exited with."""

    async def scenario():
        app = SetupApp(cfg)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause(0.1)
            for key in keys:
                await pilot.press(key)
            await pilot.pause(0.1)
            screen = app.screen
            return app.return_value, screen

    return asyncio.run(scenario())


def test_ctrl_s_returns_the_settings_and_the_start_choice(home, tmp_path):
    data = str(tmp_path / "d")
    keys = ["ctrl+u", *data, "tab", "down", "tab", "shift+tab", "ctrl+s"]
    result, _ = _run(hostconf.defaults(), keys)
    cfg, start = result
    assert cfg.data_dir == data
    assert cfg.ersilia_bin == _env(home, "chem")
    assert start == "service"


def test_a_failing_check_keeps_the_screen_open(home):
    cfg = replace(hostconf.defaults(), ersilia_bin="/no/such/ersilia")
    result, screen = _run(cfg, ["ctrl+s"])
    assert result is None
    assert isinstance(screen, SetupScreen)
    assert "to fix first" in screen.problem


def test_esc_writes_nothing(home):
    result, _ = _run(hostconf.defaults(), ["escape"])
    assert result is None
    assert not hostconf.conf_path().exists()


def test_the_start_choice_follows_the_arrows(home):
    keys = ["shift+tab", "right", "ctrl+s"]
    (_, start), _ = _run(hostconf.defaults(), keys)
    assert start == "none"


def test_the_fields_are_the_settings_setup_saves():
    assert {name for name, _, _ in FIELDS} == set(
        hostconf.HostConfig.__dataclass_fields__
    )
