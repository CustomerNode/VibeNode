"""The summary self-check (src/engine/handoff_check.py).

A session runs its draft through the stop's own rules before stopping, so a
summary is fixed before the user waits on a send-back.
"""
import json
import subprocess
import sys
from pathlib import Path

from engine import handoff_check as hc
from engine import handoff_rules as rules
from engine import smart_termination as st

REPO = Path(__file__).resolve().parents[1]

GOOD = {
    "type": "explain",
    "path": ["Restarts"],
    "title": "Second window",
    "headline": "A restart of VibeNode's sessions no longer opens a second window.",
    "context": "Restarting the sessions opened a second VibeNode window.",
    "answer": "The relaunch after a restart now opens no window, and your open tab reconnects by itself.",
    "explained": ("## What happened\nRestarting the part of VibeNode that runs your sessions used to "
                  "open a second window next to the one you had. Now every kind of restart leaves "
                  "your open tab as the only window, and the tab finds the server again by itself, "
                  "the same way it already did after a restart of the page."),
    "full_answer": "## Detail\nEvery restart now asks the relaunch not to open a window.",
    "what_was_done": "Answered from what this session already found",
    "checked": "Nothing new checked this turn",
    "zoom": {"big": "Second window on restart", "task": "Why a second window", "now": "Answered"},
    "needs": {"kind": "none", "why": "This answers the question.", "next": "Nothing follows."},
}


def test_word_count_is_the_length_rules_count():
    assert rules.length_problems(GOOD) == [] and 0 < rules.card_words(GOOD) <= rules.MAX_WORDS
    # The count printed is the count the stop's message uses.
    big = dict(GOOD, **{f"x{i}": " ".join(["w"] * 20) for i in range(6)})
    total = rules.card_words(big)
    msg = rules.length_problems(big)
    assert total > rules.MAX_WORDS
    assert msg and f"the card is {total} words" in msg[0]


def test_bare_json_is_checked_as_a_card():
    assert hc.as_message(json.dumps(GOOD)).startswith("```handoff\n{")
    fenced = "```handoff\n" + json.dumps(GOOD) + "\n```"
    assert hc.as_message(fenced) == fenced


def test_same_verdict_as_the_stop():
    for card in (GOOD, dict(GOOD, title="Why the window opens"), dict(GOOD, explained="short")):
        draft = json.dumps(card)
        res = hc.check(draft)
        assert res["reason"] == st.review(hc.as_message(draft), continued=False, sendbacks=0)
        assert res["ok"] is (res["reason"] is None)


def test_a_title_question_is_reported():
    res = hc.check(json.dumps(dict(GOOD, title="Why the window opens")))
    assert not res["ok"] and "\"title\"" in res["reason"]


def test_the_checks_own_tool_calls_are_left_out(monkeypatch):
    rows = [
        {"kind": "user", "text": "why does a restart open a second window?"},
        {"kind": "tool_use", "id": "t1", "name": "Bash", "desc": "python daemon/handoff_check.py d.json"},
        {"kind": "tool_result", "tool_use_id": "t1", "text": "OK", "is_error": False},
    ]
    from engine.backends import claude_store
    monkeypatch.setattr(claude_store.ClaudeJsonlStore, "read_log_entries",
                        lambda self, sid, cwd="", max_entries=200, until_ts=0.0: rows)
    facts = hc.facts_for("some-session")
    assert facts is not None
    assert facts.actions == [] and not facts.turn_actions
    assert facts.last_user.startswith("why does a restart")


def test_no_session_means_card_rules_only():
    assert hc.facts_for("") is None


