# Deploying the scheduler to a machine

The Python package (`pip install model-launcher`) only gets you the
dashboard. The scheduler itself — `sched-ctl.sh`, `run-model-queue.sh`, the
wave orchestrators — is bash that has to physically exist on the machine that
runs it. This is that step, done by hand, once per machine.


## What to copy

Everything under `remote/` in an installed checkout — one directory, not two.
Find it with:

```bash
python3 -c "from model_launcher.core.remote import remote_dir; print(remote_dir())"
```

```
remote/
├── sched-ctl.sh                  the control CLI — every command goes through this
├── run-model-queue.sh            the driver — runs the queue, one model at a time
├── scheduler-lib.sh              shared helpers, sourced by both of the above
├── scheduler-status.sh           plain-text queue view, no Python needed
├── start-scheduler-tmux.sh       starts the driver detached, survives disconnect
├── library-aliases.sh            short names -> full library names (e.g. molport)
├── scheduler.conf.example        copy to scheduler.conf and edit for this machine
├── example.queue                 copy to make your own queue file — see below
└── slurm/
    ├── submit-ersilia-waves.sh       wave orchestrator, ersilia mode
    ├── submit-singularity-waves.sh   wave orchestrator, singularity mode
    ├── run-ersilia-wave-job.sh       the actual #SBATCH worker, ersilia mode
    └── run-singularity-wave-job.sh   the actual #SBATCH worker, singularity mode
```

Older documentation for this scheduler (from before it was a package)
describes "two deploy targets" — the scheduler scripts and the wave
orchestrators, which used to live in different directories and had to be
synced separately. That trap no longer exists: `slurm/` moved inside
`remote/`, so one copy gets both.

## Copying it over

Any transfer method that preserves the directory structure works. For
example, over SSH:

```bash
SRC="$(python3 -c 'from model_launcher.core.remote import remote_dir; print(remote_dir())')"
DEST=/shared/scripts/scheduler   # or wherever you keep it

rsync -av --exclude '__pycache__' "$SRC"/ "you@target:$DEST"/
ssh you@target "chmod +x $DEST"/*.sh "$DEST"/slurm/*.sh
```

**`$DEST` and `$SRC` are LOCAL shell variables**, expanded on your laptop
before the command is sent to `ssh` — every later command in this guide reuses
them the same way. That only works if you run everything in the *same*
terminal session; open a new tab, start a new SSH connection, or come back
tomorrow, and `$DEST` is empty again. If a command below fails with something
like `cannot stat '/example.queue'` (a leading slash, no path before it),
that is exactly this — `$DEST` was unset, not a real error. Re-run the two
lines above (or `echo "$DEST"` to check) before continuing. Do not
backslash-escape `$DEST` in these commands (`\$DEST`) — that sends the literal
text `$DEST` to the *remote* shell, where it was never set either.

## Configuring the target

Copy the example config and edit it for the machine:

```bash
ssh you@target "cp $DEST/scheduler.conf.example $DEST/scheduler.conf"
```

Then edit `scheduler.conf` on the target, uncommenting and setting whatever
differs from the built-in defaults — `LOG_DIR`, `S3_BUCKET`, `SIF_DIR`,
`DEFAULT_QUEUE`, `MAX_CPUS_PER_TASK`. It is sourced by both `sched-ctl.sh` and
`run-model-queue.sh`, so one file configures everything.

**It must stay a defaults file.** Every line has to read `VAR="${VAR:-value}"`,
never `VAR=value` — a plain assignment would silently override an explicit
`--log-dir` flag or a `LOG_DIR=...` set in the environment. The example file
is written this way already; keep any edits in the same shape.

**Editing it (or `models.queue`, later) interactively on the target** needs a
real terminal, which a plain `ssh host "command"` does not allocate — `ssh
you@target "nano $DEST/scheduler.conf"` fails with `Error opening terminal:
unknown`. Add `-t` to force one:

```bash
ssh -t you@target "nano $DEST/scheduler.conf"
```

Editing locally and copying the finished file up works too, and avoids this
entirely:

```bash
scp "you@target:$DEST/scheduler.conf" /tmp/scheduler.conf   # pull
# edit /tmp/scheduler.conf locally
scp /tmp/scheduler.conf "you@target:$DEST/scheduler.conf"   # push
```

## Verifying the copy landed completely

A partial sync has bitten before, silently. Run these on the target after
copying — the counts are what this version of the scripts should contain:

```bash
grep -c QUEUE_LOCK_DEPTH "$DEST/scheduler-lib.sh"              # 5
grep -c CPUS_PER_TASK "$DEST/run-model-queue.sh"                # 4
grep -c CPUS_PER_TASK "$DEST/slurm/submit-ersilia-waves.sh"     # 7
grep -c 'cpus-per-task=8' "$DEST/slurm/run-ersilia-wave-job.sh" # 2
grep -c emit_counts "$DEST/sched-ctl.sh"                        # 2
grep -c audit_log "$DEST/sched-ctl.sh"                          # 15
```

If any of these come back lower than expected, the copy is incomplete — redo
it rather than starting the driver.

## Creating your queue file

Copy `example.queue` rather than writing one from scratch — it documents the
line format, the alias names, and the `hold`/`cpus=N` flags inline, and comes
with a few commented-out example jobs to edit or delete:

```bash
ssh you@target "cp $DEST/example.queue $DEST/models.queue"
```

