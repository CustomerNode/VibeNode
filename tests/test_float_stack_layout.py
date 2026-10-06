"""The floating-notice stack must never cover the composer's action buttons.

Every bottom-of-screen notice (#toast, #git-sync-mini, .vn-undo-toast,
.compose-undo-toast) is placed by ``_layoutFloats()`` in ``static/js/utils.js``,
which lifts the stack clear of the chat composer.  Two things broke that in
practice, and both are asserted here against a real browser rather than by
reading the source:

1. ``_updateFloatOffset()`` used ``bar.offsetParent`` as its "is the composer on
   screen" test.  ``offsetParent`` is ``null`` for ANY ``position: fixed``
   element in Blink and WebKit, and the phone layout pins the composer with
   ``position: fixed`` (``mobile.css .live-input-bar``).  So on every phone the
   bar measured as off-screen, the lift fell back to 20px, and the whole stack
   landed on the paste / mic / Send buttons.

2. The composer's "+" menu (``showMenu()`` in ``image-attach.js``) clamped its
   ``top`` into the viewport, which pushed it back DOWN over the same buttons
   whenever it was taller than the space above them.

The page under test is built from the real stylesheets and the real
``utils.js``, so a regression in any of them fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

# The composer's right-hand action buttons, in the order the user names them.
ACTION_BUTTONS = ("live-image-btn", "live-voice-btn", "live-send-btn")

# Every float the stack owns (_floatEls in utils.js).  `.show` is what makes
# #toast / #git-sync-mini eligible; the undo toasts are eligible whenever they
# exist in the DOM.
FLOATS = ("toast", "git-sync-mini", "vn-undo", "compose-undo")


def _page_html() -> str:
    """Real CSS + real utils.js + the composer markup live-panel.js emits."""
    style = (STATIC / "style.css").read_text(encoding="utf-8")
    mobile = (STATIC / "css" / "mobile.css").read_text(encoding="utf-8")
    utils = (STATIC / "js" / "utils.js").read_text(encoding="utf-8")
    tooltip = (STATIC / "js" / "tooltip.js").read_text(encoding="utf-8")

    return f"""<!doctype html>
<html data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{style}</style>
<style>{mobile}</style>
</head><body>
<div class="layout"><div class="main"><div class="live-panel" id="live-panel">
  <div class="conversation live-log" id="live-log" style="height:300px"></div>
  <div class="live-output-shelf" id="live-output-shelf"></div>
  <div id="live-queue-area"></div>
  <div class="live-input-bar" id="live-input-bar">
    <textarea id="live-input-ta" class="live-textarea" rows="2"></textarea>
    <div class="live-bar-row">
      <span class="vn-status" data-new="0">Idle</span>
      <span class="send-hint" style="font-size:10px">Ctrl+Enter to send</span>
      <button type="button" class="live-send-btn live-image-btn" id="live-image-btn">+</button>
      <button class="live-send-btn" id="live-voice-btn">mic</button>
      <button class="live-send-btn" id="live-send-btn">Send</button>
    </div>
  </div>
</div></div></div>

<div class="toast" id="toast">Saved</div>
<div class="git-sync-mini" id="git-sync-mini">Pulling &amp; pushing…</div>
<div class="vn-undo-toast" id="vn-undo">Session slept
  <button class="vn-undo-toast-btn">Undo</button></div>
<div class="compose-undo-toast" id="compose-undo">Deleted
  <button class="compose-undo-btn">Undo</button></div>

<!-- A titled element parked just above the composer: hovering it is the case
     where a tooltip's preferred "below" placement lands on the button row. -->
<div id="tip-anchor" title="A tooltip long enough to have real height, so that a
downward placement would cover the composer's action buttons.">hover me</div>

<script>{utils}</script>
<script>{tooltip}</script>
<script>
// Show everything the stack can hold at once — the worst case for overlap.
function showAllFloats() {{
  document.getElementById('toast').className = 'toast show';
  document.getElementById('git-sync-mini').className = 'git-sync-mini show';
  document.getElementById('vn-undo').classList.add('show');
  document.getElementById('compose-undo').classList.add('show');
  _updateFloatOffset();
}}
function rect(el) {{ const r = el.getBoundingClientRect();
  return {{top: r.top, right: r.right, bottom: r.bottom, left: r.left, h: r.height}}; }}
