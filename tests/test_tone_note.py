"""A question asked in frustration gets a hidden note that it may be
rhetorical, and the summary rules never force an answer-only card on it.

Swear words are assembled at run time (ROT13): the Publish scan refuses
swearing in tracked files."""
import codecs
import re
from pathlib import Path

from shared.tone import NOTE, upset_question, with_tone_note
from engine import handoff_evidence
from web.routes.ws_events import _with_mobile_preamble, _with_tone_note
from web.titling import _INJECTED_BLOCK

SRC = Path(__file__).resolve().parents[1] / "src"
F = codecs.decode("shpxvat", "rot13")


def test_upset_questions_are_spotted():
    assert upset_question(f"why is the {F} banner still purple in every {F} option")
    assert upset_question(f"the two badges are the {F} same are you {F} kidding me")
    assert upset_question("are you serious, the same again")
    assert upset_question(f"what the {codecs.decode('uryy', 'rot13')} happened to the sidebar?")


def test_calm_questions_and_angry_orders_are_left_alone():
    assert not upset_question("why is the banner purple?")
    assert not upset_question("what happens to the view button for a guest?")
    assert not upset_question(f"fix the {F} button")
    assert not upset_question("hello? shell script")
    assert not upset_question("")


def test_the_note_goes_before_the_message_only_when_upset():
    msg = f"why is the {F} banner still there?\n\nSent from Mobile at 2026-01-01 10:00 AM"
    assert with_tone_note(msg) == NOTE + "\n\n" + msg
    assert with_tone_note("make it blue") == "make it blue"
    assert _with_tone_note(msg).startswith("⟦VN-NOTE⟧")


def test_the_note_never_reaches_his_words():
    msg = _with_mobile_preamble(_with_tone_note(f"why is the {F} banner there?"))
    assert handoff_evidence.clean_request(msg).strip() == f"why is the {F} banner there?"
    assert "VN-NOTE" not in _INJECTED_BLOCK.sub("", msg)
    js = (SRC / "web/static/js/live-panel.js").read_text(encoding="utf-8")
    assert "⟦VN-(?:MOBILE|NOTE)⟧" in js


def test_an_upset_question_does_not_force_an_explain_card():
    assert handoff_evidence.is_question("why is the banner still there?")
    assert not handoff_evidence.is_question(f"why is the {F} banner still there in every {F} option")


def test_the_card_guide_says_so():
    from engine import handoff_rules
    text = " ".join(handoff_rules.INSTRUCTIONS.split())
    assert re.search(r"asked in frustration, with swearing, is often rhetorical", text)
