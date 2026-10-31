"""Smart termination keeps an unanswered choice open (added 2026-10-31).

The owner, on a session that asked him to pick between three designs: he
asked for an explanation, then for a demo of one of them, and the next card
ended as Done with "stays unchanged until a design is chosen" and nothing to
tap.  A question back or another request is not a pick.  The cases below are
made up in the same shape.
"""

import json
from dataclasses import dataclass

from daemon import handoff_evidence as ev
from daemon import handoff_rules as rules


@dataclass
class E:
    kind: str
    text: str = ""


def _msg(card: dict) -> str:
    return "```handoff\n" + json.dumps(card) + "\n```"


CHOICE = {"type": "needs_you", "title": "Toolbar layout",
          "headline": "Pick a layout for the toolbar in the editor",
          "asks": [{"ask": "build a clickable mockup of the toolbar layouts", "state": "needs_you"}],
          "needs": {"kind": "decision", "ask": "Which toolbar layout should the editor use?",
                    "replies": [{"key": "A", "label": "Icons on the left", "reply": "Go with icons on the left"},
                                {"key": "B", "label": "Icons in a top strip", "reply": "Go with icons in a top strip"},
                                {"key": "C", "label": "Both, kept in step", "reply": "Go with both, kept in step"}]}}

DEMO_DONE = {"type": "done", "title": "Toolbar demo", "headline": "The left-icons demo is built and clickable.",
             "needs": {"kind": "none", "why": "Nothing is running.", "next": "The editor stays as it is."}}


def _entries(*after):
    return [E("user", "build a clickable mockup of the toolbar layouts"), E("asst", _msg(CHOICE))] + list(after)


def test_another_request_leaves_the_choice_open_and_a_card_that_drops_it_is_sent_back():
    f = ev.facts_from_entries(_entries(E("user", "give me a demo of the left icons one with a full reply")))
    assert f.open_choice and f.open_choice["title"] == "Toolbar layout"
    out = ev.open_choice_problems(DEMO_DONE, f)
    assert out and "has not picked yet" in out[0] and "Icons on the left" in out[0]


def test_a_question_back_leaves_it_open_too():
    f = ev.facts_from_entries(_entries(E("user", "i don't see the differences, explain and recommend one")))
    assert f.open_choice is not None


def test_a_pick_closes_it():
    for said in ("Go with icons in a top strip", "B", "go with B", "Icons on the left", "both, kept in step."):
        f = ev.facts_from_entries(_entries(E("user", said), E("asst", "on it")))
        assert f.open_choice is None, said
        assert ev.open_choice_problems(DEMO_DONE, f) == [], said


def test_a_direction_naming_one_choice_in_his_own_words_closes_it():
    f = ev.facts_from_entries(_entries(E("user", "lets keep the strip and ship it")))
    assert f.open_choice is None


def test_a_go_ahead_after_a_demo_of_one_choice_closes_it():
    # The first version of this rule missed "ok build it" after a demo, and
    # the session added a strip and a confirm card he could not follow.
    entries = _entries(E("user", "give me a demo of the left icons one with a full reply"),
                       E("asst", _msg(DEMO_DONE)), E("user", "ok build it"))
    f = ev.facts_from_entries(entries)
    assert f.open_choice is None
    assert ev.open_choice_problems(dict(DEMO_DONE, title="Toolbar built"), f) == []
    for said in ("build it", "yes, ship that", "ok", "looks good, use that one"):
        assert ev.facts_from_entries(_entries(E("user", said))).open_choice is None, said


def test_the_offer_button_is_not_a_go_ahead_on_the_choice():
    for said in ("Yes, do that: Mock up a fourth layout", "ok but what about phones?"):
        assert ev.facts_from_entries(_entries(E("user", said))).open_choice is not None, said


def test_asking_to_see_one_choice_first_is_not_a_pick():
    for said in ("show me the strip version in a demo", "mock up the strip one with a real reply"):
        assert ev.facts_from_entries(_entries(E("user", said))).open_choice is not None, said


def test_handing_over_or_dropping_it_closes_it():
    for said in ("you pick", "your call", "Do your choice", "go with what you recommend",
                 "forget that, do the sidebar instead", "never mind", "either is fine"):
        f = ev.facts_from_entries(_entries(E("user", said)))
        assert f.open_choice is None, said


def test_naming_a_choice_in_a_question_is_not_a_pick():
    f = ev.facts_from_entries(_entries(E("user", "what does icons on the left look like on a phone?")))
    assert f.open_choice is not None


def test_asking_it_again_passes():
    f = ev.facts_from_entries(_entries(E("user", "explain the differences please")))
    again = dict(CHOICE, answer="The strip keeps the canvas wider.")
    assert ev.open_choice_problems(again, f) == []


def test_carrying_it_in_back_to_passes():
    f = ev.facts_from_entries(_entries(E("user", "give me a demo of the left icons one")))
    carried = dict(DEMO_DONE, back_to={"label": "Toolbar layout choice", "status": "waiting on your pick",
                                        "reply": "Show me the choices again"})
    assert ev.open_choice_problems(carried, f) == []
    unrelated = dict(DEMO_DONE, back_to={"label": "Login page copy", "status": "half done", "reply": "Back to it"})
    assert ev.open_choice_problems(unrelated, f)


def test_a_sent_back_card_and_the_card_being_checked_do_not_count():
    sent_back = [E("user", "build a clickable mockup of the toolbar layouts"), E("asst", _msg(CHOICE)),
                 E("user", "Stop hook feedback: too long"), E("asst", _msg(DEMO_DONE))]
    assert ev.facts_from_entries(sent_back).open_choice is None
    assert ev.facts_from_entries(sent_back[:2]).open_choice is None


def test_the_rule_runs_in_the_full_review():
    entries = _entries(E("user", "give me a demo of the left icons one with a full reply"))
    out = ev.problems(DEMO_DONE, ev.facts_from_entries(entries))
    assert any("has not picked yet" in p for p in out)


def test_a_next_line_that_waits_on_a_pick_is_caught():
    for nxt in ("The editor stays unchanged until a layout is chosen.", "Nothing ships until you pick.",
                "It stays as is until the toolbar design gets approved."):
        assert rules._WAITS_ON_USER.search(nxt), nxt
    for nxt in ("It runs until the suite is finished.", "Nothing changes until tomorrow's deploy."):
        assert not rules._WAITS_ON_USER.search(nxt), nxt
