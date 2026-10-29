"""Cards are written from the reviewer's seat (the owner, 2026-10-28).

The card that started this: title "Card rules load live", headline "VibeNode's
Session Engine was checking cards with stale rules; fixed, live after one
engine restart", big picture "Make VibeNode's handoff cards clean to read".
It answered his question "how did you sneak a long and small title through
there" with the name of the fix, machinery words, and a big picture that had
drifted from the session's thread.  "We're still falling into AI centric
reporting from the implementer rather than contextual to the reviewer."
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


SESSION_BIG = "Making decisions in VibeNode feel premium"
QUESTION = "how did you sneak a long and small title through there"

EARLIER = {"type": "done", "title": "Decide popup motion",
           "headline": "VibeNode's Decide popup now grows out of its button", "context": "x",
           "zoom": {"big": SESSION_BIG, "task": "Build the motion you picked", "now": "Built and tested"}}

BAD = {"type": "needs_you", "title": "Card rules load live",
       "headline": "VibeNode's Session Engine was checking cards with stale rules; fixed, live after one engine restart",
       "context": "x",
       "zoom": {"big": "Make VibeNode's handoff cards clean to read", "task": "Stop rule changes from silently not applying",
                "now": "Engine now reloads card rules on change; tested"},
       "needs": {"kind": "action", "ask": "Restart VibeNode's Session Engine once so it loads the live card-rule loader."}}

GOOD = {"type": "needs_you", "title": "Why my card skipped the title",
        "headline": "My VibeNode card got through without a title because the checker ran yesterday's rules; one restart fixes it",
        "answer": "My instructions never asked for a title, and the checker was running an old copy of the rules.",
        "context": "x",
        "zoom": {"big": SESSION_BIG, "task": "You asked why my card had a long, small title",
                 "now": "Found the gap and closed it; one restart applies it"},
        "needs": {"kind": "action", "ask": "Restart VibeNode's Session Engine once so new card rules reach every session."}}


def _facts(*extra):
    entries = [E("user", "Make the decide popup transitions premium"), E("asst", _msg(EARLIER)),
               E("user", QUESTION)] + list(extra)
    return ev.facts_from_entries(entries)


def test_machinery_words_where_he_looks_first_are_sent_back():
    out = " ".join(rules.machinery_problems(BAD))
    for word in ('"stale" (headline)', '"engine" (headline)', '"Engine" (zoom.now)', '"reloads" (zoom.now)',
                 '"loader" (needs.ask)'):
        assert word in out
    assert rules.machinery_problems(GOOD) == []


def test_the_session_engine_may_be_named_in_an_ask_but_not_the_headline():
    card = dict(GOOD, headline="VibeNode's Session Engine restarted")
    assert '"engine" (headline)' in " ".join(rules.machinery_problems(card))
    assert rules.machinery_problems(dict(GOOD, needs={"kind": "action", "ask": "Restart VibeNode's Session Engine"})) == []


def test_the_cards_field_names_are_not_his_words():
    # 2026-10-28: "Zoom lines capped at 40 characters" left the owner unsure
    # what problem was solved; on screen the lines read Big picture / This task / Just now.
    card = dict(GOOD, headline="VibeNode cards' three zoom lines are now capped at 40 characters")
    assert '"zoom lines" (headline)' in " ".join(rules.machinery_problems(card))
    card = dict(GOOD, headline="The Big picture lines under VibeNode card titles no longer wrap on the phone")
    assert rules.machinery_problems(card) == []


def test_facts_carry_the_last_big_picture_and_his_latest_message():
    f = _facts(E("asst", _msg(BAD)))
    assert f.prev_big == SESSION_BIG          # the card being checked is not "previous"
    assert f.last_user == QUESTION


def test_a_sent_back_card_does_not_become_the_big_picture():
    f = _facts(E("asst", _msg(BAD)), E("sent_back", "Smart termination: your handoff card was sent back."),
               E("asst", _msg(GOOD)))
    assert f.prev_big == SESSION_BIG
    f = _facts(E("asst", _msg(BAD)), E("user", "Stop hook feedback: sent back"), E("asst", _msg(GOOD)))
    assert f.prev_big == SESSION_BIG


def test_the_bad_card_is_sent_back_for_all_four_reasons():
    out = " ".join(ev.reviewer_problems(BAD, _facts()))
    assert '"zoom.big" changed from' in out
    assert "The user asked a question" in out
    assert '"title" ("Card rules load live") and "path" share no word' in out
    reason = rules.review(_msg(BAD), _facts())
    assert reason and "implementer's seat" in reason and '"zoom.big" changed' in reason


def test_the_reviewer_first_card_passes_these_checks():
    assert ev.reviewer_problems(GOOD, _facts()) == []
    assert rules.machinery_problems(GOOD) == []


def test_a_new_topic_from_the_user_may_change_the_big_picture():
    f = ev.facts_from_entries([E("user", "Make the decide popup transitions premium"), E("asst", _msg(EARLIER)),
                               E("user", "ok another issue: the voice input drops words on my phone")])
    card = dict(GOOD, title="Voice input drops words", answer="",
                zoom=dict(GOOD["zoom"], big="Voice input that keeps every word on the phone"))
    assert not any("zoom.big" in p for p in ev.reviewer_problems(card, f))


def test_shortening_the_big_picture_is_not_a_new_topic():
    # The zoom cap dropped to 40 characters (2026-10-28): a session whose last
    # line was longer cuts it down to its own words, and must not be sent back.
    f = ev.facts_from_entries([E("user", "Make the decide popup transitions premium"), E("asst", _msg(EARLIER)),
                               E("user", "now tidy the popup's close button")])
    assert f.prev_big == SESSION_BIG
    card = dict(GOOD, zoom=dict(GOOD["zoom"], big="VibeNode decisions"))
    assert not any("zoom.big" in p for p in ev.reviewer_problems(card, f))
    drifted = dict(GOOD, zoom=dict(GOOD["zoom"], big="Billing export for the accountant"))
    assert any("zoom.big" in p for p in ev.reviewer_problems(drifted, f))


def test_a_polite_request_is_not_a_question():
    assert ev.is_question(QUESTION) and ev.is_question("why is it slow?")
    assert not ev.is_question("can you fix the sidebar?") and not ev.is_question("please rename it")
    assert not ev.is_question("ok another issue. like you came back with this")


def test_sessions_are_told():
    assert "reviewer's seat" in rules.INSTRUCTIONS and '"answer"' in rules.INSTRUCTIONS
    from daemon import smart_termination as st
    assert "keep it the same on every card" in st.INSTRUCTIONS


def test_a_browser_reload_is_the_users_word():
    for h in ("reload the tab once to load it", "Your tab needs one last reload", "card changes appear without a reload"):
        assert rules.machinery_problems({"headline": h}) == [], h
    assert rules.machinery_problems({"headline": "VibeNode now reloads card rules on change"})


def test_an_answer_is_never_a_decide_card():
    """The owner, 2026-10-28: "i just got a decide on a question?"  He asked
    "so how do we reject the implementer seat" and the answer came back as a
    Decide card asking whether to add a new check."""
    asked = ev.facts_from_entries([E("user", "Make the decide popup transitions premium"), E("asst", _msg(EARLIER)),
                                   E("user", "wait so how do we reject the implementer seat")])
    decide = dict(GOOD, answer="Four checks send the card back.", type="needs_you",
                  needs={"kind": "decision", "ask": "Add an AI reader check to VibeNode cards?",
                         "replies": [{"key": "A", "label": "Add the reader check"}, {"key": "B", "label": "Keep the word checks"}]})
    assert any("answers it with a Decide card" in p for p in ev.reviewer_problems(decide, asked))
    explain = dict(decide, type="explain", needs={"kind": "none", "why": "x", "next": "y"})
    assert not any("answers it with a Decide card" in p for p in ev.reviewer_problems(explain, asked))
    # a decision after an order, not a question, is not this rule's business
    ordered = ev.facts_from_entries([E("user", "build the default templates for the popup")])
    assert not any("answers it with a Decide card" in p for p in ev.reviewer_problems(decide, ordered))


def test_spoken_lead_ins_before_a_question():
    assert ev.is_question("wait so how do we reject the implementer seat")
    assert ev.is_question("ok so right there its confusing. i just got a decide on a question?")
    assert not ev.is_question("do the templates") and not ev.is_question("I restarted the Session Engine")


def test_a_request_quoting_a_question_is_a_request():
    # The owner, 2026-10-28: his order quoted the Decide popup's question, the
    # "?" made it read as a question, and he got screenshots, not a Decide card.
    msg = ("good.\n\nid like to see some variants of\n\nWhich preview should deliverable cards show?\n\n"
           "A\nTall preview above the Open row\n220px, corner expand icon.\nWeb\nPhone\nBoth\n\n"
           "ideally put all of it into that topnav so i can distinguide the wrapper from what im revieiwng, "
           "and web need not be so small")
    assert not ev.is_question(msg)
    assert not ev.is_question("show me what does the sidebar look like?")
    assert ev.is_question("i want to know why it was slow?")
    assert ev.is_question("why the fuck did you send me variants as screens")


def test_a_decide_card_must_come_from_something_he_asked_for():
    """"why did you think that was the right choice?" (2026-10-28): the card
    offered a new check nobody asked for as a Decide card."""
    f = ev.facts_from_entries([E("user", "build default templates for the decide popup"), E("asst", _msg(EARLIER)),
                               E("user", "make sure the reviewer seat gets fixed systemically")])
    offer = dict(GOOD, type="needs_you", asks=[{"ask": "fix it systemically", "state": "done"}],
                 needs={"kind": "decision", "ask": "Add an AI reader check to VibeNode cards?",
                        "replies": [{"key": "A", "label": "Add the reader check"}, {"key": "B", "label": "Keep the word checks"}]})
    assert any("not tied to anything the user asked for" in p for p in ev.reviewer_problems(offer, f))
    blocking = dict(offer, asks=[{"ask": "default templates for the decide popup", "state": "needs_you"}])
    assert not any("not tied" in p for p in ev.reviewer_problems(blocking, f))


def test_sessions_get_a_guide_for_which_card_to_end_on():
    t = rules.INSTRUCTIONS
    assert "Which card to end on" in t and "Never a Decide card" in t and "never a \\\ndecision" not in t
    assert "is never a decision" in " ".join(t.split())


def test_a_title_must_say_which_cards():
    # 2026-10-28: "it takes me a moment when reading your title to know what cards is".
    bare = " ".join(rules.format_problems(dict(GOOD, title="Cards say the problem first")))
    assert 'says "Cards" without saying which' in bare
    for ok in ("Summaries now in your words", "Sidebar cards fit two lines", "End-of-turn cards in plain words"):
        assert "without saying which" not in " ".join(rules.format_problems(dict(GOOD, title=ok)))


# ── Variants on screen always get a pick, even after a question (owner,
#    2026-10-28: "screens with no next cta makes zero fucking sense ... i
#    blame smart termination"). ──

import pytest
from daemon import handoff_evidence as _he, handoff_rules as _hr

_ASK = "the next should give CTAs: iterate, accept and return to unfinished work"


@pytest.fixture(autouse=False)
def scratch(tmp_path, monkeypatch):
    """static/scratch is gitignored, so the tests bring their own pages."""
    (tmp_path / "card-back-to.html").write_text(
        '<section data-option="now"></section><script>const O=[{key: \'A\'},{key: \'B\'},{key: \'C\'}];'
        'O.map(o => \'<section data-option="\' + o.key + \'">\')</script>')
    (tmp_path / "deliverable-formats.html").write_text("<div id=live-log></div>")
    # A demo page whose script holds a sample card's reply keys is not a set of variants.
    (tmp_path / "standby-demo.html").write_text(
        '<div class="side" data-option="sidebar"></div><script>const C={replies:[{key: \'A\'},{key: \'B\'}]};</script>')
    monkeypatch.setattr(_hr, "_SCRATCH_DIR", tmp_path)
    return tmp_path


def _variant_card(kind):
    look = {"label": "Back-to button mockups", "kind": "link", "url": "/static/scratch/card-back-to.html"}
    needs = {"kind": kind, "ask": "Which back-to button?", "why": "A new control.", "next": "I build it.", "look": [look]}
    if kind == "decision":
        needs["replies"] = [{"key": k, "label": "Back-to option " + k + " here", "reply": k,
                             "screens": [{"label": "Back-to option " + k, "url": "/static/scratch/card-back-to.html#" + k, "device": "both"}]}
                            for k in "ABC"]
    else:
        needs = {"kind": "none", "why": "Mockups only.", "next": "Name one."}
    return {"type": "needs_you" if kind == "decision" else "explain", "title": "Why no mockups shown",
            "headline": "VibeNode back-to button mockups.", "answer": "I linked them without showing them.",
            "links": [look, {"label": "Back-to option A", "kind": "image", "url": "/static/scratch/card-back-to-A.png"}],
            "needs": needs, "asks": [{"ask": _ASK, "state": "needs_you" if kind == "decision" else "answered"}]}


def _vfacts():
    return _he.TurnFacts(last_user="why did you not show me the mockups here, another failure",
                         history=[_ASK, "why did you not show me the mockups here, another failure"])


def test_variants_shown_without_a_pick_are_sent_back(scratch):
    probs = _hr.format_problems(_variant_card("none"))
    assert any("gives no way to pick" in p for p in probs)


def test_a_pick_between_variants_may_answer_a_question(scratch):
    c = _variant_card("decision")
    assert not any("gives no way to pick" in p for p in _hr.format_problems(c))
    assert not any("answers it with a Decide card" in p for p in _he.reviewer_problems(c, _vfacts()))


def test_a_pick_answering_a_question_must_mark_its_recommendation(scratch):
    # "Which do you prefer and why" got an answer, but the choices still read
    # as an unmarked A or B (2026-10-29).
    c = _variant_card("decision")
    probs = _he.reviewer_problems(c, _vfacts())
    assert any("wants your pick" in p for p in probs)
    c["needs"]["replies"][1]["recommended"] = True
    assert not any("wants your pick" in p for p in _he.reviewer_problems(c, _vfacts()))
    c["needs"]["replies"][2]["recommended"] = True
    assert any("wants your pick" in p for p in _he.reviewer_problems(c, _vfacts()))


def test_other_decisions_after_a_question_are_still_sent_back(scratch):
    c = _variant_card("decision")
    c["needs"]["look"] = [{"label": "x", "kind": "link", "url": "/static/js/app.js"}]
    c["links"] = [{"label": "x", "kind": "link", "url": "/static/js/app.js"}]
    assert any("answers it with a Decide card" in p for p in _he.reviewer_problems(c, _vfacts()))


def test_a_demo_page_without_variants_is_not_a_choice(scratch):
    c = {"type": "explain", "title": "t", "headline": "h", "answer": "a",
         "links": [{"label": "Demo", "kind": "link", "url": "/static/scratch/deliverable-formats.html"}],
         "needs": {"kind": "none", "why": "w", "next": "n"}}
    assert not any("gives no way to pick" in p for p in _hr.format_problems(c))


def test_an_answer_may_carry_a_yes_on_work_he_asked_for():
    """The owner, 2026-10-28: an answer whose next step waited on him came back
    as "Nothing needed ... once you confirm" with no button, because an answer
    could not be anything but explain.  An approve tied to his request, with
    the answer first, is allowed; a bare approve or one tied to nothing is not."""
    asked = ev.facts_from_entries([E("user", "add an I'm lost button to the cards"), E("asst", _msg(EARLIER)),
                                   E("user", "i like b but is it always green?")])
    yes = dict(GOOD, answer="No, it took the card's status colour; it is grey now.",
               asks=[{"ask": "add an I'm lost button to the cards", "state": "needs_you"}],
               needs={"kind": "approve", "ask": "Build version B of VibeNode's Explain more in grey?",
                      "why": "x", "replies": [{"key": "Y", "label": "Build it in grey"}, {"key": "N", "label": "Keep it on the page"}]})
    assert not any("answers it with a Decide card" in p for p in ev.reviewer_problems(yes, asked))
    untied = dict(yes, asks=[])
    assert any("answers it with a Decide card" in p for p in ev.reviewer_problems(untied, asked))
    unanswered = {k: v for k, v in yes.items() if k != "answer"}
    assert any("answers it with a Decide card" in p for p in ev.reviewer_problems(unanswered, asked))
    decide = dict(yes, needs=dict(yes["needs"], kind="decision"))
    assert any("answers it with a Decide card" in p for p in ev.reviewer_problems(decide, asked))


def test_variants_built_as_list_rows_are_found(scratch):
    """The owner, 2026-10-28 ("it keeps happening to me"): a page that builds
    its variants from rows like ['A', 'A · One top bar', '...'] was not read as
    variants, so a Decide card for it after a quoted question was refused and
    an answer card of screenshots with no way to pick got through."""
    (scratch / "rows.html").write_text(
        "<script>const V=[['now','Now','x'],['A','A · One bar','y'],['B','B · Two tiers','z']];"
        "V.map(([k]) => '<section data-option=\"' + k + '\">')</script>")
    assert _hr._variant_keys("/static/scratch/rows.html") == ["A", "B"]
    shots = {"type": "explain", "title": "t", "headline": "h", "answer": "a",
             "links": [{"label": "Try", "kind": "link", "url": "/static/scratch/rows.html"},
                       {"label": "A", "kind": "image", "url": "/static/scratch/rows-A.png"}],
             "needs": {"kind": "none", "why": "w", "next": "n"}}
    assert any("gives no way to pick" in p for p in _hr.format_problems(shots))


def test_a_demo_pages_sample_data_is_not_variants(scratch):
    """2026-10-28: the Standby demo's sample card (reply keys A, B) and one
    tagged panel were read as variants, and a done card was sent back."""
    assert _hr._variant_keys("/static/scratch/standby-demo.html") == ["sidebar"]   # one tag is not a choice
    assert _hr._variant_keys("/static/scratch/card-back-to.html") == ["A", "B", "C"]
    c = {"type": "needs_you", "links": [{"label": "Demo", "url": "/static/scratch/standby-demo.html"}], "needs": {"kind": "approve"}}
    assert _hr.shows_variants(c) == [] and _hr.variants_pick_problem(c) is None


# ── A choice named by its key (owner, 2026-10-28) ─────────────────────────
# "got a dogshit title talking by variant name, should be illegal", on a done
# card titled "Header A, more frustrated" with zoom.now "Header A drawn in full".

def test_a_variant_named_by_its_key_is_sent_back():
    card = {"type": "done", "title": "Header A, more frustrated",
            "headline": "The article header now shows a bigger, overwhelmed person",
            "zoom": {"big": "LinkedIn series", "task": "Draft the next article", "now": "Header A drawn in full"}}
    out = " ".join(rules.variant_name_problems(card))
    assert '"Header A" (title)' in out and '"Header A" (zoom.now)' in out
    assert "Names a choice by its letter or number" in rules.review(_msg(card))


def test_every_shape_of_a_key_where_he_skims():
    for line in ["Layout 4 in the sidebar", "Built your pick B", "So A and B look different",
                 "D without the big picture", "Locked in E", "Went with design A",
                 "option 2 is live", "B or C for the header"]:
        assert rules.variant_name_problems({"title": line}), line
    # an explanation after the key does not excuse it in these lines
    assert rules.variant_name_problems({"headline": "You picked option C (the darker header)"})


def test_plain_words_and_idioms_pass():
    for line in ["A clearer, more frustrated header", "A Bigger Person At The Desk",
                 "Plan B for the release", "Vitamin C reminder", "Type A pages load", "Series A deck",
                 "Fix the A/B test page", "C++ build fixed", "E-mail draft ready", "Opus 5.5 on every session",
                 "Draft 16 is ready", "Step 3 of the release", "Python 3 only", "Header: a more frustrated person"]:
        assert rules.variant_name_problems({"title": line, "headline": line}) == [], line


def test_reply_labels_and_context_may_still_use_keys():
    card = {"type": "needs_you", "title": "Pick the article's header",
            "headline": "Pick a header for the LinkedIn article", "context": "Three sketches, A to C.",
            "needs": {"kind": "decision", "ask": "Pick the header for the LinkedIn article",
                      "replies": [{"key": "A", "label": "A: overwhelmed person"}]}}
    assert rules.variant_name_problems(card) == []


# ── The title names the thing; the path is the way to it (owner, 2026-10-28) ──
# static/scratch/card-title-crumb.html: "the subtitle is the sentence ... the
# title becomes the last crumb".

def test_a_title_is_a_name_not_a_sentence():
    for bad in ["Header A, more frustrated", "Decide popup: all on top", "Pick the article's header",
                "Why Done still woke up", "Make the person in the image more frustrated"]:
        assert rules.title_path_problems({"title": bad, "path": ["Article series"]}), bad
    for good in ["Header image", "Session right-click menu", "Top bar", "Release 2.4", "Gen vs Validation Leverage"]:
        assert rules.title_path_problems({"title": good, "path": ["Article series"]}) == [], good


def test_every_card_needs_a_short_path_that_leads_to_the_title():
    out = " ".join(rules.title_path_problems({"title": "Header image"}))
    assert '"path" is missing' in out
    assert rules.title_path_problems({"title": "Header image", "path": ["Article series", "Gen vs Validation Leverage"]}) == []
    assert rules.title_path_problems({"title": "Top bar", "path": ["a", "b", "c", "d"]})          # too many steps
    assert rules.title_path_problems({"title": "Top bar", "path": ["The popup that opens when you press Decide"]})
    assert rules.title_path_problems({"title": "Top bar", "path": ["Decide popup", "Top bar"]})   # ends on the title
    assert rules.variant_name_problems({"title": "Top bar", "path": ["Layout 4"]})


def test_the_path_keeps_its_first_step_unless_he_moves_on():
    earlier = dict(EARLIER, path=["Decide popup"], title="Motion")
    entries = [E("user", "Make the decide popup transitions premium"), E("asst", _msg(earlier)),
               E("user", "now the top bar")]
    facts = ev.facts_from_entries(entries)
    assert facts.prev_path == "Decide popup"
    moved = dict(EARLIER, path=["Summary cards"], title="Top bar")
    assert any('"path" starts with "Summary cards"' in p for p in ev.reviewer_problems(moved, facts))
    kept = dict(EARLIER, path=["Decide popup"], title="Top bar")
    assert not any('"path" starts with' in p for p in ev.reviewer_problems(kept, facts))
    # his own words bring the new topic
    entries[-1] = E("user", "switch to the summary cards now")
    assert not any('"path" starts with' in p for p in ev.reviewer_problems(moved, ev.facts_from_entries(entries)))


# ── Choices are shown the way Now is (owner, 2026-10-28) ──────────────────
# "idk why im getting these shitty zoomed in dark theme variants when the now
# is the way you should be presenting that ... i also need a check on variants
# that forces consistency".

def _decide(screens_by_key, page="/static/scratch/card-top-type.html"):
    return {"type": "needs_you", "needs": {"kind": "decision", "look": [{"label": "p", "kind": "link", "url": page}],
            "replies": [{"key": k, "label": "choice " + k, "reply": k, "screens": s} for k, s in screens_by_key.items()]}}


def test_a_screenshot_choice_beside_a_live_page_is_sent_back(tmp_path, monkeypatch):
    monkeypatch.setattr(rules, "_SCRATCH_DIR", tmp_path)
    (tmp_path / "p.html").write_text('<div data-option="now"></div><div id="a" data-option="A"></div><div id="b" data-option="B"></div>')
    page = "/static/scratch/p.html"
    bad = _decide({"A": [{"label": "a", "url": "/static/scratch/p-A.png"}], "B": [{"label": "b", "url": page + "#b"}]}, page)
    assert "A is a screenshot" in rules.screens_like_now_problem(bad)
    good = _decide({"A": [{"label": "a", "url": page + "#a"}], "B": [{"label": "b", "url": page + "#b"}]}, page)
    assert rules.screens_like_now_problem(good) is None
    other = _decide({"A": [{"label": "a", "url": page + "#a"}], "B": [{"label": "b", "url": "/static/scratch/q.html#b"}]}, page)
    assert "B is on another page" in rules.screens_like_now_problem(other)
    missing = _decide({"A": [{"label": "a", "url": page + "#a"}], "B": []}, page)
    assert "B has no screen" in rules.screens_like_now_problem(missing)


def test_a_page_without_a_now_section_is_sent_back(tmp_path, monkeypatch):
    monkeypatch.setattr(rules, "_SCRATCH_DIR", tmp_path)
    (tmp_path / "p.html").write_text('<div id="a" data-option="A"></div><div id="b" data-option="B"></div>')
    page = "/static/scratch/p.html"
    card = _decide({"A": [{"label": "a", "url": page + "#a"}], "B": [{"label": "b", "url": page + "#b"}]}, page)
    assert "no Now section" in rules.screens_like_now_problem(card)


def test_live_pages_of_another_app_still_pass():
    card = {"type": "needs_you", "needs": {"kind": "decision", "replies": [
        {"key": "A", "label": "one two three", "reply": "a", "screens": [{"label": "x", "url": "http://127.0.0.1:3000/trial/a"}]}]}}
    assert rules.screens_like_now_problem(card) is None
