"""
The behaviour knobs VibeNode reads from the environment, in one place.

By 2026-10-23 the Session Engine and the web server read about forty
``VIBENODE_*`` variables, most of them kill switches and thresholds for the
recovery machinery (stall watchdog, orphan recovery, silent-wait, usage-limit
auto-resume and auto-switch, API retry, the runaway reaper, Durable Jobs).
None of them had a UI and most were not in the README, so the only way to know
what the app would do was to grep the source. This table drives the read-only
"Recovery & Watchdog" section in System → Settings (``GET /api/engine/knobs``).

``tests/test_engine_knobs.py`` scans ``daemon/``, ``app/`` and ``run.py`` for
``VIBENODE_*`` names and fails when one is missing here, so a new knob must be
described before it ships.

Values are read from THIS process's environment. The web server and the Session
Engine are spawned by the same launcher with the same environment, so this is
what the engine saw at its start unless someone exported a variable to only one
of them; the record the engine writes (``data/engine_code.json``) is the
authority for its own code, not its environment.
"""

from __future__ import annotations

import os

# (name, group, default, description). ``default`` is a string as the code
# would read it ("" = unset means the built-in behaviour described).
KNOBS = [
    # Stall watchdog (health_monitor.py jobs 1-3)
    ("VIBENODE_STALL_MINUTES", "Stall watchdog", "10",
     "Minutes of zero output before a WORKING session counts as stalled."),
    ("VIBENODE_STALL_MAX_RESTARTS", "Stall watchdog", "2",
     "Auto-restart attempts per stall episode before the session is left for a person."),
    ("VIBENODE_KEEP_AWAKE", "Stall watchdog", "1",
     "Set to 0 to let Windows sleep while sessions are working."),
    # Orphaned workers (job 4)
    ("VIBENODE_ORPHAN_RECOVERY", "Stranded workers (job 4)", "1",
     "Set to 0 to stop nudging a session whose nested agent finished while it waited."),
    ("VIBENODE_ORPHAN_GRACE_SECONDS", "Stranded workers (job 4)", "90",
     "Seconds a session must sit idle before job 4 sends its one nudge."),
    # Silent wait (job 5)
    ("VIBENODE_SILENT_WAIT_RECOVERY", "Silent wait (job 5)", "1",
     "Set to 0 to stop the backstop for a session idling on background work that is dead."),
    ("VIBENODE_SILENT_WAIT_MINUTES", "Silent wait (job 5)", "10",
     "Minutes of total silence, with nothing running, before job 5 acts."),
    # Usage limits
    ("VIBENODE_LIMIT_AUTO_RESUME", "Usage limits", "1",
     "Set to 0 to turn off every automatic continue after a usage limit (Layer 1, including the switch)."),
    ("VIBENODE_LIMIT_RESUME_GRACE", "Usage limits", "90",
     "Seconds after the stated reset before the session continues."),
    ("VIBENODE_LIMIT_PROBE_INTERVAL", "Usage limits", "1800",
     "Seconds between probes when a limit message gave no reset time."),
    ("VIBENODE_LIMIT_AUTO_SWITCH", "Usage limits", "1",
     "Set to 0 to stop a Fable limit switching the session to the newest Opus."),
    ("VIBENODE_LIMIT_SWITCH_DELAY", "Usage limits", "5",
     "Seconds of countdown (with Cancel) before the auto-switch runs."),
    ("VIBENODE_LIMIT_BACKSTOP", "Usage limits", "1",
     "Set to 0 to disable job 6, the on-disk backstop that continues a limited session after a Session Engine restart."),
    # API retry
    ("VIBENODE_API_RETRY_MAX", "API retry", "30",
     "Maximum automatic retries of a turn that ended in an API error."),
    ("VIBENODE_API_RETRY_BASE", "API retry", "10",
     "Seconds before the first retry."),
    ("VIBENODE_API_RETRY_FACTOR", "API retry", "2",
     "Backoff multiplier between retries."),
    ("VIBENODE_API_RETRY_CAP", "API retry", "1800",
     "Longest wait between retries, in seconds."),
    ("VIBENODE_API_RETRY_JITTER", "API retry", "0.2",
     "Random jitter applied to each wait (0.2 = plus or minus 20%)."),
    ("VIBENODE_API_RETRY_TICK", "API retry", "30",
     "Seconds between checks of armed retry timers."),
    ("VIBENODE_API_RETRY_CONTINUE_PROMPT", "API retry", "Continue from where you left off.",
     "The prompt sent when a retry has to continue a turn whose user message was trimmed from memory."),
    # Wake-ups
    ("VIBENODE_WAKEUP_QUEUE_STARVATION_TIMEOUT", "Wake-ups", "120",
     "Seconds a queued message may wait behind a pending scheduled wake-up before it is dispatched anyway."),
    ("VIBENODE_WAKEUP_MAX_WAIT", "Wake-ups", "1800",
     "Longest a session is held for a scheduled wake-up before it is treated as missed."),
    # Runaway reaper
    ("VIBENODE_REAP_DISABLED", "Runaway reaper", "",
     "Set to 1 to disable the reaper that kills runaway helper processes (ugrep by default)."),
    ("VIBENODE_REAP_COMMS", "Runaway reaper", "ugrep",
     "Comma-separated process names the reaper watches."),
    ("VIBENODE_REAP_CPU_SECONDS", "Runaway reaper", "120",
     "CPU-seconds a watched process may burn before it is killed."),
    ("VIBENODE_REAP_INTERVAL_SECONDS", "Runaway reaper", "60",
     "Seconds between reaper sweeps."),
    # Durable Jobs
    ("VIBENODE_DURABLE_JOBS", "Durable Jobs", "1",
     "Set to 0 to stop the supervisor that relaunches long jobs after a reboot."),
    ("VIBENODE_JOBS_FILE", "Durable Jobs", "",
     "Path of the job registry; default data/jobs.json."),
    # Engine code
    ("VIBENODE_SESSION_DURABLE", "Session Engine", "1",
     "Set to 0 to stop the engine writing per-session durable state (armed retries, allow-lists, aliases, usage) to data/session_durable/ and restoring it after a restart."),
    ("VIBENODE_SESSION_HOST", "Session Engine", "1",
     "Each new session's CLI runs inside its own Session Host process, so a Session Engine restart reattaches the session (turn in flight, pending permission, timers) instead of resuming it. Costs about 0.2 s on a session's first connect and about 36 MB per live session. Set to 0 to run new CLIs inside the engine as before; hosts already running are reattached whatever this says."),
    ("VIBENODE_HOST_ORPHAN_MINUTES", "Session Engine", "30",
     "Minutes a Session Host waits with no Session Engine attached and no turn in flight before it closes its CLI and exits. A host mid-turn never exits on this timer. Fractions allowed; the floor is 1 second."),
    ("VIBENODE_HOST_FORBID_REAL_CLI", "Tests", "",
     "Set by the test suite: a Session Host refuses to launch the real Claude CLI (tests pass a fake one). Not for manual use."),
    ("VIBENODE_HOST_TEST_OWNER", "Tests", "",
     "Set by the test suite to <pid>:<create time> of the pytest process; a Session Host shuts down once that process is gone, so a stopped test run leaves no host behind. Not for manual use."),
    ("VIBENODE_SMART_TERMINATION_ENGINE", "Session Engine", "",
     "Set by the Session Engine in the environment of each CLI it starts with its own Smart termination check, so the settings-file copy (daemon/smart_termination_hook.py) stays out of the way. Not for manual use; the on/off switch is the Smart termination preference."),
    ("VIBENODE_WATCHDOG_HOT_RELOAD", "Session Engine", "1",
     "Set to 0 to stop the watchdog reloading its own jobs when their files change on disk."),
    ("VIBENODE_SDK_MAX_BUFFER_MB", "Session Engine", "",
     "Override, in megabytes, of the SDK's JSON stream buffer limit (patch 5)."),
    ("VIBENODE_TIMING_TTFT", "Session Engine", "",
     "Set to 1 to log time-to-first-token instrumentation in the engine and the socket layer."),
    ("VIBENODE_PRESERVE_DAEMON", "Session Engine", "",
     "Set by the web-only restart path so the launcher does not kill the running engine. Not for manual use."),
    ("VIBENODE_DAEMON_PATH_AUGMENTED", "Session Engine", "",
     "Internal marker that the engine's PATH was already extended. Not for manual use."),
    # Ports and paths
    ("VIBENODE_WEB_PORT", "Ports and paths", "5050", "Web server port."),
    ("VIBENODE_DAEMON_PORT", "Ports and paths", "5051", "Session Engine IPC port."),
    ("VIBENODE_REVIVER_PORT", "Ports and paths", "5052", "Control port of the reviver process."),
    ("VIBENODE_TEST_PORT", "Ports and paths", "",
     "Start a separate test instance on this port (skips the daemon spawn and the browser)."),
    ("VIBENODE_DIR", "Ports and paths", "",
     "Repository root override; default is the folder this code lives in."),
    ("VIBENODE_CONFIG", "Ports and paths", "",
     "Path of the config file; default kanban_config.json beside the repo."),
    ("VIBENODE_BOOT_STATUS_FILE", "Ports and paths", "",
     "File the launcher writes boot progress to for the splash screen."),
    # Updates
    ("VIBENODE_NO_AUTO_UPDATE", "Updates", "",
     "Set to 1 to skip the automatic Claude CLI and SDK update at launch."),
    ("VIBENODE_NO_PIP_UPDATE", "Updates", "",
     "Set to 1 to skip only the pip step of the launch update."),
    ("VIBENODE_UPDATE_ISOLATED", "Updates", "",
     "Set to 1 to force the isolated update path. Reserved."),
    # Session naming
    ("VIBENODE_AUTO_NAMER", "Session naming", "1",
     "Set to 0 to stop the web server naming new untitled sessions in the background (3 per minute)."),
    # Watchdog API
    ("VIBENODE_CUSTOMERNODE_MATCH", "Watchdog API", "customernode",
     "Substring that identifies the CustomerNode project for the pipeline watchdog routes."),
    ("VIBENODE_CUSTOMERNODE_CWD", "Watchdog API", "",
     "Explicit working directory of the CustomerNode checkout for the watchdog routes."),
    # Tests
    ("VIBENODE_TEST_NO_SANDBOX", "Tests", "",
     "Set to 1 to run the test suite without its PID-namespace sandbox (Linux)."),
]

_INTERNAL = {"VIBENODE_PRESERVE_DAEMON", "VIBENODE_DAEMON_PATH_AUGMENTED",
             "VIBENODE_HOST_FORBID_REAL_CLI", "VIBENODE_HOST_TEST_OWNER",
             # Engine → settings-hook handshake (daemon/smart_termination_hook.py)
             "VIBENODE_SMART_TERMINATION_ENGINE"}


def names() -> set:
    return {k[0] for k in KNOBS}


def snapshot(environ=None) -> list:
    """Every knob with its current value and where it came from."""
    env = os.environ if environ is None else environ
    out = []
    for name, group, default, desc in KNOBS:
        raw = env.get(name)
        if name == "_VIBENODE_DAEMON_PATH_AUGMENTED":
            raw = env.get("_" + name, raw)
        out.append({
            "name": name,
            "group": group,
            "description": desc,
            "default": default,
            "value": raw if raw is not None else default,
            "source": "env" if raw is not None else "default",
            "internal": name in _INTERNAL,
        })
    return out
