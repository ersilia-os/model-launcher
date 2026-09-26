"""Module-level logger singleton, following the Ersilia logging pattern.

Import the ``logger`` object rather than calling :func:`logging.getLogger`, so
every module in the package shares one handler and one level.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from rich.logging import RichHandler

SUCCESS = 25
"""Between INFO (20) and WARNING (30): visible by default, but not a warning."""

logging.addLevelName(SUCCESS, "SUCCESS")


class _Logger(logging.Logger):
    """A :class:`logging.Logger` with an extra ``success`` level."""

    def success(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log a completed action at the ``SUCCESS`` level.

        Parameters
        ----------
        msg : str
            Message, optionally containing ``%``-style placeholders.
        *args
            Values interpolated into ``msg``.
        """
        if self.isEnabledFor(SUCCESS):
            self._log(SUCCESS, msg, args, **kwargs)


def _build() -> _Logger:
    logging.setLoggerClass(_Logger)
    try:
        instance = logging.getLogger("model_launcher")
    finally:
        logging.setLoggerClass(logging.Logger)

    if not instance.handlers:
        handler = RichHandler(rich_tracebacks=True, show_path=False, markup=False)
        handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))
        instance.addHandler(handler)
        # The TUI owns the screen; a stray log line would corrupt it. Propagating
        # to the root logger is what would let that happen.
        instance.propagate = False

    instance.setLevel(os.environ.get("MODEL_LAUNCHER_LOG_LEVEL", "INFO").upper())
    return instance  # type: ignore[return-value]


logger = _build()

__all__ = ["SUCCESS", "logger"]
