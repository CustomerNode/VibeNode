"""Send-backs from the "Redo as…" button on the end-of-turn card.

The owner, 2026-10-29: "if we dont like the card type it chose, we somewhere
on the card give a button for that, have a popup to choose the desired card,
send the ai back to re reply, and log from the user previous message to the
response data so we can later go improve that based on real usage data".

The button (static/js/last-card.js, ``_openRedo``) sends the session his
message and posts one record here: his message before the card, the card, the
kind it was, the kind he wanted, his note.  When the redone card lands the
browser posts it too, and it joins the same record.  So each record reads:
your message → the card you got → the kind you wanted → the card you got after.

Stored append-only in data/card_feedback.jsonl (gitignored: it holds his
words), one JSON object per line, ``"event": "sent"`` or ``"event": "redo"``;
``records()`` merges them by id.  Nothing reads it at run time: it is data for
tuning the card rules (daemon/handoff_rules.py) later.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

from common.paths import DATA_DIR

logger = logging.getLogger(__name__)

PATH = DATA_DIR / "card_feedback.jsonl"
MAX_LINE_BYTES = 400_000     # one record; a card with a long deliverable is trimmed to fit
_ID = re.compile(r"^[A-Za-z0-9_.-]{4,80}$")
_TEXT_FIELDS = ("session", "project", "your_message", "card_kind", "wanted", "note", "message", "redo_kind")

_lock = threading.Lock()


def _clip(s, n: int) -> str:
    return s[:n] if isinstance(s, str) else ""


def _fit(rec: dict) -> str:
    """The record as one JSON line, trimming card text that is too long."""
    line = json.dumps(rec, ensure_ascii=False)
    if len(line.encode("utf-8")) <= MAX_LINE_BYTES:
        return line
    for key in ("card", "redo_card"):
        c = rec.get(key)
        if isinstance(c, dict):
            c = dict(c)
            for big in ("deliverable", "full_answer", "explained"):
                if big in c:
                    c[big] = "(trimmed: too long to keep)"
            rec[key] = c
    line = json.dumps(rec, ensure_ascii=False)
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        raise ValueError("record too large")
    return line


def _append(rec: dict) -> None:
    line = _fit(rec)
    with _lock:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def record_sent(body: dict) -> dict:
    """One send-back.  Raises ValueError on a body it cannot keep."""
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    rid = body.get("id")
    if not isinstance(rid, str) or not _ID.match(rid):
        raise ValueError("bad id")
    if not isinstance(body.get("wanted"), str) or not body.get("wanted"):
        raise ValueError("missing wanted")
    rec = {"event": "sent", "id": rid, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    for k in _TEXT_FIELDS:
        if k in body:
            rec[k] = _clip(body.get(k), 20000)
    rec["card"] = body.get("card") if isinstance(body.get("card"), dict) else None
    _append(rec)
    return {"ok": True, "id": rid}


def record_redo(rid: str, body: dict) -> dict:
    """The card the session answered the send-back with."""
    if not isinstance(rid, str) or not _ID.match(rid):
        raise ValueError("bad id")
    if not isinstance(body, dict) or not isinstance(body.get("redo_card"), dict):
        raise ValueError("missing redo_card")
    rec = {"event": "redo", "id": rid, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "redo_card": body["redo_card"], "redo_kind": _clip(body.get("redo_kind"), 40)}
    _append(rec)
    return {"ok": True, "id": rid}


def summary(recs=None) -> dict:
    """What the send-backs say, for a session asked to improve the card
    rules: how often each kind was sent back as which kind, how often the
    redone card was the kind he asked for, and his notes, newest first."""
    recs = records() if recs is None else recs
    pairs, redone, matched = {}, 0, 0
    for r in recs:
        key = f"{r.get('card_kind') or '?'} -> {r.get('wanted') or '?'}"
        pairs[key] = pairs.get(key, 0) + 1
        if r.get("redo_card") is not None:
            redone += 1
            matched += r.get("redo_kind") == r.get("wanted")
    notes = [{"at": r.get("at", ""), "pair": f"{r.get('card_kind')} -> {r.get('wanted')}", "note": r.get("note", ""),
              "your_message": (r.get("your_message") or "")[:200]}
             for r in reversed(recs) if r.get("note")]
    return {"send_backs": len(recs), "by_kind": dict(sorted(pairs.items(), key=lambda kv: -kv[1])),
            "redone": redone, "redone_as_asked": matched, "notes": notes[:30]}


def _main() -> None:
    s = summary()
    print(f"{s['send_backs']} send-backs ({PATH})")
    for k, n in s["by_kind"].items():
        print(f"  {n:4d}  {k}")
    print(f"redone: {s['redone']}, as the kind asked for: {s['redone_as_asked']}")
    for n in s["notes"]:
        print(f"- {n['at']}  {n['pair']}: {n['note']}")
    print("Full records: records() in this module, or GET /api/card-feedback.")


def records() -> list:
    """Every send-back, oldest first, with its redone card when it came."""
    out, by_id = [], {}
    try:
        lines = PATH.read_text("utf-8").splitlines()
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning("card feedback: could not read: %s", e)
        return []
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if not isinstance(r, dict):
            continue
        if r.get("event") == "sent" and r.get("id") not in by_id:
            rec = {k: v for k, v in r.items() if k != "event"}
            rec.setdefault("redo_card", None)
            by_id[r["id"]] = rec
            out.append(rec)
        elif r.get("event") == "redo" and r.get("id") in by_id:
            by_id[r["id"]]["redo_card"] = r.get("redo_card")
            by_id[r["id"]]["redo_kind"] = r.get("redo_kind", "")
            by_id[r["id"]]["redo_at"] = r.get("at", "")
    return out


if __name__ == "__main__":
    _main()
