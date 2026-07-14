"""Watchdog → VibeNode bridge: open a CustomerNode fix session from an email.

A "Draft fix in VibeNode" button in a CustomerNode Watchdog error email links
to ``GET /fix-from-watchdog?d=<token>``. The token is the whole error payload
(gzip + base64url JSON) built by CustomerNode's
``infra/watchdog/reports/fix_link.py`` — self-contained so it works even though
the watchdog runs server-side and can't reach this localhost console.

On click we: decode the error, switch the active project to CustomerNode (NOT
VibeNode), force manual permission so the agent pauses before editing, start a
fresh session pre-loaded with the trace + a "draft the fix, wait for approval"
instruction, and land the operator on that running session.
"""
import base64
import gzip
import html as html_lib
import json
import os
import re
import uuid
from datetime import date

from flask import Blueprint, Response, current_app, request

from ..config import (
    _CLAUDE_PROJECTS,
    _VIBENODE_DIR,
    _decode_project,
    _encode_cwd,
    _load_project_names,
    set_active_project,
)

bp = Blueprint("watchdog_api", __name__)

# Projects whose name/path contains this are treated as "the CustomerNode
# project". Override the matcher or the fallback path via env.
CUSTOMERNODE_MATCH = os.environ.get("VIBENODE_CUSTOMERNODE_MATCH", "customernode").lower()
# Fallback path used only when no existing project matches (e.g. fresh machine).
# Derived from the repo location — a "CustomerNode" checkout sitting next to VibeNode
# (the same sibling-of-repo convention used to locate SpeechNode) — so no personal path
# is baked into this public repo. Override explicitly with VIBENODE_CUSTOMERNODE_CWD.
CUSTOMERNODE_FALLBACK_CWD = os.environ.get(
    "VIBENODE_CUSTOMERNODE_CWD",
    str(_VIBENODE_DIR.parent / "CustomerNode"),
)


def _resolve_customernode_project() -> tuple:
    """Find the already-registered CustomerNode project and return (encoded, cwd).

    The operator already has a CustomerNode project in VibeNode (one level up
    from the git repo, holding the venv etc.). We must reuse it — NOT mint a new
    ``…customerNode_root`` entry that the SPA wouldn't even surface. Match any
    project whose encoded id, decoded path, or custom name contains
    ``CUSTOMERNODE_MATCH``, preferring the one with the most sessions (the real,
    established project). Falls back to a sensible path when nothing matches."""
    names = _load_project_names()
    best = None  # (session_count, encoded, cwd)
    if _CLAUDE_PROJECTS.is_dir():
        for d in sorted(_CLAUDE_PROJECTS.iterdir()):
            if not d.is_dir() or d.name.startswith(("subagents", "system-utility")):
                continue
            display = _decode_project(d.name)
            hay = f"{d.name} {display} {names.get(d.name, '')}".lower()
            if CUSTOMERNODE_MATCH not in hay:
                continue
            count = sum(1 for _ in d.glob("*.jsonl"))
            if best is None or count > best[0]:
                best = (count, d.name, display)
    if best is not None:
        return best[1], best[2]
    # Nothing registered yet — fall back to the conventional path.
    return _encode_cwd(CUSTOMERNODE_FALLBACK_CWD), CUSTOMERNODE_FALLBACK_CWD


def _decode_token(token: str) -> dict:
    """Reverse fix_link.encode_fix_payload (re-pad stripped base64url)."""
    padded = token + "=" * (-len(token) % 4)
    raw = gzip.decompress(base64.urlsafe_b64decode(padded))
    return json.loads(raw)


def _slugify(*parts: str) -> str:
    """Lowercase ``a_b`` slug from the given parts, safe for a filename."""
    slug = re.sub(r"[^a-z0-9]+", "_", "_".join(p for p in parts if p).lower()).strip("_")
    return slug or "production_issue"


def _remediation_footer(log_path: str) -> list:
    """Shared instruction: log the issue + fix in the standardized format."""
    return [
        "",
        "As PART of the fix, also write a production remediation log so this issue "
        "and its fix are recorded:",
        f"- File: `{log_path}`",
        "- Follow the format in `tasks/lessons/production/_TEMPLATE.md` exactly: "
        "the four sections **Title → Traceback → Solution → Lesson**, in order.",
        "- Title = one-line plain-terms summary. Traceback = the evidence above "
        "(traceback for a code error; the check detail / command output for an "
        "infra issue), verbatim. Solution = the actual root-cause change (files or "
        "config touched). Lesson = the generalizable takeaway + what would have "
        "caught it earlier.",
    ]


def _build_error_prompt(p: dict, date_str: str) -> str:
    exc_type = (p.get("exc_type") or "Error").strip()
    location = (p.get("location") or "unknown").strip()
    message = (p.get("message") or "").strip()
    traceback = (p.get("traceback") or "").strip()
    request_id = (p.get("request_id") or "").strip()
    paths = p.get("paths") or []
    base = (location.split(":")[0].rsplit("/", 1)[-1].rsplit(".", 1)[0]) if location else ""
    log_path = f"tasks/lessons/production/{date_str}_{_slugify(exc_type, base)}.md"

    lines = [
        "A production error was caught by CustomerNode's Watchdog. Investigate the "
        "root cause and draft a fix.",
        "",
        f"- Exception: {exc_type}",
        f"- Location: {location}",
    ]
    if message:
        lines.append(f"- Message: {message}")
    if paths:
        lines.append(f"- Request paths: {', '.join(paths)}")
    if request_id:
        lines.append(f"- Request id: {request_id}")
    if traceback:
        lines += ["", "Traceback:", "```", traceback, "```"]
    lines += [
        "",
        "Read the relevant AGENTS.md first, find the root cause (not a band-aid), "
        "then propose the fix. Do NOT apply any change until I approve it.",
    ]
    lines += _remediation_footer(log_path)
    return "\n".join(lines)


