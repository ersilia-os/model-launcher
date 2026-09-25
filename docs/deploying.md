# Deploying the scheduler

There are two sides to install:

- **Local** (your laptop): the Python client, `model-launcher`. It drives the
  scheduler over SSH.
- **Server** (the machine that runs models): the bash scheduler itself. It is
  copied there by hand, once per machine.

## Local

```bash
pip install git+https://github.com/ersilia-os/model-launcher.git
model-launcher --list-hosts           # machines you can target
model-launcher --host YOUR_ALIAS check
```

`check` finds the running scheduler on the host by itself. Pass `--ctl` only
if no driver is running and the scripts are not in `/shared/scripts/scheduler`.

## Server

### 1. Copy the scripts

Everything the server needs ships inside the package, under `remote/`:

```
remote/
├── sched-ctl.sh                  control CLI; every command goes through it
├── run-model-queue.sh            the driver; runs the queue one model at a time
├── scheduler-lib.sh              shared helpers
├── scheduler-status.sh           plain-text queue view
├── install-scheduler-service.sh  installs the driver as a systemd service
├── scheduler-service.sh          the service's start / post-stop entry point
├── start-scheduler-tmux.sh       starts the driver in tmux (machines without systemd)
├── library-aliases.sh            short library names -> full names
├── scheduler.conf.example        per-machine settings
├── example.queue                 template queue file
└── slurm/                        wave orchestrators and #SBATCH workers
    ├── submit-ersilia-waves.sh
    ├── submit-singularity-waves.sh
    ├── run-ersilia-wave-job.sh
    └── run-singularity-wave-job.sh
```

From your laptop (`$SRC`, `$DEST` and `$H` are local variables, so keep to
one terminal session):

```bash
H=YOUR_ALIAS
DEST=/shared/scripts/scheduler
SRC="$(python3 -c 'from model_launcher.core.remote import remote_dir; print(remote_dir())')"

rsync -av --exclude '__pycache__' "$SRC"/ "$H:$DEST"/
ssh $H "chmod +x $DEST/*.sh $DEST/slurm/*.sh"
```

Use `rsync`, not `cp` or `scp`: it replaces files without disturbing a
driver that is already running.

Check that the copy is complete. Run these **on the server**; the expected
counts are on the right:

```bash
ssh $H
DEST=/shared/scripts/scheduler
grep -c 'flock -n 8' "$DEST/run-model-queue.sh"                 # 1
grep -c log_submitted_ids "$DEST/scheduler-service.sh"          # 1
grep -c audit_log "$DEST/sched-ctl.sh"                          # 15
grep -c 'unset ST_STATUS' "$DEST/scheduler-lib.sh"              # 1
```

### 2. Configure (first time only)

```bash
ssh $H "cp $DEST/scheduler.conf.example $DEST/scheduler.conf"
ssh $H "cp $DEST/example.queue $DEST/models.queue"
```

Edit `scheduler.conf` if the defaults (`LOG_DIR=/shared/logs/scheduler`,
`S3_BUCKET`, `SIF_DIR`, …) don't fit the machine. Keep every line in the
`VAR="${VAR:-value}"` form. Edit `models.queue`, or leave it empty and add
models later from the dashboard.

### 3. Start the driver

**With systemd** (recommended; it restarts after crashes and reboots):

```bash
ssh $H "cd $DEST && ./install-scheduler-service.sh --print models.queue DEFAULT_LIBRARY"   # review
ssh -t $H "cd $DEST && ./install-scheduler-service.sh models.queue DEFAULT_LIBRARY"        # install + start
```

`DEFAULT_LIBRARY` must match a folder under `s3://YOUR_BUCKET/input/`
exactly. Run the installer from a shell where `squeue` works: it copies that
`PATH` into the service.

| On the server | |
|---|---|
| `sudo systemctl status ersilia-scheduler` | Is it running |
| `sudo systemctl restart ersilia-scheduler` | After a redeploy |
| `sudo systemctl stop ersilia-scheduler` | Stop (stays stopped) |
| `sudo journalctl -u ersilia-scheduler` | Starts, crashes, restarts |

The driver's own log is `$LOG_DIR/driver.log`.

**Without systemd:**

```bash
ssh $H "cd $DEST && ./start-scheduler-tmux.sh models.queue DEFAULT_LIBRARY"
```

### 4. Confirm

```bash
model-launcher --host $H check
```

It should show `driver RUNNING` and your queue.

## Redeploying

Repeat step 1, then restart the driver. `sched-ctl.sh` changes apply
immediately. Changes to the driver scripts need a restart. Changes to
`install-scheduler-service.sh` need the installer rerun, then a restart.

**Switching an existing tmux driver to the service:** stop it first with
`ssh $H "$DEST/sched-ctl.sh shutdown"`, which also cancels a running model,
so do it while the queue is idle. Then run step 3.
