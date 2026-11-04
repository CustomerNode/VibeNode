"""The working tree passes the Publish scan, checked whenever the suite runs.

Without this, the Publish scan (src/web/git_scanner.py) would be the first thing to see a real name or
key a session had written, and Update would fail.  The Session Engine refuses such a write as it happens (src/engine/publish_guard.py); this test catches
the ones written another way (a shell command, an older engine) the next time anyone runs the tests,
with the same findings Publish would show."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_working_tree_passes_the_publish_scan():
    from web import git_scanner as g
    res = g.scan_staged_files(ROOT)
    lines = [f"{f['file']}:{f['line']} {f['type']} ({f['match']})" for f in res.get("findings") or []]
    lines += [f"{b['file']}: {b['reason']}" for b in res.get("blocked_files") or []]
    assert res.get("ok"), (
        "Publish would be blocked. Replace real names, addresses, ids and keys with made-up ones; "
        "a false positive in a source pattern goes in the skip list in app/git_scanner.py:\n  "
        + "\n  ".join(lines or [res.get("summary", "")]))
