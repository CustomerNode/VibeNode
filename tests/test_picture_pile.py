"""A pile of screenshots is not a review (src/engine/handoff_rules.py,
picture_pile_problem).

A card that hands the user many loose pictures (Before / After for several
places, each its own image row) and no page to open them on makes the user
pair them up and judge thumbnails one at a time.  The card must instead
give ONE page laying them out the way they are judged, opened from its main
row, with at most two screenshots of it.  The names, pages and pictures
below are made up.
"""

import copy

from engine import handoff_rules as hr


def _pic(label, name):
    return {"label": label, "kind": "image", "url": f"/static/scratch/{name}.png"}


PILE_APPROVE = {
    "type": "needs_you",
    "path": ["Garden planner"], "title": "Seed tray menus",
    "headline": "Seed tray menus on a tablet, before and after: popovers became panels.",
    "needs": {
        "kind": "approve",
        "ask": "Ship the new seed tray menus?",
        "why": "They are built and tested.",
        "next": "Then they ship.",
        "look": [_pic("Before: tray page", "b1"), _pic("After: tray page", "a1"),
                 _pic("Before: bed page", "b2"), _pic("After: bed page", "a2"),
                 _pic("Before: plot list", "b3"), _pic("After: plot list", "a3")],
        "replies": [{"key": "A", "label": "Ship them", "reply": "Ship them", "recommended": True},
                    {"key": "B", "label": "Hold them", "reply": "Hold them"}],
    },
}


def test_six_loose_before_after_pictures_on_an_approve_are_sent_back():
    p = hr.picture_pile_problem(PILE_APPROVE)
    assert p and "6 loose screenshots" in p and "ONE review page" in p
    assert p in hr.format_problems(PILE_APPROVE)


def test_one_review_page_with_two_screenshots_passes():
    card = copy.deepcopy(PILE_APPROVE)
    card["needs"]["look"] = [
        {"label": "Before and after, every place", "kind": "link",
         "url": "/static/scratch/seed-tray-review.html"},
        _pic("Review page, top", "r1"), _pic("Review page, phone", "r2")]
    assert hr.picture_pile_problem(card) is None


def test_up_to_two_loose_pictures_need_no_page():
    card = copy.deepcopy(PILE_APPROVE)
    card["needs"]["look"] = card["needs"]["look"][:2]
    assert hr.picture_pile_problem(card) is None


def test_three_loose_pictures_on_an_answer_need_a_page():
    card = {"type": "explain", "title": "Pond pump panels", "path": ["Garden planner"],
            "headline": "How the pump panels look today", "answer": "Three panels, one per pump.",
            "links": [_pic("Pump one", "p1"), _pic("Pump two", "p2"), _pic("Pump three", "p3")]}
    assert "3 loose screenshots" in hr.picture_pile_problem(card)


def test_a_done_card_may_show_four_beside_its_page_but_not_more():
    card = {"type": "done", "title": "Compost timer", "path": ["Garden planner"],
            "headline": "The compost timer fits a phone",
            "validate": {"how": "Open the timer at phone width", "url": "/static/scratch/compost-demo.html"},
            "links": [_pic(f"Step {i}", f"s{i}") for i in range(4)]}
    assert hr.picture_pile_problem(card) is None
    card["links"].append(_pic("Step 5", "s5"))
    p = hr.picture_pile_problem(card)
    assert p and "more than the 4" in p


def test_a_file_to_read_is_not_a_page_to_review_on():
    card = copy.deepcopy(PILE_APPROVE)
    card["needs"]["look"] = [{"label": "Notes", "kind": "file", "url": "/static/scratch/notes.md"}] \
        + card["needs"]["look"][:3]
    assert hr.picture_pile_problem(card) is not None


def test_a_pairs_review_is_the_page():
    card = copy.deepcopy(PILE_APPROVE)
    card["needs"]["pairs"] = [{"id": "t", "title": "Tray page",
                               "a": _pic("Before", "b1"), "b": _pic("After", "a1")}]
    assert hr.picture_pile_problem(card) is None


def test_the_guidance_no_longer_asks_for_a_screenshot_of_each_screen():
    assert "screenshot of each screen" not in hr.INSTRUCTIONS
    assert "Never a pile of pictures" in hr.INSTRUCTIONS
    approve = {"type": "needs_you", "title": "Seed tray menus", "headline": "Seed tray menus are new",
               "needs": {"kind": "approve", "ask": "Ship the new seed tray menus?",
                         "replies": [{"label": "Ship", "reply": "Ship"}]}}
    msg = hr.approve_look_problem(approve)
    assert msg and "screenshot of each screen" not in msg and "ONE page" in msg
