"""Handoff evidence (daemon/handoff_evidence.py): the card's claims are
checked against the session's own tool calls, and the card must account for
every request the user made in this task."""

import copy
import json
from types import SimpleNamespace as E

import pytest

from daemon import handoff_evidence as ev
from daemon import handoff_rules as hr


def msg(card):
    return "```handoff\n" + json.dumps(card) + "\n```"


def user(text):
    return E(kind="user", text=text)


def asst(text):
    return E(kind="asst", text=text)


_n = iter(range(10_000))


def tool(name, desc, output="", error=False):
    i = f"t{next(_n)}"
    return [E(kind="tool_use", name=name, desc=desc, id=i),
            E(kind="tool_result", tool_use_id=i, text=output, is_error=error)]


def entries(*parts):
    out = []
    for p in parts:
        out.extend(p if isinstance(p, list) else [p])
    return out


def facts(*parts, trimmed=False):
    return ev.facts_from_entries(entries(*parts), trimmed=trimmed)


DONE = {
    "type": "done",
    "title": "Sort fix shipped",
    "headline": "CustomerNode's sort fix is in and the tests pass",
    "context": "the journey picker sorted by status in a strange order.",
    "did": [{"what": "Sorted status by the shown name", "why": "It sorted by the raw code before."}],
    "verified": [{"check": "Backend suite", "result": "All 412 tests passed.", "ok": True}],
    "links": [{"label": "The sort fix commit", "kind": "commit", "url": "abc1234"}],
    "validate": {"how": "Sort the journey picker by status", "url": "http://localhost:8000/journeys"},
    "needs": {"kind": "none", "why": "It is finished.", "next": "It ships with the next release."},
}

TESTS_PASSED = tool("Bash", "python3 -m pytest tests/ -q", "412 passed in 31.2s")
TESTS_FAILED = tool("Bash", "python3 -m pytest tests/ -q", "3 failed, 409 passed in 30.9s", error=True)


# ── facts_from_entries ────────────────────────────────────────────────────

def test_actions_pair_calls_with_results():
    f = facts(user("fix the status sort please"), TESTS_FAILED)
    assert len(f.actions) == 1
    a = f.actions[0]
    assert a.tool == "Bash" and "pytest" in a.detail and a.ok is False and "3 failed" in a.output


def test_requests_skip_acks_injected_preambles_and_system_text():
    f = facts(
        user("⟦VN-MOBILE⟧ IMPORTANT — HEADLESS MOBILE SESSION ⟦/VN-MOBILE⟧fix the sort on the journey picker"
             "\n\nSent from Q at 2026-10-27 05:44 PM"),
        user("yes"), user("go ahead"), user("<task-notification>done</task-notification>"),
        user("and make Other the last choice in the menus"),
    )
    assert f.requests == ["fix the sort on the journey picker", "and make Other the last choice in the menus"]


def test_a_screenshot_note_from_the_cli_is_not_a_request():
    # 2026-10-28: the CLI's note on a screenshot the session read was listed
    # back to the session as a request it had left out.
    f = facts(user("these are all showing as the same, please fix"),
              user("[Image: original 700x3400, displayed at 412x2000. Multiply coordinates by 1.70 to map to original image.]"))
    assert f.requests == ["these are all showing as the same, please fix"]
    assert f.last_user == "these are all showing as the same, please fix"


def test_requests_start_after_the_last_card_that_closed_a_task():
    closed = msg(dict(DONE, headline="The old task is done"))
    f = facts(user("an old request about something else"), asst("Done.\n\n" + closed),
              user("new request one about menus"), user("new request two about sorting"))
    assert f.requests == ["new request one about menus", "new request two about sorting"]


def test_a_needs_you_card_does_not_close_the_task():
    open_card = msg({"type": "needs_you", "headline": "x", "context": "y", "needs": {"kind": "decision"}})
    f = facts(user("first request about the menus"), asst(open_card), user("second request about sorting"))
    assert len(f.requests) == 2


# ── 1. Claims against actions ─────────────────────────────────────────────

