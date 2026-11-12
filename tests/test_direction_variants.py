"""Variants answered with a direction of the user's own ("how about we make
the badge the button") are still a matter of taste: a Done card that builds
ONE version of that direction is sent back to show variants of it.  All
messages and cards are made up."""

import pytest

from engine import handoff_evidence as E
from engine import handoff_rules as R


def _decision():
    return {"type": "needs_you", "title": "Empty badge", "headline": "Pick how the empty badge reads.",
            "needs": {"kind": "decision", "ask": "Pick how the empty badge on the planner reads",
                      "replies": [
                          {"key": "A", "label": "Grey badge outline",
                           "screens": [{"label": "A", "url": "http://127.0.0.1:9900/planner?v=a"}]},
                          {"key": "B", "label": "Dotted badge with hint",
                           "screens": [{"label": "B", "url": "http://127.0.0.1:9900/planner?v=b"}]},
                          {"key": "C", "label": "Faint badge with caption",
                           "screens": [{"label": "C", "url": "http://127.0.0.1:9900/planner?v=c"}]},
                      ]}}


def _done(**extra):
    card = {"type": "done", "title": "Badge as the start button",
            "headline": "The empty badge on the planner is now the start button.",
            "what_was_done": "Made the empty badge the start button.",
            "answer": "The empty badge is the start button now.", "full_answer": "## Changed\n- Badge."}
    card.update(extra)
    return card


def _facts(said, prev=None):
    return E.TurnFacts(last_user=said, prev_card=prev if prev is not None else _decision())


@pytest.mark.parametrize("said", [
    "how about we make the empty badge into the start button",
    "what if the badge itself were the start button and the hint sat under it",
    "we could try turning the badge into the start button",
    "these are close. how about the badge becomes the start button",
])
def test_a_direction_built_as_one_version_is_sent_back(said):
    out = E.direction_variants_problems(_done(), _facts(said))
    assert out and "3 or 4 variants" in out[0] and "One version because" in out[0]
    assert any("direction of their own" in p for p in E.problems(_done(), _facts(said)))


@pytest.mark.parametrize("said", [
    "B",                                         # a pick by key
    "dotted badge with hint",                    # a pick by label
    "go with the dotted one",                    # a pick
    "your call",                                 # handed over
    "never mind, forget that choice",            # dropped
    "ok build it",                               # go-ahead
    'make the badge say "Start here"',           # spelled out
    "why have the hint in addition to the badge, remove it",   # removal
    "use the same outline as the header badge",                # reuse
    "Flagged summary: your last Done card was off.",
])
def test_a_pick_a_handover_or_a_result_spelled_out_passes(said):
    assert E.direction_variants_problems(_done(), _facts(said)) == []


def test_variants_of_the_direction_pass():
    card = {"type": "needs_you", "title": "Badge as button", "headline": "Pick a start-button badge.",
            "needs": {"kind": "decision", "replies": []}}
    assert E.direction_variants_problems(card, _facts("how about the badge is the start button")) == []


def test_one_version_because_passes():
    card = _done(full_answer="One version because the user described the exact button, label and place.")
    assert E.direction_variants_problems(card, _facts("how about the badge is the start button")) == []


def test_unrelated_work_after_variants_passes():
    card = _done(title="Login timeout", headline="Sign-in no longer times out after a minute.",
                 what_was_done="Raised the sign-in timeout.")
    assert E.direction_variants_problems(card, _facts("how about we fix the login timeout first")) == []


def test_a_choice_without_design_variants_passes():
    prev = {"type": "needs_you", "needs": {"kind": "decision", "ask": "Which badge storage",
                                           "replies": [{"label": "Badge in a file"}, {"label": "Badge in a table"}]}}
    assert E.direction_variants_problems(_done(), _facts("how about the badge is the start button", prev)) == []


def test_an_automated_wake_passes():
    facts = _facts("how about the badge is the start button")
    facts.woken_by = "Background job finished"
    assert E.direction_variants_problems(_done(), facts) == []


def test_the_guide_says_a_direction_gets_variants():
    assert "direction of the user's own" in R.INSTRUCTIONS


def test_then_your_pick_is_a_wait_on_the_user():
    assert R._WAITS_ON_USER.search("Then your pick of how the planner starts.")
    assert not R._WAITS_ON_USER.search("Your pick from before is in place.")
