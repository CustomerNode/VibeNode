"""A deliverable's text draws as markdown, and its meta never breaks into
characters (owner, 2026-10-28: a notes deliverable showed its meta line as
one row per character, "0 P 1 r 2 o …", and "## The idea" raw).
Runs the real ``static/js/last-card.js`` in node."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from daemon import handoff_rules

LAST_CARD = Path(__file__).resolve().parents[1] / "static" / "js" / "last-card.js"

SCRIPT = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
const out = {};
for (const [k, c] of Object.entries(JSON.parse(process.argv[2]))) out[k] = VNLastCard.render(c, {});
// The body as the full-screen view draws it.
out.body = VNLastCard.deliverableBody('markdown', JSON.parse(process.argv[3]).md);
out.plain = VNLastCard.deliverableBody('text', JSON.parse(process.argv[3]).text);
process.stdout.write(JSON.stringify(out));
"""

BODY = ("## The idea\nEvery decision gets **the same spine**, plus a *lens*.\n\n"
        "## 1. The spine\n- One line\n- A reason\n  that wraps\n\n1. First\n2. Second\n\nPlain closing line.\n\n"
        "| Status | Means | Look |\n|---|---|---:|\n| Needs you | Your move (a \\| b) | **orange** |\n"
        "| Standby (new) | Not now |\n\n---\n\n```\n<x> | y\n```\nA line | with a pipe\n---\nafter")


def _card(meta):
    return {"type": "deliverable", "headline": "Notes", "context": "x",
            "deliverable": {"kind": "notes", "title": "Plan", "meta": meta, "body": BODY},
            "needs": {"kind": "none", "why": "y", "next": "z"}}


HTML = '<!doctype html><h1 class="x">Hello</h1><script>alert(1)</script>'
TEXT = "## not a heading\n  indented <b>raw</b>"


def _fmt(fmt, body):
    c = _card(None)
    c["deliverable"] = dict(c["deliverable"], format=fmt, body=body)
    return c


@pytest.fixture(scope="module")
def out():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    cards = {"str": _card("Proposal to think with · 2026-10-28"), "obj": _card({"To": "a@b.c", "Subject": "Hi"}),
             "list": _card(["One line", "Two line"]), "none": _card(None),
             "html": _fmt("html", HTML), "text": _fmt("text", TEXT), "bogus": _fmt("Weird", "## Hi"),
             "answer": {"type": "explain", "headline": "Q", "context": "x", "answer": "- a\n- b",
                        "needs": {"kind": "none", "why": "y", "next": "z"}}}
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(cards), json.dumps({"md": BODY, "text": TEXT})],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    return json.loads(r.stdout)


def _meta(h):
    return h.split('<div class="dv-meta">', 1)[1].split("</div>", 1)[0] if 'class="dv-meta"' in h else ""


def test_a_string_meta_is_one_line_not_one_row_per_character(out):
    m = _meta(out["str"])
    assert m == '<span class="ml">Proposal to think with · 2026-10-28</span>'
    assert '<span class="mk">0</span>' not in out["str"]


def test_object_and_list_meta(out):
    m = _meta(out["obj"])
    assert '<span class="mk">To</span>' in m and 'class="mv subj">Hi<' in m
    assert _meta(out["list"]) == '<span class="ml">One line</span><span class="ml">Two line</span>'
    assert 'class="dv-meta"' not in out["none"]


def test_the_body_is_markdown(out):
    body = out["body"]
    assert "<h4>The idea</h4>" in body and "<h4>1. The spine</h4>" in body
    assert "##" not in body
    assert "<ul><li>One line</li><li>A reason that wraps</li></ul>" in body
    assert "<ol><li>First</li><li>Second</li></ol>" in body
    assert "<b>the same spine</b>" in body and "<i>lens</i>" in body
    assert "<p>Plain closing line.</p>" in body


def test_an_answer_list_is_a_list(out):
    assert "<ul><li>a</li><li>b</li></ul>" in out["answer"]


