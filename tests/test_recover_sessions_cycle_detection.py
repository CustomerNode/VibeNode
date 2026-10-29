"""
Cycle detection in ``recover_sessions`` (subsessions spec §6.8).

A corrupted or hand-edited registry can contain A.parent_session_id = B and
B.parent_session_id = A.  Recovery must detect the cycle, force-clear one
side's ``parent_session_id`` with a logged warning, and continue without
raising or looping forever.

History: this file began as a scaffold while ``parent_session_id`` did not
exist, with the cycle-clearing half behind ``pytest.mark.xfail``.  The field
shipped (``daemon/session_manager.py``, SessionInfo), the xfail marks were
removed, and both tests have passed since.  They are regular regression
tests now.

See ``docs/plans/subsessions-spec.md`` §6.8 + §13.1 test 3.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from daemon.session_registry import SessionRegistry


def _write_registry(path: Path, sessions: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sessions": sessions}, indent=2),
                    encoding="utf-8")


def _make_mock_store():
    store = MagicMock()
    store.find_session_path.return_value = Path("/fake/session.jsonl")
    store.repair_incomplete_turn.return_value = True
    return store


# ---------------------------------------------------------------------------
# Already-green today: no infinite loop on a cyclic parent graph
# ---------------------------------------------------------------------------

def test_recover_sessions_terminates_on_cyclic_parent_graph(tmp_path):
    """A registry whose parent_session_id pointers form a cycle (A→B, B→A)
    must NOT cause ``recover_sessions`` to loop or raise: the parent walk
    is depth-bounded, and both sessions are still recovered.
    """
    reg = SessionRegistry()
    registry_file = tmp_path / "registry.json"
    now = time.time()
    sessions = {
        "sess-A": {
            "name": "Cycle A",
            "state": "working",
            "cwd": "/tmp",
            "model": "",
            "last_activity": now - 30,
            "parent_session_id": "sess-B",   # A points at B
            "subsession_origin_turn": 1,
        },
        "sess-B": {
            "name": "Cycle B",
            "state": "working",
            "cwd": "/tmp",
            "model": "",
            "last_activity": now - 30,
            "parent_session_id": "sess-A",   # B points at A — cycle!
            "subsession_origin_turn": 1,
        },
    }
    _write_registry(registry_file, sessions)

    start_fn = MagicMock(return_value={"ok": True})
    store = _make_mock_store()

    # We rely on pytest's per-test timeout (60s in pytest.ini) plus the
    # in-test assertion that this returns synchronously without raising.
    with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
        reg.recover_sessions(start_fn, store)

    # Both sessions are eligible and should each be attempted.  Neither
    # should be skipped because of the cycle in metadata.
    assert start_fn.call_count == 2
    seen = {c.kwargs["session_id"] for c in start_fn.call_args_list}
    assert seen == {"sess-A", "sess-B"}


# ---------------------------------------------------------------------------
# Force-clear one side of the cycle with a logged warning
# ---------------------------------------------------------------------------

def test_recover_sessions_force_clears_one_side_of_cycle(tmp_path, caplog):
    """When recovery detects A↔B, one session's parent_session_id is
    cleared and a warning is logged.

    The test is intentionally lenient on WHICH side is cleared — the
    invariant is "the cycle is broken," not a particular tie-breaker.

    State ``"working"`` keeps both sessions in the recovery-eligible set
    so the cleared parent is observable through ``start_session_fn``'s
    kwargs (idle sessions are short-circuited before that call site).
    """
    import logging
    caplog.set_level(logging.WARNING)

    reg = SessionRegistry()
    registry_file = tmp_path / "registry.json"
    now = time.time()
    sessions = {
        "sess-A": {
            "name": "Cycle A",
            "state": "working",
            "cwd": "/tmp",
            "model": "",
            "last_activity": now,
            "parent_session_id": "sess-B",
            "subsession_origin_turn": 1,
        },
        "sess-B": {
            "name": "Cycle B",
            "state": "working",
            "cwd": "/tmp",
            "model": "",
            "last_activity": now,
            "parent_session_id": "sess-A",
            "subsession_origin_turn": 1,
        },
    }
    _write_registry(registry_file, sessions)

    start_fn = MagicMock(return_value={"ok": True})
    store = _make_mock_store()

    with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
        reg.recover_sessions(start_fn, store)

    # Contract: at least one side must have had its
    # parent_session_id cleared on the in-memory SessionInfo that
    # start_session was called with.  We accept either side.
    cleared_sides = [
        c.kwargs.get("parent_session_id", "MISSING") in (None, "")
        for c in start_fn.call_args_list
    ]
    assert any(cleared_sides), \
        "Expected at least one session to have parent_session_id cleared"

    # And the warning must be logged so an operator can spot the
    # corruption after the fact.
    assert any(
        "cycle" in r.getMessage().lower() or "parent" in r.getMessage().lower()
        for r in caplog.records
    ), "Expected a warning log mentioning the cycle/parent fix-up"
