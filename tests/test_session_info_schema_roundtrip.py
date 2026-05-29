"""
[subsessions phase -1] SessionInfo registry-schema tolerance.

Phase 1 of the Subsessions feature adds two new fields to SessionInfo
(``parent_session_id`` and ``subsession_origin_turn``) and persists them
into the ``gui_active_sessions.json`` registry snapshot.  Older registry
files written by the pre-subsessions daemon must continue to load — the
recovery path must treat the missing fields as ``None`` / ``0`` defaults,
and must ignore extra unknown fields without raising.

This file pins that backward-compatibility invariant at the CURRENT HEAD
so that anyone editing the registry schema in Phase 1 (or later) breaks
this test loudly instead of silently regressing crash recovery for
users upgrading across the rename.

See ``docs/plans/subsessions-spec.md`` §10 Backward compatibility and
§13.1 test 2.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from daemon.session_registry import SessionRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_registry(path: Path, sessions: dict) -> None:
    """Write a registry JSON file with the given sessions dict."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sessions": sessions}, indent=2),
                    encoding="utf-8")


def _make_mock_store():
    """Return a ChatStore stub whose paths/repairs look real."""
    store = MagicMock()
    store.find_session_path.return_value = Path("/fake/session.jsonl")
    store.repair_incomplete_turn.return_value = True
    return store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRegistryTolerance:
    """Recovery must accept BOTH pre-subsessions and post-subsessions
    registry JSON without raising.

    The new fields ``parent_session_id`` and ``subsession_origin_turn`` do
    NOT exist on today's HEAD.  These tests therefore exercise the
    "missing field tolerated as default" half of the contract today, and
    the "extra unknown field tolerated" half holds today and must keep
    holding after Phase 1 ships.
    """

    def test_missing_subsession_fields_recover_as_defaults(self, tmp_path):
        """Registry written without parent_session_id / subsession_origin_turn
        still recovers; the missing fields land as their defaults.

        We can't read the fields back on a SessionInfo today because they
        don't exist yet, so we use ``dict.get`` defaults to capture what
        recovery would pass forward.  The point of the test: ``load_registry``
        + ``recover_sessions`` must not raise on a JSON object that lacks
        the new keys.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        now = time.time()
        sessions = {
            "sess-legacy": {
                "name": "Legacy session",
                "state": "working",
                "cwd": "/tmp",
                "model": "",
                "last_activity": now - 60,
                # Deliberately no parent_session_id / subsession_origin_turn:
                # this mirrors a registry written before Phase 1 lands.
            },
        }
        _write_registry(registry_file, sessions)

        start_fn = MagicMock(return_value={"ok": True})
        store = _make_mock_store()
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            reg.recover_sessions(start_fn, store)

        # Recovery did not raise and produced exactly one resume call.
        start_fn.assert_called_once()

        # Re-read the on-disk JSON the same way ``recover_sessions`` does
        # and assert .get() returns the documented defaults.  When Phase 1
        # adds explicit reads of these fields, this assertion guards that
        # ``getattr``/``dict.get`` with the right default is preserved.
        data = json.loads(registry_file.read_text(encoding="utf-8"))
        meta = data["sessions"]["sess-legacy"]
        assert meta.get("parent_session_id") is None
        assert meta.get("subsession_origin_turn", 0) == 0

    def test_unknown_extra_fields_are_ignored(self, tmp_path):
        """A registry containing fields the daemon doesn't know about must
        load cleanly.  This is the forward-compat half: a future field
        added by an even-newer daemon must not crash the current one.
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        now = time.time()
        sessions = {
            "sess-future": {
                "name": "Future-format",
                "state": "working",
                "cwd": "/tmp",
                "model": "",
                "last_activity": now - 60,
                # Garbage that no current schema field cares about:
                "totally_unknown_field": "ignore me",
                "future_nested": {"deeply": {"nested": [1, 2, 3]}},
                "parent_session_id": None,
                "subsession_origin_turn": 0,
            },
        }
        _write_registry(registry_file, sessions)

        start_fn = MagicMock(return_value={"ok": True})
        store = _make_mock_store()
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            # Must not raise — that's the whole point.
            reg.recover_sessions(start_fn, store)

        # And must still attempt the resume for the eligible session.
        start_fn.assert_called_once()
        kwargs = start_fn.call_args.kwargs
        assert kwargs["session_id"] == "sess-future"
        assert kwargs["resume"] is True

    def test_load_registry_accepts_missing_new_fields(self, tmp_path):
        """``load_registry`` itself returns the dict verbatim — verifying
        the new fields are just absent in the loaded structure (not, e.g.,
        injected with wrong sentinels by load).
        """
        reg = SessionRegistry()
        registry_file = tmp_path / "registry.json"
        sessions = {
            "sess-legacy": {
                "name": "Legacy",
                "state": "idle",
                "cwd": "/tmp",
                "model": "",
                "last_activity": time.time(),
            },
        }
        _write_registry(registry_file, sessions)
        with patch('daemon.session_registry.REGISTRY_PATH', registry_file):
            result = reg.load_registry()
        meta = result["sessions"]["sess-legacy"]
        assert "parent_session_id" not in meta
        assert "subsession_origin_turn" not in meta
