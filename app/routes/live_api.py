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
import re
import shutil
import tempfile
import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from werkzeug.utils import secure_filename

# Debug endpoint to test permission emit
from .. import socketio as _app_socketio

from ..config import _sessions_dir, get_active_project, _decode_project, _CLAUDE_PROJECTS

# System-user message classification — shared with ws_events.py
from ..platform_utils import is_system_user_content as _is_system_user_content
from ..platform_utils import system_user_label as _system_user_label

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
    Auto-retries on transient daemon IPC failures.
    """
    import time as _time

    data = request.get_json(silent=True) or {}
    tool_name = data.get("tool_name", "unknown")
    tool_input = data.get("tool_input", {})
    session_id = data.get("session_id", "")

    sm = current_app.session_manager

    # Retry up to 3 times on IPC errors (daemon reconnecting)
    for _attempt in range(3):
        try:
            result = sm.hook_pre_tool(
                tool_name=tool_name,
                tool_input=tool_input,
                session_id=session_id,
            )
        except Exception:
            result = {"ok": False, "error": "exception"}

        if isinstance(result, dict):
            # If daemon returned an IPC error, retry after brief wait
            if not result.get("ok", True) and result.get("error") and _attempt < 2:
                _time.sleep(1)
                continue
            # Success or final attempt — check for action
            if "action" in result:
                return jsonify(result)
        # No action key means IPC error — allow by default on final attempt
        if _attempt == 2:
            return jsonify({"action": "allow"})

    return jsonify({"action": "allow"})


@bp.route("/api/live/state/<session_id>")
def api_live_state(session_id):
    """Lightweight endpoint returning just the session state + entry count.

    Used by the frontend watchdog to bypass WebSocket and get ground truth
    when the UI suspects it's stuck. The entry_count field lets the watchdog
    detect missing entries even when the state is correct.
    """
    sm = current_app.session_manager
    state = sm.get_session_state(session_id)
    if state is None:
        return jsonify({"state": "stopped", "managed": False, "entry_count": 0})
    # Include entry count so the watchdog can detect missing entries
    entry_count = len(sm.get_entries(session_id)) if hasattr(sm, 'get_entries') else 0
    return jsonify({"state": state, "managed": True, "entry_count": entry_count})


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
    project = request.args.get("project", "").strip()
    path = _sessions_dir(project=project) / f"{session_id}.jsonl"
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
                text = content.strip()[:20000]
                if _is_system_user_content(text):
                    entries.append({"kind": "system", "text": _system_user_label(text)})
                else:
                    entries.append({"kind": "user", "text": text})
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text", "").strip():
                        text = block["text"].strip()[:20000]
                        if _is_system_user_content(text):
                            entries.append({"kind": "system", "text": _system_user_label(text)})
                        else:
                            entries.append({"kind": "user", "text": text})
                    elif bt == "tool_result":
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            rt = " ".join(b.get("text", "") for b in rc if isinstance(b, dict) and b.get("type") == "text")
                        else:
                            rt = str(rc)
                        entries.append({
                            "kind": "tool_result",
                            "tool_use_id": block.get("tool_use_id", ""),
                            "text": rt[:20000],
                            "is_error": bool(block.get("is_error"))
                        })
        elif t == "assistant":
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                entries.append({"kind": "asst", "text": content.strip()[:50000]})
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text", "").strip():
                        entries.append({"kind": "asst", "text": block["text"].strip()[:50000]})
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
    sd = _sessions_dir(project=proj)
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
        import sys as _sys
        _nw = subprocess.CREATE_NO_WINDOW if _sys.platform == "win32" else 0
        r = subprocess.run(
            ["claude", "-p", "hi", "--output-format", "stream-json",
             "--verbose", "--max-turns", "1"],
            capture_output=True, text=True, timeout=15, creationflags=_nw
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

    # Fast path: already written for this project (skip if ?force=1)
    force = request.args.get('force') == '1'
    cached = _agent_catalog_paths.get(proj)
    if cached and os.path.isfile(cached) and not force:
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


# ---------------------------------------------------------------------------
# Workforce — read .md assets from disk
# ---------------------------------------------------------------------------

_WORKFORCE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'workforce')


@bp.route('/api/workforce/assets', methods=['GET'])
def get_workforce_assets():
    """Read all .md files from the workforce directory and return parsed assets."""
    if not os.path.isdir(_WORKFORCE_DIR):
        return jsonify({"ok": True, "assets": [], "map": None, "source": "none"})

    assets = []
    wf_map = None

    for fname in os.listdir(_WORKFORCE_DIR):
        if not fname.endswith('.md'):
            continue
        fpath = os.path.join(_WORKFORCE_DIR, fname)
        if not os.path.isfile(fpath):
            continue

        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception:
            continue

        # Parse YAML frontmatter (simple key: value pairs, no PyYAML needed)
        frontmatter = {}
        body = content
        if content.startswith('---'):
            parts = content.split('---', 2)
            if len(parts) >= 3:
                for line in parts[1].strip().splitlines():
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if ':' in line:
                        k, v = line.split(':', 1)
                        k = k.strip()
                        v = v.strip()
                        # Handle simple arrays: [a, b, c]
                        if v.startswith('[') and v.endswith(']'):
                            v = [x.strip().strip("'\"") for x in v[1:-1].split(',') if x.strip()]
                        # Handle booleans
                        elif v.lower() == 'true':
                            v = True
                        elif v.lower() == 'false':
                            v = False
                        elif v.lower() == 'null' or v == '':
                            v = None
                        else:
                            v = v.strip("'\"")
                        frontmatter[k] = v
                body = parts[2].strip()

        # workforce-map.md is special
        if frontmatter.get('type') == 'workforce-map':
            wf_map = {"frontmatter": frontmatter, "body": body}
            continue

        asset_id = frontmatter.get('id', fname.replace('.md', ''))
        assets.append({
            "id": asset_id,
            "name": frontmatter.get('name', asset_id),
            "department": frontmatter.get('department', ''),
            "tags": frontmatter.get('tags', []),
            "active": frontmatter.get('active', True),
            "version": frontmatter.get('version'),
            "allowed_tools": frontmatter.get('allowed-tools'),
            "source": frontmatter.get('source'),
            "systemPrompt": body,
        })

    return jsonify({"ok": True, "assets": assets, "map": wf_map, "source": "disk"})


# ---------------------------------------------------------------------------
# Workforce — discover agents/skills from Claude config directories
# ---------------------------------------------------------------------------

def _parse_frontmatter(content):
    """Parse YAML frontmatter from .md content (simple key:value, no PyYAML)."""
    frontmatter = {}
    body = content
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if ':' in line:
                    k, v = line.split(':', 1)
                    k = k.strip()
                    v = v.strip()
                    if v.startswith('[') and v.endswith(']'):
                        v = [x.strip().strip("'\"") for x in v[1:-1].split(',') if x.strip()]
                    elif v.lower() == 'true':
                        v = True
                    elif v.lower() == 'false':
                        v = False
                    elif v.lower() == 'null' or v == '':
                        v = None
                    else:
                        v = v.strip("'\"")
                    frontmatter[k] = v
            body = parts[2].strip()
    return frontmatter, body


def _detect_tier(frontmatter):
    """Detect asset tier from frontmatter fields."""
    if frontmatter.get('preamble-tier') or frontmatter.get('source'):
        return 'pipeline'
    if frontmatter.get('allowed-tools') or frontmatter.get('version'):
        return 'skill'
    return 'role'


def _scan_md_files(directory, source_type, pack_name=None, prefix=None):
    """Scan a directory for .md files and return parsed asset dicts."""
    results = []
    d = Path(directory)
    if not d.is_dir():
        return results
    for fpath in sorted(d.iterdir()):
        if not fpath.suffix == '.md' or not fpath.is_file():
            continue
        try:
            content = fpath.read_text(encoding='utf-8')
        except Exception:
            continue
        frontmatter, body = _parse_frontmatter(content)
        raw_id = frontmatter.get('id', fpath.stem)
        if prefix:
            raw_id = f"{prefix}-{raw_id}"
        name = frontmatter.get('name')
        if not name:
            name = fpath.stem.replace('-', ' ').replace('_', ' ').title()
            if prefix:
                name = f"{prefix.title()}: {name}"
        tier = _detect_tier(frontmatter) if frontmatter else 'role'
        results.append({
            "id": raw_id,
            "name": name,
            "tier": tier,
            "source": source_type,
            "pack": pack_name,
            "path": str(fpath.resolve()),
            "systemPrompt": body,
            "frontmatter": frontmatter,
            "already_imported": False,  # filled in later
        })
    return results


@bp.route('/api/workforce/discover', methods=['GET'])
def discover_workforce():
    """Scan filesystem for existing Claude agent/skill definitions."""
    home = Path(os.path.expanduser('~'))
    discovered = []

    # Collect IDs already present in workforce/ dir for already_imported check
    existing_ids = set()
    if os.path.isdir(_WORKFORCE_DIR):
        for fname in os.listdir(_WORKFORCE_DIR):
            if fname.endswith('.md') and os.path.isfile(os.path.join(_WORKFORCE_DIR, fname)):
                try:
                    with open(os.path.join(_WORKFORCE_DIR, fname), 'r', encoding='utf-8') as f:
                        c = f.read()
                    fm, _ = _parse_frontmatter(c)
                    existing_ids.add(fm.get('id', fname.replace('.md', '')))
                except Exception:
                    existing_ids.add(fname.replace('.md', ''))

    # 1. Global agents: ~/.claude/agents/
    global_agents_dir = home / '.claude' / 'agents'
    discovered.extend(_scan_md_files(global_agents_dir, 'global_agent'))

    # 2. Project-scoped agents: {active_project}/.claude/agents/
    try:
        proj = get_active_project()
        if proj:
            proj_agents_dir = Path(proj) / '.claude' / 'agents'
            discovered.extend(_scan_md_files(proj_agents_dir, 'project_agent'))
    except Exception:
        pass

    # 3. Skill packs: ~/.claude/skills/*/
    skills_dir = home / '.claude' / 'skills'
    if skills_dir.is_dir():
        for pack_dir in sorted(skills_dir.iterdir()):
            if not pack_dir.is_dir():
                continue
            pack_name = pack_dir.name
            # Check if this is a multi-skill pack (has subdirectories with SKILL.md)
            sub_skills = [sd for sd in pack_dir.iterdir()
                          if sd.is_dir() and (sd / 'SKILL.md').is_file()]
            if sub_skills:
                # It's a pack with sub-skills
                for sub_dir in sorted(sub_skills):
                    skill_file = sub_dir / 'SKILL.md'
                    try:
                        content = skill_file.read_text(encoding='utf-8')
                    except Exception:
                        continue
                    frontmatter, body = _parse_frontmatter(content)
                    raw_id = frontmatter.get('id', f"{pack_name}-{sub_dir.name}")
                    name = frontmatter.get('name')
                    if not name:
                        name = f"{pack_name.title()}: {sub_dir.name.replace('-', ' ').replace('_', ' ').title()}"
                    tier = _detect_tier(frontmatter) if frontmatter else 'skill'
                    discovered.append({
                        "id": raw_id,
                        "name": name,
                        "tier": tier,
                        "source": "skill_pack",
                        "pack": pack_name,
                        "path": str(skill_file.resolve()),
                        "systemPrompt": body,
                        "frontmatter": frontmatter,
                        "already_imported": False,
                    })
            else:
                # Single skill or flat .md files in the pack dir
                # Check for SKILL.md at the pack root
                root_skill = pack_dir / 'SKILL.md'
                if root_skill.is_file():
                    try:
                        content = root_skill.read_text(encoding='utf-8')
                    except Exception:
                        content = None
                    if content:
                        frontmatter, body = _parse_frontmatter(content)
                        raw_id = frontmatter.get('id', pack_name)
                        name = frontmatter.get('name', pack_name.replace('-', ' ').replace('_', ' ').title())
                        tier = _detect_tier(frontmatter) if frontmatter else 'skill'
                        discovered.append({
                            "id": raw_id,
                            "name": name,
                            "tier": tier,
                            "source": "skill_pack",
                            "pack": pack_name,
                            "path": str(root_skill.resolve()),
                            "systemPrompt": body,
                            "frontmatter": frontmatter,
                            "already_imported": False,
                        })
                # Also scan any loose .md files
                discovered.extend(_scan_md_files(pack_dir, 'skill_pack', pack_name=pack_name, prefix=pack_name))

    # 4. Plugins: ~/.claude/plugins/*/  (with agents/ and skills/ subdirs)
    plugins_dir = home / '.claude' / 'plugins'
    if plugins_dir.is_dir():
        for plugin_dir in sorted(plugins_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue
            plugin_agents = plugin_dir / 'agents'
            discovered.extend(_scan_md_files(plugin_agents, 'plugin', pack_name=plugin_dir.name))
            plugin_skills = plugin_dir / 'skills'
            discovered.extend(_scan_md_files(plugin_skills, 'plugin', pack_name=plugin_dir.name))

    # Set already_imported flags
    for item in discovered:
        item['already_imported'] = item['id'] in existing_ids

    return jsonify({"ok": True, "discovered": discovered})


@bp.route('/api/workforce/write-asset', methods=['POST'])
def write_workforce_asset():
    """Write a .md asset file to the workforce directory."""
    data = request.get_json(silent=True) or {}
    asset_id = data.get('id', '')
    content = data.get('content', '')
    if not asset_id or not content:
        return jsonify({"ok": False, "error": "Missing id or content"}), 400

    # Sanitize filename
    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '', asset_id)
    if not safe_id:
        return jsonify({"ok": False, "error": "Invalid id"}), 400

    filepath = os.path.join(_WORKFORCE_DIR, safe_id + '.md')
    try:
        os.makedirs(_WORKFORCE_DIR, exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        # Invalidate catalog cache
        proj = get_active_project() or "default"
        _agent_catalog_paths.pop(proj, None)
        return jsonify({"ok": True, "path": filepath})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route('/api/workforce/install-pack', methods=['POST'])
def install_pack():
    """Clone a skill pack repo, scan for .md/SKILL.md files, import as workforce assets."""
    import subprocess, shutil

    data = request.get_json(silent=True) or {}
    pack_id = data.get('pack_id', '')
    git_url = data.get('git_url', '')
    setup_cmd = data.get('setup_cmd')

    if not pack_id or not git_url:
        return jsonify({"ok": False, "error": "Missing pack_id or git_url"}), 400

    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '', pack_id)
    skills_dir = Path.home() / '.claude' / 'skills' / safe_id

    # Step 1: Clone if not already present
    cloned = False
    if not skills_dir.is_dir():
        try:
            result = subprocess.run(
                ['git', 'clone', '--single-branch', '--depth', '1', git_url, str(skills_dir)],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return jsonify({"ok": False, "error": f"git clone failed: {result.stderr[:200]}"}), 500
            cloned = True
        except subprocess.TimeoutExpired:
            return jsonify({"ok": False, "error": "git clone timed out (120s)"}), 500
        except FileNotFoundError:
            return jsonify({"ok": False, "error": "git not found on system"}), 500

    # Step 2: Run setup if specified (non-blocking, best-effort)
    if setup_cmd and cloned:
        try:
            subprocess.Popen(
                setup_cmd, shell=True, cwd=str(skills_dir),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            pass  # setup is optional

    # Step 3: Scan for .md and SKILL.md files
    imported = 0
    os.makedirs(_WORKFORCE_DIR, exist_ok=True)

    for root_dir, dirs, files in os.walk(str(skills_dir)):
        # Skip hidden dirs, node_modules, dist, .git
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', 'dist', 'bin', 'test', 'tests')]

        for fname in files:
            # STRICT: Only import SKILL.md files (the standard skill definition format)
            # Everything else (README, docs, templates, reference material) is not a skill
            if fname != 'SKILL.md':
                continue

            # Use parent directory name as skill name
            parent_dir = os.path.basename(root_dir)
            if parent_dir == safe_id:
                continue  # Top-level SKILL.md is the pack index, not a skill

            fpath = os.path.join(root_dir, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception:
                continue

            # Parse frontmatter
            frontmatter = {}
            body = content
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    frontmatter, body = _parse_frontmatter(content)

            if not body or len(body) < 20:
                continue

            asset_id = safe_id + '-' + re.sub(r'[^a-zA-Z0-9_-]', '', parent_dir)
            asset_name = frontmatter.get('name', parent_dir.replace('-', ' ').title())

            # Write workforce .md file
            out = '---\n'
            out += f'id: {asset_id}\n'
            out += f'name: {asset_name}\n'
            out += f'department: {safe_id}\n'
            out += f'source: {safe_id}\n'
            if frontmatter.get('allowed-tools'):
                out += f'allowed-tools: {frontmatter["allowed-tools"]}\n'
            if frontmatter.get('version'):
                out += f'version: {frontmatter["version"]}\n'
            out += '---\n\n' + body

            out_path = os.path.join(_WORKFORCE_DIR, asset_id + '.md')
            try:
                with open(out_path, 'w', encoding='utf-8') as f:
                    f.write(out)
                imported += 1
            except Exception:
                pass

    # Invalidate catalog
    proj = get_active_project() or "default"
    _agent_catalog_paths.pop(proj, None)

    return jsonify({"ok": True, "cloned": cloned, "imported": imported, "path": str(skills_dir)})


@bp.route('/api/workforce/delete-asset', methods=['POST'])
def delete_workforce_asset():
    """Delete a single .md asset from the workforce directory."""
    data = request.get_json(silent=True) or {}
    asset_id = data.get('id', '')
    if not asset_id:
        return jsonify({"ok": False, "error": "Missing id"}), 400
    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '', asset_id)
    filepath = os.path.join(_WORKFORCE_DIR, safe_id + '.md')
    if os.path.isfile(filepath):
        os.remove(filepath)
        proj = get_active_project() or "default"
        _agent_catalog_paths.pop(proj, None)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "File not found"}), 404


@bp.route('/api/workforce/uninstall-pack', methods=['POST'])
def uninstall_pack():
    """Uninstall a community skill pack — delete imported assets and the cloned directory."""
    data = request.get_json(silent=True) or {}
    pack_id = data.get('pack_id', '')
    if not pack_id:
        return jsonify({"ok": False, "error": "Missing pack_id"}), 400

    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '', pack_id)
    assets_deleted = 0
    dir_deleted = False

    # 1. Remove imported .md files from workforce/ that have source: pack_id
    if os.path.isdir(_WORKFORCE_DIR):
        for fname in os.listdir(_WORKFORCE_DIR):
            if not fname.endswith('.md'):
                continue
            fpath = os.path.join(_WORKFORCE_DIR, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    head = f.read(500)
                if f'source: {safe_id}' in head or fname.startswith(safe_id + '-'):
                    os.remove(fpath)
                    assets_deleted += 1
            except Exception:
                pass

    # 2. Remove cloned directory from ~/.claude/skills/
    skills_dir = Path.home() / '.claude' / 'skills' / safe_id
    if skills_dir.is_dir():
        import shutil
        try:
            shutil.rmtree(skills_dir)
            dir_deleted = True
        except Exception:
            pass

    # Invalidate catalog cache
    proj = get_active_project() or "default"
    _agent_catalog_paths.pop(proj, None)

    return jsonify({"ok": True, "assets_deleted": assets_deleted, "dir_deleted": dir_deleted})


@bp.route('/api/workforce/uninstall-builtin', methods=['POST'])
def uninstall_builtin_workforce():
    """Delete all .md files from the workforce directory."""
    if not os.path.isdir(_WORKFORCE_DIR):
        return jsonify({"ok": True, "deleted": 0})
    deleted = 0
    for fname in os.listdir(_WORKFORCE_DIR):
        if fname.endswith('.md'):
            try:
                os.remove(os.path.join(_WORKFORCE_DIR, fname))
                deleted += 1
            except Exception:
                pass
    # Invalidate catalog cache
    proj = get_active_project() or "default"
    _agent_catalog_paths.pop(proj, None)
    return jsonify({"ok": True, "deleted": deleted})


# ---------------------------------------------------------------------------
# File-drop API — drag-and-drop file upload
# ---------------------------------------------------------------------------

def _is_within_home(p: Path) -> bool:
    """Check that a resolved path is within the user's home directory."""
    try:
        p.resolve().relative_to(Path.home().resolve())
        return True
    except ValueError:
        return False


