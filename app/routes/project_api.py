"""
Project picker routes -- list, switch, rename, delete, add, find, chat, new-session.
"""

import re
import shutil
import sys
import uuid as uuid_mod
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from ..platform_utils import native_folder_picker, default_project_roots

from ..config import (
    _CLAUDE_PROJECTS,
    _decode_project,
    _encode_cwd,
    _save_name,
    _sessions_dir,
    get_active_project,
    set_active_project,
    _load_project_names,
    _save_project_names,
)

bp = Blueprint('project_api', __name__)


@bp.route("/api/projects")
def api_projects():
    # On Windows, only show projects under ~/Documents.
    # On Mac/Linux, show all projects under ~/ (Documents may not exist).
    if sys.platform == "win32":
        filter_base = str(Path.home() / "Documents").replace("\\", "/").lower()
    else:
        filter_base = str(Path.home()).replace("\\", "/").lower()
    active_project = get_active_project()
    project_names = _load_project_names()
    results = []
    if not _CLAUDE_PROJECTS.is_dir():
        return jsonify(results)
    for d in sorted(_CLAUDE_PROJECTS.iterdir()):
        if not d.is_dir() or d.name.startswith("subagents"):
            continue
        display = _decode_project(d.name)
        # Only show projects that live under the platform-appropriate base
        if not display.replace("\\", "/").lower().startswith(filter_base + "/"):
            continue
        count = sum(1 for _ in d.glob("*.jsonl"))
        results.append({
            "encoded": d.name,
            "display": display,
            "custom_name": project_names.get(d.name, ""),
            "session_count": count,
            "active": d.name == active_project,
        })
    return jsonify(results)


@bp.route("/api/set-project", methods=["POST"])
def api_set_project():
    encoded = (request.get_json() or {}).get("project", "").strip()
    target = _CLAUDE_PROJECTS / encoded
    if not target.is_dir():
        return jsonify({"error": "Not found"}), 404
    set_active_project(encoded)
    return jsonify({"ok": True, "project": encoded, "display": _decode_project(encoded)})


@bp.route("/api/rename-project", methods=["POST"])
def api_rename_project():
    data = request.get_json() or {}
    encoded = data.get("encoded", "").strip()
    name = data.get("name", "").strip()
    if not encoded:
        return jsonify({"ok": False, "error": "Missing project"}), 400
    names = _load_project_names()
    if name:
        names[encoded] = name
    else:
        names.pop(encoded, None)
    _save_project_names(names)
    return jsonify({"ok": True})


@bp.route("/api/delete-project", methods=["POST"])
def api_delete_project():
    data = request.get_json() or {}
    encoded = data.get("encoded", "").strip()
    target = _CLAUDE_PROJECTS / encoded
    if not target.is_dir():
        return jsonify({"ok": False, "error": "Project not found"}), 404
    try:
        shutil.rmtree(target)
    except Exception as e:
        return jsonify({"ok": False, "error": "Could not delete: " + str(e)}), 500
    # If we deleted the active project, reset
    if get_active_project() == encoded:
        set_active_project("")
    # Clean up project name
    names = _load_project_names()
    names.pop(encoded, None)
    _save_project_names(names)
    return jsonify({"ok": True})


@bp.route("/api/add-project", methods=["POST"])
def api_add_project():
    """Add a project via browse (folder picker), path, or create new."""
    data = request.get_json() or {}
    mode = data.get("mode", "browse")

    if mode == "browse":
        chosen, err = native_folder_picker()
        if err == "cancelled" or (err is None and chosen is None):
            return jsonify({"ok": False, "cancelled": True})
        if err:
            return jsonify({"ok": False, "error": err}), 500
        path = chosen

    elif mode == "path":
        path = data.get("path", "").strip()
        if not path or not Path(path).is_dir():
            return jsonify({"ok": False, "error": "Invalid path"}), 400

    elif mode == "create":
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"ok": False, "error": "No name provided"}), 400
        if sys.platform == "linux":
            path = str(Path.home() / name)
        else:
            path = str(Path.home() / "Documents" / name)
        Path(path).mkdir(parents=True, exist_ok=True)

    else:
        return jsonify({"ok": False, "error": "Unknown mode"}), 400

    encoded = _encode_cwd(path)
    target = _CLAUDE_PROJECTS / encoded
    target.mkdir(parents=True, exist_ok=True)
    return jsonify({"ok": True, "encoded": encoded, "path": path})


@bp.route("/api/find-projects")
def api_find_projects():
    """Scan common directories for code projects not yet registered."""
    existing = {d.name for d in _CLAUDE_PROJECTS.iterdir() if d.is_dir()} if _CLAUDE_PROJECTS.is_dir() else set()
    indicators = [".git", "package.json", "Cargo.toml", "go.mod", "pyproject.toml",
                  "requirements.txt", "pom.xml", "build.gradle", "Makefile",
                  ".sln", ".csproj", "CMakeLists.txt", "Gemfile", "composer.json"]
    scan_roots = default_project_roots()
    found = []
    seen_paths = set()
    for root in scan_roots:
        if not root.is_dir():
            continue
        try:
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                encoded = _encode_cwd(str(child))
                if encoded in existing or str(child) in seen_paths:
                    continue
                # Check for code project indicators
                detected = []
                for ind in indicators:
                    if (child / ind).exists():
                        detected.append(ind)
                if detected:
                    proj_type = detected[0].replace(".", "").replace("_", " ").title()
                    if ".git" in detected:
                        proj_type = "Git repo"
                    found.append({
                        "path": str(child),
                        "name": child.name,
                        "encoded": encoded,
                        "type": proj_type,
                        "indicators": detected,
                    })
                    seen_paths.add(str(child))
        except PermissionError:
            continue
    return jsonify({"projects": found})


