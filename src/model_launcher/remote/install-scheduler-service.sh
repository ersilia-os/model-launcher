#!/bin/bash
# =============================================================================
# Install the scheduler driver as a service, so it starts by itself and is
# restarted after a crash: a systemd unit on Linux, a LaunchAgent on macOS. The
# service counterpart of start-scheduler-tmux.sh, taking the same arguments.
# =============================================================================
# macOS: a per-user LaunchAgent in ~/Library/LaunchAgents, loaded with launchctl
# and no sudo. It runs while you are logged in, which Docker Desktop needs
# anyway. A Mac only runs models with ersilia, so it needs DISPATCH=serve.
#
# Linux — why a SYSTEM unit (installed with sudo, running as you) and not a user unit:
# the AWS head node runs systemd 219 (Amazon Linux 2), where `systemctl --user`
# has no D-Bus session at all. Every setting below works on 219.
#
# Usage:
#   install-scheduler-service.sh <queue_file> [default_library] [default_wave_size]
#                                [default_queue] [--dry-run]
#   install-scheduler-service.sh --print <same args>   # render the unit, install nothing
#
# Env forwarded into the unit when set: S3_BUCKET POLL_SECONDS ON_FAIL
# AUTO_FETCH_SIF STATE_FILE DISPATCH DATA_DIR ERSILIA_BIN. LOG_DIR is always
# written, resolved the same way the driver resolves it. PATH is captured from
# THIS shell: run it from a login shell where `squeue` works (or, with
# DISPATCH=serve, where `ersilia` works), because the unit inherits nothing else.
#
# Rerun it to change the arguments (then restart the service). Rerun it after a
# head-node replacement: the unit lives in /etc on the root disk, not on /shared.
# =============================================================================

set -uo pipefail

# A bash >= 4 and, on macOS, Homebrew's tools, before anything else runs.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bash-floor.sh" || exit 1

UNIT="${SCHEDULER_UNIT:-ersilia-scheduler}"
AGENT="${SCHEDULER_AGENT:-io.ersilia.model-launcher}"
# SCHED_SERVICE_OS (tests) renders another OS's service with --print.
OS="${SCHED_SERVICE_OS:-$(uname -s)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() { sed -n '12,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-1}"; }
die() { echo "ERROR: $*" >&2; exit 1; }

PRINT=0
ARGS=()
for a in "$@"; do
    case "$a" in
        --print)   PRINT=1 ;;
        -h|--help) usage 0 ;;
        *)         ARGS+=("$a") ;;
    esac
done
[ "${#ARGS[@]}" -ge 1 ] || usage

