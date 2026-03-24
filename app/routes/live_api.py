"""
Live session routes -- log streaming for the live terminal panel.

The old polling-based endpoints (/api/waiting, /api/respond, /api/close,
/api/interrupt) have been removed. Those operations now go through WebSocket
events handled in ws_events.py via the SessionManager.

The PreToolUse hook endpoint (/api/hook/pre-tool) handles permission requests
from the Claude CLI hook system, since the SDK's can_use_tool callback doesn't
work with CLI 2.x.
"""

import json
import os
import tempfile
import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

# Debug endpoint to test permission emit
from .. import socketio as _app_socketio

from ..config import _sessions_dir, get_active_project, _decode_project, _CLAUDE_PROJECTS

bp = Blueprint('live_api', __name__)


@bp.route("/api/_emit-permission", methods=["POST"])
def internal_emit_permission():
    """Internal endpoint: emit session_permission via SocketIO.

    Legacy endpoint — kept for compatibility. In the daemon architecture,
    permissions are pushed via IPC instead.
    """
    data = request.get_json(silent=True) or {}
    _app_socketio.emit('session_state', {
        'session_id': data.get('session_id', ''),
        'state': 'waiting',
        'cost_usd': 0,
        'error': None,
        'name': '',
    })
    _app_socketio.emit('session_permission', data)
    return jsonify({"ok": True})


@bp.route("/api/hook/pre-tool", methods=["POST"])
def hook_pre_tool():
    """Handle PreToolUse hook callback from Claude CLI.

    Proxies the request to the session daemon, which blocks until the
    user responds via the WebSocket permission_response event.
    """
    data = request.get_json(silent=True) or {}
    tool_name = data.get("tool_name", "unknown")
    tool_input = data.get("tool_input", {})
    session_id = data.get("session_id", "")

    sm = current_app.session_manager
    result = sm.hook_pre_tool(
        tool_name=tool_name,
        tool_input=tool_input,
        session_id=session_id,
    )
    if isinstance(result, dict):
        return jsonify(result)
    return jsonify({"action": "allow"})


@bp.route("/api/session-log/<session_id>")
def api_session_log(session_id):
    """Return structured log entries for the live terminal panel.

    If the session is managed by the SDK SessionManager, return entries
    from memory. Otherwise, fall back to reading the .jsonl file on disk
    (for historical sessions).
    """
    try:
        since = int(request.args.get("since", 0))
    except (ValueError, TypeError):
        since = 0

    # Check if this session is managed by the SDK
    sm = current_app.session_manager
    if sm.has_session(session_id):
        entries = sm.get_entries(session_id, since=since)
        return jsonify({"entries": entries, "total_lines": since + len(entries)})

    # Fall back to .jsonl file parsing for historical sessions
    path = _sessions_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404

    try:
        raw_lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return jsonify({"entries": [], "total_lines": 0})

    total = len(raw_lines)
    entries = []
    for raw in raw_lines[since:]:
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        t = obj.get("type", "")
        if t in ("file-history-snapshot", "custom-title", "progress"):
            continue
        if t == "user":
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                entries.append({"kind": "user", "text": content.strip()[:2000]})
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text", "").strip():
                        entries.append({"kind": "user", "text": block["text"].strip()[:2000]})
                    elif bt == "tool_result":
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            rt = " ".join(b.get("text", "") for b in rc if isinstance(b, dict) and b.get("type") == "text")
                        else:
                            rt = str(rc)
                        entries.append({
                            "kind": "tool_result",
                            "tool_use_id": block.get("tool_use_id", ""),
                            "text": rt[:600],
                            "is_error": bool(block.get("is_error"))
                        })
        elif t == "assistant":
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                entries.append({"kind": "asst", "text": content.strip()[:3000]})
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text", "").strip():
                        entries.append({"kind": "asst", "text": block["text"].strip()[:3000]})
                    elif bt == "tool_use":
                        inp = block.get("input") or {}
                        if "command" in inp:
                            desc = inp["command"][:300]
                        elif "path" in inp:
                            desc = inp["path"]
                            if "content" in inp:
                                desc += f" (write {len(str(inp.get('content','')))} chars)"
                        elif "pattern" in inp:
                            desc = inp["pattern"][:200]
                        elif inp:
                            first_key = next(iter(inp))
                            desc = f"{first_key}: {str(inp[first_key])[:200]}"
                        else:
                            desc = ""
                        entries.append({
                            "kind": "tool_use",
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "desc": desc
                        })
    return jsonify({"entries": entries, "total_lines": total})


# ---------------------------------------------------------------------------
# CLAUDE.md editor API
# ---------------------------------------------------------------------------

