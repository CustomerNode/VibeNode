"""Playbooks: Settings → Tools lists what VibeNode runs when you type its words.

A workforce entry is a playbook when its frontmatter has ``playbook: true``;
``/api/workforce/assets`` serves its line, explanation, words to type and
"how it works" as ``playbook`` (``_playbook_fields`` in live_api.py), and
``openPlaybooks()`` (modals.js) draws one folding row per playbook.

What these tests pin:

* the server lists A/B Test and Code Pipeline, with their words to type split
  on ``|`` (phrases can contain commas), and nothing that is not a playbook;
* the Settings menu has the Playbooks entry under Tools;
* the real modals.js draws one line per playbook, opens one row at a time,
  and Try it fills the text box without sending anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._browser import shared_browser_scope

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


@pytest.fixture(scope="module")
def assets():
    from app import create_app
    app = create_app(testing=True)
    with app.test_client() as c:
        d = c.get("/api/workforce/assets").get_json()
    assert d["ok"]
    return d["assets"]


def test_server_lists_the_two_playbooks(assets):
    books = {a["id"]: a["playbook"] for a in assets if a.get("playbook")}
    assert set(books) >= {"ab-test", "code-pipeline"}
    ab = books["ab-test"]
    assert ab["line"] and ab["what"] and ab["how"]
    assert len(ab["say"]) >= 2
    # a phrase with commas stays one phrase
    assert any("," in s for s in ab["say"])
    assert all(s.startswith(("a b", "ab")) for s in ab["say"])
    # Code Pipeline is started as "code pipeline", never its old alias
    cp = books["code-pipeline"]
    assert cp["say"] and all(s.startswith("code pipeline ") for s in cp["say"])
    assert cp["what"] and "HIGH" in cp["how"]
    # a glance, not a page: every entry stays short
    for b in books.values():
        assert len(b["line"].split()) <= 8 and len(b["what"].split()) <= 22 and all(len(st.split()) <= 14 for st in b["how"].split("|")) and 3 <= len(b["how"].split("|")) <= 6


def test_entries_that_are_not_playbooks_carry_none(assets):
    plain = [a for a in assets if a["id"] not in ("ab-test", "code-pipeline")]
    assert plain and all(a["playbook"] is None for a in plain)


def test_settings_menu_has_playbooks_under_tools():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    tools = html.split("openBulkOperations()", 1)[1].split('class="sys-separator"', 1)[0]
    assert "openPlaybooks()" in tools


def _page(payload):
    style = (STATIC / "style.css").read_text(encoding="utf-8")
    modals = (STATIC / "js" / "modals.js").read_text(encoding="utf-8")
    return f"""<!doctype html><html data-theme="dark"><head><meta charset="utf-8">
<style>{style}</style></head><body>
<div id="hdr-sys"><div id="hdr-sys-dropdown"></div></div><div id="summary-overlay"></div>
<div id="pm-overlay"></div>
<textarea id="live-input-ta"></textarea>
<script>
  window.__sent = 0;
  window.fetch = () => Promise.resolve({{ ok: true, json: () => Promise.resolve({payload}) }});
  function _closePm() {{ document.getElementById('pm-overlay').classList.remove('show'); }}
  function showToast() {{}}
</script>
<script>{modals}</script>
</body></html>"""


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def test_rows_fold_open_one_at_a_time_and_try_it_fills_the_box(browser, assets):
    pg = browser.new_page(viewport={"width": 1100, "height": 900})
    errors = []
    # The rest of modals.js expects the whole app (other dialogs, the header
    # menu); only errors raised inside the Playbooks code count here.
    pg.on("pageerror", lambda e: errors.append(e.stack) if ("_pb" in (e.stack or "") or "openPlaybooks" in (e.stack or "")) else None)
    pg.set_content(_page(json.dumps({"ok": True, "assets": assets})))
    pg.evaluate("openPlaybooks()")
    pg.wait_for_selector(".pbk-row")
    rows = pg.locator(".pbk-row")
    names = [rows.nth(i).locator(".pbk-name").inner_text() for i in range(rows.count())]
    assert names == sorted(names) and {"A/B Test", "Code Pipeline"} <= set(names)
    assert pg.locator(".pbk-row.open").count() == 0

    rows.nth(0).locator(".pbk-hd").click()
    assert pg.locator(".pbk-row.open").count() == 1
    rows.nth(1).locator(".pbk-hd").click()
    assert rows.nth(1).get_attribute("class") == "pbk-row open"
    assert pg.locator(".pbk-row.open").count() == 1
    rows.nth(1).locator(".pbk-hd").click()
    assert pg.locator(".pbk-row.open").count() == 0

    ab = rows.filter(has_text="A/B Test")
    ab.locator(".pbk-hd").click()
    first = ab.locator(".pbk-say").first.inner_text().replace("Say", "", 1).strip()
    ab.locator(".pbk-try").click()
    assert pg.input_value("#live-input-ta") == first
    # How it works is a numbered list of short steps
    assert pg.locator(".pbk-row").first.locator(".pbk-steps li").count() >= 3
    assert "show" not in (pg.get_attribute("#pm-overlay", "class") or "")
    assert not errors


def test_an_old_web_server_says_the_list_needs_a_restart(browser):
    pg = browser.new_page()
    pg.set_content(_page(json.dumps({"ok": True, "assets": [{"id": "x", "name": "X"}]})))
    pg.evaluate("openPlaybooks()")
    pg.wait_for_function("() => !document.querySelector('#pbk-list').textContent.includes('Loading')")
    assert "restarts" in pg.inner_text("#pbk-list")
