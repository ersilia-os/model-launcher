"""Parse ``sched-ctl.sh dump`` into plain dataclasses.

Deliberately pure: strings in, dataclasses out, no I/O and no Textual imports, so
it can be unit-tested on its own and so a malformed or truncated snapshot (a
half-written file, a dropped SSH connection) degrades into "fewer sections" rather
than a crashed UI.

The queue file is the source of truth for order and flags — the same rule the driver
follows — while ``status.tsv`` supplies each job's verdict and progress. We read the
queue through ctl's ``jobs`` section rather than parsing it here, so library aliases
are resolved exactly once, on the side that owns the alias table.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SECTION_MARKER = "---8<---"

#: Every status the scheduler can report, plus the two the TUI derives itself
#: ("stale" for a running job whose driver died, "pending" as the default).
STATUS_ORDER = [
    "running",
    "pending",
    "held",
    "done",
    "failed",
    "cancelled",
    "missing-files",
    "skipped",
    "stale",
]

#: Statuses that mean "this will not run as things stand".
INACTIVE_STATUSES = {"done", "failed", "cancelled", "missing-files", "skipped", "held"}


@dataclass
class Job:
    """One queue entry, as the TUI displays it."""

    pos: int  # 1-based position in the queue file
    model: str
    mode: str
    library: str
    status: str = "pending"
    done: int = 0
    total: int = 0
    started: str = "-"
    finished: str = "-"
    log: str = ""
    note: str = ""
    wave: str = ""
    queue: str = ""
    flags: str = ""
    #: per-job SLURM cpus-per-task override ("" = the worker's own #SBATCH default)
    cpus: str = ""
    hold: bool = False
    library_is_default: bool = False
    #: True when `done` came from a fresh S3 recount rather than the driver's record
    done_is_live: bool = False

    @property
    def key(self) -> str:
        return f"{self.model}|{self.mode}|{self.library}"

    @property
    def pct(self) -> int:
        if self.total <= 0:
            return 0
        return min(100, int(self.done * 100 / self.total))

    @property
    def is_running(self) -> bool:
        return self.status == "running"


@dataclass
class Snapshot:
    """Everything one ``dump`` told us."""

    runtime: dict[str, str] = field(default_factory=dict)
    driver_info: dict[str, str] = field(default_factory=dict)
    jobs: list[Job] = field(default_factory=list)
    libraries: list[str] = field(default_factory=list)
    log_path: str = ""
    log_text: str = ""
    queue_text: str = ""
    error: str | None = None
    #: True when this snapshot carried a live S3 recount
    counts_are_live: bool = False

    # -- driver facts -----------------------------------------------------
    @property
    def driver_alive(self) -> bool:
        return self.runtime.get("driver_alive") == "1"

    @property
    def paused(self) -> bool:
        return self.runtime.get("paused") == "1"

    @property
    def stop_after_current(self) -> bool:
        return self.runtime.get("stop_after_current") == "1"

    @property
    def queue_file(self) -> str:
        return self.runtime.get("queue_file", "")

    @property
    def driver_legacy(self) -> bool:
        """A driver is running, but it predates this tooling (no driver.info).

        Matters because a pre-upgrade driver parses the queue once at startup: it
        is safe to WATCH, but queue edits stay dormant until it is restarted on the
        current version. The UI has to say so, or an edit that silently does
        nothing looks like a broken dashboard.
        """
        return self.runtime.get("driver_legacy") == "1"

    @property
    def driver_pid(self) -> str:
        return self.driver_info.get("pid", "") or self.runtime.get("legacy_pid", "")

    @property
    def default_library(self) -> str:
        return self.driver_info.get("default_library", "")

    @property
    def max_cpus_per_task(self) -> int:
        """Ceiling for a per-job cpus override, as reported by ctl.

        Read from the dump rather than hardcoded so the client tracks the cluster:
        the fallback only applies against a ctl too old to publish it.
        """
        raw = self.runtime.get("max_cpus_per_task", "")
        return _int(raw) if raw.isdigit() else 32

    @property
    def dispatch(self) -> str:
        """How this target runs models: ``slurm`` or ``serve``.

        Absent on a ctl too old to publish it, which predates any dispatch
        mode but SLURM — so the fallback is the value every existing cluster
        already is.
        """
        return self.runtime.get("dispatch") or "slurm"

    @property
    def sif_dir(self) -> str:
        """Where this target expects Singularity/Apptainer images, as reported by ctl."""
        return self.runtime.get("sif_dir", "")

    @property
    def driver_state(self) -> str:
        """One word for the header: what is this scheduler doing right now."""
        if not self.driver_alive:
            return "STOPPED"
        if self.driver_legacy:
            return "RUNNING (old driver)"
        if self.paused:
            return "PAUSED"
        if self.stop_after_current:
            return "STOPPING"
        return "RUNNING"

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for job in self.jobs:
            out[job.status] = out.get(job.status, 0) + 1
        return out

    def running_job(self) -> Job | None:
        for job in self.jobs:
            if job.is_running:
                return job
        return None

    def find(self, model: str) -> Job | None:
        """First job with this model id. Ambiguous when a model is queued against
        several libraries — prefer :meth:`find_by_key` wherever identity matters."""
        for job in self.jobs:
            if job.model == model:
                return job
        return None

    def find_by_key(self, key: str) -> Job | None:
        """The one job with this `model|mode|library`."""
        for job in self.jobs:
            if job.key == key:
                return job
        return None

    def model_count(self, model: str) -> int:
        return sum(1 for job in self.jobs if job.model == model)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def split_sections(text: str) -> dict[str, list[str]]:
    """Split a dump blob into ``{section_name: [lines]}``.

    Section headers look like ``---8<--- state.tsv`` or ``---8<--- log /path``.
    Anything before the first marker is ignored, which is what makes this
    tolerant of an SSH banner or a stray warning on stdout.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith(SECTION_MARKER):
            header = line[len(SECTION_MARKER) :].strip()
            name = header.split(" ", 1)[0] if header else ""
            arg = header.split(" ", 1)[1] if " " in header else ""
            current = name
            sections[name] = []
            if arg:
                sections.setdefault(f"{name}:arg", []).append(arg)
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def _parse_kv(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _parse_status_tsv(lines: list[str]) -> dict[str, dict[str, str]]:
    """status.tsv -> {key: {status, done, total, started, finished, log, note}}.

    ``log`` and ``note`` are written as ``-`` on disk when empty
    (``scheduler-lib.sh:status_write``) — the same placeholder ``started`` and
    ``finished`` already use for "no value". Unlike those two, ``-`` is not a
    real value for log or note, so it is translated back to ``""`` here,
    mirroring what ``status_load`` does on the bash side.
    """
    cols = ["status", "done", "total", "started", "finished", "log", "note"]
    out: dict[str, dict[str, str]] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        key = parts[0]
        values = parts[1:]
        row = {
            name: (values[i] if i < len(values) else "") for i, name in enumerate(cols)
        }
        if row["log"] == "-":
            row["log"] = ""
        if row["note"] == "-":
            row["note"] = ""
        out[key] = row
    return out


def _parse_state_tsv(lines: list[str]) -> dict[str, dict[str, str]]:
    """state.tsv -> {model: row}. Fallback for entries missing from status.tsv."""
    cols = [
        "idx",
        "model",
        "mode",
        "library",
        "status",
        "done",
        "total",
        "started",
        "finished",
        "log",
    ]
    out: dict[str, dict[str, str]] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        row = {
            name: (parts[i] if i < len(parts) else "") for i, name in enumerate(cols)
        }
        out[row["model"]] = row
    return out


def _int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


#: Bare words that are flags rather than positional fields.
BARE_FLAGS = {"hold"}


def is_queue_flag(token: str) -> bool:
    """Mirror of ``is_queue_flag`` in scheduler-lib.sh."""
    return token in BARE_FLAGS or "=" in token


def parse_queue_line(raw: str):
    """Mirror of ``parse_queue_line`` in scheduler-lib.sh.

    Flags are recognised by shape and may appear anywhere after the model id, so
    ``eos1 ersilia mylib hold`` parses as library=mylib + flag=hold rather than
    putting ``hold`` in the wave_size slot. Keep this in lockstep with the bash
    version — a disagreement here shows the wrong status in the table.

    Returns ``None`` for blank/comment lines, else
    ``(model, mode, library, wave, queue, flags)``.
    """
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        return None
    tokens = stripped.split()
    model = tokens[0]
    positional: list[str] = []
    flags: list[str] = []
    for token in tokens[1:]:
        if is_queue_flag(token):
            flags.append(token)
        elif len(positional) < 4:
            positional.append(token)
        else:
            flags.append(token)
    positional += [""] * (4 - len(positional))
    mode, library, wave, queue = positional[:4]
    return model, mode, library, wave, queue, " ".join(flags)


def _read_jobs(sections: dict[str, list[str]], default_library: str) -> list[Job]:
    """Jobs in queue order.

    Prefers the ``jobs`` section, which ctl produces with library aliases already
    resolved the way the driver resolves them — getting that wrong means every
    status lookup for an aliased entry (``molport``) misses and reads "pending".
    Falls back to parsing the raw queue text so a client stays usable against a
    cluster whose ctl has not been redeployed yet; alias resolution is the only
    thing lost in that mode.
    """
    rows = sections.get("jobs")
    if rows:
        jobs: list[Job] = []
        for line in rows:
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            # cpus is the ninth column, appended by a newer ctl. Padding rather than
            # requiring it keeps this client working against a cluster whose
            # scheduler/ has not been redeployed yet — the override just reads empty.
            parts += [""] * (9 - len(parts))
            (pos, model, mode, library, wave, queue, flags, is_default, cpus) = parts[
                :9
            ]
            jobs.append(
                Job(
                    pos=_int(pos),
                    model=model,
                    mode=mode,
                    library=library,
                    wave=wave,
                    queue=queue,
                    flags=flags,
                    # Fall back to the flags string when the column is absent, so an
                    # older ctl still shows the override it is honouring.
                    cpus=cpus or flag_value(flags, "cpus"),
                    hold=_has_hold(flags),
                    library_is_default=is_default == "1",
                )
            )
        return jobs

    jobs = []
    pos = 0
    for raw in sections.get("queue", []):
        parsed = parse_queue_line(raw)
        if parsed is None:
            continue
        model, mode, library, wave, queue, flags = parsed
        pos += 1
        jobs.append(
            Job(
                pos=pos,
                model=model,
                mode=mode,
                library=library or default_library,
                wave=wave,
                queue=queue,
                flags=flags,
                cpus=flag_value(flags, "cpus"),
                hold=_has_hold(flags),
                library_is_default=not library,
            )
        )
    return jobs


def _has_hold(flags: str) -> bool:
    return any(f in ("hold", "hold=1", "hold=true") for f in flags.split())


def flag_value(flags: str, key: str) -> str:
    """Mirror of ``queue_flag_value`` in scheduler-lib.sh. "" when absent."""
    prefix = f"{key}="
    for token in flags.split():
        if token.startswith(prefix):
            return token[len(prefix) :]
    return ""


def _parse_counts(lines: list[str]) -> dict[str, dict[str, int | None]]:
    """counts section -> {key: {"done": int|None, "total": int|None}}.

    A blank done field means ctl did not recount that row, so the recorded value
    must be kept rather than overwritten with a zero.
    """
    out: dict[str, dict[str, int | None]] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        key, done, total = parts[0], parts[1], parts[2]
        out[key] = {
            "done": _int(done) if done.strip() else None,
            "total": _int(total) if total.strip() else None,
        }
    return out


def harvest_counts(snap: Snapshot, cache: dict[str, dict[str, int]]) -> None:
    """Remember the live counts from this snapshot, keyed by job.

    The cheap 2-second refresh carries no counts at all, so without a cache the
    good numbers from a live tick would be wiped a moment later and the table would
    flicker back to ``0/?``.
    """
    for job in snap.jobs:
        entry = cache.setdefault(job.key, {})
        if job.total > 0:
            entry["total"] = job.total
        if job.done_is_live:
            entry["done"] = job.done


def apply_cached_counts(snap: Snapshot, cache: dict[str, dict[str, int]]) -> None:
    """Fill in counts this snapshot lacks from previously seen live values.

    Totals are safe to reuse outright — a library's chunk count does not change.
    A cached ``done`` is only used when the snapshot has nothing better, so a driver
    that records fresher progress is never overwritten by a stale cached value.
    """
    for job in snap.jobs:
        entry = cache.get(job.key)
        if not entry:
            continue
        if job.total <= 0 and entry.get("total"):
            job.total = entry["total"]
        if job.done <= 0 and entry.get("done"):
            job.done = entry["done"]


def parse_dump(text: str) -> Snapshot:
    """Build a :class:`Snapshot` from a dump blob."""
    snap = Snapshot()
    if not text or SECTION_MARKER not in text:
        snap.error = "no snapshot returned (is sched-ctl.sh deployed?)"
        return snap

    sections = split_sections(text)
    snap.runtime = _parse_kv(sections.get("runtime", []))
    snap.driver_info = _parse_kv(sections.get("driver.info", []))
    snap.libraries = [
        name.strip() for name in sections.get("libraries", []) if name.strip()
    ]
    snap.queue_text = "\n".join(sections.get("queue", []))
    snap.log_path = (sections.get("log:arg") or [""])[0]
    snap.log_text = "\n".join(sections.get("log", [])).strip("\n")

    statuses = _parse_status_tsv(sections.get("status.tsv", []))
    states = _parse_state_tsv(sections.get("state.tsv", []))
    counts = _parse_counts(sections.get("counts", []))
    snap.counts_are_live = bool(counts)
    # status.tsv is the durable authority; state.tsv is only a render view the
    # driver writes. Falling back per-job would resurrect verdicts that `retry`
    # just cleared, so state.tsv is consulted only when there is no status store
    # at all (a pre-upgrade driver, or a wiped LOG_DIR).
    fallback_states = states if not statuses else {}
    default_library = snap.default_library
    driver_alive = snap.driver_alive

    for job in _read_jobs(sections, default_library):
        row = statuses.get(job.key) or fallback_states.get(job.model)
        if row:
            job.status = row.get("status", "pending") or "pending"
            job.done = _int(row.get("done", "0"))
            job.total = _int(row.get("total", "0"))
            job.started = row.get("started", "-") or "-"
            job.finished = row.get("finished", "-") or "-"
            job.log = row.get("log", "")
            job.note = row.get("note", "")

        # `hold` outranks only `pending`. It stops a job being STARTED, so it must
        # not mask a real verdict: a running job is still running, and a cancelled
        # one must not read as "held". The flag stays visible as a marker.
        if job.hold and job.status == "pending":
            job.status = "held"
        # A `running` row with no live driver is a leftover from a killed driver;
        # showing it as running would be a lie.
        if job.status == "running" and not driver_alive:
            job.status = "stale"
        if not job.mode:
            job.status = "skipped"
            job.note = job.note or "missing mode (want ersilia|singularity)"

        # Live S3 counts win over what the driver recorded: an old driver only ever
        # wrote counts at job start, so its recorded numbers are frozen at zero.
        live = counts.get(job.key)
        if live:
            if live["total"] is not None:
                job.total = live["total"]
            if live["done"] is not None:
                job.done = live["done"]
                job.done_is_live = True

        snap.jobs.append(job)

    return snap
