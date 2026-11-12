"""Project add-ons for the end-of-turn stop (smart termination).

The stop's own checks hold every card to the same shape.  A project adds
checks of its own here without touching them.  An add-on is one Python file
exposing

    NAME: str                                  shown in every send-back it causes
    applies(card, facts) -> bool               does this card / session concern the project?
    problems(card, message, facts) -> list     why the card goes back ([] = it passes)

and optionally

    UNCOUNTED_KEYS: tuple                      card keys the add-on owns, outside the 120 words

``facts`` is a ``engine.handoff_evidence.TurnFacts`` or None (the stop could
not read the session's history); the add-on decides what it can conclude
without it.

WHERE ADD-ONS LIVE.  This repository is public, so a project's add-on lives
in the project, never here.  ``data/stop_plugins.json`` (gitignored, this
machine only) lists the folders to load::

    {"folders": [{"dir": "C:/path/to/project/.vibenode/stop_plugins",
                  "pin": "C:/path/to/project/.vibenode/stop_plugins.pin.json",
                  "when": ["path/to/project", "project name"]}]}

``when`` is optional: words (case-insensitive) of which at least one must
appear in the card's identity (its path, title or big picture) or in a FILE
PATH one of the session's tool calls touched, for the folder's add-ons to be
consulted at all.  Words in commands, file contents and the card's prose do
not count, and neither do paths inside a project's ``.vibenode`` folder: a
word matched anywhere would pull unrelated sessions in.  It scopes
failing closed: an add-on that cannot load holds only its own project's cards,
never another project's sessions.

``pin`` is optional: a JSON file ``{"files": {"<file name>": "<sha256>"}}``
that the project keeps under its own approval process.  When a folder names a
pin, an add-on file whose bytes do not match it, or that it does not list, is
not loaded and every card goes back saying so: an add-on edited outside the
project's approval cannot quietly change what the stop enforces.

FAILING CLOSED.  An add-on that cannot be loaded, whose ``applies`` raises, or
whose ``problems`` raises sends the card back with the reason.  The stop's
send-back ceiling still bounds how often.

Files are read at every call, so an edited add-on takes effect at the next
stop, like the rules themselves (``src/engine/smart_termination_live.py``).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "data" / "stop_plugins.json"


def _folders(config) -> list:
    config = Path(config) if config is not None else CONFIG
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as e:
        return [{"error": f"{config.name} is unreadable ({type(e).__name__}: {e})"}]
    out = []
    for f in data.get("folders") or []:
        if isinstance(f, str):
            f = {"dir": f}
        if isinstance(f, dict) and f.get("dir"):
            out.append(f)
    return out


def _load(path: Path):
    name = "vn_stop_plugin_" + hashlib.sha1(str(path).encode()).hexdigest()[:10] + "_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _pinned(pin_path: Path) -> dict:
    try:
        return dict(json.loads(pin_path.read_text(encoding="utf-8")).get("files") or {})
    except Exception as e:
        raise RuntimeError(f"its pin file {pin_path} is unreadable ({type(e).__name__})") from e


_FILE_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep"}
_PATHISH = re.compile(r"[^\s\"'`<>|;,()]*[/\\][^\s\"'`<>|;,()]*")


def _identity(card) -> list:
    """What the card says it is about: its path, title and big picture.  Not its
    headline, findings or prose: a card may MENTION another project (an "Also
    found" about it) without being that project's work."""
    if not isinstance(card, dict):
        return []
    out = [str(s) for s in card.get("path") or [] if isinstance(s, str)]
    if isinstance(card.get("title"), str):
        out.append(card["title"])
    zoom = card.get("zoom") if isinstance(card.get("zoom"), dict) else {}
    if isinstance(zoom.get("big"), str):
        out.append(zoom["big"])
    return out


def _haystack(card, facts) -> str:
    """Where a session's own work shows: the card's identity and the FILE PATHS its
    tool calls touched.  Words inside a command, a heredoc or a file's content do
    not count: a sample sentence that happens to contain a project's word would
    otherwise put an unrelated session under that project's checks.  Paths inside
    a project's own ``.vibenode`` folder do not count either: reading an add-on
    is not working in its project."""
    parts = _identity(card)
    for a in (getattr(facts, "actions", None) or []) if facts is not None else []:
        # Only the file tools name a file the session worked on.  A shell command's
        # text can hold any path (a demo script listing another project's file did).
        if (getattr(a, "tool", "") or "") not in _FILE_TOOLS:
            continue
        for p in _PATHISH.findall(getattr(a, "detail", "") or ""):
            p = p.replace("\\", "/")
            if "/.vibenode/" in p + "/" or p.endswith("stop_plugins.json"):
                continue
            parts.append(p)
    return "\n".join(parts).lower().replace("\\", "/")


def _norm_dir(p: str) -> str:
    return str(p).replace("\\", "/").rstrip("/").lower()


def _project_folders(entry: dict) -> list:
    """The folders whose sessions this add-on folder may check: ``projects`` when
    the entry lists them, else the folder that holds its ``.vibenode``."""
    listed = [p for p in entry.get("projects") or [] if isinstance(p, str) and p.strip()]
    if listed:
        return [_norm_dir(p) for p in listed]
    d = Path(entry["dir"])
    for parent in [d] + list(d.parents):
        if parent.name == ".vibenode":
            return [_norm_dir(parent.parent)]
    return []


def _inside(cwd: str, folders: list) -> bool:
    c = _norm_dir(cwd)
    return any(c == f or c.startswith(f + "/") for f in folders)


def _in_scope(entry: dict, hay, facts=None) -> bool:
    """FIRST the folder: a session running outside every folder of the project is
    never checked, whatever its card or commands say.  The folder is the one
    recorded under the session's unique id (the engine's record, the history
    file Claude keeps under that id, or the folder the CLI reports for that id);
    a session whose folder cannot be tied to its id is NOT checked.  Then, inside the project, the
    ``when`` words narrow it."""
    if hay is not None:
        folders = _project_folders(entry)
        if folders:
            cwd = getattr(facts, "cwd", "") if facts is not None else ""
            if not cwd or not _inside(cwd, folders):
                return False
    words = [str(w).lower().replace("\\", "/") for w in entry.get("when") or []]
    return hay is None or not words or any(w in hay for w in words)


def plugins(config=None, card=None, facts=None, scoped: bool = False) -> list:
    """[(label, module or the reason it was not loaded)] for every configured add-on
    (only the folders whose ``when`` matches the card or session, when ``scoped``)."""
    out = []
    hay = _haystack(card, facts) if scoped else None
    for entry in _folders(config):
        if "error" in entry:
            out.append(("stop_plugins.json", entry["error"]))
            continue
        if not _in_scope(entry, hay, facts):
            continue
        folder = Path(entry["dir"])
        if not folder.is_dir():
            out.append((str(folder), "folder is missing"))
            continue
        try:
            pins = _pinned(Path(entry["pin"])) if entry.get("pin") else None
        except RuntimeError as e:
            out.append((str(folder), str(e)))
            continue
        files = sorted(p for p in folder.glob("*.py") if not p.name.startswith("_"))
        for path in files:
            if pins is not None:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if pins.get(path.name) != digest:
                    out.append((path.stem, "does not match its approved pin (edited without the project's "
                                           "approval, or never approved), so it was not loaded"))
                    continue
            try:
                out.append((path.stem, _load(path)))
            except Exception:
                last = traceback.format_exc(limit=2).strip().splitlines()[-1]
                out.append((path.stem, "could not be loaded: " + last))
        if pins is not None:
            for name in sorted(set(pins) - {p.name for p in files}):
                out.append((Path(name).stem, "is approved but missing from its folder"))
    return out


def uncounted_keys(config=None) -> tuple:
    keys = []
    for _label, mod in plugins(config):
        if not isinstance(mod, str):
            keys.extend(getattr(mod, "UNCOUNTED_KEYS", ()) or ())
    return tuple(keys)


def problems(card, message: str, facts=None, config=None) -> list:
    """Every add-on's reasons to send this card back, each prefixed with the add-on's name."""
    if not isinstance(card, dict):
        return []
    out = []
    for label, mod in plugins(config, card, facts, scoped=True):
        if isinstance(mod, str):
            out.append(f"[{label}] this project's stop add-on {mod}. The card cannot be checked, so it goes back.")
            continue
        name = getattr(mod, "NAME", label)
        try:
            if not mod.applies(card, facts):
                continue
        except Exception as e:
            out.append(f"[{name}] could not tell whether it applies ({type(e).__name__}: {e}); "
                       "it is treated as applying, so the card goes back.")
            continue
        try:
            found = list(mod.problems(card, message, facts) or [])
        except Exception as e:
            found = [f"the add-on failed ({type(e).__name__}: {e}). The card cannot be checked, so it goes back."]
        out.extend(f"[{name}] {p}" for p in found)
    return out
