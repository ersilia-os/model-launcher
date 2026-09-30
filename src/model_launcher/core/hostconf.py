"""Configuring the computer you are on as a host that runs models.

A serve host (``DISPATCH=serve``) runs models with the ersilia CLI and keeps its
input and results in a local folder. Its settings live in
``~/.config/model-launcher/scheduler.conf``, which every scheduler script reads
when there is no conf beside it. That file is bash, in the ``VAR="${VAR:-value}"``
form the scripts require, so this module reads and writes only that form and
keeps every other line of an existing file as it was.

Nothing here runs ``ersilia``: importing its CLI stops other sessions' orphaned
servers, which is not a side effect a settings check may have.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from .remote import remote_dir

#: The settings setup writes; any other line of an existing conf is kept.
MANAGED = ("DISPATCH", "DATA_DIR", "ERSILIA_BIN", "LOG_DIR")

#: What the service needs besides the conf, kept as comments in it.
SETUP_KEYS = ("queue_file", "default_library")

HEADER = "# Written by `model-launcher setup`. Rerun it to change these settings."
_SETUP_COMMENT = "# setup: "
_CONF_LINE = re.compile(r'^\s*([A-Z_][A-Z0-9_]*)="\$\{\1:-(.*)\}"\s*$')

#: Characters a path cannot hold: systemd and the installer split or expand
#: them, and `}` would end the ${VAR:-...} it is written into.
_UNSAFE = re.compile(r"""[\s%$\\"'`}]""")

#: Where conda-style installs keep environments, besides ``$CONDA_PREFIX``.
_CONDA_ROOTS = (
    "~/miniconda3",
    "~/anaconda3",
    "~/miniforge3",
    "~/mambaforge",
    "~/micromamba",
    "/opt/miniconda3",
    "/opt/anaconda3",
    "/opt/homebrew/Caskroom/miniconda/base",
    "/usr/local/Caskroom/miniconda/base",
)

QUEUE_HEADER = """\
# model-launcher queue: one job per line, run top to bottom.
#
#   <model_id>  ersilia  [library]
#
# library is a folder under DATA_DIR/input; leave it out to use the default one.
# Add jobs from the dashboard (`model-launcher`, then `a`) rather than by hand.
"""


@dataclass(frozen=True)
class HostConfig:
    """What makes this computer a serve host.

    Attributes
    ----------
    data_dir : str
        Folder holding ``input/<library>/`` and ``output/<library>/<model>/``.
    ersilia_bin : str
        The ersilia CLI each job runs.
    log_dir : str
        The scheduler's state: queue lock, status, per-job logs.
    queue_file : str
        The queue the driver runs.
    default_library : str
        Library for queue lines that name none; may be empty.
    """

    data_dir: str
    ersilia_bin: str
    log_dir: str
    queue_file: str
    default_library: str = ""


@dataclass(frozen=True)
class Check:
    """One line of the setup checks.

    Attributes
    ----------
    level : str
        ``ok``, ``note``, ``warn`` or ``bad``. Any ``bad`` blocks saving.
    text : str
        What was found, and what to do about it.
    """

    level: str
    text: str


def conf_path() -> Path:
    """Return the conf file setup writes: ``~/.config/model-launcher/scheduler.conf``.

    Built from ``$HOME``, like the scripts' own lookup (not ``XDG_CONFIG_HOME``,
    which they do not read).
    """
    return (
        Path(os.path.expanduser("~")) / ".config" / "model-launcher" / "scheduler.conf"
    )


def platform_name() -> str:
    """Return ``macOS`` or ``Linux`` (anything else reads as its own name)."""
    return {"darwin": "macOS", "linux": "Linux"}.get(sys.platform, sys.platform)


def service_kind() -> str:
    """Return the service the installer sets up here: ``launchd`` or ``systemd``."""
    return "launchd" if sys.platform == "darwin" else "systemd"


