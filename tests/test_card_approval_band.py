"""The Approve band on the end-of-turn card: an approval only the person gives.

An approval is its own band under Review & respond: one button to approve, a line saying what is approved, and an
information button with the Short, Plain words and Full tabs. Card field::

    "approval": {"id": "RS-1a2b3c4d5e", "what": "...", "answer": "...", "explained": "...", "full_answer": "..."}

``src/web/static/js/last-card.js`` (``approvalBand``) draws it in Review & respond: what is approved, an info button that
opens the three tabs, and one Approve button that sends "Approve <id>: <what>" as the person's own message. A project's stop
add-on accepts an approval only from that message. Structure runs the real file in node; the click runs it in
headless Chromium through Playwright (skips without it).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._browser import shared_browser_scope
from tests.test_card_finding_band import SCRIPT, _open

ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / "src" / "web" / "static" / "js" / "last-card.js"

APPROVAL = {"id": "RS-1a2b3c4d5e", "what": "3 changes to the project's checks",
            "answer": "Short: what approving does.",
            "explained": "Plain words: **these files** become what judges the work.",
            "full_answer": "## Full\n\nEvery changed part."}
REPLY = "Approve RS-1a2b3c4d5e: 3 changes to the project's checks"


def _card(**extra):
    return dict({"type": "needs_you", "title": "T", "headline": "H", "context": "x",
                 "needs": {"kind": "approve", "ask": "Approve the changed checks?", "why": "y"}}, **extra)


@pytest.fixture(scope="module")
def out():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    cards = {"with": _card(approval=APPROVAL), "without": _card(),
             "bad": _card(approval={"what": "no id"}), "escaped": _card(approval=dict(APPROVAL, what="<b>x</b>"))}
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(cards)],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    return json.loads(r.stdout)


def test_the_band_shows_what_is_approved_and_one_approve_button(out):
    h = out["with"]
    assert h.count("lm-approveband") == 1 and ">Approve<" in h
    band = h.split("lm-approveband", 1)[1]
    assert APPROVAL["what"].replace("'", "&#39;") in band
    assert 'data-find="' + REPLY.replace("'", "&#39;") + '"' in band
    assert band.split("lm-find-xp", 1)[0].count("<button") == 2   # the info button and Approve


def test_it_sits_in_review_and_respond(out):
    h = out["with"]
    assert h.index(">Review &amp; respond<") < h.index("lm-approveband")


def test_no_block_or_no_id_draws_nothing_and_text_is_escaped(out):
    assert "lm-approveband" not in out["without"] and "lm-approveband" not in out["bad"]
    assert "<b>x</b>" not in out["escaped"].split("lm-approveband", 1)[1].split("lm-find-xp", 1)[0]


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def test_approve_sends_his_approval_message_once(browser, tmp_path):
    page = _open(browser, [_card(approval=APPROVAL)], tmp_path)
    try:
        btn = page.locator(".lm-approveband .go")
        assert btn.count() == 1 and btn.inner_text() == "Approve"
        btn.click()
        assert page.evaluate("window.__sent") == [["s1", REPLY]]
        page.locator(".lm-approveband .go").click(force=True)
        assert page.evaluate("window.__sent.length") == 1
    finally:
        page.close()


def test_the_info_button_opens_short_plain_words_and_full(browser, tmp_path):
    page = _open(browser, [_card(approval=APPROVAL)], tmp_path)
    try:
        xp = page.locator(".lm-approveband .lm-find-xp")
        assert not xp.is_visible()
        page.locator(".lm-approveband .lm-find-info").click()
        assert xp.is_visible() and "Short: what approving does." in xp.inner_text()
        xp.locator(".lm-depth-b[data-depth=plain]").click()
        assert "become what judges the work" in xp.inner_text()
        xp.locator(".lm-depth-b[data-depth=full]").click()
        assert "Every changed part." in xp.inner_text()
        assert page.evaluate("window.__sent") == []
    finally:
        page.close()
