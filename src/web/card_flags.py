"""The red flag on the end-of-turn summary card.

The person flags a summary as bad: one or more reasons from ``REASONS`` plus
an optional note.  The browser (``_openFlag`` in src/web/static/js/last-card.js)
posts it to ``POST /api/card-flag``; this module checks it, appends it to
data/card_flags.jsonl (gitignored: it holds the person's words) and builds the
prompt for a new session in the VibeNode project.  That session reads the
flagged session's transcript and patches the Smart termination rules so the
same fault is caught next time.

The reasons are fixed keys, so the prompt never carries a label a page made
up; ``REASONS`` and ``FLAG_REASONS`` in last-card.js are held equal by
tests/test_card_flag.py.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from shared.paths import DATA_DIR

PATH = DATA_DIR / "card_flags.jsonl"
MAX_LINE_BYTES = 400_000
_SID = re.compile(r"^[A-Za-z0-9_.-]{4,80}$")
_PROJECT = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")

# key -> what the person sees, and what it means for the rules.
REASONS = {
    "technical": "Unnecessary technical details",
    "focus": "Lost focus on my task",
    "long": "Too long or wordy",
    "unanswered": "Didn't answer my question",
    "kind": "Wrong kind of card",
    "unclear": "Hard to follow",
    "chore": "Left me a job it could do",
    "unbacked": "Claims not backed up",
}

_lock = threading.Lock()


def _clip(s, n: int) -> str:
    return s[:n] if isinstance(s, str) else ""


def clean(body) -> dict:
    """The flag as it will be kept.  Raises ValueError on a body it cannot use."""
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    sid = body.get("session")
    if not isinstance(sid, str) or not _SID.match(sid):
        raise ValueError("bad session")
    project = body.get("project") or ""
    if not isinstance(project, str) or (project and not _PROJECT.match(project)):
        raise ValueError("bad project")
    raw = body.get("reasons") or []
    if not isinstance(raw, list):
        raise ValueError("reasons must be a list")
    reasons = []
    for k in raw:
        if k not in REASONS:
            raise ValueError(f"unknown reason: {k!r}")
        if k not in reasons:
            reasons.append(k)
    note = _clip(body.get("note"), 4000).strip()
    if not reasons and not note:
        raise ValueError("pick a reason or write a note")
    card = body.get("card") if isinstance(body.get("card"), dict) else None
    return {"session": sid, "project": project, "reasons": reasons, "note": note,
            "card": card, "your_message": _clip(body.get("your_message"), 20000)}


def transcript_path(sid: str, project: str, projects_dir: Path):
    """Where the flagged session's transcript is, or None."""
    if project:
        p = projects_dir / project / f"{sid}.jsonl"
        if p.is_file():
            return p
    try:
        for p in projects_dir.glob(f"*/{sid}.jsonl"):
            return p
    except OSError:
        pass
    return None


def record(flag: dict, fixer_id: str = "") -> str:
    """Append the flag; return its id."""
    fid = "fl-" + time.strftime("%Y%m%d%H%M%S") + "-" + flag["session"][:8]
    rec = dict(flag, id=fid, at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), fixer=fixer_id)
    line = json.dumps(rec, ensure_ascii=False)
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        rec["card"] = None
        line = json.dumps(rec, ensure_ascii=False)
    with _lock:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return fid


def records() -> list:
    try:
        lines = PATH.read_text("utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def title(flag: dict) -> str:
    """The fixing session's name in the sidebar."""
    first = REASONS[flag["reasons"][0]] if flag["reasons"] else "Flagged summary"
    return ("Fix summary: " + first)[:60]


def prompt(flag: dict, transcript) -> str:
    """What the fixing session is told."""
    reasons = "\n".join(f"- {REASONS[k]}" for k in flag["reasons"]) or "- (none ticked; see the note)"
    note = flag["note"] or "(no note)"
    card = json.dumps(flag["card"], ensure_ascii=False, indent=1) if flag["card"] else "(not sent)"
    where = str(transcript) if transcript else f"(not found; session id {flag['session']}, project {flag['project'] or 'unknown'})"
    return (
        "The owner flagged an end-of-turn summary card (Smart termination) as bad. "
        "Find why the rules let it through and patch them so the same fault is sent back next time.\n\n"
        f"Flagged session: {flag['session']}\n"
        f"Its transcript: {where}\n\n"
        f"What was wrong, in his picks:\n{reasons}\n\n"
        f"His note: {note}\n\n"
        f"The card he flagged:\n```json\n{card}\n```\n\n"
        "How to work:\n"
        "1. Read the transcript around that card: his request before it, what the session did, any send-backs. "
        "Say in one or two sentences what the card got wrong from his seat.\n"
        "2. Patch the rules where the fault belongs: the guidance sessions get (INSTRUCTIONS in "
        "src/engine/smart_termination.py and src/engine/handoff_rules.py) and, where a check can catch it "
        "without false alarms, a check in src/engine/handoff_rules.py or src/engine/handoff_evidence.py. "
        "Fix the class of fault, not this one card. Replay a new check on the local cards in "
        "~/.claude/projects (the summaries in assistant messages) and report how many it flags and whether they are real.\n"
        "3. Add tests in your own made-up words. His messages, this card and the transcript never go into a tracked "
        "file: this is a public repository.\n"
        "4. Run the tests you touched and the Smart termination tests.\n"
        "Do not commit or push unless he asks."
    )
