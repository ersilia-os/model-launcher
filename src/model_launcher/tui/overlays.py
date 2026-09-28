"""Overlays drawn over the dashboard: the confirm modal, the row menu, the add panel.

Each is a ``ModalScreen`` with a transparent background, so the dashboard stays
visible beneath it. The dimming is not Textual's: an overlay declares ``DIM``,
the app blends every cell of the screen below toward ``bg`` by that much (see
``Tokens.dim``), and the overlay's own widgets are drawn undimmed. That is
exactly what the design generator's ``G.dim()`` does.
"""

from __future__ import annotations

from typing import ClassVar, TypeVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.events import Click, Key, MouseMove, Paste
from textual.screen import ModalScreen

from . import draw
from .theme import Tokens
from .widgets import HintClicked, Painted

ResultT = TypeVar("ResultT")


class Overlay(ModalScreen[ResultT]):
    """A modal screen over the dashboard, which it dims by ``DIM``."""

    DEFAULT_CSS = "Overlay { background: transparent; }"
    DIM: ClassVar[float] = 0.0

    def on_mount(self) -> None:
        self.app.refresh_base()  # type: ignore[attr-defined]

    def on_unmount(self) -> None:
        self.app.refresh_base()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------
class ConfirmBox(Painted):
    """The confirm modal's box. Data: the arguments of ``draw.confirm_box``."""

    DIMMED = False
    ALLOW_SELECT = False

    def paint(self, width: int, t: Tokens) -> list[Text]:
        title, detail, keep, ok, subject, kept, tone, glyph = self.data
        lines, spans = draw.confirm_box(
            t, title, detail, keep, ok, subject, kept, getattr(t, tone), glyph
        )
        row = len(lines) - 2
        self.hints = [(row, start, end, key) for start, end, key in spans]
        return lines


class ConfirmScreen(Overlay[bool]):
    """Ask before a verb that changes a job. Returns True to go ahead.

    ``y`` confirms; any other key, or a click outside the box, keeps things as
    they are — so a stray keypress can never cancel a running job.

    Parameters
    ----------
    title : str
        The question, e.g. ``Cancel the RUNNING model?``.
    detail : str
        What will happen, in plain words.
    keep, ok : str
        Labels of the ``esc`` and ``y`` buttons.
    subject : tuple of str, optional
        (model, library) the verb applies to.
    kept : tuple of int, optional
        (done, total) chunks, for a running job whose output is kept.
    tone : str
        Token for the border and title: ``err`` for destructive verbs,
        ``warn`` for the rest.
    glyph : str
        Drawn before the title: ``✕`` unless the verb has its own.
    """

    DEFAULT_CSS = "ConfirmScreen { align: center middle; }"
    DIM = 0.65

    def __init__(
        self,
        title: str,
        detail: str,
        keep: str = "keep it",
        ok: str = "confirm",
        subject: tuple[str, str] | None = None,
        kept: tuple[int, int] | None = None,
        tone: str = "err",
        glyph: str = "✕",
    ) -> None:
        super().__init__()
        self.args = (title, detail, keep, ok, subject, kept, tone, glyph)

    def compose(self) -> ComposeResult:
        yield ConfirmBox()

    def on_mount(self) -> None:
        super().on_mount()
        box = self.query_one(ConfirmBox)
        box.styles.width = draw.CONFIRM_WIDTH
        box.styles.height = len(draw.wrap(self.args[1], draw.CONFIRM_WIDTH - 8)) + 11
        box.show(*self.args)

    def on_key(self, event: Key) -> None:
        event.stop()
        event.prevent_default()
        self.dismiss(event.key == "y")

    @on(HintClicked)
    def _on_button(self, event: HintClicked) -> None:
        event.stop()
        self.dismiss(event.key == "y")

    def on_click(self, event: Click) -> None:
        if event.widget is self:  # outside the box
            self.dismiss(False)


