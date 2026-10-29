"""Smart termination: no leaving work to a session that has stopped, and no
Next line that only hopes (added 2026-10-28).

The owner, on a Blocked card that left 3 failing tests to "another session's
in-progress edit" (that session had stopped 11 minutes earlier), with the
Next line "That session's own test run should catch these 3": "how do we fix
the code in smart termination so this does not happen again".
"""

from __future__ import annotations

import json

from daemon import handoff_evidence as ev
from daemon import handoff_rules as rules
from daemon.session_manager import SessionManager, SessionInfo, SessionState


def _blocked(**over):
    card = {
        "type": "blocked",
        "path": ["Publish", "Pre-publish tests"],
        "title": "Publish test failures",
        "headline": "All 30 failures that blocked publish are fixed. Another session's status change now breaks 3 more tests.",
        "status": {"done": ["24 Code Pipeline card tests"], "remaining": [],
                   "broken": ["2 homepage tests and 1 sort-order test still fail"]},
        "tried": ["Re-ran them at 8:11 PM: still failing"],
        "needs": {"kind": "none", "why": "Another session is changing when a session counts as Done.",
                  "next": "That session's own test run should catch these 3."},
    }
    card.update(over)
    return card


def test_the_card_that_started_this_is_sent_back_for_both_reasons():
    msg = "```handoff\n" + json.dumps(_blocked()) + "\n```"
    reason = rules.review(msg, None)
    assert reason and "only hopes" in reason and "another session" in reason


def test_a_hope_in_next_is_sent_back_on_any_card():
    for nxt in ("That session's own test run should catch these 3.", "Hopefully the nightly run covers it.",
                "CI will probably pick it up.", "The other session should pick them up."):
        assert ev.hope_problems({"type": "done", "needs": {"kind": "none", "next": nxt}}), nxt


def test_a_plain_next_passes():
    for nxt in ("Nothing is committed.", "It finishes in about 5 minutes and posts the result here.",
                "The deploy should take 5 minutes.", "Publish is no longer blocked."):
        assert not ev.hope_problems({"type": "done", "needs": {"kind": "none", "next": nxt}}), nxt


def test_blaming_a_session_when_nobody_is_working_is_sent_back():
    out = ev.blame_problems(_blocked(), [])
    assert out and "no other session in this project is working" in out[0]


def test_blaming_a_session_that_really_is_working_passes():
    assert ev.blame_problems(_blocked(), ["a1b2c3d4"]) == []


def test_unknown_needs_a_dated_sighting_in_tried():
    assert ev.blame_problems(_blocked(), None)                     # "at 8:11 PM: still failing" is not a sighting
    seen = _blocked(tried=["Checked 8:11 PM: the other session is still editing, last edit 8:10"])
    assert ev.blame_problems(seen, None) == []


def test_mentioning_other_sessions_on_finished_work_is_not_blame():
    card = {"type": "done", "headline": "The fix also covers other sessions",
            "status": {"done": ["x"], "remaining": [], "broken": []}}
    assert ev.blame_problems(card, []) == []


def test_facts_carry_who_is_working_into_the_review():
    facts = ev.TurnFacts(others_working=[])
    assert any("no other session" in p for p in ev.problems(_blocked(), facts))
    facts.others_working = ["a1b2c3d4"]
    assert not any("another session" in p for p in ev.problems(_blocked(), facts))


def test_engine_lists_working_sessions_in_the_same_folder_only():
    sm = SessionManager.__new__(SessionManager)
    import threading
    sm._lock = threading.RLock()

    def info(sid, cwd, state):
        i = SessionInfo(session_id=sid, cwd=cwd)
        i.state = state
        return i

    sm._sessions = {
        "me-000000000": info("me-000000000", "C:/repo", SessionState.WORKING),
        "busy-1111111": info("busy-1111111", "C:/repo/", SessionState.WORKING),
        "idle-2222222": info("idle-2222222", "C:/repo", SessionState.IDLE),
        "else-3333333": info("else-3333333", "C:/other", SessionState.WORKING),
    }
    assert sm._others_working("me-000000000", "C:/repo") == ["busy-111"]
    assert sm._others_working("me-000000000", "") == []


def test_the_other_sessions_note_no_longer_says_stop():
    from app.session_awareness import _CROSS_SESSION_TEMPLATE as t
    assert "never a reason to leave your task unfinished" in t
    assert "snapshot" in t
    assert "Not completed" not in t
