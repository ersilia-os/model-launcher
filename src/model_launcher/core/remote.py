"""Locating the bash layer that ships inside this package.

The scheduler proper is ~2,500 lines of bash under ``model_launcher/remote``.
It is shipped as package data so that the Python client and the scripts it
drives are versioned together — the previous arrangement kept them in two
directories synced separately, which repeatedly produced a client talking to a
scheduler that did not have the feature it was asking for.

Nothing here reads or executes the scripts; it only resolves paths.
"""

from __future__ import annotations

from importlib.resources import as_file, files
from pathlib import Path
from typing import List

#: Basename of the control CLI — the only server-side entry point the client uses.
CTL_NAME = "sched-ctl.sh"


def remote_dir() -> Path:
    """Return the directory holding the packaged bash layer.

    Returns
    -------
    Path
        Absolute path to ``model_launcher/remote``.
    """
    resource = files("model_launcher").joinpath("remote")
    with as_file(resource) as path:
        return Path(path)


def ctl_path() -> Path:
    """Return the path to the packaged ``sched-ctl.sh``.

    Returns
    -------
    Path
        Absolute path to the control CLI shipped with this package.
    """
    return remote_dir() / CTL_NAME


def payload_files() -> List[Path]:
    """List every file that makes up the deployable bash layer.

    Returns
    -------
    list of Path
        All regular files under :func:`remote_dir`, sorted, excluding the
        example queue (which is documentation, not code).
    """
    root = remote_dir()
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name != "example.queue"
    )
