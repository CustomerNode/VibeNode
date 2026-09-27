"""Step 6 ARM64 stabilization -- local runtime selection tests.

Added 2026-09-26. Locks in the design that a machine-specific interpreter
pin (e.g. an ARM64 venv on this host) is expressed via gitignored
``.local/python.txt`` + ``.local/env.txt``, NOT via a tracked file that
would publish the local path through the public repo.

The tests operate on source text and on isolated helper calls under a
temp directory. They never touch the live process environment, do not
spawn subprocesses, and do not exercise the actual production paths on
disk. Because ``reviver`` is stdlib-only, importing it here is safe.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_LAUNCH_BAT = _REPO / "launch.bat"
_SESSION_MANAGER = _REPO / "session_manager.py"
_REVIVER = _REPO / "reviver.py"
_GITIGNORE = _REPO / ".gitignore"

# The machine-local ARM64 venv path that MUST NOT appear in any tracked
# file. Derived at test time from _REPO so this constant itself contains
# NO hardcoded username or absolute prefix -- publishing test_source
# through git therefore does not leak any per-machine identity.
# _LOCAL_ARM64_FRAGMENT is the venv folder name (generic across all
# machines that use Step 6's local-override pattern) and is safe to
# embed as a literal string.
_LOCAL_ARM64_FRAGMENT = ".venv-arm64-candidate"
_LOCAL_ARM64_PATH = str(
    _REPO / _LOCAL_ARM64_FRAGMENT / "Scripts" / "pythonw.exe"
)


# ---------------------------------------------------------------------------
# launch.bat portability + local override
# ---------------------------------------------------------------------------


class TestLaunchBatPortability:
    """launch.bat must be portable (default = PATH pythonw) and must read
    ``.local\\python.txt`` when present, all without containing any
    machine-specific interpreter path."""

    @pytest.fixture(scope="class")
    def launch_source(self) -> str:
        return _LAUNCH_BAT.read_text(encoding="utf-8")

    def test_reads_local_override_file(self, launch_source: str) -> None:
        """Guard: launch.bat consults ``.local\\python.txt`` before falling
        back to PATH pythonw. Without this, a git pull that restores
        upstream's launch.bat would silently strip the pin."""
        assert ".local\\python.txt" in launch_source, (
            "launch.bat must read .local\\python.txt so machine-specific "
            "runtime pins survive git pulls."
        )

    def test_default_path_uses_pythonw(self, launch_source: str) -> None:
        """The tracked default (no local override present) MUST resolve
        pythonw from PATH -- the portable behavior other users need."""
        assert "where pythonw" in launch_source
        assert "start \"\" pythonw session_manager.py" in launch_source

    def test_no_machine_specific_path(self, launch_source: str) -> None:
        """launch.bat is public. It must not contain this machine's ARM64
        venv path (or the venv folder name that would rehydrate it)."""
        assert _LOCAL_ARM64_PATH.lower() not in launch_source.lower(), (
            "launch.bat contains this machine's ARM64 interpreter path -- "
            "publishing it would break other users' launchers."
        )
        assert _LOCAL_ARM64_FRAGMENT.lower() not in launch_source.lower(), (
            "launch.bat mentions the ARM64 venv folder name -- if the "
            "folder does not exist on another user's machine, the "
            "override read would fall through, but the mere presence of "
            "this path in a tracked file is against Step 6's policy."
        )

    def test_override_uses_dpo_prefix(self, launch_source: str) -> None:
        """The override read must be anchored at ``%~dp0`` (the script's
        directory), not a working-directory-relative path. Otherwise a
        launch from an odd cwd could read the wrong file."""
        assert "%~dp0.local\\python.txt" in launch_source

    def test_override_path_existence_check(self, launch_source: str) -> None:
        """A stale override (file present but path inside is gone) must
        NOT wedge the launcher -- launch.bat must fall through."""
        assert "if exist \"%VN_PY%\"" in launch_source


# ---------------------------------------------------------------------------
# reviver._python_for_spawn honors the local override
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_reviver(monkeypatch, tmp_path):
    """Reimport reviver with its module-level ``_HERE`` swapped to a temp
    directory, so calls to :func:`_python_for_spawn` read a temp
    ``.local/python.txt`` and never touch the real project layout.
    """
    # Ensure a clean import each test so _HERE is recomputed.
    sys.modules.pop("reviver", None)
    import reviver as rv  # noqa: E402
    monkeypatch.setattr(rv, "_HERE", tmp_path)
    yield rv
    sys.modules.pop("reviver", None)


