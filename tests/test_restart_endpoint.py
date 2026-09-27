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

Additional history — Step 5 ARM64 cutover (2026-09-26): the __pycache__
cleanup that runs inside the restart command previously used
``Get-ChildItem -Recurse -Filter '__pycache__'`` on Windows and
``find ... -name __pycache__`` on POSIX.  Both recurse into every
subdirectory, including ``.venv-arm64-candidate`` (the ARM64 candidate
venv that lives inside the project tree).  Site-packages alone carries
tens of thousands of __pycache__ dirs; walking them stretched the
restart window enough for reviver.py to bind port 5050 as a bystander.
The kill loop then killed the reviver and the strict cutover assertion
tripped.  The fix prunes ``.venv*`` subtrees BEFORE descending, on both
platforms.  These tests lock that fix in.

Second Step 5 restart-race fix (2026-09-26, later the same day): even
with the pycache prune in place, the replacement web process's own
``_kill_port(_WEB_PORT)`` at boot in ``run.py`` was killing the reviver.
By that point in the boot, whoever holds 5050 is legitimately the
reviver (serving the Start page while VibeNode is between web PIDs) —
the guardian respawns it immediately so the system self-heals, but the
reviver PID changes, tripping the strict cutover assertion.  The fix
skips ``_kill_port(_WEB_PORT)`` when ``VIBENODE_PRESERVE_DAEMON=1``, so
``reclaim_port()`` further down the boot negotiates the yield via the
reviver's ``/yield`` control endpoint without killing the process.
``TestRunPySkipsWebKillOnPreserveDaemon`` locks that fix in — both by
AST source shape and by executing the actual guarded If blocks under
mocked environments.
"""

import ast
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


_MAIN_PY = Path(__file__).resolve().parent.parent / "app" / "routes" / "main.py"
_RUN_PY = Path(__file__).resolve().parent.parent / "run.py"


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


class TestPycacheCleanupPrunesVenv:
    """The __pycache__ cleanup inside /api/restart must NOT descend into
    ``.venv*`` directories.  Reverting either the Windows or POSIX guard
    re-introduces the Step 5 ARM64 cutover restart-race: the recursive
    walk into ``.venv-arm64-candidate/Lib/site-packages`` stalls the
    restart window long enough for reviver.py to bind port 5050, and the
    kill loop then kills the reviver as a bystander.
    """

    # ------------------------------------------------------------------
    # Source guards — cheap, run on every platform.
    # ------------------------------------------------------------------

    def test_windows_powershell_skips_venv(self):
        """Windows restart's PowerShell block must skip ``.venv*``
        subtrees during traversal.  Get-ChildItem -Recurse walks every
        subdirectory even with -Exclude, so the pre-fix pattern
        (``Get-ChildItem -Recurse -Directory -Filter '__pycache__'``)
        cannot be resurrected.
        """
        src = _MAIN_PY.read_text(encoding="utf-8")
        # New guard: prune .venv* before descending.
        assert "-like '.venv*'" in src, (
            "Windows restart PowerShell must test each directory name "
            "with `-like '.venv*'` and skip it, so that Get-ChildItem "
            "never descends into a virtualenv's site-packages tree."
        )
        # Old broken pattern must not reappear.
        assert (
            "-Recurse -Directory -Filter '__pycache__'" not in src
        ), (
            "Windows restart must not use Get-ChildItem -Recurse for "
            "__pycache__ cleanup.  -Recurse walks .venv* trees even when "
            "-Exclude is applied — see Step 5 restart-race diagnosis."
        )
        # And the stack-based traversal helper must be present.
        assert "System.Collections.Stack" in src, (
            "Expected the stack-based directory walk that prunes .venv*"
        )

    def test_posix_find_prunes_venv(self):
        """POSIX restart's find command must ``-prune`` ``.venv*`` before
        descending.  Without the prune, find recursively scans every
        subdirectory including the ARM64 candidate venv's
        site-packages."""
        src = _MAIN_PY.read_text(encoding="utf-8")
        # New guard: explicit -prune of .venv*.  The source has this
        # inside an f-string with escaped double-quotes, so a raw scan
        # sees ``\"``.  Look for the `.venv*` -prune signature in a way
        # that's tolerant of either double- or single-quoted find
        # argument syntax.
        prune_markers = (
            '-name ".venv*" -prune',
            "-name '.venv*' -prune",
            '-name \\".venv*\\" -prune',
        )
        prune_idx = -1
        for marker in prune_markers:
            i = src.find(marker)
            if i >= 0:
                prune_idx = i
                break
        assert prune_idx >= 0, (
            "POSIX restart's find must include a `.venv*` -prune "
            "clause on the left of the -o, so descent into virtualenv "
            "trees short-circuits BEFORE __pycache__ matches."
        )
        # Old broken pattern (no prune) must not stand alone.  The
        # __pycache__ -exec rm branch must still exist AFTER the prune.
        assert "-type d -name __pycache__ -exec rm -rf" in src, (
            "The __pycache__ -exec rm branch must still exist for real "
            "project bytecode to be removed."
        )
        pycache_idx = src.find("-name __pycache__ -exec rm -rf")
        # The prune clause must precede the __pycache__ clause in the
        # find expression (short-circuit semantics of `-o`).
        assert prune_idx < pycache_idx, (
            "The .venv* -prune clause must appear BEFORE the "
            "__pycache__ -exec rm branch in the find expression, so "
            "find short-circuits and never enters virtualenv trees."
        )

    def test_restart_still_ships_surrounding_structure(self):
        """The rest of restart_server must be intact — the port kill
        loop, the preserve-daemon env, and the pythonw spawn are all
        essential and must not have been damaged by the __pycache__
        edit."""
        src = _MAIN_PY.read_text(encoding="utf-8")
        # Kill loop preserved (both platforms).
        assert "Get-NetTCPConnection -LocalPort" in src
        assert "lsof -ti :" in src
        # Preserve-daemon plumbing preserved (both platforms).
        assert "$env:VIBENODE_PRESERVE_DAEMON='1'" in src
        assert "export VIBENODE_PRESERVE_DAEMON=1" in src
        # Windows spawn preserved.
        assert "Start-Process -FilePath" in src
        # POSIX spawn preserved.  The source form is ``nohup \"...\"``
        # (escaped double-quote inside an f-string), so scan for the
        # backslash-escaped form.
        assert 'nohup \\"' in src, (
            "POSIX spawn's `nohup \"$python\" \"$entry\"` construction "
            "must remain intact."
        )
        # Both branches still hand session_manager.py to the child.
        assert src.count("session_manager.py") >= 1

    # ------------------------------------------------------------------
    # Functional probes — run the exact platform command on a synthetic
    # project tree.  Never touches production processes, sockets or the
    # real project directory.  All paths are under ``tmp_path``.
    # ------------------------------------------------------------------

    @staticmethod
    def _build_fake_tree(root):
        """Build a fake project tree with:
        - project __pycache__ at two nesting levels (both must be removed)
        - .venv-arm64-candidate/**/__pycache__ (must be preserved)
        - .venv-arm64-test/**/__pycache__ (must be preserved)
        - .venvNAME/**/__pycache__ (must be preserved — future venv name)
        Returns the four cache dirs so tests can assert on each.
        """
        proj_cache_a = root / "app" / "__pycache__"
        proj_cache_a.mkdir(parents=True)
        (proj_cache_a / "module.cpython-314.pyc").write_bytes(b"proj-a")

        proj_cache_b = root / "app" / "routes" / "__pycache__"
        proj_cache_b.mkdir(parents=True)
        (proj_cache_b / "routes.cpython-314.pyc").write_bytes(b"proj-b")

        venv_a = (
            root / ".venv-arm64-candidate" / "Lib" / "site-packages"
            / "flask" / "__pycache__"
        )
        venv_a.mkdir(parents=True)
        (venv_a / "flask.cpython-314.pyc").write_bytes(b"venv-a")

        venv_b = (
            root / ".venv-arm64-test" / "Lib" / "site-packages"
            / "click" / "__pycache__"
        )
        venv_b.mkdir(parents=True)
        (venv_b / "click.cpython-314.pyc").write_bytes(b"venv-b")

        venv_c = root / ".venvNAME" / "lib" / "pkg" / "__pycache__"
        venv_c.mkdir(parents=True)
        (venv_c / "pkg.cpython-314.pyc").write_bytes(b"venv-c")

        return proj_cache_a, proj_cache_b, venv_a, venv_b, venv_c

    @pytest.mark.skipif(
        os.name != "nt",
        reason="Windows PowerShell traversal is Windows-only",
    )
    def test_windows_cleanup_functional(self, tmp_path):
        """Run the SAME PowerShell block the endpoint issues, on a
        synthetic tree; project __pycache__ must go, .venv* must
        survive."""
        project = tmp_path / "vibenode-fake"
        project.mkdir()
        (
            proj_a,
            proj_b,
            venv_a,
            venv_b,
            venv_c,
        ) = self._build_fake_tree(project)

        # Byte-identical to the traversal in app/routes/main.py.
        # See the "PYCACHE_PRUNE_WINDOWS" reference below — the strings
        # are kept in sync manually; a source guard test asserts that
        # the key `-like '.venv*'` marker is present in main.py.
        ps = (
            "$stk = [System.Collections.Stack]::new(); "
            f"$stk.Push('{project}'); "
            "while ($stk.Count -gt 0) { "
            "  $d = $stk.Pop(); "
            "  Get-ChildItem -LiteralPath $d -Directory -Force"
            " -ErrorAction SilentlyContinue | ForEach-Object { "
            "    if ($_.Name -like '.venv*') { return }; "
            "    if ($_.Name -eq '__pycache__') {"
            " Remove-Item -LiteralPath $_.FullName -Recurse -Force"
            " -ErrorAction SilentlyContinue } "
            "    else { $stk.Push($_.FullName) } "
            "  } "
            "}"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, (
            f"powershell failed: stderr={r.stderr!r} stdout={r.stdout!r}"
        )

        # Project caches removed.
        assert not proj_a.exists(), (
            "Regular project __pycache__ (top-level) was not removed."
        )
        assert not proj_b.exists(), (
            "Regular project __pycache__ (nested) was not removed."
        )
        # Venv caches preserved.
        for label, path in (
            (".venv-arm64-candidate", venv_a),
            (".venv-arm64-test", venv_b),
            (".venvNAME", venv_c),
        ):
            assert path.exists(), (
                f"__pycache__ under {label} was deleted — traversal is "
                f"not pruning .venv* trees."
            )
            pyc = next(path.iterdir(), None)
            assert pyc is not None and pyc.read_bytes(), (
                f".pyc under {label} was corrupted."
            )

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX find/prune is not the Windows code path",
    )
    def test_posix_cleanup_functional(self, tmp_path):
        """Run the SAME find command the POSIX endpoint issues, on a
        synthetic tree; project __pycache__ must go, .venv* must
        survive."""
        project = tmp_path / "vibenode-fake"
        project.mkdir()
        (
            proj_a,
            proj_b,
            venv_a,
            venv_b,
            venv_c,
        ) = self._build_fake_tree(project)

        # Byte-identical to the find in app/routes/main.py (POSIX
        # branch).  Source guard asserts main.py still carries the
        # `-name '.venv*' -prune` clause.
        cmd = (
            f'find "{project}" '
            r'\( -type d -name ".venv*" -prune \) '
            r'-o \( -type d -name __pycache__ -exec rm -rf {} + \) '
            r'2>/dev/null'
        )
        r = subprocess.run(
            cmd, shell=True, executable="/bin/bash",
            capture_output=True, text=True, timeout=10,
        )
        assert r.returncode == 0, (
            f"find failed: stderr={r.stderr!r} stdout={r.stdout!r}"
        )

        assert not proj_a.exists(), (
            "Regular project __pycache__ (top-level) was not removed."
        )
        assert not proj_b.exists(), (
            "Regular project __pycache__ (nested) was not removed."
        )
        for label, path in (
            (".venv-arm64-candidate", venv_a),
            (".venv-arm64-test", venv_b),
            (".venvNAME", venv_c),
        ):
            assert path.exists(), (
                f"__pycache__ under {label} was deleted — find is not "
                f"pruning .venv* trees."
            )
            pyc = next(path.iterdir(), None)
            assert pyc is not None and pyc.read_bytes(), (
                f".pyc under {label} was corrupted."
            )

    def test_no_live_process_or_port_touched(self):
        """Contract test: every other test in this class must operate
        on ``tmp_path`` only, and never touch a real port, a real
        project path, or spawn a detached process.  This is enforced
        by walking the class AST and looking for concrete numeric
        constants or forbidden call targets — docstrings and comments
        that just mention "port 5050" for context are fine.
        """
        import ast
        import inspect
        src = inspect.getsource(TestPycacheCleanupPrunesVenv)
        tree = ast.parse(src)

        # Build the port set from strings so the numeric literals never
        # appear in this method's own AST — otherwise the walk below
        # would catch itself.
        forbidden_ints = {int(p) for p in ("50" + str(i) for i in range(50, 54))}
        forbidden_attrs = {"Popen"}  # subprocess.Popen spawns detached
        offending = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(
                node.value, int
            ):
                if node.value in forbidden_ints:
                    offending.append(
                        f"literal int {node.value} at line {node.lineno}"
                    )
            if isinstance(node, ast.Attribute):
                if node.attr in forbidden_attrs:
                    offending.append(
                        f".{node.attr} call at line {node.lineno}"
                    )
        assert not offending, (
            "Test class must not reference live ports "
            "(5050/5051/5052/5053) as numeric constants, nor spawn "
            f"detached processes via subprocess.Popen.  Offenders: "
            f"{offending}"
        )


class TestRunPySkipsWebKillOnPreserveDaemon:
    """Second Step 5 restart-race fix (2026-09-26): the replacement web
    process's own ``_kill_port(_WEB_PORT)`` at boot in ``run.py`` used to
    kill the reviver as a bystander.

    ``VIBENODE_PRESERVE_DAEMON=1`` is set on exactly two boot paths, both
    of which have ALREADY handled the outgoing web process before this
    boot begins:

      1. ``/api/restart`` with ``scope="web"`` — the ``restart_server``
         endpoint synchronously kills the prior web PID via a
         ``Stop-Process`` / ``lsof`` kill loop before launching this
         replacement.
      2. The phone's Start button on the reviver's Start page — the
         reviver only opens the Start page when 5050 is already down.

    In BOTH cases, whatever holds port 5050 by the time this replacement
    web boots is the reviver, legitimately serving the Start page while
    VibeNode is between web PIDs.  Killing it triggers the guardian to
    respawn the reviver with a new PID — self-healing but visible, and
    it trips the strict cutover assertion ("[RECOVERY] reviver
    unaffected").  ``reclaim_port()`` further down the boot NEGOTIATES
    the yield synchronously via the reviver's ``/yield`` control
    endpoint without killing the reviver process.

    These tests lock the fix in two ways:
      * **Source shape** — AST-parse ``run.py`` and prove the
        ``_kill_port(_WEB_PORT)`` call sits behind an ``if`` whose test
        references ``VIBENODE_PRESERVE_DAEMON``.
      * **Behavioural simulation** — extract the two guarded ``If``
        blocks that decide which ports to kill, exec them with a mocked
        ``os.environ`` and a mocked ``_kill_port``, and assert which
        ports would actually have been killed under each environment.

    No live process, port, socket, or file outside ``tmp_path`` is
    touched by any test in this class.
    """

    def _run_py_source_and_tree(self):
        src = _RUN_PY.read_text(encoding="utf-8")
        return src, ast.parse(src)

    def _port_kill_ifs(self):
        """Locate the two module-level ``If`` nodes in ``run.py`` that
        gate the port-kill calls.  Returns ``(src, outer_if,
        [web_if, daemon_if])`` in source order.  Raises AssertionError
        with a clear diagnostic if the surrounding shape has drifted
        far enough that the block can't be found — that itself is a
        regression signal worth surfacing loudly.
        """
        src, tree = self._run_py_source_and_tree()
        outer = None
        for node in tree.body:
            if not isinstance(node, ast.If):
                continue
            test_src = ast.unparse(node.test)
            body_src = ast.unparse(node)
            if "_TEST_PORT" in test_src and "_kill_port(" in body_src:
                outer = node
                break
        assert outer is not None, (
            "Could not find the top-level `if not _TEST_PORT:` block "
            "in run.py that contains _kill_port() calls.  The port-kill "
            "block has moved or the outer guard was renamed; the "
            "restart-race fix cannot be verified until the tests are "
            "updated to match."
        )
        ifs = []
        for stmt in outer.body:
            if isinstance(stmt, ast.If) and "_kill_port(" in ast.unparse(stmt):
                ifs.append(stmt)
        assert len(ifs) >= 2, (
            "Expected at least two `If` blocks that gate _kill_port "
            "calls inside `if not _TEST_PORT:`; found %d.  The Step 5 "
            "second restart-race fix requires BOTH the web-port and "
            "daemon-port kills to be gated by VIBENODE_PRESERVE_DAEMON."
            % len(ifs)
        )
        return src, outer, ifs[:2]

    # ------------------------------------------------------------------
    # Source-shape guards.
    # ------------------------------------------------------------------

    def test_web_port_kill_gated_by_preserve_daemon(self):
        """The first _kill_port If gate must test VIBENODE_PRESERVE_DAEMON,
        and exactly one of its branches must call _kill_port(_WEB_PORT)."""
        _, _, ifs = self._port_kill_ifs()
        web_if = ifs[0]
        test_src = ast.unparse(web_if.test)
        assert "VIBENODE_PRESERVE_DAEMON" in test_src, (
            "The first _kill_port(...) gate in run.py's boot block must "
            "test os.environ.get('VIBENODE_PRESERVE_DAEMON'). "
            "Actual test source: %r" % test_src
        )
        body_src = ast.unparse(web_if.body)
        else_src = ast.unparse(web_if.orelse) if web_if.orelse else ""
        web_calls_in_body = "_kill_port(_WEB_PORT)" in body_src
        web_calls_in_else = "_kill_port(_WEB_PORT)" in else_src
        assert web_calls_in_body ^ web_calls_in_else, (
            "Exactly one branch of the VIBENODE_PRESERVE_DAEMON guard "
            "must call _kill_port(_WEB_PORT).  body has it=%s, else "
            "has it=%s.  Both branches killing (or neither killing) "
            "the web port breaks the reviver-preservation guarantee."
            % (web_calls_in_body, web_calls_in_else)
        )

    def test_daemon_port_kill_still_gated_by_preserve_daemon(self):
        """The daemon-port kill must retain its pre-existing gate on
        VIBENODE_PRESERVE_DAEMON — this fix must not accidentally alter
        the daemon-preservation behavior."""
        _, _, ifs = self._port_kill_ifs()
        daemon_if = ifs[1]
        test_src = ast.unparse(daemon_if.test)
        assert "VIBENODE_PRESERVE_DAEMON" in test_src, (
            "The second _kill_port(...) gate (DAEMON_PORT) must still "
            "test VIBENODE_PRESERVE_DAEMON.  Actual: %r" % test_src
        )
        combined = ast.unparse(daemon_if.body) + ast.unparse(
            daemon_if.orelse or []
        )
        assert "_kill_port(DAEMON_PORT)" in combined, (
            "Daemon-port kill branch missing from run.py — the fix must "
            "not have removed the daemon-port cleanup on cold start."
        )

    def test_reclaim_port_still_called_before_bind(self):
        """The reviver-yield negotiation must still exist further down
        in run.py; otherwise we'd exit at bind time when the reviver
        legitimately holds 5050."""
        src, _ = self._run_py_source_and_tree()
        assert "reclaim_port(_port)" in src, (
            "run.py must still call reclaim_port(_port) before binding "
            "the web socket.  Without it, a scope=web restart would "
            "race the reviver's HTTP handler at the exclusive bind."
        )

    def test_wait_for_web_singleton_still_called(self):
        """The mutex-based singleton gate must remain — it's the
        socket-level backstop that prevents two web servers co-binding
        5050 on Windows."""
        src, _ = self._run_py_source_and_tree()
        assert "wait_for_web_singleton()" in src, (
            "wait_for_web_singleton() must remain in run.py.  Removing "
            "it re-enables the 'two live web servers' regression that "
            "produced the permanent 'VibeNode Engine Stopped' overlay."
        )

    def test_singleton_gate_runs_after_port_kills(self):
        """Ordering: the port-kill block precedes the singleton gate.
        The singleton gate uses ``port_has_listener`` to disambiguate a
        held mutex; that check depends on the kill loop having run
        first (in the cold-start branch)."""
        src, _ = self._run_py_source_and_tree()
        kills_pos = src.find("_kill_port(_WEB_PORT)")
        singleton_pos = src.find("wait_for_web_singleton()")
        assert 0 <= kills_pos < singleton_pos, (
            "The port-kill block must appear BEFORE the "
            "wait_for_web_singleton() call in run.py.  Reordering them "
            "breaks the cold-start disambiguation logic."
        )

    # ------------------------------------------------------------------
    # Behavioural simulation — exec the two If blocks with mocked
    # helpers and capture which ports would be killed.
    # ------------------------------------------------------------------

    def _simulate(self, preserve_value):
        """Compile and exec the two port-kill If blocks (verbatim from
        run.py) under a fake ``os.environ`` and a mocked ``_kill_port``.

        Returns a list of ports that ``_kill_port`` was called with, in
        the order the block would have called them.  The mocks make it
        impossible for the exec'd code to touch a real port, spawn a
        subprocess, or emit output — the only observable side effect is
        the port list this method returns.
        """
        _, _, ifs = self._port_kill_ifs()
        module = ast.Module(body=list(ifs), type_ignores=[])
        code = compile(
            module, filename="<run.py:port-kill-snippet>", mode="exec"
        )
        killed_ports = []

        class _FakeOs:
            environ = {}

        if preserve_value is not None:
            _FakeOs.environ["VIBENODE_PRESERVE_DAEMON"] = preserve_value

        def _fake_kill_port(p):
            killed_ports.append(p)

        # Silence the informational prints inside the branches; they
        # aren't part of the contract this test is enforcing.
        def _silent(*_a, **_k):
            pass

        ns = {
            "os": _FakeOs,
            "_kill_port": _fake_kill_port,
            # The port constants match run.py's own defaults — they are
            # not live listeners in this test, just integers routed to
            # the mocked _kill_port.
            "_WEB_PORT": 5050,
            "DAEMON_PORT": 5051,
            "print": _silent,
        }
        exec(code, ns)
        return killed_ports

    def test_scope_web_restart_skips_web_port_kill(self):
        """VIBENODE_PRESERVE_DAEMON=1 → _kill_port(_WEB_PORT) is NOT
        called.  This is the core assertion of the fix."""
        killed = self._simulate("1")
        web_port = 5050
        assert web_port not in killed, (
            "The replacement web process killed port %d even though "
            "VIBENODE_PRESERVE_DAEMON=1.  This is the exact regression "
            "the Step 5 second restart-race fix addresses: killing the "
            "reviver as a bystander during scope=web restart.  Killed: "
            "%r" % (web_port, killed)
        )

    def test_scope_web_restart_skips_daemon_port_kill(self):
        """VIBENODE_PRESERVE_DAEMON=1 → _kill_port(DAEMON_PORT) is NOT
        called.  Existing behavior that must be preserved."""
        killed = self._simulate("1")
        daemon_port = 5051
        assert daemon_port not in killed, (
            "The replacement web process killed the daemon port (%d) "
            "even though VIBENODE_PRESERVE_DAEMON=1.  This would "
            "destroy every active session and CLI subprocess.  "
            "Killed: %r" % (daemon_port, killed)
        )

    def test_scope_web_restart_kills_nothing(self):
        """Under VIBENODE_PRESERVE_DAEMON=1 the block must issue ZERO
        _kill_port calls — no other ports may sneak in as collateral."""
        killed = self._simulate("1")
        assert killed == [], (
            "Under VIBENODE_PRESERVE_DAEMON=1 the port-kill block must "
            "kill nothing.  Ports killed: %r" % killed
        )

    def test_cold_start_kills_both_ports(self):
        """VIBENODE_PRESERVE_DAEMON unset → both web and daemon ports
        are killed.  Preserves cold-start behavior."""
        killed = self._simulate(None)
        assert 5050 in killed, (
            "Cold start must still kill the web port to clear stale "
            "squatters.  Killed: %r" % killed
        )
        assert 5051 in killed, (
            "Cold start must still kill the daemon port.  "
            "Killed: %r" % killed
        )
        # And only those two — no accidental extras.
        assert set(killed) == {5050, 5051}, (
            "Cold-start kill block should touch ONLY 5050 and 5051; "
            "no other ports.  Killed: %r" % killed
        )

    def test_arbitrary_env_value_treated_as_cold_start(self):
        """Only the exact string ``"1"`` triggers preserve behavior —
        any other value (``"0"``, ``"true"``, ``""``) means cold start.
        This matches the ``== "1"`` and ``!= "1"`` tests used in run.py.
        """
        for probe in ("0", "true", "", "yes", "1 ", " 1"):
            killed = self._simulate(probe)
            assert 5050 in killed, (
                "VIBENODE_PRESERVE_DAEMON=%r was silently treated as "
                "preserve.  Only the exact string '1' should enable "
                "the skip.  Killed: %r" % (probe, killed)
            )
            assert 5051 in killed, (
                "VIBENODE_PRESERVE_DAEMON=%r was silently treated as "
                "preserve for the daemon port.  Killed: %r"
                % (probe, killed)
            )

    def test_snippet_uses_only_mocked_helpers(self):
        """Meta-guard: the extracted If blocks must not reference any
        name we haven't mocked.  If run.py's port-kill block starts
        touching, say, ``socket`` or ``subprocess`` directly, that's a
        design change that needs a new test — this catch surfaces it
        loudly instead of letting the simulation silently NameError."""
        _, _, ifs = self._port_kill_ifs()
        module = ast.Module(body=list(ifs), type_ignores=[])
        # Names that our _simulate mock actually provides.
        allowed = {
            "os", "_kill_port", "_WEB_PORT", "DAEMON_PORT", "print",
            # Constants and literals AST also exposes:
            "True", "False", "None",
            # ``flush`` is a keyword argument to print(); it appears in
            # the AST as an ``arg`` name, not a Name load, but include it
            # defensively.
            "flush",
        }
        referenced = set()
        for stmt in ifs:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Name) and isinstance(
                    node.ctx, ast.Load
                ):
                    referenced.add(node.id)
        missing = referenced - allowed
        assert not missing, (
            "The port-kill block in run.py references names not "
            "covered by the simulation namespace: %r.  Either mock the "
            "new name in _simulate() or extract the block differently."
            % sorted(missing)
        )

    # ------------------------------------------------------------------
    # Meta: no live process / port / socket / subprocess touched.
    # ------------------------------------------------------------------

    def test_no_live_process_or_port_touched(self):
        """Every test in this class must be pure-AST / pure-exec on
        mocked helpers.  Forbid ``subprocess.Popen``, ``subprocess.run``,
        ``socket.socket``, and ``urllib.*`` in this class's own source.
        """
        src = inspect.getsource(TestRunPySkipsWebKillOnPreserveDaemon)
        tree = ast.parse(src)
        forbidden_attrs = {"Popen", "run", "socket", "urlopen"}
        offending = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                offending.append(
                    "%s at line %d" % (node.attr, node.lineno)
                )
        assert not offending, (
            "This class must not spawn processes, open sockets, or "
            "make HTTP calls — the whole point of the AST/exec "
            "approach is that the simulation is hermetic.  Offenders: "
            "%r" % offending
        )


class TestRestartKillLoopCapturesInitialPids:
    """Step 5 second restart-race fix, PART 2 (2026-09-26): the kill
    loop in ``restart_server`` (app/routes/main.py) must capture the
    target PIDs UPFRONT and never re-query port owners between
    iterations.  Live evidence during the x64 validation showed the
    fix in run.py was necessary but insufficient: the PowerShell kill
    loop's ``Get-NetTCPConnection`` re-query per iteration was ALSO
    catching the reviver as it re-bound 5050 mid-restart.

    Both source-shape and behavioural tests are enforced:

      * **Source shape** (both platforms): the initial-PID capture
        line is present, and the pre-fix "``Get-NetTCPConnection`` /
        ``lsof -ti :<port>`` re-queried every iteration" pattern is
        gone.
      * **Behavioural** (Windows only for the PowerShell block, POSIX
        only for the bash block): run the exact shell command against
        a fake process that dies immediately (mimicking the killed
        web PID) plus a fake process that appears mid-loop (mimicking
        the reviver re-binding 5050 as a bystander).  Assert only the
        pre-captured PID is targeted.

    Nothing outside ``tmp_path`` is touched.
    """

    def _main_src(self):
        return _MAIN_PY.read_text(encoding="utf-8")

    # -----------------------------------------------------------------
    # Source-shape guards (all platforms).
    # -----------------------------------------------------------------

    def test_windows_captures_target_pids_upfront(self):
        """PowerShell path must define ``$targetPids`` from the initial
        port-owner query, and the kill loop's ``$alive`` filter must
        check process existence (not re-query port owners)."""
        src = self._main_src()
        assert "$targetPids" in src, (
            "PowerShell restart command must capture target PIDs into "
            "$targetPids BEFORE the kill loop, so newcomers (like a "
            "reviver that binds 5050 after the web is killed) are not "
            "swept up as collateral damage."
        )
        # The kill loop must consult $targetPids for each iteration's
        # alive set — not re-run Get-NetTCPConnection.
        assert "$targetPids | Where-Object" in src, (
            "The kill loop must derive its alive set from $targetPids, "
            "not from a fresh Get-NetTCPConnection call each round."
        )

    def test_windows_no_per_iteration_port_query(self):
        """The pre-fix pattern was ``$pids = @(Get-NetTCPConnection ...`
        INSIDE the for loop body.  That must not reappear — the
        initial capture line lives OUTSIDE the loop now."""
        src = self._main_src()
        # Locate the for-loop body (from `for ($i = 0` to the
        # matching `};`) and assert Get-NetTCPConnection is not
        # inside it.
        loop_start = src.find("for ($i = 0; $i -lt $maxTries")
        assert loop_start >= 0, (
            "Could not locate PowerShell kill loop for structural check."
        )
        # A crude but tight bound: the "};" that closes the for-loop
        # is the first "};" after loop_start followed by the
        # pycache traversal line.
        pycache_marker = src.find("$stk = [System.Collections.Stack]", loop_start)
        assert pycache_marker > loop_start, (
            "Could not locate pycache traversal after the kill loop."
        )
        loop_body = src[loop_start:pycache_marker]
        assert "Get-NetTCPConnection" not in loop_body, (
            "The PowerShell kill loop body must NOT contain "
            "Get-NetTCPConnection — that pattern re-queries port "
            "owners every iteration and re-introduces the reviver "
            "bystander-kill regression."
        )

    def test_posix_captures_target_pids_upfront(self):
        """POSIX path must define ``target_pids`` from the initial
        lsof query, then poll for their death with ``kill -0`` —
        never re-run ``lsof`` each round."""
        src = self._main_src()
        assert "target_pids=$(" in src, (
            "POSIX restart command must capture target PIDs into "
            "target_pids BEFORE the kill loop.  Without this, the "
            "loop re-queries port owners each iteration and kills "
            "the reviver as a bystander."
        )
        # kill -0 is the POSIX signal-0 idiom for "does this PID
        # exist?" — it must be present.
        assert "kill -0" in src, (
            "POSIX kill loop must poll for target-PID death with "
            "`kill -0 $pid`, not by re-running lsof."
        )

    def test_posix_no_per_iteration_lsof_kill_pipe(self):
        """The pre-fix pattern in ``restart_server`` was
        ``lsof -ti :<port> | xargs kill -9 2>/dev/null;`` executed
        inside a ``for i in $(seq 1 10); do ... sleep 0.5; done``
        loop.  It must be gone from restart_server's code.  The
        unrelated ``shutdown_server`` function may legitimately still
        use lsof|xargs since it's a one-shot terminal shutdown, not a
        restart racing the reviver — this test explicitly excludes it.
        """
        src = self._main_src()
        # Isolate restart_server's function body: everything from
        # "def restart_server(" to the next top-level "def " (which
        # is shutdown_server).
        rs_start = src.find("def restart_server(")
        assert rs_start >= 0, "restart_server function not found"
        # Next top-level `def ` (at column 0) after restart_server.
        rs_end = src.find("\ndef ", rs_start + 1)
        assert rs_end >= 0, (
            "Could not delimit restart_server function body — the "
            "test needs a following top-level def to bound the slice."
        )
        rs_body = src[rs_start:rs_end]
        # Strip Python comment-only lines so this test doesn't
        # catch the explanatory comment that quotes the pre-fix
        # pattern as history.
        code_lines = []
        for line in rs_body.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            code_lines.append(line)
        code_only = "\n".join(code_lines)
        # The pre-fix f-string form is unique to restart_server's
        # code and doesn't appear in any comment.
        assert 'lsof -ti :{p}' not in code_only, (
            "The pre-fix f-string ``lsof -ti :{p} | xargs kill -9`` "
            "must not reappear in restart_server's code."
        )
        # Also assert the pipe-to-xargs-kill fragment is absent from
        # restart_server code (the shutdown_server usage is out of
        # this slice by construction).
        assert "| xargs kill -9" not in code_only, (
            "The ``| xargs kill -9`` pipe must not appear in "
            "restart_server's code.  Any port-owner kill inside a "
            "restart must go through the initial-PID-capture pattern."
        )

    def test_posix_kill_loop_wrapper_removed(self):
        """The outer wrapper ``for i in $(seq 1 10); do {kill_cmds}
        sleep 0.5; done;`` was redundant once kill_cmds became the
        complete loop.  Make sure it wasn't left behind (it would
        run the whole capture-and-kill sequence 10 times over)."""
        src = self._main_src()
        # The pre-fix outer wrapper looked exactly like this line
        # substring.  Grep for it verbatim.
        assert (
            'for i in $(seq 1 10); do {kill_cmds}'
        ) not in src, (
            "The old outer for-loop wrapper is still in the source "
            "and would repeat the entire capture-and-kill 10x — "
            "delete it."
        )

    def test_kill_loop_still_iterates(self):
        """Retries must remain — some processes ignore the first
        SIGTERM/Stop-Process and need a second/third try."""
        src = self._main_src()
        # Windows: `for ($i = 0; $i -lt $maxTries ...` still present.
        assert "for ($i = 0; $i -lt $maxTries" in src, (
            "PowerShell retry loop was removed; kills that don't take "
            "on the first attempt will now be silently dropped."
        )
        # POSIX: `for _i in $(seq 1 10)` still present.
        assert "for _i in $(seq 1 10)" in src, (
            "POSIX retry loop was removed; kills that don't take on "
            "the first attempt will now be silently dropped."
        )

    def test_start_process_and_nohup_still_present(self):
        """The spawn of the replacement web must not have been
        disturbed by the kill-loop refactor."""
        src = self._main_src()
        assert "Start-Process -FilePath" in src
        assert 'nohup \\"' in src
        # session_manager.py handoff intact on both platforms.
        assert src.count("session_manager.py") >= 1

    # -----------------------------------------------------------------
    # Behavioural probe: extract the shell block and run it against a
    # synthetic scenario.  We start a live "victim" child process (the
    # simulated web) plus a live "bystander" child process (the
    # simulated reviver).  The bystander is NOT in the initial
    # target_pids set.  After the kill loop, the victim must be dead
    # and the bystander must still be alive.  Nothing binds to a real
    # port; the shell block operates purely on PIDs.
    # -----------------------------------------------------------------

    @pytest.mark.skipif(
        os.name != "nt",
        reason="PowerShell kill-loop behavioural probe is Windows-only",
    )
    def test_windows_kill_loop_spares_bystander(self, tmp_path):
        """Launch two long-sleep victims, capture ONE of their PIDs as
        $targetPids, then run the exact PowerShell kill loop.  Only
        the captured PID must die."""
        # Two hermetic long-sleep processes — pythonw with a wait
        # loop.  We use the current interpreter so no external tool
        # is required.
        script = tmp_path / "sleeper.py"
        script.write_text(
            "import time\n"
            "while True:\n"
            "    time.sleep(60)\n",
            encoding="utf-8",
        )
        victims = []
        try:
            for _ in range(2):
                pythonw = os.path.join(
                    os.path.dirname(sys.executable), "pythonw.exe"
                )
                if not os.path.exists(pythonw):
                    pythonw = sys.executable
                p = subprocess.Popen(
                    [pythonw, str(script)],
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                )
                victims.append(p)
            # Grace period so both are visibly Running.
            time.sleep(0.5)
            victim_pid = victims[0].pid
            bystander_pid = victims[1].pid

            # Exact PowerShell block from main.py, with $targetPids
            # seeded to ONLY the victim PID (mimicking the
            # initial-capture step).  If the fix were reverted to
            # per-iteration Get-NetTCPConnection, the bystander
            # would still be visible via the port query — but here
            # we're proving the fix's core invariant: given a
            # captured set, the loop only kills that set.
            ps = (
                f"$targetPids = @({victim_pid}); "
                "$maxTries = 10; "
                "for ($i = 0; $i -lt $maxTries; $i++) { "
                "  $alive = @($targetPids | Where-Object { "
                "    (Get-Process -Id $_ -ErrorAction SilentlyContinue) -ne $null "
                "  }); "
                "  if ($alive.Count -eq 0) { break }; "
                "  $alive | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }; "
                "  Start-Sleep -Milliseconds 500 "
                "}"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=30,
                creationflags=0x08000000,
            )
            assert r.returncode == 0, (
                f"PowerShell kill loop failed: stderr={r.stderr!r}"
            )
            # Confirm victim is dead and bystander is alive.
            time.sleep(0.3)
            victim_alive = victims[0].poll() is None
            bystander_alive = victims[1].poll() is None
            assert not victim_alive, (
                "Kill loop failed to kill the captured target PID."
            )
            assert bystander_alive, (
                "Kill loop killed the bystander PID — the "
                "initial-capture invariant is broken."
            )
        finally:
            for p in victims:
                try:
                    if p.poll() is None:
                        p.kill()
                        p.wait(timeout=5)
                except Exception:
                    pass

    @pytest.mark.skipif(
        os.name == "nt",
        reason="bash kill-loop behavioural probe is POSIX-only",
    )
    def test_posix_kill_loop_spares_bystander(self, tmp_path):
        """POSIX equivalent of the Windows probe above."""
        # Two long-sleep victims via /bin/sleep.
        victims = [
            subprocess.Popen(
                ["/bin/sh", "-c", "sleep 60"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(2)
        ]
        try:
            time.sleep(0.3)
            victim_pid = victims[0].pid
            bystander_pid = victims[1].pid
            bash_block = (
                f"target_pids={victim_pid}; "
                "for _i in $(seq 1 10); do "
                "_alive=; "
                "for _pid in $target_pids; do "
                "kill -0 $_pid 2>/dev/null && _alive=\"$_alive $_pid\"; "
                "done; "
                "[ -z \"$_alive\" ] && break; "
                "for _pid in $_alive; do kill -9 $_pid 2>/dev/null; done; "
                "sleep 0.5; "
                "done"
            )
            r = subprocess.run(
                ["bash", "-c", bash_block],
                capture_output=True, text=True, timeout=30,
            )
            assert r.returncode == 0, (
                f"bash kill loop failed: stderr={r.stderr!r}"
            )
            time.sleep(0.3)
            victim_alive = victims[0].poll() is None
            bystander_alive = victims[1].poll() is None
            assert not victim_alive, (
                "Kill loop failed to kill the captured target PID."
            )
            assert bystander_alive, (
                "Kill loop killed the bystander PID — the "
                "initial-capture invariant is broken."
            )
        finally:
            for p in victims:
                try:
                    if p.poll() is None:
                        p.kill()
                        p.wait(timeout=5)
                except Exception:
                    pass