def _build_infra_prompt(p: dict, date_str: str) -> str:
    check_name = (p.get("check_name") or "infra_issue").strip()
    severity = (p.get("severity") or "").strip()
    message = (p.get("message") or "").strip()
    detail = (p.get("detail") or "").strip()
    runbook_cmd = (p.get("runbook_cmd") or "").strip()
    log_path = f"tasks/lessons/production/{date_str}_{_slugify(check_name)}.md"

    headline = f"Watchdog flagged an infrastructure issue: {check_name}"
    if severity:
        headline += f" ({severity.upper()})"
    lines = [
        headline + ".",
        "Investigate the root cause and propose a fix. This is an INFRASTRUCTURE "
        "issue (deployment, container, disk, service, security, external API) — not "
        "necessarily an application bug; the fix may be config, Docker, nginx, a "
        "runbook step, or code.",
        "",
        f"- Check: {check_name}",
    ]
    if severity:
        lines.append(f"- Severity: {severity}")
    if message:
        lines.append(f"- Summary: {message}")
    if detail:
        lines.append(f"- Detail: {detail}")
    if runbook_cmd:
        lines += ["", "Suggested runbook command (diagnostic starting point):",
                  "```", runbook_cmd, "```"]
    lines += [
        "",
        "Read the relevant AGENTS.md first (likely `infra/AGENTS.md` or a "
        "subdirectory). Confirm the issue, find the root cause, then propose the "
        "fix. Do NOT apply any change until I approve it.",
    ]
    lines += _remediation_footer(log_path)
    return "\n".join(lines)


def _build_fix_prompt(p: dict, date_str: str) -> str:
    """Build the session's opening instruction from the decoded payload.

    Dispatches on ``kind``: an error digest entry (code traceback) vs an infra
    issue (flagged check from the morning/evening/weekly report). ``date_str``
    (YYYY-MM-DD) seeds the deterministic remediation-log filename so the agent
    doesn't have to guess today's date."""
    if (p.get("kind") or "error") == "infra":
        return _build_infra_prompt(p, date_str)
    return _build_error_prompt(p, date_str)


def _redirect_html(encoded_project: str, session_id: str) -> str:
    """Interstitial that points the SPA at the new session.

    The SPA picks the active project by session count and focuses a session via
    ``?chat=<id>`` only if that id is already in the loaded session list — both
    read at page-load time. A freshly-started session may not be listed yet, so
    a bare redirect lands on the most-recent chat instead. We therefore set
    ``localStorage['activeProject']`` first, then POLL ``/api/sessions`` until
    the new session is visible before redirecting (with a timeout fallback so
    the operator is never stuck)."""
    proj = json.dumps(encoded_project)
    sid = json.dumps(session_id)
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>Opening fix session…</title></head>
<body style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;
background:#141b2d;color:#fff;padding:40px;">
<p id="msg">Starting CustomerNode fix session…</p>
<script>
(function() {{
  var proj = {proj}, sid = {sid};
  localStorage.setItem('activeProject', proj);
  var deadline = Date.now() + 12000;  // give the daemon time to register it
  function go() {{ location.replace('/?chat=' + encodeURIComponent(sid)); }}
  function poll() {{
    fetch('/api/sessions?project=' + encodeURIComponent(proj))
      .then(function(r) {{ return r.json(); }})
      .then(function(d) {{
        var list = Array.isArray(d) ? d : (d && d.sessions) || [];
        if (list.some(function(s) {{ return s && s.id === sid; }})) return go();
        if (Date.now() > deadline) return go();
        setTimeout(poll, 400);
      }})
      .catch(function() {{
        if (Date.now() > deadline) return go();
        setTimeout(poll, 400);
      }});
  }}
  poll();
}})();
</script>
</body></html>"""


@bp.route("/fix-from-watchdog")
def fix_from_watchdog():
    # 1. Decode the self-contained error payload.
    token = request.args.get("d", "")
    if not token:
        return Response("Missing payload", status=400)
    try:
        payload = _decode_token(token)
    except Exception:
        return Response("Invalid payload", status=400)

    # 2. Target the EXISTING CustomerNode project (NOT VibeNode, and NOT a fresh
    #    …customerNode_root entry the SPA wouldn't surface).
    encoded, cwd = _resolve_customernode_project()
    set_active_project(encoded)

    sm = current_app.session_manager

    # 3. Force manual approval so the agent pauses before the first edit. Policy
    #    is global-only in VibeNode; this is the only mechanism that guarantees a
    #    WAITING state regardless of the operator's prior setting.
    try:
        sm.set_permission_policy("manual")
    except Exception:
        current_app.logger.exception("fix-from-watchdog: could not force manual policy")

    # 4. Fresh session id (start_session requires a caller-supplied id).
    session_id = str(uuid.uuid4())

    # 5./6. Start the session in CustomerNode with the fix prompt. permission_mode
    #    'default' routes every edit through the approval callback.
    prompt = _build_fix_prompt(payload, date.today().isoformat())
    try:
        result = sm.start_session(
            session_id=session_id,
            prompt=prompt,
            cwd=cwd,
            name="Watchdog fix",
            resume=False,
            permission_mode="default",
        )
    except Exception:
        current_app.logger.exception("fix-from-watchdog: start_session failed")
        return Response("Failed to start fix session", status=502)
    if isinstance(result, dict) and result.get("error"):
        return Response(
            "Failed to start fix session: " + html_lib.escape(str(result["error"])),
            status=502,
        )

    # 7. Land the operator on the running session.
    return Response(_redirect_html(encoded, session_id), mimetype="text/html")
