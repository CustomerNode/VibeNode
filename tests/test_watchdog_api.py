"""Tests for the Watchdog → VibeNode fix-session bridge (/fix-from-watchdog).

A CustomerNode Watchdog error email links here with the whole error gzip'd +
base64url'd into the ``d`` query param. The route must decode it, target the
CustomerNode project, force manual approval, start a session there, and redirect
the SPA to the new session.
"""
import base64
import gzip
import json
from unittest.mock import MagicMock, patch

import pytest

from app import create_app


def _make_token(**fields) -> str:
    """Mirror CustomerNode's infra/watchdog/reports/fix_link.encode_fix_payload
    so this test stays hermetic (no cross-repo import)."""
    payload = {
        "exc_type": fields.get("exc_type", ""),
        "location": fields.get("location", ""),
        "message": fields.get("message", ""),
        "traceback": fields.get("traceback", ""),
        "request_id": fields.get("request_id", ""),
        "paths": fields.get("paths", []),
    }
    payload["kind"] = fields.get("kind", "error")
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(gzip.compress(raw)).decode("ascii").rstrip("=")


def _make_infra_token(**fields) -> str:
    """Mirror CustomerNode's encode_infra_payload (kind=infra)."""
    payload = {
        "kind": "infra",
        "check_name": fields.get("check_name", ""),
        "severity": fields.get("severity", ""),
        "message": fields.get("message", ""),
        "detail": fields.get("detail", ""),
        "runbook_cmd": fields.get("runbook_cmd", ""),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(gzip.compress(raw)).decode("ascii").rstrip("=")


@pytest.fixture
def app():
    application = create_app(testing=True)
    application.session_manager.set_permission_policy = MagicMock(return_value={"ok": True})
    application.session_manager.start_session = MagicMock(return_value={"ok": True})
    return application


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


def test_opens_customernode_session_with_manual_approval(app, client):
    tok = _make_token(
        exc_type="TimeoutError", location="core/services/journey.py:212",
        message="Upstream timed out", traceback="Traceback...\nTimeoutError: boom",
        request_id="req_ABC123", paths=["/app/journey/sync"],
    )
    # Pin the resolved project so the test doesn't depend on the host's
    # ~/.claude/projects. Mirrors the real existing CustomerNode project.
    with patch(
        "app.routes.watchdog_api._resolve_customernode_project",
        return_value=("C--example-CustomerNode", r"C:\example\CustomerNode"),
    ):
        resp = client.get("/fix-from-watchdog?d=" + tok)

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Interstitial sets the CustomerNode project, polls /api/sessions until the
    # new session is listed, then redirects the SPA to it.
    assert "activeProject" in body
    assert "C--example-CustomerNode" in body
    assert "/api/sessions?project=" in body
    assert "?chat=" in body

    # Manual approval is forced so the agent pauses before editing.
    app.session_manager.set_permission_policy.assert_called_once_with("manual")

    # Session starts in the resolved CustomerNode project (not VibeNode), edits
    # gated by the callback.
    kw = app.session_manager.start_session.call_args.kwargs
    assert kw["cwd"].replace("\\", "/").endswith("code/CustomerNode")
    assert kw["permission_mode"] == "default"
    assert kw["resume"] is False
    assert kw["name"] == "Watchdog fix"
    # The prompt carries the trace and the do-not-apply instruction.
    assert "TimeoutError" in kw["prompt"]
    assert "core/services/journey.py:212" in kw["prompt"]
    assert "Do NOT apply" in kw["prompt"]
    # And it instructs a standardized production remediation log (dated filename
    # + Title/Traceback/Solution/Lesson format).
    assert "tasks/lessons/production/" in kw["prompt"]
    assert "_timeouterror_journey.md" in kw["prompt"]
    assert "Title" in kw["prompt"] and "Solution" in kw["prompt"] and "Lesson" in kw["prompt"]


def test_opens_session_for_infra_issue(app, client):
    tok = _make_infra_token(
        check_name="root_disk", severity="critical", message="/ at 95% used",
        detail="5 GB free of 100 GB", runbook_cmd="df -h /",
    )
    with patch(
        "app.routes.watchdog_api._resolve_customernode_project",
        return_value=("C--example-CustomerNode", r"C:\example\CustomerNode"),
    ):
        resp = client.get("/fix-from-watchdog?d=" + tok)

    assert resp.status_code == 200
    kw = app.session_manager.start_session.call_args.kwargs
    prompt = kw["prompt"]
    # Infra-flavored prompt (not "production error"), with the check + runbook.
    assert "infrastructure issue" in prompt.lower()
    assert "root_disk" in prompt
    assert "df -h /" in prompt
    # Remediation log slug derives from the check name.
    assert "tasks/lessons/production/" in prompt
    assert "_root_disk.md" in prompt
    # Still forces manual approval.
    app.session_manager.set_permission_policy.assert_called_once_with("manual")


def test_missing_payload_is_400(client):
    assert client.get("/fix-from-watchdog").status_code == 400


def test_invalid_payload_is_400(client):
    assert client.get("/fix-from-watchdog?d=not-a-valid-token!!").status_code == 400


def test_start_failure_is_502(app, client):
    app.session_manager.start_session = MagicMock(return_value={"error": "daemon down"})
    tok = _make_token(exc_type="ValueError", location="x.py:1")
    with patch(
        "app.routes.watchdog_api._resolve_customernode_project",
        return_value=("C--example-CustomerNode", r"C:\example\CustomerNode"),
    ):
        assert client.get("/fix-from-watchdog?d=" + tok).status_code == 502


def test_resolver_prefers_existing_project_by_session_count(tmp_path):
    """The resolver must reuse the operator's established CustomerNode project
    (most sessions) rather than minting a new …customerNode_root entry."""
    from app.routes import watchdog_api as wa

    projects = tmp_path / "projects"
    projects.mkdir()
    # Two candidates: the established parent (3 sessions) and an orphan subdir (1).
    parent = projects / "C--example-CustomerNode"
    parent.mkdir()
    for n in ("a", "b", "c"):
        (parent / f"{n}.jsonl").write_text("{}", encoding="utf-8")
    orphan = projects / "C--example-CustomerNode-customerNode-root"
    orphan.mkdir()
    (orphan / "z.jsonl").write_text("{}", encoding="utf-8")
    # A non-matching project must be ignored.
    (projects / "C--Users-dev-code-VibeNode").mkdir()

    with patch.object(wa, "_CLAUDE_PROJECTS", projects), \
         patch.object(wa, "_load_project_names", lambda: {}), \
         patch.object(wa, "_decode_project", lambda name: name):
        encoded, _cwd = wa._resolve_customernode_project()

    assert encoded == "C--example-CustomerNode"