def test_tests_pass_with_a_passing_run_is_fine():
    assert ev.problems(DONE, facts(user("fix the status sort"), TESTS_PASSED)) == []


def test_tests_pass_with_no_run_is_sent_back():
    p = ev.problems(DONE, facts(user("fix the status sort"), tool("Edit", "app/sort.py")))
    assert len(p) == 1 and "no test run" in p[0]


def test_tests_pass_when_the_latest_run_failed_is_sent_back():
    p = ev.problems(DONE, facts(user("fix the status sort"), TESTS_PASSED, tool("Edit", "app/sort.py"), TESTS_FAILED))
    assert len(p) == 1 and "latest test run" in p[0] and "3 failed" in p[0]


def test_a_failed_run_then_a_passing_run_is_fine():
    assert ev.problems(DONE, facts(user("fix the status sort"), TESTS_FAILED, TESTS_PASSED)) == []


def test_a_background_run_counts_by_the_summary_read_later():
    bg = tool("Bash", "python3 -m pytest tests/ -q", "Command running in background with ID: b12")
    read_ok = tool("Read", "/tmp/claude-1000/tasks/b12.output", "======= 412 passed in 30.1s =======")
    read_bad = tool("Read", "/tmp/claude-1000/tasks/b12.output", "======= 2 failed, 410 passed =======")
    assert ev.problems(DONE, facts(user("fix the status sort"), bg, read_ok)) == []
    assert "latest test run" in ev.problems(DONE, facts(user("fix the status sort"), bg, read_bad))[0]
    # Launched but never read: no result to stand on.
    assert "no test run" in ev.problems(DONE, facts(user("fix the status sort"), bg))[0]


@pytest.mark.parametrize("out,passed", [
    ("frontend passed passed 13733 failed 0 130.1 s", True),
    ("backend passed passed=12108 failed=0 skipped=0", True),
    ("backend failed passed=12100 failed=8 skipped=0", False),
    ("====== 3 failed, 409 passed in 30s ======", False),
    ("Tests:  2 failed, 98 passed, 100 total", False),
    ("# pass 40\n# fail 0", True),
    ("# pass 38\n# fail 2", False),
])
def test_runner_summaries_of_both_shapes(out, passed):
    assert ev.test_runs(facts(tool("Read", "/tmp/run.output", out)))[0][0] is passed


def test_only_the_final_summary_line_decides():
    log = ("E   psycopg.OperationalError: connection to server at localhost:55432 failed\n"
           "retrying...\n"
           "5 passed, 7 warnings in 8.35s")
    assert ev.test_runs(facts(tool("Bash", "pytest tests/test_x.py", log)))[0][0] is True


@pytest.mark.parametrize("cmd", ["npm test", "npx vitest run", "node tests/test_thread_scroll.js",
                                 "go test ./...", "cargo test", "./scripts/run_tests.sh", "bash scripts/test_all.sh"])
def test_test_runners_are_recognised(cmd):
    assert ev.test_runs(facts(tool("Bash", cmd, "ok"))) == [(True, cmd[:80])]


@pytest.mark.parametrize("cmd", ["cat tests/test_sort.py", "sed -n 1,40p tests/test_sort.py", "git add tests/"])
def test_reading_a_test_file_is_not_a_run(cmd):
    assert ev.test_runs(facts(tool("Bash", cmd, "def test_x(): ..."))) == []


def test_no_evidence_is_not_concluded_when_history_was_trimmed():
    assert ev.problems(DONE, facts(user("fix the status sort"), trimmed=True)) == []
    # A contradiction still counts: the failed run is right there.
    assert ev.problems(DONE, facts(user("fix the status sort"), TESTS_FAILED, trimmed=True))


@pytest.mark.parametrize("text", [
    "Tests were not run yet.", "No tests pass on Windows before the fix.",
    "Tests passed earlier today on the old branch.", "Another session pushed its branch.",
])
def test_negated_and_earlier_claims_are_not_claims(text):
    assert ev.claims({"headline": text}) == []


