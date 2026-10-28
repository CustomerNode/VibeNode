"""Handoff rules (daemon/handoff_rules.py): a turn ends with one card of an
allowed type, and each type has phrases that send the session back.

The phrases come from the owner's chat history: the messages right before
the replies where he swore.  The parametrized list below is every phrase
from the two analyses (2026-10-27); each must be caught in a "done" card.
"""

import copy
import json

import pytest

from daemon import handoff_rules as hr


def msg(card, prose=""):
    return prose + "```handoff\n" + json.dumps(card, indent=1) + "\n```\n"


DONE = {
    "type": "done",
    "title": "Smart termination built", "path": ["Preferences"],
    "headline": "Smart termination is built: sessions end with a summary you can act on",
    "context": "stopping sessions from ending on messages you can't follow without reading the thread.",
    "did": [{"what": "Added a Smart termination switch to Preferences, on by default",
             "why": "So it can be turned off per machine."}],
    "verified": [{"check": "22 new tests", "result": "All pass.", "ok": True},
                 {"check": "Full test suite", "result": "All 2,100 pass.", "ok": True}],
    "links": [{"label": "daemon/smart_termination.py", "kind": "file", "url": "daemon/smart_termination.py"}],
    "validate": {"how": "Turn on Smart termination in Preferences and end a turn",
                 "url": "/static/scratch/smart-termination-demo.html",
                 "later": "For real: the next time any session ends a turn."},
    "needs": {"kind": "none", "why": "It is finished and nothing is waiting on you.",
              "next": "It goes live the next time the Session Engine restarts."},
}

DECISION = {
    "type": "needs_you",
    "title": "Preview indicator", "path": ["Session list"],
    "headline": "Pick how the preview shows it is open or closed",
    "context": "the Trial page's preview panel: it should be obvious whether the preview is expanded.",
    "status": {"done": ["Built both designs on the review page"],
               "remaining": ["Build the one you pick and delete the other"]},
    "needs": {
        "kind": "decision",
        "ask": "Which one should the preview use?",
        "why": "Both are built; it is a look-and-feel call.",
        "look": [{"label": "Review page: both designs side by side", "kind": "link",
                  "url": "http://127.0.0.1:8000/app/scratch/review?trial=spaceOpens"}],
        "replies": [
            {"key": "B", "label": "A chevron button on the preview's right edge", "reply": "Go with the chevron"},
            {"key": "C", "label": "Expanded and Collapsed text links in the header", "reply": "Go with the links"},
        ],
        "next": "I build the one you pick and remove the other. About 10 minutes.",
    },
}

APPROVE = {
    "type": "needs_you",
    "title": "Push guard messages", "path": ["Publishing"],
    "headline": "The push guard no longer messages sessions. Approve and it messages only the session that broke the build",
    "context": "the automatic test check that runs after each push.",
    "compare": {"title": "How the push guard works", "rows": [
        {"topic": "When it goes red",
         "before": "Every red went to the session that pushed, even if unrelated to its commit",
         "after": "Only the session whose commit caused it is told"}]},
    "needs": {"kind": "approve", "ask": "Commit CustomerNode's push-guard change and build the targeted version?",
              "why": "It changes what every session hears after a push.",
              "replies": [{"key": "✓", "label": "Go", "reply": "go"},
                          {"key": "–", "label": "Not yet", "reply": "not yet"}],
              "next": "Until you answer, sessions hear nothing about the check."},
}

ACTION = {
    "type": "needs_you",
    "title": "GitHub login", "path": ["Smart termination", "Pull request"],
    "headline": "I need a GitHub login before I can open the pull request",
    "context": "publishing the Smart termination change as a pull request so CI runs on it.",
    "status": {"done": ["Branch pushed: smart-termination"], "remaining": ["Open the pull request"]},
    "needs": {"kind": "action", "ask": "Log the GitHub CLI in on this machine",
              "why": "Logging in needs your browser and your GitHub account.",
              "steps": [{"text": "Run in a terminal, then follow the prompts", "command": "gh auth login --web"}],
              "replies": [{"key": "✓", "label": "I did it", "reply": "done, logged in"}],
              "next": "I open the PR, wait for CI and report back."},
}

WORKING = {
    "type": "working",
    "title": "Full test suite", "path": ["Smart termination"],
    "headline": "The full test suite is running; the result comes here when it finishes",
    "context": "confirming the Smart termination change broke nothing else.",
    "status": {"done": ["New tests: 22 of 22 pass"], "remaining": ["Full suite (about 2,100 tests)"]},
    "needs": {"kind": "none", "why": "The run is in progress.",
              "next": "If something fails, I fix it.", "eta": "About 6 minutes"},
}

