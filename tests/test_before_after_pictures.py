"""Pictures of a fix to how something looks show the old state and the new,
each labelled.  A card that says it fixed a menu and shows two screenshots
named only after the place leaves the reader unable to tell the problem from
the fix (before_after_problem in engine/handoff_rules.py)."""
import copy

from engine import handoff_rules as R


def _card(**over):
    card = {
        "type": "done",
        "path": ["Garden planner"],
        "title": "Seed list on a tablet",
        "headline": "The seed list's sort menu no longer runs off the side of a tablet screen.",
        "answer": "The sort menu opens inside the screen now, with rows big enough to tap.",
        "links": [
            {"label": "Sort menu, seed list", "kind": "image", "url": "/static/scratch/seed_sort.png"},
            {"label": "Sort menu, dark", "kind": "image", "url": "/static/scratch/seed_sort_dark.png"},
        ],
        "needs": {"kind": "none", "why": "Finished.", "next": "Nothing."},
    }
    card.update(over)
    return card


def test_place_only_labels_on_a_fixed_look_are_sent_back():
    p = R.before_after_problem(_card())
    assert p and "Before:" in p and "After:" in p


def test_only_the_new_state_is_sent_back_too():
    c = _card(links=[{"label": "After: sort menu", "kind": "image", "url": "/static/scratch/a.png"}])
    p = R.before_after_problem(c)
    assert p and "only the new state" in p


def test_before_and_after_pass():
    c = _card(links=[
        {"label": "Before: menu cut off at the edge", "kind": "image", "url": "/static/scratch/b.png"},
        {"label": "After: menu inside the screen", "kind": "image", "url": "/static/scratch/a.png"},
    ])
    assert R.before_after_problem(c) is None


def test_an_approval_with_pictures_in_look_is_held_to_it():
    c = _card(type="needs_you", links=[], headline="The planner's toolbar menus are fixed on tablets; OK to publish?",
              needs={"kind": "approve", "ask": "Publish the toolbar menus?", "why": "x", "next": "y",
                     "look": [{"label": "Toolbar menu, tablet", "kind": "image", "url": "/static/scratch/t.png"}],
                     "replies": [{"key": "A", "label": "Publish", "reply": "Publish", "recommended": True}]})
    assert R.before_after_problem(c)


def test_a_new_addition_is_not_a_fix():
    c = _card(headline="The seed list now has a sort menu with three orders.", answer="Added a sort menu.")
    assert R.before_after_problem(c) is None


def test_a_change_meant_to_look_identical_passes():
    c = _card(headline="The sort menu is now drawn by the shared menu part instead of its own copy.",
              answer="Before and after are pixel-identical.")
    assert R.before_after_problem(c) is None


def test_a_motion_fix_passes():
    c = _card(headline="The sort menu no longer flashes when the list reloads.")
    assert R.before_after_problem(c) is None


def test_an_answer_card_is_not_held_to_it():
    assert R.before_after_problem(_card(type="explain")) is None


def test_a_side_by_side_pair_review_passes():
    c = _card(type="needs_you", needs={"kind": "decision", "ask": "Same?", "pairs": [{"id": "p"}]})
    assert R.before_after_problem(c) is None


def test_no_pictures_no_problem():
    assert R.before_after_problem(_card(links=[])) is None


def test_the_stop_reports_it():
    assert any("before and which is after" in p for p in R.format_problems(_card()))


def test_sessions_are_told_both_rules():
    assert '"Before: ..."' in R.INSTRUCTIONS
    assert "The redo stays on the same task" in R.INSTRUCTIONS


def test_the_check_does_not_change_the_card():
    c = _card()
    before = copy.deepcopy(c)
    R.before_after_problem(c)
    assert c == before
