#!/usr/bin/env bash
# Run a command inside a transient cgroup scope, then reap the ENTIRE cgroup.
#
# WHY
#   A child can escape its parent with setsid/nohup/daemonisation -- that is
#   exactly how 64 CPU burners survived their script on 2026-07-19 and span for
#   20 hours. Nothing escapes its cgroup. So we put the command in a scope and,
#   when it returns, kill everything still in that scope.
#
#   This is the containment layer of the orphan-leak plan. The other layers:
#     - lifetime binding at spawn  -> enforced by
#       core/testing/backend/test_background_process_lint.py
#     - report-only detection      -> infra/scripts/leak_detector.py
#
# USE IT FOR anything that backgrounds processes and might not clean up:
# repro/load scripts, benchmarks, ad-hoc experiments.
#
#   infra/scripts/run_contained.sh ./tasks/plans/.../repro7_100wide.sh
#   infra/scripts/run_contained.sh bash -c 'python3 -c "while True: pass" & sleep 5'
#
# NOTE ON PERSISTENCE: things you WANT to outlive the command (dev servers)
# must not run under this wrapper -- they will be reaped. Launch those in their
# own named unit instead, so persistence is explicit rather than accidental:
#   systemd-run --user --unit=cnode-dev-vite -- <cmd>
#
# EXIT CODE is the wrapped command's exit code, so this is drop-in transparent.
#
# PLATFORM: Linux + systemd (cgroup v2) + a reachable user bus. Anywhere else
# (Windows, macOS, cron, `ssh host cmd` without lingering) it runs the command
# UNCONTAINED and says so loudly on stderr -- degrading to today's behaviour,
# never blocking work. The Windows equivalent would be a Job Object with
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.

set -uo pipefail

if [[ $# -eq 0 ]]; then
    echo "usage: $(basename "$0") <command> [args...]" >&2
    exit 2
fi

# --- Can we actually contain? -----------------------------------------------
# `command -v systemd-run` is NOT sufficient: the binary exists in cron and in
# non-lingering SSH sessions, but `--user` needs a reachable per-user bus, and
# without one systemd-run dies with "Failed to connect to bus" AFTER we have
# committed to it -- silently never running the command at all. Probe the bus.
containable() {
    command -v systemd-run >/dev/null 2>&1 || return 1
    command -v systemctl   >/dev/null 2>&1 || return 1
    [[ -d /sys/fs/cgroup ]]                || return 1
    systemctl --user show-environment >/dev/null 2>&1 || return 1
    return 0
}

if ! containable; then
    echo "[run_contained] WARN: no reachable systemd user bus / cgroup2 here --" >&2
    echo "[run_contained] WARN: running UNCONTAINED; leaks will NOT be reaped." >&2
    exec "$@"
fi

UNIT="cnode-contained-$$-$(date +%s)"

# Reap the whole cgroup. Safe to call twice; ignores an already-gone unit.
# This is what catches processes that outlived the command, including any that
# setsid'd away from it.
reap() {
    # NB: the property is TasksCurrent. `Tasks` exists but is always empty --
    # using it made this leak report unreachable dead code.
    local leaked
    leaked=$(systemctl --user show "$UNIT.scope" -p TasksCurrent --value 2>/dev/null)
    if [[ "$leaked" =~ ^[0-9]+$ ]] && (( leaked > 0 )); then
        echo "[run_contained] reaping $leaked leaked process(es) in $UNIT.scope" >&2
    fi
    # SIGKILL the cgroup FIRST. A plain `stop` waits out TimeoutStopSec (90s by
    # default) on anything ignoring SIGTERM -- and bash defers our own EXIT trap
    # while blocked there, reproducing the very deferral bug this tool exists to
    # fix. Kill, then stop to release the unit.
    systemctl --user kill --kill-whom=all --signal=SIGKILL "$UNIT.scope" >/dev/null 2>&1 || true
    systemctl --user stop "$UNIT.scope" >/dev/null 2>&1 || true
}
trap 'reap' EXIT INT TERM HUP

# --collect drops the unit once it goes inactive, so repeated runs do not
# accumulate failed/dead scope units. TimeoutStopSec bounds the worst case if
# `stop` is ever reached before the SIGKILL lands.
systemd-run --user --scope --quiet --collect \
    -p TimeoutStopSec=5s \
    --unit="$UNIT" -- "$@"
rc=$?

# The scope stays active while ANY process remains in it -- which is precisely
# the leak signal. reap() (via the EXIT trap) kills them cgroup-wide.
exit $rc
