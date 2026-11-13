"""A design pick about another project's own app is shown with variants built
in that app, never a copy of it redrawn by hand on a scratch page.  Every
project, page and message here is made up."""

from types import SimpleNamespace

import pytest

from engine import handoff_evidence as E
from engine import handoff_rules as R
from engine import smart_termination as S

HAND_DRAWN = """<!doctype html><html><head><style>.bar{display:flex}.pill{border-radius:9px}</style></head><body>
<section id="now" data-option="now"><div class="bar"><span class="pill">Orders</span><span class="pill">Map</span></div></section>
<section id="A" data-option="A"><div class="bar"><span class="pill">Timeline</span></div><ul><li>Monday</li></ul></section>
<section id="B" data-option="B"><div class="bar"><span class="pill">Now and next</span></div><table><tr><td>v2</td></tr></table></section>
<section id="C" data-option="C"><div class="bar"><span class="pill">Side by side</span></div></section>
</body></html>"""

FRAMED = """<!doctype html><html><body>
<section id="now" data-option="now"><iframe src="live/now.html"></iframe></section>
<section id="A" data-option="A"><iframe src="live/A.html"></iframe></section>
<section id="B" data-option="B"><img src="shots/b.png" alt=""></section>
<section id="C" data-option="C"><iframe src="http://127.0.0.1:8123/orders?trial=c"></iframe></section>
</body></html>"""


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    d = tmp_path / "scratch"
    d.mkdir()
    (d / "orders-hand.html").write_text(HAND_DRAWN)
    (d / "orders-framed.html").write_text(FRAMED)
    monkeypatch.setattr(R, "_SCRATCH_DIR", d)
    return d


@pytest.fixture
def shop(tmp_path):
    """Another project with a web front end of its own, two folders down."""
    root = tmp_path / "shopfront"
    (root / "site" / "web").mkdir(parents=True)
    (root / "site" / "web" / "package.json").write_text("{}")
    return root


def _decision(page, title="Orders window", headline="Pick a direction for the Orders window of the shop app."):
    replies = [{"key": k, "label": label, "reply": label,
                "screens": [{"label": label, "url": f"/static/scratch/{page}#{k}", "device": "web"}]}
               for k, label in (("A", "Timeline list"), ("B", "Now and next"), ("C", "Side by side"))]
    return {"type": "needs_you", "title": title, "headline": headline,
            "needs": {"kind": "decision", "ask": "Which direction should the Orders window take?",
                      "look": [{"label": "The directions", "kind": "link", "url": f"/static/scratch/{page}"}],
                      "replies": replies}}


def _facts(cwd, ports=()):
    acts = [E.Action(tool="Bash", detail=f"curl -s http://127.0.0.1:{p}/api/orders", ok=True) for p in ports]
    return E.TurnFacts(cwd=str(cwd), actions=acts)


def test_hand_drawn_copy_of_a_running_app_is_sent_back(scratch, tmp_path):
    out = E.real_product_problems(_decision("orders-hand.html"), _facts(tmp_path / "plain", ports=["8123"]))
    assert out and "drawn by hand" in out[0] and "8123" in out[0]
    assert "IN the app" in out[0] and "realistic data" in out[0]
    # It rides the normal list of evidence problems.
    assert any("drawn by hand" in p for p in E.problems(_decision("orders-hand.html"),
                                                        _facts(tmp_path / "plain", ports=["8123"])))


def test_a_project_with_its_own_front_end_counts_even_before_it_runs(scratch, shop):
    out = E.real_product_problems(_decision("orders-hand.html"), _facts(shop))
    assert out and "drawn by hand" in out[0]


def test_variants_framed_from_the_real_app_pass(scratch, shop):
    assert E.real_product_problems(_decision("orders-framed.html"), _facts(shop, ports=["8123"])) == []


def test_vibenode_itself_may_use_scratch_mockups(scratch, tmp_path):
    home = tmp_path / "vn"
    (home / "src" / "engine").mkdir(parents=True)
    (home / "src" / "engine" / "handoff_rules.py").write_text("")
    (home / "package.json").write_text("{}")
    assert E.real_product_problems(_decision("orders-hand.html"), _facts(home, ports=["8123"])) == []


def test_no_app_in_sight_passes(scratch, tmp_path):
    plain = tmp_path / "essays"
    plain.mkdir()
    # Only VibeNode's own port was reached, and the folder has no front end.
    assert E.real_product_problems(_decision("orders-hand.html"), _facts(plain, ports=["5050"])) == []


def test_a_choice_that_is_not_about_a_screen_passes(scratch, shop):
    card = _decision("orders-hand.html", title="Merge the fix",
                     headline="Merging the fix waits on a local test build.")
    card["needs"]["ask"] = "May I build a local test image before merging?"
    for r, label in zip(card["needs"]["replies"], ("Build then merge", "Merge now", "Wait a day")):
        r["label"] = label
    assert E.real_product_problems(card, _facts(shop, ports=["8123"])) == []


def test_unknown_folder_passes(scratch):
    assert E.real_product_problems(_decision("orders-hand.html"), E.TurnFacts(cwd="")) == []


def test_live_screens_on_the_app_pass(scratch, shop):
    card = _decision("orders-hand.html")
    for r in card["needs"]["replies"]:
        r["screens"] = [{"label": r["label"], "url": f"http://127.0.0.1:8123/orders?trial={r['key']}",
                         "device": "web"}]
    assert E.real_product_problems(card, _facts(shop, ports=["8123"])) == []


def test_the_guidance_says_so():
    text = S.INSTRUCTIONS if isinstance(getattr(S, "INSTRUCTIONS", None), str) else ""
    assert "REAL product variants" in text and "Thin demo data" in text
    assert "another project's app, the variants are built in that app" in R.INSTRUCTIONS
