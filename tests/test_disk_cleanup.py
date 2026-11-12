"""Disk gauge and cleanup: which sessions each rule picks, what is never
picked, that a full disk still frees space, the Trash, and the cap."""

import json
import os
import time

import pytest

from web import disk_cleanup as dc

NOW = float(int(time.time()))
DAY = 86400


def _session(root, project, sid, started_days, last_days, size=10):
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    from datetime import datetime, timezone
    ts = datetime.fromtimestamp(NOW - started_days * DAY, timezone.utc).isoformat()
    line = json.dumps({"type": "user", "timestamp": ts}) + "\n"
    f.write_text(line + "x" * size, encoding="utf-8")
    os.utime(f, (NOW - last_days * DAY, NOW - last_days * DAY))
    return f


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "projects"
    _session(r, "-home-x-alpha", "old-quiet", 60, 40, size=5000)
    _session(r, "-home-x-alpha", "old-busy", 60, 1)
    _session(r, "-home-x-alpha", "new-quiet", 5, 5)
    _session(r, "-home-x-beta", "pinned-old", 90, 90)
    (r / "-home-x-beta" / "_session_pins.json").write_text(
        json.dumps({"pinned-old": {"set": NOW, "rank": 1}}), encoding="utf-8")
    from web import config as cfg
    monkeypatch.setattr(cfg, "_CLAUDE_PROJECTS", r)
    dc._share_cache.clear()
    return r


def _ids(items):
    return sorted(i["id"] for i in items)


def test_no_rule_picks_nothing(root):
    assert dc.delete_candidates(root, now=NOW) == []


def test_rules(root):
    assert _ids(dc.delete_candidates(root, inactive_days=30, now=NOW)) == ["old-quiet"]
    assert _ids(dc.delete_candidates(root, created_days=30, now=NOW)) == ["old-busy", "old-quiet"]
    assert _ids(dc.delete_candidates(root, created_days=30, inactive_days=3, now=NOW)) == ["old-quiet"]


def test_running_and_pinned_sessions_are_never_picked(root):
    got = _ids(dc.delete_candidates(root, inactive_days=0, running={"old-quiet"}, now=NOW))
    assert "old-quiet" not in got and "pinned-old" not in got


def test_an_engine_that_does_not_answer_protects_recent_sessions(root):
    # Unknown running set: anything written in the last 10 minutes counts as running.
    f = root / "-home-x-alpha" / "new-quiet.jsonl"
    os.utime(f, (NOW - 60, NOW - 60))
    got = _ids(dc.delete_candidates(root, inactive_days=0, running={"__recent__"}, now=NOW))
    assert "new-quiet" not in got and "old-quiet" in got


def test_delete_goes_to_the_trash_when_there_is_room(root):
    r = dc.delete_sessions(dc.delete_candidates(root, inactive_days=30, now=NOW), root)
    assert r == {"deleted": 1, "trashed": 1, "removed": 0, "freed": 0}
    assert (root / "-home-x-alpha" / "_trash" / "old-quiet.jsonl").exists()
    assert dc.delete_candidates(root, inactive_days=30, now=NOW) == []


def test_a_full_disk_frees_the_space_first_and_writes_after(root, monkeypatch):
    monkeypatch.setattr(dc, "disk_reading", lambda r: {"known": True, "total": 1, "free": 0})
    from web import session_store as ss

    def no_room(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(ss, "_mark_deleted", no_room)
    monkeypatch.setattr(ss, "_delete_name", no_room)
    r = dc.delete_sessions(dc.delete_candidates(root, inactive_days=30, now=NOW), root)
    assert r["removed"] == 1 and r["freed"] > 5000
    assert not (root / "-home-x-alpha" / "old-quiet.jsonl").exists()
    assert not (root / "-home-x-alpha" / "_trash").exists()


def test_empty_trash_keeps_its_list_and_frees_the_files(root):
    dc.delete_sessions(dc.delete_candidates(root, inactive_days=3, now=NOW), root)
    trash = root / "-home-x-alpha" / "_trash"
    assert len(list(trash.glob("*.jsonl"))) == 2
    assert dc.empty_trash(root) > 5000
    assert list(trash.glob("*.jsonl")) == []
    assert json.loads((trash / "_trash_index.json").read_text()) == {}


def test_share_counts_sessions_and_trash_apart(root):
    dc.delete_sessions(dc.delete_candidates(root, inactive_days=30, now=NOW), root)
    s = dc.share(root, fresh=True)
    assert s["trash"] > 5000 and s["sessions"] > 0


def test_biggest_lists_deletable_sessions_largest_first(root):
    got = dc.biggest(root, running=set(), now=NOW)
    assert got[0]["id"] == "old-quiet"
    assert "pinned-old" not in _ids(got)


def test_cap_none_by_default_and_at_the_drives_size():
    assert dc.cap_of({}, 100) == 0 and dc.cap_of({dc.CAP_KEY: 100}, 100) == 0
    assert dc.cap_of({dc.CAP_KEY: 40}, 100) == 40


def test_over_the_cap_empties_the_trash_then_removes_the_quietest(root):
    class Engine:
        def get_all_states(self):
            return [{"session_id": "old-busy", "state": "idle"}]
    dc.delete_sessions(dc.delete_candidates(root, inactive_days=30, now=NOW), root)  # old-quiet in Trash
    # Cap below what sessions alone take: Trash first, then the quietest sleeping one.
    done = dc.enforce_cap(Engine(), root, 1)
    assert done["trash_freed"] > 5000 and done["removed"] >= 1
    left = {f.stem for f in (root / "-home-x-alpha").glob("*.jsonl")}
    assert "old-busy" in left                                   # running: kept
    assert (root / "-home-x-beta" / "pinned-old.jsonl").exists()   # pinned: kept
    assert dc.enforce_cap(Engine(), root, 0) == {}


def test_disk_reading_has_a_percent(tmp_path):
    r = dc.disk_reading(tmp_path)
    assert r["known"] and 0 <= r["percent"] <= 100 and r["total"] > 0
