"""
Tests for ``app.update_safety.upgrade_sdk_safely``.

These tests NEVER invoke a real pip subprocess. Every call routes through
an injected ``runner`` callable that returns a scripted (returncode, output)
tuple, so:

  * The test suite cannot mutate any real Python environment.
  * ARM64 wheel-missing scenarios can be simulated exactly by scripting the
    dry-run to return pip's real "No matching distribution found" message.
  * Assertions can pin the exact pip command line — both the flags used
    and the interpreter path — which is what matters for the
    architecture-agnostic safety property (``--only-binary=:all:`` on
    every install).

Coverage mirrors the user's Step 2 correction checklist:

  1. Shared x64 environment — clean upgrade path
  2. Dedicated x64 venv — full upgrade allowed
  3. Dedicated Windows ARM64 venv — full upgrade allowed WHEN wheels exist
  4. ARM64 case where cryptography cannot be resolved — blocked cleanly
  5. SDK release with unchanged dependencies — proceeds
  6. SDK release that changes a dependency — proceeds in venv, blocked in shared
  7. pip check failure post-install — blocked_inconsistent
  8. No unsafe downgrade or source build — ``--only-binary=:all:`` everywhere,
     no ``pin cryptography==46.0.3`` fallback anywhere in the module.
"""

import re

import pytest

from app import update_safety


# ---------------------------------------------------------------------------
# Scripted runner
# ---------------------------------------------------------------------------

class ScriptedRunner:
    """A programmable fake for the (cmd, timeout) -> (rc, output) callable.

    Programmable per pip subcommand:
      * ``show`` — set via ``set_version(pkg, version)``.
      * ``install --dry-run …`` — set via ``script_dry_run(rc, output)``.
      * ``install …`` (real) — set via ``script_install(rc, output)``. Also
        auto-bumps the pip-show version if the real install succeeded (rc 0)
        and ``bump_version_on_install`` was set.
      * ``check`` — set via ``script_check(rc, output)``.

    Every call is recorded in ``self.calls`` for later assertion.
    """

    def __init__(self):
        self.calls = []
        self._show_versions = {}
        self._dry_run_response = (0, "")
        self._install_response = (0, "")
        self._check_response = (0, "")
        self._bump_after_install = None

    def set_version(self, pkg, version):
        self._show_versions[pkg] = version

    def bump_version_on_install(self, pkg, new_version):
        """When the next real install call fires and returns rc==0, flip
        pip-show's stored version to ``new_version`` — simulating an
        actual upgrade taking effect on disk."""
        self._bump_after_install = (pkg, new_version)

    def script_dry_run(self, response):
        self._dry_run_response = response

    def script_install(self, response):
        self._install_response = response

    def script_check(self, response):
        self._check_response = response

    def __call__(self, cmd, timeout):
        self.calls.append({"cmd": list(cmd), "timeout": timeout})
        if len(cmd) >= 4 and cmd[1:3] == ["-m", "pip"]:
            op = cmd[3]
            if op == "show":
                pkg = cmd[4]
                ver = self._show_versions.get(pkg)
                if ver is None:
                    return (1, "")
                return (0, f"Name: {pkg}\nVersion: {ver}\n")
            if op == "install":
                # Distinguish dry-run from real install by scanning args.
                if "--dry-run" in cmd:
                    return self._dry_run_response
                rc, out = self._install_response
                if rc == 0 and self._bump_after_install:
                    pkg, new_ver = self._bump_after_install
                    self._show_versions[pkg] = new_ver
                    self._bump_after_install = None
                return (rc, out)
            if op == "check":
                return self._check_response
        return (0, "")

    def all_pip_commands(self):
        return [c["cmd"] for c in self.calls
                if len(c["cmd"]) >= 3 and c["cmd"][1:3] == ["-m", "pip"]]

    def install_calls(self):
        return [c for c in self.all_pip_commands()
                if len(c) >= 4 and c[3] == "install"]

    def dry_run_calls(self):
        return [c for c in self.install_calls() if "--dry-run" in c]

    def real_install_calls(self):
        return [c for c in self.install_calls() if "--dry-run" not in c]

    def check_calls(self):
        return [c for c in self.all_pip_commands()
                if len(c) >= 4 and c[3] == "check"]


@pytest.fixture
def runner():
    return ScriptedRunner()


