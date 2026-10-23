"""
Every VIBENODE_* environment variable the code reads is described in
app/engine_knobs.py, so System → Settings can show what the app will do
without anyone grepping the source.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import engine_knobs  # noqa: E402

_NAME = re.compile(r"\bVIBENODE_[A-Z0-9_]+\b")


def _names_in_source() -> set:
    found = set()
    files = list((ROOT / "daemon").rglob("*.py")) + list((ROOT / "app").rglob("*.py")) + [ROOT / "run.py"]
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in _NAME.finditer(text):
            found.add(m.group(0))
    return found


def test_every_knob_in_the_code_is_described():
    missing = sorted(_names_in_source() - engine_knobs.names())
    assert not missing, "VIBENODE_* variables read by the code but not described in app/engine_knobs.py: %s" % missing


def test_described_knobs_exist_in_the_code():
    stale = sorted(engine_knobs.names() - _names_in_source() - {"VIBENODE_TEST_NO_SANDBOX"})
    assert not stale, "knobs described but read nowhere: %s" % stale


def test_snapshot_reports_value_and_source():
    env = {"VIBENODE_STALL_MINUTES": "25"}
    rows = {r["name"]: r for r in engine_knobs.snapshot(env)}
    assert rows["VIBENODE_STALL_MINUTES"] == {
        "name": "VIBENODE_STALL_MINUTES", "group": "Stall watchdog",
        "description": rows["VIBENODE_STALL_MINUTES"]["description"],
        "default": "10", "value": "25", "source": "env", "internal": False,
    }
    assert rows["VIBENODE_LIMIT_AUTO_RESUME"]["value"] == "1"
    assert rows["VIBENODE_LIMIT_AUTO_RESUME"]["source"] == "default"
    assert rows["VIBENODE_PRESERVE_DAEMON"]["internal"] is True
    assert all(r["description"] for r in rows.values())


def test_route_serves_the_snapshot():
    from app import create_app
    app = create_app(testing=True)
    r = app.test_client().get("/api/engine/knobs")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] and len(body["knobs"]) == len(engine_knobs.KNOBS)
    assert {"name", "group", "description", "default", "value", "source"} <= set(body["knobs"][0])