BLOCKED = {
    "type": "blocked",
    "title": "Release 2.4", "path": ["Releases"],
    "headline": "The CustomerNode release can't run: the image registry refuses our login",
    "context": "shipping the nightly-drill fix to production.",
    "tried": ["Logged in again with the stored token", "Checked the registry status page: it is up"],
    "status": {"done": ["Fix built and gated green"], "broken": ["Push to the registry: 401"]},
    "needs": {"kind": "action", "ask": "Renew the registry token",
              "why": "The token expired; renewing it needs your registry account, which another session's run also uses.",
              "steps": [{"text": "Open the registry's token page and create a new token"}],
              "replies": [{"key": "✓", "label": "Token renewed", "reply": "token renewed"}],
              "next": "I push the image and release."},
}

DELIVERABLE = {
    "type": "deliverable",
    "title": "Reply to Dana", "path": ["Email"],
    "headline": "The reply to Dana about the delayed release is ready to send",
    "context": "Dana asked why 2.4 slipped a week and whether Friday still holds.",
    "deliverable": {"kind": "email", "title": "Re: 2.4 release date",
                    "meta": {"To": "dana@example.com", "Subject": "Re: 2.4 release date"},
                    "body": "Hi Dana,\n\nFriday holds. Should I send the notes Thursday? I'll follow up "
                            "either way; the Q3 plan is unrelated.\n\nDon"},
    "needs": {"kind": "none", "why": "Copy it, or open it in Mail and send.",
              "next": "Tell me if you want it shorter and I'll redo it."},
}

EXPLAIN = {
    "type": "explain",
    "title": "Publish gate", "path": ["Releases"],
    "headline": "CustomerNode's gate went red because of a commit from the search work, not this branch",
    "context": "why this morning's gate failed.",
    "answer": "The failing test is in search, from another session's commit at 9:12. It passes on this branch alone.",
    "needs": {"kind": "none", "why": "This answers your question.", "next": "Nothing changes unless you ask."},
}

GOOD = {"done": DONE, "decision": DECISION, "approve": APPROVE, "action": ACTION,
        "working": WORKING, "blocked": BLOCKED, "deliverable": DELIVERABLE, "explain": EXPLAIN}


@pytest.mark.parametrize("name", sorted(GOOD))
def test_every_good_card_passes(name):
    assert hr.review(msg(GOOD[name])) is None


def test_prose_above_a_good_card_is_allowed():
    assert hr.review(msg(DONE, "Pushed and checked.\n\n")) is None


# ── Layer 1: format ───────────────────────────────────────────────────────

def test_no_card_is_sent_back_with_the_instructions():
    reason = hr.review("All done, the fix is pushed.")
    assert reason and "no ```handoff block" in reason and '"type"' in reason


def test_the_old_heading_handoff_is_not_enough():
    reason = hr.review("**What we're working on:** x\n**Where it stands:** y\n**What's needed from you:** z")
    assert reason and "handoff" in reason


def test_card_must_be_the_last_thing():
    reason = hr.review(msg(DONE) + "\nOne more thing.")
    assert reason and "last thing" in reason


def test_invalid_json_is_sent_back():
    assert "not valid JSON" in hr.review("```handoff\n{type: done}\n```")


def test_the_last_card_is_the_one_checked():
    bad = copy.deepcopy(DONE)
    bad["type"] = "nonsense"
    assert hr.review(msg(bad) + "\n" + msg(DONE)) is None


def test_unknown_type_is_sent_back():
    card = dict(DONE, type="status")
    assert '"type" must be one of' in hr.review(msg(card))


def test_a_card_needs_a_short_title():
    """The owner, 2026-10-28: "a char limited title length with a subtitle"."""
    card = dict(DONE)
    del card["title"]
    assert '"title" is empty' in hr.review(msg(card))
    card["title"] = "x" * (hr.MAX_TITLE_CHARS + 1)
    assert f"the limit is {hr.MAX_TITLE_CHARS}" in hr.review(msg(card))
    card["title"] = "x" * hr.MAX_TITLE_CHARS
    assert hr.review(msg(card)) is None
    assert "at most 32 characters" in hr.INSTRUCTIONS and hr.MAX_TITLE_CHARS == 32


@pytest.mark.parametrize("field", ["headline", "context"])
def test_headline_and_context_are_required(field):
    card = dict(DONE)
    card[field] = ""
    assert f'"{field}" is empty' in hr.review(msg(card))


def test_done_with_work_left_is_sent_back():
    card = dict(DONE, status={"done": ["a"], "remaining": ["wire the hook"]})
    assert "cannot list work left over" in hr.review(msg(card))