# Constants used in multiple tests.
_SHARED_X64 = r"C:\Python314\python.exe"
_VENV_X64 = r"C:\Users\x\proj\.venv\Scripts\python.exe"
_VENV_ARM64 = r"C:\Users\x\proj\.venv\Scripts\python.exe"  # same path shape;
# ARM64 vs x64 is a build-of-Python distinction the tests don't need to
# reproduce here — the updater's arch-safety comes from ``--only-binary=:all:``,
# which we assert directly.


# Realistic pip output samples.
_DRY_RUN_SDK_ONLY = """\
Requirement already satisfied: anyio in .../site-packages (4.13.0)
Requirement already satisfied: mcp in .../site-packages (1.27.0)
Collecting claude-code-sdk
  Downloading claude_code_sdk-0.0.26-py3-none-any.whl
Would install claude-code-sdk-0.0.26
"""

_DRY_RUN_SDK_AND_DEPS = """\
Collecting claude-code-sdk
  Downloading claude_code_sdk-0.0.26-py3-none-any.whl
Collecting mcp>=1.28.0
  Downloading mcp-1.28.0-py3-none-any.whl
Collecting anyio>=4.14.0
  Downloading anyio-4.14.0-py3-none-any.whl
Would install anyio-4.14.0 claude-code-sdk-0.0.26 mcp-1.28.0
"""

_DRY_RUN_ALREADY_SATISFIED = """\
Requirement already satisfied: claude-code-sdk in .../site-packages (0.0.25)
Requirement already satisfied: anyio in .../site-packages (4.13.0)
Requirement already satisfied: mcp in .../site-packages (1.27.0)
"""

_DRY_RUN_ARM64_CRYPTO_MISSING = """\
Collecting claude-code-sdk
  Downloading claude_code_sdk-0.0.26-py3-none-any.whl
Collecting mcp>=1.28.0 (from claude-code-sdk)
  Downloading mcp-1.28.0-py3-none-any.whl
Collecting pyjwt[crypto]>=2.4 (from mcp>=1.28.0->claude-code-sdk)
  Downloading pyjwt-2.9.0-py3-none-any.whl
Collecting cryptography>=46.0.7 (from pyjwt[crypto]>=2.4)
ERROR: Could not find a version that satisfies the requirement cryptography>=46.0.7 (from pyjwt[crypto]) (from versions: 46.0.0, 46.0.1, 46.0.2, 46.0.3)
ERROR: No matching distribution found for cryptography>=46.0.7
"""


# ===========================================================================
# Feature gates
# ===========================================================================

def test_env_flag_disables_upgrade_entirely(runner):
    result = update_safety.upgrade_sdk_safely(
        interpreter=_SHARED_X64, runner=runner,
        env={"VIBENODE_NO_AUTO_UPDATE": "1"},
    )
    assert result["status"] == update_safety.STATUS_SKIPPED_BY_ENV
    assert runner.calls == []


def test_pip_only_flag_disables_pip_step(runner):
    result = update_safety.upgrade_sdk_safely(
        interpreter=_SHARED_X64, runner=runner,
        env={"VIBENODE_NO_PIP_UPDATE": "1"},
    )
    assert result["status"] == update_safety.STATUS_SKIPPED_BY_ENV
    assert runner.calls == []


def test_isolated_override_forces_venv_treatment(runner):
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_ONLY))
    runner.script_install((0, "Successfully installed claude-code-sdk-0.0.26"))
    runner.bump_version_on_install("claude-code-sdk", "0.0.26")
    result = update_safety.upgrade_sdk_safely(
        interpreter=_SHARED_X64,  # shared-looking path
        runner=runner, env={"VIBENODE_UPDATE_ISOLATED": "1"},
    )
    assert result["isolated"] is True


# ===========================================================================
# Isolation detection
# ===========================================================================

def test_venv_path_heuristic_detects_isolation():
    assert update_safety.is_isolated_interpreter(
        r"C:\Users\x\project\.venv\Scripts\pythonw.exe", env={}) is True


def test_shared_python_path_treated_as_not_isolated():
    assert update_safety.is_isolated_interpreter(_SHARED_X64, env={}) is False


# ===========================================================================
# The core arch-safety invariant: --only-binary=:all: is present everywhere
# ===========================================================================

