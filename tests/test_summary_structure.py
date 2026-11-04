"""The end-of-turn summary: one structure and one emphasis rule for every card type.

Group headings with indented levels, the Copy menu, and one emphasis (a line
down Review & respond and a highlighted main action) on every card type.  The
real app CSS and
last-card.js in headless Chromium, inside the thread's own markup.
"""
import pathlib

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
ROOT = pathlib.Path(__file__).resolve().parents[1]

Z = {"big": "Night theme", "task": "Add a night theme", "now": "Switch built"}
COMMON = {"path": ["Settings"], "context": "x", "answer": "Short text here.",
          "explained": "## Plain\n" + "Plain words here. " * 12, "full_answer": "## Full\n" + "Full text. " * 30,
          "what_was_done": "Built it.", "checked": "Tried it.", "zoom": Z,
          "status": {"done": ["Switch built"], "remaining": [], "broken": []}}
CARDS = {
    "done": dict(COMMON, type="done", title="Night switch", headline="Done.",
                 needs={"kind": "none", "why": "Done.", "next": "Nothing."}),
    "working": dict(COMMON, type="working", title="Night switch", headline="Working.",
                    needs={"kind": "none", "why": "Running.", "next": "It finishes.", "eta_minutes": 5}),
    "explain": dict(COMMON, type="explain", title="Night switch", headline="Answer.",
                    needs={"kind": "none", "why": "Answered.", "next": "Nothing."}),
    "decide": dict(COMMON, type="needs_you", title="Night switch", headline="Pick one.",
                   asks=[{"ask": "add a night theme", "state": "needs_you"}],
                   needs={"kind": "decision", "ask": "When should it turn on?", "why": "Sunset follows the season.",
                          "next": "I wire it.", "replies": [
                              {"key": "A", "label": "At sunset", "reply": "Sunset", "recommended": True},
                              {"key": "B", "label": "At a fixed hour", "reply": "Fixed hour"}]}),
}

PAGE = """<html data-theme="dark"><head><style>{app}</style><style>{card}</style></head>
<body style="width:900px"><div class="conversation live-log">{cells}</div></body></html>"""


@pytest.fixture(scope="module")
def page():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # no browser installed
            pytest.skip(f"no Chromium: {e}")
        pg = b.new_page(viewport={"width": 900, "height": 1000})
        cells = "".join(f'<div class="msg assistant"><div class="msg-body" id="{k}"></div></div>' for k in CARDS)
        pg.set_content(PAGE.format(app=(ROOT / "src/web/static/style.css").read_text(encoding="utf-8"),
                                   card=(ROOT / "src/web/static/css/last-card.css").read_text(encoding="utf-8"), cells=cells))
        pg.add_script_tag(content=(ROOT / "src/web/static/js/last-card.js").read_text(encoding="utf-8"))
        pg.evaluate("cs => { for (const k in cs) document.getElementById(k).innerHTML = VNLastCard.render(cs[k]);"
                    " document.querySelectorAll('.lm-finish[hidden]').forEach(f => f.hidden = false); }", CARDS)
        yield pg
        b.close()


def _style(page, sel, prop, pseudo=None):
    return page.evaluate("([s, p, ps]) => { const e = document.querySelector(s); return e ? getComputedStyle(e, ps).getPropertyValue(p) : null; }",
                         [sel, prop, pseudo])


AMBER = "rgb(240, 166, 40)"


def test_every_card_that_waits_on_you_has_the_line_and_working_does_not(page):
    for k in ("done", "explain", "decide"):
        assert AMBER in _style(page, f"#{k} .lm > .lm-move", "box-shadow"), k
    assert _style(page, "#working .lm > .lm-move", "box-shadow") == "none"


def test_the_main_action_has_the_old_highlight(page):
    bg = _style(page, "#decide .lm-decide", "background-color")
    assert bg.startswith("rgba(240, 166, 40"), bg
    assert "240, 166, 40" in _style(page, "#decide .lm-decide", "box-shadow")


def test_answer_buttons_are_chips_with_their_coloured_icons(page):
    for k in ("done", "decide"):
        r = _style(page, f"#{k} .lm-verdict button[data-verdict]", "border-top-left-radius")
        assert r == "999px", (k, r)
    icon = _style(page, "#decide .lm-verdict .v-done .ic", "color")
    assert icon not in ("rgb(128, 128, 128)", "rgb(136, 136, 136)"), icon   # coloured, not grey


def test_the_groups_summary_details_and_review_and_respond(page):
    assert page.inner_text("#done .lm-answer .vn-ghrow b") == "Summary"
    assert _style(page, "#done .lm > .lm-answer + .lm-status", "content", "::before") == '"Details"'
    left = page.evaluate("() => { const c = document.querySelector('#done .lm'); const L = s => document.querySelector(s).getBoundingClientRect().left - c.getBoundingClientRect().left;"
                         " return [L('#done .vn-ghrow b'), L('#done .vn-sub > .dt'), L('#done .lm-checked > .dt')]; }")
    assert abs(left[1] - left[0] - 14) <= 1 and abs(left[2] - left[1]) <= 1, left   # parts indented 14px under the heading


def test_status_words_keep_their_colour(page):
    """The card eyebrow keeps its status colour."""
    for k in ("done", "decide", "working"):
        c = _style(page, f"#{k} .lm-pill", "color")
        r, g, b = [int(x) for x in c[c.index("(") + 1:c.index(")")].split(",")[:3]]
        assert max(r, g, b) - min(r, g, b) > 40, (k, c)


def test_sub_items_are_parted_by_space_and_a_hairline(page):
    """Sub-items are parted so they do not bleed into each other."""
    assert _style(page, "#done .vn-sub + .vn-sub", "border-top-style") == "solid"
    assert float(_style(page, "#done .vn-sub + .vn-sub", "padding-top")[:-2]) >= 18
    assert _style(page, "#done .lm > .lm-checked", "border-top-style", "::after") == "solid"