def _dedup_filename(directory: Path, name: str) -> str:
    """Return a unique filename in directory, adding numeric suffix if needed."""
    candidate = directory / name
    if not candidate.exists():
        return name
    stem = Path(name).stem
    suffix = Path(name).suffix
    n = 1
    while True:
        new_name = f"{stem} ({n}){suffix}"
        if not (directory / new_name).exists():
            return new_name
        n += 1


@bp.route('/api/file-info')
def api_file_info():
    """Return name, human-readable size, and existence for a file path."""
    raw = request.args.get('path', '')
    if not raw:
        return jsonify({"error": "No path provided"}), 400

    p = Path(raw).resolve()
    if not _is_within_home(p):
        return jsonify({"error": "Path must be within home"}), 403

    exists = p.is_file()
    name = p.name
    size = ""
    if exists:
        try:
            n = p.stat().st_size
            if n < 1024:
                size = f"{n} B"
            elif n < 1024 * 1024:
                size = f"{n / 1024:.1f} KB"
            else:
                size = f"{n / (1024 * 1024):.1f} MB"
        except OSError:
            size = ""

    return jsonify({"name": name, "size": size, "exists": exists})


@bp.route('/api/open-file', methods=['POST'])
def api_open_file():
    """Open a file using the system default application (Windows os.startfile)."""
    data = request.get_json(silent=True) or {}
    raw = data.get('path', '')
    if not raw:
        return jsonify({"error": "No path provided"}), 400

    p = Path(raw).resolve()
    if not _is_within_home(p):
        return jsonify({"error": "Path must be within home"}), 403

    if not p.is_file():
        return jsonify({"error": "File does not exist"}), 404

    try:
        os.startfile(str(p))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True})


