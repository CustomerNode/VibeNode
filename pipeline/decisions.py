"""Decisions: who allowed a verification command to run, or a lower grade to
ship, and the proof.

Phase 1 of Code Pipeline authorization (docs/plans/code-pipeline-phase1-
authorization-design.md, revision 4).  Nothing here touches VibeNode's
permission code; the only thing read from VibeNode is the user's chosen
policy file.

A decision is one JSON record under ``<run dir>/decisions/<nonce>.json``,
written only by the pipeline programs (the harness treats any recorded tool
call that writes there the way it treats a forged evidence file).  It binds
one exact operation (``operation_sha`` over the expanded command, the
working directory, the repository, the plan and the source fingerprint) and
is used once.

* Under the ``auto`` policy the decision is ``allow`` at once and nothing is
  shown to the user: trusted work runs unattended, as it does today.
* Under any other policy the decision is ``pending`` and carries a handoff
  card (``pipeline.cards``).  The agent ends its turn with that card; the
  user's tap sends the card's reply text as his own message.  The decision
  is answered when the run's root transcript holds that exact text as a user
  message, after the card was issued, not posted by a script, and after the
  card itself was shown unchanged.  An agent cannot write a user message into
  the transcript, and a card it edits no longer matches, so the answer is the
  user's.

A decision never waits: the agent ends its turn and the next turn checks.
That is what keeps a long suite and a slow answer both inside the shell
tool's limits.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from pipeline.evidence import load_run
from pipeline.transcripts import assistant_texts, find_root_transcript, user_texts

DECISIONS_DIR = "decisions"
DECISION_PREFIX = "PIPELINE-DECISION"
KINDS = ("check", "delivery")
# VibeNode's permission policy (daemon/permission_manager.py writes it).  Read
# only; "auto" is the one policy that approves without a person.  Unreadable
# or missing reads as "manual": fail closed.
POLICY_PATH = Path.home() / ".claude" / "gui_permission_policy.json"
_HANDOFF_FENCE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,})[ \t]*handoff[^\n]*\n(.*?)^[ \t]*\1[`~]*[ \t]*$")
# What the app adds around a message the user sends from the phone, and what
# a script's post carries (app/routes/live_api.py wrap_hook_message).
_MOBILE_RE = re.compile(r"⟦VN-MOBILE⟧.*?⟦/VN-MOBILE⟧", re.S)
_SENT_FROM_RE = re.compile(r"\n?\s*Sent from \S+ at [^\n]*$")
SCRIPT_MARK = "⟦VN-EVENT"
# Fields the agent MAY add to an issued card without voiding it: the user's
# own requests (the card rules require them, in his words), screenshots and
# the "Left waiting" strip.  None of them carries the decision's words.
AGENT_MAY_ADD = ("asks", "links", "back_to", "zoom", "explained", "context")


def card_core(card: dict) -> dict:
    """The part of a card that must be shown unchanged: everything but the
    fields in ``AGENT_MAY_ADD``."""
    return {k: v for k, v in (card or {}).items() if k not in AGENT_MAY_ADD}


class DecisionError(ValueError):
    pass


def policy_name(path: Path | None = None) -> str:
    p = path or POLICY_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        name = str(data.get("policy") or "").strip().lower()
        return name or "manual"
    except (OSError, ValueError, AttributeError):
        return "manual"


def operation_sha(operation: dict) -> str:
    return hashlib.sha256(json.dumps(operation, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def card_sha(card: dict) -> str:
    return hashlib.sha256(json.dumps(card, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def decisions_dir(run_dir: Path) -> Path:
    return run_dir / DECISIONS_DIR


def decision_path(run_dir: Path, nonce: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{8}", nonce or ""):
        raise DecisionError(f"{nonce!r} is not a decision id")
    return decisions_dir(run_dir) / f"{nonce}.json"


def decision_line(rec: dict) -> str:
    """The one line ``authorize`` and ``deliver`` print; the harness requires
    it in the run's transcript for every decision it honours."""
    return (f"{DECISION_PREFIX} id={rec['nonce']} kind={rec['kind']} verdict={rec['verdict']} "
            f"sha={rec['operation_sha']}")


