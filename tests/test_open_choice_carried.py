"""Smart termination keeps an unanswered choice open.

A session asks the user to pick between designs; the user asks for an
explanation, then for a demo of one of them.  The next card must not end as
Done with "stays unchanged until a design is chosen" and nothing to tap.  A
question back or another request is not a pick.  The cases below are made up.
"""

import json
from dataclasses import dataclass

from engine import handoff_evidence as ev
from engine import handoff_rules as rules


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


def test_a_pick_by_key_with_changes_after_it_closes_it():
    # A pick followed by a change ("option b however …", "… except …") is still a pick.
    for said in ("we should do option b however make the icons bigger",
                 "go with c though keep the old colours",
                 "option a except the strip should be grey",
                 "yeah so i like b, but the icons still need work",
                 "i like c, did we lose the highlight it had before?"):
        f = ev.facts_from_entries(_entries(E("user", said), E("asst", "on it")))
        assert f.open_choice is None, said


def test_a_direction_naming_one_choice_in_his_own_words_closes_it():
    f = ev.facts_from_entries(_entries(E("user", "lets keep the strip and ship it")))
    assert f.open_choice is None


def test_a_go_ahead_after_a_demo_of_one_choice_closes_it():
    # A go-ahead such as "ok build it" after a demo of one choice is a pick.
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
                 "forget that, do the sidebar instead", "never mind", "either is fine",
                 # The choice named by what it is about (a "drop the <topic>
                 # question" reply drops it).
                 "Drop the toolbar layout question, the first answer covers it",
                 "skip that layout choice"):
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


def test_a_key_inside_a_question_is_not_a_pick():
    f = ev.facts_from_entries(_entries(E("user", "do you like b?"), E("asst", "on it")))
    assert f.open_choice is not None


def test_his_own_version_instead_of_the_choices_closes_it():
    # Answering a choice with an idea of one's own ("i was imagining …") answers
    # it: later summaries must not keep carrying it in a Left waiting strip.
    for said in ("i was imagining a floating bar under the title, maybe an icon",
                 "i'd rather have a single row of icons",
                 "none of these, put it in the menu",
                 "i had in mind something much smaller"):
        f = ev.facts_from_entries(_entries(E("user", said), E("asst", "on it")))
        assert f.open_choice is None, said
        assert ev.open_choice_problems(DEMO_DONE, f) == [], said


def test_looks_good_on_what_was_built_closes_it():
    for said in ("looks good", "ok looks great", "that's it", "love it", "lgtm", "works now"):
        f = ev.facts_from_entries(_entries(E("user", "make the icons smaller"), E("asst", _msg(DEMO_DONE)),
                                           E("user", said), E("asst", "on it")))
        assert f.open_choice is None, said


def test_asking_what_he_imagines_is_still_not_a_pick():
    for said in ("i was thinking about it, which one is faster?", "does it look good on the phone?"):
        f = ev.facts_from_entries(_entries(E("user", said), E("asst", "on it")))
        assert f.open_choice is not None, said


# A session that keeps polishing the preview a "ship it?" choice is
# about and ended every turn on a Done with the choice in back_to.  Each card
# reads as finished while nothing reaches the real page.  Made-up case.
SHIP = {"type": "needs_you", "title": "Ship the new header",
        "headline": "Should the new header replace the old one on the live page?",
        "asks": [{"ask": "make the header feel more premium", "state": "needs_you"}],
        "needs": {"kind": "decision", "ask": "Put the new header on the live page?",
                  "look": [{"label": "Header", "kind": "link", "url": "/static/scratch/header-v3.html"}],
                  "replies": [{"key": "P", "label": "Ship the new header", "reply": "Ship the new header.",
                               "screens": [{"label": "New", "url": "/static/scratch/header-v3.html#P"}]},
                              {"key": "now", "label": "Keep the live page", "reply": "Keep the live page as it is.",
                               "screens": [{"label": "Now", "url": "/static/scratch/header-v3.html#now"}]}]}}


