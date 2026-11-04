"""The end-of-turn summary is pixel aligned.

Every edge the eye runs down is one edge: the summary switch's first
word and Copy's last letter sit on the content edges and share a baseline; every
list (done / remaining, checked, tried, the Decide choices) starts on the content
edge with its text at one offset.  The real app CSS and last-card.js in headless
Chromium, inside the thread's own markup so its list styles apply.
"""
import pathlib

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
ROOT = pathlib.Path(__file__).resolve().parents[1]

CARD = {
    "type": "needs_you", "title": "Night theme switch", "path": ["Settings", "Theme"],
    "headline": "The night theme switch is built; pick when it turns on.",
    "context": "Adding a night theme.",
    "answer": "It works by hand. It can also follow sunset.",
    "explained": "## What\nA night theme. " + "More words here. " * 10,
    "full_answer": "## Full\nAll of it.",
    "what_was_done": "Built the switch and the theme.", "checked": "Tried both themes by hand.",
    "status": {"done": ["Switch built"], "remaining": ["Sunset timing"], "broken": []},
    "verified": [{"check": "Switch by hand", "result": "theme changes", "ok": True}],
    "compare": {"before": ["Day only"], "after": ["Day and night"]},
    "zoom": {"big": "Night theme", "task": "Add a night theme", "now": "Switch built"},
    "asks": [{"ask": "add a night theme", "state": "needs_you"}],
    "needs": {"kind": "decision", "ask": "When should the night theme turn on?",
              "why": "Sunset follows the season.", "next": "I wire up the one you pick.",
              "tried": [{"what": "Fixed hour", "result": "wrong in winter"},
                        {"what": "Light sensor", "result": "none on this machine"}],
              "replies": [{"key": "A", "label": "At sunset", "reply": "Sunset", "recommended": True},
                          {"key": "B", "label": "At a fixed hour", "reply": "Fixed hour"}]},
}

BLOCKED = {"type": "needs_you", "title": "Phone check", "path": ["Settings"],
           "headline": "The phone check waits on a sign-in.", "context": "x",
           "answer": "Needs a sign-in.", "explained": "## Why\n" + "Words here. " * 20,
           "full_answer": "## Full\nAll.", "what_was_done": "Tried twice.", "checked": "Both failed.",
           "zoom": {"big": "Night theme", "task": "Check it on the phone", "now": "Waits on sign-in"},
           "needs": {"kind": "action", "ask": "Sign in to the phone network", "why": "Only you can sign in.",
                     "next": "I check right after.", "only_you": "A sign-in is yours.",
                     "replies": [{"key": "A", "label": "I signed in", "reply": "Signed in"}],
                     "tried": [{"what": "Restarted it", "result": "asks for a sign-in"},
                               {"what": "Reached the phone", "result": "no route"}]}}

PAGE = """<html data-theme="dark"><head><style>{app}</style><style>{card}</style></head>
<body style="width:900px"><div class="conversation live-log"><div class="msg assistant">
<div class="msg-body msg-content" id="h"></div></div></div></body></html>"""


@pytest.fixture(scope="module", params=[900, 390], ids=["desktop", "phone"])
def page(request):
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # no browser installed
            pytest.skip(f"no Chromium: {e}")
        pg = b.new_page(viewport={"width": request.param, "height": 1000})
        pg.set_content(PAGE.format(app=(ROOT / "src/web/static/style.css").read_text(encoding="utf-8"),
                                   card=(ROOT / "src/web/static/css/last-card.css").read_text(encoding="utf-8")))
        pg.add_script_tag(content=(ROOT / "src/web/static/js/last-card.js").read_text(encoding="utf-8"))
        pg.evaluate("cs => { document.getElementById('h').innerHTML = cs.map(c => VNLastCard.render(c)).join(''); }", [CARD, BLOCKED])
        yield pg
        b.close()


