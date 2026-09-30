#!/bin/bash
# =============================================================================
# Sourced FIRST by every entry script: a bash >= 4 and the tools it needs.
# =============================================================================
# The scheduler needs bash 4 (associative arrays hold status.tsv; `${x,,}`
# resolves library aliases). macOS ships bash 3.2 as /bin/bash, and 3.2 does not
# fail on that code — it silently collapses every status key into one row. So a
# script started by 3.2 (its #!/bin/bash shebang, `bash sched-ctl.sh` over SSH,
# a launcher) re-runs itself here under a newer bash, or stops with a reason.
#
# macOS also puts Homebrew (bash, flock) on PATH only for login shells; an SSH
# command or a LaunchAgent gets /usr/bin:/bin. Those directories are added here.
# On Linux this file changes nothing.
#
# Everything below must itself run on bash 3.2.
# =============================================================================

if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
    for _floor_dir in /usr/local/bin /opt/homebrew/bin; do
        case ":${PATH}:" in
            *":${_floor_dir}:"*) ;;
            *) [ -d "$_floor_dir" ] && PATH="${_floor_dir}:${PATH}" ;;
        esac
    done
    export PATH
    unset _floor_dir
fi

if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
    for _floor_bash in /opt/homebrew/bin/bash /usr/local/bin/bash "$(command -v bash 2>/dev/null)"; do
        [ -n "$_floor_bash" ] && [ -x "$_floor_bash" ] || continue
        if [ "$("$_floor_bash" -c 'echo "${BASH_VERSINFO[0]}"' 2>/dev/null)" -ge 4 ] 2>/dev/null; then
            exec "$_floor_bash" "${BASH_SOURCE[1]}" ${1+"$@"}
        fi
    done
    echo "ERROR: $(basename "${BASH_SOURCE[1]:-$0}") needs bash 4 or newer; this is bash ${BASH_VERSION}." >&2
    echo "       On macOS: brew install bash" >&2
    exit 1
fi
