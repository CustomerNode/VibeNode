"""The engine's alias index (spec step 1, docs/plans/session-host-spec.md).

The engine writes ``old -> new`` to ``data/session_aliases.json`` on every id
adoption (sandboxed by tests/conftest.py); a restarted engine and the web both
read it, so a tab holding a launch id still reaches the session after an
engine restart.  The per-project remap file wins where both know an id.
"""

import json
import time

from app import session_store
from common import session_names


def test_record_and_load():
    session_names.record_engine_alias("temp-a", "real-a")
    session_names.record_engine_alias("temp-a", "temp-a")      # self-alias ignored
    session_names.record_engine_alias("", "x")
    got = session_names.load_engine_aliases()
    assert set(got) == {"temp-a"} and got["temp-a"]["new_id"] == "real-a"


def test_week_old_aliases_are_pruned():
    path = session_names.ENGINE_ALIASES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ancient": {"new_id": "z", "ts": time.time() - 8 * 86400}}),
                    encoding="utf-8")
    session_names.record_engine_alias("fresh", "real-fresh")
    assert set(session_names.load_engine_aliases()) == {"fresh"}


def test_web_reads_the_engine_index_through_and_its_own_file_wins(tmp_path, monkeypatch):
    own = tmp_path / "remaps.json"
    monkeypatch.setattr(session_store, "_remap_file", lambda project="": own)
    session_store._save_remaps({"both": {"new_id": "from-project", "ts": time.time()}})
    session_names.record_engine_alias("both", "from-engine")
    session_names.record_engine_alias("engine-only", "real-e")
    merged = session_store._load_remaps_cached()
    assert merged["both"]["new_id"] == "from-project"
    assert merged["engine-only"]["new_id"] == "real-e"
    assert session_store._resolve_remapped_id("engine-only") == "real-e"
    assert "engine-only" in session_store._get_remapped_ids()


def test_engine_rehydrates_aliases_at_start(monkeypatch):
    from unittest.mock import MagicMock
    from daemon.session_manager import SessionManager
    session_names.record_engine_alias("temp-r", "real-r")
    sm = SessionManager(sdk=MagicMock())
    sm.start(lambda *a, **k: None)
    try:
        assert sm._resolve_id("temp-r") == "real-r"
    finally:
        sm.stop()
