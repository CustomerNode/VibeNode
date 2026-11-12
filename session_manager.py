"""Start VibeNode.

This file stays at the top of the folder on purpose: desktop shortcuts,
sign-in entries and the recovery helper of every installed copy start
VibeNode by running this path, so it must never move. The launcher's code
lives in src/launcher/session_manager.py; this runs it unchanged."""
import runpy
import sys
from pathlib import Path

_entry = Path(__file__).resolve().parent / "src" / "launcher" / "session_manager.py"
sys.argv[0] = str(_entry)
runpy.run_path(str(_entry), run_name="__main__")