# ---------------------------------------------------------------------------
# row menu
# ---------------------------------------------------------------------------
class MenuBox(Painted):
    """The row menu's box. Data: (model, highlighted index)."""

    DIMMED = False
    ALLOW_SELECT = False

    def paint(self, width: int, t: Tokens) -> list[Text]:
        model, highlighted = self.data
        return draw.context_menu(t, model, highlighted)

    def item_at(self, y: int) -> int | None:
        """Index into ``draw.MENU_ITEMS`` of the item on this row, if any."""
        i = y - 1
        if 0 <= i < len(draw.MENU_ITEMS) and draw.MENU_ITEMS[i] is not None:
            return i
        return None


class MenuScreen(Overlay[str | None]):
    """The verb menu for one row. Returns the chosen verb, or None.

    Opened by right-click at the pointer, or from the keyboard beside the
    selected row. ``↑``/``↓`` and ``⏎``, an item's own key, or the mouse pick
    an item; ``esc`` or a click outside closes it.

    Parameters
    ----------
    model : str
        The job's model id, the menu's title.
    x, y : int
        Screen cell for the top-left corner, moved in so the menu fits.
    """

    KEYS: ClassVar[dict[str, int]] = {
        item[1]: i for i, item in enumerate(draw.MENU_ITEMS) if item is not None
    }

    def __init__(self, model: str, x: int, y: int) -> None:
        super().__init__()
        self.model = model
        self.at = (x, y)
        self.highlighted = 0

    def compose(self) -> ComposeResult:
        yield MenuBox()

    def on_mount(self) -> None:
        super().on_mount()
        box = self.query_one(MenuBox)
        box.styles.width = draw.MENU_WIDTH
        box.styles.height = draw.MENU_HEIGHT
        x = max(0, min(self.at[0], self.size.width - draw.MENU_WIDTH))
        y = max(0, min(self.at[1], self.size.height - draw.MENU_HEIGHT))
        box.styles.offset = (x, y)
        box.show(self.model, self.highlighted)

    def _highlight(self, index: int) -> None:
        self.highlighted = index
        self.query_one(MenuBox).show(self.model, index)

    def _move(self, step: int) -> None:
        n = len(draw.MENU_ITEMS)
        i = self.highlighted
        while True:
            i = (i + step) % n
            if draw.MENU_ITEMS[i] is not None:
                break
        self._highlight(i)

    def _choose(self, index: int) -> None:
        item = draw.MENU_ITEMS[index]
        self.dismiss(item[2] if item else None)

    def on_key(self, event: Key) -> None:
        event.stop()
        event.prevent_default()
        if event.key in ("up", "down"):
            self._move(-1 if event.key == "up" else 1)
        elif event.key == "enter":
            self._choose(self.highlighted)
        elif event.character in self.KEYS:
            self._choose(self.KEYS[event.character])
        elif event.key == "escape":
            self.dismiss(None)

    def on_mouse_move(self, event: MouseMove) -> None:
        box = self.query_one(MenuBox)
        offset = event.get_content_offset(box)
        if offset is not None:
            index = box.item_at(offset.y)
            if index is not None and index != self.highlighted:
                self._highlight(index)

    def on_click(self, event: Click) -> None:
        box = self.query_one(MenuBox)
        offset = event.get_content_offset(box)
        if offset is None:
            self.dismiss(None)  # outside the menu
            return
        index = box.item_at(offset.y)
        if index is not None:
            self._choose(index)


# ---------------------------------------------------------------------------
# add panel
# ---------------------------------------------------------------------------
MODES = ["ersilia", "singularity"]

#: The positional fields of a queue line, in order; flags may follow the mode
#: anywhere, as in the queue file itself.
FIELDS = ["model", "mode", "library", "wave", "queue"]


def _is_flag(part: str) -> bool:
    """``--top`` and ``cpus=N``, or a prefix of either being typed."""
    return part.startswith("cpus=") or (
        part.startswith("-") and "--top".startswith(part)
    )