@bp.route('/api/download-to-downloads', methods=['POST'])
def api_download_to_downloads():
    """Copy a file to the user's Downloads folder with dedup naming."""
    data = request.get_json(silent=True) or {}
    raw = data.get('path', '')
    if not raw:
        return jsonify({"error": "No path provided"}), 400

    p = Path(raw).resolve()
    if not _is_within_home(p):
        return jsonify({"error": "Path must be within home"}), 403

    if not p.is_file():
        return jsonify({"error": "File does not exist"}), 404

    downloads = Path.home() / "Downloads"
    if not downloads.is_dir():
        return jsonify({"error": "Downloads folder not found"}), 500

    filename = _dedup_filename(downloads, p.name)
    dest = downloads / filename
    try:
        shutil.copy2(str(p), str(dest))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, "filename": filename, "dest": str(dest)})


@bp.route('/api/copy-file-to', methods=['POST'])
def api_copy_file_to():
    """Copy a file to a user-chosen directory with dedup naming."""
    data = request.get_json(silent=True) or {}
    source = data.get('source', '')
    target_dir = data.get('target_dir', '')
    if not source or not target_dir:
        return jsonify({"error": "source and target_dir required"}), 400

    src = Path(source).resolve()
    dst_dir = Path(target_dir).resolve()

    if not _is_within_home(src):
        return jsonify({"error": "Source must be within home"}), 403
    if not _is_within_home(dst_dir):
        return jsonify({"error": "Target must be within home"}), 403
    if not src.is_file():
        return jsonify({"error": "Source file does not exist"}), 404
    if not dst_dir.is_dir():
        return jsonify({"error": "Target directory does not exist"}), 404

    filename = _dedup_filename(dst_dir, src.name)
    dest = dst_dir / filename
    try:
        shutil.copy2(str(src), str(dest))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, "filename": filename, "path": str(dest)})


