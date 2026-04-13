#!/usr/bin/env bash
# VibeNode launcher for macOS and Linux
cd "$(dirname "$0")"

# Find a working Python 3 interpreter
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo "Error: Python 3 is not installed or not on PATH."
    read -p "Press Enter to close..."
    exit 1
fi

"$PY" session_manager.py
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo "VibeNode exited with error code $EXIT_CODE"
    read -p "Press Enter to close..."
fi

exit $EXIT_CODE