def test_sessions_are_told_the_deliverable_shape():
    t = handoff_rules.INSTRUCTIONS
    assert '"meta" is an object' in t and "markdown" in t


def _body(_h):
    return OUT_BODY[0]


OUT_BODY = [""]


@pytest.fixture(autouse=True)
def _md_body(request):
    if "out" in request.fixturenames:
        OUT_BODY[0] = request.getfixturevalue("out")["body"]


def test_a_table_is_a_table(out):
    """2026-10-28: a proposal's status table showed as raw pipes."""
    b = _body(out["str"])
    assert '<div class="md-tbl"><table><thead><tr><th>Status</th><th>Means</th><th style="text-align:right">Look</th></tr></thead>' in b
    assert "<tr><td>Needs you</td><td>Your move (a | b)</td><td style=\"text-align:right\"><b>orange</b></td></tr>" in b
    assert "<tr><td>Standby (new)</td><td>Not now</td><td style=\"text-align:right\"></td></tr>" in b
    assert "|---" not in b and "| Status" not in b


def test_rules_and_code_fences(out):
    b = _body(out["str"])
    assert "<hr>" in b
    assert "<pre><code>&lt;x&gt; | y</code></pre>" in b
    assert "```" not in b
    # a pipe in a sentence followed by a rule is not a table
    assert "<p>A line | with a pipe</p><hr><p>after</p>" in b


# ── Formats: markdown (default), html, text; each opens full screen with
#    Copy (owner, 2026-10-28: "the ability for a deliverable to be rendered
#    html ... types markdown, html, or plaintext"). ──

def _preview(h):
    return h.split('class="dv dv-pv lm-dvopen', 1)[1].split('<div class="lm-rows">', 1)[0]


def test_the_preview_is_the_one_way_to_full_screen(out):
    """Owner, 2026-10-28: "a max heighted preview of the rendered content
    that is effectively then the same click to expand mechanism", with no
    Open button beside it."""
    for k, fmt, label in (("str", "markdown", "Formatted text"), ("html", "html", "Web page"), ("text", "text", "Plain text")):
        p = _preview(out[k])
        assert 'data-dv="' + fmt + '"' in p and 'role="button"' in p and 'tabindex="0"' in p
        assert '<span class="k">' + label + "</span>" in p
        assert out[k].count("lm-dvopen") == 1
        assert ">Open<" not in out[k] and "· full screen" not in out[k]


def test_the_preview_shows_the_rendered_text(out):
    assert '<div class="dv-body">' in _preview(out["str"]) and "<h4>The idea</h4>" in _preview(out["str"])
    assert '<div class="dv-plain">## not a heading' in _preview(out["text"])


def test_an_html_page_is_previewed_in_an_empty_sandbox(out):
    h = out["html"]
    p = _preview(h)
    assert '<iframe class="dv-pvframe" sandbox=""' in p and "allow-scripts" not in h and "allow-same-origin" not in h
    # the page is never drawn as tags in the card: only escaped, in srcdoc and Copy
    assert "<h1" not in h and "<script" not in h
    assert "Copy the HTML" in h and "&lt;script&gt;alert(1)&lt;/script&gt;" in h


def test_plain_text_is_shown_as_written(out):
    b = out["plain"]
    assert b.startswith('<div class="dv-plain">## not a heading\n  indented &lt;b&gt;raw&lt;/b&gt;</div>')
    assert "<h4>" not in b


def test_an_unknown_format_falls_back_to_markdown(out):
    assert 'data-dv="markdown"' in out["bogus"]


def test_the_rules_accept_the_three_formats_and_refuse_others():
    def probs(fmt):
        c = _fmt(fmt, "Body")
        return [p for p in handoff_rules.format_problems(c) if "deliverable.format" in p]
    assert probs("html") == [] and probs("text") == [] and probs("Markdown") == []
    assert probs("pdf")
    assert '"format": "markdown"' in handoff_rules.INSTRUCTIONS and '"html"' in handoff_rules.INSTRUCTIONS