def test_only_binary_flag_is_on_dry_run_shared(runner):
    """The dry-run pip call must always pass --only-binary=:all:, in every
    environment. That is the load-bearing flag that stops any source build
    attempt on ARM64."""
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_ONLY))
    runner.script_install((0, "Successfully installed"))
    update_safety.upgrade_sdk_safely(
        interpreter=_SHARED_X64, runner=runner, env={},
    )
    for c in runner.dry_run_calls():
        assert "--only-binary=:all:" in c


def test_only_binary_flag_is_on_dry_run_isolated(runner):
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_ONLY))
    runner.script_install((0, "Successfully installed"))
    update_safety.upgrade_sdk_safely(
        interpreter=_VENV_X64, runner=runner, env={},
    )
    for c in runner.dry_run_calls():
        assert "--only-binary=:all:" in c


def test_only_binary_flag_is_on_real_install_shared(runner):
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_ONLY))
    runner.script_install((0, "Successfully installed"))
    runner.bump_version_on_install("claude-code-sdk", "0.0.26")
    update_safety.upgrade_sdk_safely(
        interpreter=_SHARED_X64, runner=runner, env={},
    )
    for c in runner.real_install_calls():
        assert "--only-binary=:all:" in c


def test_only_binary_flag_is_on_real_install_isolated(runner):
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_ONLY))
    runner.script_install((0, "Successfully installed"))
    runner.bump_version_on_install("claude-code-sdk", "0.0.26")
    update_safety.upgrade_sdk_safely(
        interpreter=_VENV_X64, runner=runner, env={},
    )
    for c in runner.real_install_calls():
        assert "--only-binary=:all:" in c


def test_never_quiet_anywhere(runner):
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_ONLY))
    runner.script_install((0, "Successfully installed"))
    runner.bump_version_on_install("claude-code-sdk", "0.0.26")
    for interp in (_SHARED_X64, _VENV_X64):
        r = ScriptedRunner()
        r.set_version("claude-code-sdk", "0.0.25")
        r.script_dry_run((0, _DRY_RUN_SDK_ONLY))
        r.script_install((0, "Successfully installed"))
        r.bump_version_on_install("claude-code-sdk", "0.0.26")
        update_safety.upgrade_sdk_safely(
            interpreter=interp, runner=r, env={},
        )
        for c in r.install_calls():
            assert "--quiet" not in c


def test_module_never_pins_cryptography_46_0_3():
    """Guardrail: the fix for ARM64 must never be 'pin cryptography to the
    last version that had an ARM64 wheel (46.0.3)'. That reintroduces
    three CVEs. Grep the module source and refuse if that pin ever
    appears.
    """
    import inspect
    src = inspect.getsource(update_safety)
    # We're allowed to MENTION 46.0.3 in a comment (e.g. as documentation),
    # but never as a version pin. The specific patterns we forbid are the
    # ones pip would accept as a downgrade:
    forbidden_pins = (
        "cryptography==46.0.3", "cryptography<=46.0.3",
        "cryptography<46.0.4", "cryptography~=46.0.3",
    )
    for needle in forbidden_pins:
        assert needle not in src, (
            f"update_safety.py must never pin {needle!r} — that would trade "
            f"three CVEs for build convenience. Report only that a manual "
            f"dependency update is required."
        )


# ===========================================================================
# 1. Shared x64: clean upgrade (no other deps change)
# ===========================================================================

def test_shared_x64_clean_upgrade(runner):
    """Baseline case A: shared x64 today. Dry-run shows only the SDK
    changes; real install passes --no-deps to avoid touching neighbours."""
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_ONLY))
    runner.script_install((0, "Successfully installed claude-code-sdk-0.0.26"))
    runner.bump_version_on_install("claude-code-sdk", "0.0.26")
    result = update_safety.upgrade_sdk_safely(
        interpreter=_SHARED_X64, runner=runner, env={},
    )
    assert result["status"] == update_safety.STATUS_UPGRADED
    assert result["isolated"] is False
    real = runner.real_install_calls()
    assert len(real) == 1
    assert "--no-deps" in real[0]
    assert "--only-binary=:all:" in real[0]
    assert result["before_version"] == "0.0.25"
    assert result["after_version"] == "0.0.26"


# ===========================================================================
# 2. Dedicated x64 venv: full upgrade allowed
# ===========================================================================

