"""A bulk filesystem change is not copied into the snapshot store.

A CustomerNode session whose folder holds git worktrees snapshotted a median
of 11,154 files per turn (max 72,216): every `git worktree add` looked like a
turn's worth of edits, and ~/.claude/file-history grew ~220 MB a day.
`SessionManager._FS_BULK_CHANGE_LIMIT` stops that.  Files the session edited
directly are still snapshotted, and a change at the limit still is in full.
"""

import json
from pathlib import Path as _Path
from unittest.mock import patch

import pytest


def _run_post_turn_snapshot(tmp_path, n_extras):
    from app.config import _encode_cwd
    from daemon.session_manager import SessionManager, SessionInfo, SessionState

    proj = tmp_path / "proj"
    proj.mkdir()
    proj_dir = tmp_path / ".claude" / "projects" / _encode_cwd(str(proj))
    proj_dir.mkdir(parents=True)
    sid = "bulk-limit"
    jsonl = proj_dir / f"{sid}.jsonl"
    jsonl.write_text("", encoding="utf-8")

    edited = proj / "edited.py"
    edited.write_text("v1\n", encoding="utf-8")
    extras = set()
    for i in range(n_extras):
        fp = proj / f"wt_{i:05d}.py"
        fp.write_text(f"x {i}\n", encoding="utf-8")
        extras.add(str(fp))

    info = SessionInfo(session_id=sid, cwd=str(proj), state=SessionState.IDLE)
    info.tracked_files.add(str(edited))
    info._turn_had_direct_edit = False  # the filesystem fallback engages

    mgr = SessionManager()
    with mgr._lock:
        mgr._sessions[sid] = info
    with patch("daemon.session_manager.Path") as MP, \
         patch("daemon.backends.claude_store.Path") as SP, \
         patch.object(SessionManager, "_detect_changed_files",
                      lambda self, info: set(extras)):
        MP.side_effect = _Path
        MP.home.return_value = tmp_path
        SP.side_effect = _Path
        SP.home.return_value = tmp_path
        mgr._write_file_snapshot(sid, is_post_turn=True)

    backed = set()
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        o = json.loads(line)
        if o.get("type") == "file-history-snapshot":
            backed |= set((o.get("snapshot") or {}).get("trackedFileBackups") or {})
    history = tmp_path / ".claude" / "file-history" / sid
    copies = list(history.iterdir()) if history.exists() else []
    return edited, extras, backed, copies, SessionManager._FS_BULK_CHANGE_LIMIT


def _norm(paths):
    return {str(_Path(p)).lower() for p in paths}


def test_a_bulk_change_copies_only_the_direct_edit(tmp_path):
    from daemon.session_manager import SessionManager
    n = SessionManager._FS_BULK_CHANGE_LIMIT + 1
    edited, extras, backed, copies, _ = _run_post_turn_snapshot(tmp_path, n)

    assert str(edited).lower() in _norm(backed), "the direct edit lost its snapshot"
    assert not (_norm(backed) & _norm(extras)), "bulk files reached the snapshot"
    assert len(copies) <= 1, "%d files copied for a bulk change" % len(copies)


def test_a_change_at_the_limit_is_still_snapshotted_in_full(tmp_path):
    from daemon.session_manager import SessionManager
    n = SessionManager._FS_BULK_CHANGE_LIMIT
    edited, extras, backed, copies, _ = _run_post_turn_snapshot(tmp_path, n)

    assert _norm(extras) <= _norm(backed)
    assert str(edited).lower() in _norm(backed)


def test_the_limit_stays_well_above_ordinary_turns():
    """Real shell edits run to tens of files; 200 extras is an existing
    perf-guard scenario (test_performance_guards) that must still be kept."""
    from daemon.session_manager import SessionManager
    assert SessionManager._FS_BULK_CHANGE_LIMIT >= 200
