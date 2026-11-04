"""Smart termination keeps a session on the user's task.

The failure shapes guarded here: an automated wake-up (a background job's
notice, a check-in the session scheduled for itself) producing a summary with
a new task; summaries about the session's own machinery (commits, pushes, test
counts, ports); a chore left for the user inside the Full tab; and a reply to
a redirect ("come back focused on the actual work") that only tidies up.  Each
case below reproduces one shape in made-up words.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from engine import handoff_evidence as ev
from engine import handoff_rules as rules
from engine.handoff_evidence import TurnFacts


def _e(kind, text, auto=""):
    return SimpleNamespace(kind=kind, text=text, auto=auto, name="", id="", tool_use_id="",
                           is_error=False, desc="")


def _card_msg(card):
    return "```handoff\n" + json.dumps(card) + "\n```"


PREV = {"type": "done", "title": "Gallery thumbnails", "headline": "Thumbnails now load in order.",
        "zoom": {"big": "Gallery that loads fast", "task": "Thumbnails in order", "now": "Fixed the order"}}


def _facts(entries):
    return ev.facts_from_entries(entries)


# ── 1. An automated wake-up never changes the task ─────────────────────────

def _woken_entries():
    return [
        _e("user", "make the gallery thumbnails load in the right order"),
        _e("asst", _card_msg(PREV)),
        _e("user", "Check the upload watcher log; if my build is green, report done.", auto="Scheduled check-in"),
    ]


def test_a_turn_woken_by_a_check_in_knows_it_was_not_him():
    f = _facts(_woken_entries())
    assert f.woken_by == "Scheduled check-in"
    assert f.prev_title == "Gallery thumbnails" and f.prev_task == "Thumbnails in order"


def test_a_woken_turn_that_renames_the_task_is_sent_back():
    card = {"type": "done", "title": "Build pushed", "headline": "The build is up.",
            "zoom": {"big": "Gallery that loads fast", "task": "Confirm the build", "now": "Checked"}}
    out = ev.drift_problems(card, _facts(_woken_entries()))
    assert any("not the user" in p and "Build pushed" in p for p in out)


def test_a_woken_turn_that_keeps_the_task_passes_that_check():
    card = {"type": "done", "title": "Gallery thumbnails", "headline": "Thumbnails still load in order.",
            "zoom": {"big": "Gallery that loads fast", "task": "Thumbnails in order", "now": "Rechecked"}}
    assert not [p for p in ev.drift_problems(card, _facts(_woken_entries())) if "not the user" in p]


def test_his_own_message_is_not_a_wake_up():
    entries = _woken_entries()[:2] + [_e("user", "now make the captions wrap on phones")]
    assert _facts(entries).woken_by == ""


# ── 2. The lines the user reads first are about the user's ask ───────────

def _his(*msgs):
    return TurnFacts(requests=list(msgs), last_user=msgs[-1], history=list(msgs))


def test_a_short_about_commits_and_test_counts_is_sent_back():
    card = {"type": "done", "title": "Gallery thumbnails", "headline": "Thumbnails load in order.",
            "answer": "Both commits are on the remote and 1,204 tests pass."}
    out = ev.drift_problems(card, _his("make the gallery thumbnails load in the right order"))
    assert any("how you worked" in p for p in out)


def test_the_same_words_pass_when_he_asked_about_them():
    card = {"type": "done", "title": "Gallery fix pushed", "headline": "Your commits are pushed.",
            "answer": "Both commits are pushed to the remote."}
    out = ev.drift_problems(card, _his("commit the gallery fix and push it to the remote"))
    assert not [p for p in out if "how you worked" in p or "housekeeping" in p]


def test_ordinary_words_that_start_like_jargon_pass():
    card = {"type": "explain", "title": "Original task status", "headline": "Your original task is finished.",
            "answer": "The original layout is back, at 560px wide as you asked, and remotely loaded fonts stay."}
    out = ev.drift_problems(card, _his("what was the original layout width again"))
    assert not [p for p in out if "how you worked" in p]


def test_plain_words_with_insider_terms_is_sent_back():
    card = {"type": "done", "title": "Gallery thumbnails", "headline": "Thumbnails load in order.",
            "explained": "I checked it in a spare worktree and the e2e run was clean on port 8031."}
    out = ev.drift_problems(card, _his("make the gallery thumbnails load in the right order"))
    assert any("insider terms" in p for p in out)


def test_a_summary_about_housekeeping_is_sent_back():
    card = {"type": "done", "title": "Gallery thumbnails",
            "headline": "Cleaned up my spare copy and stopped the watcher.",
            "answer": "Tidied up after myself; nothing to retry."}
    out = ev.drift_problems(card, _his("make the gallery thumbnails load in the right order"))
    assert any("housekeeping" in p for p in out)


# ── 3. A redirect is answered whole, against the first task ────────────────

def _redirect_facts():
    first = "make the gallery thumbnails load in the right order"
    last = "stop fiddling and come back focused on the actual work"
    f = TurnFacts(requests=[last], last_user=last, history=[first, last])
    f.first_ask = first
    return f


def test_a_redirect_answered_with_half_of_it_and_no_task_is_sent_back():
    card = {"type": "done", "title": "Spare copy removed", "headline": "Removed my spare copy.",
            "asks": [{"ask": "stop fiddling", "state": "done"}]}
    out = ev.drift_problems(card, _redirect_facts())
    assert any("WHOLE message" in p for p in out)
    assert any("task the user gave this session" in p for p in out)


def test_a_redirect_answered_whole_and_on_the_task_passes():
    card = {"type": "needs_you", "title": "Gallery thumbnails",
            "headline": "Back on the gallery: thumbnails load in order on desktop, phones next.",
            "asks": [{"ask": "stop fiddling and come back focused on the actual work", "state": "working"}]}
    out = ev.drift_problems(card, _redirect_facts())
    assert not [p for p in out if "WHOLE" in p or "task the user gave" in p]


# ── 4. No chore for the user in any tab, and nothing of yours left running

def test_a_chore_hidden_in_the_full_tab_is_sent_back():
    card = {"type": "done", "title": "Gallery thumbnails", "headline": "Thumbnails load in order.",
            "full_answer": "## Notes\nI left a spare copy registered; `git worktree prune` clears it."}
    out = ev.chore_problems(card)
    assert any("job you can do yourself" in p for p in out)
    assert any("left behind" in p for p in out)


def test_a_quoted_phrase_is_not_a_chore():
    card = {"type": "done", "title": "Chore check", "headline": "Chores for you are refused now.",
            "full_answer": "Summaries that say \"you can run…\" or \"`x` clears it\" are sent back."}
    assert not ev.chore_problems(card)


def test_a_step_only_he_can_do_is_not_a_chore():
    card = {"type": "needs_you", "title": "Store login", "headline": "Sign in to the store once.",
            "explained": "You need to run the sign in yourself: it asks for your password and 2FA code."}
    assert not ev.chore_problems(card)


def test_a_test_server_still_running_on_a_done_card_is_sent_back():
    card = {"type": "done", "title": "Gallery thumbnails", "headline": "Thumbnails load in order.",
            "explained": "Everything works. My test server is still running in case you want it."}
    assert any("left behind" in p for p in ev.chore_problems(card))


# ── 5. Full is the deep version, not a log ─────────────────────────────────

def test_a_full_tab_over_the_cap_is_sent_back_but_a_deliverable_body_is_not():
    long = " ".join(["word"] * (rules.MAX_FULL_WORDS + 1))
    card = {"type": "done", "title": "Gallery thumbnails", "headline": "Thumbnails load in order.",
            "full_answer": long}
    assert any("Full (full_answer)" in p for p in rules.length_problems(card))
    card2 = {"type": "deliverable", "title": "Gallery notes", "headline": "The notes you asked for.",
             "deliverable": {"kind": "notes", "title": "Notes", "body": long}}
    assert not [p for p in rules.length_problems(card2) if "full_answer" in p]


def test_the_instructions_say_so():
    t = rules.INSTRUCTIONS
    assert "Stay on the user's task" in t and "400 words" in t and "automated session message" in t