def test_dedicated_x64_venv_full_upgrade(runner):
    """A dedicated x64 venv can safely upgrade transitive deps too. The
    dry-run plan may include multiple packages; the real install proceeds
    without --no-deps."""
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_AND_DEPS))
    runner.script_install((0,
        "Successfully installed anyio-4.14.0 claude-code-sdk-0.0.26 mcp-1.28.0"))
    runner.bump_version_on_install("claude-code-sdk", "0.0.26")
    result = update_safety.upgrade_sdk_safely(
        interpreter=_VENV_X64, runner=runner, env={},
    )
    assert result["status"] == update_safety.STATUS_UPGRADED
    assert result["isolated"] is True
    real = runner.real_install_calls()
    assert len(real) == 1
    assert "--no-deps" not in real[0]
    assert "--only-binary=:all:" in real[0]


# ===========================================================================
# 3. Dedicated ARM64 venv: full upgrade allowed when wheels exist
# ===========================================================================

def test_dedicated_arm64_venv_full_upgrade_when_wheels_exist(runner):
    """The dedicated ARM64 venv is not distinguishable at the API level
    from a dedicated x64 venv — the safety difference is whether required
    wheels exist for the target arch. When they do (the healthy case),
    the upgrade proceeds identically to case #2.
    """
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_AND_DEPS))
    runner.script_install((0, "Successfully installed"))
    runner.bump_version_on_install("claude-code-sdk", "0.0.26")
    result = update_safety.upgrade_sdk_safely(
        interpreter=_VENV_ARM64,
        runner=runner,
        env={"VIBENODE_UPDATE_ISOLATED": "1"},  # ARM64 launcher signal
    )
    assert result["status"] == update_safety.STATUS_UPGRADED
    assert result["isolated"] is True
    for c in runner.install_calls():
        assert "--only-binary=:all:" in c   # arch-safety enforced


# ===========================================================================
# 4. ARM64 with cryptography wheel missing: blocked cleanly
# ===========================================================================

def test_arm64_cryptography_wheel_missing_blocks_without_touching_env(runner):
    """The Step 2 correction target case: a new SDK release pulls a new
    pyjwt[crypto] that needs a cryptography version with no ARM64 wheel.
    ``--only-binary=:all:`` turns this into a clean dry-run failure. The
    updater MUST:
      * report STATUS_BLOCKED_MISSING_WHEEL
      * never invoke the real install command
      * leave before_version == on-disk version (nothing changed)
      * surface pip's actual error line in reason
    """
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((1, _DRY_RUN_ARM64_CRYPTO_MISSING))
    # If the real install were called, it would return this fake success.
    # The whole point of the test is that it MUST NOT be called.
    runner.script_install((0, "SHOULD NOT BE INVOKED"))
    runner.bump_version_on_install("claude-code-sdk", "SHOULD-NOT-CHANGE")

    result = update_safety.upgrade_sdk_safely(
        interpreter=_VENV_ARM64,
        runner=runner,
        env={"VIBENODE_UPDATE_ISOLATED": "1"},
    )
    assert result["status"] == update_safety.STATUS_BLOCKED_MISSING_WHEEL
    # Env untouched: no real install, no pip check.
    assert runner.real_install_calls() == []
    assert runner.check_calls() == []
    # Version unchanged.
    assert result["before_version"] == "0.0.25"
    assert result["after_version"] is None
    # Reason names the offending package.
    assert "cryptography" in result["reason"].lower()
    # And carries pip's original wording.
    assert "no matching distribution" in result["dry_run_output_tail"].lower()


def test_shared_env_cryptography_wheel_missing_blocks_too(runner):
    """Same failure in the shared env path: the dry-run's arch check
    fires before we even reach the shared-vs-isolated decision."""
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((1, _DRY_RUN_ARM64_CRYPTO_MISSING))
    runner.script_install((0, "SHOULD NOT BE INVOKED"))
    result = update_safety.upgrade_sdk_safely(
        interpreter=_SHARED_X64, runner=runner, env={},
    )
    assert result["status"] == update_safety.STATUS_BLOCKED_MISSING_WHEEL
    assert runner.real_install_calls() == []


# ===========================================================================
# 5. SDK release with unchanged dependencies: proceeds
# ===========================================================================

