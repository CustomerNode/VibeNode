"""A question only the user's own words can answer gets no button: a button
that only stands in for typing sends a message with nothing in it."""

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


def test_a_lead_in_button_is_sent_back():
    c = card(replies=[{"key": "A", "label": "I'll type it in the reply box", "reply": "Here is the first change:"}])
    assert rules.typing_button_problems(c)
    assert any("stand" in p for p in st.unusable_problems(c))


def test_a_box_pointer_button_is_sent_back():
    c = card(replies=[{"key": "A", "label": "Sent it in the reply box", "reply": "Sent the change above."}])
    assert rules.typing_button_problems(c)


def test_an_action_still_needs_a_button():
    # An action the user's own words answer has none (the reply box answers
    # it: tests/test_words_card_buttons.py); any other action keeps its button.
    press = card(ask="Press Publish in the Garden app again.", steps=[{"text": "Press Publish."}])
    assert st.unusable_problems(press)
    assert st.unusable_problems(card()) == []


def test_real_done_buttons_pass():
    for label, reply in (("I did it", "I did it"), ("Logged in", "I logged in"),
                         ("Done", "Done: the key is in the settings file")):
        assert rules.typing_button_problems(card(replies=[{"key": "A", "label": label, "reply": reply}])) == [], label
