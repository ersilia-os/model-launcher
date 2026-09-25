# Running Ersilia models over large chemical libraries

Queue up Ersilia models, run them one at a time over a chemical library, and
watch progress from a terminal dashboard. Jobs are dispatched to an AWS
ParallelCluster through SLURM; progress is the number of result files in S3, so
every run resumes where it left off.

The package is a thin client. All scheduling logic is the bash layer under
`src/model_launcher/remote/`, which runs on the target machine — the Python side
only calls `sched-ctl.sh` and parses the snapshot it prints. The two ship
together so a client can never talk to a scheduler that lacks the feature it is
asking for.

## Installation

```bash
conda create -n model-launcher python=3.12
conda activate model-launcher
pip install git+https://github.com/ersilia-os/model-launcher.git
```

## Quick start

```bash
model-launcher --list-hosts                 # which machines can I reach?
model-launcher --host ai2050cluster check   # is that one reachable?
model-launcher --host ai2050cluster         # open the dashboard
```

`--host` is any alias `ssh` can resolve — a `~/.ssh/config` entry, or a device
on the Ersilia tailnet reachable over Tailscale SSH. `--list-hosts` only shows
tailnet machines you actually have a login on: your own devices, plus any
tagged as shared team infrastructure (`tag:dev`). With no `--host`, everything
runs on the local machine.

## Commands

| Command | What it does |
|---|---|
| `model-launcher` | Open the dashboard (same as `tui`) |
| `model-launcher tui` | Watch and steer the queue full-screen |
| `model-launcher check` | Print transport, driver state and per-job progress, then exit |
| `model-launcher --list-hosts` | List the machines available as `--host` targets (SSH config and tailnet) |

Run `--help` on any command for its options.

## The queue

One job per line; blank lines and `#` comments are ignored. Line order **is**
priority, and the driver re-reads the file before every job, so it can be edited
while a run is in flight.

```
<model_id>  <mode>  [library]  [wave_size]  [queue]  [flags]
```

`mode` is `ersilia` or `singularity`. Flags are `hold` (park the job) and
`cpus=N` (override the per-task CPU count), recognised anywhere after the model
id. Omitting `cpus` leaves the worker's own `#SBATCH` default in charge.

## Deploying the scheduler to a machine

The Python package only gets you the dashboard — the scheduler itself is bash
that has to be copied onto the target machine separately. See
[`docs/deploying.md`](docs/deploying.md).

## Attribution

Everyone on a cluster typically shares one unix account, so the scheduler
cannot tell operators apart on its own. The client reads your own machine's
local username and passes it along automatically; `$LOG_DIR/audit.log` records
who ran every mutating command, and cancellation notes say who asked.

## Development

```bash
pip install -e ".[dev]"
pytest          # 154 tests, no cluster and no AWS credentials needed
ruff check . && ruff format .
```

The test suite drives the real bash scheduler against a fake S3 fixture
(`SCHED_FAKE_S3`) and a fake orchestrator (`--dry-run`), with recording stubs for
`aws`, `sbatch`, `squeue` and `scancel` on `PATH`. `tests/test_invariants.py`
pins the fourteen invariants documented in the original `HANDOFF.md`, each of
which was written after a production bug — read it before changing the bash.

## About the Ersilia Open Source Initiative

The [Ersilia Open Source Initiative](https://ersilia.io) is a tech-nonprofit organization fueling sustainable research in the Global South. Ersilia's main asset is the [Ersilia Model Hub](https://github.com/ersilia-os/ersilia), an open-source repository of AI/ML models for antimicrobial drug discovery.

![Ersilia Logo](assets/Ersilia_Brand.png)
