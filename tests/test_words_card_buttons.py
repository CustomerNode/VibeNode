"""A card that asks for the user's own words is answered in the reply box.

Any button on it is a stand-in: a "done" that carries nothing, or an errand
("show me the list first") the session should have run before it stopped.
And a button that only asks the session to put something on screen is
refused on every card: the session shows it before it asks."""

import copy

from engine import handoff_rules as rules
from engine import smart_termination as st

BASE = {
    "type": "needs_you",
    "path": ["Garden app", "Seed list"],
    "title": "Seed list",
    "headline": "Ready on the Garden app seed list: tell me the first thing you want changed there.",
    "context": "You reopened the seed list.",
    "answer": "Nothing built yet; name the first change.",
    "needs": {
        "kind": "action",
        "ask": "Tell me the first change you want on the Garden app seed list.",
        "why": "I build from your words only.",
        "next": "I build that one change.",
        "steps": [{"text": "Type the change you want."}],
    },
}


def card(**needs):
    c = copy.deepcopy(BASE)
    c["needs"].update(needs)
    return c


def test_a_words_card_needs_no_button():
    c = card()
    assert rules.asks_for_own_words(c)
    assert rules.words_card_problems(c) == []
    assert not any("action" in p.lower() and "button" in p.lower() for p in rules.format_problems(c))
    assert st.unusable_problems(c) == []


def test_any_button_on_a_words_card_is_sent_back():
    for label, reply in (("Show me the list now", "Show me the seed list as it is now so I can point at it."),
                         ("Open the live list", "Open the live seed list."),
                         ("Sort it by season", "Sort the seed list by planting season.")):
        c = card(replies=[{"key": "A", "label": label, "reply": reply}])
        probs = rules.words_card_problems(c)
        assert probs and "reply box" in probs[0], label
        assert any("reply box" in p for p in st.unusable_problems(c)), label


def test_other_asks_still_need_their_done_button():
    for ask, step in (("Press Publish in the Garden app again.", "Press Publish."),
                      ("Sign in to the Garden app, then tell me.", "Type your password on the sign-in page."),
                      ("Save the seed photo to Documents.", "Save it as seeds.png.")):
        c = card(ask=ask, steps=[{"text": step}])
        assert not rules.asks_for_own_words(c), ask
        assert any("button" in p for p in st.unusable_problems(c)), ask
        c2 = card(ask=ask, steps=[{"text": step}], replies=[{"key": "A", "label": "I did it", "reply": "I did it."}])
        assert rules.words_card_problems(c2) == [], ask


def test_an_errand_button_is_sent_back_on_any_card():
    c = copy.deepcopy(BASE)
    c["needs"] = {"kind": "decision", "ask": "Which seed list layout should the Garden app use?",
                  "why": "Both work.", "next": "I build it.",
                  "replies": [{"key": "A", "label": "Keep the table layout", "reply": "Keep the table."},
                              {"key": "B", "label": "Pull up both first", "reply": "Pull up both layouts so I can compare."}]}
    probs = rules.errand_button_problems(c)
    assert probs and "before you stop" in probs[0]


def test_real_choices_are_not_errands():
    for reply in ("Keep the table layout.", "Show the season column on the seed list.",
                  "Open the seed list to everyone in the club.", "Take the photos off the list."):
        c = card(kind="decision", replies=[{"key": "A", "label": "First option here", "reply": reply},
                                           {"key": "B", "label": "Second option here", "reply": "Leave it."}])
        assert rules.errand_button_problems(c) == [], reply


def test_the_guide_says_the_reply_box_answers():
    text = rules.INSTRUCTIONS.replace("\n", " ")
    assert "the reply box is the answer" in text
