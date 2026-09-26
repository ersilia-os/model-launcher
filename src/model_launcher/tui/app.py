"""The scheduler-tui Textual application.

Shape of the thing:

    * one background worker polls ``sched-ctl.sh dump`` and posts a Snapshot
    * every mutation shells out to ``sched-ctl.sh <verb>`` in another worker
    * the UI is a pure function of the latest Snapshot

Nothing blocks the UI thread, so a slow shared filesystem or a dropped VPN shows
up as a "stale" banner rather than a frozen terminal.
"""

from __future__ import annotations

import os
import socket
from typing import List, Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive

from .dialogs import AddScreen, ConfirmScreen
from .hosts import HostScreen
from ..core.hosts import LOCAL, save_last_host
from ..core.model import (
    Job,
    Snapshot,
    apply_cached_counts,
    harvest_counts,
    parse_dump,
)
from ..core.runner import Runner, RunnerError
from ..core.target import Resolution
from . import draw
from .theme import DARK_THEME, LIGHT_THEME, THEMES, Tokens, tokens
from .widgets import (
    Band,
    Banner,
    ContextLine,
    ContextMenu,
    KeyFooter,
    LogDrawer,
    QueueHeader,
    QueueView,
    RunningCard,
    StatusSummary,
)


def _shorten_path(path: str, keep: int = 44) -> str:
    """Elide the middle of a long path so the header stays on one line.

    Queue files live under deep shared-filesystem paths; what identifies them is
    the last couple of components, not the mount point.
    """
    if not path or len(path) <= keep:
        return path
    parts = path.split(os.sep)
    tail = os.sep.join(parts[-2:]) if len(parts) > 2 else parts[-1]
    return f"…{os.sep}{tail}"


