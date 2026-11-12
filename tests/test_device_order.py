"""A summary never tells the user which device to pick up to check the work.

The user chooses the device and may be at the computer; a change that shows
only at phone size is shown at that size inside the app (a page framing the
live page at phone width, a phone screenshot).  Made-up cards throughout.
"""
from engine import handoff_rules as R


def _card(**over):
    card = {
        "type": "done",
        "title": "Compact header",
        "headline": "The Acme header now folds into one row on small screens.",
        "answer": "Every page header folds on narrow screens; wide screens are unchanged.",
        "validate": {"how": "Press the folded button in the narrow frame", "url": "/static/scratch/x.html"},
        "needs": {"kind": "none", "why": "It is live.", "next": "Pick the icons"},
    }
    for k, v in over.items():
        if k in ("how",):
            card["validate"] = dict(card["validate"], how=v)
        elif k in ("ask", "next", "why"):
            card["needs"] = dict(card["needs"], **{k: v})
        else:
            card[k] = v
    return card


def test_validate_that_sends_the_user_to_a_phone_is_sent_back():
    p = R.device_order_problems(_card(how="Open the orders page on your phone and tap Menu"))
    assert p and "device" in p[0]


def test_any_device_and_any_article_in_validate():
    for how in ("On a laptop, scroll the report down",
                "Refresh the page on your computer",
                "Open the demo on your iPhone and flip Dark",
                "Try it on a tablet"):
        assert R.device_order_problems(_card(how=how)), how


def test_an_order_in_the_short_answer_is_sent_back():
    p = R.device_order_problems(_card(answer="Done. Open the build on your phone, or look at the screenshots."))
    assert p and "(answer)" in p[0]


def test_describing_what_changed_on_a_size_passes():
    card = _card(headline="Every phone toolbar now folds into one button.",
                 answer="On a phone the toolbar folds; the desktop is unchanged.",
                 ask="How should the toolbar fold on a phone?",
                 next="I check the live site on a phone after publishing.",
                 why="On a phone nothing changes because the menu is a sheet.")
    assert R.device_order_problems(card) == []


def test_showing_it_in_a_phone_frame_passes():
    assert R.device_order_problems(_card(how="Press Menu in each of the phone-width frames")) == []


def test_a_table_cell_is_not_a_device():
    assert R.device_order_problems(_card(how="Click a dropdown cell in the table and scroll")) == []


def test_something_only_a_handheld_device_does_passes():
    for how in ("Scan the QR code with your phone",
                "Enter the sign-in code sent to your phone",
                "Dictate a sentence on your phone's microphone"):
        assert R.device_order_problems(_card(how=how)) == [], how


def test_review_sends_the_card_back():
    import json
    card = _card(how="Open the orders page on your phone")
    msg = "```handoff\n" + json.dumps(card) + "\n```"
    reason = R.review(msg)
    assert reason and "which device" in reason


def test_the_guidance_says_so():
    assert "Never tell the user which device to use" in R.INSTRUCTIONS
