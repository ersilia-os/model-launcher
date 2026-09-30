"""The setup screen: configure the computer you are on as a host that runs models.

Five settings on the left, the checks on the right, and a choice of whether to
start the driver as a service. Nothing is written here: the screen returns the
settings and the choice, and ``model-launcher setup`` saves them in the terminal
afterwards, where the installer can ask for a password.

:class:`SetupForm` holds the editing rules and knows nothing of Textual, so the
behaviour can be tested without an app running.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.events import Click, Key, Paste
from textual.screen import Screen

from ..core import hostconf
from ..core.hostconf import Check, HostConfig
from . import draw
from .theme import DARK_THEME, LIGHT_THEME, THEMES, Tokens, tokens
from .widgets import Band, HintClicked, KeyFooter, Painted

#: (attribute of HostConfig, label, help shown while focused).
FIELDS = (
    (
        "data_dir",
        "Data folder",
        "input/<library>/ holds the chunks; results go to output/",
    ),
    (
        "ersilia_bin",
        "ersilia CLI",
        "the ersilia that runs each job; ↓ lists the ones found",
    ),
    ("log_dir", "Log folder", "the scheduler's queue lock, status and job logs"),
    ("queue_file", "Queue file", "the jobs to run, one per line; created empty"),
    (
        "default_library",
        "Default library",
        "for jobs that name none; optional, ↓ lists input/",
    ),
)

#: The start choices, in the order drawn.
STARTS = ("service", "none")


class SetupForm:
    """The settings being edited, the focus, and the completion popup.

    Parameters
    ----------
    cfg : HostConfig
        The settings to start from.
    service : str
        What this OS runs the driver under (``systemd`` or ``launchd``), for the
        start choice's label.
    """

    def __init__(self, cfg: HostConfig, service: str) -> None:
        self.values = {name: getattr(cfg, name) for name, _, _ in FIELDS}
        self.service = service
        self.focus = 0
        self.start = 0
        self.highlighted = -1

    # -- reading ------------------------------------------------------------------
    @property
    def config(self) -> HostConfig:
        """The settings as typed."""
        return HostConfig(**{k: v.strip() for k, v in self.values.items()})

    @property
    def on_start(self) -> bool:
        """Whether the start choice, below the fields, has the focus."""
        return self.focus == len(FIELDS)

    def field(self) -> str:
        """The name of the focused field; ``""`` on the start choice."""
        return "" if self.on_start else FIELDS[self.focus][0]

    def completions(self) -> list[str]:
        """What the focused field can be completed to, other than its own value."""
        name = self.field()
        value = self.values.get(name, "")
        if name == "ersilia_bin":
            options = hostconf.ersilia_candidates()
        elif name == "default_library":
            options = hostconf.libraries(self.config)
        else:
            return []
        low = value.lower()
        return [
            o
            for o in options
            if o != value and (not low or low in o.lower() or name == "ersilia_bin")
        ]

    def start_labels(self) -> list[str]:
        """The start choices as drawn."""
        return [f"install the {self.service} service", "don't start it now"]

    @property
    def start_choice(self) -> str:
        """``service`` or ``none``."""
        return STARTS[self.start]

    # -- editing ------------------------------------------------------------------
    def type(self, text: str) -> None:
        """Add printable characters to the focused field."""
        name = self.field()
        if name:
            self.values[name] += "".join(
                c for c in text if c.isprintable() and c not in "\r\n"
            )
            self.highlighted = -1

    def backspace(self) -> None:
        """Delete the last character of the focused field."""
        name = self.field()
        if name:
            self.values[name] = self.values[name][:-1]
            self.highlighted = -1

    def clear(self) -> None:
        """Empty the focused field."""
        name = self.field()
        if name:
            self.values[name] = ""
            self.highlighted = -1

    def move_focus(self, step: int) -> None:
        """Focus the next (``+1``) or previous (``-1``) setting, wrapping round."""
        self.focus = (self.focus + step) % (len(FIELDS) + 1)
        self.highlighted = -1

    def move(self, step: int) -> None:
        """↑/↓: through the popup while it is open, else between settings."""
        options = self.completions()
        if options and (self.highlighted >= 0 or step > 0):
            self.highlighted = self.highlighted + step
            if self.highlighted >= len(options):
                self.highlighted = len(options) - 1
            return  # up from the first item closes the highlight (-1)
        self.move_focus(step)

    def accept(self, index: int | None = None) -> bool:
        """Take completion ``index`` (default: the highlighted one). True if taken."""
        options = self.completions()
        index = self.highlighted if index is None else index
        if not 0 <= index < len(options):
            return False
        self.values[self.field()] = options[index]
        self.highlighted = -1
        return True

    def tab(self, step: int = 1) -> None:
        """Tab: take the highlighted completion if any, then move on."""
        if step > 0:
            self.accept()
        self.move_focus(step)

    def choose_start(self, step: int) -> None:
        """←/→: switch between installing the service and not starting."""
        self.start = (self.start + step) % len(STARTS)

    # -- drawing ------------------------------------------------------------------
    def views(self) -> tuple[draw.SetupField, ...]:
        """What each field shows."""
        ghosts = {
            "ersilia_bin": "not found: type the path to the ersilia CLI",
            "default_library": "none",
        }
        return tuple(
            draw.SetupField(
                label, self.values[name], ghosts.get(name, "required"), help_
            )
            for name, label, help_ in FIELDS
        )


class SetupView(Painted):
    """The form and the checks. Data: the arguments of ``draw.setup_view`` after
    the size and tokens."""

    DEFAULT_CSS = "SetupView { height: 1fr; }"
    ALLOW_SELECT = False

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.targets: list[tuple[int, int, int, tuple[str, int]]] = []

    def paint(self, width: int, t: Tokens) -> list[Text]:
        lines, self.targets = draw.setup_view(
            width, self.size.height or 32, t, *self.data
        )
        return lines

    def target_at(self, x: int, y: int) -> tuple[str, int] | None:
        """What a click at this cell means: ("field"|"start"|"popup", index)."""
        return next(
            (tg for row, a, b, tg in self.targets if row == y and a <= x < b), None
        )


class SetupScreen(Screen[tuple[HostConfig, str] | None]):
    """Configure this computer as a serve host.

    Returns ``(settings, start)`` with start ``service`` or ``none``, or None
    when left with ``esc``. Saving is refused while any check is ``bad``.

    Parameters
    ----------
    cfg : HostConfig
        The settings to start from (an existing conf, or the defaults).
    """

    ALLOW_SELECT = False

    #: Footer hint → what it does.
    HINTS: ClassVar[dict[str, tuple[str, int]]] = {
        "tab": ("tab", 1),
        "shift+tab": ("tab", -1),
        "←": ("start", -1),
        "→": ("start", 1),
        "^s": ("save", 0),
        "esc": ("cancel", 0),
        "^t": ("theme", 0),
    }

    def __init__(self, cfg: HostConfig) -> None:
        super().__init__()
        self.form = SetupForm(cfg, hostconf.service_kind())
        self.slow: list[Check] = []
        self.checking = True
        self.problem = ""

    def compose(self) -> ComposeResult:
        yield Band()
        yield SetupView()
        yield KeyFooter()

    def on_mount(self) -> None:
        self.query_one(Band).show(f"set up this computer · {hostconf.platform_name()}")
        self.query_one(KeyFooter).show(tuple(tuple(g) for g in draw.SETUP_KEYS))
        self._redraw()
        self.run_worker(self._slow_checks, thread=True, group="checks")

    def _slow_checks(self) -> None:
        found = hostconf.slow_checks()
        self.app.call_from_thread(self._show_slow, found)

    def _show_slow(self, found: list[Check]) -> None:
        self.slow, self.checking = found, False
        self._redraw()

    def checks(self) -> list[Check]:
        """Every check, the quick ones recomputed from what is typed now."""
        return hostconf.quick_checks(self.form.config) + self.slow

    def _redraw(self) -> None:
        form = self.form
        self.query_one(SetupView).show(
            form.views(),
            form.focus,
            tuple(form.start_labels()),
            form.start,
            tuple(form.completions()),
            form.highlighted,
            tuple((c.level, c.text) for c in self.checks()),
            self.checking,
            self.problem,
            f"saves {hostconf.conf_path()}",
        )

    # -- doing ----------------------------------------------------------------------
    def _save(self) -> None:
        bad = [c for c in self.checks() if c.level == "bad"]
        if bad:
            self.problem = (
                f"{len(bad)} check{'s' if len(bad) > 1 else ''} marked ✕ to fix first"
            )
            self._redraw()
            return
        self.dismiss((self.form.config, self.form.start_choice))

    def _do(self, action: str, step: int) -> None:
        form = self.form
        if action == "tab":
            form.tab(step)
        elif action == "start":
            form.choose_start(step)
        elif action == "save":
            self._save()
            return
        elif action == "cancel":
            self.dismiss(None)
            return
        elif action == "theme":
            self.app.action_toggle_dark_theme()  # type: ignore[attr-defined]
        self.problem = ""
        self._redraw()

    def on_key(self, event: Key) -> None:
        event.stop()
        event.prevent_default()
        form, key = self.form, event.key
        if key == "escape":
            if form.highlighted >= 0:
                form.highlighted = -1
                self._redraw()
            else:
                self.dismiss(None)
            return
        if key == "ctrl+s":
            self._save()
            return
        if key == "tab":
            form.tab(1)
        elif key == "shift+tab":
            form.tab(-1)
        elif key == "enter":
            if not form.accept():
                form.move_focus(1)
        elif key in ("up", "down"):
            form.move(-1 if key == "up" else 1)
        elif key in ("left", "right") and form.on_start:
            form.choose_start(-1 if key == "left" else 1)
        elif key == "backspace":
            form.backspace()
        elif key == "ctrl+u":
            form.clear()
        elif key == "ctrl+t":
            self.app.action_toggle_dark_theme()  # type: ignore[attr-defined]
        elif event.character and event.character.isprintable():
            form.type(event.character)
        else:
            return
        self.problem = ""
        self._redraw()

    def on_paste(self, event: Paste) -> None:
        event.stop()
        self.form.type(event.text)
        self.problem = ""
        self._redraw()

    def on_click(self, event: Click) -> None:
        view = self.query_one(SetupView)
        offset = event.get_content_offset(view)
        if offset is None:
            return
        target = view.target_at(offset.x, offset.y)
        if target is None:
            return
        kind, index = target
        if kind == "popup":
            self.form.accept(index)
        elif kind == "start":
            self.form.focus, self.form.start = len(FIELDS), index
        else:
            self.form.focus, self.form.highlighted = index, -1
        self.problem = ""
        self._redraw()

    @on(HintClicked)
    def _on_hint(self, message: HintClicked) -> None:
        action = self.HINTS.get(message.key)
        if action:
            self._do(*action)


class SetupApp(App[tuple[HostConfig, str] | None]):
    """``model-launcher setup``: the setup screen on its own.

    Parameters
    ----------
    cfg : HostConfig, optional
        The settings to start from; the existing conf (or the defaults) if None.
    """

    CSS = "Screen { background: $background; }"

    def __init__(self, cfg: HostConfig | None = None) -> None:
        super().__init__()
        self.cfg = cfg or hostconf.load()
        self._dark = True

    @property
    def tokens(self) -> Tokens:
        """Cell colours for the current theme, read by every painted widget."""
        return tokens(self._dark)

    @property
    def base_tokens(self) -> Tokens:
        """The same, undimmed (there are no overlays here)."""
        return tokens(self._dark)

    def on_mount(self) -> None:
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = DARK_THEME
        self.push_screen(SetupScreen(self.cfg), callback=self.exit)

    def watch_theme(self, theme_name: str) -> None:
        theme = self.get_theme(theme_name)
        self._dark = bool(theme.dark) if theme else True
        if self.is_mounted:
            for widget in self.screen.query("Painted"):
                widget.refresh()

    def action_toggle_dark_theme(self) -> None:
        """Switch between the two Ersilia themes."""
        self.theme = LIGHT_THEME if self.theme == DARK_THEME else DARK_THEME
