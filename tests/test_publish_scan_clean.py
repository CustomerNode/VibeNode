"""The working tree passes the Publish scan, checked whenever the suite runs.

Without this, the Publish scan (src/web/git_scanner.py) would be the first thing to see a real name or
key a session had written, and Update would fail.  The Session Engine refuses such a write as it happens (src/engine/publish_guard.py); this test catches
the ones written another way (a shell command, an older engine) the next time anyone runs the tests,
with the same findings Publish would show."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_working_tree_passes_the_publish_scan(monkeypatch):
    from web import git_scanner as g
    # The real list of org members' names (the suite points it elsewhere).
    monkeypatch.setattr(g, "ORG_NAMES_FILE", ROOT / "data" / "org_names.json")
    res = g.scan_staged_files(ROOT)
    lines = [f"{f['file']}:{f['line']} {f['type']} ({f['match']})" for f in res.get("findings") or []]
    lines += [f"{b['file']}: {b['reason']}" for b in res.get("blocked_files") or []]
    assert res.get("ok"), (
        "Publish would be blocked. Replace real names, addresses, ids and keys with made-up ones; "
        "a false positive in a source pattern goes in the skip list in app/git_scanner.py:\n  "
        + "\n  ".join(lines or [res.get("summary", "")]))


def test_names_of_org_members_are_refused():
    """Every username in this VibeNode's org is refused, whole and by its
    distinctive parts; words that only name a computer are not."""
    import tempfile
    from web import git_scanner as g
    f = Path(tempfile.mkdtemp()) / "org_names.json"
    old = g.ORG_NAMES_FILE
    g.ORG_NAMES_FILE = f
    try:
        assert g.remember_org_names(["QuillanDesk", "Zorbo"]) is True
        assert g.remember_org_names(["QuillanDesk", "Zorbo"]) is False
        hits = lambda text: [x["type"] for x in g._identity_findings(text)]
        assert hits("call QuillanDesk now") == ["Org Member Name"]
        assert hits("quillandesk") == ["Org Member Name"]
        assert hits("ask zorbo") == ["Org Member Name"]
        assert hits("a desk and a linux laptop") == []
    finally:
        g.ORG_NAMES_FILE = old
        g._org_cache.update(mtime=None, rule=None)