GEOM = r"""() => {
  const card = document.querySelector('.lm');
  function baseline(el) {
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, { acceptNode: n => n.textContent.trim() ? 1 : 3 });
    const t = w.nextNode();
    const m = document.createElement('span'); m.style.cssText = 'display:inline-block;width:0;height:0;vertical-align:baseline';
    t.parentNode.insertBefore(m, t); const y = m.getBoundingClientRect().top; m.remove(); return y;
  }
  function textBox(el) {
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, { acceptNode: n => n.textContent.trim() ? 1 : 3 });
    const t = w.nextNode(); const r = document.createRange(); r.selectNodeContents(t); return r.getBoundingClientRect();
  }
  const bar = document.querySelector('.lm-answer .lm-depth-bar');
  const edge = bar.getBoundingClientRect();
  const sub = document.querySelector('.lm-answer .lm-depth-p:not([hidden])');
  const para = document.querySelector('.lm-checked .dd') || sub;
  const sw = document.querySelector('.lm-answer .lm-depth-b'), cp = document.querySelector('.lm-answer .lm-depth-copy');
  const lists = {};
  for (const sel of ['.lm-statcols ul', '.lm-list', '.lm-tried']) {
    const li = document.querySelector(sel + ' li'); if (!li) continue;
    const ul = li.parentElement.getBoundingClientRect();
    lists[sel] = { mark: li.querySelector('svg').getBoundingClientRect().left - ul.left,
                   text: textBox(li.querySelector('.w')).left - ul.left,
                   ul: ul.left };
  }
  const dli = document.querySelector('.lm-dlist li');
  return {
    left: edge.left, right: edge.right,
    swText: textBox(sw).left, swBase: baseline(sw),
    cpText: cp.querySelector('span').getBoundingClientRect().right, cpBase: baseline(cp),
    paraLeft: para.getBoundingClientRect().left,
    lists, dlistMarker: getComputedStyle(dli).listStyleType,
    dlistLeft: dli.getBoundingClientRect().left,
  };
}"""


def test_switch_and_copy_sit_on_the_content_edges_and_share_a_baseline(page):
    """Horizontal switch, so the card reads as it did before the vertical
    trial: Short on the left, Copy on the right, same baseline."""
    g = page.evaluate(GEOM)
    assert abs(g["swText"] - g["left"]) <= 1.5, g        # "Short" starts on the content edge
    assert abs(g["cpText"] - g["right"]) <= 1.5, g       # Copy's text ends on the right edge
    assert abs(g["cpBase"] - g["swBase"]) <= 1.5, g      # one baseline


def test_every_list_has_one_shape_on_the_content_edge(page):
    g = page.evaluate(GEOM)
    assert set(g["lists"]) == {".lm-statcols ul", ".lm-list", ".lm-tried"}, g
    for sel, l in g["lists"].items():
        assert abs(l["ul"] - g["paraLeft"]) <= 0.5 or sel == ".lm-statcols ul", (sel, g)
        assert abs(l["mark"]) <= 0.5, (sel, l)            # mark on the list's left edge
    texts = {round(l["text"], 1) for l in g["lists"].values()}
    assert max(texts) - min(texts) <= 0.5, g["lists"]    # text at one offset everywhere


def test_decide_choices_have_no_stray_bullet_and_start_on_the_edge(page):
    g = page.evaluate(GEOM)
    assert g["dlistMarker"] == "none"
    assert abs(g["dlistLeft"] - g["paraLeft"]) <= 0.5, g


def test_recaps_and_labels_are_quiet(page):
    st = page.evaluate("""() => {
      const w = s => getComputedStyle(document.querySelector(s));
      return { now: w('.lm-zoom .z2 .t').fontWeight, label: w('.lm-checked > .dt').textTransform };
    }""")
    assert int(st["now"]) < 600 and st["label"] == "none", st


def test_a_before_after_written_as_single_lines_still_draws(page):
    """A real Decide summary drew as a blank box: its compare sides were strings."""
    html = page.evaluate("c => VNLastCard.render(c)", dict(CARD, compare={"before": "Day only", "after": "Day and night"}))
    assert "Day only" in html and "Day and night" in html