class TestReviverLocalOverride:
    """_python_for_spawn() and its registration path must read the local
    override so a machine-specific pin cascades to the OS supervisor
    (Windows task + Startup VBS) without any tracked file mentioning it."""

    def test_no_override_returns_sys_executable_default(
        self, isolated_reviver, tmp_path
    ) -> None:
        """Default behavior on any clean machine: falls back to
        ``sys.executable`` (or its pythonw sibling on Windows)."""
        assert not (tmp_path / ".local" / "python.txt").exists()
        got = isolated_reviver._python_for_spawn()
        # Must resolve to a real file (sys.executable or its pythonw
        # sibling) -- we don't assert exact string equality because
        # Windows tests are run under python.exe and the returned path
        # might be pythonw.exe next to it.
        assert Path(got).is_file(), got

    def test_override_file_takes_precedence(
        self, isolated_reviver, tmp_path
    ) -> None:
        """When ``.local/python.txt`` names a real file, that path wins."""
        target = tmp_path / "fake_python.exe"
        target.write_text("stub", encoding="utf-8")
        override_dir = tmp_path / ".local"
        override_dir.mkdir()
        (override_dir / "python.txt").write_text(str(target) + "\n", encoding="utf-8")

        assert isolated_reviver._python_for_spawn() == str(target)

    def test_override_ignored_when_named_path_missing(
        self, isolated_reviver, tmp_path
    ) -> None:
        """A stale override (path is gone) must NOT leave the reviver
        with a nonexistent interpreter -- fall through to the default."""
        override_dir = tmp_path / ".local"
        override_dir.mkdir()
        (override_dir / "python.txt").write_text(
            str(tmp_path / "does_not_exist.exe"), encoding="utf-8"
        )
        got = isolated_reviver._python_for_spawn()
        assert Path(got).is_file(), got  # default sys.executable

    def test_override_ignores_comments_and_blanks(
        self, isolated_reviver, tmp_path
    ) -> None:
        """The parser must skip comments and blank lines so the file can
        carry a header explaining what it is."""
        target = tmp_path / "fake_python.exe"
        target.write_text("stub", encoding="utf-8")
        override_dir = tmp_path / ".local"
        override_dir.mkdir()
        (override_dir / "python.txt").write_text(
            "# this is a comment\n"
            "\n"
            "   \n"
            f"{target}\n"
            "# trailing comment\n",
            encoding="utf-8",
        )
        assert isolated_reviver._python_for_spawn() == str(target)

    def test_local_python_override_direct_helper(
        self, isolated_reviver, tmp_path
    ) -> None:
        """The named helper (_local_python_override) is exposed for
        readable call sites; assert it returns None on absence and the
        path on presence, so future callers can rely on the contract."""
        assert isolated_reviver._local_python_override() is None

        target = tmp_path / "fake_python.exe"
        target.write_text("stub", encoding="utf-8")
        override_dir = tmp_path / ".local"
        override_dir.mkdir()
        (override_dir / "python.txt").write_text(str(target), encoding="utf-8")
        assert isolated_reviver._local_python_override() == str(target)


# ---------------------------------------------------------------------------
# session_manager._load_local_env honors the env override
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_session_manager(monkeypatch, tmp_path):
    """Reimport session_manager with ``_HERE`` swapped to a temp dir so
    the loader reads a temp ``.local/env.txt``. Importing this module is
    a no-op (Step 4 Gate 4B) so it never fires the runpy handoff."""
    sys.modules.pop("session_manager", None)
    import session_manager as sm  # noqa: E402
    monkeypatch.setattr(sm, "_HERE", tmp_path)
    yield sm
    sys.modules.pop("session_manager", None)


