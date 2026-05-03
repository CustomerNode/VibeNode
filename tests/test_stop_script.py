"""Source guards for stopvibenode.sh.

The Stop VibeNode desktop shortcut runs ``stopvibenode.sh``, which kills
the web server (5050), the session daemon (5051), and any orphaned
subprocesses spawned from the local VibeNode checkout. Three things must
stay true; if any drifts, the user clicks Stop and either nothing dies
or — worse — the wrong process dies.

We can't actually execute the script in tests (it would kill the dev
server / daemon you're running on this machine), so these are read-the-
source guards, same pattern as test_browser_launch.py and
test_launcher_splash.py.
"""

from pathlib import Path


_STOP_SH = Path(__file__).resolve().parent.parent / "stopvibenode.sh"


def _read():
    assert _STOP_SH.exists(), \
        "stopvibenode.sh must exist at the repo root — the Stop VibeNode " \
        "desktop shortcut depends on it."
    return _STOP_SH.read_text(encoding="utf-8")


class TestStopScriptCorePorts:
    """The two ports VibeNode actually owns must be in the kill list. If
    either is missing, clicking Stop leaves a half-running app."""

    def test_kills_web_server_port_5050(self):
        src = _read()
        assert " 5050 " in src or "for port in 5050" in src, \
            "stopvibenode.sh must include port 5050 (Flask web server). " \
            "If 5050 is removed, Stop leaves the web UI running and the " \
            "next Boot will hit an Address-already-in-use."

    def test_kills_daemon_port_5051(self):
        src = _read()
        assert "5051" in src, \
            "stopvibenode.sh must include port 5051 (session daemon). " \
            "Without it the daemon keeps running, sessions stay alive, " \
            "and Stop becomes a misnomer."


class TestStopScriptScoping:
    """Pattern-kill is scoped to THIS checkout's absolute path so a user
    with two VibeNode clones doesn't accidentally kill the wrong one when
    they click Stop on the desktop shortcut for a specific clone."""

    def test_uses_dirname_of_script_for_cwd(self):
        """``cd "$(dirname "$0")"`` makes the script portable: the user
        can clone VibeNode anywhere and the shortcut still works."""
        src = _read()
        assert 'cd "$(dirname "$0")"' in src, \
            "stopvibenode.sh must cd into its own directory so the kill " \
            "patterns reference the right absolute path. Hardcoding a " \
            "path breaks every user except the one who wrote the script."

    def test_pattern_kill_scoped_to_vibenode_dir(self):
        """pkill patterns must include ${VIBENODE_DIR} so the regex only
        matches python processes running THIS checkout's scripts. Without
        the prefix, pkill would match any python running session_manager.py
        or run.py from any directory — including unrelated projects."""
        src = _read()
        assert 'VIBENODE_DIR="$(pwd)"' in src or 'VIBENODE_DIR=$(pwd)' in src, \
            "Script must capture VIBENODE_DIR for scoping pkill patterns."
        # The pkill loop must reference VIBENODE_DIR, not bare basenames
        assert '${VIBENODE_DIR}/session_manager.py' in src, \
            "pkill must use ${VIBENODE_DIR}/session_manager.py — without " \
            "the directory prefix, pkill -f matches any python process " \
            "running a file called session_manager.py anywhere on disk."


class TestStopScriptRobustness:
    """Defensive details that have caught real bugs in the customerNode
    stopdev.sh this is modeled on."""

    def test_does_not_use_set_e(self):
        """``set -e`` would abort on the first kill that fails (because the
        process was already dead). The script needs to keep going."""
        src = _read()
        # Acceptable: `set -uo pipefail` (no -e) or no set at all
        assert "set -e" not in src or "set -uo pipefail" in src, \
            "stopvibenode.sh must NOT use `set -e`. Many kills fail " \
            "harmlessly (process already dead, port not bound) and we " \
            "want to keep going through every cleanup step."

    def test_supports_lsof_or_fuser_for_port_lookup(self):
        """fuser and lsof are both common; not every Linux box ships both.
        The script must try one and fall back to the other so it works
        on bare Debian/Ubuntu installs (lsof not installed by default)."""
        src = _read()
        assert "fuser" in src and "lsof" in src, \
            "stopvibenode.sh must try both `fuser` and `lsof` for port-" \
            "to-PID lookup — neither is universally installed."

    def test_cleans_up_boot_splash_status_file(self):
        """session_manager._launch_splash() writes /tmp/vibenode_boot_*.status
        and orphans them on hard kill. Stop should sweep these up."""
        src = _read()
        assert "vibenode_boot_*.status" in src, \
            "Stop should rm /tmp/vibenode_boot_*.status — these orphan " \
            "after a force-kill and confuse the next launch's IPC."
