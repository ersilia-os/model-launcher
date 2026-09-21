"""Tests for the dump parser.

These are the tests worth having: the parser is the one place where a
misunderstanding between the bash scheduler and this client turns into a wrong
status on screen — and a wrong status is what makes someone cancel the wrong model.

No cluster, no AWS, no SSH needed.
"""

from model_launcher.core.model import (
    Snapshot,
    flag_value,
    is_queue_flag,
    parse_dump,
    parse_queue_line,
    split_sections,
)

DUMP = """\
---8<--- runtime
schema=1
driver_alive=1
paused=0
stop_after_current=0
queue_file=/shared/scheduler/my.queue
now=2026-08-05T11:00:00Z
---8<--- driver.info
pid=41288
default_library=Enamine_Real_Sample_1.4B
default_wave_size=1000
default_queue=cpu-queue
dry_run=0
---8<--- queue
# banner

eos4k4f_v1          ersilia      Enamine_Real_Sample_1.4B
eos12x7_v1          ersilia      Enamine_Real_Sample_1.4B
eos6ojg_v1          ersilia      Enamine_Real_Sample_1.4B     hold
eos_default         ersilia
---8<--- state.tsv
#idx\tmodel\tmode\tlibrary\tstatus\tdone\ttotal\tstarted\tfinished\tlog
1\teos4k4f_v1\tersilia\tEnamine_Real_Sample_1.4B\tdone\t13644\t13644\tT1\tT2\t/l/a.log
---8<--- status.tsv
#key\tstatus\tdone\ttotal\tstarted\tfinished\tlog\tnote
eos4k4f_v1|ersilia|Enamine_Real_Sample_1.4B\tdone\t13644\t13644\tT1\tT2\t/l/a.log\t
eos12x7_v1|ersilia|Enamine_Real_Sample_1.4B\trunning\t4102\t13644\tT3\t-\t/l/b.log\t
---8<--- libraries
Coconut_715K
Enamine_Real_Sample_1.4B
---8<--- log /l/b.log
Wave 5/14 : chunks 4001-5000
  Submitted array job 118432
---8<--- end
"""


class TestSections:
    def test_splits_named_sections(self):
        sections = split_sections(DUMP)
        assert "runtime" in sections
        assert "status.tsv" in sections
        assert sections["log:arg"] == ["/l/b.log"]

    def test_ignores_preamble(self):
        """An SSH banner or stray warning before the first marker must not break us."""
        snap = parse_dump("Warning: something\nmotd line\n" + DUMP)
        assert len(snap.jobs) == 4
        assert snap.driver_alive


class TestQueueLine:
    def test_blank_and_comment(self):
        assert parse_queue_line("") is None
        assert parse_queue_line("   ") is None
        assert parse_queue_line("  # indented comment") is None

    def test_full_line(self):
        assert parse_queue_line("m1 ersilia lib 500 gpu-queue") == (
            "m1",
            "ersilia",
            "lib",
            "500",
            "gpu-queue",
            "",
        )

    def test_flag_is_position_independent(self):
        """`hold` in the wave slot must be read as a flag, not as wave_size=hold —
        otherwise the driver rejects the line as 'wave_size out of 1..1000'."""
        assert parse_queue_line("m1 ersilia lib hold") == (
            "m1",
            "ersilia",
            "lib",
            "",
            "",
            "hold",
        )

    def test_unknown_flags_preserved(self):
        _, _, _, _, _, flags = parse_queue_line("m1 ersilia lib 500 q future=x hold")
        assert flags == "future=x hold"

    def test_only_model_and_mode(self):
        assert parse_queue_line("m1 ersilia") == ("m1", "ersilia", "", "", "", "")

    def test_flag_shapes(self):
        assert is_queue_flag("hold")
        assert is_queue_flag("hold=1")
        assert is_queue_flag("anything=value")
        assert not is_queue_flag("ersilia")
        assert not is_queue_flag("500")