def issue(run_dir: Path, kind: str, operation: dict, *, build=None, policy: str | None = None) -> dict:
    """Create a decision for *operation*.  Allowed at once under the ``auto``
    policy (a check only; a lower grade always needs the person); pending
    otherwise, with the card ``build(nonce)`` returns as ``(card, replies)``."""
    if kind not in KINDS:
        raise DecisionError(f"kind must be one of {', '.join(KINDS)}")
    run_dir = run_dir.resolve()
    run = load_run(run_dir)
    pol = policy if policy is not None else policy_name()
    auto = pol == "auto" and kind == "check"
    if not auto and build is None:
        raise DecisionError("a decision that needs the user carries a card and its replies")
    nonce = secrets.token_hex(4)
    card, replies = build(nonce) if not auto else (None, None)
    rec = {
        "nonce": nonce,
        "kind": kind,
        "run_id": run.get("run_id"),
        "operation": operation,
        "operation_sha": operation_sha(operation),
        "policy": pol,
        "verdict": "allow" if auto else "pending",
        "how": f"policy:{pol}" if auto else "card",
        "issued_at": _now(),
        "used": False,
    }
    if not auto:
        rec["card"] = card
        rec["card_sha"] = card_sha(card)
        rec["replies"] = {"allow": replies["allow"], "deny": replies["deny"]}
    d = decisions_dir(run_dir)
    d.mkdir(parents=True, exist_ok=True)
    _write(decision_path(run_dir, nonce), rec)
    return rec


def load(run_dir: Path, nonce: str) -> dict:
    p = decision_path(run_dir.resolve(), nonce)
    if not p.is_file():
        raise DecisionError(f"no decision {nonce} in this run")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        raise DecisionError(f"decision {nonce} is not valid JSON") from None


def update(run_dir: Path, nonce: str, **fields) -> dict:
    rec = load(run_dir, nonce)
    rec.update(fields)
    _write(decision_path(run_dir.resolve(), nonce), rec)
    return rec


def clean_text(text: str) -> str:
    """The user's own words: no phone preamble, no "Sent from" line."""
    t = _MOBILE_RE.sub("", text or "")
    t = _SENT_FROM_RE.sub("", t)
    return " ".join(t.split())


def answer(run_dir: Path, rec: dict, *, run: dict | None = None) -> tuple[str | None, str]:
    """``("allow" | "deny" | None, detail)``: the user's answer to a pending
    decision, read from the run's root transcript.

    The answer counts only when, in order: the card was shown (an assistant
    message after the decision was issued carries a ```handoff block equal
    to the issued card), and then a user MESSAGE (not a tool result, not a
    script's ⟦VN-EVENT⟧ post) equals one of the card's replies.  An agent
    cannot write either of those entries as the user.
    """
    run_dir = run_dir.resolve()
    run = run or load_run(run_dir)
    if rec.get("verdict") == "allow":
        return "allow", rec.get("how", "")
    if rec.get("verdict") == "deny":
        return "deny", rec.get("how", "")
    nonce = str(run.get("nonce") or "")
    root = find_root_transcript(str(run.get("project_cwd") or run.get("repo") or run_dir),
                                f"PIPELINE-RUN nonce={nonce}") if nonce else None
    if root is None:
        return None, "the run's session transcript was not found"
    issued = _ts(rec.get("issued_at"))
    if issued is None:
        return None, "the decision has no issue time"
    shown_at = None
    want = card_core(rec.get("card") or {})
    for ts, text in assistant_texts(root):
        t = _ts(ts)
        if t is None or t < issued:
            continue
        for m in _HANDOFF_FENCE.finditer(text):
            try:
                got = json.loads(m.group(2))
            except ValueError:
                continue
            if isinstance(got, dict) and card_core(got) == want:
                shown_at = t
                break
        if shown_at:
            break
    if shown_at is None:
        return None, "the card was not shown to the user unchanged after the decision was issued"
    replies = rec.get("replies") or {}
    for ts, text in user_texts(root):
        t = _ts(ts)
        if t is None or t < shown_at or SCRIPT_MARK in text:
            continue
        cleaned = clean_text(text)
        if cleaned == clean_text(replies.get("allow", "")):
            return "allow", f"the user answered at {ts}"
        if cleaned == clean_text(replies.get("deny", "")):
            return "deny", f"the user answered at {ts}"
    return None, "the user has not answered yet"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _ts(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _write(path: Path, rec: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