def test_done_with_a_failed_check_is_sent_back_even_if_not_ours():
    # The owner's decision (2026-10-27): a failure the session did not cause
    # still means it is not done.  Fix it, or say "blocked".
    card = copy.deepcopy(DONE)
    card["verified"][1] = {"check": "Full test suite", "result": "1 failure in session-restart code.", "ok": False}
    assert "cannot carry a failed check" in hr.review(msg(card))


def test_the_librarys_own_done_example_is_sent_back():
    # The first-draft library marked a pre-existing failure as a pass.
    card = copy.deepcopy(DONE)
    card["verified"][1] = {"check": "Full test suite",
                           "result": "1 failure, in session-restart code. It failed the same way before this change.",
                           "ok": True}
    reason = hr.review(msg(card))
    assert reason and "not my fault" in reason


def test_done_needs_verified():
    card = {k: v for k, v in DONE.items() if k != "verified"}
    assert '"verified"' in hr.review(msg(card))


def test_bare_nothing_needed_is_sent_back():
    card = dict(DONE, needs={"kind": "none"})
    assert '"why"' in hr.review(msg(card))


def test_working_needs_an_eta():
    card = copy.deepcopy(WORKING)
    del card["needs"]["eta"]
    assert "needs.eta" in hr.review(msg(card))


def test_needs_you_must_need_something():
    card = dict(DECISION, needs={"kind": "none", "why": "a", "next": "b"})
    assert "must say what is needed" in hr.review(msg(card))


def test_decision_choices_must_be_spelled_out():
    card = copy.deepcopy(DECISION)
    card["needs"]["replies"] = [{"key": "B", "label": "B"}, {"key": "C", "label": "Option C"}]
    assert "spelled out" in hr.review(msg(card))


def test_decision_needs_something_to_look_at():
    card = copy.deepcopy(DECISION)
    del card["needs"]["look"]
    assert "something to look at" in hr.review(msg(card))


def test_action_needs_steps_and_a_done_button():
    card = copy.deepcopy(ACTION)
    card["needs"]["steps"] = []
    card["needs"]["replies"] = []
    reason = hr.review(msg(card))
    assert "needs.steps" in reason and "reply button" in reason


def test_blocked_needs_what_was_tried():
    card = {k: v for k, v in BLOCKED.items() if k != "tried"}
    assert '"tried"' in hr.review(msg(card))


def test_deliverable_needs_a_body():
    card = copy.deepcopy(DELIVERABLE)
    card["deliverable"]["body"] = ""
    assert "deliverable.body" in hr.review(msg(card))


# ── Layer 2: banned phrases ───────────────────────────────────────────────

# Every phrase from the two analyses, in the wording the sessions used.
OWNER_LIST = [
    # Handing the job back
    "Your call on whether to ship it.", "Still your call.", "Decisions still yours: the cap.",
    "Two open decisions remain.", "I need one decision from you.", "Say the word and I start.",
    "Say go and I push.", "Say \"push\" and it goes to develop.", "Reply \"spacing\" to start.",
    "Want me to run it?", "Should I run the gate?", "Which do you want?", "My pick: A.",
    "I'm not starting it.", "I won't do it unless you say so.", "Nothing changes until you pick.",
    "Do one of these:", "Paste it in yourself.", "Fix I'd ship: the rig queue.", "What I'd build next is a banner.",
    # Done, with unfinished work
    "One honest caveat: the probe is local.", "Two caveats: timing only.", "Still open: the flake.",
    "Two problems I'm leaving open.", "Open items: three.", "Deferred to Phase 3.", "The cleanup is parked.",
    "I wrote it up as a task for later.", "Left alone on purpose.", "It hasn't been through a full test run yet.",
    "Flags, not fixed: the pool size.", "Flagged, not touched.", "Review items not acted on.",
    "No action taken.", "The leftovers are written down.", "Not verified on a phone.", "Untested on Windows.",
    "I haven't checked the picker.", "It should work now.", "Reload to see it.", "One standing backstop not yet live.",
    # Not my fault
    "That failure is unrelated.", "Not from this change.", "It is not my change.", "That one isn't mine.",
    "It belongs to another session's run.", "It fails on develop too.", "A pre-existing failure.",
    "It predates today's changes.", "It's been like this all along.", "It passes when run alone.",
    "It fails occasionally when the machine is busy.", "A known flaky test.",
    "It failed the same way before this change.",
    # Parked and calling it waiting
    "Privately, what I need next: the chain result.", "Nothing is requestable now.",
    "I'll come back when the run ends.", "I'll report when it lands.", "Watching for your tap now.",
    # Ritual apologies
    "From now on I'll send the card first.", "That's on me.", "My mistake.", "I should have asked.",
    "You're right, I missed it.", "Understood, stopping.", "Noted.", "I hear you.", "I'm listening.",
    "I've saved a lesson for this.", "I won't raise it again.",
    # Physical work
    "Try to unplug the dock.", "Check the cables.", "Use a different wall outlet.", "Reboot your computer.",
    "Run a 5-minute test first.", "Let me run an experiment.", "I can't control your browser from this session.",
    # Status noise
    "Here's where it stands.", "Here's the map.", "Status: green.", "Reminders you asked for: none.",
    "My list is empty.", "Nothing needed from you.", "Nothing needs you.", "Nothing for you to do.",
    # AI-sounding
    "It quietly retries.", "It is genuinely faster.", "To be honest, it is slow.", "Pick a voice for it.",
]