def _get_project_path() -> Path:
    """Resolve the active project's filesystem path."""
    proj = get_active_project()
    if proj:
        decoded = _decode_project(proj)
        p = Path(decoded)
        if p.is_dir():
            return p
    # Fall back: use sessions dir parent heuristic
    sd = _sessions_dir()
    if sd != _CLAUDE_PROJECTS:
        decoded = _decode_project(sd.name)
        p = Path(decoded)
        if p.is_dir():
            return p
    return Path.cwd()


@bp.route('/api/claude-md', methods=['GET'])
def get_claude_md():
    """Read CLAUDE.md from active project directory."""
    try:
        proj_path = _get_project_path()
        md_path = proj_path / "CLAUDE.md"
        if md_path.is_file():
            content = md_path.read_text(encoding="utf-8")
            return jsonify({"content": content, "path": str(md_path), "exists": True})
        return jsonify({"content": "", "path": str(md_path), "exists": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route('/api/claude-md', methods=['PUT'])
def put_claude_md():
    """Write CLAUDE.md to active project directory."""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or "content" not in data:
            return jsonify({"error": "Request body must contain 'content'"}), 400

        proj_path = _get_project_path()
        md_path = proj_path / "CLAUDE.md"
        md_path.write_text(data["content"], encoding="utf-8")
        return jsonify({"ok": True, "path": str(md_path)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route('/api/claude-md-global', methods=['GET'])
def get_claude_md_global():
    """Read ~/.claude/CLAUDE.md"""
    try:
        md_path = Path.home() / ".claude" / "CLAUDE.md"
        if md_path.is_file():
            content = md_path.read_text(encoding="utf-8")
            return jsonify({"content": content, "path": str(md_path), "exists": True})
        return jsonify({"content": "", "path": str(md_path), "exists": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route('/api/claude-md-global', methods=['PUT'])
def put_claude_md_global():
    """Write ~/.claude/CLAUDE.md"""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or "content" not in data:
            return jsonify({"error": "Request body must contain 'content'"}), 400

        md_path = Path.home() / ".claude" / "CLAUDE.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(data["content"], encoding="utf-8")
        return jsonify({"ok": True, "path": str(md_path)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Settings/config API
# ---------------------------------------------------------------------------

@bp.route('/api/config', methods=['GET'])
def get_config():
    """Read ~/.claude/settings.json"""
    try:
        settings_path = Path.home() / ".claude" / "settings.json"
        if settings_path.is_file():
            content = json.loads(settings_path.read_text(encoding="utf-8"))
            return jsonify(content)
        return jsonify({})
    except json.JSONDecodeError:
        return jsonify({"error": "settings.json contains invalid JSON"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route('/api/config', methods=['PUT'])
def put_config():
    """Write ~/.claude/settings.json"""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Request body must be a JSON object"}), 400

        settings_path = Path.home() / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        # Write atomically via temp file
        tmp_path = settings_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(settings_path)

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Folder-tree persistence API
# ---------------------------------------------------------------------------

_CLAUDE_DIR = Path.home() / ".claude"


def _folder_tree_path() -> Path:
    """Return path to the per-project folder tree JSON file."""
    proj = get_active_project()
    if proj:
        return _CLAUDE_DIR / f"gui_folder_tree_{proj}.json"
    # No active project yet — try to find any existing tree file
    candidates = list(_CLAUDE_DIR.glob("gui_folder_tree_*.json"))
    if candidates:
        return candidates[0]
    return _CLAUDE_DIR / "gui_folder_tree.json"


@bp.route('/api/folder-tree', methods=['GET'])
def get_folder_tree():
    """Read folder tree from ~/.claude/gui_folder_tree_{project}.json"""
    try:
        ft_path = _folder_tree_path()
        if ft_path.is_file():
            content = json.loads(ft_path.read_text(encoding="utf-8"))
            return jsonify(content)
        return jsonify({})
    except json.JSONDecodeError:
        return jsonify({}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route('/api/folder-tree', methods=['PUT'])
def put_folder_tree():
    """Write folder tree to ~/.claude/gui_folder_tree_{project}.json (atomic)."""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Request body must be a JSON object"}), 400

        ft_path = _folder_tree_path()
        ft_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: write to temp file in same directory, then rename
        fd, tmp_path = tempfile.mkstemp(
            dir=str(ft_path.parent), suffix=".tmp", prefix="gui_ft_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            # On Windows, target must not exist for os.rename; use replace
            os.replace(tmp_path, str(ft_path))
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Available models API
# ---------------------------------------------------------------------------

_models_cache = {"data": None, "ts": 0}

@bp.route('/api/models')
def get_models():
    """Return available Claude models dynamically from the Anthropic API."""
    import time as _time

    # Cache for 10 minutes
    if _models_cache["data"] and _time.time() - _models_cache["ts"] < 600:
        return jsonify(_models_cache["data"])

    models = []
    try:
        import subprocess
        # Query the CLI for its init message which includes the current model
        r = subprocess.run(
            ["claude", "-p", "hi", "--output-format", "stream-json",
             "--verbose", "--max-turns", "1"],
            capture_output=True, text=True, timeout=15
        )
        current_model = None
        for line in r.stdout.strip().split("\n"):
            try:
                d = json.loads(line)
                if d.get("type") == "system" and d.get("subtype") == "init":
                    current_model = d.get("model", "")
                if d.get("type") == "result":
                    model_usage = d.get("modelUsage", {})
                    for mid in model_usage:
                        info = model_usage[mid]
                        # Extract clean name from model ID
                        clean = mid.split("[")[0]  # remove [1m] suffix
                        models.append({
                            "id": clean,
                            "name": clean.replace("claude-", "Claude ").replace("-", " ").title(),
                            "context_window": info.get("contextWindow", 0),
                            "max_output": info.get("maxOutputTokens", 0),
                            "current": mid == current_model,
                        })
            except (json.JSONDecodeError, KeyError):
                continue
    except Exception:
        pass

    # Always include the alias shortcuts the CLI accepts
    aliases = [
        {"id": "", "name": "Default", "desc": "Uses your Claude Code settings", "default": True},
        {"id": "sonnet", "name": "Sonnet", "desc": "Fast, capable, balanced", "alias": True},
        {"id": "opus", "name": "Opus", "desc": "Most capable, deeper reasoning", "alias": True},
        {"id": "haiku", "name": "Haiku", "desc": "Fastest, most cost-efficient", "alias": True},
    ]

    result = aliases + models
    _models_cache["data"] = result
    _models_cache["ts"] = _time.time()
    return jsonify(result)


# ---------------------------------------------------------------------------
# Agent catalog file — write agent definitions to a temp file so the system
# prompt only needs a compact index + file path (saves ~8K tokens).
# ---------------------------------------------------------------------------

# Cache keyed by project encoded string → absolute file path
_agent_catalog_paths: dict[str, str] = {}


@bp.route('/api/agents/write-catalog', methods=['POST'])
def write_agent_catalog():
    """Write agent definitions to a per-project temp file.

    Receives a JSON body with an ``agents`` array of
    ``{id, label, systemPrompt}`` objects.  Writes them as a readable Markdown
    file scoped to the current project and returns
    ``{ok: true, path: "<absolute path>"}``.
    """
    proj = get_active_project() or "default"

    # Fast path: already written for this project
    cached = _agent_catalog_paths.get(proj)
    if cached and os.path.isfile(cached):
        return jsonify({"ok": True, "path": cached})

    data = request.get_json(silent=True) or {}
    agents = data.get('agents', [])
    if not agents:
        return jsonify({"ok": False, "error": "No agents provided"}), 400

    # Build the complete catalog file: instructions + index + full prompts.
    # Everything Claude needs is in this one file so the system prompt can
    # be a single-line pointer.
    agent_count = len(agents)
    lines = [
        "# AVAILABLE AGENTS",
        "",
        f"You have {agent_count} specialist agents available in your workforce.",
        "These agents are defined HERE in this file — do NOT look for them on",
        "disk or in .claude/agents/. This is the authoritative and complete list.",
        "",
        "When a task would benefit from a specialist, use the Agent tool to spawn",
        "one. Copy that agent's FULL system prompt (provided below) into the",
        'Agent tool\'s "prompt" parameter so the subprocess adopts that role.',
        "",
        "When a user asks what agents are available, list them from this file.",
        "Do not search the filesystem for agent definitions.",
        "",
        "## Agent Index",
        "",
    ]
    for agent in agents:
        aid = agent.get('id', '')
        label = agent.get('label', aid)
        lines.append(f"- **{label}** ({aid})")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Full Agent Definitions")
    lines.append("")
    for agent in agents:
        aid = agent.get('id', '')
        label = agent.get('label', aid)
        prompt = agent.get('systemPrompt', '')
        lines.append(f"### {label} ({aid})")
        lines.append(prompt)
        lines.append("")

    content = "\n".join(lines)

    # Write to a per-project file in the OS temp directory
    filepath = os.path.join(
        tempfile.gettempdir(),
        f"claude-gui-agent-catalog_{proj}.md",
    )
    try:
        tmp = filepath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        if os.path.exists(filepath):
            os.replace(tmp, filepath)
        else:
            os.rename(tmp, filepath)
        _agent_catalog_paths[proj] = filepath
        return jsonify({"ok": True, "path": filepath})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
