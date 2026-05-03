#!/usr/bin/env bash
# Stops VibeNode: kills the Flask web server (5050), the session daemon
# (5051), the boot splash if still running, and any orphaned subprocesses
# spawned from this VibeNode checkout.
#
# WARNING: this is a HARD STOP. Any active Claude sessions or agents
# running through the daemon will be terminated. Use the in-app
# "System → Restart Server → Web Only" option instead if you just want
# to reload the UI without dropping sessions.
#
# Linux/macOS only. The Windows path uses a different launcher.

set -uo pipefail   # NOT -e; we want to keep going past already-dead procs

cd "$(dirname "$0")"
VIBENODE_DIR="$(pwd)"

echo "Stopping VibeNode..."

killed_any=0

# --- 1. Kill processes by port (most reliable signal) ----------------------
# 5050: Flask web server (run.py)
# 5051: session daemon (daemon/run_daemon.py)
for port in 5050 5051; do
    if command -v fuser >/dev/null 2>&1; then
        pids="$(fuser "${port}/tcp" 2>/dev/null | tr -s ' ' || true)"
    elif command -v lsof >/dev/null 2>&1; then
        pids="$(lsof -ti tcp:"$port" 2>/dev/null | xargs || true)"
    else
        pids=""
    fi
    if [[ -n "$pids" ]]; then
        echo "  killing port $port (PIDs: $pids)"
        # shellcheck disable=SC2086
        kill $pids 2>/dev/null || true
        sleep 0.3
        # shellcheck disable=SC2086
        kill -9 $pids 2>/dev/null || true
        killed_any=1
    fi
done

# --- 2. Pattern-kill anything that escaped ---------------------------------
# Match against this specific VibeNode directory so we never kill another
# user's VibeNode checkout, or unrelated python processes that happen to be
# running session_manager.py or run.py from somewhere else.
for pat in \
    "${VIBENODE_DIR}/session_manager.py" \
    "${VIBENODE_DIR}/run.py" \
    "${VIBENODE_DIR}/daemon/run_daemon.py" \
    "${VIBENODE_DIR}/app/boot_splash.py"
do
    if pkill -f "$pat" 2>/dev/null; then
        echo "  killed pattern: $pat"
        killed_any=1
    fi
done

# --- 3. Close any auth-login terminals VibeNode spawned --------------------
# auth_api.py opens a terminal window for `claude login` on Linux/macOS.
# It tags the window so we can find it.
if command -v xdotool >/dev/null 2>&1; then
    closed=0
    for needle in "VibeNode auth login" "claude login"; do
        for wid in $(xdotool search --name "$needle" 2>/dev/null); do
            xdotool windowclose "$wid" 2>/dev/null && closed=$((closed + 1))
        done
    done
    if (( closed > 0 )); then
        echo "  closed $closed auth-login terminal(s)"
    fi
fi

# --- 4. Clean up the boot splash status file -------------------------------
# session_manager._launch_splash() writes vibenode_boot_<pid>.status into
# /tmp; if the launcher was force-killed it can leave one behind.
rm -f /tmp/vibenode_boot_*.status 2>/dev/null || true

if (( killed_any == 0 )); then
    echo "Nothing was running."
else
    echo "VibeNode stopped."
fi
