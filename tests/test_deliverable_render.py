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
                       capture_output=True, text=True, timeout=30, check=True)
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
                       capture_output=True, text=True, timeout=30, check=True)
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