class CommandLine:
    """The add panel's one-line command, as text plus what it means.

    ``model mode [library] [wave] [queue] [flags]``: tokens are split on single
    spaces and the last one is being typed. Editing happens at the end only.

    Parameters
    ----------
    libraries : list of str
        Library names offered for completion.
    defaults : dict
        Driver defaults shown as ghosts: ``library``, ``wave``, ``queue``.
    max_cpus : int
        Upper bound for ``cpus=N``.
    """

    def __init__(self, libraries: list[str], defaults: dict, max_cpus: int) -> None:
        self.text = ""
        self.libraries = list(libraries)
        self.defaults = defaults
        self.max_cpus = max_cpus
        self.highlighted = 0

    # -- reading ---------------------------------------------------------
    def parts(self) -> list[tuple[str, str]]:
        """Each token with its field (a ``FIELDS`` name, ``flag`` or ``extra``)."""
        out, n = [], 0
        for i, part in enumerate(self.text.split(" ")):
            if i >= 2 and part and _is_flag(part):
                out.append((part, "flag"))
            else:
                out.append((part, FIELDS[n] if n < len(FIELDS) else "extra"))
                n += 1
        return out

    @property
    def current(self) -> tuple[str, str]:
        """(token, field) under the cursor."""
        return self.parts()[-1]

    def completions(self) -> list[tuple[str, bool]]:
        """Choices for the token being typed, as (value, is_default)."""
        token, field = self.current
        if field == "mode":
            return [(m, False) for m in MODES if m.startswith(token)]
        if field == "library":
            default = self.defaults.get("library", "")
            names = sorted(self.libraries, key=lambda name: name != default)
            low = token.lower()
            return [(n, n == default) for n in names if low in n.lower()]
        return []

    def ghost(self) -> str:
        """What ``tab`` would add to the current token, drawn after the cursor."""
        token, field = self.current
        options = self.completions()
        if options:
            value = options[min(self.highlighted, len(options) - 1)][0]
            if value.lower().startswith(token.lower()):
                return value[len(token) :]
            return ""
        if not token:
            return {"flag": "--top"}.get(field, self.defaults.get(field, ""))
        return ""

    def later(self) -> list[tuple[str, str]]:
        """Ghost defaults of the fields after the current one: (text, field)."""
        _, field = self.current
        if field not in FIELDS:
            return []
        out = [
            (self.defaults[f], f)
            for f in FIELDS[FIELDS.index(field) + 1 :]
            if f in ("wave", "queue") and self.defaults.get(f)
        ]
        return [*out, ("--top", "flag")]

    # -- editing ---------------------------------------------------------
    def type(self, text: str) -> None:
        """Append typed or pasted text; runs of spaces collapse to one."""
        for ch in text:
            if ch.isspace():
                if self.text and not self.text.endswith(" "):
                    self.text += " "
            elif ch.isprintable():
                self.text += ch
        self.highlighted = 0

    def backspace(self) -> None:
        self.text = self.text[:-1]
        self.highlighted = 0

    def move(self, step: int) -> None:
        """Move the completion highlight."""
        n = len(self.completions())
        if n:
            self.highlighted = (self.highlighted + step) % n

    def accept(self, value: str | None = None) -> None:
        """``tab``: take a completion (or the ghost), then go to the next field."""
        token, _ = self.current
        options = self.completions()
        if value is None and options:
            # The completion's own spelling: `coc` becomes Coconut_715K.
            value = options[min(self.highlighted, len(options) - 1)][0]
        elif value is None:
            value = token + self.ghost()
        if not value:
            return
        head = self.text[: len(self.text) - len(token)]
        self.text = head + value + " "
        self.highlighted = 0

    # -- the result ------------------------------------------------------
    def error(self) -> str:
        """Why the line cannot be added, or ``""`` when it can."""
        fields = {f: "" for f in FIELDS}
        for part, field in self.parts():
            if field == "extra" and part:
                return f"too many fields: '{part}'"
            if field in fields:
                fields[field] = part
        cpus = [p[5:] for p, f in self.parts() if f == "flag" and p.startswith("cpus=")]
        if not fields["model"]:
            return "a model id is required"
        if fields["mode"] not in MODES:
            return "mode must be ersilia or singularity"
        wave = fields["wave"]
        if wave and not (wave.isdigit() and 1 <= int(wave) <= 1000):
            return "wave size must be 1..1000"
        if any(not (c.isdigit() and 1 <= int(c) <= self.max_cpus) for c in cpus):
            return f"cpus must be 1..{self.max_cpus}"
        if any(
            f == "flag" and p not in ("--top",) and not p.startswith("cpus=")
            for p, f in self.parts()
        ):
            return "flags are --top and cpus=N"
        return ""

    def result(self) -> dict:
        """The entry, in the dict ``SchedulerTUI.action_add`` turns into ctl args."""
        fields = {f: "" for f in FIELDS}
        cpus, top = "", False
        for part, field in self.parts():
            if field in fields:
                fields[field] = part
            elif part == "--top":
                top = True
            elif part.startswith("cpus="):
                cpus = part[5:]
        return {**fields, "cpus": cpus, "top": top}


