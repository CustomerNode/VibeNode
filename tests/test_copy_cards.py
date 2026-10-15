"""Copy cards: a long text the chat offers to copy without displaying it.

An agent writes the text to ``~/.claude/copy-exports/`` and puts a fenced
block with the language ``copyfile`` in its reply (line 1 the path, optional
line 2 a label). ``mdParse`` (markdown.js) turns the block into a placeholder,
``addSmartCopyButtons`` (smart-copy.js) turns that into a card with the name,
the size and a Copy button, and ``GET /api/copy-export`` (live_api.py) serves
the text.

What these tests pin:

* the route reads ONLY files inside the exports folder (a phone on the tailnet
  reaches it too): no other path, no ``..``, no symlink out, no binary;
* the card never shows the text, shows its size, and copies it byte for byte;
* the copy runs inside the tap itself, because iOS refuses a copy started
  after an await (so the text must already be loaded when the button enables);
* every new session is told about copy cards (app.js and kanban.js).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import create_app
    from app.routes import live_api

    exports = tmp_path / "copy-exports"
    exports.mkdir()
    monkeypatch.setattr(live_api, "COPY_EXPORTS_DIR", exports)
    app = create_app(testing=True)
    with app.test_client() as c:
        yield c, exports, tmp_path


def _get(c, path):
    r = c.get("/api/copy-export", query_string={"path": str(path)})
    return r.status_code, r.get_json()


def test_route_returns_a_file_in_the_exports_folder(client):
    c, exports, _ = client
    body = "Part 1. Voice rules\n\nNo em-dashes — ever.\n" * 50
    f = exports / "pack.md"
    f.write_text(body, encoding="utf-8")
    status, data = _get(c, f)
    assert status == 200
    assert data["ok"] and data["text"] == body and data["name"] == "pack.md"
    assert data["chars"] == len(body) and data["bytes"] == f.stat().st_size


def test_route_refuses_anything_outside_the_exports_folder(client):
    c, exports, tmp = client
    secret = tmp / "secret.env"
    secret.write_text("API_KEY=nope\n", encoding="utf-8")
    link = exports / "innocent.md"
    os.symlink(secret, link)
    for path in (secret, f"{exports}/../secret.env", link, "/etc/passwd", "~/.ssh/id_rsa"):
        status, data = _get(c, path)
        assert status == 403, (path, status)
        assert not data["ok"] and "text" not in data


def test_route_reports_missing_binary_and_empty(client):
    c, exports, _ = client
    assert _get(c, exports / "gone.md")[0] == 404
    (exports / "blob.bin").write_bytes(b"\xff\xfe\x00\x80binary")
    assert _get(c, exports / "blob.bin")[0] == 415
    assert c.get("/api/copy-export").status_code == 400


# ---------------------------------------------------------------------------
# The card, in headless Chromium with the real markdown.js and smart-copy.js
# ---------------------------------------------------------------------------

TEXT = "# CustomerNode brand pack\n\nSECRET-BODY-LINE that must never be drawn.\n" + ("x" * 5000)


def _page(reply_json):
    style = (STATIC / "style.css").read_text(encoding="utf-8")
    markdown = (STATIC / "js" / "markdown.js").read_text(encoding="utf-8")
    smart = (STATIC / "js" / "smart-copy.js").read_text(encoding="utf-8")
    return f"""<!doctype html><html data-theme="dark"><head><meta charset="utf-8">
<style>{style}</style></head><body>
<div class="msg assistant"><div class="msg-body msg-content" id="asst"></div></div>
<script>
  window.__fetched = [];
  window.fetch = (url) => {{ window.__fetched.push(url);
    return Promise.resolve({{ status: 200, json: () => Promise.resolve({reply_json}) }}); }};
</script>
<script>{markdown}</script>
<script>{smart}</script>
<script>
  const reply = 'Here is the pack.\\n\\n```copyfile\\n/home/someone/.claude/copy-exports/pack \\"q\\".md\\nBrand pack\\n```\\n\\nUpload it to ChatGPT.';
  const asst = document.getElementById('asst');
  asst.innerHTML = mdParse(reply);
  addSmartCopyButtons(asst, reply);
