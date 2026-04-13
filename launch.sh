#!/usr/bin/env bash
# VibeNode launcher for macOS and Linux
cd "$(dirname "$0")"

# Find a working Python 3.10+ interpreter
if command -v python3 &>/dev/null; then
    if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
        PY=python3
    else
        echo "Error: python3 was found but is older than 3.10. Install Python 3.10 or later."
        read -p "Press Enter to close..."
        exit 1
    fi
elif command -v python &>/dev/null; then
    # Verify it's actually Python 3
    if "$( command -v python )" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
        PY=python
    else
        echo "Error: 'python' was found but is not Python 3.10+. Install Python 3.10 or later."
        read -p "Press Enter to close..."
        exit 1
    fi
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