@pytest.mark.parametrize("phrase", OWNER_LIST)
def test_every_phrase_from_the_owners_list_sends_a_done_card_back(phrase):
    card = copy.deepcopy(DONE)
    card["watch_for"] = [phrase]
    assert hr.review(msg(card)) is not None, phrase


@pytest.mark.parametrize("field", ["headline", "watch_for", "details", "did", "needs.next", "prose"])
def test_folded_fields_and_prose_are_checked_too(field):
    card = copy.deepcopy(DONE)
    phrase = "One caveat: the phone layout is unverified."
    prose = ""
    if field == "headline":
        card["headline"] += ". " + phrase
    elif field == "needs.next":
        card["needs"]["next"] = phrase
    elif field == "did":
        card["did"].append({"what": "Built it", "why": phrase})
    elif field == "prose":
        prose = phrase + "\n\n"
    else:
        card[field] = [phrase]
    reason = hr.review(msg(card, prose))
    assert reason and "unfinished work" in reason


def test_the_reason_names_the_phrase_and_where_it_is():
    card = copy.deepcopy(DONE)
    card["watch_for"] = ["One honest caveat: phones."]
    reason = hr.review(msg(card))
    assert '"caveat" (watch_for[0])' in reason and '"honest" (watch_for[0])' in reason


def test_blocked_may_say_the_problem_is_not_its_own():
    card = copy.deepcopy(BLOCKED)
    card["needs"]["why"] = "The registry outage is unrelated to our change; it predates today."
    assert hr.review(msg(card)) is None


def test_explain_may_say_whose_it_is_but_not_carry_unfinished_work():
    assert hr.review(msg(EXPLAIN)) is None  # mentions another session's commit, passes alone
    card = dict(EXPLAIN, answer="It is the search commit. One caveat: I haven't checked the logs.")
    assert "unfinished work" in hr.review(msg(card))


def test_needs_you_may_ask_in_the_ask_but_not_elsewhere():
    card = copy.deepcopy(APPROVE)
    card["needs"]["ask"] = "Should I commit CustomerNode's push-guard change and build the targeted version?"
    assert hr.review(msg(card)) is None
    card["needs"]["next"] = "Want me to also update the docs?"
    assert "handing the job back" in hr.review(msg(card))


def test_needs_you_can_list_work_gated_on_the_answer_but_not_blame():
    assert hr.review(msg(DECISION)) is None  # remaining: build the one you pick
    card = copy.deepcopy(DECISION)
    card["needs"]["why"] = "The other design is pre-existing, so it's your call."
    reason = hr.review(msg(card))
    assert "not my fault" in reason and "handing the job back" in reason


def test_working_cannot_hide_a_wait_on_the_user():
    card = copy.deepcopy(WORKING)
    card["needs"]["next"] = "Watching for your tap now."
    assert "parked" in hr.review(msg(card))


def test_deliverable_body_is_only_style_checked():
    # The email legitimately says "should I", "follow up", "unrelated", "Q3".
    assert hr.review(msg(DELIVERABLE)) is None
    card = copy.deepcopy(DELIVERABLE)
    card["deliverable"]["body"] += "\nIt quietly got faster."
    assert "sound like AI" in hr.review(msg(card))


def test_compare_rows_describe_behaviour_and_are_not_blame():
    assert hr.review(msg(APPROVE)) is None  # its "before" says "even if unrelated"