def ersilia_candidates() -> list[str]:
    """Find ersilia CLIs on this computer, most likely first.

    Returns
    -------
    list of str
        Executable ``ersilia`` paths: on PATH, in the active conda env, then in
        every env of the usual conda installs (an env named ``ersilia`` first).
        Duplicates are dropped.
    """
    found = [shutil.which("ersilia") or ""]
    prefix = os.environ.get("CONDA_PREFIX")
    if prefix:
        found.append(os.path.join(prefix, "bin", "ersilia"))
    for root in _CONDA_ROOTS:
        base = os.path.expanduser(root)
        found.append(os.path.join(base, "bin", "ersilia"))
        envs = glob.glob(os.path.join(base, "envs", "*", "bin", "ersilia"))
        # An env called `ersilia` is the one meant for it; other envs follow.
        found.extend(sorted(envs, key=lambda p: (Path(p).parts[-3] != "ersilia", p)))
    out, seen = [], set()
    for path in found:
        if not path or not (os.path.isfile(path) and os.access(path, os.X_OK)):
            continue
        key = os.path.realpath(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def defaults() -> HostConfig:
    """Return the settings a fresh host starts from, under ``~/model-launcher``."""
    root = Path(os.path.expanduser("~")) / "model-launcher"
    candidates = ersilia_candidates()
    return HostConfig(
        data_dir=str(root / "data"),
        ersilia_bin=candidates[0] if candidates else "",
        log_dir=str(root / "logs"),
        queue_file=str(root / "models.queue"),
    )


def load(path: Path | None = None) -> HostConfig:
    """Read an existing conf, falling back to :func:`defaults` for anything unset.

    Parameters
    ----------
    path : Path, optional
        The conf to read; :func:`conf_path` by default.

    Returns
    -------
    HostConfig
        The settings found, over the defaults.
    """
    cfg = defaults()
    try:
        text = (path or conf_path()).read_text()
    except (OSError, UnicodeError):
        return cfg
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith(_SETUP_COMMENT):
            key, _, value = line[len(_SETUP_COMMENT) :].partition("=")
            if key in SETUP_KEYS:
                values[key] = value
            continue
        match = _CONF_LINE.match(line)
        if match and match.group(1) in MANAGED and match.group(2):
            values[match.group(1).lower()] = match.group(2)
    fields = {
        "data_dir": values.get("data_dir"),
        "ersilia_bin": values.get("ersilia_bin"),
        "log_dir": values.get("log_dir"),
        "queue_file": values.get("queue_file"),
        "default_library": values.get("default_library"),
    }
    return replace(cfg, **{k: v for k, v in fields.items() if v is not None})


def render(cfg: HostConfig, existing: str = "") -> str:
    """Return the conf text for ``cfg``, keeping the other lines of ``existing``.

    Parameters
    ----------
    cfg : HostConfig
        The settings to write.
    existing : str
        The conf's current text, if any.

    Returns
    -------
    str
        The new conf: setup's lines first, then every line of ``existing`` that
        setup does not manage, unchanged.
    """
    values = {
        "DISPATCH": "serve",
        "DATA_DIR": cfg.data_dir,
        "ERSILIA_BIN": cfg.ersilia_bin,
        "LOG_DIR": cfg.log_dir,
    }
    lines = [HEADER]
    lines += [f'{name}="${{{name}:-{values[name]}}}"' for name in MANAGED]
    lines += [f"{_SETUP_COMMENT}queue_file={cfg.queue_file}"]
    lines += [f"{_SETUP_COMMENT}default_library={cfg.default_library}"]
    kept = []
    for line in existing.splitlines():
        match = _CONF_LINE.match(line)
        if line == HEADER or line.startswith(_SETUP_COMMENT):
            continue
        if match and match.group(1) in MANAGED:
            continue
        kept.append(line)
    while kept and not kept[0].strip():
        kept.pop(0)
    if kept:
        lines += ["", *kept]
    return "\n".join(lines) + "\n"


def save(cfg: HostConfig, path: Path | None = None) -> list[str]:
    """Write the conf and create the folders and queue file it names.

    Parameters
    ----------
    cfg : HostConfig
        The settings to write.
    path : Path, optional
        Where to write the conf; :func:`conf_path` by default.

    Returns
    -------
    list of str
        Every file written and folder created, for the user to see.
    """
    path = path or conf_path()
    done = []
    for folder in (
        Path(cfg.data_dir) / "input",
        Path(cfg.data_dir) / "output",
        Path(cfg.log_dir),
        Path(cfg.queue_file).parent,
    ):
        if not folder.is_dir():
            folder.mkdir(parents=True)
            done.append(f"created {folder}/")
    queue = Path(cfg.queue_file)
    if not queue.exists():
        queue.write_text(QUEUE_HEADER)
        done.append(f"created {queue} (no jobs yet)")
    try:
        existing = path.read_text()
    except OSError:
        existing = ""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render(cfg, existing))
    tmp.replace(path)
    done.append(f"wrote {path}")
    return done


