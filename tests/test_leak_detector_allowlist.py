"""ALLOWLIST GUARD for ``scripts/leak_detector.py``.

The detector is only as good as its allowlist, and that allowlist has been wrong
twice -- both times silently, both times in the direction of seeing nothing:

1. ``[/\\]code\\b`` (intended: VS Code) also matched any checkout living under a
   ``code/`` directory, so EVERY process launched from there was exempt --
   precisely the leaks the tool exists to find. It reported "clean" with 64
   orphaned CPU burners on the box.
2. Re-anchoring those patterns to ``^`` (argv[0]) then broke the opposite way: a
   script normally appears as argv[1] behind its interpreter
   (``/usr/bin/python3 .../reviver.py``), so ``^\\S*[/\\]reviver\\.py`` matched
   nothing and several daemons silently lost their exemption.

Both directions are failures and neither is visible by running the tool: a
too-broad allowlist prints "clean" while the box melts, and a too-narrow one is
invisible until a legitimate daemon gets reported to a human who then kills it.
Hence this matrix.

**When you add an ALLOWLIST entry, add a case here** -- one proving the intended
process matches, and one proving a leak at a similar-looking path does NOT.

Paths below are synthesised from a fake home so this file stays free of any
machine-specific or personal path (this is a public repository).

Run: python -m pytest tests/test_leak_detector_allowlist.py -q
Static only: imports the module and matches strings. Spawns no processes.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_DETECTOR = REPO_ROOT / "scripts" / "leak_detector.py"

# A plausible-but-fictional layout: a home dir, a checkout under `code/`, and a
# repo. The `code/` segment is deliberate -- it is what broke the allowlist the
# first time.
HOME = "/home/devuser"
CHECKOUT = f"{HOME}/code/someproject"


def _load():
    spec = importlib.util.spec_from_file_location("_leak_detector", _DETECTOR)
    assert spec and spec.loader, f"cannot load {_DETECTOR}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_leak_detector"] = mod  # dataclasses need this registered
    spec.loader.exec_module(mod)
    return mod


# MUST stay exempt. The first few ARE the session host: acting on a report for
# those aborts every running session, so a regression here is a P0.
MUST_ALLOW = [
    f"/usr/bin/python3 {HOME}/code/vibenode/VibeNode/reviver.py --guardian",
    "/usr/bin/python3 /opt/vibenode-prelogin/prelogin.py",
    "python [VibeNode-DO-NOT-KILL:web-server--active-AI-session-host]",
    f"{HOME}/.local/bin/claude --output-format stream-json",
    f"/bin/bash -c source {HOME}/.claude/shell-snapshots/snapshot-bash-1.sh",
    "/usr/share/code/code --type=zygote",
    "/opt/google/chrome/chrome --type=renderer",
    "/usr/local/bin/python3.12 /usr/local/bin/gunicorn myapp.wsgi:application",
    "/usr/local/bin/python3.12 /usr/local/bin/daphne -b 0.0.0.0 -p 8001",
    "python /app/watchdog.py",
    "/usr/bin/redis-server 127.0.0.1:6390",
    "postgres: 16/main: autovacuum launcher",
    "/usr/lib/postgresql/16/bin/postgres -D /var/lib/postgresql/data",
    "/usr/bin/containerd",
    "/usr/sbin/tailscaled --state=/var/lib/tailscale/tailscaled.state",
    "/usr/lib/xorg/Xorg -core :0",
]

# MUST stay visible. Several are shaped to sit at paths a sloppy entry swallows.
MUST_FLAG = [
    "python3 -c while True: pass",
    f"bash {CHECKOUT}/scripts/repro_load.sh",          # the incident's own shape
    f"python3 {CHECKOUT}/some_leaked_script.py",
    f"python3 {CHECKOUT}/.claude/hooks/burner.py",     # hooks can background
    "python3 /tmp/scratch/.claude/burner.py",
    "python3 /tmp/chromium_bench.py",                  # resembles `chromium`
    "python3 /tmp/gunicorn_burner.py",                 # resembles `gunicorn`
    "/tmp/postgres_loadgen --spin",                    # resembles `postgres`
]


@pytest.mark.parametrize("cmdline", MUST_ALLOW)
def test_allowlisted_processes_are_never_reported(cmdline: str) -> None:
    mod = _load()
    assert mod._ALLOW_RE.search(cmdline), (
        f"NOT allowlisted, so the detector would report it: {cmdline!r}\n"
        "If this is the session host, a human acting on that report aborts every "
        "running session. Note a script usually appears as argv[1] behind its "
        "interpreter, so `^` alone is the wrong anchor."
    )


@pytest.mark.parametrize("cmdline", MUST_FLAG)
def test_leaks_are_not_swallowed_by_the_allowlist(cmdline: str) -> None:
    mod = _load()
    assert not mod._ALLOW_RE.search(cmdline), (
        f"allowlisted, so this leak would be INVISIBLE: {cmdline!r}\n"
        "An over-broad pattern makes the detector print 'clean' while the box "
        "melts. Anchor to a path-component boundary, and check every new pattern "
        "against a checkout path and a /tmp path."
    )


def test_no_allowlist_entry_matches_a_plain_checkout_path() -> None:
    """The bug that started this file, pinned directly."""
    mod = _load()
    offenders = [
        p for p in mod.ALLOWLIST
        if re.search(p, f"python3 {CHECKOUT}/x.py", re.IGNORECASE)
    ]
    assert not offenders, (
        "ALLOWLIST entries match a plain checkout path, exempting everything "
        f"launched from a working copy: {offenders}"
    )


def test_this_file_contains_no_machine_specific_paths() -> None:
    """Public repo: no real home dirs or usernames may be committed here."""
    text = Path(__file__).read_text(encoding="utf-8")
    leaked = re.findall(r"/home/(?!devuser\b)[A-Za-z0-9_.-]+", text)
    assert not leaked, f"personal paths must not be committed: {sorted(set(leaked))}"