@bp.route("/api/project-chat", methods=["POST"])
def api_project_chat():
    """AI-assisted project finder. Searches filesystem based on user description."""
    data = request.get_json() or {}
    user_msg = data.get("message", "").strip().lower()
    if not user_msg:
        return jsonify({"content": "Tell me what kind of project you're looking for.", "suggestions": []})

    # Search for projects matching the description
    search_roots = default_project_roots() + [Path.home()]
    indicators = {
        ".git": "Git",
        "package.json": "Node.js",
        "pyproject.toml": "Python",
        "requirements.txt": "Python",
        "Cargo.toml": "Rust",
        "go.mod": "Go",
        "pom.xml": "Java/Maven",
        "build.gradle": "Java/Gradle",
        ".sln": ".NET",
        "Gemfile": "Ruby",
        "composer.json": "PHP",
        "CMakeLists.txt": "C/C++",
    }
    # Keywords to match against directory names
    keywords = [w for w in re.split(r'\W+', user_msg) if len(w) > 2]

    existing = {d.name for d in _CLAUDE_PROJECTS.iterdir() if d.is_dir()} if _CLAUDE_PROJECTS.is_dir() else set()
    matches = []
    max_depth = 2

    def _scan(root, depth=0):
        if depth > max_depth or len(matches) >= 15:
            return
        try:
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name.startswith(".") or child.name in ("node_modules", "__pycache__", ".git", "venv", ".venv"):
                    continue
                name_lower = child.name.lower()
                # Check if directory name matches any keyword
                name_match = any(kw in name_lower for kw in keywords)
                # Check for project indicators
                detected = [ind for ind, label in indicators.items() if (child / ind).exists()]
                encoded = _encode_cwd(str(child))

                if (name_match or detected) and encoded not in existing:
                    tech = ", ".join(indicators[d] for d in detected if d in indicators) or "Folder"
                    score = (2 if name_match else 0) + len(detected)
                    matches.append({"path": str(child), "name": child.name, "type": tech, "encoded": encoded, "score": score})

                if depth < max_depth:
                    _scan(child, depth + 1)
        except PermissionError:
            pass

    for root in search_roots:
        if root.is_dir():
            _scan(root)

    matches.sort(key=lambda x: x["score"], reverse=True)
    matches = matches[:10]

    if matches:
        lines = ["I found these projects that might match:\n"]
        for i, m in enumerate(matches):
            lines.append(f"**{m['name']}** ({m['type']})")
            lines.append(f"`{m['path']}`\n")
        content = "\n".join(lines) + "\nClick a suggestion below to add one, or describe more specifically what you're looking for."
        suggestions = [m["name"] + " \u2014 Add" for m in matches[:5]]
    else:
        content = "I couldn't find any projects matching that description. Try different keywords, or use **Browse** to pick a folder manually."
        suggestions = ["Browse for folder"]

    return jsonify({
        "content": content,
        "suggestions": suggestions,
        "matches": matches,
    })


@bp.route("/api/new-session", methods=["POST"])
def api_new_session():
    """Launch a Claude SDK session. If resume_id is provided, resume that session."""
    try:
        data = request.get_json() or {}
        name = (data.get("name") or "").strip()
        resume_id = (data.get("resume_id") or "").strip()
        prompt = (data.get("prompt") or "").strip()
        active_project = get_active_project()

        # Find the actual project directory
        proj_dir = str(Path.home())
        if active_project:
            decoded = _decode_project(active_project)
            if Path(decoded).is_dir():
                proj_dir = decoded
            else:
                for scan_root in default_project_roots():
                    found = False
                    for p in scan_root.iterdir():
                        if not p.is_dir():
                            continue
                        enc = _encode_cwd(str(p))
                        if enc == active_project:
                            proj_dir = str(p)
                            found = True
                            break
                        for sub in p.iterdir():
                            if not sub.is_dir():
                                continue
                            enc2 = _encode_cwd(str(sub))
                            if enc2 == active_project:
                                proj_dir = str(sub)
                                found = True
                                break
                        if found:
                            break
                    if found:
                        break

        if not Path(proj_dir).is_dir():
            return jsonify({"error": f"Project directory not found: {proj_dir}"}), 400

        sm = current_app.session_manager

        if resume_id:
            # Resume existing session via SDK
            result = sm.start_session(
                session_id=resume_id,
                prompt=prompt,
                cwd=proj_dir,
                name=name,
                resume=True,
            )
            if result.get("ok"):
                return jsonify({"ok": True, "new_id": resume_id})
            return jsonify({"error": result.get("error", "Failed to resume")}), 500

        # New session: generate a new UUID
        new_id = str(uuid_mod.uuid4())

        # Create the .jsonl so it shows in our session list
        jsonl_path = _sessions_dir(project=active_project) / f"{new_id}.jsonl"
        if not jsonl_path.exists():
            jsonl_path.write_text("", encoding="utf-8")

        # Write the user-provided name if given
        if name:
            _save_name(new_id, name, project=active_project)

        # Start SDK session
        result = sm.start_session(
            session_id=new_id,
            prompt=prompt,
            cwd=proj_dir,
            name=name,
            resume=False,
        )

        if result.get("ok"):
            return jsonify({"ok": True, "new_id": new_id})
        return jsonify({"error": result.get("error", "Failed to start session")}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500
