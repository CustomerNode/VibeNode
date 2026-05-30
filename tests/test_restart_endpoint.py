"""Source + execution guards for the /api/restart endpoint.

History: every Linux "Restart Web" silently failed for months because the
restart shell command used ``nohup VAR=value cmd`` to pass
``VIBENODE_PRESERVE_DAEMON=1`` to the child python.  That syntax is a bash
builtin (only valid for "simple commands") — bash sees ``nohup`` as the
command and ``VAR=value`` as its first arg, so nohup tries to execute a file
literally named "VAR=value" and dies with "No such file or directory".  The
bash subshell had already killed port 5050 by that point, so the user was
left with no web server and an unresponsive UI, with the symptom matching
"restart doesn't work on Linux" perfectly.

These tests lock in the fix two ways:
  1. **Source guard** — the dangerous ``nohup VAR=...`` pattern must not
     reappear in app/routes/main.py.  The correct form is ``export VAR=...;
     nohup ...`` (env var set in the shell, inherited by nohup).
  2. **Execution probe** — running the exact bash construction (with the
     real python swapped for /bin/sh) must propagate the env var to the
     child process and produce no nohup error on stderr.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest


_MAIN_PY = Path(__file__).resolve().parent.parent / "app" / "routes" / "main.py"


@pytest.mark.skipif(
    os.name == "nt", reason="bash/nohup execution probe is POSIX-only"
)
class TestRestartShellSyntax:
    """The Linux restart command must use ``export VAR=...; nohup cmd``,
    NOT ``nohup VAR=... cmd``.  See module docstring for the bug history.
    """

    def test_no_env_prefix_on_nohup(self):
        """Source guard: forbid the broken ``nohup VAR=value`` pattern."""
        src = _MAIN_PY.read_text(encoding="utf-8")
        # Look only inside restart_server / shutdown_server — false-positive
        # safe because this file is small and these are the only places we
        # build a nohup command line.
        forbidden = 'nohup {env'
        assert forbidden not in src, (
            "Restart shell command must NOT interpolate env_prefix between "
            "`nohup` and the executable.  Use `export VAR=value; nohup ...` "
            "instead — see test_restart_endpoint.py module docstring for the "
            "history of the bug this guard prevents."
        )

    def test_export_pattern_present(self):
        """Source guard: the safe ``export VAR=...; nohup`` pattern is what
        we actually shipped."""
        src = _MAIN_PY.read_text(encoding="utf-8")
        assert "export VIBENODE_PRESERVE_DAEMON=1" in src
        # Ensure it precedes the nohup in the same f-string (regex-ish check
        # by substring ordering inside the file).
        export_pos = src.find("export VIBENODE_PRESERVE_DAEMON=1")
        nohup_pos = src.find("nohup ", export_pos)
        assert 0 <= export_pos < nohup_pos, (
            "`export VAR=1` must appear before `nohup ...` so nohup inherits "
            "the var via the shell environment."
        )

    def test_env_var_actually_reaches_child(self, tmp_path):
        """End-to-end probe: build the EXACT bash construction the endpoint
        uses (substituting a harmless /bin/sh for the real python) and prove
        the child inherits the env var AND nohup emits no error."""
        log = tmp_path / "restart_probe.log"
        # Mirror app/routes/main.py exactly: kill loop (no-op here), then
        # `export VAR=1; nohup CHILD ...`.  The child writes the var value to
        # the log and exits.  We then assert the log shows "CHILD_SAW=1" and
        # that nohup did NOT log its "No such file or directory" error.
        cmd = (
            "bash -c '"
            "for i in $(seq 1 1); do true; done; "
            "sleep 0; "
            "export VIBENODE_PRESERVE_DAEMON=1; "
            f"nohup /bin/sh -c \"echo CHILD_SAW=$VIBENODE_PRESERVE_DAEMON\" "
            f"</dev/null >>\"{log}\" 2>&1 &"
            "'"
        )
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=5)
        assert r.returncode == 0, f"bash itself failed: {r.stderr}"
        # Background nohup needs a moment to write its log
        for _ in range(20):
            if log.exists() and log.stat().st_size > 0:
                break
            time.sleep(0.1)
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        assert "No such file or directory" not in text, (
            f"nohup choked on the command line — env-prefix bug is back. "
            f"Log:\n{text}"
        )
        assert "CHILD_SAW=1" in text, (
            f"Child did not inherit VIBENODE_PRESERVE_DAEMON. Log:\n{text}"
        )

    def test_broken_pattern_actually_fails(self, tmp_path):
        """Sanity check: prove the OLD broken pattern really did fail this
        way on Linux.  If this test ever passes (i.e. nohup starts accepting
        env-prefix syntax), the source guards above can be relaxed."""
        log = tmp_path / "broken_probe.log"
        cmd = (
            "bash -c '"
            f"nohup VIBENODE_PRESERVE_DAEMON=1 /bin/sh -c \"echo CHILD_SAW=$VIBENODE_PRESERVE_DAEMON\" "
            f"</dev/null >>\"{log}\" 2>&1; true'"
        )
        subprocess.run(cmd, shell=True, capture_output=True,
                       text=True, timeout=5)
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        assert "No such file or directory" in text or "command not found" in text, (
            f"Expected nohup to reject `VIBENODE_PRESERVE_DAEMON=1` as a "
            f"command, but it didn't.  If this regression-canary test starts "
            f"passing the wrong way (child inherits the var), the underlying "
            f"OS/nohup behaviour has changed and the source guards above can "
            f"be reviewed.  Log:\n{text}"
        )