# Each of these was a false alarm when replaying the owner's real history.
@pytest.mark.parametrize("text", [
    "Nothing is committed or pushed.",
    "It's only on my local branch and hasn't been pushed anywhere.",
    "Once the branch is pushed I'll send the email.",
    "The new values are pushed to every open browser straight away.",
    "I haven't watched either state run in a live browser session.",
    "Neither is tested on a phone yet.",
    "I'll check the result myself with screenshots of the real app.",
    "The final test pass and the re-review are both running.",
    "Every suite still runs, and a run still only passes if every test passes.",
    "Full frontend run: 9,209 of 9,213 tests pass.",
    "How to start it, sign in, and take a screenshot.",
    "On red: the retried test's first-pass error.",
])
def test_not_claims_from_the_replay(text):
    assert ev.claims({"headline": text}) == []


@pytest.mark.parametrize("text,kind", [
    ("Linked from the README and pushed (`e814454`).", "pushed"),
    ("CustomerNode's sort fix is pushed to develop.", "pushed"),
    ("5297 passed.", "tests"),
    ("I took screenshots at both widths.", "app"),
])
def test_real_claims_from_the_replay(text, kind):
    assert [k for k, _, _ in ev.claims({"headline": text})] == [kind]


def test_a_refused_tool_call_is_not_a_run():
    refused = tool("Bash", "python3 -m pytest tests/", "The user doesn't want to proceed with this tool use.", error=True)
    assert ev.test_runs(facts(refused)) == []


def test_a_file_that_mentions_failures_is_not_a_run():
    assert ev.test_runs(facts(tool("Read", "memory/notes.md", "FIXED: the gallery check had 1 failed seed"))) == []
    # Even a runner-shaped line quoted in notes is not a run.
    assert ev.test_runs(facts(tool("Bash", "cat memory/e2e.md", "=== 3 failed, 9 passed in 2.0s ==="))) == []


@pytest.mark.parametrize("text", [
    "The new tests and the scanner both failed on the old code and pass on the fix.",
    "I re-added the count line to confirm it goes red, then restored it. The tests pass.",
    "Every guard was proven red, then the tests pass.",
])
def test_red_first_wording_is_recognised(text):
    card = dict(DONE, headline="CustomerNode's sort guard is in", verified=[{"check": "Tests", "result": text, "ok": True}])
    red_last = tool("Bash", "python3 -m pytest tests/test_guard.py", "1 failed in 0.4s", error=True)
    assert ev.problems(card, facts(user("add the guard"), TESTS_PASSED, red_last)) == []


def test_red_first_proofs_need_a_green_run_not_a_green_last_run():
    card = dict(DONE, headline="CustomerNode's sort guard is in", verified=[
        {"check": "Tests seen red first, now green", "result": "Reintroduced the bug: red. Restored: green.", "ok": True}])
    red_last = tool("Bash", "python3 -m pytest tests/test_guard.py", "1 failed in 0.4s", error=True)
    assert ev.problems(card, facts(user("add the guard"), TESTS_PASSED, red_last)) == []
    assert "no passing" in ev.problems(card, facts(user("add the guard"), red_last))[0]


def test_app_claim_needs_a_browser_step():
    card = dict(DONE, verified=[{"check": "Checked it in the real app on a phone width", "result": "Sorted.", "ok": True}],
                headline="CustomerNode's sort fix is in")
    assert "browser" in ev.problems(card, facts(user("fix the status sort")))[0]
    for step in (tool("mcp__playwright__browser_navigate", "url: http://127.0.0.1:5050"),
                 tool("Bash", "curl -s -X POST http://127.0.0.1:5050/api/preview/screenshot"),
                 tool("Read", "/tmp/shot-phone.png")):
        assert ev.problems(card, facts(user("fix the status sort"), step)) == []


def test_ordinary_in_the_app_wording_is_not_a_claim():
    assert ev.claims({"did": [{"what": "Added a switch in the app's Preferences"}]}) == []