class TestSnapshot:
    def setup_method(self):
        self.snap = parse_dump(DUMP)

    def test_driver_facts(self):
        assert self.snap.driver_state == "RUNNING"
        assert self.snap.driver_pid == "41288"
        assert self.snap.queue_file == "/shared/scheduler/my.queue"
        assert self.snap.libraries == ["Coconut_715K", "Enamine_Real_Sample_1.4B"]

    def test_queue_order_is_preserved(self):
        assert [j.model for j in self.snap.jobs] == [
            "eos4k4f_v1",
            "eos12x7_v1",
            "eos6ojg_v1",
            "eos_default",
        ]
        assert [j.pos for j in self.snap.jobs] == [1, 2, 3, 4]

    def test_status_and_progress_from_store(self):
        job = self.snap.find("eos12x7_v1")
        assert job.status == "running"
        assert (job.done, job.total, job.pct) == (4102, 13644, 30)
        assert job.log == "/l/b.log"

    def test_hold_wins_over_pending(self):
        assert self.snap.find("eos6ojg_v1").status == "held"

    def test_hold_does_not_mask_running(self):
        """`hold` stops a job being STARTED; it does not stop one in flight, so a
        held+running job must still read as running or you cannot see what to
        cancel."""
        dump = DUMP.replace(
            "eos12x7_v1          ersilia      Enamine_Real_Sample_1.4B",
            "eos12x7_v1          ersilia      Enamine_Real_Sample_1.4B     hold",
        )
        job = parse_dump(dump).find("eos12x7_v1")
        assert job.status == "running"
        assert job.hold  # the flag is still recorded, for the UI marker

    def test_hold_does_not_mask_a_verdict(self):
        """Holding a cancelled job must not make it read as merely 'held' — that
        hides the cancellation you just performed."""
        dump = DUMP.replace(
            "eos12x7_v1|ersilia|Enamine_Real_Sample_1.4B\trunning",
            "eos12x7_v1|ersilia|Enamine_Real_Sample_1.4B\tcancelled",
        ).replace(
            "eos12x7_v1          ersilia      Enamine_Real_Sample_1.4B",
            "eos12x7_v1          ersilia      Enamine_Real_Sample_1.4B     hold",
        )
        job = parse_dump(dump).find("eos12x7_v1")
        assert job.status == "cancelled"
        assert job.hold

    def test_default_library_applied(self):
        job = self.snap.find("eos_default")
        assert job.library == "Enamine_Real_Sample_1.4B"
        assert job.library_is_default

    def test_counts_and_running(self):
        assert self.snap.counts() == {"done": 1, "running": 1, "held": 1, "pending": 1}
        assert self.snap.running_job().model == "eos12x7_v1"

    def test_log_section(self):
        assert self.snap.log_path == "/l/b.log"
        assert "Submitted array job 118432" in self.snap.log_text


class TestDegradedInput:
    def test_empty(self):
        snap = parse_dump("")
        assert snap.error
        assert snap.jobs == []

    def test_garbage(self):
        snap = parse_dump("total nonsense\nno markers here")
        assert snap.error

    def test_truncated_mid_dump(self):
        """A connection cut halfway through must yield fewer facts, not an exception."""
        truncated = DUMP[: DUMP.index("---8<--- status.tsv")]
        snap = parse_dump(truncated)
        assert len(snap.jobs) == 4
        # no status store at all -> fall back to state.tsv
        assert snap.find("eos4k4f_v1").status == "done"
        assert snap.find("eos12x7_v1").status == "pending"

    def test_running_without_driver_is_stale(self):
        """A `running` row with a dead driver must not be shown as live progress."""
        snap = parse_dump(DUMP.replace("driver_alive=1", "driver_alive=0"))
        assert snap.find("eos12x7_v1").status == "stale"
        assert snap.driver_state == "STOPPED"

    def test_retry_cleared_verdict_is_not_resurrected(self):
        """status.tsv is the authority: once `retry` drops a key, the stale
        state.tsv row must not bring the old verdict back."""
        dump = DUMP.replace(
            "eos4k4f_v1|ersilia|Enamine_Real_Sample_1.4B\tdone\t13644\t13644\tT1\tT2\t/l/a.log\t\n",
            "",
        )
        snap = parse_dump(dump)
        assert snap.find("eos4k4f_v1").status == "pending"

    def test_paused_state(self):
        snap = parse_dump(DUMP.replace("paused=0", "paused=1"))
        assert snap.driver_state == "PAUSED"

    def test_stopping_state(self):
        snap = parse_dump(DUMP.replace("stop_after_current=0", "stop_after_current=1"))
        assert snap.driver_state == "STOPPING"

    def test_missing_mode_is_skipped(self):
        snap = parse_dump(DUMP.replace("eos_default         ersilia", "eos_default"))
        assert snap.find("eos_default").status == "skipped"