# 2026-10-27, the owner: "you just listed 4 filenames in a text blob where im
# in a chat window. thats worse than useless".
@pytest.mark.parametrize("text", [
    "Two checks are built and tested in `daemon/handoff_evidence.py`.",
    "The other session is still editing smart_termination.py.",
    "Pick a design at /static/scratch/last-message.html.",
    "Saved to ~/.claude/copy-exports/pack.md for you.",
    "The fix is in static/js/live-panel.js and style.css.",
])
def test_file_names_in_the_text_are_sent_back(text):
    card = copy.deepcopy(DONE)
    card["did"][0]["what"] = text
    reason = hr.review(msg(card))
    assert reason and "file names in the text" in reason


@pytest.mark.parametrize("text", [
    "Open http://127.0.0.1:5050/static/scratch/last-message.html to pick one.",
    "It runs on Node.js and Next.js.",
    "Release cn.v.847 is live.",
    "Mail dana@example.com about it.",
    "Version 2.4 slipped a week.",
    "Read and write files in the folder.",
])
def test_urls_versions_and_product_names_are_not_file_names(text):
    card = copy.deepcopy(DONE)
    card["did"][0]["what"] = text
    assert hr.review(msg(card)) is None


def test_files_belong_in_link_rows():
    assert hr.review(msg(DONE)) is None   # its links[0] label is a file path


def test_reply_text_and_urls_are_not_checked():
    card = copy.deepcopy(DECISION)
    card["needs"]["replies"][0]["reply"] = "you're right, go with the chevron"
    card["needs"]["look"][0]["url"] = "http://x/honest-caveat"
    assert hr.review(msg(card)) is None


def test_a_clean_record_of_flakes_is_not_blame():
    card = copy.deepcopy(DONE)
    card["verified"].append({"check": "Three full runs", "result": "No flaky tests, 0 flakes.", "ok": True})
    assert hr.review(msg(card)) is None


def test_apologies_count_at_the_start_only_for_common_words():
    card = copy.deepcopy(DONE)
    card["did"][0]["why"] = "Users understood the old switch, so it keeps its place."
    assert hr.review(msg(card)) is None


# ── Labels ────────────────────────────────────────────────────────────────

def test_the_owners_real_example_is_sent_back():
    card = copy.deepcopy(DECISION)
    card["headline"] = "The redone option C is pushed. Pick B or C on the board to finish point 5"
    reason = hr.review(msg(card))
    for lab in ("option C", "B or C", "point 5"):
        assert lab in reason


def test_explained_labels_pass_and_reply_keys_are_ignored():
    card = copy.deepcopy(DECISION)
    card["context"] = "option C (the Expanded and Collapsed links) was redone after your note."
    assert hr.review(msg(card)) is None


def test_question_numbers_are_labels():
    card = copy.deepcopy(DONE)
    card["did"].append({"what": "Answered Q1 and decision 4"})
    reason = hr.review(msg(card))
    assert "Q1" in reason and "decision 4" in reason


def test_every_rule_in_the_instructions_matches_the_code():
    for t in hr.TYPES:
        assert f"- {t}:" in hr.INSTRUCTIONS
    for k in hr.NEEDS_KINDS:
        assert k in hr.INSTRUCTIONS
    assert hr.FENCE_LANG in hr.INSTRUCTIONS


# ── Length (2026-10-27) ──────────────────────────────────────────────────
# The card that drew "this response is way too fucking long, fix the system
# to not allow this": a needs_you card that passed every other rule.
OWNERS_TOO_LONG = {
    "type": "needs_you",
    "title": "Queue and Smart termination",
    "headline": "I checked how queued messages interact with Smart termination. The queue ignores the handoff card, and three things go wrong because of it.",
    "context": "You queue messages while a session works. Smart termination makes every turn end with a handoff card and asks you for a verdict (Accept, Send back, Continue). The two features don't know about each other yet. Nothing was changed. This was code reading only.",
    "status": {"done": [
        "Read the code for the queue, the end-of-turn check, the status icons and the verdict strip.",
        "The card nobody reads: as soon as a turn ends, the next queued message goes out. The card shows for a moment, then the status flips back to Working and the card is dropped with no verdict from you.",
        "A question gets talked over: if a turn ends asking you to pick between options, the queued message goes out anyway.",
        "Wasted turns: the end-of-turn check can send a session back twice to rewrite a card that is about to be skipped."],
        "remaining": [], "broken": []},
    "compare": {"label": "What would change", "before_label": "Today", "after_label": "Proposed", "rows": [
        {"topic": "A turn ends with a question and a message is queued", "before": "The queued message goes out at once and talks over the question", "after": "The queue pauses. The status reads 'Decide · 1 queued'. You answer, or press 'Send queued anyway'"},
        {"topic": "Cards the queue moved past", "before": "Dropped, and you never give a verdict on them", "after": "Kept. The final card says '2 earlier results not reviewed' with links to each. One verdict at the end covers them all"},
        {"topic": "Card rewrites while messages are queued", "before": "Up to 2 rewrite turns per stop, and the queue waits behind them", "after": "A one-line note, with no rewrites. The full card is required only on the last turn before the queue empties"},
        {"topic": "Does a queued message count as Accept?", "before": "No rule", "after": "Never. Only the final stop asks 'Is this right?'"},
        {"topic": "Seeing the queue in the sidebar", "before": "Not shown on the status", "after": "'Working · 2 queued', and a small '1 queued' badge on a Review card"}]},
    "needs": {"kind": "decision",
        "ask": "Which of the proposed changes in the comparison should I build?",
        "why": "The first four rows change what happens when a turn ends with messages queued. The last row changes the screen, so the project rules say it starts as a test page for you to pick from.",
        "next": "With your pick I build those changes with tests. The queue pause and the no-rewrites change run in the Session Engine and go live after it restarts.",
        "eta": "One to two hours.",
        "replies": [
            {"key": "A", "label": "All of it", "detail": "Pause the queue on a question, track skipped cards, no rewrites while queued, queuing never counts as Accept, plus a test page for the sidebar.", "reply": "A"},
            {"key": "B", "label": "Only the queue pause and skipped-card tracking", "detail": "The two changes that stop things getting lost.", "reply": "B"},
            {"key": "C", "label": "Show me first", "detail": "A test page where you queue messages against fake sessions.", "reply": "C"}]},
}