Then edit `models.queue` on the target for the models and library you
actually want to run (same `ssh -t ... nano ...`, or pull/edit/push, as
above). It is a live file — `sched-ctl.sh` (or the dashboard) edits it safely
under a lock while the driver runs, and hand-editing is fine too, so there is
no need to get it exactly right before starting the driver.

**Taking over an existing deployment?** Everything above assumes a machine
that has never run this scheduler before. If you are instead moving an
already-running production scheduler to a new install path (this package,
replacing an older ad hoc checkout), there is no queue file to copy yet — the
production one is wherever the old deployment kept it. Before doing anything
else:

1. **Confirm the old driver is actually stopped.** `tmux has-session -t
   scheduler` on the target — exit code 1 means no session, but tmux phrases
   this two different ways and both are the same "no session" answer: `can't
   find session: scheduler` if a tmux server is running but that session
   doesn't exist, or `failed to connect to server` if no tmux server has run
   on that host at all yet. Neither is an error to fix. Starting a new driver
   while an old one is still live is exactly the "two models running at once,
   fighting over the cluster" failure the scheduler's invariants exist to
   prevent — if a session *is* found, stop it first (`tmux attach -t
   scheduler`, Ctrl-C) rather than proceeding.
2. **Decide whether to carry over history.** The old `LOG_DIR` holds
   `status.tsv` — every job's verdict — and the old queue file holds whatever
   was still pending. Copy both over if that history matters; start fresh with
   `example.queue` and a **new `LOG_DIR` path** (not the old one) if it does
   not. Reusing the old `LOG_DIR` path silently inherits the old
   `status.tsv`/`state.tsv`, which is rarely what "start fresh" means.

## Starting the driver

Use `start-scheduler-tmux.sh` rather than a raw `tmux` command — it refuses to
double-start, absolutizes paths (a detached tmux session starts in `$HOME`,
not wherever you ran it from), and tees the driver's output to
`$LOG_DIR/driver.log`:

```bash
ssh you@target "cd $DEST && LOG_DIR=/shared/logs/scheduler S3_BUCKET=my-bucket \
    ./start-scheduler-tmux.sh models.queue Enamine_Real_Sample_1.4B"
```

The positional arguments after the queue file — here, the default library —
are the same ones `run-model-queue.sh` itself takes; see its own `--help` for
all of them (default wave size, default partition).

## Confirming it works

From your own laptop, with the package installed:

```bash
model-launcher --host YOUR_ALIAS check
```

Replace `YOUR_ALIAS` with your actual `~/.ssh/config` alias or tailnet name —
never write a placeholder in angle brackets (`<alias>`) into a command you
intend to paste as-is. `<` and `>` are real shell syntax (input/output
redirection), so `--host <alias>` does not mean "put your alias here"; it
silently redirects the command's stdin from a file literally named `alias`
and usually fails with a confusing `No such file or directory`. Every runnable
example in this guide is written to be pasted exactly as shown, with the
placeholders spelled out in caps like `YOUR_ALIAS` precisely so they cannot be
mistaken for real syntax.

**No `--ctl` or `--log-dir` needed while the driver is running.** Over SSH,
the client looks for the running driver and takes both from it: the
`sched-ctl.sh` beside the driver's own script, and the `LOG_DIR` it was
started with. `check` shows what it found (`found  running driver pid …
(discovered)`). If several drivers are running, it asks which one (or, with
no terminal, prints one `--log-dir …` line per driver to choose with).

Pass them explicitly only when discovery cannot help:

- **No driver running**, and you deployed somewhere other than
  `/shared/scripts/scheduler` — pass `--ctl "$DEST/sched-ctl.sh"`.
- **Inspecting a stopped instance** — pass its `--log-dir`. If a driver is
  running under a *different* `LOG_DIR`, `check` warns and names it, since
  that is usually a typo rather than intent.

This is the fastest end-to-end signal that a deploy worked: it prints the
transport, whether the driver is alive, the queue file, and per-job progress.
If it reports the driver as running and shows your queue, you're done.

## Redeploying

Two things behave differently after a redeploy, and it matters which one you
changed:

- **`sched-ctl.sh` changes take effect immediately.** It is a fresh process on
  every invocation — the next `add`, `cancel`, `dump`, whatever, picks up the
  new code automatically.
- **`run-model-queue.sh` and `scheduler-lib.sh` changes need a driver
  restart.** The driver is a long-running process; it read `scheduler-lib.sh`
  once at startup and keeps running its own copy of `run-model-queue.sh`'s
  logic until it is stopped and started again. `sched-ctl.sh`, by contrast,
  re-sources `scheduler-lib.sh` on every call — so after a lib change, the
  live driver and a fresh `ctl` invocation are running *different* versions of
  code that has to agree (queue parsing, status-store rules). Restart the
  driver after any change to either file.

Restarting:

```bash
tmux attach -t scheduler   # Ctrl-C — this also stops the orchestrator it launched
rmdir "$LOG_DIR/.lock" 2>/dev/null   # only if it was SIGKILLed, not on a clean Ctrl-C
# then start it again, as above
```

(`$LOG_DIR` here is whatever you started the driver with — the same value
from "Starting the driver" above, not necessarily the built-in default.)

## Attribution

Everyone on the cluster typically shares one unix account, so `sched-ctl.sh`
cannot tell operators apart on its own. Pass `--who YOUR_NAME` (or set
`$SCHED_WHO`) so the audit log (`$LOG_DIR/audit.log`) and cancellation notes
say who did what. The `model-launcher` Python client does this automatically,
reading your own machine's local username.
