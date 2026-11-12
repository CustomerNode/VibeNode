"""A Done card that answers an open-ended matter of taste with ONE version is
sent back once to show a range of variants.  All messages are made up."""

import pytest

from engine import handoff_evidence as E
from engine import handoff_rules as R


def _done(**extra):
    card = {"type": "done", "title": "Settings page", "headline": "The settings page has a new look.",
            "answer": "Rebuilt the settings page.", "full_answer": "## Changes\n- New layout."}
    card.update(extra)
    return card


def _facts(said):
    return E.TurnFacts(last_user=said)


@pytest.mark.parametrize("said", [
    "redesign the settings page",
    "make the header look cleaner",
    "make the sidebar more modern",
    "the colors dont really match the other pages",
    "find a better word for that label",
    "these slides are way too text heavy",
    "come up with a logo for the app",
])
def test_an_open_taste_ask_answered_with_one_version_is_sent_back(said):
    out = E.one_version_problems(_done(), _facts(said))
    assert out and "3 or 4" in out[0] and "One version because" in out[0]
    # It rides the normal list of evidence problems.
    assert any("matter of taste" in p for p in E.problems(_done(), _facts(said)))


@pytest.mark.parametrize("said", [
    "fix the login bug",
    'rename the button to "Save"',
    "make the title #ff8800",
    "set the gap to 12px",
    "the silhouette looks bad, you could just merge it with the chair",
    "theres a gap above it that looks weird",
    "i like the second one, build it",
    "redesign the settings page, your call, just do it",
])
def test_spelled_out_picked_or_not_taste_passes(said):
    assert E.one_version_problems(_done(), _facts(said)) == []


def test_other_card_types_are_left_alone():
    assert E.one_version_problems(_done(type="explain"), _facts("redesign the settings page")) == []


def test_saying_why_one_version_fits_passes():
    card = _done(full_answer="One version because it must match the existing articles exactly.")
    assert E.one_version_problems(card, _facts("redesign the settings page")) == []


def test_a_card_showing_variants_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_SCRATCH_DIR", tmp_path)
    (tmp_path / "settings.html").write_text(
        '<section data-option="now"></section><section data-option="A"></section>'
        '<section data-option="B"></section><section data-option="C"></section>')
    card = _done(validate={"how": "Flip through them.", "url": "/static/scratch/settings.html"})
    assert E.one_version_problems(card, _facts("redesign the settings page")) == []
    card = _done(links=[{"label": "Variants", "kind": "link", "url": "/static/scratch/settings.html"}])
    assert E.one_version_problems(card, _facts("redesign the settings page")) == []
