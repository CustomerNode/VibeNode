"""Smart termination: a session that ships its own work never ends a finished
order on an "OK to ship?" approval.

The case: the user orders a change, the session builds and checks it, and,
although it already pushed earlier work in the same session without asking,
ends on an Approve card asking whether to ship.  The user is handed a job the
session could do, and a panel to read cold.  The fix: ship it the same way and
end on a Done card with its Validate row.
"""

from __future__ import annotations

from engine import handoff_evidence as ev
from engine import handoff_rules as rules


def _approve(ask="Ship the compact sidebar on tablets?", **needs_over):
    needs = {
        "kind": "approve",
        "ask": ask,
        "why": "Same look as the phone sidebar.",
        "next": "Shipping puts it live.",
        "look": [{"label": "Tablet sidebar", "kind": "image", "url": "/static/scratch/tablet.png"}],
        "replies": [{"key": "A", "label": "Ship it", "reply": "Ship it", "recommended": True}],
    }
    needs.update(needs_over)
    return {"type": "needs_you", "title": "Sidebar on a tablet",
            "headline": "The Acme sidebar folds on tablets now; OK to ship?", "needs": needs}


def _facts(history=("make the sidebar fold on tablets too",), pushed=True):
    facts = ev.TurnFacts(history=list(history), requests=list(history))
    if pushed:
        facts.actions.append(ev.Action(
            tool="Bash", ok=True,
            detail="cd repo && git commit -qm 'Fold the sidebar' && git push origin HEAD:main",
            output="   1a2b3c4d..5e6f7a8b  HEAD -> main"))
    return facts


def test_ok_to_ship_from_a_session_that_ships_by_itself_is_sent_back():
    out = ev.ship_ask_problems(_approve(), _facts())
    assert out and "Shipping is part of the order" in out[0]
    assert out[0] in ev.problems(_approve(), _facts())


def test_a_push_seen_only_in_gits_output_counts():
    facts = _facts(pushed=False)
    facts.actions.append(ev.Action(tool="Bash", ok=True, detail="cd repo; O=$(git rev-parse x); <long command cut>",
                                   output=" 2 files changed\n   ea6c765df..2d6d84b3d  2d6d84b -> sprint/one"))
    assert ev.ship_ask_problems(_approve(), facts)


def test_a_commit_made_with_lower_level_git_counts():
    facts = _facts(pushed=False)
    facts.actions.append(ev.Action(tool="Bash", ok=True, detail="C=$(git commit-tree $T -p $O)", output="abc"))
    assert ev.ship_ask_problems(_approve("Commit the sidebar change?"), facts)


def test_a_session_that_never_shipped_may_ask():
    assert not ev.ship_ask_problems(_approve("Publish the new pricing page to the site?"), _facts(pushed=False))


def test_a_failed_push_does_not_count():
    facts = _facts(pushed=False)
    facts.actions.append(ev.Action(tool="Bash", ok=False, detail="git push origin main", output="rejected"))
    assert not ev.ship_ask_problems(_approve(), facts)


def test_when_the_user_talks_about_shipping_the_user_decides():
    facts = _facts(history=("fold the sidebar on tablets", "don't push anything until I see it"))
    assert not ev.ship_ask_problems(_approve(), facts)


def test_a_project_rule_named_in_only_you_lets_the_approval_stand():
    card = _approve(only_you="This project's release rule needs the owner's OK before every push.")
    assert not ev.ship_ask_problems(card, _facts())


def test_an_approval_answered_on_its_own_page_is_left_alone():
    card = _approve(replies=[], look=[{"label": "Release page", "url": "http://127.0.0.1:8765/"}])
    assert not ev.ship_ask_problems(card, _facts())


def test_an_approval_about_something_else_is_left_alone():
    card = _approve("Delete the 40 old preview images?")
    assert not ev.ship_ask_problems(card, _facts())


def test_a_done_card_is_left_alone():
    card = _approve()
    card["type"] = "done"
    assert not ev.ship_ask_problems(card, _facts())


def test_without_session_history_nothing_is_judged():
    assert not any("Shipping is part of the order" in p for p in ev.problems(_approve(), None))


def test_the_guide_tells_sessions_to_ship_an_order_themselves():
    assert "Shipping is part of an order" in rules.INSTRUCTIONS
    assert "needs.only_you" in rules.INSTRUCTIONS.split("Shipping is part of an order", 1)[1][:600]
