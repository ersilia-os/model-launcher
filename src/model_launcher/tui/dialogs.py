"""Modal screens: add a model, and confirm a destructive verb."""

from __future__ import annotations

from typing import List, Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

MODES = ["ersilia", "singularity"]


class AddScreen(ModalScreen[Optional[dict]]):
    """Collect a new queue entry. Returns a dict, or None if cancelled."""

    BINDINGS = [("escape", "dismiss_none", "Cancel")]

    def __init__(
        self,
        libraries: List[str],
        default_library: str = "",
        max_cpus: int = 32,
    ) -> None:
        super().__init__()
        self.libraries = libraries
        self.default_library = default_library
        self.max_cpus = max_cpus

    def compose(self) -> ComposeResult:
        library_options = [("<driver default>", "")] + [
            (name, name) for name in self.libraries
        ]
        initial = self.default_library if self.default_library in self.libraries else ""
        with Vertical(id="add-box"):
            yield Static("Add a model to the queue", id="add-title")

            yield Label(
                "Model id  (its SIF must be at /shared/sif-files/<id>.sif)",
                classes="field-label",
            )
            yield Input(placeholder="eos4k4f_v1", id="in-model")

            yield Label("Run mode", classes="field-label")
            yield Select(
                [(m, m) for m in MODES],
                value="ersilia",
                allow_blank=False,
                id="in-mode",
            )

            yield Label("Library", classes="field-label")
            yield Select(
                library_options, value=initial, allow_blank=False, id="in-library"
            )

            yield Label(
                "Wave size  (blank = driver default; 1..1000)", classes="field-label"
            )
            yield Input(placeholder="1000", id="in-wave")

            yield Label(
                "SLURM partition  (blank = driver default)", classes="field-label"
            )
            yield Input(placeholder="cpu-queue", id="in-queue")

            # cpu-queue is CR_CPU — SLURM does no memory accounting there, so
            # cpus-per-task is the only lever on how densely tasks pack a 32-vCPU
            # node, and therefore the only defence against a heavy model OOMing.
            # Blank keeps the worker's tuned default, which differs per mode.
            #
            # Keep this label on ONE line: .field-label is height 1, so a label that
            # wraps at the 60-column dialog width loses its second row silently. The
            # "why" lives in the placeholder and in example.queue instead.
            yield Label(
                f"CPUs per task  (blank = worker default; 1..{self.max_cpus})",
                classes="field-label",
            )
            yield Input(placeholder="raise for heavy models, e.g. 16", id="in-cpus")

            yield Checkbox("Run it next (insert at the top of the queue)", id="in-top")

            with Horizontal(classes="btn-row"):
                yield Button("Cancel", id="add-cancel")
                yield Button("Add", variant="primary", id="add-ok")

    def on_mount(self) -> None:
        self.query_one("#in-model", Input).focus()

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        model = self.query_one("#in-model", Input).value.strip()
        if not model:
            self.notify("A model id is required.", severity="error")
            self.query_one("#in-model", Input).focus()
            return
        wave = self.query_one("#in-wave", Input).value.strip()
        if wave and (not wave.isdigit() or not 1 <= int(wave) <= 1000):
            self.notify("Wave size must be a number in 1..1000.", severity="error")
            self.query_one("#in-wave", Input).focus()
            return
        cpus = self.query_one("#in-cpus", Input).value.strip()
        if cpus and (not cpus.isdigit() or not 1 <= int(cpus) <= self.max_cpus):
            self.notify(
                f"CPUs per task must be a number in 1..{self.max_cpus}, "
                "or blank for the worker default.",
                severity="error",
            )
            self.query_one("#in-cpus", Input).focus()
            return
        self.dismiss(
            {
                "model": model,
                "mode": str(self.query_one("#in-mode", Select).value),
                "library": str(self.query_one("#in-library", Select).value or ""),
                "wave": wave,
                "queue": self.query_one("#in-queue", Input).value.strip(),
                "cpus": cpus,
                "top": self.query_one("#in-top", Checkbox).value,
            }
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "add-ok":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()


class ConfirmScreen(ModalScreen[bool]):
    """Confirm a destructive verb. Spells out exactly what will happen."""

    BINDINGS = [("escape", "dismiss_false", "Cancel")]

    def __init__(self, title: str, detail: str, ok_label: str = "Confirm") -> None:
        super().__init__()
        self.title_text = title
        self.detail = detail
        self.ok_label = ok_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self.title_text, id="confirm-title")
            yield Static(self.detail, id="confirm-detail")
            with Horizontal(classes="btn-row"):
                yield Button("Cancel", id="cf-no")
                yield Button(self.ok_label, variant="error", id="cf-yes")

    def on_mount(self) -> None:
        self.query_one("#cf-no", Button).focus()

    def action_dismiss_false(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "cf-yes")
