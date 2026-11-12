"""Check a draft end-of-turn summary before stopping.

Most sent-back summaries are sent back for length: a session cannot count its
own words while it writes, so it guesses, stops, and is sent back.  This
check lets it count first.

Two commands, one module::

    python src/engine/handoff_check.py --start [type]   # a skeleton to fill in
    python src/engine/handoff_check.py <<'CARD' ... CARD   # the draft, piped in (never a file)

``--start`` builds the quality in before anything is written: a card
skeleton of the right type, every required key present, each value a
placeholder naming its budget, and what the session's own history fixes
already filled in: the path's first step and the big picture from the last
summary, the asks from the user's requests, a choice not yet answered.

The check runs the draft through the same rules the stop uses
(``engine.smart_termination.review``, which calls ``handoff_rules`` and
``handoff_evidence``), with the facts read from the session's own transcript
when it can find it, and prints the word count and every problem at once;
too long, it prints where the words are, so the cut lands first time.  A
draft that passes is stamped (``smart_termination.stamp_card``): the stop
refuses a card without that stamp.  Exit 0 when the stop would let it
through, 1 when it would send it back, 2 when there is nothing to check.

The transcript is found through ``CLAUDE_CODE_SESSION_ID`` (the CLI sets it
for every tool call) or ``--session <id>``.  The check's own tool calls are
left out of the facts, so a card that says "no tools used" is judged on the
work, not on the check.  Without a transcript only the card's own rules run,
and the output says so.  Read-only apart from the stamp.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[2]
_SELF = "handoff_check"


def as_message(draft: str) -> str:
    """The draft as a final message: a bare card JSON is fenced."""
    text = (draft or "").strip()
    if "```handoff" in text:
        return text
    if text.startswith("{"):
        return "```handoff\n" + text + "\n```"
    return text


def _entries(session_id: str, cwd: str) -> list:
    from engine.backends.claude_store import ClaudeJsonlStore
    rows = ClaudeJsonlStore().read_log_entries(session_id, cwd, max_entries=0)
    out, skip = [], set()
    for r in rows:
        if r.get("kind") == "tool_use" and _SELF in (r.get("desc") or ""):
            skip.add(r.get("id"))
            continue
        if r.get("kind") == "tool_result" and r.get("tool_use_id") in skip:
            continue
        out.append(SimpleNamespace(**{"name": "", "desc": "", "id": "", "tool_use_id": "",
                                      "is_error": False, **r}))
    return out


def home_folder(session_id: str) -> str:
    """The folder the session with this unique id was started in, '' when it
    cannot be tied to the id.

    Bound to the id by Claude itself: the session's history file is
    ``~/.claude/projects/<encoded home folder>/<session id>.jsonl``, so the
    answer is the first recorded ``cwd`` whose encoding equals the directory
    name (the engine's own rule, ``_transcript_cwd``).  A ``cwd`` line alone is
    not enough: it follows every ``cd``, and a bad wake can record a wrong
    folder.  No match means no folder, and no folder means no project checks."""
    if not session_id:
        return ""
    try:
        from shared.paths import encode_cwd
    except Exception:
        return ""
    for f in (Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"):
        home = f.parent.name
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 2000:
                        break
                    if '"cwd"' not in line:
                        continue
                    try:
                        cwd = json.loads(line).get("cwd")
                    except ValueError:
                        continue
                    if isinstance(cwd, str) and cwd and encode_cwd(cwd) == home:
                        return cwd
        except OSError:
            continue
    return ""


def facts_for(session_id: str, cwd: str = ""):
    """The stop's facts for this session, or None when its transcript is
    not found."""
    if not session_id:
        return None
    try:
        from engine import handoff_evidence as ev
        entries = _entries(session_id, cwd)
        if not entries:
            return None
        facts = ev.facts_from_entries(entries)
        facts.session_id = session_id
        facts.cwd = home_folder(session_id)
        return facts
    except Exception:
        return None


def check(draft: str, facts=None, session_id: str = "") -> dict:
    """{ok, words, limit, reason, table} for one draft."""
    from engine import handoff_rules as rules
    from engine import smart_termination as st
    message = as_message(draft)
    card, _prose, err = rules.extract(message)
    have_card = isinstance(card, dict) and not err
    words = rules.card_words(card) if have_card else None
    table = rules.word_table(card) if have_card else []
    reason = st.review(message, continued=False, sendbacks=0, facts=facts, self_check=False)
    if reason is None and have_card:
        foreign = getattr(st, "foreign_card_problem", lambda *_a: [])(card, session_id)
        if foreign:
            reason = "Smart termination: your handoff card was sent back.\n\n" + "\n".join(foreign)
    if reason is None and have_card:
        # The stamp the stop looks for: this exact card passed. Written only
        # on a pass, so a failing draft never satisfies the stop.
        st.stamp_card(card, session_id)
    return {"ok": reason is None, "words": words, "limit": rules.MAX_WORDS,
            "reason": reason, "table": table}


def where_the_words_are(table: list, limit: int) -> str:
    """The counted fields, biggest first, and how much to cut."""
    counted = sorted([(w, n) for w, n, c, _cap in table if c and n], key=lambda t: -t[1])
    total = sum(n for _w, n in counted)
    lines = [f"Where the words are (counted toward the {limit}; cut {total - limit}):"]
    lines += [f"  {n:3d}  {w}" for w, n in counted[:12]]
    skipped = [w for w, n, c, _cap in table if not c and n]
    if skipped:
        lines.append("  not counted: " + ", ".join(sorted({w.split("[")[0].split(".")[0] for w in skipped})))
    return "\n".join(lines)


# ── The skeleton ─────────────────────────────────────────────────────────

def _short(text: str, n: int = 10) -> str:
    w = (text or "").split()
    return " ".join(w[:n]) + ("…" if len(w) > n else "")


def suggested_type(facts) -> str:
    """explain when the user's latest message asks something, else done."""
    try:
        from engine import handoff_evidence as ev
        if facts is not None and ev.is_question(getattr(facts, "last_user", "") or ""):
            return "explain"
    except Exception:
        pass
    return "done"


def skeleton(kind: str, facts=None) -> dict:
    """A card of ``kind`` with every required key, placeholders naming each
    budget, and what history fixes filled in."""
    from engine import handoff_rules as rules
    from engine import smart_termination as st
    # A split-view template ("form", "pairs") is a needs_you decision whose
    # answers live in its items (SPLIT_TEMPLATES in handoff_rules.py).
    split = kind if kind in rules.SPLIT_TEMPLATES else ""
    if split:
        kind = "needs_you"
    if kind not in rules.TYPES:
        raise ValueError("type must be one of: " + ", ".join(rules.TYPES + tuple(rules.SPLIT_TEMPLATES)))
    prev_path = (getattr(facts, "prev_path", "") or "") if facts else ""
    prev_big = (getattr(facts, "prev_big", "") or "") if facts else ""
    requests = list(getattr(facts, "requests", []) or []) if facts else []
    last_user = (getattr(facts, "last_user", "") or "") if facts else ""
    open_choice = getattr(facts, "open_choice", None) if facts else None
    L, H, A, Z = rules.MAX_LINE_WORDS, rules.MAX_HEADLINE_WORDS, rules.MAX_ANSWER_WORDS, st.ZOOM_MAX_CHARS

    card = {
        "type": kind,
        "path": [prev_path or "<area the user knows, ≤4 words; keep the first step all session>",
                 "<the thing itself, ≤4 words>"],
        "title": f"<name of the thing: ≤{rules.MAX_TITLE_WORDS} words, ≤{rules.MAX_TITLE_CHARS} chars, "
                 "no verb first, no question word, no : , ; . ? !>",
        "headline": f"<≤{H} words: what happened to it, naming the project and the exact thing>",
        "context": f"<≤{L} words: what we are working on, plain words>",
        "answer": f"<Short: ≤{A} words, one level past the headline, never a repeat of it>",
        "explained": f"<Plain words: markdown, ## headings, ≥{st.EXPLAINED_MIN_WORDS} words and longer "
                     "than Short; every fact of the card, each insider term explained; NOT counted>",
        "full_answer": "<Full: markdown, everything behind it, no limit; NOT counted>",
        "what_was_done": f"<≤{L} words: what you actually did this turn, true to the tools you ran>",
        "checked": f"<≤{L} words: what you looked at to be sure, true to the tools you ran>",
        "asks": ([{"ask": _short(r), "state": "<done | answered | replaced | needs_you | blocked | working>"}
                  for r in requests[-4:]]
                 or [{"ask": "<the user's request, a few of the user's own words>", "state": "done"}]),
        "zoom": {"big": prev_big or f"<≤{Z} chars: what the whole session is for; same every card>",
                 "task": _short(last_user, 6) if last_user and len(_short(last_user, 6)) <= Z
                 else f"<≤{Z} chars: the user's latest ask in the user's words>",
                 "now": f"<≤{Z} chars: what changed for the user this turn>"},
    }
    needs = {"kind": "none", "why": f"<≤{L} words: why nothing waits on the user>",
             "next": f"<≤{L} words: what happens next, never 'once you confirm'>"}
    if kind == "done":
        card["status"] = {"done": ["<≤4 items, ≤%d words each>" % L], "remaining": [], "broken": []}
        card["verified"] = [{"check": "<what was checked>", "result": "<what it showed>", "ok": True}]
        card["links"] = [{"label": "<what to review: the page, commit or files>", "kind": "link|file|commit|image",
                          "url": "</static/... or http://... that loads; a scratch page needs an image link too>"}]
        card["validate"] = {"how": f"<≤{L} words: how the user sees it work himself>",
                            "url": "<a page the user opens NOW: /static/... or http://...; never the app's front page, never a screenshot>",
                            "later": "<only when the real moment is later; else drop this key>"}
    elif kind == "explain":
        pass
    elif kind == "deliverable":
        card["deliverable"] = {"kind": "<email|notes|report|…>", "title": "<its title>",
                               "body": "<the text, markdown; NOT counted>"}
    elif kind == "working":
        needs = {"kind": "none", "why": "<what your own background work is doing>",
                 "next": "<what you do when it reports>", "eta_minutes": "<a number of minutes>",
                 "stages": ["<the job's stages in order, 2 to 8 short names>"], "stage": "<the one running>"}
    elif kind == "blocked":
        needs = {"kind": "none", "why": "<exactly what outside your control blocks you>",
                 "next": "<what happens next>",
                 "tried": [{"what": "<a way you tried>", "result": "<what happened>"},
                           {"what": "<a second way>", "result": "<what happened>"}]}
    elif kind == "needs_you":
        needs = {"kind": "decision",
                 "ask": f"<≤{L} words: the one yes-or-no the user can picture, project and thing named, no code>",
                 "why": f"<≤{L} words: why the recommended choice wins>",
                 "next": f"<≤{L} words: what you do after the user's answer>",
                 "replies": [
                     {"key": "A", "label": "<the choice spelled out>", "reply": "<what the button sends>",
                      "what": "<≥6 words: what it is>", "gain": "<≥6 words: what it gets the user>",
                      "verdict": "<≥6 words: why it beats the others>", "recommended": True},
                     {"key": "B", "label": "<the other choice>", "reply": "<what the button sends>",
                      "what": "<≥6 words>", "gain": "<≥6 words>",
                      "verdict": "<≥6 words: why the recommended one wins over it>"}]}
    if split:
        t = rules.SPLIT_TEMPLATES[split]
        needs = {"kind": "decision",
                 "ask": f"<≤{L} words: what the user answers, project and thing named, no code>",
                 "why": f"<≤{L} words: why these block the work>",
                 "next": f"<≤{L} words: what you do with the answers>",
                 split: [dict(t["example"], id=f"<short id {i + 1}>") for i in range(max(t["min"], 2))]}
    card["needs"] = needs
    if isinstance(open_choice, dict):
        card["_open_choice"] = ("The user has not answered this earlier choice; ask it again with its buttons or "
                                "carry it in back_to, then DELETE this key: "
                                + _short(" ".join(str(open_choice.get(k, "")) for k in ("title", "ask")), 14))
    return card


_PREAMBLE = """\
Fill every <placeholder> with real words and keep every key; delete nothing but "_open_choice".
Do NOT save it to a file: pipe it straight into the check, so there is nothing on disk another session can overwrite:
  {command} <<'CARD'
  {{ ...the card... }}
  CARD
Counted toward the {limit}: headline, context, answer, status, verified, links, validate, needs.
Not counted: explained, full_answer, asks, zoom, what_was_done, checked, deliverable.
Write for the reviewer, in the user's words: no engine, hook, cache, module, config, regex, reload (except "reload the tab");
no option letters ("Header A"); no file paths in the text (files go in links); no "quietly", "honest", "genuinely".
"""


# A card is checked from what is piped in, never from a file: every session
# runs as the same user, so a draft file with one shared name can be
# overwritten by another session.  Piping leaves nothing to collide on and
# needs no session id.
FILE_REFUSED = ("Not checked: pipe the card into the check instead of passing a file. Every session shares "
                "the temp folder, so a draft file can be overwritten by another session between your write "
                "and this check. Run:\n  {command} <<'CARD'\n  {{ ...the card... }}\n  CARD")


def _command() -> str:
    try:
        from engine import smart_termination as st
        return st._CHECK_COMMAND
    except Exception:
        return "python src/engine/handoff_check.py"


def _split_hint(kind: str) -> str:
    """On a decision's blank: the split-view templates, so a session meets
    them at the moment it picks its card."""
    from engine import handoff_rules as rules
    if kind == "needs_you":
        return "".join(f"If {t['when']}, use --start {k} instead.\n" for k, t in rules.SPLIT_TEMPLATES.items())
    if kind in rules.SPLIT_TEMPLATES:
        t = rules.SPLIT_TEMPLATES[kind]
        return (f"needs.{kind}: {t['min']} to {t['max']} items; {t['rules']}. Add or remove items as needed; "
                f"the user's answer arrives as one message starting \"{t['prefix']}\".\n")
    return ""


def start_text(kind: str, facts=None, limit: int = 120) -> str:
    card = skeleton(kind, facts)
    return (_PREAMBLE.format(limit=limit, command=_command()) + _split_hint(kind)
            + json.dumps(card, indent=1, ensure_ascii=False))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if "--session" in argv:
        i = argv.index("--session")
        sid = argv[i + 1] if i + 1 < len(argv) else ""
        del argv[i:i + 2]
    if "--start" in argv:
        i = argv.index("--start")
        kind = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") else ""
        facts = facts_for(sid, os.getcwd())
        kind = kind or suggested_type(facts)
        try:
            print(start_text(kind, facts))
        except ValueError as e:
            print(str(e))
            return 2
        if facts is None:
            print("\nNote: no transcript found, so nothing from this session's history is filled in.")
        return 0
    path = next((a for a in argv if not a.startswith("-")), None)
    if path:
        print(FILE_REFUSED.format(command=_command()))
        return 1
    draft = sys.stdin.read()
    if not draft.strip():
        print("Nothing to check: pipe the card in. --start [type] prints a skeleton to fill in.")
        return 2
    facts = facts_for(sid, os.getcwd())
    res = check(draft, facts, session_id=sid)
    if res["words"] is not None:
        print(f"Words: {res['words']} of {res['limit']}")
    if facts is None:
        print("Note: no transcript found, so only the card's own rules ran "
              "(not the checks against what you did and what the user asked).")
    if res["ok"]:
        print("OK: the stop would let this summary through.")
        return 0
    print(res["reason"])
    if res["words"] is not None and res["words"] > res["limit"]:
        print()
        print(where_the_words_are(res["table"], res["limit"]))
    return 1


if __name__ == "__main__":
    sys.path.insert(0, str(_REPO / "src"))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    # The piped card is UTF-8. On Windows stdin defaults to the console code page,
    # so a card with an ellipsis or a curly quote was read as other characters:
    # the check passed and stamped THAT card, and the stop, reading the real one,
    # found no stamp.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    sys.exit(main())