class SchedulerTUI(App):
    """Watch and steer a running wave scheduler."""

    CSS_PATH = "app.tcss"
    TITLE = "Ersilia wave scheduler"

    # The footer is drawn by KeyFooter from draw.DASHBOARD_KEYS, not from these,
    # so `show` is irrelevant; every binding still appears in the command palette.
    BINDINGS = [
        Binding("a", "add", "add"),
        Binding("x", "remove", "remove"),
        Binding("t", "top", "run next"),
        Binding("K", "move_up", "move up"),
        Binding("J", "move_down", "move down"),
        Binding("h", "hold", "hold / unhold"),
        Binding("r", "retry", "retry"),
        Binding("c", "cancel", "cancel"),
        Binding("p", "pause", "pause / resume"),
        Binding("l", "toggle_log", "log"),
        Binding("ctrl+o", "switch_host", "hosts"),
        Binding("H", "switch_host", "hosts", show=False),
        Binding("s", "stop_after", "stop after current"),
        Binding("f", "toggle_follow", "follow log"),
        Binding("plus,equals_sign", "log_size(2)", "taller log", show=False),
        Binding("minus", "log_size(-2)", "shorter log", show=False),
        Binding("escape", "close_log", "close log", show=False),
        Binding("R", "recount", "recount from S3"),
        Binding("D", "toggle_dark_theme", "light/dark theme"),
        Binding("q", "quit", "quit"),
    ]

    show_log: reactive[bool] = reactive(False)
    follow_log: reactive[bool] = reactive(True)

    #: Options that only make sense for the machine they were given with. An
    #: explicit --ctl or --log-dir names a path on ONE host; carried over to the
    #: next host after a switch it would point at nothing, or at the wrong thing.
    HOST_SPECIFIC = ("ctl", "log_dir", "queue_file")

    def __init__(
        self,
        runner: Optional[Runner],
        refresh_interval: float = 2.0,
        live_interval: float = 60.0,
        start_theme: str = DARK_THEME,
        options: Optional[dict] = None,
    ) -> None:
        super().__init__()
        #: None until a host is picked (the dashboard was opened without --host).
        self.runner = runner
        self._options = dict(options or {})
        self._host_key: Optional[str] = None
        if runner is not None:
            self._host_key = getattr(runner, "host", "") or LOCAL
        self.refresh_interval = refresh_interval
        self.live_interval = live_interval
        self.start_theme = start_theme
        # Scope of the S3 recount to request on the NEXT dump:
        #   ""        cheap dump, use the counts the driver recorded
        #   "running" totals per library + done for the running row  (a few calls)
        #   "all"     done for every row (user-requested; seconds on a long queue)
        self._live_scope = "running"
        self._recounting = False
        #: an S3 recount is in flight; cheap refreshes stand aside
        self._live_in_flight = False
        #: last known live S3 counts, {job key: {done, total}}
        self._count_cache: dict = {}
        self.snapshot: Snapshot = Snapshot()
        self.filter_status: Optional[str] = None
        self._log_model: Optional[str] = None
        #: (job key, lines) last shown in the drawer; frozen while follow is off
        self._log_shown: Optional[tuple] = None
        self._log_height = 14
        self._dark = True
        self._menu: Optional[ContextMenu] = None

    @property
    def tokens(self) -> Tokens:
        """Cell colours for the current theme, read by every painted widget."""
        return tokens(self._dark)

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Band(id="band")
        yield ContextLine(id="context")
        yield Banner(id="banner")
        yield RunningCard(id="card")
        yield StatusSummary(id="summary")
        yield QueueHeader(id="qhead")
        yield QueueView(id="table")
        yield LogDrawer(id="log")
        yield KeyFooter(id="footer")

    def on_mount(self) -> None:
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = self.start_theme
        self._render_footer()
        self._render_banner(None)
        self._render_header()
        self.query_one("#table", QueueView).focus()
        if self.runner is None:
            self._pick_host(initial=True)
        else:
            self.refresh_snapshot()
        self.set_interval(self.refresh_interval, self._tick)
        # Periodically ask for a real S3 recount, the way scheduler-status.sh does.
        # Totals cost one listing per library and `done` one for the running row, so
        # this stays bounded no matter how long the queue is.
        if self.live_interval > 0:
            self.set_interval(self.live_interval, self._request_live)

    # ------------------------------------------------------------------
    # data flow
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        """The cheap periodic refresh.

        Skipped while an S3 recount is in flight: the dump worker is `exclusive`, so
        a 2-second tick would cancel the recount and discard its result — the very
        numbers the user asked for, thrown away seconds before they arrive.
        """
        if self._live_in_flight or self.runner is None:
            return
        self.refresh_snapshot()

    def _request_live(self, scope: str = "running") -> None:
        """Mark the next dump as needing an S3 recount, and fetch it now."""
        if self.runner is None:
            return
        # "all" must not be downgraded by a periodic tick that lands first.
        if scope == "all" or self._live_scope != "all":
            self._live_scope = scope
        self._live_in_flight = True
        self.refresh_snapshot()

    @work(thread=True, exclusive=True, group="dump")
    def refresh_snapshot(self) -> None:
        """Poll one snapshot. Runs off the UI thread; posts the result back.

        The runner is captured once and travels with the result. After a host
        switch, a dump still in flight from the old host (a full recount can
        take minutes) must be recognised as stale and dropped — otherwise its
        counts would be harvested into the new host's cache under matching job
        keys, and one machine's progress would show on another's jobs.
        """
        runner = self.runner
        if runner is None:
            return
        log_path = self._log_path_for_request()
        scope, self._live_scope = self._live_scope, ""
        try:
            text = runner.dump(log_path, live=scope)
        except RunnerError as exc:
            self.call_from_thread(self._on_transport_error, str(exc), runner)
            return
        snapshot = parse_dump(text)
        self.call_from_thread(self._on_snapshot, snapshot, runner)

    def _log_path_for_request(self) -> Optional[str]:
        """Ask for a log tail only when the pane is open — a 300-line tail per
        tick is wasted bytes over SSH when nobody is looking at it."""
        if not self.show_log:
            return None
        running = self.snapshot.running_job()
        key = self._log_model or (running.key if running else None)
        if not key:
            return None
        job = self.snapshot.find_by_key(key)
        return job.log if job and job.log else None

    def _on_transport_error(self, message: str, runner: Runner) -> None:
        if runner is not self.runner:
            return  # from a host we have since switched away from
        self._live_in_flight = False
        if self._recounting:
            self._recounting = False
            self.notify("Recount failed — see the banner.", severity="error", timeout=6)
        self.snapshot.error = message
        self._render_banner(f"Cannot reach the scheduler: {message}", error=True)

    def _on_snapshot(self, snapshot: Snapshot, runner: Runner) -> None:
        if runner is not self.runner:
            return  # from a host we have since switched away from
        self._live_in_flight = False
        # Carry live S3 counts across the cheap refreshes that do not include them.
        if snapshot.counts_are_live:
            harvest_counts(snapshot, self._count_cache)
        apply_cached_counts(snapshot, self._count_cache)
        self.snapshot = snapshot
        if snapshot.error:
            self._render_banner(snapshot.error, error=True)
        elif snapshot.driver_legacy:
            self._render_banner(
                f"Old driver (pid {snapshot.driver_pid or '?'}). Watching is safe and "
                "the progress below is real. Queue edits will not apply until you "
                "restart the driver on the current version."
            )
        elif not snapshot.driver_alive:
            # Say which half of the UI still works, because "STOPPED" alone leaves
            # you guessing whether an edit landed. Queue edits are written to the
            # queue file and the driver re-reads it before every job, so staging a
            # queue against a stopped driver is a real workflow — but run-control
            # has nothing to act on and ctl will refuse it.
            self._render_banner(
                "No driver running. Queue edits (add, remove, reorder, hold, retry) "
                "are saved and take effect when you start one. Run-control "
                "(stop-after-current) is unavailable, and Cancel falls back to Hold."
            )
        else:
            self._render_banner(None)
        self._render_header()
        self._render_footer()
        self._render_chips()
        self._render_table()
        self._render_log()
        if self._recounting and snapshot.counts_are_live:
            self._recounting = False
            self.notify("S3 recount done.", timeout=3)

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------
    def _render_banner(self, message: Optional[str], error: bool = False) -> None:
        self.query_one("#banner", Banner).show(message or "", error)

    def _host_label(self) -> str:
        if self.runner is None:
            return "no host"
        location = self.runner.location
        return socket.gethostname() if location == "local" else location

    def _render_header(self) -> None:
        snap = self.snapshot
        self.query_one("#band", Band).show(self._host_label(), snap)
        self.query_one("#context", ContextLine).show(
            (
                ("queue", _shorten_path(snap.queue_file) or "<unknown>"),
                ("lib", snap.default_library or "<none>"),
                ("wave", snap.driver_info.get("default_wave_size", "?")),
                ("partition", snap.driver_info.get("default_queue", "?")),
            )
        )
        self.query_one("#card", RunningCard).show_snapshot(snap)

    def _render_footer(self) -> None:
        groups = [list(group) for group in draw.DASHBOARD_KEYS]
        if self.snapshot.paused:
            groups[3][0] = ("p", "resume")
        self.query_one("#footer", KeyFooter).show(tuple(tuple(g) for g in groups))

    def _render_chips(self) -> None:
        self.query_one("#summary", StatusSummary).show(
            self.snapshot.counts(), self.filter_status
        )

    def visible_jobs(self) -> List[Job]:
        if self.filter_status is None:
            return self.snapshot.jobs
        return [j for j in self.snapshot.jobs if j.status == self.filter_status]

    def _render_table(self) -> None:
        table = self.query_one("#table", QueueView)
        self.query_one("#qhead", QueueHeader).show(table.cols)
        table.render_jobs(self.visible_jobs())

    def _render_log(self) -> None:
        if not self.show_log:
            return
        snap = self.snapshot
        key = self._log_model
        if key is None:
            running = snap.running_job()
            key = running.key if running else None
        job = snap.find_by_key(key) if key else None
        model = job.model if job else ""

        # With follow off the view is frozen so you can actually read a wall of
        # orchestrator output without it being yanked to the bottom every tick.
        if self.follow_log or self._log_shown is None or self._log_shown[0] != key:
            self._log_shown = (key, tuple(snap.log_text.splitlines()))
        lines = self._log_shown[1]

        if model:
            placeholder = f"(no output yet in {snap.log_path or 'the job log'})"
        else:
            placeholder = "(select a job and press l, or double-click a row)"
        self.query_one("#log", LogDrawer).show(
            model, self.follow_log, lines, placeholder
        )

    # ------------------------------------------------------------------
    # ctl invocation
    # ------------------------------------------------------------------
    @work(thread=True, group="ctl")
    def run_ctl(self, *args: str) -> None:
        """Invoke a ctl verb and report the outcome as a toast."""
        runner = self.runner
        if runner is None:
            return
        try:
            rc, out, err = runner.run(*args)
        except RunnerError as exc:
            self.call_from_thread(self.notify, str(exc), severity="error", timeout=8)
            return
        message = out.strip() or err.strip() or f"{' '.join(args)}: rc={rc}"
        # Report the first line as the headline; ctl is chatty on purpose.
        headline = message.splitlines()[0]
        severity = "information" if rc == 0 else "error"
        self.call_from_thread(self.notify, headline, severity=severity, timeout=6)
        # Pull a fresh snapshot immediately so the table reflects the change now
        # rather than at the next tick — unless the host changed meanwhile.
        if runner is self.runner:
            self.call_from_thread(self.refresh_snapshot)

    # ------------------------------------------------------------------
    # selection helpers
    # ------------------------------------------------------------------
    @property
    def selected(self) -> Optional[Job]:
        key = self.query_one("#table", QueueView).selected_key
        if not key:
            return None
        return self.snapshot.find_by_key(key)

    def _sel(self, job: Job) -> str:
        """An unambiguous ctl selector for this job.

        ctl accepts a model id or a 1-based position. A model id is stable across
        reordering and so is normally the better choice — but the same model may be
        queued against several libraries, and ctl then acts on the FIRST match. In
        that case only the position identifies the row the user is looking at.
        """
        if self.snapshot.model_count(job.model) > 1:
            return str(job.pos)
        return job.model

    def _need_selection(self) -> Optional[Job]:
        job = self.selected
        if job is None:
            self.notify(
                "Select a job first (click a row, or use ↑/↓).", severity="warning"
            )
        return job

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def action_add(self) -> None:
        snap = self.snapshot

        def on_close(result: Optional[dict]) -> None:
            if not result:
                return
            args = ["add", result["model"], result["mode"]]
            # ctl takes positionals in order, so a later field being set means the
            # earlier ones must be present too.
            library = result["library"]
            wave = result["wave"]
            queue = result["queue"]
            if library or wave or queue:
                args.append(library)
            if wave or queue:
                args.append(wave)
            if queue:
                args.append(queue)
            # --cpus is a named option, so unlike the positionals it needs no
            # placeholder padding and can be omitted independently.
            if result.get("cpus"):
                args += ["--cpus", result["cpus"]]
            if result["top"]:
                args.append("--top")
            self.run_ctl(*args)

        self.push_screen(
            AddScreen(snap.libraries, snap.default_library, snap.max_cpus_per_task),
            on_close,
        )

    def action_remove(self) -> None:
        job = self._need_selection()
        if not job:
            return

        def on_close(confirmed: bool) -> None:
            if confirmed:
                self.run_ctl("rm", self._sel(job))

        self.push_screen(
            ConfirmScreen(
                "Remove from the queue?",
                f"{job.model} ({job.mode}) on {job.library}\n\n"
                "The queue line is deleted. Results already in S3 are untouched, "
                "and re-adding it later resumes where it left off.",
                ok_label="Remove",
            ),
            on_close,
        )

    def action_top(self) -> None:
        job = self._need_selection()
        if job:
            self.run_ctl("top", self._sel(job))

    def action_move_up(self) -> None:
        job = self._need_selection()
        if job:
            self.run_ctl("up", self._sel(job))

    def action_move_down(self) -> None:
        job = self._need_selection()
        if job:
            self.run_ctl("down", self._sel(job))

    def action_hold(self) -> None:
        job = self._need_selection()
        if job:
            self.run_ctl("unhold" if job.hold else "hold", self._sel(job))

    def action_retry(self) -> None:
        job = self._need_selection()
        if job:
            self.run_ctl("retry", self._sel(job))

    def action_cancel(self) -> None:
        job = self._need_selection()
        if not job:
            return
        if job.is_running:
            title = "Cancel the RUNNING model?"
            detail = (
                f"{job.model} on {job.library}\n\n"
                "Its in-flight SLURM array will be scancel'd and the orchestrator "
                "killed. Chunks already written to S3 are kept, so a later retry "
                "resumes from there. The queue then moves to the next model."
            )
        else:
            title = "Hold this job?"
            detail = (
                f"{job.model} is {job.status}, not running.\n\n"
                "It will be marked `hold` in the queue file so the driver skips it. "
                "Use Remove to drop it entirely."
            )

        def on_close(confirmed: bool) -> None:
            if confirmed:
                self.run_ctl("cancel", self._sel(job))

        self.push_screen(ConfirmScreen(title, detail, ok_label="Do it"), on_close)

    def action_pause(self) -> None:
        self.run_ctl("resume" if self.snapshot.paused else "pause")

    def action_stop_after(self) -> None:
        # There is no "current" to stop after. The flag would sit in the control dir
        # and arm the NEXT driver to quit after its first model — a delayed surprise
        # rather than a no-op, which is why the driver discards it at startup and ctl
        # refuses it. Fail here too so the key does not open a dialog that cannot work.
        if not self.snapshot.driver_alive:
            self.notify(
                "No driver running — nothing to stop after. Start one first.",
                severity="warning",
                timeout=5,
            )
            return

        def on_close(confirmed: bool) -> None:
            if confirmed:
                self.run_ctl("stop-after-current")

        self.push_screen(
            ConfirmScreen(
                "Stop after the current model?",
                "The driver finishes the model it is running, then exits. "
                "Nothing in flight is killed.",
                ok_label="Arm it",
            ),
            on_close,
        )

    def action_recount(self) -> None:
        """Recount every row from S3, like scheduler-status.sh does.

        Costs one `aws s3 ls` per model, so it is on demand rather than on a timer;
        the periodic refresh only recounts totals and the running row. Also nudges a
        live driver to update its own record while we are at it.
        """
        self._recounting = True
        self.notify(
            f"Recounting {len(self.snapshot.jobs)} row(s) from S3 — this takes a moment.",
            timeout=4,
        )
        if self.snapshot.driver_alive and not self.snapshot.driver_legacy:
            self.run_ctl("refresh")
        self._request_live("all")

    def action_toggle_log(self) -> None:
        job = self.selected
        if job:
            self._log_model = job.key
        self.show_log = not self.show_log

    def action_toggle_follow(self) -> None:
        self.follow_log = not self.follow_log
        if self.follow_log:
            self.query_one("#log", LogDrawer).back = 0
        self._render_log()

    def action_close_log(self) -> None:
        self.show_log = False

    def action_log_size(self, delta: int) -> None:
        if self.show_log:
            self._resize_log(delta)

    def _resize_log(self, delta: int) -> None:
        # Clamp so neither the drawer nor the queue above it vanishes.
        limit = max(6, self.size.height - 10)
        self._log_height = max(6, min(limit, self._log_height + delta))
        self.query_one("#log", LogDrawer).styles.height = self._log_height

    def action_switch_host(self) -> None:
        self._pick_host(initial=False)

    # ------------------------------------------------------------------
    # host switching
    # ------------------------------------------------------------------
    def _pick_host(self, initial: bool) -> None:
        """Open the host picker. Cancelled at startup, there is nothing to show."""

        def on_close(resolution: Optional[Resolution]) -> None:
            if resolution is not None and resolution.runner is not None:
                self._connect(resolution)
            elif self.runner is None:
                self.exit()

        self.push_screen(HostScreen(self._options, self._host_key), on_close)

    def _connect(self, resolution: Resolution) -> None:
        """Point the dashboard at another scheduler, forgetting the old one.

        Everything cached is per-host and must go: the S3 counts (keyed by job
        key, which says nothing about the machine), the snapshot, the selected
        log, the status filter. In-flight dumps are cancelled, and any that
        still report back are dropped by the runner check in _on_snapshot.
        """
        self.workers.cancel_group(self, "dump")
        self.runner = resolution.runner
        self._host_key = resolution.host or LOCAL
        # Host-specific CLI options applied to the first connection only.
        self._options = {
            k: v for k, v in self._options.items() if k not in self.HOST_SPECIFIC
        }
        self._count_cache = {}
        self.snapshot = Snapshot()
        self._log_model = None
        self._log_shown = None
        self.filter_status = None
        self._live_scope = "running"
        self._live_in_flight = False
        self._recounting = False
        save_last_host(resolution.host or None)
        self._render_banner(None)
        self.query_one("#table", QueueView).reset()
        self._render_header()
        self._render_table()
        self._render_chips()
        if resolution.warning:
            self.notify(resolution.warning, severity="warning", timeout=10)
        if resolution.hint:
            self.notify(resolution.hint, severity="warning", timeout=10)
        self.refresh_snapshot()

    # ------------------------------------------------------------------
    # reactive watchers
    # ------------------------------------------------------------------
    def watch_theme(self, theme_name: str) -> None:
        """Keep the painted regions in step with the app theme.

        They are drawn with literal colours from the design tokens, so they
        cannot pick up TCSS variables — they have to be told, then repainted.
        """
        obj = self.get_theme(theme_name)
        self._dark = bool(obj.dark) if obj else True
        if self.is_mounted:
            for widget in self.query("Painted, QueueView"):
                widget.refresh()

    def action_toggle_dark_theme(self) -> None:
        # Only the two Ersilia themes are in play; this toggles strictly between
        # them rather than cycling Textual's built-ins.
        self.theme = LIGHT_THEME if self.theme == DARK_THEME else DARK_THEME

    def watch_show_log(self, show: bool) -> None:
        if not self.is_mounted:
            return
        self.query_one("#log", LogDrawer).set_class(show, "visible")
        if show:
            self.refresh_snapshot()

    # ------------------------------------------------------------------
    # widget messages
    # ------------------------------------------------------------------
    @on(StatusSummary.Toggled)
    def _on_filter(self, event: StatusSummary.Toggled) -> None:
        self.filter_status = event.status
        self._render_chips()
        self._render_table()

    @on(QueueView.Resized)
    def _on_table_resized(self, event: QueueView.Resized) -> None:
        self.query_one("#qhead", QueueHeader).show(event.cols)

    @on(QueueView.OpenLog)
    def _on_open_log(self, event: QueueView.OpenLog) -> None:
        self._log_model = event.key
        self.show_log = True
        self.refresh_snapshot()

    @on(QueueView.ContextRequested)
    def _on_context(self, event: QueueView.ContextRequested) -> None:
        self._close_menu()
        menu = ContextMenu(event.key)
        self._menu = menu
        self.mount(menu)
        menu.styles.offset = (event.x, min(event.y, max(0, self.size.height - 10)))

    @on(ContextMenu.Chosen)
    def _on_context_chosen(self, event: ContextMenu.Chosen) -> None:
        self._close_menu()
        job = self.snapshot.find_by_key(event.key)
        if job is None:
            return
        verb = event.verb
        if verb == "log":
            self._log_model = job.key
            self.show_log = True
            self.refresh_snapshot()
            return
        if verb == "hold":
            self.run_ctl("unhold" if job.hold else "hold", self._sel(job))
            return
        if verb == "cancel":
            self.action_cancel()
            return
        if verb == "rm":
            self.action_remove()
            return
        self.run_ctl(verb, self._sel(job))

    def _close_menu(self) -> None:
        if self._menu is not None:
            self._menu.remove()
            self._menu = None

    @on(LogDrawer.Dragged)
    def _on_log_dragged(self, event: LogDrawer.Dragged) -> None:
        # Dragging the top edge down shrinks the drawer.
        self._resize_log(-event.delta)

    @on(LogDrawer.Scrolled)
    def _on_log_scrolled(self, event: LogDrawer.Scrolled) -> None:
        # Reading back through the tail must not be yanked to the end each tick.
        if self.follow_log:
            self.follow_log = False
            self._render_log()