JOBS_SECTION = """\
---8<--- jobs
#pos\tmodel\tmode\tlibrary\twave\tqueue\tflags\tlib_is_default
1\tmtb-public-models\tsingularity\tMolport_Screening_Compounds_5.3M\t500\t\t\t0
2\teos_default\tersilia\tEnamine_Real_Sample_1.4B\t\t\t\t1
3\teos_held\tersilia\tCoconut_715K\t\t\thold\t0
"""


class TestJobsSection:
    """ctl resolves library aliases before keying status; if the client re-derived
    the library from the raw queue text, an aliased entry ('molport') would key on
    the alias, miss the status store, and display "pending" for a job the driver
    had already marked missing-files."""

    def _dump_with_jobs(self, extra_status=""):
        head = DUMP[: DUMP.index("---8<--- state.tsv")]
        return head + JOBS_SECTION + "---8<--- status.tsv\n" + extra_status

    def test_jobs_section_wins_over_raw_queue(self):
        snap = parse_dump(self._dump_with_jobs())
        assert [j.model for j in snap.jobs] == [
            "mtb-public-models",
            "eos_default",
            "eos_held",
        ]
        # the resolved canonical name, not the `molport` alias in the queue text
        assert (
            snap.find("mtb-public-models").library == "Molport_Screening_Compounds_5.3M"
        )
        assert snap.find("mtb-public-models").wave == "500"

    def test_alias_resolved_status_lookup_hits(self):
        status = (
            "mtb-public-models|singularity|Molport_Screening_Compounds_5.3M"
            "\tmissing-files\t0\t0\t-\t-\t\tno input chunks\n"
        )
        snap = parse_dump(self._dump_with_jobs(status))
        job = snap.find("mtb-public-models")
        assert job.status == "missing-files"
        assert job.note == "no input chunks"

    def test_flags_and_default_marker(self):
        snap = parse_dump(self._dump_with_jobs())
        assert snap.find("eos_held").hold
        assert snap.find("eos_held").status == "held"
        assert snap.find("eos_default").library_is_default
        assert not snap.find("mtb-public-models").library_is_default

    def test_falls_back_when_ctl_is_older(self):
        """A cluster whose ctl predates the jobs section must still render."""
        snap = parse_dump(DUMP)  # no jobs section
        assert len(snap.jobs) == 4
        assert snap.find("eos12x7_v1").status == "running"


class TestEmptySnapshot:
    def test_defaults_are_safe(self):
        """The app renders a Snapshot() before the first dump arrives."""
        snap = Snapshot()
        assert snap.driver_state == "STOPPED"
        assert snap.counts() == {}
        assert snap.running_job() is None
        assert snap.find("anything") is None


# Nine columns: `cpus` appended after lib_is_default. The eight-column
# JOBS_SECTION above is deliberately left as-is — it is the regression test for a
# cluster whose scheduler/ has not been redeployed yet.
JOBS_SECTION_CPUS = """\
---8<--- jobs
#pos\tmodel\tmode\tlibrary\twave\tqueue\tflags\tlib_is_default\tcpus
1\teos_heavy\tersilia\tCoconut_715K\t\t\tcpus=16\t0\t16
2\teos_plain\tersilia\tCoconut_715K\t\t\t\t0\t
3\teos_both\tersilia\tCoconut_715K\t\t\thold cpus=32\t0\t32
"""