function intersects(a, b) {{
  return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
}}
/** Every (float, button) pair that physically overlaps. */
function overlaps(floatIds, btnIds) {{
  const bad = [];
  floatIds.forEach(fid => {{
    const f = rect(document.getElementById(fid));
    if (f.h <= 0) return;                       // not rendered — can't cover anything
    btnIds.forEach(bid => {{
      const b = rect(document.getElementById(bid));
      if (b.h > 0 && intersects(f, b)) bad.push(fid + ' over ' + bid);
    }});
  }});
  return bad;
}}
</script>
</body></html>"""


@pytest.fixture(scope="module")
def page():
    pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browser binary not downloaded on this machine
            pytest.skip(f"chromium unavailable: {str(exc).splitlines()[0][:80]}")
        pg = browser.new_page(viewport={"width": 390, "height": 844})
        pg.set_content(_page_html())
        yield pg
        browser.close()


@pytest.mark.slow
@pytest.mark.parametrize(
    "width,height,label",
    [(390, 844, "phone-portrait"), (844, 390, "phone-landscape"), (1440, 900, "desktop")],
)
def test_floats_never_cover_the_composer_buttons(page, width, height, label):
    """No notice in the shared stack may touch paste / mic / Send."""
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("showAllFloats()")
    # Sampled twice: once while the slide-in animations are still running (each
    # float travels upward into place, so it is lower than its settled rect for
    # a few hundred ms) and once settled.  Both frames must be clean.
    mid = page.evaluate("([f, b]) => overlaps(f, b)", [list(FLOATS), list(ACTION_BUTTONS)])
    page.wait_for_timeout(500)
    settled = page.evaluate("([f, b]) => overlaps(f, b)", [list(FLOATS), list(ACTION_BUTTONS)])
    assert mid == [], f"{label}: floats cover buttons mid-animation -> {json.dumps(mid)}"
    assert settled == [], f"{label}: floats cover buttons at rest -> {json.dumps(settled)}"


def _css_block(css: str, opener: str) -> str:
    """The body of the first rule/at-rule whose text starts with ``opener``."""
    start = css.index(opener)
    depth, i = 0, css.index("{", start)
    out = i
    while i < len(css):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                out = i
                break
        i += 1
    return css[start : out + 1]


def test_entry_animations_cannot_dip_into_the_composer():
    """Each float slides up into place, so its travel is how far it dips.

    The clearance in ``utils.js`` has to be larger than the biggest travel or a
    notice sits over the paste / mic / Send buttons for the length of its
    animation — which is exactly what happened with a 12px clearance against the
    git-sync indicator's 16px slide and the compose undo toast's 20px one.  No
    browser needed: this is pure arithmetic between two files.
    """
    import re

    utils = (STATIC / "js" / "utils.js").read_text(encoding="utf-8")
    m = re.search(r"_FLOAT_CLEARANCE\s*=\s*(\d+)", utils)
    assert m, "_FLOAT_CLEARANCE is gone from utils.js — the stack lost its gap"
    clearance = int(m.group(1))

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    blocks = {
        "git-sync-mini slide-in": _css_block(css, "@keyframes miniSlideIn"),
        ".toast": _css_block(css, ".toast {"),
        ".vn-undo-toast": _css_block(css, ".vn-undo-toast {"),
        ".compose-undo-toast": _css_block(css, ".compose-undo-toast {"),
    }
    for name, block in blocks.items():
        for travel in re.findall(r"translateY\(\s*(\d+(?:\.\d+)?)px\s*\)", block):
            assert float(travel) < clearance, (
                f"{name} slides up {travel}px into a {clearance}px gap — it will "
                f"cover the composer buttons while animating"
            )


def test_no_float_transitions_its_own_bottom():
    """``bottom`` is written by _layoutFloats and must jump, never animate.

    ``.git-sync-mini`` used ``transition: all``, so each appearance animated its
    ``bottom`` from the 20px corner fallback up to its slot in the stack —
    travelling straight across the paste / mic / Send buttons for 250ms.
    """
    import re

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for sel in (".git-sync-mini {", ".toast {", ".vn-undo-toast {", ".compose-undo-toast {"):
        block = _css_block(css, sel)
        for decl in re.findall(r"transition\s*:\s*([^;}]+)", block):
            props = {p.strip().split()[0] for p in decl.split(",") if p.strip()}
            assert "all" not in props, f"{sel} uses `transition: all`, which animates `bottom`"
            assert "bottom" not in props, f"{sel} transitions `bottom` — it must jump"


@pytest.mark.slow
def test_offsetparent_is_null_for_the_pinned_phone_composer(page):
    """Documents WHY the on-screen test cannot be ``offsetParent``.

    If this ever starts returning an element, the composer stopped being
    ``position: fixed`` on phones and the pin in mobile.css regressed.
    """
    page.set_viewport_size({"width": 390, "height": 844})
    probe = page.evaluate(
        """() => {
          const bar = document.getElementById('live-input-bar');
          return {
            position: getComputedStyle(bar).position,
            offsetParent: bar.offsetParent ? bar.offsetParent.tagName : null,
            clientRects: bar.getClientRects().length,
          };
        }"""
    )
    assert probe["position"] == "fixed", "phone composer is no longer pinned"
    assert probe["offsetParent"] is None, "offsetParent became usable — comment needs updating"
    assert probe["clientRects"] > 0, "getClientRects() must still see the pinned bar"


@pytest.mark.slow
def test_lift_actually_clears_the_composer(page):
    """The computed lift must put the stack above the whole composer."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("showAllFloats()")
    res = page.evaluate(
        """() => {
          const bar = document.getElementById('live-input-bar').getBoundingClientRect();
          const lift = parseFloat(getComputedStyle(document.documentElement)
            .getPropertyValue('--vn-float-bottom'));
          return {lift, barTop: bar.top, vh: window.innerHeight};
        }"""
    )
    # bottom:<lift> puts the float's lower edge at (vh - lift) in viewport coords.
    float_baseline = res["vh"] - res["lift"]
    assert float_baseline <= res["barTop"], (
        f"stack baseline {float_baseline} sits inside the composer (top {res['barTop']})"
    )