def test_pushed_claim_needs_a_push_that_went_through():
    card = dict(DONE, headline="CustomerNode's sort fix is pushed to develop", verified=[{"check": "Read the code", "result": "ok", "ok": True}])
    assert "push" in ev.problems(card, facts(user("fix the status sort")))[0]
    refused = tool("Bash", "git push origin develop", "pre-push hook failed", error=True)
    assert "push" in ev.problems(card, facts(user("fix the status sort"), refused))[0]
    assert ev.problems(card, facts(user("fix the status sort"), tool("Bash", "git push origin develop", "ok"))) == []
    # Pushed from another folder, by a release script, or by a sub-agent.
    for step in (tool("Bash", "git -C /srv/wt push origin HEAD:develop", "ok"),
                 tool("Bash", "./release.sh 847", "To github.com:o/r.git\n   1a2b3c4d..5e6f7a8b  develop -> develop"),
                 tool("Agent", "Push the branch", "Pushed fix/sort to develop; CI started.")):
        assert ev.problems(card, facts(user("fix the status sort"), step)) == []
    # A sub-agent that says it did NOT push is not evidence.
    assert ev.problems(card, facts(user("fix the status sort"), tool("Agent", "Push it", "Not pushed: hook refused.")))


# ── 2. The card against the requests ──────────────────────────────────────

REQS = (user("on the journey picker, when we sort by status its ordering is weird"),
        user("also on dropdown menus in stage tables, always render Other as the last choice"))


def test_one_request_needs_no_asks():
    assert ev.problems(DONE, facts(REQS[0], TESTS_PASSED)) == []


def test_several_requests_need_asks():
    p = ev.problems(DONE, facts(*REQS, TESTS_PASSED))
    assert len(p) == 1 and '"asks"' in p[0] and "Other as the last choice" in p[0]


def test_every_request_must_appear_in_asks():
    card = dict(DONE, asks=[{"ask": "sort by status ordering on the journey picker", "state": "done"}])
    p = ev.problems(card, facts(*REQS, TESTS_PASSED))
    assert len(p) == 1 and "leaves out" in p[0] and "2." in p[0] and "dropdown" in p[0]


def test_all_requests_covered_passes():
    card = dict(DONE, asks=[{"ask": "journey picker status sort ordering", "state": "done"},
                            {"ask": "Other last in stage table dropdown menus", "state": "done"}])
    assert ev.problems(card, facts(*REQS, TESTS_PASSED)) == []


def test_a_done_card_cannot_have_an_open_ask():
    card = dict(DONE, asks=[{"ask": "journey picker status sort ordering", "state": "done"},
                            {"ask": "Other last in stage table dropdown menus", "state": "blocked"}])
    assert any("not done" in p for p in ev.problems(card, facts(*REQS, TESTS_PASSED)))


def test_a_needs_you_card_may_carry_open_asks():
    card = {"type": "needs_you", "headline": "Pick the order", "context": "menus",
            "asks": [{"ask": "journey picker status sort ordering", "state": "done"},
                     {"ask": "Other last in stage table dropdown menus", "state": "needs_you"}],
            "needs": {"kind": "decision"}}
    assert ev.problems(card, facts(*REQS)) == []


def test_bad_state_is_sent_back():
    card = dict(DONE, asks=[{"ask": "journey picker status sort ordering", "state": "mostly"},
                            {"ask": "Other last in stage table dropdown menus", "state": "done"}])
    assert any('"state"' in p for p in ev.problems(card, facts(*REQS, TESTS_PASSED)))


def test_a_repeated_request_must_be_done():
    reqs = (user("fix the open issues you left on the token meter"),
            user("add the FAQ sentence about fair use"),
            user("fix the open issues you left please, I already asked"))
    card = {"type": "needs_you", "headline": "Pick the FAQ wording", "context": "the token meter",
            "asks": [{"ask": "fix the open issues you left on the meter", "state": "needs_you"},
                     {"ask": "FAQ sentence about fair use", "state": "needs_you"}],
            "needs": {"kind": "decision"}}
    p = ev.problems(card, facts(*reqs))
    assert any("asked for this" in x and "open issues" in x for x in p)
    assert not any("fair use" in x and "asked for this" in x for x in p)


