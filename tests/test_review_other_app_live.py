"""Another app on this machine shows live in the Decide popup (2026-10-28).

The owner, on CustomerNode's design trials: "i was imagining live pages plug
into the vibenode stuff".  CustomerNode's trial pages are its real app, signed
in, at http://127.0.0.1:8000/app/...?<trial>=<option>.  The preview proxy
cannot show them: it sends no cookies (the page is its login) and the page's
own script requests go to VibeNode.  So on this computer a page on another
port of this machine is framed as itself, under this page's host name (only
the same site sends that app's sign-in into a frame), with ?theme= on its
address.  The phone cannot reach it, so there a screen's "image" is drawn.

Runs the real ``static/js/last-card.js`` in node with the browser stubbed.
"""

import html
import json
import shutil
import subprocess
from pathlib import Path

import pytest

LAST_CARD = Path(__file__).resolve().parents[1] / "static" / "js" / "last-card.js"

SCRIPT = r"""
const [file, host, input] = process.argv.slice(1);
global.window = global;
global.location = { host, hostname: host.replace(/:\d+$/, ''), port: (host.match(/:(\d+)$/) || [])[1] || '' };
global.document = { addEventListener() {}, querySelectorAll() { return []; },
                    documentElement: { getAttribute: () => 'dark' } };
eval(require('fs').readFileSync(file, 'utf8'));
const t = JSON.parse(input);
const out = { src: t.src.map(u => VNLastCard.reviewSrc(u)), themed: t.themed.map(u => VNLastCard.themedSrc(u)),
              card: VNLastCard.render(t.card, {}),
              stage: t.stage.map(([r, dev]) => VNLastCard.tplStage(r, dev, 0)) };
process.stdout.write(JSON.stringify(out));
"""

LIVE = "http://127.0.0.1:8000/app/journeys/j1?spaceOpens=b"
SHOTS = "C:/Users/me/shots/spaceOpens/raw"
IMAGE = {"web": {"light": f"{SHOTS}/0_desktop_b_light.png", "dark": f"{SHOTS}/0_desktop_b_dark.png"},
         "phone": {"light": f"{SHOTS}/0_phone_b_light.png", "dark": f"{SHOTS}/0_phone_b_dark.png"}}
SCREEN = {"label": "Discovery on the journey home", "url": LIVE, "device": "web", "image": IMAGE}


def _card():
    replies = [{"key": "A", "label": "Today", "reply": "a", "screens": [dict(SCREEN, url=LIVE.replace("=b", "=a"))]},
               {"key": "B", "label": "Opens as shown", "reply": "b", "screens": [SCREEN]},
               {"key": "C", "label": "Two ways in", "reply": "c",
                "screens": [{"label": "Bad picture", "url": LIVE, "image": "notes.md"},
                            {"label": "Relative", "url": LIVE, "image": ".design_review/x/0.png"},
                            {"label": "Two slashes", "url": LIVE, "image": "file://C:/x.png"}]}]
    return {"type": "needs_you", "headline": "Pick how a preview opens", "context": "x",
            "links": [{"label": "A picture from the app", "kind": "image", "url": "http://127.0.0.1:8000/x.png"}],
            "needs": {"kind": "decision", "ask": "Which?", "why": "y", "replies": replies}}


def _run(host):
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    reply = {"screens": [SCREEN]}
    t = {"src": [LIVE, "http://localhost:8000/app/x#top", "http://127.0.0.1:5050/static/scratch/x.html",
                 "https://example.com/page", "http://[::1]:3001/", "http://localhost:5050/static/scratch/x.html#b"],
         "themed": [LIVE + "#v-b", "http://127.0.0.1:8000/app/x?theme=light"],
         "card": _card(),
         "stage": [[reply, "web"], [reply, "phone"], [reply, "both"]]}
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), host, json.dumps(t)],
                       capture_output=True, text=True, timeout=30, check=True)
    return json.loads(r.stdout)


@pytest.fixture(scope="module")
def desk():
    return _run("127.0.0.1:5050")


