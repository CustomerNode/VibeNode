"""Capability ledger: human intervention per verified, delivered capability.

A *capability* is a piece of work a session finished and proved: a turn that
ended on a Smart termination ``done`` card with nothing left or broken and at
least one passing check in ``verified``.  For each one the ledger records how
much of the user's attention it took: the messages the user typed into that
session since the previous capability there (button taps included, automated
messages excluded) and the user's active time, measured on ONE timeline across every session so
parallel sessions are never counted twice.

It is rebuilt from the session transcripts on every run, so it needs no hook in
the Session Engine and keeps itself current: run it any time.

    python scripts/capability_ledger.py            # write ledger + report
    python scripts/capability_ledger.py --print    # also print the monthly table

Writes ``data/capability_ledger.jsonl`` (one capability per line) and a report
page ``src/web/static/scratch/capability-ledger.html``.  Both folders are gitignored:
the ledger quotes session transcripts.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import html
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from shared.handoff_status import last_card  # noqa: E402
from shared.transcripts import STOP_HOOK_FEEDBACK_PREFIX, automated_source  # noqa: E402

GAP_S = 15 * 60          # a gap shorter than this between the user's messages is attention
LONE_S = 60              # a message after a longer gap counts one minute
_SKIP_DIRS = ("vibenode-experiments", "-claude--system", "AppData-Local-Temp")
_ACCEPT = ("done", "got it", "sent it")
_MOBILE_END = "⟦/VN-MOBILE⟧"


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _is_tool_result(content) -> bool:
    return isinstance(content, list) and any(
        isinstance(c, dict) and c.get("type") == "tool_result" for c in content)


def _when(ts: str):
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _his_words(text: str) -> str:
    if _MOBILE_END in text:
        text = text.split(_MOBILE_END, 1)[1]
    return text.strip()


def read_session(path: Path):
    """Events of one transcript, in order: ('msg', when, text) for each message
    the user typed, ('card', when, card) for each card that ended a turn."""
    events, pending = [], None
    try:
        lines = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return events
    with lines:
        for line in lines:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("isSidechain"):
                continue
            kind = e.get("type")
            msg = e.get("message") or {}
            when = _when(e.get("timestamp") or "")
            if when is None:
                continue
            if kind == "assistant":
                card = last_card(_text(msg.get("content")))
                if card:
                    pending = (when, card)
                continue
            if kind != "user" or _is_tool_result(msg.get("content")):
                continue
            text = _text(msg.get("content"))
            if text.lstrip().startswith(STOP_HOOK_FEEDBACK_PREFIX):
                pending = None          # the stop refused that card
                continue
            if pending:
                events.append(("card",) + pending)
                pending = None
            words = _his_words(text)
            if (not words or automated_source(words, bool(e.get("isMeta")))
                    or words.startswith(("<command", "<local-command", "Caveat:",
                                         "[Request interrupted", "<system-reminder"))):
                continue
            events.append(("msg", when, words))
    if pending:
        events.append(("card",) + pending)
    return events


def is_capability(card: dict) -> bool:
    if str(card.get("type") or "").lower() != "done":
        return False
    status = card.get("status") if isinstance(card.get("status"), dict) else {}
    if status.get("remaining") or status.get("broken"):
        return False
    checks = card.get("verified") if isinstance(card.get("verified"), list) else []
    if any(isinstance(c, dict) and c.get("ok") is False for c in checks):
        return False
    return any(isinstance(c, dict) and c.get("ok") is True for c in checks)


def _holds(proj: Path) -> dict:
    """The user's Done marks for a project's sessions (the sidebar's verdict,
    ``src/web/session_holds.py``), leaving out the one-time backfill."""
    try:
        data = json.loads((proj / "_session_holds.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {sid: h.get("set") or 0 for sid, h in data.items()
            if isinstance(h, dict) and h.get("state") == "done" and not h.get("backfill")}


def build(projects_dir: Path):
    sessions, done_marks = {}, {}
    for proj in sorted(p for p in projects_dir.iterdir() if p.is_dir()):
        if any(s in proj.name for s in _SKIP_DIRS):
            continue
        for sid, at in _holds(proj).items():
            done_marks[(proj.name, sid)] = at
        for f in proj.glob("*.jsonl"):
            ev = read_session(f)
            if ev:
                sessions[(proj.name, f.stem)] = ev

    # One timeline of the user's messages across all sessions: each message
    # carries the attention since the previous message anywhere (or LONE_S).
    msgs = sorted(((ev[1], key, i) for key, evs in sessions.items()
                   for i, ev in enumerate(evs) if ev[0] == "msg"), key=lambda m: m[0])
    attention, prev = {}, None
    for when, key, i in msgs:
        gap = (when - prev).total_seconds() if prev else None
        attention[(key, i)] = gap if gap is not None and 0 <= gap < GAP_S else LONE_S
        prev = when

    ledger = []
    for key, evs in sessions.items():
        project, sid = key
        count = seconds = 0
        for i, ev in enumerate(evs):
            if ev[0] == "msg":
                count += 1
                seconds += attention.get((key, i), LONE_S)
                continue
            card = ev[2]
            if not is_capability(card):
                continue
            nxt = next((x[2] for x in evs[i + 1:] if x[0] == "msg"), "")
            checks = [c for c in card.get("verified") or [] if isinstance(c, dict)]
            ledger.append({
                "when": ev[1].isoformat(),
                "project": project.replace("C--Users-", "").split("-Documents-")[-1],
                "session": sid,
                "capability": card.get("title") or card.get("headline") or "",
                "headline": card.get("headline") or "",
                "checks": [str(c.get("check") or "") for c in checks if c.get("ok") is True],
                "interventions": count,
                "attention_min": round(seconds / 60, 1),
                "accepted": (nxt.lower().strip(" .!").startswith(_ACCEPT)
                             or done_marks.get(key, 0) >= ev[1].timestamp()),
            })
            count = seconds = 0
    ledger.sort(key=lambda r: r["when"])
    return ledger


def monthly(ledger):
    out = collections.OrderedDict()
    for r in ledger:
        m = r["when"][:7]
        row = out.setdefault(m, {"capabilities": 0, "interventions": 0, "attention_min": 0.0})
        row["capabilities"] += 1
        row["interventions"] += r["interventions"]
        row["attention_min"] += r["attention_min"]
    for row in out.values():
        n = row["capabilities"]
        row["interventions_per"] = round(row["interventions"] / n, 1)
        row["minutes_per"] = round(row["attention_min"] / n, 1)
    return out


def weekly(ledger):
    out = collections.OrderedDict()
    for r in ledger:
        d = dt.date.fromisoformat(r["when"][:10])
        wk = (d - dt.timedelta(days=d.weekday())).isoformat()
        row = out.setdefault(wk, [0, 0, 0.0])
        row[0] += 1
        row[1] += r["interventions"]
        row[2] += r["attention_min"]
    return out


def report_html(ledger) -> str:
    e = html.escape
    weeks = weekly(ledger)
    rows_w = "".join(
        f"<tr><td>{e(w)}</td><td>{n}</td><td>{i / n:.1f}</td><td>{m / n:.0f}</td></tr>"
        for w, (n, i, m) in weeks.items())
    rows_c = "".join(
        f"<tr><td>{e(r['when'][:10])}</td><td>{e(r['project'])}</td>"
        f"<td>{e(r['capability'])}<div class=s>{e(r['headline'])}</div></td>"
        f"<td>{r['interventions']}</td><td>{r['attention_min']:.0f}</td>"
        f"<td>{'yes' if r['accepted'] else ''}</td></tr>"
        for r in reversed(ledger))
    n = len(ledger)
    tot_i = sum(r["interventions"] for r in ledger)
    tot_m = sum(r["attention_min"] for r in ledger)
    head = (f"{n} verified capabilities · {tot_i / n:.1f} messages and "
            f"{tot_m / n:.0f} minutes of your attention each") if n else "No capabilities yet"
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Capability ledger</title>
<style>
:root{{color-scheme:light dark;--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e5e5ea}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1c1e;--fg:#f2f2f7;--mut:#98989d;--line:#38383a}}}}
html[data-theme=dark]{{--bg:#1c1c1e;--fg:#f2f2f7;--mut:#98989d;--line:#38383a}}
html[data-theme=light]{{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e5e5ea}}
body{{font:15px/1.45 -apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:20px;max-width:900px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:26px 0 8px}}
.lead{{color:var(--mut);margin:0 0 6px}}
table{{border-collapse:collapse;width:100%}} td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-size:12px;color:var(--mut);font-weight:600}} td{{white-space:nowrap}}
.s{{color:var(--mut);font-size:13px;white-space:normal}}
#caps td:nth-child(3){{white-space:normal}}
</style>
<script>(function(){{var t=new URLSearchParams(location.search).get('theme');if(t)document.documentElement.dataset.theme=t;}})();</script>
</head><body>
<h1>Capability ledger</h1>
<p class=lead>{e(head)}</p>
<p class=lead>A capability is work a session finished with a passing check and nothing left open. Messages are the ones you typed in that session since its previous capability; minutes are your active time on one timeline across all sessions.</p>
<h2>By week</h2>
<table><tr><th>Week of</th><th>Capabilities</th><th>Messages each</th><th>Minutes each</th></tr>{rows_w}</table>
<h2>Every capability, newest first</h2>
<table id=caps><tr><th>Date</th><th>Project</th><th>Capability</th><th>Messages</th><th>Minutes</th><th>You said Done</th></tr>{rows_c}</table>
</body></html>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--projects", default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--out", default=str(REPO / "data" / "capability_ledger.jsonl"))
    ap.add_argument("--html", default=str(REPO / "src" / "web" / "static" / "scratch" / "capability-ledger.html"))
    ap.add_argument("--print", action="store_true", help="print the monthly table")
    a = ap.parse_args(argv)
    ledger = build(Path(a.projects))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ledger), encoding="utf-8")
    os.replace(tmp, out)
    if a.html:
        page = Path(a.html)
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(report_html(ledger), encoding="utf-8")
    if a.print:
        for m, row in monthly(ledger).items():
            print(f"{m}  {row['capabilities']:4d} capabilities  "
                  f"{row['interventions_per']:5.1f} messages each  {row['minutes_per']:5.1f} min each")
    print(f"{len(ledger)} capabilities -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
