"""A form question is one short sentence; its background goes in "context".

The popup lists every form question down its left side (the card itself
shows only the "Answer the N questions" button).  A question that carries its own
background (figures, history, a sentence before the ask) turns a form of
several into a wall of text, and the button that opens the form is lost under
it: the reader no longer sees that the card is a form at all.
``_form_question_faults`` (src/engine/handoff_rules.py) sends such a card back.
"""
from engine import handoff_rules as hr
from engine import smart_termination as st


def _opt(key, label, rec=False):
    return {"key": key, "label": label, "recommended": rec,
            "gain": f"What sample option {key} gets you",
            "verdict": f"Why sample option {key} {'wins' if rec else 'loses'}"}


def _q(qid, question):
    return {"id": qid, "question": question,
            "context": "The sample widget is slow today, and the team measured it twice last week.",
            "options": [_opt("A", "Cache the sample widget", True), _opt("B", "Leave the widget alone")]}


def _card(*questions):
    form = [_q(f"q{i}", q) for i, q in enumerate(questions)]
    return {"type": "needs_you", "title": "Sample widget choices", "headline": "Two sample questions",
            "context": "x", "needs": {"kind": "decision", "ask": "Answer the sample widget questions",
                                      "why": "They block the sample task", "form": form}}


def _short_faults(card):
    return [p for p in hr.split_problems(card) if "short sentence" in p]


def test_short_questions_pass():
    assert _short_faults(_card("Should the sample widget be cached?",
                               "Where does the sample setting live (panel, profile or both)?")) == []


def test_a_question_carrying_its_background_is_sent_back():
    long_one = ("Nearly every sample widget render waits on the slow store, doubling the wait. "
                "Keep waiting, or cache it?")
    probs = _short_faults(_card("Should the sample widget be cached?", long_one))
    assert probs and "question 2" in probs[0] and "context" in probs[0]


def test_a_single_sentence_over_the_word_cap_is_sent_back():
    rambling = ("Should the sample widget that sits on every page of the demo be cached for "
                "every visitor including the guests?")
    assert len(rambling.split()) > hr.FORM_QUESTION_MAX_WORDS
    assert _short_faults(_card("Should the sample widget be cached?", rambling))


def test_two_clauses_joined_by_a_semicolon_are_two_sentences():
    assert _short_faults(_card("Should the sample widget be cached?",
                               "The widget reads the store; keep that rule?"))


def test_a_version_number_is_not_a_sentence_end():
    assert _short_faults(_card("Should the sample widget be cached?",
                               "Ship the sample widget in version 2.4?")) == []


def test_sessions_are_told_the_rule():
    assert f"at most {hr.FORM_QUESTION_MAX_WORDS} words" in st.INSTRUCTIONS
    assert "context" in hr.SPLIT_TEMPLATES["form"]["rules"]


# ── The questions stay off the card ──────────────────────────────────────

def _face_faults(card):
    return [p for p in hr.split_problems(card) if "only in the popup" in p]


def test_a_card_that_keeps_its_questions_in_the_form_passes():
    c = _card("Should the sample widget be cached?", "Where does the sample setting live?")
    c["answer"] = "Two sample widget calls are open; answer them in the form."
    assert _face_faults(c) == []


def test_a_question_copied_into_the_short_answer_is_sent_back():
    c = _card("Should the sample widget be cached?", "Where does the sample setting live?")
    c["answer"] = "First: should the sample widget be cached? Then the setting."
    probs = _face_faults(c)
    assert probs and "question 1" in probs[0]


def test_a_question_copied_into_the_ask_is_sent_back():
    c = _card("Should the sample widget be cached?", "Where does the sample setting live?")
    c["needs"]["ask"] = "Where does the sample setting live, and the cache call"
    assert _face_faults(c)


def test_a_three_word_question_is_too_short_to_count_as_copied():
    c = _card("Cache the widget?", "Where does the sample setting live?")
    c["headline"] = "Cache the widget is one of two sample calls"
    assert _face_faults(c) == []


def test_sessions_are_told_the_card_shows_only_the_button():
    assert "the card shows only the button" in hr.SPLIT_TEMPLATES["form"]["rules"]
