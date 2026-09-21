"""Launch Ersilia models over large chemical libraries.

The package is a thin client. All scheduling logic lives in the bash layer
shipped under :mod:`model_launcher.remote`, which runs on the target machine;
Python only spawns ``sched-ctl.sh`` and parses the snapshot it prints.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("model-launcher")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