def test_sdk_release_with_unchanged_deps_proceeds(runner):
    """The dry-run reports 'Would install claude-code-sdk-X' with no other
    packages. Both shared env and venv should upgrade cleanly."""
    for interp, is_iso in [(_SHARED_X64, False), (_VENV_X64, True)]:
        r = ScriptedRunner()
        r.set_version("claude-code-sdk", "0.0.25")
        r.script_dry_run((0, _DRY_RUN_SDK_ONLY))
        r.script_install((0, "Successfully installed claude-code-sdk-0.0.26"))
        r.bump_version_on_install("claude-code-sdk", "0.0.26")
        result = update_safety.upgrade_sdk_safely(
            interpreter=interp, runner=r, env={},
        )
        assert result["status"] == update_safety.STATUS_UPGRADED, (
            f"{interp} should upgrade cleanly, got {result['status']} / "
            f"{result['reason']}"
        )
        assert result["isolated"] is is_iso
        assert result["dry_run_plan"] == ["claude-code-sdk"]


def test_already_up_to_date_reports_no_change(runner):
    """When pip dry-run says 'Requirement already satisfied' (no 'Would
    install' line), the updater must still record a clean pass."""
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_ALREADY_SATISFIED))
    runner.script_install((0, "Requirement already satisfied: claude-code-sdk"))
    result = update_safety.upgrade_sdk_safely(
        interpreter=_VENV_X64, runner=runner, env={},
    )
    assert result["status"] == update_safety.STATUS_NO_CHANGE
    assert result["before_version"] == "0.0.25"
    assert result["after_version"] == "0.0.25"


# ===========================================================================
# 6. SDK release that changes a dependency: proceeds in venv, blocked in shared
# ===========================================================================

def test_shared_env_blocks_when_deps_would_change(runner):
    """Shared env: if the dry-run plan touches anything beyond the SDK,
    refuse. Reason must name the offending packages so the operator knows
    what to do (migrate to a venv or update manually).
    """
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_AND_DEPS))
    runner.script_install((0, "SHOULD NOT BE INVOKED"))
    result = update_safety.upgrade_sdk_safely(
        interpreter=_SHARED_X64, runner=runner, env={},
    )
    assert result["status"] == update_safety.STATUS_BLOCKED_SHARED_ENV_DEP_CHANGE
    assert runner.real_install_calls() == []
    # Reason names the neighbours that would move.
    reason_low = result["reason"].lower()
    assert "mcp" in reason_low
    assert "anyio" in reason_low
    # And still tells the user what to do about it.
    assert "venv" in reason_low or "manual" in reason_low


def test_venv_proceeds_when_deps_would_change(runner):
    """Dedicated venv: same dry-run plan; the venv can safely accept it."""
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_AND_DEPS))
    runner.script_install((0, "Successfully installed"))
    runner.bump_version_on_install("claude-code-sdk", "0.0.26")
    result = update_safety.upgrade_sdk_safely(
        interpreter=_VENV_X64, runner=runner, env={},
    )
    assert result["status"] == update_safety.STATUS_UPGRADED
    assert result["dry_run_plan"] == ["anyio", "claude-code-sdk", "mcp"]


# ===========================================================================
# 7. pip check failure after install: blocked_inconsistent
# ===========================================================================

def test_pip_check_failure_referencing_sdk_reports_inconsistent(runner):
    """The rare case: dry-run passed but pip check flags an SDK dependency
    as unsatisfied post-install. We must surface this as
    STATUS_BLOCKED_INCONSISTENT with the offending line in ``reason``,
    NOT silently claim success.
    """
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_ONLY))
    runner.script_install((0, "Successfully installed claude-code-sdk-0.0.26"))
    runner.bump_version_on_install("claude-code-sdk", "0.0.26")
    runner.script_check((
        1,
        "claude-code-sdk 0.0.26 requires mcp>=999, but you have mcp 1.27.0",
    ))
    result = update_safety.upgrade_sdk_safely(
        interpreter=_SHARED_X64, runner=runner, env={},
    )
    assert result["status"] == update_safety.STATUS_BLOCKED_INCONSISTENT
    assert "manual" in result["reason"].lower()
    # The offending package name appears verbatim.
    assert "mcp" in result["reason"]


def test_pip_check_failure_unrelated_to_sdk_is_ignored(runner):
    """A pre-existing pip check error in the shared env that has nothing
    to do with our SDK must NOT poison a successful upgrade result."""
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_ONLY))
    runner.script_install((0, "Successfully installed"))
    runner.bump_version_on_install("claude-code-sdk", "0.0.26")
    runner.script_check((
        1,
        "unrelated-lib 1.0 requires other-thing>=2.0, but you have other-thing 1.5.",
    ))
    result = update_safety.upgrade_sdk_safely(
        interpreter=_SHARED_X64, runner=runner, env={},
    )
    assert result["status"] == update_safety.STATUS_UPGRADED