@pytest.fixture(scope="module")
def phone():
    return _run("my-pc.tail1234.ts.net:5050")


def test_on_this_computer_another_app_is_framed_as_itself(desk):
    # live, signed in: never the proxy, which shows its login
    assert desk["src"][0] == LIVE
    # as given, never renamed: a server on ::1 only (Vite under "localhost")
    # refuses 127.0.0.1, and a sign-in belongs to the name it was made at
    assert desk["src"][1] == "http://localhost:8000/app/x#top"
    assert desk["src"][4] == "http://[::1]:3001/"
    assert desk["src"][2] == "/static/scratch/x.html"
    # VibeNode itself under its other name: this page's own path
    assert desk["src"][5] == "/static/scratch/x.html#b"
    assert desk["src"][3] == ""


def test_the_phone_still_goes_through_the_proxy(phone):
    assert phone["src"][0] == "/api/preview/proxy?u=" + "http%3A%2F%2F127.0.0.1%3A8000%2Fapp%2Fjourneys%2Fj1%3FspaceOpens%3Db"


def test_a_framed_app_is_told_the_theme_on_its_address(desk):
    assert desk["themed"][0] == LIVE + "&theme=dark#v-b"
    assert desk["themed"][1] == "http://127.0.0.1:8000/app/x?theme=light"


def test_the_screen_picture_rides_on_the_decide_button(desk):
    h = desk["card"]
    data = json.loads(html.unescape(h.split('data-decide="', 1)[1].split('"', 1)[0]))
    a, b, c = data["replies"]
    assert b["screens"] == [SCREEN]
    # not a picture: dropped, the screen stays
    assert c["screens"] == [{"label": n, "url": LIVE, "device": "web"} for n in ("Bad picture", "Relative", "Two slashes")]


def test_on_this_computer_the_popup_shows_the_live_page(desk):
    web, ph, both = desk["stage"]
    assert '<iframe title="Discovery on the journey home" data-src="' + html.escape(LIVE) + '"' in web
    assert "<img" not in web and "<img" not in ph and "<img" not in both
    assert both.count("<iframe") == 2


def test_on_the_phone_the_popup_shows_the_picture_for_the_device(phone):
    web, ph, both = phone["stage"]
    assert "<iframe" not in web + ph + both
    # the app's theme (dark here), the device asked for
    assert "/api/card-image?path=" + "C%3A%2FUsers%2Fme%2Fshots%2FspaceOpens%2Fraw%2F0_desktop_b_dark.png" in web
    assert "/api/card-image?path=" + "C%3A%2FUsers%2Fme%2Fshots%2FspaceOpens%2Fraw%2F0_phone_b_dark.png" in ph
    assert both.count("<img") == 2


def test_a_picture_from_another_port_still_comes_through_the_proxy(desk):
    # A picture needs no sign-in, and a server that keeps its files to its own
    # site refuses an <img> from VibeNode's port: the proxy fetches it.
    assert 'src="/api/preview/proxy?u=http%3A%2F%2F127.0.0.1%3A8000%2Fx.png"' in desk["card"]


# ── The gate: a live page of another app carries its picture for the phone ──

from daemon import handoff_rules as hr  # noqa: E402


def _decision(screen):
    return {"type": "needs_you", "title": "How a preview opens", "headline": "Pick how a preview opens",
            "needs": {"kind": "decision", "ask": "Which?", "why": "y",
                      "look": [{"label": "Today", "kind": "link", "url": LIVE}],
                      "replies": [{"key": "A", "label": "Today, as it is", "reply": "a", "screens": [screen]},
                                  {"key": "B", "label": "Opens as shown", "reply": "b", "screens": [screen]}]}}


def test_a_live_screen_without_a_picture_is_sent_back():
    bare = {k: v for k, v in SCREEN.items() if k != "image"}
    why = hr.live_screen_picture_problem(_decision(bare))
    assert why and "phone cannot reach it" in why and LIVE in why
    assert why in hr.format_problems(_decision(bare))


