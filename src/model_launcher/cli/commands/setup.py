"""Make the computer you are on a host that runs models."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import click
from rich_click import RichCommand

from ...branding import console
from ...core import hostconf
from .check import check

#: How long to wait for a freshly installed service's driver to come up.
START_WAIT = 10.0


@click.command(cls=RichCommand)
@click.pass_context
def setup(ctx):
    """Set up this computer to run models with the ersilia CLI.

    Opens a screen for the settings (data folder, ersilia CLI, logs, queue),
    checks them, saves ~/.config/model-launcher/scheduler.conf, and can install
    the driver as a service: systemd on Linux, a LaunchAgent on macOS.
    """
    # Imported here so --help works even where Textual is missing.
    from ...tui.setup import SetupApp

    out, err = console(), console(stderr=True)
    result = SetupApp().run()
    if result is None:
        out.print("[muted]Nothing written.[/muted]")
        return
    cfg, start = result

    out.print("[heading]Saved[/heading]")
    for line in hostconf.save(cfg):
        out.print(f"  [ok]✓[/ok] {line}")

    argv, extra = hostconf.service_command(cfg)
    env = {**os.environ, **extra}
    kind = hostconf.service_kind()
    prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in extra.items())
    if start != "service":
        out.print()
        out.print("To start the driver later, as a service:")
        out.print(f"  {prefix} {shlex.join(argv)}", soft_wrap=True, highlight=False)
    else:
        preview, _ = hostconf.service_command(cfg, print_only=True)
        shown = subprocess.run(
            preview, env=env, capture_output=True, text=True, check=False
        )
        if shown.returncode != 0:
            err.print(f"[bad]FAILED[/bad] {shown.stderr.strip()}")
            sys.exit(1)
        out.print()
        out.print(f"[heading]Installing the {kind} service[/heading]")
        review = f"{prefix} {shlex.join(preview)}"
        out.print(
            f"  [muted]to review the service file: {review}[/muted]",
            soft_wrap=True,
            highlight=False,
        )
        out.print()
        # In this terminal, not captured: the installer may ask for a password.
        rc = subprocess.run(argv, env=env, check=False).returncode
        if rc != 0:
            err.print(f"[bad]FAILED[/bad] the {kind} installer exited with {rc}")
            sys.exit(rc)
        _wait_for_driver(Path(cfg.log_dir))

    out.print()
    ctx.obj["log_dir"] = cfg.log_dir
    ctx.obj["queue_file"] = cfg.queue_file
    ctx.invoke(check)


def _wait_for_driver(log_dir: Path) -> None:
    """Give a just-started service's driver a moment to announce itself."""
    deadline = time.monotonic() + START_WAIT
    while time.monotonic() < deadline and not (log_dir / "driver.info").exists():
        time.sleep(0.25)
