"""Project tool guards: refuse a session's tool call before it runs.

The stop add-ons (``src/engine/stop_plugins``) judge a turn when it ends; a tool guard refuses the call itself, for
things that must never happen at all (a session answering its owner's approval page, for one). Every session
VibeNode starts carries one PreToolUse hook (``src/engine/backends/claude.py``) that asks the guards here, whatever the
permission mode: the SDK runs PreToolUse hooks before its own approval logic.

A guard is one Python file exposing

    NAME: str
    decide(tool_name: str, tool_input: dict, cwd: str) -> str | None     a reason to refuse, or None

WHERE GUARDS LIVE.  This repository is public, so a project's guard lives in the project. ``data/tool_guards.json``
(gitignored) lists the folders::

    {"folders": [{"dir": "C:/path/to/project/.vibenode/tool_guards",
                  "pin": "C:/path/to/project/.vibenode/tool_guards.pin.json",
                  "when": ["word", "another"]}]}

``pin`` (optional): ``{"files": {"<file name>": "<sha256>"}}``; a guard whose bytes differ, or that the pin does not
list, is not loaded. ``when`` (optional): words of which one must appear in the tool call for the folder's guards to
be asked. A guard that cannot load or raises REFUSES the calls in its scope (fail closed), with the reason; outside
its scope nothing is refused, so a broken guard never stops another project's sessions.

Files are read at every call: an edited guard takes effect on the next tool call.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "data" / "tool_guards.json"


def _folders(config) -> list:
    config = Path(config) if config is not None else CONFIG
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as e:
        return [{"error": f"{config.name} is unreadable ({type(e).__name__}: {e})"}]
    return [f if isinstance(f, dict) else {"dir": f} for f in data.get("folders") or [] if f]


def _load(path: Path):
    name = "vn_tool_guard_" + hashlib.sha1(str(path).encode()).hexdigest()[:10] + "_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _text(tool_name, tool_input) -> str:
    try:
        return (str(tool_name) + " " + json.dumps(tool_input, ensure_ascii=False)).lower().replace("\\\\", "/")
    except Exception:
        return str(tool_name).lower()


# (mark, what it does, tools that may still name it).  Code that serves the
# button may be read and edited; only calling it, or touching its record, is
# refused.
_READ_ONLY_TOOLS = ("Read", "Grep", "Glob")
_HUMAN_ONLY = (
    ("pace-5h/reset", "Resetting the 5-hour pace limit",
     _READ_ONLY_TOOLS + ("Write", "Edit", "MultiEdit", "NotebookEdit")),
    ("pace_5h.json", "Changing the 5-hour pace limit's record", _READ_ONLY_TOOLS),
)


def decide(tool_name: str, tool_input, cwd: str = "", config=None):
    """The first reason a guard gives to refuse this call, or None to let it run.

    VibeNode's own guard runs first: a write into this public repository that the
    Publish scan would fail (``src/engine/publish_guard.py``; it fails open).
    """
    from engine import publish_guard
    why = publish_guard.decide(tool_name, tool_input, cwd)
    if why:
        return f"[{publish_guard.NAME}] {why}"
    hay = _text(tool_name, tool_input)
    # Buttons only a person may press: a session is refused any tool call
    # that names them, whatever the tool (a request, a script, a file edit).
    for mark, what, allowed in _HUMAN_ONLY:
        if mark in hay and str(tool_name) not in allowed:
            return f"[human only] {what} is a button for a person in VibeNode's window; a session may not press it."
    for entry in _folders(config):
        if "error" in entry:
            return None    # an unreadable config refuses nothing: no folder can be scoped
        words = [str(w).lower() for w in entry.get("when") or []]
        if words and not any(w in hay for w in words):
            continue
        folder = Path(entry["dir"])
        pins = None
        if entry.get("pin"):
            try:
                pins = dict(json.loads(Path(entry["pin"]).read_text(encoding="utf-8")).get("files") or {})
            except Exception as e:
                return f"[tool guard] its pin file is unreadable ({type(e).__name__}); refused until it is fixed."
        files = sorted(p for p in folder.glob("*.py") if not p.name.startswith("_")) if folder.is_dir() else []
        if not files:
            return f"[tool guard] the guard folder {folder} is missing; refused until it is restored."
        for path in files:
            if pins is not None and pins.get(path.name) != hashlib.sha256(path.read_bytes()).hexdigest():
                return f"[tool guard] {path.name} does not match its approved pin; refused until it is approved."
            try:
                mod = _load(path)
                why = mod.decide(tool_name, tool_input if isinstance(tool_input, dict) else {}, cwd or "")
            except Exception:
                last = traceback.format_exc(limit=2).strip().splitlines()[-1]
                return f"[tool guard] {path.stem} failed ({last}); refused until it is fixed."
            if why:
                return f"[{getattr(mod, 'NAME', path.stem)}] {why}"
    return None


def hook_output(reason):
    """The PreToolUse hook answer for a refusal, or {} to let the call run."""
    if not reason:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}