# systemd splits ExecStart= on whitespace and expands % and $ itself; refuse
# anything it would reinterpret rather than attempt to escape it.
safe() {  # $1 = what, $2 = value
    case "$2" in
        *[[:space:]%\$\\\"\']*) die "$1 contains a space, quote, %, \$ or backslash: $2" ;;
    esac
}

QUEUE_FILE="${ARGS[0]}"
[ -f "$QUEUE_FILE" ] || die "queue file not found: $QUEUE_FILE"
ARGS[0]="$(cd "$(dirname "$QUEUE_FILE")" && pwd)/$(basename "$QUEUE_FILE")"
for a in "${ARGS[@]}"; do safe "argument" "$a"; done
safe "script directory" "$SCRIPT_DIR"

# Resolve LOG_DIR exactly as the driver will: environment > scheduler.conf > default.
# Beside the scripts; else, for a pip-installed copy (whose folder `pip install -U`
# replaces), ~/.config/model-launcher/scheduler.conf. An explicit SCHEDULER_CONF wins.
if [ -z "${SCHEDULER_CONF:-}" ]; then
    SCHEDULER_CONF="${SCRIPT_DIR}/scheduler.conf"
    [ -f "$SCHEDULER_CONF" ] || SCHEDULER_CONF="${HOME:-}/.config/model-launcher/scheduler.conf"
fi
# shellcheck source=/dev/null
[ -f "$SCHEDULER_CONF" ] && source "$SCHEDULER_CONF"
LOG_DIR="${LOG_DIR:-/shared/logs/scheduler}"
safe "LOG_DIR" "$LOG_DIR"

# A unit gets almost no PATH and never reads /etc/profile.d. Without /opt/slurm/bin
# scancel and squeue fail silently — and a failed squeue reads as "keep waiting",
# so a wave would hang forever. Capture the PATH that works here, and prove it.
# A serve machine has no SLURM: what it needs is the ersilia CLI.
# Not defaulted in place: DISPATCH is forwarded into the unit only when set.
SERVE=0
[ "${DISPATCH:-slurm}" = "serve" ] && SERVE=1
if [ "$OS" = "Darwin" ] && [ "$SERVE" -eq 0 ]; then
    die "on macOS the scheduler runs models with ersilia: set DISPATCH=serve (and DATA_DIR) in scheduler.conf."
fi
if [ "$SERVE" -eq 1 ]; then
    ERSILIA_BIN="${ERSILIA_BIN:-ersilia}"
    command -v "$ERSILIA_BIN" >/dev/null 2>&1 \
        || die "ersilia CLI '$ERSILIA_BIN' not found. Set ERSILIA_BIN, e.g. to a conda env's bin/ersilia."
    if [ "$OS" = "Darwin" ]; then
        # Docker Desktop has no docker group; what matters is that it is running.
        docker info >/dev/null 2>&1 \
            || echo "WARNING: Docker is not answering; start Docker Desktop, or ersilia cannot serve models." >&2
    else
        id -nG | tr ' ' '\n' | grep -qx docker \
            || echo "WARNING: $(id -un) is not in the docker group; ersilia may not be able to serve models." >&2
    fi
else
    for bin in sbatch squeue scancel; do
        command -v "$bin" >/dev/null 2>&1 \
            || die "'$bin' is not on PATH. Run this from a shell where SLURM commands work."
    done
fi
safe "PATH" "$PATH"

RUN_USER="$(id -un)"
RUN_GROUP="$(id -gn)"

env_lines() {
    echo "Environment=\"PATH=${PATH}\""
    echo "Environment=\"LOG_DIR=${LOG_DIR}\""
    # ersilia keeps its models and sessions under $HOME/eos, and a system unit
    # cannot be relied on to set HOME.
    if [ "$SERVE" -eq 1 ]; then
        safe "HOME" "$HOME"
        echo "Environment=\"HOME=${HOME}\""
    fi
    local var
    for var in S3_BUCKET POLL_SECONDS ON_FAIL AUTO_FETCH_SIF STATE_FILE DISPATCH DATA_DIR ERSILIA_BIN; do
        if [ -n "${!var:-}" ]; then
            safe "$var" "${!var}"
            echo "Environment=\"${var}=${!var}\""
        fi
    done
}

render() {
    cat <<EOF
# Generated by install-scheduler-service.sh — edit by rerunning it, not by hand.
[Unit]
Description=Ersilia model scheduler driver (${LOG_DIR})
Wants=network-online.target
After=network-online.target remote-fs.target slurmctld.service$([ "$SERVE" -eq 1 ] && echo " docker.service")
RequiresMountsFor=${SCRIPT_DIR} ${LOG_DIR}

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${SCRIPT_DIR}
$(env_lines)
ExecStart=${SCRIPT_DIR}/scheduler-service.sh start ${ARGS[*]}
ExecStopPost=${SCRIPT_DIR}/scheduler-service.sh stop-post

# Stop = SIGTERM to the driver ONLY, so its trap can kill the orchestrator and
# scancel the array in the right order. The default (control-group) would signal
# the orchestrator at the same moment — setsid does not escape a cgroup — and if
# it died first, the array would never be cancelled.
KillMode=mixed
# The trap runs only after the current foreground command (e.g. an S3 recount)
# returns, then spends up to ~11s on TERM->KILL before scancel. If even this is
# exceeded, ExecStopPost still cancels the array.
TimeoutStopSec=180
# The driver exits 130/143 from its INT/TERM traps. Without this a deliberate
# "systemctl stop" would count as a failure, and Restart= would undo it.
SuccessExitStatus=130 143

Restart=on-failure
RestartSec=30
# 75 = another driver holds the lock: fail once, loudly, rather than loop.
RestartPreventExitStatus=75
# systemd 219 spells these in [Service] (newer versions moved them to [Unit]).
StartLimitInterval=600
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
EOF
}

# ---- macOS: a LaunchAgent ----

xml() {  # $1 = text, escaped for a plist <string>
    # Replacements are quoted: since bash 5.2 a bare & in one means "the match".
    local t="${1//&/"&amp;"}"
    t="${t//</"&lt;"}"
    printf '%s' "${t//>/"&gt;"}"
}

render_plist() {
    local a var
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Generated by install-scheduler-service.sh — edit by rerunning it, not by hand. -->
<plist version="1.0">
<dict>
    <key>Label</key><string>$(xml "$AGENT")</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(xml "$BASH")</string>
        <string>$(xml "${SCRIPT_DIR}/scheduler-service.sh")</string>
        <string>launchd</string>
PLIST
    for a in "${ARGS[@]}"; do echo "        <string>$(xml "$a")</string>"; done
    cat <<PLIST
    </array>
    <key>WorkingDirectory</key><string>$(xml "$SCRIPT_DIR")</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>$(xml "$PATH")</string>
        <key>HOME</key><string>$(xml "$HOME")</string>
        <key>LOG_DIR</key><string>$(xml "$LOG_DIR")</string>
PLIST
    for var in S3_BUCKET POLL_SECONDS ON_FAIL AUTO_FETCH_SIF STATE_FILE DISPATCH DATA_DIR ERSILIA_BIN; do
        [ -n "${!var:-}" ] && echo "        <key>${var}</key><string>$(xml "${!var}")</string>"
    done
    cat <<PLIST
    </dict>
    <!-- Start at login, and again after a crash. scheduler-service.sh exits 0 for
         a deliberate stop and for "another driver holds the lock", so neither
         is restarted. -->
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key><false/>
    </dict>
    <key>ThrottleInterval</key><integer>30</integer>
    <!-- A stop lets the driver cancel its job and close the ersilia model. -->
    <key>ExitTimeOut</key><integer>180</integer>
    <key>StandardOutPath</key><string>$(xml "${LOG_DIR}/driver.log")</string>
    <key>StandardErrorPath</key><string>$(xml "${LOG_DIR}/driver.log")</string>
</dict>
</plist>
PLIST
}

if [ "$OS" = "Darwin" ]; then
    if [ "$PRINT" -eq 1 ]; then
        render_plist
        exit 0
    fi
    command -v launchctl >/dev/null 2>&1 || die "launchctl not found."
    DOMAIN="gui/$(id -u)"
    TARGET="${HOME}/Library/LaunchAgents/${AGENT}.plist"
    LOADED=0
    launchctl print "${DOMAIN}/${AGENT}" >/dev/null 2>&1 && LOADED=1
    if [ "$LOADED" -eq 0 ]; then
        # shellcheck source=/dev/null
        source "${SCRIPT_DIR}/scheduler-lib.sh" || die "cannot source scheduler-lib.sh"
        pid="$(driver_pid_scan)"
        if [ -n "$pid" ]; then
            die "a driver is already running for ${LOG_DIR} (pid ${pid}, probably started by hand).
       Stop it first:  ${SCRIPT_DIR}/sched-ctl.sh --log-dir ${LOG_DIR} shutdown"
        fi
    fi
    mkdir -p "${HOME}/Library/LaunchAgents" "$LOG_DIR" || die "cannot create ${HOME}/Library/LaunchAgents"
    { render_plist > "${TARGET}.tmp" && mv -f "${TARGET}.tmp" "$TARGET"; } || die "could not write $TARGET"
    if [ "$LOADED" -eq 1 ]; then
        echo "Updated ${TARGET}. The running driver keeps its old settings until you reload it:"
        echo "  launchctl bootout ${DOMAIN}/${AGENT} && launchctl bootstrap ${DOMAIN} ${TARGET}"
    else
        launchctl enable "${DOMAIN}/${AGENT}" 2>/dev/null
        launchctl bootstrap "$DOMAIN" "$TARGET" || die "launchctl bootstrap failed — see ${LOG_DIR}/driver.log"
        echo "Installed and started ${AGENT} (${TARGET})."
    fi
    echo "  status     : launchctl print ${DOMAIN}/${AGENT}"
    echo "  stop       : launchctl bootout ${DOMAIN}/${AGENT}   (starts again at next login)"
    echo "  remove     : launchctl bootout ${DOMAIN}/${AGENT}; rm ${TARGET}"
    echo "  driver log : ${LOG_DIR}/driver.log"
    exit 0
fi

# ---- Linux: a systemd unit ----

if [ "$PRINT" -eq 1 ]; then
    render
    exit 0
fi

command -v systemctl >/dev/null 2>&1 || die "systemctl not found — use start-scheduler-tmux.sh on this machine."

ACTIVE=0
systemctl is-active --quiet "$UNIT" 2>/dev/null && ACTIVE=1

# A driver started some other way (tmux, by hand) would hold the lock, and the
# service would fail its first start with exit 75. Say so up front instead.
if [ "$ACTIVE" -eq 0 ]; then
    # shellcheck source=/dev/null
    source "${SCRIPT_DIR}/scheduler-lib.sh" || die "cannot source scheduler-lib.sh"
    pid="$(driver_pid_scan)"
    if [ -n "$pid" ]; then
        die "a driver is already running for ${LOG_DIR} (pid ${pid}, probably in tmux).
       Stop it first:  ${SCRIPT_DIR}/sched-ctl.sh --log-dir ${LOG_DIR} shutdown"
    fi
fi

TARGET="/etc/systemd/system/${UNIT}.service"
tmp="$(mktemp)" || die "cannot create a temporary file"
trap 'rm -f "$tmp"' EXIT
render > "$tmp"

sudo install -m 0644 "$tmp" "$TARGET" || die "could not write $TARGET (sudo)"
sudo systemctl daemon-reload || die "systemctl daemon-reload failed"
sudo systemctl enable "$UNIT" || die "systemctl enable failed"

if [ "$ACTIVE" -eq 1 ]; then
    echo "Updated ${TARGET}. The running driver keeps its old settings until:"
    echo "  sudo systemctl restart ${UNIT}"
else
    sudo systemctl start "$UNIT" || die "systemctl start failed — see: sudo journalctl -u ${UNIT}"
    echo "Installed and started ${UNIT} (${TARGET})."
fi
echo "  status     : sudo systemctl status ${UNIT}"
echo "  stop       : sudo systemctl stop ${UNIT}      (stays stopped until started)"
echo "  unit events: sudo journalctl -u ${UNIT}"
echo "  driver log : ${LOG_DIR}/driver.log"
