"""The work trail: steps between messages as one line on the thread's rail.

See src/web/static/js/work-trail.js. Runs on the REAL
index.html with every real script; only the network is faked. The rows the
trail stands for must stay where they are, hidden: everything else that walks
the thread depends on that.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._browser import shared_browser_scope
from tests import _fake_app

ROOT = Path(__file__).resolve().parents[1]
SID = "sess-1"


@pytest.fixture(scope="module")
def _browser():
    pytest.importorskip("playwright.sync_api")
    with shared_browser_scope() as b:
        yield b


STEPS = {
    "t1": {"label": "Run the toast tests"},
    "t2": {"file": "/x/utils.js", "patch": [{"newStart": 182, "lines": [" keep", "-old", "+new"]}]},
}


@pytest.fixture
def app(_browser):
    a = _fake_app.FakeApp(_browser, _fake_app.fake_sessions(1),
                          wait_for="typeof startLivePanel === 'function'", clock=True,
                          extra_routes={"/api/session-steps/": lambda r: (200, {"steps": STEPS})})
    # The splash is fading over the page for about a second: a real click waits for it.
    a.eval("() => { const s = document.getElementById('vn-splash'); if (s) s.remove(); }")
    a.eval("""() => {
      // Its own host: the app redraws #main-body by itself while it loads.
      const host = document.createElement('div');
      host.className = 'conversation';
      host.style.cssText = 'position:fixed;inset:0;z-index:99999;overflow:auto;background:#111;display:block;height:auto';
      host.innerHTML = '<div id="vt-test-log"></div>';
      document.body.appendChild(host);
      window.__log = document.getElementById('vt-test-log');
      liveSessionId = '%s';
      sessionKinds['%s'] = 'working';
      WorkTrail.attach(window.__log, '%s');
      window.__add = (e) => window.__log.appendChild(renderLiveEntry(e));
    }""" % (SID, SID, SID))
    yield a
    a.close()


def _add(app, *entries):
    for e in entries:
        app.eval("(e) => { window.__add(e); }", e)
    app.advance(30)


FETCH_MS = 700   # past FETCH_DEBOUNCE_MS, work-trail.js


def _words_arrived(app, step_id):
    """The steps route has answered and the page has drawn what it said."""
    app.page.wait_for_function(f"() => WorkTrail._steps.has({step_id!r})", timeout=2000)
    app.advance(30)


READ = {"kind": "tool_use", "id": "r1", "name": "Read", "desc": "/x/static/js/mobile.js"}
READ_OK = {"kind": "tool_result", "tool_use_id": "r1", "text": "line one"}
EDIT = {"kind": "tool_use", "id": "t2", "name": "Edit", "desc": "/x/utils.js"}
EDIT_OK = {"kind": "tool_result", "tool_use_id": "t2", "text": "updated"}
TEST = {"kind": "tool_use", "id": "t1", "name": "Bash", "desc": "pytest tests/x.py -q"}
TEST_BAD = {"kind": "tool_result", "tool_use_id": "t1", "text": "1 failed, 11 passed in 2s", "is_error": True}
TEST2 = {"kind": "tool_use", "id": "t3", "name": "Bash", "desc": "pytest tests/x.py -q"}
TEST_GOOD = {"kind": "tool_result", "tool_use_id": "t3", "text": "12 passed in 2s"}
SAY = {"kind": "asst", "text": "Found it."}


def test_rows_stay_in_the_thread_hidden_behind_one_line(app):
    _add(app, READ, READ_OK, EDIT, EDIT_OK)
    r = app.eval("""() => ({
      trails: __log.querySelectorAll(':scope > .vn-trail').length,
      rows: __log.querySelectorAll(':scope > .live-entry-tool, :scope > .live-entry-result').length,
      shown: [...__log.querySelectorAll(':scope > .live-entry-tool')].filter(n => n.offsetHeight > 0).length,
    })""")
    assert r == {"trails": 1, "rows": 4, "shown": 0}


def test_a_message_starts_a_new_trail_and_keeps_its_bubble(app):
    _add(app, READ, READ_OK, SAY, EDIT, EDIT_OK)
    r = app.eval("""() => ({
      order: [...__log.children].filter(n => n.offsetHeight > 0).map(n => n.classList.contains('vn-trail') ? 'trail' : n.classList.contains('msg') ? 'msg' : '?'),
      firstSettled: __log.querySelector('.vn-trail').classList.contains('vt-settled'),
      bubble: getComputedStyle(__log.querySelector('.msg.assistant .msg-body')).borderTopWidth,
    })""")
    assert [x for x in r["order"] if x != "?"] == ["trail", "msg", "trail"]
    assert r["firstSettled"] is True
    assert r["bubble"] == "1px"


def test_the_words_never_move_sideways_as_steps_land(app):
    """Alignment: the words must not shift right with every icon."""
    x = []
    for i in range(9):
        _add(app, {"kind": "tool_use", "id": "a%d" % i, "name": "Read", "desc": "/x/f%d.js" % i})
        x.append(app.eval("() => document.querySelector('.vt-lab').getBoundingClientRect().left"))
    assert len(set(x)) == 1, x
    assert app.eval("() => document.querySelectorAll('.vt-chip').length") == 6


def test_the_step_count_sits_on_one_line_with_its_word(app):
    """Alignment: the number must not ride above the word 'steps'."""
    _add(app, READ, READ_OK, EDIT)
    app.advance(350)
    app.animations_done()
    r = app.eval("""() => {
      const m = el => { const b = el.getBoundingClientRect(); return b.top + b.height / 2; };
      return [m(document.querySelector('.vt-cnt > span:last-child')), m(document.querySelector('.vt-cw'))];
    }""")
    assert abs(r[0] - r[1]) < 1, r


def test_live_line_says_what_it_is_doing_then_settles_into_a_sentence(app):
    _add(app, READ)
    assert "Reading mobile.js" in app.eval("() => document.querySelector('.vt-lab').textContent")
    assert app.eval("() => document.querySelector('.vn-trail').classList.contains('vt-live')")
    _add(app, READ_OK)
    app.eval("() => { sessionKinds['%s'] = 'idle'; WorkTrail.refresh(); }" % SID)
    app.advance(30)
    assert app.eval("() => document.querySelector('.vt-lab').textContent") == "Read mobile.js"
    assert app.eval("() => document.querySelector('.vn-trail').classList.contains('vt-settled')")


def test_a_failure_shows_on_the_line_and_a_later_pass_clears_it(app):
    _add(app, TEST, TEST_BAD)
    assert app.eval("() => document.querySelector('.vn-trail').classList.contains('vt-trouble')")
    _add(app, TEST2, TEST_GOOD)
    assert not app.eval("() => document.querySelector('.vn-trail').classList.contains('vt-trouble')")
    app.eval("() => { sessionKinds['%s'] = 'idle'; WorkTrail.refresh(); }" % SID)
    app.advance(FETCH_MS)
    _words_arrived(app, "t1")  # the transcript words
    text = app.eval("() => document.querySelector('.vt-lab').textContent")
    assert "1 of 12 failed" in text and "all 12 passed" in text
    assert text.startswith("Ran the toast tests"), text


def test_opening_shows_plain_steps_with_changes_open_and_the_raw_row_a_tap_away(app):
    _add(app, READ, READ_OK, EDIT, EDIT_OK)
    app.advance(FETCH_MS)
    _words_arrived(app, "t2")
    app.page.click(".vt-bar")
    app.advance(400)
    r = app.eval("""() => ({
      steps: [...document.querySelectorAll('.vt-step .tx')].map(n => n.textContent.trim()),
      dets: document.querySelectorAll('.vt-det').length,
      num: document.querySelector('.vt-diff .add .n').textContent,
    })""")
    assert r["steps"] == ["Read mobile.js", "Changed utils.js"]
    assert r["dets"] == 1            # the change opened itself; the read did not
    assert r["num"] == "183"         # real line numbers, from the transcript
    app.page.click(".vt-rawlink")
    app.advance(50)
    assert app.eval("() => document.querySelectorAll('.vt-raw .live-tool-line').length") == 1
    app.page.click(".vt-step")       # a read opens on a tap
    app.advance(50)
    assert app.eval("() => document.querySelectorAll('.vt-det').length") == 2


def test_with_smart_termination_off_the_thread_is_as_it_was(app):
    _add(app, READ, READ_OK)
    app.eval("() => applySmartTerminationView(false)")
    app.advance(30)
    r = app.eval("""() => ({
      trails: document.querySelectorAll('.vn-trail').length,
      shown: [...__log.querySelectorAll(':scope > .live-entry-tool')].filter(n => n.offsetHeight > 0).length,
    })""")
    assert r == {"trails": 0, "shown": 1}
    app.eval("() => applySmartTerminationView(true)")


def test_a_full_redraw_does_not_animate_and_keeps_one_line_per_run(app):
    _add(app, READ, READ_OK)
    app.eval("""(es) => { __log.innerHTML = ''; const f = document.createDocumentFragment();
       es.forEach(e => f.appendChild(renderLiveEntry(e))); __log.appendChild(f); }""",
             [READ, READ_OK, SAY, EDIT, EDIT_OK, SAY, TEST, TEST_BAD])
    app.advance(30)
    assert app.eval("() => document.querySelectorAll('.vn-trail').length") == 3
    assert app.eval("() => document.querySelectorAll('.vt-chip.new').length") == 0


def test_a_commands_words_read_as_past_and_present_without_mangling(app):
    # The session writes orders; only verbs known to the page change form.
    r = app.eval("""() => {
      const d = (label, result) => {
        WorkTrail._steps.set('v', { label });
        return WorkTrail._describe({ use: { id: 'v', name: 'Bash', desc: 'x' }, result });
      };
      return [d('Run the tests', {}).obj, d('Run the tests', null).doing, d('Fetch the page', {}).obj,
              d("Take a picture", null).doing, d('Git status', {}).obj, d('Stop the job', {}).obj];
    }""")
    assert r == ["Ran the tests", "Running the tests", "Fetched the page",
                 "Taking a picture", "Git status", "Stopped the job"]


SENT_BACK = {"kind": "sent_back", "name": "Incomplete response",
             "desc": "Its response was missing required parts.", "text": "Too long."}


def test_a_send_back_is_a_line_shaped_like_the_steps_line(app):
    """It reads as a step, with its details behind a tap."""
    _add(app, READ, READ_OK, {"kind": "asst", "text": "Done, I think."}, SENT_BACK)
    app.advance(30)
    r = app.eval("""() => {
      const t = document.querySelector('.vn-trail'), s = document.querySelector('.vn-sent-back');
      const left = n => Math.round(n.getBoundingClientRect().left);
      return { text: s.querySelector('.vn-sb-main').textContent,
               after: s.querySelector('.vn-sb-after').textContent,
               sameWords: left(t.querySelector('.vt-lab')) === left(s.querySelector('.vn-sb-main')),
               sameRail: left(t) === left(s),
               height: s.offsetHeight,
               bubbleGone: !__log.querySelector(':scope > .msg.assistant'),
               detailsShut: s.querySelector('.vn-sb-told').getBoundingClientRect().height < 1 || s.querySelector('.vn-sb-clip').offsetHeight === 0 };
    }""")
    assert r["text"] == "Invalid termination response \u00b7 incomplete response"
    assert r["after"] == "rewrote it"
    assert r["sameWords"] and r["sameRail"]
    assert r["height"] <= 40
    assert r["bubbleGone"]      # the message it tried to stop on moved inside
    assert r["detailsShut"]
    app.page.click(".vn-sb-toggle")
    app.advance(400)
    app.page.wait_for_function("() => document.querySelector('.vn-sb-told').offsetHeight > 0", timeout=3000)
    assert app.eval("() => document.querySelector('.vn-sb-told').offsetHeight > 0")
    assert app.eval("() => document.querySelector('.vn-sb-tried').textContent.includes('Done, I think.')")


# A Windows session's commands run in PowerShell and must not each be drawn as
# "Used PowerShell"; a file chip a step wrote must not stand under the trail as
# if it were a step of its own.
PS = {"kind": "tool_use", "id": "t1", "name": "PowerShell", "desc": "python -m pytest tests/x.py -q"}
PS_BAD = {"kind": "tool_result", "tool_use_id": "t1", "text": "1 failed, 11 passed in 2s", "is_error": True}


def test_a_powershell_command_reads_like_any_other_command(app):
    _add(app, READ, READ_OK, PS, PS_BAD)
    app.eval("() => { sessionKinds['%s'] = 'idle'; WorkTrail.refresh(); }" % SID)
    app.advance(FETCH_MS)
    _words_arrived(app, "t1")       # the transcript words arrive
    text = app.eval("() => document.querySelector('.vt-lab').textContent")
    assert "PowerShell" not in text, text
    assert text.startswith("Ran the toast tests: 1 of 12 failed"), text
    assert app.eval("() => document.querySelector('.vn-trail').classList.contains('vt-trouble')")


def test_every_order_in_a_commands_words_takes_the_same_form(app):
    r = app.eval("""() => {
      WorkTrail._steps.set('v', { label: 'Run banner tests and drive the demo' });
      const t = { use: { id: 'v', name: 'Bash', desc: 'x' } };
      return [WorkTrail._describe({ ...t, result: null }).doing, WorkTrail._describe({ ...t, result: {} }).obj];
    }""")
    assert r == ["Running banner tests and driving the demo", "Ran banner tests and drove the demo"]


def test_a_file_chip_inside_a_run_is_part_of_the_trail(app):
    write = {"kind": "tool_use", "id": "w1", "name": "Write", "desc": "/x/demo.html"}
    _add(app, READ, READ_OK, write)
    app.eval("""() => { const c = document.createElement('div'); c.className = 'live-output-card-inline';
      c.innerHTML = '<span class="live-output-card-name">demo.html</span>'; __log.appendChild(c); }""")
    _add(app, {"kind": "tool_result", "tool_use_id": "w1", "text": "File created"})
    r = app.eval("""() => ({
      trails: __log.querySelectorAll(':scope > .vn-trail').length,
      chipShown: [...__log.querySelectorAll(':scope > .live-output-card-inline')].filter(n => n.offsetHeight > 0).length,
    })""")
    assert r == {"trails": 1, "chipShown": 0}
    app.page.click(".vt-bar")
    app.advance(400)
    app.animations_done()
    assert app.eval("() => document.querySelector('.vt-files .live-output-card-name').textContent") == "demo.html"


def test_a_list_of_orders_reads_as_one_tense(app):
    r = app.eval("""() => {
      WorkTrail._steps.set('v', { label: 'Save screenshots, rerun tests, check rules' });
      return WorkTrail._describe({ use: { id: 'v', name: 'Bash', desc: 'x' }, result: {} }).obj;
    }""")
    assert r == "Saved screenshots, reran tests, checked rules"


def test_the_clis_note_on_a_picture_stays_inside_the_run(app):
    """The CLI adds "[Image: original WxH, displayed at ...]" after a Read of a
    picture; it was drawn as a pill and split the run in two."""
    pic = {"kind": "tool_use", "id": "p1", "name": "Read", "desc": "/x/shot.png"}
    _add(app, pic, {"kind": "tool_result", "tool_use_id": "p1", "text": ""},
         {"kind": "user", "text": "[Image: original 780x2888, displayed at 540x2000. Multiply coordinates by 1.44 to map to original image.]"},
         READ, READ_OK)
    r = app.eval("""() => ({
      trails: __log.querySelectorAll(':scope > .vn-trail').length,
      pills: [...__log.querySelectorAll(':scope > .live-interrupted')].filter(n => n.offsetHeight > 0).length,
    })""")
    assert r == {"trails": 1, "pills": 0}


def test_a_real_stop_pill_still_shows(app):
    _add(app, READ, READ_OK, {"kind": "user", "text": "[Request interrupted by user]"})
    assert app.eval("() => [...__log.querySelectorAll(':scope > .live-interrupted')].filter(n => n.offsetHeight > 0).length") == 1


def test_a_stop_is_a_thin_divider_saying_you_stopped_it(app):
    """A hairline
    across the thread with "Stopped by you" and the time, from either form the
    stop arrives in, never the old bold pill."""
    _add(app, READ, READ_OK,
         {"kind": "user", "text": "[Request interrupted by user]", "timestamp": "2026-11-03T19:35:00"},
         {"kind": "system", "text": "Request interrupted by user", "timestamp": "2026-11-03T19:36:00"})
    r = app.eval("""() => [...__log.querySelectorAll(':scope > .live-interrupted')].map(n => ({
      divider: n.classList.contains('vn-stopped'), pill: !!n.querySelector('.live-interrupted-pill'),
      text: n.textContent, line: getComputedStyle(n, '::before').height, shown: n.offsetHeight > 0 }))""")
    assert len(r) == 2
    for d in r:
        assert d["divider"] and not d["pill"] and d["shown"] and d["line"] == "1px"
        assert d["text"].startswith("Stopped by you") and "PM" in d["text"]
    import os
    shot = os.environ.get("VN_STOP_SHOT")
    if shot:
        app.page.locator("#vt-test-log").screenshot(path=shot)


def test_common_tools_have_plain_words(app):
    r = app.eval("""() => ['ListAgents', 'SendMessage', 'mcp__claude_ai_Google_Drive__search_files', 'Mystery'].map(name => {
      const w = WorkTrail._describe({ use: { id: 'o-' + name, name, desc: '' }, result: {} });
      return w.verb + ' ' + w.obj; })""")
    assert r == ["Checked its helpers", "Sent a helper a message",
                 "Used search files (Google Drive)", "Used Mystery"]


# ---- Fold by default: the guarantee ----
# Between two steps, every row is folded unless it is meant for the person. These tests
# hold that for every kind the renderer draws, for rows it does not draw, and
# for a kind added later: a new e.kind in live-panel.js fails the first test
# until it is listed below as folded or as staying visible.

FOLDED_KINDS = {
    "tool_use": {"kind": "tool_use", "id": "f-use", "name": "Mystery", "desc": "?"},
    "tool_result": {"kind": "tool_result", "tool_use_id": "f-use", "text": "ok"},
    "system": {"kind": "system", "text": "Some CLI notice"},
    "system error": {"kind": "system", "text": "Something broke", "is_error": True},
    "permission": {"kind": "permission", "text": "Auto-approved (server-policy) Bash: ls"},
    "picture note": {"kind": "user", "text": "[Image: original 780x2888, displayed at 540x2000.]"},
}
VISIBLE_KINDS = {
    "asst": {"kind": "asst", "text": "Said something."},
    "user": {"kind": "user", "text": "hello"},
    "stream": {"kind": "stream", "text": "typing"},
    "sent_back": SENT_BACK,
    "stop pill": {"kind": "user", "text": "[Request interrupted by user]"},
    "directive_conflict": {"kind": "directive_conflict", "conflict": {}, "new_directive": {}, "existing_directive": {}},
}


def test_every_kind_the_renderer_draws_is_classified():
    import re
    src = (ROOT / "src/web/static/js/live-panel.js").read_text(encoding="utf-8")
    kinds = set(re.findall(r"e\.kind === '([a-z_]+)'", src))
    known = {v["kind"] for v in list(FOLDED_KINDS.values()) + list(VISIBLE_KINDS.values())}
    assert kinds <= known, ("New row kind(s) %s: decide whether they fold into the step line or stay "
                            "visible (static/js/work-trail.js _SAID/_FRAME) and list them here." % sorted(kinds - known))


def _visible_children(app):
    return app.eval("""() => [...__log.children].filter(n => !n.classList.contains('vt-hidden') && n.offsetHeight > 0)
      .map(n => n.classList.contains('vn-trail') ? 'trail' : n.className)""")


@pytest.mark.parametrize("name", sorted(FOLDED_KINDS))
def test_a_folded_kind_between_steps_never_shows(app, name):
    _add(app, READ, READ_OK, FOLDED_KINDS[name], EDIT, EDIT_OK)
    assert _visible_children(app) == ["trail"]


def test_a_row_nobody_has_heard_of_folds_too(app):
    _add(app, READ, READ_OK)
    app.eval("""() => { for (const cls of ['vn-some-future-row', 'live-entry live-entry-result', 'live-output-card-inline']) {
      const d = document.createElement('div'); d.className = cls; d.textContent = 'x'; __log.appendChild(d); } }""")
    _add(app, EDIT, EDIT_OK)
    assert _visible_children(app) == ["trail"]


@pytest.mark.parametrize("name", sorted(VISIBLE_KINDS))
def test_his_things_stay_and_split_the_steps(app, name):
    _add(app, READ, READ_OK, VISIBLE_KINDS[name], EDIT, EDIT_OK)
    seen = _visible_children(app)
    assert seen[0] == "trail" and seen[-1] == "trail" and len(seen) == 3, seen


def test_a_result_whose_step_is_on_an_earlier_page_folds(app):
    _add(app, {"kind": "tool_result", "tool_use_id": "gone", "text": "done"}, SAY, READ, READ_OK)
    seen = _visible_children(app)
    assert seen[0] == "trail" and seen.count("trail") == 2, seen


def test_a_folded_failure_turns_the_line_amber_and_is_listed_when_opened(app):
    _add(app, READ, READ_OK, FOLDED_KINDS["system error"])
    assert app.eval("() => document.querySelector('.vn-trail').classList.contains('vt-trouble')")
    app.page.click(".vt-bar")
    app.advance(400)
    app.animations_done()
    assert app.eval("() => document.querySelector('.vt-note.err').textContent") == "Something broke"


def test_a_system_line_that_says_nothing_is_not_drawn(app):
    _add(app, SAY, {"kind": "system", "text": "System message"}, {"kind": "asst", "text": "More."})
    assert [x for x in _visible_children(app) if "msg" not in x] == []


def test_other_steps_are_named_while_they_fit(app):
    _add(app, {"kind": "tool_use", "id": "m1", "name": "Monitor", "desc": ""}, {"kind": "tool_result", "tool_use_id": "m1", "text": ""},
         {"kind": "tool_use", "id": "m2", "name": "TaskStop", "desc": ""}, {"kind": "tool_result", "tool_use_id": "m2", "text": ""})
    app.eval("() => { sessionKinds['%s'] = 'idle'; WorkTrail.refresh(); }" % SID)
    app.advance(50)
    assert app.eval("() => document.querySelector('.vt-lab').textContent") == "Watched a background job and stopped a background task"


# ---- Several send-backs in a row: one line that adds them up ----
SB_LONG = {"kind": "sent_back", "name": "Too long", "desc": "Too long.", "text": "Too long: 160 words."}
SB_CLAIM = {"kind": "sent_back", "name": "Claim not backed", "desc": "A claim.", "text": "Claim not backed."}


def _shown(app):
    return app.eval("""() => [...__log.children].filter(n => n.offsetHeight > 0).map(n =>
      n.classList.contains('vn-sb-group') ? 'group' : n.classList.contains('vn-sent-back') ? 'sb'
      : n.classList.contains('vn-trail') ? 'trail' : n.classList.contains('msg') ? 'msg' : n.className)""")


def test_send_backs_in_a_row_are_one_line_that_counts_them(app):
    _add(app, READ, READ_OK, {"kind": "asst", "text": "Try one"}, SB_LONG,
         {"kind": "asst", "text": "Try two"}, SB_LONG, {"kind": "asst", "text": "Try three"}, SB_CLAIM,
         {"kind": "asst", "text": "Final."})
    app.advance(50)
    assert _shown(app) == ["trail", "group", "msg"]
    line = app.eval("() => document.querySelector('.vn-sb-group .vn-sb-head').textContent")
    assert "×3" in line and "too long, claim not backed" in line and "rewrote it 3 times" in line, line
    # Each attempt's own card stays inside its own row, nothing lost.
    assert app.eval("() => [...document.querySelectorAll('.vn-sent-back .vn-sb-tried')].map(n => n.textContent.trim())") == \
        ["Try one", "Try two", "Try three"]


def test_opening_the_line_shows_each_send_back_in_place(app):
    _add(app, {"kind": "asst", "text": "a"}, SB_LONG, {"kind": "asst", "text": "b"}, SB_CLAIM)
    app.advance(50)
    app.page.click(".vn-sbg-head")
    app.advance(50)
    assert _shown(app) == ["group", "sb", "sb"]
    app.page.click(".vn-sent-back .vn-sb-toggle")   # each still opens its own details
    app.advance(400)
    app.animations_done()
    assert app.eval("() => document.querySelector('.vn-sent-back .vn-sb-told').offsetHeight > 0")
    app.page.click(".vn-sbg-head")
    app.advance(50)
    assert _shown(app) == ["group"]


def test_a_single_send_back_or_one_split_by_steps_is_left_alone(app):
    _add(app, {"kind": "asst", "text": "a"}, SB_LONG, READ, READ_OK, {"kind": "asst", "text": "b"}, SB_CLAIM)
    app.advance(50)
    assert _shown(app) == ["sb", "trail", "sb"]


def test_a_new_send_back_joins_the_line_and_the_count_goes_up(app):
    _add(app, {"kind": "asst", "text": "a"}, SB_LONG, {"kind": "asst", "text": "b"}, SB_LONG)
    app.advance(50)
    assert "×2" in app.eval("() => document.querySelector('.vn-sb-group').textContent")
    assert "twice" in app.eval("() => document.querySelector('.vn-sb-group').textContent")
    _add(app, {"kind": "asst", "text": "c"}, SB_CLAIM)
    app.advance(50)
    assert app.eval("() => document.querySelectorAll('.vn-sb-group').length") == 1
    assert "×3" in app.eval("() => document.querySelector('.vn-sb-group').textContent")