</script></body></html>"""


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {str(exc).splitlines()[0][:80]}")
        yield b
        b.close()


def _open(browser, reply, width=390):
    pg = browser.new_page(viewport={"width": width, "height": 844})
    pg.set_content(_page(json.dumps(reply)))
    pg.wait_for_function("() => document.querySelector('.vn-copy-export-meta') && "
                         "!document.querySelector('.vn-copy-export-meta').textContent.startsWith('Loading')")
    return pg


OK = {"ok": True, "name": "pack.md", "bytes": 160378, "chars": len(TEXT), "text": TEXT}


def test_card_shows_name_and_size_but_never_the_text(browser):
    pg = _open(browser, OK)
    try:
        card = pg.evaluate("""() => { const c = document.querySelector('.vn-copy-export');
            return { text: document.body.innerText, name: c.querySelector('.vn-copy-export-name').textContent,
                     meta: c.querySelector('.vn-copy-export-meta').textContent,
                     disabled: c.querySelector('button').disabled,
                     url: window.__fetched[0], right: c.getBoundingClientRect().right,
                     vw: document.documentElement.clientWidth }; }""")
        assert "SECRET-BODY-LINE" not in card["text"]
        assert card["name"] == "Brand pack"
        assert card["meta"].startswith("156.6 KB") and f"{len(TEXT):,}" in card["meta"]
        assert card["disabled"] is False
        # The path with a quote in it survived the attribute and reached the route.
        assert "pack%20%22q%22.md" in card["url"], card["url"]
        assert card["right"] <= card["vw"] + 1, "card runs off a phone screen"
        assert "Upload it to ChatGPT." in card["text"]
    finally:
        pg.close()


def test_copy_happens_inside_the_tap_and_copies_the_exact_text(browser):
    pg = _open(browser, OK)
    try:
        got = pg.evaluate("""() => { let got = null;
            window.vnCopyText = (t) => { got = t; return Promise.resolve(); };
            document.querySelector('.vn-copy-export-btn').click();
            return got; }""")  # read synchronously: no await between tap and copy
        assert got == TEXT
        pg.wait_for_function("() => document.querySelector('.vn-copy-export-btn').textContent === 'Copied'")
    finally:
        pg.close()


def test_an_unreadable_file_disables_the_button_and_says_why(browser):
    pg = _open(browser, {"ok": False, "error": "File no longer exists"})
    try:
        state = pg.evaluate("""() => { const c = document.querySelector('.vn-copy-export');
            return { meta: c.querySelector('.vn-copy-export-meta').textContent,
                     disabled: c.querySelector('button').disabled, cls: c.className }; }""")
        assert state["meta"] == "File no longer exists"
        assert state["disabled"] is True and "unavailable" in state["cls"]
    finally:
        pg.close()


def test_outside_vibenode_the_block_reads_as_the_path():
    """No JS hydration: the placeholder still carries the path as plain code."""
    md = (STATIC / "js" / "markdown.js").read_text(encoding="utf-8")
    assert "lang === 'copyfile'" in md
    assert "'\"><code>' + path + '</code></div>'" in md


# ---------------------------------------------------------------------------
# Every new session is told about copy cards
# ---------------------------------------------------------------------------

def test_new_sessions_get_the_copy_card_instruction():
    from app.routes import live_api

    smart = (STATIC / "js" / "smart-copy.js").read_text(encoding="utf-8")
    start = smart.index("function _buildCopyExportNote()")
    note = smart[start:smart.index("}", start)]
    assert "copyfile" in note and "~/.claude/copy-exports/" in note
    assert live_api.COPY_EXPORTS_DIR.parts[-2:] == (".claude", "copy-exports")
    for js in ("app.js", "kanban.js"):
        src = (STATIC / "js" / js).read_text(encoding="utf-8")
        assert "_buildCopyExportNote()" in src, f"{js} does not add the copy-card note"
