"""A round of variants that comes back with at most one variant new or
changed is sent back unless the card says why the same set is right.  It is
judged by what changed, not by how the user worded the reply.  All messages
and labels are made up."""

import json

import pytest

from engine import handoff_evidence as E

OLD_LABELS = ("Keep today's banner", "Wide strip", "Corner badge", "Full-width photo")


def _decision(*labels, **extra):
    card = {"type": "needs_you", "title": "Banner layout", "headline": "Pick a banner layout.",
            "needs": {"kind": "decision", "ask": "Which banner layout?",
                      "replies": [{"key": chr(65 + i), "label": l, "reply": l} for i, l in enumerate(labels)]}}
    card.update(extra)
    return card


OLD = _decision(*OLD_LABELS)


def _facts(said="whatever the user wrote", prev=OLD, **kw):
    return E.TurnFacts(last_user=said, prev_card=prev, **kw)


@pytest.mark.parametrize("labels", [
    OLD_LABELS,                                                            # nothing changed
    OLD_LABELS + ("Strip in the photo's colours",),                        # one added beside them
    ("Keep today's banner", "Wide strip", "Corner badge", "Badge, bigger"),  # one swapped
])
def test_a_round_with_at_most_one_variant_changed_is_sent_back(labels):
    card = _decision(*labels)
    out = E.refine_variants_problems(card, _facts())
    assert out and "same_set_because" in out[0]
    assert any("same_set_because" in p for p in E.problems(card, _facts()))


@pytest.mark.parametrize("said", ["i like B but bigger", "404", "what does C do?", "", "B"])
def test_the_wording_of_the_reply_does_not_matter(said):
    assert E.refine_variants_problems(_decision(*OLD_LABELS), _facts(said))


def test_two_or_more_new_variants_pass():
    fresh = _decision("Keep today's banner", "Wide strip", "Strip, bold heading", "Strip, plain heading")
    assert E.refine_variants_problems(fresh, _facts()) == []


def test_a_reason_in_the_hidden_field_passes():
    card = _decision(*OLD_LABELS, same_set_because="the page would not load for the user before")
    assert E.refine_variants_problems(card, _facts("404 page not found")) == []


@pytest.mark.parametrize("extra", [
    {"same_set_because": "because"},                                       # not a reason
    {"full_answer": "Same set because the page would not load for the user before."},  # visible text no longer counts
])
def test_a_token_or_visible_reason_does_not_pass(extra):
    assert E.refine_variants_problems(_decision(*OLD_LABELS, **extra), _facts())


def test_the_hidden_reason_is_outside_the_word_count():
    from engine import handoff_rules as R
    card = _decision(*OLD_LABELS, same_set_because="the page would not load for the user before")
    rows = [r for r in R.word_table(card) if r[0] == "same_set_because"]
    assert rows and rows[0][2] is False
    assert not any("same_set_because" in p for p in R.length_problems(card))


def test_only_decision_after_decision_is_judged():
    done = {"type": "done", "title": "Banner", "headline": "Built it."}
    assert E.refine_variants_problems(done, _facts()) == []
    assert E.refine_variants_problems(_decision(*OLD_LABELS), _facts(prev=done)) == []


def test_a_turn_woken_by_an_automated_message_is_left_alone():
    assert E.refine_variants_problems(_decision(*OLD_LABELS), _facts(woken_by="Background job")) == []


def test_facts_carry_the_previous_card():
    class Entry:
        def __init__(self, kind, text):
            self.kind, self.text = kind, text

    card_text = "```handoff\n" + json.dumps(OLD) + "\n```"
    facts = E.facts_from_entries([Entry("user", "show me banner variants"), Entry("asst", card_text),
                                  Entry("user", "i like B but bigger")])
    assert facts.prev_card and facts.prev_card.get("title") == "Banner layout"
