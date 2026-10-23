"""SDK patches fail loudly (daemon/sdk_patches.py, 2026-10-23).

Every patch reaches into claude_code_sdk's internals.  A precondition failure
used to be one WARNING line; the engine then ran unpatched and nobody knew.
Now: the installed version is checked against TESTED_SDK_RANGE, each failure
is logged at ERROR with the patch name and precondition, the failures and the
SDK version go into the engine code record (daemon/engine_code.py), the web
server passes them through /api/engine/status (app/engine_freshness.py), and
the browser shows a red banner (tests/test_engine_freshness_banner.py).

These tests stub every patch function: re-running the real ones would stack a
second Popen wrapper in this process.
"""

import json
import logging
import os

import pytest

from daemon import sdk_patches as sp
from daemon import engine_code
from app import engine_freshness as ef


@pytest.fixture
def fresh_patches(monkeypatch):
    """Reset the apply-once guard and stub all five patches to 'applied'."""
    monkeypatch.setattr(sp, "_patches_applied", False)
    monkeypatch.setattr(sp, "_patch_failures", [])
    monkeypatch.setattr(sp, "_sdk_version_tested", True)
    for _name, attr in sp._PATCH_TABLE:
        monkeypatch.setattr(sp, attr, lambda: True)
    yield
    # Leave the module believing the real patches (applied at import by other
    # tests) are in place, with a clean report.
    monkeypatch.setattr(sp, "_patches_applied", True)


# ── Version range ─────────────────────────────────────────────────────────

def test_version_parts_and_range():
    assert sp._version_parts("0.0.25") == (0, 0, 25)
    assert sp._version_parts("1.2rc1") == (1,)
    low, high = sp.TESTED_SDK_RANGE
    assert low < high
    assert sp.sdk_version_in_tested_range("0.0.25") is True
    assert sp.sdk_version_in_tested_range("0.0.99") is True
    assert sp.sdk_version_in_tested_range("0.1.0") is False
    assert sp.sdk_version_in_tested_range("1.0") is False
    assert sp.sdk_version_in_tested_range("garbage") is False


def test_the_installed_sdk_is_inside_the_tested_range():
    # If this fails, the SDK moved: re-check every precondition in
    # daemon/sdk_patches.py and tests/test_sdk_patches.py, then move
    # TESTED_SDK_RANGE and the requirements.txt ceiling together.
    assert sp.sdk_version_in_tested_range(sp._SDK_VERSION), sp._SDK_VERSION


def test_out_of_range_version_is_an_error_log_not_a_refusal(fresh_patches, monkeypatch, caplog):
    monkeypatch.setattr(sp, "_SDK_VERSION", "9.9.9")
    with caplog.at_level(logging.ERROR, logger="daemon.sdk_patches"):
        applied = sp.apply_patches()
    assert len(applied) == 5, "an untested version must still get the patches"
    assert any("outside the range" in r.getMessage() and "9.9.9" in r.getMessage()
               for r in caplog.records if r.levelno == logging.ERROR)
    report = sp.patch_report()
    assert report["sdk_version"] == "9.9.9"
    assert report["sdk_version_tested"] is False
    assert report["sdk_patch_failures"] == []


# ── Failure recording ─────────────────────────────────────────────────────

def test_a_failed_precondition_is_logged_at_error_and_recorded(fresh_patches, monkeypatch, caplog):
    def broken():
        raise AssertionError("Query.__init__ missing 'transport' param: ['self']")
    monkeypatch.setattr(sp, "_apply_patch_transport_adapter", broken)

    with caplog.at_level(logging.ERROR, logger="daemon.sdk_patches"):
        applied = sp.apply_patches()

    assert "transport_adapter" not in applied and len(applied) == 4
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("transport_adapter" in m and "did NOT apply" in m
               and "missing 'transport' param" in m for m in errors), errors
    report = sp.patch_report()
    assert report["sdk_patch_failures"] == ["transport_adapter"]
    assert report["sdk_patch_failure_details"] == [{
        "name": "transport_adapter",
        "precondition": "Query.__init__ missing 'transport' param: ['self']",
        "error_type": "AssertionError"}]


def test_all_applied_means_an_empty_failure_list(fresh_patches):
    sp.apply_patches()
    assert sp.patch_report()["sdk_patch_failures"] == []
    assert sp.patch_report()["sdk_version"] == sp._SDK_VERSION


def test_a_second_call_keeps_the_first_report(fresh_patches, monkeypatch):
    monkeypatch.setattr(sp, "_apply_patch_raise_json_buffer_limit",
                        lambda: (_ for _ in ()).throw(AssertionError("_MAX_BUFFER_SIZE not found")))
    sp.apply_patches()
    assert sp.apply_patches() == []          # idempotent
    assert sp.patch_report()["sdk_patch_failures"] == ["raise_json_buffer_limit"]