def _ship(*after):
    return [E("user", "make the header feel more premium"), E("asst", _msg(SHIP))] + list(after)


def test_polishing_the_thing_the_choice_is_about_must_ask_it_again():
    f = ev.facts_from_entries(_ship(E("user", "the shadow looks cut off, lighten it")))
    polished = dict(DEMO_DONE, title="Header shadow", validate={"how": "Look", "url": "/static/scratch/header-v3.html#P"},
                    back_to={"label": "Ship the new header", "status": "waiting on your pick",
                             "reply": "Show me the choices again"})
    out = ev.open_choice_problems(polished, f)
    assert out and "very thing" in out[0] and "header-v3.html" in out[0]
    asked = dict(SHIP, answer="The shadow is lighter and no longer cut off.")
    assert ev.open_choice_problems(asked, f) == []


def test_other_work_may_still_carry_it_in_back_to():
    f = ev.facts_from_entries(_ship(E("user", "fix the typo in the footer")))
    other = dict(DEMO_DONE, title="Footer typo", validate={"how": "Look", "url": "/static/scratch/footer.html"},
                 back_to={"label": "Ship the new header", "status": "waiting on your pick",
                          "reply": "Show me the choices again"})
    assert ev.open_choice_problems(other, f) == []


def test_walking_away_from_the_choices_closes_it():
    # Turning the choices down and asking for something else, or asking for
    # what was shown before instead, must not bring the choices back in a
    # Left waiting strip.
    for said in ("I dont even necessarily want to trim.\n\nWhat about putting the bottom line below the fold",
                 "i don't really want to change the toolbar",
                 "no the one i liked",
                 "not the one I wanted"):
        f = ev.facts_from_entries(_entries(E("user", said), E("asst", "on it")))
        assert f.open_choice is None, said
    # a question about another case stays a question
    assert ev.facts_from_entries(_entries(E("user", "ok but\nwhat about phones?"))).open_choice is not None


# ── A pick made in the user's own words, shown by the work ───────────────
# The user answers with a direction that names no choice; the session builds
# it and ends on Done; the user moves on.  The choice is closed.

BUILT = {"type": "done", "title": "Editor toolbar", "headline": "The editor toolbar now sits in a strip like the menus.",
         "what_was_done": "Built the toolbar layout into the editor.",
         "needs": {"kind": "none", "why": "Built.", "next": "Nothing."}}


def test_a_direction_then_a_build_on_its_topic_then_moving_on_closes_it():
    f = ev.facts_from_entries(_entries(E("user", "put it up with the menus like the file bar"),
                                       E("asst", _msg(BUILT)), E("user", "now tidy the status bar")))
    assert f.open_choice is None


def test_an_objection_after_the_build_keeps_it_open():
    for said in ("no, that's not what i meant", "wrong one, go back", "that is wrong, redo it"):
        f = ev.facts_from_entries(_entries(E("user", "put it up with the menus like the file bar"),
                                           E("asst", _msg(BUILT)), E("user", said)))
        assert f.open_choice is not None, said


def test_the_build_must_be_answered_by_a_next_message_and_be_on_the_choices_topic():
    # the build is the card being checked: nothing from the user after it yet
    f = ev.facts_from_entries(_entries(E("user", "put it up with the menus like the file bar"), E("asst", _msg(BUILT))))
    assert f.open_choice is not None
    off = dict(BUILT, title="Login page", headline="The login page shows a remember-me box.",
               what_was_done="Added a remember-me box to the login page.")
    f = ev.facts_from_entries(_entries(E("user", "the login page should remember me between visits"),
                                       E("asst", _msg(off)), E("user", "now tidy the status bar")))
    assert f.open_choice is not None


def test_a_question_or_a_look_first_request_is_not_a_direction():
    for said in ("which one is faster?", "show me the strip one again"):
        f = ev.facts_from_entries(_entries(E("user", said), E("asst", _msg(BUILT)), E("user", "ok next thing")))
        assert f.open_choice is not None, said