class AddPanel(Painted):
    """The add panel. Data: the arguments of ``draw.add_panel`` after ``t``."""

    DIMMED = False
    ALLOW_SELECT = False
    DEFAULT_CSS = f"AddPanel {{ height: {draw.ADD_HEIGHT}; margin-bottom: 1; }}"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.popup_rows: list[tuple[int, int]] = []

    def paint(self, width: int, t: Tokens) -> list[Text]:
        lines, self.popup_rows = draw.add_panel(width, t, *self.data)
        return lines

    def item_at(self, y: int) -> int | None:
        """Index of the completion drawn on this row, if any."""
        return next((i for row, i in self.popup_rows if row == y), None)


class AddScreen(Overlay[dict | None]):
    """Add a queue entry from one typed line. Returns the entry, or None.

    ``model mode [library] [wave] [queue] [--top] [cpus=N]``, with completion
    for the mode and the library. ``tab`` accepts a completion or the ghost
    default and moves on; ``⏎`` adds; ``esc`` or a click outside cancels.

    Parameters
    ----------
    libraries : list of str
        Libraries the scheduler knows, for completion.
    defaults : dict
        Driver defaults: ``library``, ``wave``, ``queue``.
    max_cpus : int
        Upper bound for ``cpus=N``.
    where : str
        ``host · dispatch``, for the header.
    sif_dir : str
        Where the model's SIF is expected; empty to leave the line out.
    """

    DEFAULT_CSS = "AddScreen { align: left bottom; }"
    DIM = 0.5

    def __init__(
        self,
        libraries: list[str],
        defaults: dict,
        max_cpus: int,
        where: str,
        sif_dir: str = "",
    ) -> None:
        super().__init__()
        self.line = CommandLine(libraries, defaults, max_cpus)
        self.where = where
        self.sif_dir = sif_dir
        self.problem = ""

    def compose(self) -> ComposeResult:
        yield AddPanel()

    def on_mount(self) -> None:
        super().on_mount()
        self._show()

    def _show(self) -> None:
        line = self.line
        model = line.parts()[0][0] if line.text.strip() else ""
        sif = f"{self.sif_dir}/{model or '<model_id>'}.sif" if self.sif_dir else ""
        self.query_one(AddPanel).show(
            self.where,
            tuple(line.parts()),
            line.ghost(),
            tuple(line.later()),
            tuple(line.completions()),
            line.highlighted,
            sif,
            self.problem,
            line.max_cpus,
        )

    def _submit(self) -> None:
        self.problem = self.line.error()
        if self.problem:
            self._show()
            return
        self.dismiss(self.line.result())

    def on_key(self, event: Key) -> None:
        event.stop()
        event.prevent_default()
        line, key = self.line, event.key
        if key == "escape":
            self.dismiss(None)
            return
        if key == "enter":
            self._submit()
            return
        if key == "tab":
            line.accept()
        elif key == "backspace":
            line.backspace()
        elif key == "ctrl+u":
            line.text = ""
        elif key in ("up", "down"):
            line.move(-1 if key == "up" else 1)
        elif event.character and event.character.isprintable():
            line.type(event.character)
        else:
            return
        self.problem = ""
        self._show()

    def on_paste(self, event: Paste) -> None:
        event.stop()
        self.line.type(event.text)
        self.problem = ""
        self._show()

    def on_click(self, event: Click) -> None:
        panel = self.query_one(AddPanel)
        offset = event.get_content_offset(panel)
        if offset is None:
            self.dismiss(None)  # outside the panel
            return
        index = panel.item_at(offset.y)
        if index is not None:
            self.line.accept(self.line.completions()[index][0])
            self._show()
