#!/usr/bin/env bash
# Install the leak-detector systemd USER timer on a Linux dev box.
#
#   bash scripts/systemd/install.sh           # install + enable + start
#   bash scripts/systemd/install.sh --status  # show what is installed
#   bash scripts/systemd/install.sh --remove  # disable + remove
#
# Per-user units: no sudo, nothing system-wide. The detector is REPORT-ONLY
# (see scripts/leak_detector.py); it never kills anything.
#
# WHY: sessions spawn processes that can outlive the command that started them.
# One leaked batch of CPU burners once sat on a dev box for 20 hours before
# anyone noticed. This makes "something leaked" visible in minutes.
#
# Linux/systemd only. Everywhere else it exits 0 with a notice -- the detector
# still works when run by hand, it is just not scheduled. (macOS equivalent
# would be a launchd agent; Windows, a Scheduled Task.)

set -uo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
UNIT_SRC="$REPO_ROOT/scripts/systemd"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNITS=(vibenode-leak-detector.service vibenode-leak-detector.timer)
TIMER=vibenode-leak-detector.timer

if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "[install] no reachable systemd user bus -- skipping (not an error)."
    echo "[install] run it by hand instead: python3 scripts/leak_detector.py"
    exit 0
fi

case "${1:-}" in
    --status)
        systemctl --user list-timers "$TIMER" --all --no-pager || true
        exit 0
        ;;
    --remove)
        systemctl --user disable --now "$TIMER" >/dev/null 2>&1 || true
        for u in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$u"; done
        systemctl --user daemon-reload
        echo "[install] removed."
        exit 0
        ;;
esac

# The detector needs psutil. Prefer an interpreter that actually has it rather
# than baking in a guess -- a unit that cannot import psutil exits 2 and is
# useless, and silently scheduling that is worse than not scheduling at all.
PYTHON=""
for candidate in "${VIBENODE_PYTHON:-}" "$(command -v python3 || true)" /usr/bin/python3; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    if "$candidate" -c "import psutil" >/dev/null 2>&1; then PYTHON="$candidate"; break; fi
done
if [[ -z "$PYTHON" ]]; then
    echo "[install] ERROR: no python3 with psutil found. Install psutil, or set" >&2
    echo "[install]        VIBENODE_PYTHON=/path/to/python, then re-run." >&2
    exit 1
fi

mkdir -p "$UNIT_DIR"
for u in "${UNITS[@]}"; do
    # Placeholders are substituted so the unit works from any checkout path and
    # any interpreter -- nothing personal or machine-specific is committed.
    sed -e "s|__REPO_ROOT__|$REPO_ROOT|g" -e "s|__PYTHON__|$PYTHON|g" \
        "$UNIT_SRC/$u" > "$UNIT_DIR/$u"
done

systemctl --user daemon-reload
systemctl --user enable --now "$TIMER"

echo "[install] installed to $UNIT_DIR (python: $PYTHON)"
systemctl --user list-timers "$TIMER" --no-pager