# ── "Left waiting": a way back to earlier unfinished work (owner's pick C,
#    2026-10-28, static/scratch/card-back-to.html). ──

BACK_SCRIPT = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
const out = {};
for (const [k, c] of Object.entries(JSON.parse(process.argv[2]))) out[k] = VNLastCard.render(c, {});
process.stdout.write(JSON.stringify(out));
"""


def _done(**extra):
    c = {"type": "done", "title": "t", "headline": "h", "status": {"done": ["x"]},
         "links": [{"label": "l", "kind": "link", "url": "/x"}], "needs": {"kind": "none", "why": "w", "next": "n"}}
    c.update(extra)
    return c


@pytest.fixture(scope="module")
def back():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    cards = {"with": _done(back_to={"label": "Standby, the fourth status", "status": "Spec written",
                                    "reply": "Back to <Standby> now"}),
             "noreply": _done(back_to={"label": "Standby"}), "without": _done(), "bad": _done(back_to="Standby")}
    r = subprocess.run(["node", "-e", BACK_SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    return json.loads(r.stdout)


def test_back_to_draws_a_left_waiting_strip_with_one_button(back):
    h = back["with"]
    assert h.count('class="lm-backto"') == 1
    assert "Left waiting: Standby, the fourth status" in h and "Spec written" in h
    assert 'data-back="Back to &lt;Standby&gt; now">Back to it</button>' in h
    # above the verdict rows, inside the Next band
    assert h.index("lm-backto") < h.index("lm-finish")


def test_back_to_without_a_reply_says_where_it_goes(back):
    assert 'data-back="That’s done. Back to Standby."' in back["noreply"]


def test_no_strip_without_back_to(back):
    assert "lm-backto" not in back["without"] and "lm-backto" not in back["bad"]


def test_the_rules_check_back_to_and_tell_sessions():
    assert not [p for p in handoff_rules.format_problems(_done(back_to={"label": "Standby"})) if "back_to" in p]
    assert [p for p in handoff_rules.format_problems(_done(back_to="Standby")) if "back_to" in p]
    assert '"back_to"' in handoff_rules.INSTRUCTIONS and "Left waiting" in handoff_rules.INSTRUCTIONS


# ── Files to download (2026-10-28: a presentation's zip was handed over as a
#    "done" card's Validate row; "Try it" framed the archive and showed
#    nothing.  The owner: "that should be a deliverable type, and what it did
#    needs to get rejected"). ──

ZIP = "/static/scratch/smart-termination-presentation.zip"


def _the_card_that_was_sent():
    return {"type": "done", "title": "Presentation ready to download",
            "headline": "The smart termination presentation is ready to download.",
            "context": "x", "status": {"done": ["Zip: offline presentation", "PDF: 24 pages"]},
            "verified": [{"check": "unzipped", "result": "opens", "ok": True}],
            "validate": {"how": "Download the zip, unzip it, and open the page inside in Chrome", "url": ZIP},
            "needs": {"kind": "none", "why": "w", "next": "n"}}


def _files_card(**d):
    dv = {"kind": "presentation", "title": "Smart termination",
          "files": [{"label": "Offline presentation", "url": ZIP, "size": "3.4 MB"},
                    {"label": "PDF with speaker notes", "url": "/static/scratch/deck%20notes.pdf", "size": "4.6 MB"}]}
    dv.update(d)
    return {"type": "deliverable", "title": "Presentation files", "path": ["Smart termination deck"], "headline": "h", "context": "x",
            "deliverable": dv, "needs": {"kind": "none", "why": "w", "next": "n"}}


def test_a_download_behind_try_it_is_sent_back():
    msg = "Done.\n\n```handoff\n" + json.dumps(_the_card_that_was_sent()) + "\n```"
    reason = handoff_rules.review(msg)
    assert reason and "file to download" in reason and '"files"' in reason
    # a download in links or needs.look is sent back too; a screenshot is not
    c = _the_card_that_was_sent()
    c["validate"]["url"] = "/static/scratch/demo.html"
    c["links"] = [{"label": "PDF", "kind": "file", "url": "/static/scratch/x.pdf?v=2"}]
    assert handoff_rules.download_problems(c)
    c["links"] = [{"label": "shot", "kind": "image", "url": "/static/scratch/x.png"},
                  {"label": "code", "kind": "file", "url": "app/x.py"}]
    assert handoff_rules.download_problems(c) == []


def test_a_files_deliverable_passes_and_a_disk_path_does_not():
    assert handoff_rules.format_problems(_files_card()) == []
    assert handoff_rules.download_problems(_files_card()) == []
    bad = _files_card(files=[{"label": "Zip", "url": "/home/me/deck.zip"}, {"url": "~/x.pdf"}])
    probs = handoff_rules.download_problems(bad)
    assert any("files[0].url" in p for p in probs) and any("files[1] needs \"label\"" in p for p in probs)
    assert handoff_rules.format_problems(_files_card(files=None)) != []
    assert '"files": [{"label"' in handoff_rules.INSTRUCTIONS and "Download buttons" in handoff_rules.INSTRUCTIONS


@pytest.fixture(scope="module")
def files_out():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    cards = {"files": _files_card(), "both": _files_card(body="## Notes\nRead me first.")}
    r = subprocess.run(["node", "-e", BACK_SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    return json.loads(r.stdout)


def test_files_are_download_links_not_viewers(files_out):
    h = files_out["files"]
    assert h.count('class="lm-row lm-dlfile"') == 2
    assert '<a class="lm-row lm-dlfile" href="' + ZIP + '" download="smart-termination-presentation.zip">' in h
    assert 'download="deck notes.pdf"' in h
    assert "3.4 MB · smart-termination-presentation.zip" in h and " Download</span></a>" in h
    # no viewer, no preview, no copy rows for a files-only deliverable
    assert "data-view" not in h.split('lm-dlfiles"', 1)[1].split("</div>", 1)[0]
    assert "lm-dvopen" not in h and "data-copy" not in h


def test_files_and_a_body_show_both(files_out):
    h = files_out["both"]
    assert h.count("lm-dlfile\"") == 2 and h.count("lm-dvopen") == 1 and "Copy the text" in h


def test_a_text_file_marked_copy_is_a_copy_button():
    """Owner, 2026-10-29: "I want it in my copy buffer when I select the
    button".  A text file with "copy": true is a Copy row that loads its text
    on render; anything else, or a file outside static/scratch, still downloads."""
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    cards = {"copy": _files_card(files=[{"label": "Code", "url": "/static/scratch/code.txt", "size": "175 KB", "copy": True},
                                        {"label": "Zip", "url": ZIP, "copy": True},
                                        {"label": "Elsewhere", "url": "/api/x.txt", "copy": True}])}
    assert handoff_rules.format_problems(cards["copy"]) == []
    r = subprocess.run(["node", "-e", BACK_SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    h = json.loads(r.stdout)["copy"]
    assert h.count("data-copy-src=") == 1 and 'data-copy-src="/static/scratch/code.txt" aria-disabled="true"' in h
    assert " Save .txt</span></button>" in h and h.count('class="lm-row lm-dlfile"') == 2
    assert "data-copy=" not in h   # filled only once the text has arrived
    assert '"copy": true' in handoff_rules.INSTRUCTIONS


def test_an_older_card_pointing_at_a_download_downloads_it():
    """Cards already in a thread are not rewritten: their zip downloads."""
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    old = _the_card_that_was_sent()
    old["links"] = [{"label": "PDF", "kind": "file", "url": "/static/scratch/x.pdf"}]
    r = subprocess.run(["node", "-e", BACK_SCRIPT, str(LAST_CARD), json.dumps({"old": old})],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    h = json.loads(r.stdout)["old"]
    assert '<a class="lm-row lm-dlfile lm-validate" href="' + ZIP + '" download=' in h
    assert 'href="/static/scratch/x.pdf" download="x.pdf"' in h
    assert 'data-view="' + ZIP not in h and 'data-view="/static/scratch/x.pdf' not in h
