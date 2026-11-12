"""Start VibeNode's recovery helper (the "Start VibeNode" page).

This file stays at the top of the folder on purpose: boot-time tasks and
sign-in entries of every installed copy run this path, so it must never move.
The helper's code lives in src/launcher/reviver.py; this runs it unchanged."""
import runpy
import sys
from pathlib import Path

_entry = Path(__file__).resolve().parent / "src" / "launcher" / "reviver.py"
sys.argv[0] = str(_entry)
runpy.run_path(str(_entry), run_name="__main__")