@bp.route('/api/file-drop', methods=['POST'])
def api_file_drop():
    """Accept a multipart file upload and save to target_dir."""
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files['file']
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    target_dir = request.form.get('target_dir', '')
    if not target_dir:
        return jsonify({"error": "No target_dir provided"}), 400

    target = Path(target_dir).resolve()
    if not _is_within_home(target):
        return jsonify({"error": "Target directory must be within home"}), 403

    if not target.is_dir():
        return jsonify({"error": "Target directory does not exist"}), 400

    filename = secure_filename(f.filename) or f.filename
    filename = _dedup_filename(target, filename)
    dest = target / filename
    f.save(str(dest))

    return jsonify({"ok": True, "path": str(dest), "filename": filename})


@bp.route('/api/browse-dir')
def api_browse_dir():
    """Return immediate subdirectory names for the given path."""
    raw = request.args.get('path', '')
    if not raw:
        return jsonify({"error": "No path provided"}), 400

    target = Path(raw).resolve()
    if not _is_within_home(target):
        return jsonify({"error": "Path must be within home"}), 403

    if not target.is_dir():
        return jsonify({"error": "Not a directory"}), 400

    dirs = []
    try:
        for entry in sorted(target.iterdir()):
            if entry.is_dir() and not entry.name.startswith('.'):
                dirs.append(entry.name)
    except PermissionError:
        pass

    return jsonify({"path": str(target), "dirs": dirs})


@bp.route('/api/project-path')
def api_project_path():
    """Return the active project's filesystem path."""
    return jsonify({"path": str(_get_project_path())})


@bp.route('/api/default-save-dir')
def api_default_save_dir():
    """Return a sensible default save directory (Downloads)."""
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        return jsonify({"path": str(downloads)})
    return jsonify({"path": str(Path.home())})