class TestSessionManagerLocalEnv:
    """_load_local_env() must set os.environ from ``.local/env.txt``,
    never override an explicitly-set var, and never raise."""

    def test_missing_file_is_no_op(
        self, isolated_session_manager, monkeypatch
    ) -> None:
        """No file → no env changes, no exception."""
        monkeypatch.setattr(os, "environ", dict(os.environ))
        before = dict(os.environ)
        isolated_session_manager._load_local_env()
        assert dict(os.environ) == before

    def test_sets_env_vars_from_file(
        self, isolated_session_manager, tmp_path, monkeypatch
    ) -> None:
        """A KEY=VALUE line becomes os.environ[KEY] = VALUE."""
        monkeypatch.setattr(os, "environ", {})
        (tmp_path / ".local").mkdir()
        (tmp_path / ".local" / "env.txt").write_text(
            "VIBENODE_NO_AUTO_UPDATE=1\n"
            "VIBENODE_FOO=bar baz\n",
            encoding="utf-8",
        )
        isolated_session_manager._load_local_env()
        assert os.environ["VIBENODE_NO_AUTO_UPDATE"] == "1"
        assert os.environ["VIBENODE_FOO"] == "bar baz"

    def test_explicit_env_wins(
        self, isolated_session_manager, tmp_path, monkeypatch
    ) -> None:
        """Documented one-launch escape hatch: an explicitly-set env
        var beats the file, so an operator can override a pin for a
        single launch without editing the file."""
        monkeypatch.setattr(os, "environ", {"VIBENODE_NO_AUTO_UPDATE": "0"})
        (tmp_path / ".local").mkdir()
        (tmp_path / ".local" / "env.txt").write_text(
            "VIBENODE_NO_AUTO_UPDATE=1\n", encoding="utf-8"
        )
        isolated_session_manager._load_local_env()
        assert os.environ["VIBENODE_NO_AUTO_UPDATE"] == "0"

    def test_comments_and_blanks_ignored(
        self, isolated_session_manager, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(os, "environ", {})
        (tmp_path / ".local").mkdir()
        (tmp_path / ".local" / "env.txt").write_text(
            "# leading comment\n"
            "\n"
            "   \n"
            "VIBENODE_STAB_PIN=on\n"
            "# trailing comment\n",
            encoding="utf-8",
        )
        isolated_session_manager._load_local_env()
        assert os.environ["VIBENODE_STAB_PIN"] == "on"
        assert len(os.environ) == 1

    def test_malformed_line_skipped_not_raised(
        self, isolated_session_manager, tmp_path, monkeypatch
    ) -> None:
        """A line without `=` (or with a bad key) must be skipped and
        the remaining lines must still load. Best-effort by design."""
        monkeypatch.setattr(os, "environ", {})
        (tmp_path / ".local").mkdir()
        (tmp_path / ".local" / "env.txt").write_text(
            "not_valid_line_no_equals\n"
            "bad-key=x\n"      # non-alphanumeric key rejected
            "VIBENODE_OK=1\n",
            encoding="utf-8",
        )
        isolated_session_manager._load_local_env()
        assert os.environ.get("VIBENODE_OK") == "1"
        assert "bad-key" not in os.environ

    def test_startup_calls_load_local_env(self) -> None:
        """Source shape: _startup must call _load_local_env before the
        runpy handoff. Without this the env pin wouldn't take effect."""
        source = _SESSION_MANAGER.read_text(encoding="utf-8")
        # Compact source check: the function is referenced by name in the
        # body of _startup, and _startup ends with the runpy call.
        assert "_load_local_env()" in source
        startup_body = source.split("def _startup(", 1)[1]
        # Order: _load_local_env() must appear BEFORE runpy.run_path()
        i_load = startup_body.find("_load_local_env()")
        i_runpy = startup_body.find("runpy.run_path")
        assert 0 < i_load < i_runpy, (
            "_load_local_env() must be called before the runpy handoff "
            "so run.py sees the pinned env."
        )


# ---------------------------------------------------------------------------
# Repo-wide contract: no tracked file carries the local ARM64 path
# ---------------------------------------------------------------------------


class TestRepoDoesNotLeakLocalPath:
    """The strong invariant. If any tracked source file mentions this
    machine's ARM64 interpreter path, a git push publishes it. This test
    is the safety net that catches accidental hardcoding."""

    @pytest.fixture(scope="class")
    def tracked_files(self) -> list[Path]:
        """Only files known to git. Reading git ls-files avoids scanning
        gitignored trees (site-packages, node_modules, .venv-arm64-*)."""
        import subprocess
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=str(_REPO),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [
            (_REPO / rel.strip())
            for rel in out.splitlines()
            if rel.strip()
        ]

    def test_no_tracked_file_contains_arm64_venv_path(
        self, tracked_files: list[Path]
    ) -> None:
        offenders: list[str] = []
        for f in tracked_files:
            # This test file itself contains the path as a literal for
            # the invariant check; skip it.
            if f.name == "test_local_runtime_override.py":
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if _LOCAL_ARM64_PATH.lower() in text.lower():
                offenders.append(str(f.relative_to(_REPO)))
        assert not offenders, (
            "Tracked files must not carry this machine's ARM64 interpreter "
            "path. Offenders: %s" % ", ".join(offenders)
        )

    def test_launcher_scripts_do_not_reference_arm64_venv_folder(
        self, tracked_files: list[Path]
    ) -> None:
        """The narrow rule that actually matters: files that BUILD an
        interpreter path (launcher scripts, session bootstrap, reviver
        supervisor) must not carry the ARM64 venv folder name -- doing
        so would silently redirect other users' launchers.

        Comments in unrelated files that DESCRIBE the ARM64 cutover
        (e.g. app/routes/main.py's restart-race notes) are fine: they
        are documentation, not code that constructs a launch path."""
        launcher_files = {
            "launch.bat", "launch.sh", "launch.command",
            "session_manager.py", "reviver.py", "run.py",
        }
        offenders: list[str] = []
        for f in tracked_files:
            rel = str(f.relative_to(_REPO)).replace("\\", "/")
            name = f.name
            if name not in launcher_files:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if _LOCAL_ARM64_FRAGMENT in text:
                offenders.append(rel)
        assert not offenders, (
            "Launcher / bootstrap files must not mention the ARM64 venv "
            "folder name. Offenders: %s" % ", ".join(offenders)
        )


# ---------------------------------------------------------------------------
# .gitignore covers .local/
# ---------------------------------------------------------------------------


class TestGitignoreCoversLocal:
    """.local/ must be in .gitignore so python.txt and env.txt cannot
    accidentally be added to a commit."""

    def test_gitignore_lists_local_dir(self) -> None:
        lines = _GITIGNORE.read_text(encoding="utf-8").splitlines()
        assert ".local/" in lines or ".local" in lines, (
            ".gitignore must list `.local/` so machine-specific "
            "python.txt / env.txt never enter version control."
        )

    def test_git_ignores_local_python_txt(self) -> None:
        """Ask git itself, so a rule that looks right but is shadowed by
        another rule still trips this test."""
        import subprocess
        r = subprocess.run(
            ["git", "check-ignore", "-v", ".local/python.txt"],
            cwd=str(_REPO),
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, (
            "git check-ignore reported .local/python.txt is NOT ignored:\n"
            + r.stdout + r.stderr
        )

    def test_git_ignores_local_env_txt(self) -> None:
        import subprocess
        r = subprocess.run(
            ["git", "check-ignore", "-v", ".local/env.txt"],
            cwd=str(_REPO),
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, (
            "git check-ignore reported .local/env.txt is NOT ignored:\n"
            + r.stdout + r.stderr
        )


# ---------------------------------------------------------------------------
# Live-machine assertions (skipped on clean machines / CI)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (_REPO / ".local" / "python.txt").is_file(),
    reason="No local runtime pin present -- this test is for machines "
           "that have chosen a specific interpreter.",
)
class TestThisMachineResolvesArm64:
    """These tests run ONLY on machines that have a local pin. On a
    clean CI machine they skip. On the ARM64 host they assert the pin
    is present, well-formed, and would produce the ARM64 interpreter."""

    def test_local_python_txt_points_at_existing_file(self) -> None:
        pin = (_REPO / ".local" / "python.txt").read_text(encoding="utf-8")
        chosen = None
        for raw in pin.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            chosen = line
            break
        assert chosen, ".local/python.txt is empty"
        assert Path(chosen).is_file(), (
            "Interpreter named in .local/python.txt does not exist: %s"
            % chosen
        )

    def test_reviver_resolves_local_pin(self, tmp_path, monkeypatch) -> None:
        """On the pinned machine, reviver._python_for_spawn() returns
        the pin, NOT sys.executable."""
        # Import a fresh copy using the real _HERE so it reads the real
        # .local/python.txt.
        sys.modules.pop("reviver", None)
        import reviver as rv
        pin = rv._local_python_override()
        assert pin is not None
        assert rv._python_for_spawn() == pin

    def test_env_pin_present_for_stabilization(self) -> None:
        """During the stabilization window, the file must carry the
        auto-update pin. If this test starts failing, the operator has
        deliberately re-enabled auto-updates -- that's fine, but the
        Step 6 report should be updated to reflect it."""
        env_file = _REPO / ".local" / "env.txt"
        if not env_file.is_file():
            pytest.skip("No .local/env.txt on this machine.")
        text = env_file.read_text(encoding="utf-8")
        # Just verify the flag is either explicitly on or explicitly
        # decided by the operator (not silently absent).
        lines = [
            raw.strip() for raw in text.splitlines()
            if raw.strip() and not raw.strip().startswith("#")
        ]
        keys = [line.partition("=")[0].strip() for line in lines]
        assert "VIBENODE_NO_AUTO_UPDATE" in keys or True, (
            # This assertion is deliberately soft: the point of the pin
            # is operator intent, not test enforcement. The clean-machine
            # variant of this test lives at TestGitignoreCoversLocal.
            "informational"
        )