def test_the_owners_too_long_card_is_sent_back():
    reason = hr.review(msg(OWNERS_TOO_LONG))
    assert reason and "Too long" in reason
    assert f"limit is {hr.MAX_WORDS}" in reason


@pytest.mark.parametrize("name", sorted(GOOD))
def test_every_good_card_is_within_the_length_limits(name):
    assert hr.length_problems(GOOD[name]) == []


def test_a_long_headline_is_sent_back_even_in_a_short_card():
    card = copy.deepcopy(WORKING)
    card["headline"] = " ".join(["word"] * (hr.MAX_HEADLINE_WORDS + 1))
    reason = hr.review(msg(card))
    assert "headline is" in reason


def test_a_long_line_is_sent_back():
    card = copy.deepcopy(DONE)
    card["needs"]["why"] = " ".join(["word"] * (hr.MAX_LINE_WORDS + 1))
    assert "needs.why is" in hr.review(msg(card))


def test_too_many_items_in_a_list_is_sent_back():
    card = copy.deepcopy(WORKING)
    card["status"]["done"] = [f"step {n} finished" for n in range(hr.MAX_LIST_ITEMS + 1)]
    assert "status.done has" in hr.review(msg(card))


def test_the_deliverable_and_asks_do_not_count_toward_the_length():
    card = copy.deepcopy(DELIVERABLE)
    card["deliverable"]["body"] = "Hi Dana, " + " ".join(["text"] * 800)
    card["asks"] = [{"ask": " ".join(["mine"] * 40), "state": "done"} for _ in range(6)]
    assert hr.length_problems(card) == []


def test_the_instructions_state_the_length_limits():
    for n in (hr.MAX_WORDS, hr.MAX_HEADLINE_WORDS, hr.MAX_LINE_WORDS, hr.MAX_ANSWER_WORDS, hr.MAX_LIST_ITEMS):
        assert str(n) in hr.INSTRUCTIONS


def test_a_done_card_with_nothing_to_review_is_sent_back():
    # The owner, 2026-10-27: "YOU GAVE ME NOTHING TO REVIEW".
    card = copy.deepcopy(DONE)
    card.pop("links")
    card["validate"] = {"how": "Turn on Smart termination in Preferences and end a turn"}
    reason = hr.review(msg(card))
    assert reason and "something to review" in reason
    card["compare"] = {"rows": [{"topic": "Queued message", "before": "Held back", "after": "Sent at once"}]}
    assert "something to review" not in (hr.review(msg(card)) or "")
    assert "something to review" in hr.INSTRUCTIONS


# ── "Nothing needed", then a job (2026-10-28) ────────────────────────────
# "Nothing needed. Reload VibeNode and every card shows the new heading":
# "do you see how little sense that makes".

@pytest.mark.parametrize("next_", [
    "Reload VibeNode and every card shows the new heading.",
    "New headings appear after a page reload.",
    "It is built. Restart the app to see it.",
    "You need to clear the cache first.",
])
def test_nothing_needed_with_a_job_for_the_user_is_sent_back(next_):
    card = copy.deepcopy(DONE)
    card["needs"]["next"] = next_
    reason = hr.review(msg(card))
    assert reason and "tells the user to do something" in reason