def libraries(cfg: HostConfig) -> list[str]:
    """Return the library folders under ``data_dir/input``, sorted."""
    base = Path(cfg.data_dir) / "input"
    try:
        return sorted(p.name for p in base.iterdir() if p.is_dir())
    except OSError:
        return []


def _creatable(path: Path) -> bool:
    """Whether ``path`` exists as a writable folder, or its nearest parent is one."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate.is_dir() and os.access(candidate, os.W_OK)
    return False


def _path_problem(label: str, value: str) -> Check | None:
    if not value:
        return Check("bad", f"{label}: required")
    if not os.path.isabs(value):
        return Check("bad", f"{label}: must be an absolute path")
    if _UNSAFE.search(value):
        return Check("bad", f"{label}: no spaces, quotes, $, %, }} or backslashes")
    return None


def _bash4_here() -> str:
    """The first bash >= 4 the scripts would re-run under on macOS, or ``""``."""
    for path in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash"):
        if not os.access(path, os.X_OK):
            continue
        try:
            out = subprocess.run(
                [path, "-c", 'echo "${BASH_VERSINFO[0]}"'],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if out.isdigit() and int(out) >= 4:
            return path
    return ""


def quick_checks(cfg: HostConfig) -> list[Check]:
    """Check the settings themselves: paths, the ersilia CLI, this OS's tools.

    Fast enough to rerun after every keystroke.

    Parameters
    ----------
    cfg : HostConfig
        The settings to check.

    Returns
    -------
    list of Check
        One line per finding, in the order the form shows its fields.
    """
    out: list[Check] = []

    problem = _path_problem("data folder", cfg.data_dir)
    if problem:
        out.append(problem)
    elif (Path(cfg.data_dir) / "input").is_dir():
        found = libraries(cfg)
        out.append(
            Check(
                "ok",
                f"data folder: {len(found)} librar{'y' if len(found) == 1 else 'ies'} in input/",
            )
        )
    elif _creatable(Path(cfg.data_dir)):
        out.append(
            Check("warn", "data folder: will be created, with empty input/ and output/")
        )
    else:
        out.append(Check("bad", "data folder: cannot be created here"))

    problem = _path_problem("ersilia", cfg.ersilia_bin)
    if problem:
        out.append(
            Check(
                "bad", "ersilia: not set; install ersilia or give the path to its CLI"
            )
            if not cfg.ersilia_bin
            else problem
        )
    elif os.path.isfile(cfg.ersilia_bin) and os.access(cfg.ersilia_bin, os.X_OK):
        out.append(Check("ok", "ersilia: found"))
    else:
        out.append(Check("bad", "ersilia: no executable at this path"))

    problem = _path_problem("log folder", cfg.log_dir)
    if problem:
        out.append(problem)
    elif _creatable(Path(cfg.log_dir)):
        exists = Path(cfg.log_dir).is_dir()
        out.append(
            Check(
                "ok",
                "log folder: writable" if exists else "log folder: will be created",
            )
        )
    else:
        out.append(Check("bad", "log folder: not writable"))

    problem = _path_problem("queue file", cfg.queue_file)
    if problem:
        out.append(problem)
    elif Path(cfg.queue_file).is_file():
        out.append(Check("ok", "queue file: exists, kept as it is"))
    elif _creatable(Path(cfg.queue_file).parent):
        out.append(Check("ok", "queue file: will be created, with no jobs"))
    else:
        out.append(Check("bad", "queue file: its folder is not writable"))

    lib = cfg.default_library
    if not lib:
        out.append(Check("ok", "default library: none (each job names its own)"))
    elif _UNSAFE.search(lib) or "/" in lib:
        out.append(
            Check("bad", "default library: a folder name under input/, no spaces or /")
        )
    elif lib in libraries(cfg):
        out.append(Check("ok", f"default library: input/{lib}/ found"))
    else:
        out.append(Check("warn", f"default library: no input/{lib}/ yet"))

    if sys.platform == "darwin":
        if _bash4_here():
            out.append(Check("ok", "Homebrew bash found"))
        else:
            out.append(Check("bad", "needs bash 4 or newer: brew install bash"))
        if shutil.which("flock") or os.access("/opt/homebrew/bin/flock", os.X_OK):
            out.append(Check("ok", "flock found"))
        else:
            out.append(Check("bad", "needs flock: brew install flock"))
        out.append(
            Check("note", "for other computers to reach this Mac, turn on Remote Login")
        )
    elif not shutil.which("systemctl"):
        out.append(
            Check("warn", "no systemd: the driver cannot be installed as a service")
        )

    shadow = remote_dir() / "scheduler.conf"
    if shadow.is_file():
        out.append(
            Check("bad", f"{shadow} exists and would be used instead: remove it")
        )
    return out


def slow_checks() -> list[Check]:
    """Check what takes a moment: Docker, and a driver already running here.

    Returns
    -------
    list of Check
        One line each.
    """
    from .discover import probe_host

    out: list[Check] = []
    if not shutil.which("docker"):
        out.append(
            Check("warn", "Docker not found: ersilia needs it to serve most models")
        )
    else:
        try:
            ok = (
                subprocess.run(
                    ["docker", "info"], capture_output=True, timeout=5, check=False
                ).returncode
                == 0
            )
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            out.append(Check("ok", "Docker is running"))
        elif sys.platform == "darwin":
            out.append(Check("warn", "Docker is not answering: start Docker Desktop"))
        else:
            out.append(
                Check(
                    "warn",
                    "Docker is not answering: is it running, and are you in the docker group?",
                )
            )
    status = probe_host(None)
    if status.state == "running":
        pids = ", ".join(str(d.pid) for d in status.drivers)
        out.append(
            Check(
                "note",
                f"a driver is already running here (pid {pids}); it keeps its old settings until restarted",
            )
        )
    return out


def service_command(
    cfg: HostConfig, *, print_only: bool = False
) -> tuple[list[str], dict[str, str]]:
    """Return the command that installs (or, with ``print_only``, shows) the service.

    Parameters
    ----------
    cfg : HostConfig
        The saved settings.
    print_only : bool
        Render the unit or LaunchAgent without installing it.

    Returns
    -------
    tuple of (list of str, dict)
        The argv, and the environment variables to add to the current ones.
    """
    argv = ["bash", str(remote_dir() / "install-scheduler-service.sh")]
    if print_only:
        argv.append("--print")
    argv.append(cfg.queue_file)
    if cfg.default_library:
        argv.append(cfg.default_library)
    return argv, {"SCHEDULER_CONF": str(conf_path())}