def test_command_line_prints_count_and_exit_code(tmp_path):
    card = json.dumps(dict(GOOD, title="Why the window opens"))
    env = {k: v for k, v in __import__("os").environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    p = subprocess.run([sys.executable, str(REPO / "src" / "engine" / "handoff_check.py")], input=card,
                       capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(tmp_path))
    assert p.returncode == 1, p.stdout + p.stderr
    assert p.stdout.startswith("Words: ")
    assert "no transcript found" in p.stdout
    p = subprocess.run([sys.executable, str(REPO / "src" / "engine" / "handoff_check.py")], input="",
                       capture_output=True, text=True, env=env)
    assert p.returncode == 2


def test_every_session_is_told_to_run_it():
    assert "THEN CHECK IT, BEFORE YOU STOP" in st.INSTRUCTIONS
    assert "handoff_check.py" in st.INSTRUCTIONS
    assert "pythonw" not in st._CHECK_COMMAND.lower()
    assert "\\" not in st._CHECK_COMMAND


def _on(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBENODE_HANDOFF_SELF_CHECK", "1")
    monkeypatch.setattr(st, "stamp_dir", lambda: tmp_path / "checked")


def _msg(card):
    return "```handoff\n" + json.dumps(card) + "\n```"


def test_the_stop_refuses_a_card_the_check_never_passed(monkeypatch, tmp_path):
    _on(monkeypatch, tmp_path)
    reason = st.review(_msg(GOOD), continued=False, sendbacks=0)
    assert reason and "Not checked" in reason and "handoff_check.py" in reason


def test_a_checked_card_goes_through(monkeypatch, tmp_path):
    _on(monkeypatch, tmp_path)
    res = hc.check(json.dumps(GOOD))
    assert res["ok"], res["reason"]
    assert st.review(_msg(GOOD), continued=False, sendbacks=0) is None
    # Spacing and key order in the message do not matter: it is the same card.
    reordered = "```handoff\n" + json.dumps(dict(reversed(list(GOOD.items()))), indent=2) + "\n```"
    assert st.review(reordered, continued=False, sendbacks=0) is None


def test_a_card_changed_after_the_check_is_refused(monkeypatch, tmp_path):
    _on(monkeypatch, tmp_path)
    assert hc.check(json.dumps(GOOD))["ok"]
    edited = dict(GOOD, headline=GOOD["headline"] + " Really.")
    reason = st.review(_msg(edited), continued=False, sendbacks=0)
    assert reason and "Not checked" in reason


def test_a_failing_check_leaves_no_stamp(monkeypatch, tmp_path):
    _on(monkeypatch, tmp_path)
    bad = dict(GOOD, title="Why the window opens")
    assert not hc.check(json.dumps(bad))["ok"]
    assert not st.card_checked(bad)
    assert not list((tmp_path / "checked").glob("*")) if (tmp_path / "checked").exists() else True


def test_unchecked_keeps_going_back_past_the_polite_tries(monkeypatch, tmp_path):
    _on(monkeypatch, tmp_path)
    reason = st.review(_msg(GOOD), continued=True, sendbacks=st.MAX_SENDBACKS)
    assert reason and "Not checked" in reason
    assert st.released_faults(_msg(GOOD), st.HARD_CEILING) and \
        any("Not checked" in f for f in st.released_faults(_msg(GOOD), st.HARD_CEILING))


def test_the_check_itself_does_not_demand_a_stamp(monkeypatch, tmp_path):
    _on(monkeypatch, tmp_path)
    assert st.review(_msg(GOOD), continued=False, sendbacks=0, self_check=False) is None


def test_switch_off_for_the_suite_only(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBENODE_HANDOFF_SELF_CHECK", "0")
    assert st.review(_msg(GOOD), continued=False, sendbacks=0) is None


def test_sessions_are_told_the_stop_refuses_an_unchecked_card():
    assert "THE STOP REFUSES A CARD THE CHECK DID NOT PASS" in st.INSTRUCTIONS


# ── Building the quality in: the skeleton and "where the words are" ──────

class _Facts:
    prev_path = "Restarts"
    prev_big = "Second window on restart"
    requests = ["fix the second window on restart", "build a summary self-check"]
    last_user = "why does it open a second window?"
    open_choice = None


def test_skeleton_has_every_required_key_for_each_type():
    for kind in rules.TYPES:
        sk = hc.skeleton(kind)
        for key in ("type", "path", "title", "headline", "context", "answer", "explained",
                    "full_answer", "what_was_done", "checked", "asks", "zoom", "needs"):
            assert key in sk, (kind, key)
        assert sk["type"] == kind
    assert "links" in hc.skeleton("done") and "validate" in hc.skeleton("done")
    assert hc.skeleton("needs_you")["needs"]["kind"] == "decision"
    assert len(hc.skeleton("needs_you")["needs"]["replies"]) == 2
    assert "tried" in hc.skeleton("blocked")["needs"]
    assert "eta_minutes" in hc.skeleton("working")["needs"]
    assert "deliverable" in hc.skeleton("deliverable")


def test_skeleton_fills_in_what_history_fixes():
    sk = hc.skeleton("explain", _Facts())
    assert sk["path"][0] == "Restarts"
    assert sk["zoom"]["big"] == "Second window on restart"
    assert [a["ask"] for a in sk["asks"]] == ["fix the second window on restart", "build a summary self-check"]
    assert "_open_choice" not in sk


def test_skeleton_carries_an_unanswered_choice():
    f = _Facts()
    f.open_choice = {"title": "Button style", "ask": "Pick the button style for the summary cards"}
    sk = hc.skeleton("done", f)
    assert "Button style" in sk["_open_choice"]


def test_suggested_type_follows_his_latest_message():
    assert hc.suggested_type(_Facts()) == "explain"
    f = _Facts()
    f.last_user = "fix the second window please"
    assert hc.suggested_type(f) == "done"
    assert hc.suggested_type(None) == "done"


def test_placeholders_name_the_budgets():
    text = hc.start_text("done", _Facts())
    assert f"≤{rules.MAX_HEADLINE_WORDS} words" in text
    assert f"≤{rules.MAX_ANSWER_WORDS} words" in text
    assert f"≥{st.EXPLAINED_MIN_WORDS} words" in text
    assert "Counted toward the 120" in text
    json.loads(text[text.index("\n{") + 1:])   # the skeleton itself is valid JSON


def test_where_the_words_are_says_what_to_cut():
    big = dict(GOOD, context=" ".join(["w"] * 25), **{f"x{i}": " ".join(["w"] * 20) for i in range(5)})
    res = hc.check(json.dumps(big))
    assert not res["ok"] and res["words"] > res["limit"]
    out = hc.where_the_words_are(res["table"], res["limit"])
    assert f"cut {res['words'] - res['limit']}" in out
    assert "context" in out and "not counted: " in out and "explained" in out
    # The table and the count agree.
    assert sum(n for _w, n, c, _cap in res["table"] if c) == res["words"]


def test_start_on_the_command_line(tmp_path):
    env = {k: v for k, v in __import__("os").environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    p = subprocess.run([sys.executable, str(REPO / "src" / "engine" / "handoff_check.py"), "--start", "explain"],
                       capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(tmp_path))
    assert p.returncode == 0, p.stdout + p.stderr
    assert '"type": "explain"' in p.stdout and "no transcript found" in p.stdout
    p = subprocess.run([sys.executable, str(REPO / "src" / "engine" / "handoff_check.py"), "--start", "nonsense"],
                       capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(tmp_path))
    assert p.returncode == 2


def test_sessions_are_told_to_start_from_the_skeleton():
    assert "--start" in st.INSTRUCTIONS and "BUILD THE CARD FROM ITS SKELETON" in st.INSTRUCTIONS


def test_writing_the_summary_is_not_tools_used():
    """An answer from knowledge may still say "no tools used" after the
    skeleton, the draft file and the check: the stop and the check agree."""
    from engine import handoff_evidence as ev
    card = dict(GOOD, what_was_done="Answered from training data; no tools used",
                checked="No research beyond base training data")
    facts = ev.TurnFacts(complete=True, turn_actions=[
        ev.Action(tool="Bash", ok=True, detail='"python.exe" "src/engine/handoff_check.py" --start'),
        ev.Action(tool="Write", ok=True, detail="C:/Users/x/AppData/Local/Temp/handoff-card.json (write 900 chars)"),
        ev.Action(tool="Bash", ok=True, detail="cat > /tmp/hc/card.md <<'EOF'\n```handoff\n{...}"),
        ev.Action(tool="Bash", ok=True, detail='"python.exe" "src/engine/handoff_check.py" /tmp/handoff-card.json'),
    ])
    assert ev.research_problems(card, facts) == []
    facts.turn_actions.append(ev.Action(tool="Grep", ok=True, detail="restart"))
    assert ev.research_problems(card, facts), "a real tool call still contradicts the line"


def test_research_about_handoffs_still_counts():
    """A web search whose query mentions handoffs is real research: a card
    that says it searched the web must pass, one that denies it must not."""
    from engine import handoff_evidence as ev
    searched = dict(GOOD, what_was_done="Searched the web for agent handoff tools",
                    checked="Two web searches")
    facts = ev.TurnFacts(complete=True, turn_actions=[
        ev.Action(tool="WebSearch", ok=True, detail="query: AI agent handoff report enforce"),
        ev.Action(tool="PowerShell", ok=True, detail='"python.exe" "src/engine/handoff_check.py" card.json'),
    ])
    assert ev.research_problems(searched, facts) == []
    denied = dict(GOOD, what_was_done="Answered from training data; no web search")
    assert ev.research_problems(denied, facts)


def test_demo_pages_need_a_screenshot_too():
    # The picture rule covers every scratch page; the instructions now say so.
    assert "a demo page for Validate too" in st.INSTRUCTIONS


# --- The card is piped into the check, never read from a file: a shared draft
# file lets sessions overwrite each other's cards.  Nothing on disk, so nothing
# to collide on. ---

def test_a_file_is_refused_and_the_way_to_pipe_is_shown(tmp_path, capsys):
    f = tmp_path / "handoff-card.json"
    f.write_text('{"type": "done"}', encoding="utf-8")
    assert hc.main([str(f)]) == 1
    out = capsys.readouterr().out
    assert "pipe the card" in out and "<<'CARD'" in out


def test_a_piped_card_is_checked(monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO('{"type": "done"}'))
    assert hc.main([]) == 1                       # a bare card fails its rules, but it WAS checked
    assert "Words:" in capsys.readouterr().out


def test_start_tells_the_session_to_pipe_not_save():
    text = hc.start_text("done", None)
    assert "Do NOT save it to a file" in text and "<<'CARD'" in text
    assert "handoff-card.json" not in text
    assert "handoff-card.json" not in st.INSTRUCTIONS and "<<'CARD'" in st.INSTRUCTIONS


def test_the_folder_is_the_one_claude_recorded_under_the_sessions_id(tmp_path, monkeypatch):
    """Bound to the unique id: the history file lives under the encoded home folder, so only
    a cwd line matching that directory counts.  A first line from a wrong folder (a bad wake)
    is skipped; a file with no matching line gives no folder, so no project checks."""
    base = tmp_path / ".claude" / "projects"
    proj = base / "C--work-ProjectX"
    proj.mkdir(parents=True)
    rows = [{"type": "summary"}, {"type": "user", "cwd": r"C:\work\Other"},
            {"type": "user", "cwd": r"C:\work\ProjectX"}, {"type": "user", "cwd": r"C:\work\ProjectX\sub"}]
    (proj / "sess-1.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    (proj / "sess-2.jsonl").write_text(json.dumps({"type": "user", "cwd": r"C:\work\Other"}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert hc.home_folder("sess-1") == r"C:\work\ProjectX"
    assert hc.home_folder("sess-2") == ""
    assert hc.home_folder("missing") == ""
    assert hc.home_folder("") == ""


# The check leaves its own calls out of the evidence, but a test run that
# names a test file after it is the session's real work and must count.
def test_a_test_run_naming_the_checks_test_file_is_still_evidence():
    assert hc._SELF.search('python3 src/engine/handoff_check.py --start')
    assert hc._SELF.search('python -m engine.handoff_check')
    assert not hc._SELF.search('python3 -m pytest tests/test_handoff_check.py tests/test_widgets.py')
    from engine import handoff_evidence as ev
    assert ev._SUMMARY_WORK.search('python3 src/engine/handoff_check.py <<CARD')
    assert not ev._SUMMARY_WORK.search('python3 -m pytest tests/test_handoff_check.py')