@pytest.mark.slow
def test_tooltip_flips_above_rather_than_onto_the_composer(page):
    """A tooltip anchored just above the composer must not drop onto it.

    ``tooltip.js::place()`` prefers "below" and only flipped when the VIEWPORT
    bottom would clip it, so an anchor sitting a few pixels above the composer
    put its tooltip squarely on the paste / mic / Send buttons.  The composer's
    top edge is now a clip boundary of its own.
    """
    page.set_viewport_size({"width": 1440, "height": 900})
    page.evaluate(
        """() => {
          const bar = document.getElementById('live-input-bar').getBoundingClientRect();
          const a = document.getElementById('tip-anchor');
          // Right-aligned with the action buttons, so a downward placement
          // lands ON them rather than merely inside the composer.
          const send = document.getElementById('live-send-btn').getBoundingClientRect();
          a.style.cssText = 'position:fixed;width:140px;height:20px;background:#444;' +
            'color:#fff;z-index:1;left:' + Math.round(send.right - 140) + 'px;' +
            'top:' + Math.round(bar.top - 26) + 'px';
        }"""
    )
    page.hover("#tip-anchor")
    page.wait_for_selector(".vn-tip.visible", timeout=4000)
    res = page.evaluate(
        """(btnIds) => {
          const t = document.querySelector('.vn-tip').getBoundingClientRect();
          const bar = document.getElementById('live-input-bar').getBoundingClientRect();
          const hit = btnIds.filter(id => {
            const b = document.getElementById(id).getBoundingClientRect();
            return t.left < b.right && t.right > b.left && t.top < b.bottom && t.bottom > b.top;
          });
          return {hit, dir: document.querySelector('.vn-tip').getAttribute('data-dir'),
                  tipBottom: t.bottom, barTop: bar.top};
        }""",
        list(ACTION_BUTTONS),
    )
    assert res["hit"] == [], f"tooltip covering {res['hit']}"
    assert res["dir"] == "above", f"expected the tooltip to flip above, got {res['dir']!r}"
    assert res["tipBottom"] <= res["barTop"], "tooltip reaches into the composer"
    page.mouse.move(0, 0)


@pytest.mark.slow
def test_plus_menu_opens_above_the_buttons_it_belongs_to(page):
    """``showMenu()`` anchors by ``bottom``, so a tall menu scrolls, never descends.

    Re-implements the positioning from ``image-attach.js::showMenu`` against an
    oversized menu — the case the old ``top: Math.max(8, ...)`` clamp pushed
    down over the composer.
    """
    page.set_viewport_size({"width": 390, "height": 844})
    bad = page.evaluate(
        """(btnIds) => {
          const btn = document.getElementById('live-image-btn');
          const menu = document.createElement('div');
          menu.className = 'vn-more-menu';
          // Deliberately taller than the space above the button.
          menu.style.height = '4000px';
          document.body.appendChild(menu);
          const r = btn.getBoundingClientRect();
          const w = menu.offsetWidth;
          menu.style.left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8)) + 'px';
          menu.style.bottom = Math.round(window.innerHeight - r.top + 8) + 'px';
          menu.style.maxHeight = Math.max(80, Math.round(r.top - 16)) + 'px';
          const m = menu.getBoundingClientRect();
          const out = btnIds.filter(id => {
            const b = document.getElementById(id).getBoundingClientRect();
            return m.left < b.right && m.right > b.left && m.top < b.bottom && m.bottom > b.top;
          });
          const top = m.top;
          menu.remove();
          return {out, top};
        }""",
        list(ACTION_BUTTONS),
    )
    assert bad["out"] == [], f"+ menu covering {bad['out']}"
    assert bad["top"] >= 0, "+ menu overflowed the top of the viewport"
