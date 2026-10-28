"""Smart termination as a Claude Code settings hook (added 2026-10-27).

The engine's own Stop hook (``SessionManager._make_turn_end_callback``) only
runs in a Session Engine started after it was written, and restarting an old
engine ends every running session.  This script runs the same rules from the
CLI's settings instead (``~/.claude/settings.json`` → ``hooks``), so a CLI the
engine starts or wakes picks it up with no restart.

Wired for three events:

* ``Stop``: ``daemon.smart_termination.review`` on the final message; prints
  ``{"decision": "block", "reason": ...}`` to send the session back.
* ``SessionStart``: adds the handoff instructions to the session's context.
* ``UserPromptSubmit``: when the switch changed since the session was last
  told, adds one note saying so (the rules, or that they no longer apply).
  This one runs for engine-run sessions too: the engine bakes the rules into
  the system prompt at launch and has no way to take them back.

Stop and SessionStart's instructions do nothing (exit 0, no output) when:

* the ``smart_termination`` preference is off (read on every call, so the
  switch in System → Preferences takes effect at once);
* the engine handles it itself (``VIBENODE_SMART_TERMINATION_ENGINE=1`` in
  the CLI's environment, set by an engine that registers its own hook);

Every event does nothing when:

* the CLI was not started through the SDK (a terminal ``claude``, or a
  ``claude -p`` one-shot);
* the session is a VibeNode utility session: its folder is ``~/.claude/_system``
  (titles, usage probe) or the registry marks it ``planner``/``title``;
* the user has a message queued for the session (Stop only): it goes out
  at once and the next turn's card covers it;
* anything goes wrong.  This hook must never trap a session.

Install: ``python3 -P daemon/smart_termination_hook.py --install`` adds the
three hooks to ``~/.claude/settings.json`` (``--uninstall`` removes them).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_MARK = "smart_termination_hook.py"


def _registry_type(session_id: str) -> str:
    try:
        reg = json.loads((Path.home() / ".claude" / "gui_active_sessions.json").read_text("utf-8"))
        return ((reg.get("sessions") or {}).get(session_id) or {}).get("session_type", "") or ""
    except Exception:
        return ""


def _engine_runs_it() -> bool:
    return os.environ.get("VIBENODE_SMART_TERMINATION_ENGINE") == "1"


def _ours(data: dict) -> bool:
    """A VibeNode session that talks to a person (engine-run or not)."""
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "") != "sdk-py":
        return False
    cwd = str(data.get("cwd") or "")
    system_dir = str(Path.home() / ".claude" / "_system")
    if cwd == system_dir or cwd.startswith(system_dir + os.sep):
        return False
    if _registry_type(str(data.get("session_id") or "")) in ("planner", "title"):
        return False
    return True


def _pref_on() -> bool:
    sys.path.insert(0, str(_REPO))
    from common.config import get_kanban_config
    return get_kanban_config().get("smart_termination", False) is True


def _applies(data: dict) -> bool:
    """The Stop rules run here: ours, not engine-run, preference on."""
    return not _engine_runs_it() and _ours(data) and _pref_on()


# ── Keeping a running session in step with the switch (2026-10-28) ───────
# The owner: "when you check to enable smart termination on or off, make sure
# that the app updates the way it presents to you without me having to
# refresh".  The rules reach a session once, at launch (the engine's
# append_system_prompt, or SessionStart below), so a session started with the
# switch off never learned the card, and one started with it on kept writing
# cards after it was turned off.  Now every message the user sends passes
# through UserPromptSubmit, which compares what the session was last told
# ("told") with the switch and, only when they differ, adds one note.
# "base" is what an engine-run session's system prompt says (the switch at
# launch); a compaction keeps the system prompt but may drop a later note.

TURNED_ON = ("VibeNode's Smart termination preference was just turned ON. "
             "From your next final message on, follow these rules:\n\n")
TURNED_OFF = ("VibeNode's Smart termination preference was just turned OFF. "
              "The handoff card rules given earlier (in your system prompt or "
              "context) no longer apply: do not end turns with a ```handoff "
              "card or a zoom block; end each turn with a normal, plain reply.")


def _state_path(session_id: str) -> Path:
    return _counter(session_id).with_suffix(".state.json")


def _load_state(session_id: str):
    try:
        rec = json.loads(_state_path(session_id).read_text("utf-8"))
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def _save_state(session_id: str, rec: dict) -> None:
    try:
        p = _state_path(session_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec), "utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


def _transcript_shows_cards(path) -> bool:
    """For a session with no record (started before this existed, or the
    temp folder was cleared): has it been ending turns with cards?  Reads the
    tail only."""
    try:
        with open(str(path), "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            return b"```handoff" in f.read()
    except Exception:
        return False


def _note(on: bool) -> str:
    if not on:
        return TURNED_OFF
    from daemon import smart_termination as st
    return TURNED_ON + st.INSTRUCTIONS


def _context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _session_start(data: dict, on: bool):
    sid = str(data.get("session_id") or "")
    source = str(data.get("source") or "startup")
    rec = _load_state(sid) or {}
    if _engine_runs_it():
        # A launch or resume is a new CLI whose system prompt the engine just
        # built from the switch; a compaction or /clear keeps the old one.
        if source in ("startup", "resume") or "base" not in rec:
            base = on
        else:
            base = bool(rec["base"])
        _save_state(sid, {"base": base, "told": on})
        return None if on == base else _context("SessionStart", _note(on))
    if on:
        from daemon import smart_termination as st
        _save_state(sid, {"base": False, "told": True})
        return _context("SessionStart", st.INSTRUCTIONS)
    was_told = rec.get("told") is True
    _save_state(sid, {"base": False, "told": False})
    return _context("SessionStart", TURNED_OFF) if was_told else None


def _prompt_submit(data: dict, on: bool):
    sid = str(data.get("session_id") or "")
    rec = _load_state(sid)
    if rec is None or not isinstance(rec.get("told"), bool):
        rec = dict(rec or {})
        rec["told"] = _transcript_shows_cards(data.get("transcript_path"))
    if rec["told"] == on:
        _save_state(sid, rec)
        return None
    rec["told"] = on
    _save_state(sid, rec)
    return _context("UserPromptSubmit", _note(on))


def _saw_card(data: dict) -> None:
    """A stop that ends on a card proves the session is following the card
    rules, whatever the switch says now (the next prompt corrects it)."""
    msg = data.get("last_assistant_message") or ""
    if "```handoff" not in msg:
        return
    sid = str(data.get("session_id") or "")
    rec = _load_state(sid) or {}
    if rec.get("told") is not True:
        rec["told"] = True
        _save_state(sid, rec)


def _has_queued(session_id: str) -> bool:
    """True when the user has messages queued for this session.

    The owner (2026-10-27): "if i queued it, i want that to occur and the ai
    to adapt to me".  A card rewrite would only hold the queued message back,
    and the next turn's card has to cover it anyway.  Read-only: the queue
    belongs to the Session Engine (daemon/message_queue.py), which writes this
    file within a second of a change."""
    try:
        q = json.loads((Path.home() / ".claude" / "gui_message_queues.json").read_text("utf-8"))
        return bool(isinstance(q, dict) and q.get(session_id))
    except Exception:
        return False


def _counter(session_id: str) -> Path:
    d = Path(tempfile.gettempdir()) / "vibenode-smart-termination"
    d.mkdir(exist_ok=True)
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:80] or "unknown"
    return d / safe


def handle(data: dict):
    """The JSON to print for one hook call, or None to print nothing."""
    if not _ours(data):
        return None
    event = data.get("hook_event_name", "")
    on = _pref_on()
    if event == "SessionStart":
        return _session_start(data, on)
    if event == "UserPromptSubmit":
        return _prompt_submit(data, on)
    if event != "Stop":
        return None
    _saw_card(data)
    if _engine_runs_it() or not on:
        return None
    from daemon import smart_termination as st
    continued = bool(data.get("stop_hook_active"))
    counter = _counter(str(data.get("session_id") or ""))
    if _has_queued(str(data.get("session_id") or "")):
        return None   # let the queued message go out now

    sendbacks = 0
    if continued:
        try:
            sendbacks = int(counter.read_text().strip() or 0)
        except Exception:
            sendbacks = 0
    reason = st.review(data.get("last_assistant_message"), continued=continued,
                       sendbacks=sendbacks,
                       background_in_flight=bool(data.get("background_tasks")))
    try:
        counter.write_text(str(sendbacks + 1 if reason else sendbacks))
    except Exception:
        pass
    return {"decision": "block", "reason": reason} if reason else None


# ── Install / uninstall into ~/.claude/settings.json ─────────────────────

def _command() -> str:
    return "%s -P %s" % (sys.executable or "python3", Path(__file__).resolve())


def install(settings_path: Path, remove: bool = False) -> None:
    try:
        cfg = json.loads(settings_path.read_text("utf-8"))
    except FileNotFoundError:
        cfg = {}
    hooks = cfg.setdefault("hooks", {})
    for event in ("Stop", "SessionStart", "UserPromptSubmit"):
        groups = [g for g in hooks.get(event, [])
                  if not any(_MARK in (h.get("command") or "") for h in g.get("hooks", []))]
        if not remove:
            groups.append({"hooks": [{"type": "command", "command": _command(), "timeout": 15}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        cfg.pop("hooks", None)
    tmp = settings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", "utf-8")
    os.replace(tmp, settings_path)


def main() -> int:
    if "--install" in sys.argv or "--uninstall" in sys.argv:
        install(Path.home() / ".claude" / "settings.json", remove="--uninstall" in sys.argv)
        return 0
    try:
        out = handle(json.loads(sys.stdin.read() or "{}"))
    except Exception:
        return 0
    if out:
        sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