# ── Into the engine record ────────────────────────────────────────────────

def test_the_engine_record_carries_the_sdk_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_code, "_in_tests", lambda: False)
    monkeypatch.setattr(engine_code, "record_path", lambda: tmp_path / "engine_code.json")
    monkeypatch.setattr(sp, "patch_report", lambda: {
        "sdk_version": "0.0.25", "sdk_version_tested": True,
        "sdk_patch_failures": ["transport_adapter"],
        "sdk_patch_failure_details": [{"name": "transport_adapter", "precondition": "x"}]})

    engine_code.record_loaded_code()

    record = json.loads((tmp_path / "engine_code.json").read_text())
    assert record["sdk_version"] == "0.0.25"
    assert record["sdk_patch_failures"] == ["transport_adapter"]
    assert record["sdk_version_tested"] is True
    assert "daemon/engine_code.py" in record["loaded"]


def test_a_missing_sdk_is_recorded_as_its_own_failure(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_sdk(name, *a, **kw):
        if name == "daemon.sdk_patches" or name == "daemon":
            raise ImportError("No module named 'claude_code_sdk'")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", no_sdk)
    fields = engine_code.sdk_fields()
    assert fields["sdk_patch_failures"] == ["sdk_import"]
    assert fields["sdk_version_tested"] is False


def test_a_hot_reload_note_preserves_the_sdk_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_code, "_in_tests", lambda: False)
    monkeypatch.setattr(engine_code, "record_path", lambda: tmp_path / "engine_code.json")
    (tmp_path / "engine_code.json").write_text(json.dumps({
        "pid": os.getpid(), "loaded": {}, "sdk_version": "0.0.25",
        "sdk_patch_failures": ["safe_parse_message"]}))
    engine_code.note_reloaded(["daemon/engine_code.py"])
    record = json.loads((tmp_path / "engine_code.json").read_text())
    assert record["sdk_patch_failures"] == ["safe_parse_message"]
    assert record["sdk_version"] == "0.0.25"


# ── Through /api/engine/status ────────────────────────────────────────────

class _Proc:
    def __init__(self, pid):
        self.pid = pid

    def create_time(self):
        return 1.0


def test_engine_status_passes_the_sdk_fields_through(tmp_path, monkeypatch):
    monkeypatch.setattr(ef, "ROOT", tmp_path)
    monkeypatch.setattr(ef, "RECORD_PATH", tmp_path / "engine_code.json")
    monkeypatch.setattr(ef, "engine_process", lambda: _Proc(4242))
    (tmp_path / "engine_code.json").write_text(json.dumps({
        "pid": 4242, "loaded": {}, "sdk_version": "0.0.25", "sdk_version_tested": False,
        "sdk_patch_failures": ["transport_adapter", "raise_json_buffer_limit"],
        "sdk_patch_failure_details": [{"name": "transport_adapter", "precondition": "p"}]}))

    body = ef.status()

    assert body["running"] is True
    assert body["sdk_version"] == "0.0.25"
    assert body["sdk_version_tested"] is False
    assert body["sdk_patch_failures"] == ["transport_adapter", "raise_json_buffer_limit"]
    assert body["sdk_patch_failure_details"] == [{"name": "transport_adapter", "precondition": "p"}]


def test_an_old_engine_without_the_fields_reports_no_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(ef, "ROOT", tmp_path)
    monkeypatch.setattr(ef, "RECORD_PATH", tmp_path / "engine_code.json")
    monkeypatch.setattr(ef, "engine_process", lambda: _Proc(7))
    (tmp_path / "engine_code.json").write_text(json.dumps({"pid": 7, "loaded": {}}))
    body = ef.status()
    assert body["sdk_patch_failures"] == [] and body["sdk_version"] == ""
    assert body["sdk_version_tested"] is True


def test_no_record_at_all_still_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(ef, "ROOT", tmp_path)
    monkeypatch.setattr(ef, "RECORD_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(ef, "engine_process", lambda: _Proc(7))
    body = ef.status()
    assert body["has_record"] is False and body["sdk_patch_failures"] == []


def test_the_route_returns_the_fields(monkeypatch):
    from flask import Flask
    from app.routes import admin_api
    monkeypatch.setattr(ef, "status", lambda: {
        "ok": True, "running": True, "stale": False, "outdated": [], "restart_armed": False,
        "sdk_version": "0.0.25", "sdk_patch_failures": ["transport_adapter"]})
    app = Flask(__name__)
    app.register_blueprint(admin_api.bp)
    body = app.test_client().get("/api/engine/status").get_json()
    assert body["sdk_patch_failures"] == ["transport_adapter"]