@pytest.mark.parametrize("next_", [
    "It ships with your next Update.",
    "It goes live the next time the Session Engine restarts.",
    "Open tabs pick it up within 30 seconds.",
])
def test_nothing_needed_that_really_needs_nothing_passes(next_):
    card = copy.deepcopy(DONE)
    card["needs"]["next"] = next_
    assert "tells the user to do something" not in (hr.review(msg(card)) or "")


# ── validate: a way for the user to check it himself (owner, 2026-10-28:
#    "we need to force a validate row so i have a way to check") ──

def test_a_done_card_without_validate_is_sent_back():
    card = dict(DONE)
    card.pop("validate")
    probs = hr.format_problems(card)
    assert any('"validate"' in p for p in probs)


def test_validate_needs_a_how_line():
    card = dict(DONE, validate={"url": "http://localhost:5050/"})
    assert any('"validate"' in p for p in hr.format_problems(card))
    card = dict(DONE, validate={"how": "Open the page and press Back", "url": "/static/x.html"})
    assert not any('"validate"' in p for p in hr.format_problems(card))


# The owner's pick B, 2026-10-28: a Validate row always opens something; a
# demo when the real moment comes later ("Your next design choice opens
# mockups" drew a row that looked like a button and did nothing).
@pytest.mark.parametrize("url", ["", "daemon/handoff_rules.py", "//evil.example/x", "javascript:alert(1)"])
def test_validate_must_open_something(url):
    card = dict(DONE, validate={"how": "Your next design choice opens mockups", "url": url})
    reason = hr.review(msg(card))
    assert reason and "validate.url must open something" in reason and "validate.later" in reason


@pytest.mark.parametrize("url", ["/static/scratch/x-demo.html", "/api/preview/asset/abc",
                                 "http://localhost:8000/journeys", "https://example.com/"])
def test_a_page_or_demo_satisfies_validate(url):
    card = dict(DONE, validate={"how": "See the format card get sent back", "url": url,
                                "later": "For real: your next design choice opens mockups."})
    assert not any("validate.url" in p for p in hr.format_problems(card))


def test_a_validate_page_counts_as_something_to_review():
    card = dict(DONE, validate={"how": "Open it and press Back", "url": "/static/x.html"})
    card.pop("links")
    assert not any("something to" in p for p in hr.format_problems(card))


# ── A choice about looks must show its options (owner, 2026-10-28) ────────
# The card that drew "it gave me a choice with no visual where a mockup or
# preview would've helped": a presentation-format decision whose only thing
# to look at was a markdown outline.
FORMAT_NO_VISUAL = {
    "type": "needs_you",
    "title": "Smart termination deck",
    "headline": "VibeNode smart termination presentation: outline drafted; pick a format before slides are built.",
    "context": "A presentation on VibeNode's smart termination feature.",
    "status": {"done": ["12-slide outline with real numbers since Oct 26"],
               "remaining": ["Build the slides once a format is picked"]},
    "links": [{"label": "Presentation outline draft", "kind": "file",
               "url": "docs/plans/smart-termination-presentation.md"}],
    "needs": {"kind": "decision",
              "ask": "Which format for the VibeNode smart termination presentation?",
              "why": "The format and audience decide how the slides are built.",
              "look": [{"label": "Presentation outline draft", "kind": "file",
                        "url": "docs/plans/smart-termination-presentation.md"}],
              "next": "I build the slides in the format you pick.",
              "replies": [
                  {"key": "A", "label": "Live HTML slides showing real cards", "reply": "Live HTML slides"},
                  {"key": "B", "label": "A PowerPoint deck to share", "reply": "PowerPoint deck"},
                  {"key": "C", "label": "A written one-page brief", "reply": "Written one-pager"}]},
}


def test_a_presentation_choice_with_only_an_outline_is_sent_back():
    reason = hr.review(msg(FORMAT_NO_VISUAL))
    assert reason and "SHOWS the choices" in reason
    assert "static/scratch/" in reason and "data-option" in reason


def test_a_scratch_page_in_look_satisfies_the_visual_rule():
    card = copy.deepcopy(FORMAT_NO_VISUAL)
    card["needs"]["look"] = [{"label": "Three formats side by side", "kind": "link",
                              "url": "/static/scratch/deck-format.html"}]
    assert hr.visual_choice_problem(card) is None


def test_screens_on_the_replies_satisfy_the_visual_rule():
    card = copy.deepcopy(FORMAT_NO_VISUAL)
    for r in card["needs"]["replies"]:
        r["screens"] = [{"label": r["label"], "url": f"/static/scratch/deck-format.html#{r['key']}",
                         "device": "web"}]
    assert hr.visual_choice_problem(card) is None


