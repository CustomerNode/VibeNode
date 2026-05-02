#!/usr/bin/env bash
# VibeNode launcher for macOS and Linux
# Handles: Python version check, virtualenv activation, package install,
#          claude CLI check, then hands off to session_manager.py.

cd "$(dirname "$0")"
VIBENODE_DIR="$(pwd)"

# ── Python 3.10+ detection ──────────────────────────────────────────────────

find_python() {
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
                printf '%s' "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

if ! PY="$(find_python)"; then
    echo ""
    echo "Error: Python 3.10+ is not installed or not on PATH."
    echo ""
    echo "Install it with your package manager:"
    echo "  Ubuntu/Debian:  sudo apt install python3 python3-pip python3-tk"
    echo "  Fedora/RHEL:    sudo dnf install python3 python3-pip python3-tkinter"
    echo "  Arch Linux:     sudo pacman -S python python-pip tk"
    echo "  macOS (Homebrew): brew install python@3.12"
    echo ""
    echo "Or download from: https://www.python.org/downloads/"
    echo ""
    read -rp "Press Enter to close..."
    exit 1
fi

PY_VER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"

# ── Virtual environment detection ───────────────────────────────────────────
# Activate an existing venv if found — never create one automatically.

VENV_ACTIVE=0
for VENV_DIR in ".venv" "venv"; do
    if [ -f "$VENV_DIR/bin/activate" ]; then
        # shellcheck disable=SC1090,SC1091
        source "$VENV_DIR/bin/activate"
        PY="$VENV_DIR/bin/python"
        VENV_ACTIVE=1
        echo "  Using virtual environment: $VENV_DIR"
        break
    fi
done

# ── Python package check ────────────────────────────────────────────────────
# If flask isn't importable, run pip install before launching.
# This covers: fresh clones, git pulls that added new dependencies,
# and systems where pip installs land in user-site (not on PATH).

if ! "$PY" -c "import flask" 2>/dev/null; then
    echo "  Installing Python dependencies..."
    # Try plain install first (works in venvs and older distros).
    # Fall back to --user (no-venv, older pip), then --break-system-packages
    # (Ubuntu 23.04+ / Debian 12+ enforce PEP 668 which blocks plain installs).
    if "$PY" -m pip install --quiet -r requirements.txt 2>/dev/null; then
        echo "  Dependencies installed."
    elif "$PY" -m pip install --quiet --user -r requirements.txt 2>/dev/null; then
        echo "  Dependencies installed (user-local)."
    elif "$PY" -m pip install --quiet --break-system-packages -r requirements.txt 2>/dev/null; then
        echo "  Dependencies installed (system-packages override)."
    else
        echo ""
        echo "Error: Could not install required packages."
        echo ""
        echo "Recommended: use a virtual environment:"
        echo "  $PY -m venv .venv"
        echo "  source .venv/bin/activate"
        echo "  pip install -r requirements.txt"
        echo "  ./launch.sh"
        echo ""
        echo "Or with user-local install:"
        echo "  $PY -m pip install --user -r requirements.txt"
        echo ""
        read -rp "Press Enter to close..."
        exit 1
    fi
fi

# ── Claude CLI check ────────────────────────────────────────────────────────
# Sessions require the 'claude' CLI. Warn clearly if it's missing rather
# than letting VibeNode open with a confusing 'sessions won't work' state.

if ! command -v claude &>/dev/null; then
    echo ""
    echo "  Warning: 'claude' CLI not found on PATH."
    echo "  VibeNode needs Claude Code to run sessions."
    echo ""
    echo "  Install Claude Code: https://docs.anthropic.com/en/docs/claude-code"
    echo "  Then run VibeNode again."
    echo ""
    echo "  (Continuing anyway — the UI will open but sessions won't start)"
    echo ""
fi

# ── tkinter availability note (Linux only) ─────────────────────────────────
# The boot splash uses tkinter. If it's missing the splash is silently
# skipped — VibeNode still starts, just without the animated loading screen.
# We only print this note so the user isn't confused by a plain terminal
# with no visible progress indicator.

if [ "$(uname -s)" = "Linux" ]; then
    if ! "$PY" -c "import tkinter" 2>/dev/null; then
        echo "  Note: tkinter not installed — boot splash will be skipped."
        echo "  For the animated startup screen:"
        echo "    Ubuntu/Debian: sudo apt install python3-tk"
        echo "    Fedora/RHEL:   sudo dnf install python3-tkinter"
        echo "    Arch Linux:    sudo pacman -S tk"
        echo ""
    fi
fi

# ── Launch ──────────────────────────────────────────────────────────────────

"$PY" session_manager.py
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo "VibeNode exited with error code $EXIT_CODE"
    echo "Check logs/web_server.log and logs/daemon_debug.log for details."
    read -rp "Press Enter to close..."
fi

exit $EXIT_CODE
