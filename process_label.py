"""
Make VibeNode's long-lived processes self-identify in the OS process list, so
any human — or any AI agent — hunting for resource hogs to kill sees a loud
"do not kill" warning BEFORE acting.

WHY THIS EXISTS
---------------
VibeNode runs as plain ``python`` (the web server and the session daemon) plus
one ``claude``/``node`` CLI child per active session.  None of these advertise
what they are: a process list shows anonymous ``python.exe`` and several
``claude.exe`` each eating a few hundred MB.  An automated agent under memory or
CPU pressure that sorts by RSS and kills the biggest process can take down the
DAEMON — which destroys every running session at once (the daemon is the parent
of every session CLI and the single most catastrophic thing to kill).

There is no portable API to embed a message in a process's *name* (that is the
executable, ``python``/``claude``).  But two channels ARE visible to anything
inspecting the process list, and we control both:

  1. The COMMAND LINE.  Visible cross-platform:
       • Windows: ``Get-CimInstance Win32_Process | select CommandLine``,
                  ``wmic process get commandline``
       • Linux/macOS: ``ps aux`` (full argv shown by default)
     We append a marker token to the daemon subprocess's command line.  Because
     every session ``claude.exe`` is a CHILD of that daemon, a process-tree-aware
     agent that walks ParentProcessId sees the whole family is VibeNode.

  2. The OS PROCESS TITLE, where the platform lets us change it at runtime:
       • setproctitle (if installed) — rewrites the full argv shown in ``ps``;
         best result, used opportunistically with no hard dependency.
       • Linux fallback: prctl(PR_SET_NAME) — sets the 15-char ``comm`` shown by
         ``top``/``htop``/``ps -o comm``.
       • Windows: SetConsoleTitleW — sets the console window title (a no-op for
         the daemon, which runs windowless, but useful for the web process).

We also stamp marker ENV VARS into the process environment.  They propagate to
the spawned ``claude`` children (which inherit the daemon's env) and are
readable via ``/proc/<pid>/environ`` on Linux, giving the otherwise-unlabelled
children a discoverable VibeNode fingerprint.

Everything here is BEST-EFFORT and must never raise into a boot path: labeling
is a nicety, not a correctness requirement.  Deliberately a top-level,
stdlib-only module (mirrors _early_boot.py) so both entry points — run.py and
daemon/daemon_server.py — can import it without pulling in the ``app`` package.

NOTE: a label only takes effect when the process is (re)started.  Editing this
file does not relabel an already-running daemon or web server.
"""

import os
import sys

# Short, greppable token. An agent can match on this to recognize a VibeNode
# process; a human reads the surrounding words.
MARKER = "VibeNode-DO-NOT-KILL"

# Human-readable, one-line explanation woven into the command line / title.
_EXPLAIN = (
    "active-AI-session-host--killing-this-aborts-ALL-running-AI-sessions"
)


def _role_blurb(role: str) -> str:
    """Full single-line label for a given process role (no spaces, so it stays
    one argv token / one clean line in process listings)."""
    return f"[{MARKER}:{role}--{_EXPLAIN}]"


def daemon_spawn_marker_args(role: str = "session-daemon") -> list:
    """Marker tokens to append to the daemon's Popen command line.

    The daemon reads ALL its configuration from environment variables and never
    parses ``sys.argv`` (verified), so appending these tokens is inert — they
    exist purely to show up in the daemon's command line for anyone (human or
    AI) inspecting the process list.
    """
    return [_role_blurb(role)]


def marker_env(role: str) -> dict:
    """Env vars that fingerprint a VibeNode process and, via inheritance, its
    ``claude`` CLI children.  Merge into the child env at spawn time and/or into
    ``os.environ`` so descendants carry them."""
    return {
        "VIBENODE_PROCESS": MARKER,
        "VIBENODE_PROCESS_ROLE": role,
        "VIBENODE_DO_NOT_KILL": (
            "Killing this aborts all running AI sessions managed by VibeNode."
        ),
    }


def _set_comm_linux(name: str) -> None:
    """Set the 15-char ``comm`` (shown by top/htop/ps -o comm) via prctl."""
    try:
        import ctypes
        import ctypes.util

        # Resolve libc portably: glibc is "libc.so.6", but musl (Alpine) uses a
        # different name.  find_library("c") returns whatever this distro ships;
        # fall back to the glibc soname only if resolution fails.
        libc_name = ctypes.util.find_library("c") or "libc.so.6"
        libc = ctypes.CDLL(libc_name, use_errno=True)
        PR_SET_NAME = 15
        # comm is capped at 16 bytes including the NUL terminator.
        buf = ctypes.create_string_buffer(name.encode("utf-8", "replace")[:15])
        libc.prctl(PR_SET_NAME, ctypes.cast(buf, ctypes.c_char_p), 0, 0, 0)
    except Exception:
        pass


def _set_console_title_windows(title: str) -> None:
    """Set the console window title (harmless no-op for windowless processes)."""
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


def label_current_process(role: str) -> None:
    """Stamp the current process so a process hunter can identify it.

    ``role`` is a short slug, e.g. ``"web-server"`` or ``"session-daemon"``.
    Best-effort across mechanisms; never raises.
    """
    blurb = _role_blurb(role)

    # 1) Env markers — also inherited by any child we later spawn (e.g. the
    #    session CLIs under the daemon).
    try:
        os.environ.update(marker_env(role))
    except Exception:
        pass

    # 2) Full-argv title via setproctitle, if the user has it installed. This is
    #    the loudest signal on POSIX (`ps aux` shows the whole blurb). Optional
    #    dependency — absent by default, used automatically when present.
    try:
        import setproctitle  # type: ignore

        setproctitle.setproctitle(f"python {blurb}")
        # setproctitle covers the full argv; comm below is redundant but cheap.
    except Exception:
        pass

    # 3) Platform-native fallbacks.
    if sys.platform == "win32":
        _set_console_title_windows(f"VibeNode {role} — DO NOT KILL "
                                   "(active AI session host)")
    elif sys.platform.startswith("linux"):
        # comm is 15 chars max; keep it recognizable.
        _set_comm_linux(f"vibenode-{role}"[:15])
