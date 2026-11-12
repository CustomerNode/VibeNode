"""A deliverable's body is the text itself, never a note saying where it is.

The deliverable box's Copy button copies the body, so a body like "use the
Copy button on the file above" hands the user that sentence instead of what
they asked for.  And "files" is read only inside "deliverable": a card-level
"files" key is drawn nowhere.
"""
from engine import handoff_rules as hr


def _card(body=None, files=None, top_files=None):
    d = {"kind": "notes", "title": "Onboarding brief"}
    if body is not None:
        d["body"] = body
    if files is not None:
        d["files"] = files
    card = {"type": "deliverable", "title": "Onboarding brief", "deliverable": d}
    if top_files is not None:
        card["files"] = top_files
    return card


_FILE = [{"label": "Onboarding brief", "url": "/static/scratch/brief.md", "copy": True}]


def test_a_body_that_points_at_a_copy_button_is_sent_back():
    p = hr.deliverable_pointer_problems(_card(body="Use the Copy button on the file above: the whole brief."))
    assert p and "Copy button copies the body" in p[0]


def test_other_pointer_wordings_are_sent_back():
    for body in ("The full text is in the attached file.",
                 "Tap Copy on the row below to get the brief.",
                 "See the attached file for the brief.",
                 "Download button above has the whole draft."):
        assert hr.deliverable_pointer_problems(_card(body=body)), body


def test_files_outside_the_deliverable_are_sent_back():
    p = hr.deliverable_pointer_problems(_card(body="Welcome aboard, Rowan.\n\nHere is your first week.",
                                              top_files=_FILE))
    assert p and "nothing draws it" in p[0]


def test_a_real_body_passes():
    body = ("You are joining the parcel-tracking team. Start by reading the service map, then run the "
            "local stack and open a small fix. Ask Juniper for access to the staging board.")
    assert hr.deliverable_pointer_problems(_card(body=body)) == []


def test_a_long_text_that_mentions_a_copy_button_passes():
    # A real deliverable may itself talk about buttons (a how-to); only a
    # short note that points elsewhere is a pointer.
    body = ("To share a report, use the Copy button in the header. " + "Step details follow here. " * 30)
    assert hr.deliverable_pointer_problems(_card(body=body)) == []


def test_files_inside_the_deliverable_with_no_body_pass():
    assert hr.deliverable_pointer_problems(_card(files=_FILE)) == []


def test_the_stop_runs_the_check():
    card = _card(body="Use the Copy button on the file above.")
    import json
    out = hr.review("```handoff\n" + json.dumps(card) + "\n```") or ""
    assert "says where the text is" in out


def test_the_instructions_say_the_body_is_the_text():
    assert "The body IS the text" in hr.INSTRUCTIONS
    assert "nothing draws it" in hr.INSTRUCTIONS
