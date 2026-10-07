"""Backstop for stalls of ANY cause: an IDLE session waiting on dead work.

Why this exists
---------------
Every stall fix before this one recognised one specific cause: a WORKING turn
with no output, a phantom wake-up, a starved queue, a nested agent's result
misrouted to the top-level session (``orphaned_workers.py``).  Each new cause
got through until someone diagnosed it, and the user lost the hours in
between.  This module recognises the symptom instead: the session ended its
turn saying it is waiting on background work it launched, and nothing that
could ever wake it is alive.

What the full local session history showed (2,082 turn endings, 2026-10-07):

* 116 turns ended waiting on background work the session itself launched.
  100 resolved normally, when the work reported back.
* Every slow-but-normal wait (up to 72 minutes with no agent writing anything)
  had a background command still running.
* Waiting wording alone is useless: most "waiting" is waiting on the USER
  ("Waiting on your pick", "say the word").  Silence alone false-alarms on
  quiet long commands.  Liveness is what separates a slow wait from a dead
  one.

Signals (none of them names a cause)
------------------------------------
1. Wording.  The final message says it is waiting on background work, not on
   the user (:func:`says_waiting_on_background`).
2. Structure.  The daemon saw background work in the current task: a
   background launch, or a CLI task event, since the last genuine send.
   session_manager records this as ``_bg_work_at`` / ``_task_started_at``.
3. Liveness.  The newest write anywhere the session's background work writes
   (:func:`footprint_last_write`), plus any background command still running
   under the session's CLI process (:func:`live_commands`).

HealthMonitor job 5 combines these with the thresholds and the act-once rules.
"""

import os
import re
import tempfile
from pathlib import Path
from typing import Optional

# ── Wording ───────────────────────────────────────────────────────────────
# "Waiting on background work", deliberately NOT a bare "wait": the history
# scan showed most waiting is on the user.  Three shapes:
#   * "waiting on its final report", "still waiting for the reviewer"
#     (never "waiting for the user / you / your go-ahead ...");
#   * "still running", "in the background";
#   * "once the suite finishes", "when they report", "after both complete".
_NOT_BACKGROUND = (
    r"(?:the |a |your )?(?:user|you|your|human|go-ahead|approval|"
    r"confirmation|decision|answer|reply|response|input|call|pick)\b"
)
_WAIT_BG = re.compile(
    r"\b(?:still )?waiting (?:on|for) (?!" + _NOT_BACKGROUND + r")"
    r"(?:its|their|the|a|my|both|all|each|those|these|that|this|it|them)\b"
    r"|\bstill (?:running|in progress|executing)\b"
    r"|\bin the background\b"
    r"|\b(?:once|when|after) (?:it|they|both|all|each|those|these|"
    r"the [\w-]+(?: [\w-]+)?) (?:finish|finishes|report|reports|complete|"
    r"completes|return|returns|land|lands|come back|comes back|is done|are done)\b",
    re.IGNORECASE,
)


def says_waiting_on_background(text: str) -> bool:
    """True if ``text`` says the session is waiting on background work."""
    return bool(_WAIT_BG.search(text or ""))


# ── Footprint ─────────────────────────────────────────────────────────────

def task_output_dir(root_transcript) -> Path:
    """Where the CLI writes this session's background-task output files.

    ``<tempdir>/claude/<project dir>/<session id>/tasks``, the directory of
    every ``<output-file>`` path in the CLI's task notifications.
    """
    root = Path(root_transcript)
    return (Path(tempfile.gettempdir()) / "claude" / root.parent.name
            / root.stem / "tasks")


def footprint_last_write(root_transcript) -> float:
    """Newest mtime across everything the session's background work writes.

    That is the root transcript, every agent transcript and meta file under
    ``<sid>/subagents``, and every background-task output file.  A running
    agent appends to its transcript on every step, and a background command
    appends to its output file as it prints.  Returns 0.0 if nothing exists.
    """
    root = Path(root_transcript)
    newest = 0.0
    try:
        newest = os.stat(root).st_mtime
    except OSError:
        pass
    for directory in (root.parent / root.stem / "subagents", task_output_dir(root)):
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    try:
                        if entry.is_file():
                            newest = max(newest, entry.stat().st_mtime)
                    except OSError:
                        continue
        except OSError:
            continue
    return newest


# ── Live background commands ──────────────────────────────────────────────
# Background commands (Bash / PowerShell with run_in_background) run inside a
# shell spawned by the CLI.  Verified 2026-10-07 on Windows: a background
# ``sleep`` shows up as bash.exe descended from claude.exe.  So does a
# subagent's long foreground command, which is correctly "alive" too.
_SHELL_NAMES = frozenset({
    "bash", "sh", "zsh", "dash", "fish", "ksh", "pwsh", "powershell", "cmd",
})
# Shells the CLI starts with itself (MCP servers launched via ``cmd /c npx``,
# startup hooks) are not task work.  Only shells started this long after the
# CLI count.
_CLI_STARTUP_GRACE_SECONDS = 60.0
# The PID must still belong to a Claude CLI.  A recycled PID would otherwise
# report some unrelated process's children as alive.
_CLI_NAME_HINTS = ("claude", "node", "bun")


def live_commands(cli_pid) -> Optional[list]:
    """Background commands still running under the session's CLI process.

    Returns a list of ``"name[pid]"`` labels (empty when nothing is running),
    or None when liveness cannot be determined: psutil is missing, there is no
    PID, or the PID is gone or no longer a Claude CLI.  Callers must treat
    None as "unknown", not "dead".
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        pid = int(cli_pid or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        cli = psutil.Process(pid)
        if not any(h in cli.name().lower() for h in _CLI_NAME_HINTS):
            return None
        started = cli.create_time()
        children = cli.children(recursive=True)
    except psutil.Error:
        return None
    alive = []
    for proc in children:
        try:
            name = proc.name().lower()
            if name.endswith(".exe"):
                name = name[:-4]
            if name not in _SHELL_NAMES:
                continue
            if proc.create_time() < started + _CLI_STARTUP_GRACE_SECONDS:
                continue
            alive.append(f"{name}[{proc.pid}]")
        except psutil.Error:
            continue
    return alive


# ── Messages ──────────────────────────────────────────────────────────────

def build_nudge(quiet_minutes: int, live: Optional[list] = None) -> str:
    """The one message sent to a session found waiting on dead work."""
    if live:
        state = (
            f"background commands are still running ({', '.join(live[:5])}) "
            f"but nothing in this session has written any output for "
            f"{quiet_minutes} minutes"
        )
    else:
        state = (
            f"nothing in this session has run for {quiet_minutes} minutes: no "
            "agent has written anything, no background command is running, "
            "and no wake-up is scheduled"
        )
    return (
        "[VibeNode watchdog] You ended your turn waiting on background work, "
        f"but {state}. Whatever you are waiting for may never arrive on its "
        "own. Check each thing you are waiting on yourself (read its output "
        "file, check whether its process is alive, or SendMessage the agent), "
        "then either finish the work or tell the user exactly what is blocked "
        "and why. Do not end your turn waiting again unless something is "
        "actually still running."
    )