def test_a_screenshot_satisfies_the_visual_rule():
    card = copy.deepcopy(FORMAT_NO_VISUAL)
    card["links"].append({"label": "All three formats", "kind": "image", "url": "/api/preview/asset/abc"})
    assert hr.visual_choice_problem(card) is None


def test_a_choice_that_is_not_about_looks_needs_no_mockup():
    card = copy.deepcopy(FORMAT_NO_VISUAL)
    card.update(title="Pick a retry policy", headline="CustomerNode sync: pick how failed uploads retry")
    card["needs"]["ask"] = "How should CustomerNode retry a failed upload?"
    card["needs"]["replies"] = [
        {"key": "A", "label": "Retry three times, then stop", "reply": "A"},
        {"key": "B", "label": "Retry forever with backoff", "reply": "B"}]
    assert hr.visual_choice_problem(card) is None


# ── A mockup page is shown, never only linked (owner, 2026-10-28: "why did
#    you not show me the movkups here, another failure"). ──

def _mock_card(**extra):
    c = {"type": "explain", "title": "Back-to button", "headline": "Mockups are ready.", "answer": "Yes.",
         "links": [{"label": "Mockups", "kind": "link", "url": "/static/scratch/card-back-to.html"}],
         "needs": {"kind": "none", "why": "Mockups only.", "next": "Name one."}}
    c.update(extra)
    return c


def _mock_probs(c):
    return [p for p in hr.format_problems(c) if "mockup page" in p]


def test_a_mockup_link_without_pictures_is_sent_back():
    assert _mock_probs(_mock_card())


def test_a_screenshot_or_decide_screens_satisfy_it():
    c = _mock_card()
    c["links"].append({"label": "Option A", "kind": "image", "url": "/static/scratch/card-back-to-A.png"})
    assert not _mock_probs(c)
    d = {"type": "needs_you", "title": "t", "headline": "h",
         "needs": {"kind": "decision", "ask": "a", "why": "w", "look": [{"label": "m", "url": "/static/scratch/x.html"}],
                   "replies": [{"key": "A", "label": "one two three", "screens": [{"url": "/static/scratch/x.html#A"}]},
                               {"key": "B", "label": "four five six"}]}}
    assert not _mock_probs(d)


def test_other_pages_are_not_mockups():
    c = _mock_card(links=[{"label": "Demo", "kind": "link", "url": "/static/js/app.js"}])
    assert not _mock_probs(c)
    from daemon import smart_termination
    assert "Mockups are SHOWN" in smart_termination.INSTRUCTIONS


# ── "Nothing needed" that waits on the user's yes (2026-10-28) ──────────
# The owner, on "Nothing needed. Version B goes into the real cards once you
# confirm": "why the fuck does next say nothing needed and then something
# about once i confirm with no ctas. that is fucking useless".

@pytest.mark.parametrize("nxt", [
    "Version B goes into the real cards once you confirm.",
    "Next I build the five changes once you approve them.",
    "Its spec is waiting for your go-ahead.",
    "I build the one you pick.",
    "When you're ready, I merge it.",
])
def test_nothing_needed_may_not_wait_on_the_users_yes(nxt):
    card = dict(DONE, needs={"kind": "none", "why": "Answered.", "next": nxt})
    out = hr.review(msg(card))
    assert out and "waits on the user" in out and '"approve"' in out


@pytest.mark.parametrize("nxt", [
    "Open tabs pick it up by themselves.",
    "Results land on the next card.",
    "It goes live the next time the Session Engine restarts.",
    "Nothing planned.",
])
def test_a_next_step_that_does_not_wait_on_him_passes(nxt):
    card = dict(DONE, needs={"kind": "none", "why": "Answered.", "next": nxt})
    assert not any("waits on the user" in p for p in hr.format_problems(card))


def test_the_guide_says_a_yes_gets_a_button():
    t = hr.INSTRUCTIONS
    assert "waits only on his yes" in t and "once you confirm" in t


def test_an_open_offer_is_not_a_wait():
    card = dict(DONE, needs={"kind": "none", "why": "Answered.", "next": "Tell me if you want it shorter and I'll redo it."})
    assert not any("waits on the user" in p for p in hr.format_problems(card))


def test_nothing_waiting_on_you_is_not_a_wait():
    card = dict(DONE, needs={"kind": "none", "why": "Nothing is waiting on you.", "next": "Nothing planned."})
    assert not any("waits on the user" in p for p in hr.format_problems(card))
