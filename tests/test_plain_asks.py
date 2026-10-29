"""A yes-or-no the user can picture, and a home for findings (2026-10-28).

The card that started this ended a test-cleanup task with an Approve:
"Should Claude Auto in VibeNode refuse the Windows delete-everything commands
(del /s /q, rmdir /s /q, Remove-Item -Recurse -Force) like rm -rf?"  The
owner: "I don't know what you are talking about."  He had asked for a test
cleanup; a test the session wrote found a permission gap it may not fix
alone.  Three things let that card through, and three checks close them:

1. the rules had no place for a finding: a done card may not mention one
   (unfinished work), so the session turned it into an approval.  Now
   ``finding`` {what, risk, recommend} is drawn as an "Also found" band and
   is neither unfinished work nor a decision (``finding_problems``);
2. a Decide card had to be tied to the user's request, an Approve did not;
   the card's own "asks" entry, text it wrote for itself, was enough.  Now
   both are matched against his actual messages (``reviewer_problems``);
3. nothing stopped a question written in command syntax and code names, with
   no recommendation.  Now the ask, why, next and button labels of a
   Decide/Approve carry none of those, and one reply is marked recommended
   (``plain_choice_problems``); a pick between mockups is exempt.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass

from daemon import handoff_evidence as ev
from daemon import handoff_rules as hr
from daemon import smart_termination as st


@dataclass
class E:
    kind: str
    text: str = ""


def _msg(card: dict) -> str:
    return "```handoff\n" + json.dumps(card) + "\n```"


# The card the owner could not place, as sent (shortened "explained").
BAD = {
    "type": "needs_you", "path": ["Publish checks", "Cleanup"], "title": "Test suite cleanup",
    "headline": "VibeNode's test suite is leaner, faster and guards sleep and auto-approval; one refuse-list choice is yours.",
    "context": "Publish-check tests",
    "zoom": {"big": "Keep the publish checks fast and sound", "task": "The test-suite cleanup you approved",
             "now": "Full run green: 4,234 passed, 0 failed"},
    "status": {"done": ["Dead browser tests, orphan script, nightly job removed"]},
    "asks": [{"ask": "Yes, do the whole test-suite cleanup", "state": "done"},
             {"ask": "refuse the Windows delete commands under Claude Auto", "state": "needs_you"}],
    "links": [{"label": "Run log", "kind": "file", "url": "logs/cleanup_full_run.log"}],
    "needs": {"kind": "approve",
              "ask": "Should Claude Auto in VibeNode refuse the Windows delete-everything commands (del /s /q, rmdir /s /q, Remove-Item -Recurse -Force) like rm -rf?",
              "why": "The new test shows it approves them today; a refuse-list change is a permission rule.",
              "next": "On yes I add them to both refuse lists and extend the test.",
              "replies": [{"key": "A", "label": "Yes, refuse them", "reply": "Yes, refuse the Windows delete commands under Claude Auto"},
                          {"key": "B", "label": "Leave as is", "reply": "Leave Claude Auto's refuse list as it is"}]},
    "explained": " ".join(["word"] * 45),
}

# The same news, the way it should have been handed over: a finished card
# with the gap as a finding.
GOOD = {
    "type": "done", "path": ["Publish checks", "Cleanup"], "title": "Test suite cleanup",
    "headline": "VibeNode's test suite is leaner, faster and guards sleep and auto-approval.",
    "context": "Publish-check tests",
    "zoom": {"big": "Keep the publish checks fast and sound", "task": "The test-suite cleanup you approved",
             "now": "Full run green: 4,234 passed, 0 failed"},
    "status": {"done": ["Dead browser tests, orphan script, nightly job removed",
                        "Two safety tests added: auto-approval, sleep recovery"]},
    "verified": [{"check": "Full suite, publish command", "result": "4,234 passed, 0 failed", "ok": True}],
    "asks": [{"ask": "Yes, do the whole test-suite cleanup", "state": "done"}],
    "links": [{"label": "Run log", "kind": "file", "url": "logs/cleanup_full_run.log"}],
    "validate": {"how": "Open the test modes list; then press Update and watch the run.", "url": "/api/test-modes"},
    "needs": {"kind": "none", "why": "All approved work is in and green.", "next": "The next Update publishes it."},
    "finding": {"what": "On Windows, Auto mode lets a session wipe a folder without asking; on Mac that is stopped.",
                "risk": "A session under Auto mode could delete a folder on your PC unasked.",
                "recommend": "Stop it on Windows too; a permission rule, so not changed without you.",
                "reply": "Stop sessions wiping folders on Windows under Auto mode"},
    "explained": " ".join(["word"] * 45),
}

USER_ASKED = "Yes, do the whole test-suite cleanup"


def _facts(*user_messages: str):
    entries = [E("user", m) for m in user_messages] or [E("user", USER_ASKED)]
    return ev.facts_from_entries(entries)


# ── 3. The question reads like code, and has no recommendation ───────────

def test_the_owners_card_is_sent_back_for_code_in_the_question_and_no_recommendation():
    out = " ".join(hr.plain_choice_problems(BAD))
    assert "read like code" in out
    for token in ('"del /s" (needs.ask)', '"/q" (needs.ask)', '"Remove-Item -Recurse" (needs.ask)',
                  '"-Force" (needs.ask)', '"rm -rf" (needs.ask)'):
        assert token in out, (token, out)
    assert '"recommended": true' in out
    reason = st.review(_msg(BAD), continued=False, sendbacks=0)
    assert reason and "read like code" in reason and "recommended" in reason


def test_a_plain_yes_or_no_with_a_recommendation_passes():
    card = copy.deepcopy(BAD)
    card["needs"]["ask"] = ("Should Auto mode stop a session from wiping a folder on Windows, "
                            "the way it is already stopped on Mac?")
    card["needs"]["next"] = "On yes I add the Windows forms to the refuse list and extend the test."
    card["needs"]["replies"][0]["recommended"] = True
    card["needs"]["replies"][0]["detail"] = "del /s /q, rmdir /s /q and Remove-Item -Recurse -Force join rm -rf"
    assert hr.plain_choice_problems(card) == []


def test_code_tokens_each_form():
    def ask(text):
        c = copy.deepcopy(BAD)
        c["needs"]["ask"] = text
        c["needs"]["replies"][0]["recommended"] = True
        return " ".join(hr.plain_choice_problems(c))
    assert '"--force"' in ask("Allow git push --force here?")
    assert '"chrome_control"' in ask("Turn chrome_control on?")
    assert '"`rm`"' in ask("Allow `rm` in Auto mode?")
    assert '"run()"' in ask("Should run() retry?")
    assert '"$HOME"' in ask("Read $HOME at start?")
    assert '"git push"' in ask("Allow git push from sessions?")
    # English hyphens, dates, slashes in prose and quoted names are not code.
    assert ask("A yes-or-no on the 2026-10-28 change: block it and/or warn, as “sub() fix” asks?") == ""
    assert ask("Re-use the old T-shirt design (the one with the logo)?") == ""


def test_the_fine_print_may_name_the_command():
    card = copy.deepcopy(BAD)
    card["needs"]["ask"] = "Stop sessions from wiping folders on Windows under Auto mode?"
    card["needs"]["next"] = "On yes I add them and extend the test."
    card["needs"]["replies"][0]["recommended"] = True
    card["needs"]["replies"][0]["detail"] = "Refuses del /s /q, rmdir /s /q, Remove-Item -Recurse -Force, taskkill /F"
    assert hr.plain_choice_problems(card) == []


def test_a_button_label_in_code_is_sent_back():
    card = copy.deepcopy(BAD)
    card["needs"]["ask"] = "Stop sessions from wiping folders on Windows under Auto mode?"
    card["needs"]["next"] = "On yes I add them and extend the test."
    card["needs"]["replies"][0].update(label="Yes, refuse rm -rf and del /s /q", recommended=True)
    assert '"rm -rf" (needs.replies[0].label)' in " ".join(hr.plain_choice_problems(card))


def test_exactly_one_recommended_reply():
    card = copy.deepcopy(BAD)
    card["needs"]["ask"] = "Stop sessions from wiping folders on Windows under Auto mode?"
    card["needs"]["next"] = "On yes I add them and extend the test."
    assert "recommended" in " ".join(hr.plain_choice_problems(card))
    for r in card["needs"]["replies"]:
        r["recommended"] = True
    assert "More than one" in " ".join(hr.plain_choice_problems(card))
    card["needs"]["replies"][1]["recommended"] = False
    assert hr.plain_choice_problems(card) == []


def test_a_pick_between_mockups_needs_no_recommendation():
    card = copy.deepcopy(BAD)
    card["needs"].update(kind="decision", ask="Which header do you want on the article?",
                         next="I build the one you pick.")
    for i, r in enumerate(card["needs"]["replies"]):
        r["label"] = "Header with a " + ("frustrated" if i else "calm") + " person"
        r["screens"] = [{"label": r["label"], "url": f"/static/scratch/headers.html#{r['key']}", "device": "web"}]
    assert hr.plain_choice_problems(card) == []


def test_an_approval_answered_on_its_own_page_needs_no_recommendation():
    card = copy.deepcopy(BAD)
    card["needs"].update(ask="Approve the CustomerNode release on its page?",
                         next="The release runs once you answer on the page.", replies=[],
                         look=[{"label": "Approval page", "kind": "link", "url": "http://127.0.0.1:8765/"}])
    assert hr.plain_choice_problems(card) == []


def test_cards_that_ask_nothing_are_not_scanned():
    card = copy.deepcopy(GOOD)
    card["needs"]["next"] = "Update commits it; the refuse list keeps rm -rf and --force."
    assert hr.plain_choice_problems(card) == []


# ── 1. A home for findings ────────────────────────────────────────────────

def test_a_finished_card_may_carry_a_finding():
    assert hr.finding_problems(GOOD) == []
    assert hr.review(_msg(GOOD)) is None, hr.review(_msg(GOOD))


def test_a_findings_words_for_the_open_thing_are_not_unfinished_work():
    card = copy.deepcopy(GOOD)
    card["finding"]["what"] = "A known gap, left open for now: Auto mode does not stop Windows folder wipes."
    card["finding"]["risk"] = "Pre-existing, not from my change, and still untested on Windows."
    assert hr.review(_msg(card)) is None, hr.review(_msg(card))
    # The same words anywhere else on a done card are still sent back.
    bad = copy.deepcopy(GOOD)
    bad["needs"]["next"] = "A known gap is left open for now."
    reason = hr.review(_msg(bad))
    assert reason and "unfinished work" in reason


def test_a_finding_still_cannot_sound_like_ai_or_name_files():
    card = copy.deepcopy(GOOD)
    card["finding"]["recommend"] = "Honestly, quietly patch daemon/permission_manager.py."
    reason = hr.review(_msg(card))
    assert reason and "sound like AI" in reason and "file names" in reason


def test_a_finding_needs_its_three_lines():
    card = copy.deepcopy(GOOD)
    card["finding"] = {"what": "Something"}
    out = " ".join(hr.finding_problems(card))
    assert "Missing: risk, recommend" in out
    card["finding"] = "a string"
    assert hr.finding_problems(card)
    assert hr.finding_problems(copy.deepcopy(BAD)) == []   # absent: nothing to say


def test_a_finding_is_outside_the_120_words_but_each_line_is_capped():
    card = copy.deepcopy(GOOD)
    base = hr.length_problems(card)
    card["finding"]["what"] = " ".join(["word"] * 24)
    assert hr.length_problems(card) == base
    card["finding"]["what"] = " ".join(["word"] * 26)
    assert any("finding.what is 26 words" in p for p in hr.length_problems(card))


# ── 2. An approval comes from his words ───────────────────────────────────

def test_an_approval_not_tied_to_his_request_is_sent_back():
    out = " ".join(ev.reviewer_problems(BAD, _facts(USER_ASKED)))
    assert "Approve card is not tied to anything the user asked for" in out
    assert '"finding"' in out


def test_an_approval_tied_to_his_request_passes_the_tie_check():
    card = copy.deepcopy(BAD)
    card["asks"] = [{"ask": "refuse the Windows delete commands under Auto mode", "state": "needs_you"}]
    facts = _facts("Please make Auto mode refuse the Windows delete commands too")
    assert not any("not tied" in p for p in ev.reviewer_problems(card, facts))


def test_a_decision_is_still_held_to_the_same_tie():
    card = copy.deepcopy(BAD)
    card["needs"]["kind"] = "decision"
    card["needs"]["look"] = [{"label": "x", "kind": "link", "url": "/static/scratch/x.html"}]
    out = " ".join(ev.reviewer_problems(card, _facts(USER_ASKED)))
    assert "Decide card is not tied to anything the user asked for" in out


def test_the_instructions_teach_all_three():
    text = hr.INSTRUCTIONS
    assert '"finding"' in text and "Also found" in text
    assert '"recommended": true' in text
    assert "command" in text and "code names" in text
    assert "tied to" in text and "approval" in text
