"""One table of split-view templates on each side, held together.

A decision may carry a split-view template instead of needs.replies (a form of
several questions, before-and-after pairs).  The server's table is
``SPLIT_TEMPLATES`` in src/engine/handoff_rules.py; the page's is ``_SPLIT`` in
src/web/static/js/last-card.js.  Both must name the same templates, with the same
answer prefix and the same smallest number of items, or a card one side accepts
the other refuses, and an answer one side reads as given the other does not.
"""
import re

import pytest
from pathlib import Path

from engine import handoff_evidence as ev
from engine import handoff_rules as hr
from engine import smart_termination as st

JS = (Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "js" / "last-card.js").read_text(encoding="utf-8")


def _js_table():
    body = JS[JS.index("const _SPLIT = {"):JS.index("function _splitOf(")]
    return {m.group(1): {"prefix": m.group(2), "min": int(m.group(3))}
            for m in re.finditer(r"^    (\w+): \{\n      prefix: '([^']+)', min: (\d+),", body, re.M)}


def test_both_tables_name_the_same_templates_prefixes_and_sizes():
    js = _js_table()
    assert set(js) == set(hr.SPLIT_TEMPLATES) == {"form", "pairs"}
    for k, t in hr.SPLIT_TEMPLATES.items():
        assert js[k] == {"prefix": t["prefix"], "min": t["min"]}, k


def test_every_template_has_a_check_and_is_outside_the_word_count():
    for k in hr.SPLIT_TEMPLATES:
        assert k in hr._SPLIT_CHECKS
        assert hr._POPUP_ONLY.match(f"needs.{k}[0].x")


def test_sessions_are_told_each_templates_field_and_prefix():
    for k, t in hr.SPLIT_TEMPLATES.items():
        assert f"needs.{k}" in st.INSTRUCTIONS and t["prefix"] in st.INSTRUCTIONS


def test_two_templates_on_one_card_are_sent_back():
    card = {"needs": {"kind": "decision", "form": [], "pairs": []}}
    assert any("leave needs.pairs" in p for p in hr.split_problems(card))


def test_each_templates_answer_closes_only_its_own_kind_of_card():
    for k, t in hr.SPLIT_TEMPLATES.items():
        other = next(o for o in hr.SPLIT_TEMPLATES if o != k)
        card = {"needs": {"kind": "decision", k: [{"id": "x"}]}}
        assert ev.answers_choice(card, t["prefix"] + " 1 item:")
        assert not ev.answers_choice(card, hr.SPLIT_TEMPLATES[other]["prefix"] + " 1 item:")


# ── Discoverable where a session picks its card ──────────────────────────

from engine import handoff_check as hc


def test_the_card_guide_routes_to_every_template():
    for k, t in hr.SPLIT_TEMPLATES.items():
        assert f"needs.{k} instead of needs.replies (start it with --start {k})" in st.INSTRUCTIONS
    assert "{split" not in st.INSTRUCTIONS and "{split" not in hr.INSTRUCTIONS


def test_the_instructions_describe_each_template_from_the_table():
    for k, t in hr.SPLIT_TEMPLATES.items():
        assert f"When {t['when']}: ONE decision with needs.{k}" in st.INSTRUCTIONS
        assert t["rules"] in st.INSTRUCTIONS


def test_a_decisions_blank_points_at_every_template():
    text = hc.start_text("needs_you")
    for k, t in hr.SPLIT_TEMPLATES.items():
        assert f"If {t['when']}, use --start {k} instead." in text


def _fill(v, key=""):
    """Fill a blank the way a session would: every placeholder becomes real words."""
    if isinstance(v, dict):
        return {k: _fill(x, k) for k, x in v.items()}
    if isinstance(v, list):
        return [_fill(x, key) for x in v]
    if isinstance(v, str) and v.startswith("<"):
        if key == "url":
            return "/static/sample.png"
        if key == "id":
            return "sample-" + "".join(c for c in v if c.isdigit())
        return f"sample words for the {key} field here"
    return v


def test_each_templates_blank_has_its_items_and_once_filled_passes():
    for k, t in hr.SPLIT_TEMPLATES.items():
        card = hc.skeleton(k)
        assert card["type"] == "needs_you" and card["needs"]["kind"] == "decision"
        items = card["needs"][k]
        assert len(items) >= t["min"] and "replies" not in card["needs"]
        filled = _fill(card)
        # the two options' verdicts are worded apart, as a session writes them
        for q in filled["needs"].get("form", []):
            for o in q["options"]:
                o["verdict"] += " " + o["key"]
        assert hr.split_problems(filled) == [], k
        assert hc.start_text(k).count(f'"{k}": [') == 1


def test_an_unknown_kind_names_the_templates_too():
    with pytest.raises(ValueError) as e:
        hc.skeleton("nope")
    assert all(k in str(e.value) for k in hr.SPLIT_TEMPLATES)