def test_repeat_markers_count_even_without_a_twin():
    assert ev.repeats(["what part of come back when it is all done was unclear"]) == {0: 2}
    assert ev.repeats(["so again, you need to stop stopping and finish"]) == {0: 2}
    assert ev.repeats(["the topnav right elements still dont look aligned"]) == {0: 2}
    assert ev.repeats(["fix the sort", "add a menu item for export"]) == {}


@pytest.mark.parametrize("text", [
    "Check if edit and write work again",
    "those should again use the right icon from the left panel",
])
def test_plain_again_is_not_a_repeat(text):
    assert ev.repeats([text]) == {}


def test_app_events_and_watchdog_nudges_are_not_requests():
    f = facts(user('⟦VN-EVENT source="Release" kind="release.live"⟧ cn.v.848 is live. Health reports ok.'),
              user("[VibeNode watchdog] Your previous turn produced no output. Continue where you left off."),
              user("fix the sort on the journey picker"))
    assert f.requests == ["fix the sort on the journey picker"]


# ── Through handoff_rules.review ──────────────────────────────────────────

def test_review_reports_evidence_and_skips_it_without_facts():
    assert hr.review(msg(DONE)) is None   # no facts: format and phrases only
    reason = hr.review(msg(DONE), facts=facts(user("fix the status sort"), TESTS_FAILED))
    assert reason and "against what actually happened" in reason and "3 failed" in reason


def test_asks_quote_the_user_and_are_not_phrase_checked():
    card = dict(DONE, asks=[{"ask": "fix the open issues you left, no honest caveats", "state": "done"},
                            {"ask": "journey picker status sort ordering", "state": "done"}])
    f = facts(user("fix the open issues you left, no honest caveats or flags"), REQS[0], TESTS_PASSED)
    assert hr.review(msg(card), facts=f) is None


# ── 3. Lines that must make sense on their own (2026-10-27) ─────────────────
# "Pick a button design for the card": "idek what that is referring to ...
# fix that systemically".  The headline and the ask are shown alone in the
# sidebar, the Needs you list and notifications.

@pytest.mark.parametrize("headline", [
    "Pick a button design for the card",
    "It is fixed", "This is ready for review", "It is fixed in VibeNode",
    "The fix is live", "Approve the change", "Merge the branch?", "Reload the page",
    "Three new designs for the card's buttons are on a test page",
    "The sort fix is in and the tests pass",
])
def test_headlines_that_lean_on_the_session_are_sent_back(headline):
    p = ev.standalone_problems({"headline": headline})
    assert p and "on their own" in p[0]


@pytest.mark.parametrize("headline", [
    "Pick a design for the buttons on VibeNode's end-of-turn summary card",
    "Double-tapping a session now sleeps it, even while it is working",
    "The VibeNode sleep fix is live", "CustomerNode release 849 is live",
    "CustomerNode's sort fix is in and the tests pass",
    "Approve the change to CustomerNode's push guard",
    "Merge the smart-termination branch into develop?",
    "The push guard no longer messages sessions",
    "The card sessions end with now has a Review button",
    "I need a GitHub login before I can open the pull request",
])
def test_headlines_that_say_which_thing_pass(headline):
    assert ev.standalone_problems({"headline": headline}) == []


@pytest.mark.parametrize("ask,bad", [
    ("Which one?", True), ("Review the options", True),
    ("Which button design should VibeNode's summary card use?", False),
])
def test_the_ask_must_stand_alone_too(ask, bad):
    card = {"headline": "VibeNode's summary card buttons", "needs": {"ask": ask}}
    assert bool(ev.standalone_problems(card)) is bad


def test_the_standalone_check_runs_without_session_facts():
    # The settings-file hook calls review() with no facts; this check must still run.
    card = dict(DONE, headline="The fix is live")
    reason = hr.review(msg(card))
    assert reason and "on their own" in reason


def test_an_ask_a_later_message_replaced_counts_as_settled():
    # A queued message that changes course: the first ask was not done as
    # first asked, and a done card may say so (2026-10-27).
    from daemon import handoff_evidence as he
    assert "replaced" in he.ASK_STATES and "replaced" in he.ASK_DONE_STATES