def test_a_live_screen_with_its_picture_passes():
    assert hr.live_screen_picture_problem(_decision(SCREEN)) is None
    assert hr.live_screen_picture_problem(_decision(dict(SCREEN, image=f"{SHOTS}/0_desktop_b_dark.png"))) is None


def test_a_picture_that_is_not_one_does_not_count():
    assert hr.live_screen_picture_problem(_decision(dict(SCREEN, image="notes.md")))
    assert hr.live_screen_picture_problem(_decision(dict(SCREEN, image={"web": "x.txt"})))
    # the popup cannot load these either (_screenImage drops them)
    assert hr.live_screen_picture_problem(_decision(dict(SCREEN, image=".design_review/x/0_phone_b_dark.png")))
    assert hr.live_screen_picture_problem(_decision(dict(SCREEN, image="file://C:/x.png")))
    assert hr.live_screen_picture_problem(_decision(dict(SCREEN, image="file:///C:/x.png"))) is None


def test_every_way_of_naming_this_machine_needs_its_picture():
    # what the browser reads as another port of this machine, as the popup does
    for url in ("http://localhost/app/x", "http://me@127.0.0.1:8000/app/x", "http://[::1]:3001/", "https://localhost/x"):
        assert hr.live_screen_picture_problem(_decision({"label": "x", "url": url})), url


def test_vibenodes_own_pages_and_the_web_need_no_picture():
    for url in ("http://127.0.0.1:5050/static/scratch/x.html#a", "/static/scratch/x.html#a", "https://example.com/x"):
        assert hr.live_screen_picture_problem(_decision({"label": "x", "url": url})) is None


def test_choices_with_their_own_screens_need_no_look_page():
    # Today's look is choice A: a page in needs.look would add a "Now" tab
    # showing it twice, and that tab has no picture for the phone.
    card = _decision(SCREEN)
    del card["needs"]["look"]
    assert not [p for p in hr.format_problems(card) if "something to look at" in p]
    for r in card["needs"]["replies"]:
        del r["screens"]
    assert [p for p in hr.format_problems(card) if "something to look at" in p]


# ── Switching choices keeps the live page (owner, 2026-10-29: "if i click
#    between them i keep seeing the splash over and fucking over again") ──

SWITCH = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050', hostname: '127.0.0.1', port: '5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; },
                    documentElement: { getAttribute: () => 'dark' } };
const posted = [];
global.addEventListener = () => {}; global.removeEventListener = () => {};
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
const frame = (url) => ({ _vnUrl: url, contentWindow: { postMessage: (m, o) => posted.push([m, o]) }, set src(v) { posted.push(['reload', v]); } });
const stage = (frames) => ({ querySelectorAll: sel => sel === 'iframe' ? frames : [] });
const at = 'http://127.0.0.1:8000/app/journeys/j1?stage=5&spaceOpens=a&theme=dark';
const r = (url) => ({ screens: [{ label: 's', url, device: 'web' }] });
const out = {
  same: VNLastCard.switchInPlace(stage([frame(at)]), r('http://127.0.0.1:8000/app/journeys/j1?stage=5&spaceOpens=b'), 'web', 0),
  other: VNLastCard.switchInPlace(stage([frame(at)]), r('http://127.0.0.1:8000/app/journeys/j1/stages/5?spaceOpens=b'), 'web', 0),
  own: VNLastCard.switchInPlace(stage([frame('http://127.0.0.1:5050/static/scratch/x.html')]), r('/static/scratch/x.html#b'), 'web', 0),
};
out.posted = posted;
process.stdout.write(JSON.stringify(out));
"""


def test_another_choice_on_the_same_page_is_switched_in_place():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", SWITCH, str(LAST_CARD)], capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(r.stdout)
    # same page, another choice: asked to rewrite its address, not reloaded
    assert out["same"] is True
    assert out["posted"][0] == [{"type": "vn:set-address", "url": "/app/journeys/j1?stage=5&spaceOpens=b&theme=dark"},
                                "http://127.0.0.1:8000"]
    assert not [p for p in out["posted"] if p[0] == "reload"]
    # another page, or VibeNode's own page: built as before
    assert out["other"] is False and out["own"] is False