# ===========================================================================
# 8. No unsafe downgrade or source-build attempt
# ===========================================================================

def test_no_source_build_flag_leaks_anywhere(runner):
    """--no-binary is the flag that WOULD invite a source build. It must
    never appear in any install command line the updater produces.
    """
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_ONLY))
    runner.script_install((0, "Successfully installed"))
    runner.bump_version_on_install("claude-code-sdk", "0.0.26")
    for interp in (_SHARED_X64, _VENV_X64):
        r = ScriptedRunner()
        r.set_version("claude-code-sdk", "0.0.25")
        r.script_dry_run((0, _DRY_RUN_SDK_ONLY))
        r.script_install((0, "Successfully installed"))
        r.bump_version_on_install("claude-code-sdk", "0.0.26")
        update_safety.upgrade_sdk_safely(
            interpreter=interp, runner=r, env={},
        )
        for c in r.install_calls():
            joined = " ".join(c)
            assert "--no-binary" not in joined, (
                f"install command must never allow source builds: {c}"
            )


def test_updater_never_issues_downgrade_command():
    """No code path in the module should ever emit ``pip install pkg==0.x.y``
    (a downgrade pin). The design is 'refuse and report'; downgrade is a
    manual operator decision.
    """
    import inspect
    src = inspect.getsource(update_safety)
    # Match any explicit version-pin install like ``pkg==0.0.3`` in the
    # module source. Comments/docstrings referencing 46.0.3 as history
    # are fine; a pin-op is not.
    pin_pattern = re.compile(r'"[^"]*==\d+\.\d+\.\d+[^"]*"|\'[^\']*==\d+\.\d+\.\d+[^\']*\'')
    assert not pin_pattern.search(src), (
        "update_safety.py must not embed any version pin — "
        "pinning is a manual operator decision."
    )


# ===========================================================================
# The interpreter path is honored (never accidentally shells out to another Python)
# ===========================================================================

def test_interpreter_arg_is_the_one_pip_runs_under(runner):
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_ONLY))
    runner.script_install((0, "Successfully installed"))
    runner.bump_version_on_install("claude-code-sdk", "0.0.26")
    target = r"D:\some\other\python.exe"
    update_safety.upgrade_sdk_safely(
        interpreter=target, runner=runner,
        env={"VIBENODE_UPDATE_ISOLATED": "1"},
    )
    for call in runner.calls:
        assert call["cmd"][0] == target


# ===========================================================================
# Result shape stability
# ===========================================================================

def test_result_shape_is_stable(runner):
    runner.set_version("claude-code-sdk", "0.0.25")
    runner.script_dry_run((0, _DRY_RUN_SDK_ONLY))
    runner.script_install((0, "Successfully installed"))
    runner.bump_version_on_install("claude-code-sdk", "0.0.26")
    result = update_safety.upgrade_sdk_safely(
        interpreter=_SHARED_X64, runner=runner, env={},
    )
    for key in (
        "status", "interpreter", "isolated",
        "dry_run_returncode", "dry_run_output_tail", "dry_run_plan",
        "pip_returncode", "pip_output_tail", "pip_command",
        "pip_check_returncode", "pip_check_output_tail",
        "before_version", "after_version", "reason",
    ):
        assert key in result, f"missing key in result: {key}"
    assert result["reason"], "reason must always be non-empty"


# ===========================================================================
# Blocked outcomes are never labelled 'upgraded'
# ===========================================================================

def test_blocked_statuses_never_equal_upgraded():
    """A quick property assertion: no blocked constant collides with
    the upgraded constant, so downstream ``if status == 'upgraded'``
    filters can never accept a blocked outcome.
    """
    for blocked in (
        update_safety.STATUS_BLOCKED_MISSING_WHEEL,
        update_safety.STATUS_BLOCKED_SHARED_ENV_DEP_CHANGE,
        update_safety.STATUS_BLOCKED_INCONSISTENT,
        update_safety.STATUS_FAILED,
        update_safety.STATUS_SKIPPED_BY_ENV,
    ):
        assert blocked != update_safety.STATUS_UPGRADED
        assert blocked != update_safety.STATUS_NO_CHANGE