class TestCpusOverride:
    """`cpus=N` decides how densely tasks pack a node, and cpu-queue does no memory
    accounting — so a job displayed without its override looks identical to one that
    will OOM. The column and the flags string must never disagree."""

    def _dump(self, jobs_section):
        head = DUMP[: DUMP.index("---8<--- state.tsv")]
        return head + jobs_section + "---8<--- status.tsv\n"

    def test_reads_the_cpus_column(self):
        snap = parse_dump(self._dump(JOBS_SECTION_CPUS))
        assert snap.find("eos_heavy").cpus == "16"
        assert snap.find("eos_both").cpus == "32"

    def test_absent_override_is_empty_not_a_default(self):
        """Empty must stay empty: the driver turns "" into "pass no
        --cpus-per-task at all", so inventing a number here would silently re-pack
        the job with a value nobody tuned."""
        snap = parse_dump(self._dump(JOBS_SECTION_CPUS))
        assert snap.find("eos_plain").cpus == ""

    def test_cpus_coexists_with_hold(self):
        job = parse_dump(self._dump(JOBS_SECTION_CPUS)).find("eos_both")
        assert job.hold and job.cpus == "32"

    def test_falls_back_to_flags_when_ctl_is_older(self):
        """An eight-column ctl still honours a `cpus=` flag it cannot report, so read
        it out of `flags` rather than showing the job as un-overridden."""
        older = (
            "---8<--- jobs\n"
            "#pos\tmodel\tmode\tlibrary\twave\tqueue\tflags\tlib_is_default\n"
            "1\teos_heavy\tersilia\tCoconut_715K\t\t\tcpus=16\t0\n"
            "2\teos_plain\tersilia\tCoconut_715K\t\t\t\t0\n"
        )
        snap = parse_dump(self._dump(older))
        assert snap.find("eos_heavy").cpus == "16"
        assert snap.find("eos_plain").cpus == ""

    def test_parsed_from_raw_queue_text_too(self):
        """The no-jobs-section fallback path must not lose the override either."""
        dump = DUMP.replace(
            "eos6ojg_v1          ersilia      Enamine_Real_Sample_1.4B     hold",
            "eos6ojg_v1          ersilia      Enamine_Real_Sample_1.4B     hold cpus=8",
        )
        snap = parse_dump(dump)
        job = snap.find("eos6ojg_v1")
        assert job.cpus == "8" and job.hold


class TestMaxCpus:
    def test_reads_the_published_ceiling(self):
        dump = DUMP.replace("schema=1", "schema=1\nmax_cpus_per_task=64")
        assert parse_dump(dump).max_cpus_per_task == 64

    def test_defaults_when_ctl_does_not_publish_it(self):
        """A ctl too old to publish it still has to give the add dialog a bound."""
        assert parse_dump(DUMP).max_cpus_per_task == 32


class TestFlagValue:
    """Mirror of queue_flag_value in scheduler-lib.sh."""

    def test_reads_a_key_value_flag(self):
        assert flag_value("hold cpus=16", "cpus") == "16"

    def test_missing_key_is_empty(self):
        assert flag_value("hold", "cpus") == ""
        assert flag_value("", "cpus") == ""

    def test_does_not_match_a_key_that_merely_ends_the_same(self):
        assert flag_value("maxcpus=4", "cpus") == ""


DUP_JOBS = """\
---8<--- runtime
schema=1
driver_alive=1
paused=0
stop_after_current=0
---8<--- driver.info
pid=1
---8<--- jobs
#pos\tmodel\tmode\tlibrary\twave\tqueue\tflags\tlib_is_default\tcpus
1\tmtb-public-models\tsingularity\tEnamine_Real_44g_selected_100M\t\t\t\t0\t
2\teos21dr_v4\tersilia\tEnamine_Liquid_Stock_2.5M\t\t\tcpus=8\t0\t8
3\teos21dr_v4\tersilia\tMolport_Screening_Compounds_5.3M\t\t\t\t0\t
---8<--- status.tsv
---8<--- end
"""


class TestDuplicateModel:
    """The same model queued against two libraries is legitimate — and it once
    crashed the table with DuplicateKey because rows were keyed by model id."""

    def setup_method(self):
        self.snap = parse_dump(DUP_JOBS)

    def test_both_rows_survive(self):
        assert [j.pos for j in self.snap.jobs] == [1, 2, 3]
        assert self.snap.model_count("eos21dr_v4") == 2

    def test_keys_are_unique(self):
        keys = [j.key for j in self.snap.jobs]
        assert len(keys) == len(set(keys))

    def test_find_by_key_picks_the_right_one(self):
        job = self.snap.find_by_key(
            "eos21dr_v4|ersilia|Molport_Screening_Compounds_5.3M"
        )
        assert job.pos == 3
        assert job.library == "Molport_Screening_Compounds_5.3M"

    def test_find_by_model_is_the_first_and_therefore_ambiguous(self):
        # documents why find() must not be used for identity
        assert self.snap.find("eos21dr_v4").pos == 2
