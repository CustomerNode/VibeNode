"""The cards Code Pipeline shows a person: a permission to run a check, and
an unverified result to accept or send back.

They are Smart termination handoff cards (``daemon/handoff_rules.py`` is the
schema; ``static/js/last-card.js`` draws them), so they look and act like
every other card in the app: one line on what happened, one on what it means,
one question, two buttons.  The pipeline READS that format and never writes
into Smart termination; ``tests/test_code_pipeline_cards.py`` runs every card
built here through the real validators, so a rule added there fails our test
before it fails a card.

The words come from this file, never from the agent (the owner, 2026-10-28:
"you're designing around Code Pipeline's internal data rather than the
decision I need to make").  The one piece of agent text is the reason a check
could not run, quoted as the plan gave it.  A card the agent edits no longer
matches the decision that issued it (``pipeline.decisions.answer``), so the
buttons on an edited card answer nothing.
"""

from __future__ import annotations

import re

# One entry per harness finding code: what happened, in the person's words,
# and what it means.  ``{reason}`` is the plan's own reason for a check that
# cannot run; ``{detail}`` is the harness's finding text.
WORDS: dict[str, tuple[str, str, str]] = {
    # code: (title <= 32 chars, headline <= 20 words, what it means)
    "unexecuted": ("A check could not run", "We couldn't run one of the checks on \u201c{task}\u201d.",
                   "The plan says: {reason}. Every check that could run passed."),
    "check-failed": ("A check failed", "One of the checks on \u201c{task}\u201d failed.", "{detail}"),
    "never-ran": ("A check never ran", "One of the checks on \u201c{task}\u201d never ran.", "{detail}"),
    "baseline-passed": ("Tests miss the change", "The new tests don't prove \u201c{task}\u201d works.",
                        "They pass even without the change, so they wouldn't notice if it broke."),
    "baseline-unrelated": ("Tests miss the change", "The new tests don't prove \u201c{task}\u201d works.",
                           "Without the change they fail for some other reason, not the one the plan named."),
    "baseline-missing": ("Tests not proven", "Nothing shows the tests would catch a broken \u201c{task}\u201d.",
                         "The tests were never run against the code as it was before the change."),
    "no-change-detection": ("Tests not proven", "Nothing shows the tests would catch a broken \u201c{task}\u201d.",
                            "No test was named as the one that fails without the change."),
    "finding-unresolved": ("A problem is unfixed", "A reviewer found a problem in \u201c{task}\u201d that isn't fixed.", "{detail}"),
    "finding-dropped": ("A problem went missing", "A reviewer's problem with \u201c{task}\u201d disappeared from the report.", "{detail}"),
    "coverage": ("Checks don't cover the ask", "The checks don't cover all of \u201c{task}\u201d.", "{detail}"),
    "fourth-question": ("Audit incomplete", "The audit of \u201c{task}\u201d didn't say what the checks could miss.", "{detail}"),
    "stale-tree": ("Changed after checking", "\u201c{task}\u201d changed after it was checked.",
                   "The checks ran on an earlier version; they would have to run again."),
    "checker-stale": ("Audit of an old version", "The audit looked at an older version of \u201c{task}\u201d.", "{detail}"),
    "no-suite": ("Full tests didn't run", "The project's full test suite didn't run on \u201c{task}\u201d.", "{detail}"),
    "evidence-unrecorded": ("A check has no record", "A check on \u201c{task}\u201d has no record of running.", "{detail}"),
    "evidence-forged": ("A record was altered", "A record of a check on \u201c{task}\u201d was written by hand.", "{detail}"),
    "unauthorized": ("A check ran unapproved", "A check on \u201c{task}\u201d ran without an approval.", "{detail}"),
    "decision-forged": ("An approval was altered", "An approval record for \u201c{task}\u201d was written by hand.", "{detail}"),
    "self-graded": ("Checked by its author", "\u201c{task}\u201d was checked by the same agent that wrote it.", "{detail}"),
    "committed-before-delivery": ("Committed too early", "\u201c{task}\u201d was committed before you approved it.", "{detail}"),
    "pipeline-impl-mismatch": ("Checker changed mid-run", "The checking program changed after \u201c{task}\u201d was checked.",
                               "The checks have to run again under the current program before they count."),
    "pipeline-impl-unknown": ("Checker not identified", "A check on \u201c{task}\u201d came from an unknown checking program.", "{detail}"),
    "stage-tampered": ("Report changed after signing", "A checker\u2019s report on \u201c{task}\u201d changed after it was signed.", "{detail}"),
    "stage-unattested": ("Report not signed", "A checker\u2019s report on \u201c{task}\u201d was never signed.", "{detail}"),
    "plan-changed": ("Plan changed after checks", "The plan for \u201c{task}\u201d changed after its checks ran.", "{detail}"),
}
DEFAULT_WORDS = ("Not every check passed", "\u201c{task}\u201d didn't pass every check.", "{detail}")
# The order in which findings are told: the one named on the card first.
PRIORITY = ("check-failed", "never-ran", "evidence-forged", "decision-forged", "evidence-unrecorded", "stage-tampered",
            "stage-unattested", "pipeline-impl-mismatch", "pipeline-impl-unknown", "plan-changed",
            "unauthorized", "committed-before-delivery", "finding-unresolved", "finding-dropped",
            "unexecuted", "baseline-passed", "baseline-unrelated", "baseline-missing", "no-change-detection",
            "no-suite", "coverage", "stale-tree", "checker-stale", "self-graded", "fourth-question")
ACTIONS = {"commit": "Commit", "push": "Push", "merge": "Merge", "deploy": "Deploy", "package": "Package"}
MAX_TITLE = 32
MAX_ZOOM = 40
_MACHINE = re.compile(r"[`<>{}|\\]")


def _clip(s: str, n: int) -> str:
    """At most *n* characters, cut at a word boundary where one exists."""
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    if " " in cut[n // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:(") + "…"


def _plain(s: str) -> str:
    """Finding text with anything that reads as machinery removed."""
    s = _MACHINE.sub("", str(s or ""))
    return " ".join(s.split())


def task_name(request_text: str) -> str:
    """The task in the person's words: the request's first sentence, short."""
    first = re.split(r"(?<=[.!?])\s|\n", (request_text or "").strip(), maxsplit=1)[0]
    return _clip(first.rstrip(".!?"), MAX_ZOOM)


def lead_finding(findings: list[dict]) -> dict | None:
    """The finding the card is about: a low before a medium, then by PRIORITY."""
    graded = [f for f in findings if f.get("severity") in ("low", "medium", "block")]
    if not graded:
        return None
    sev = {"block": 0, "low": 1, "medium": 2}
    rank = {c: i for i, c in enumerate(PRIORITY)}
    return sorted(graded, key=lambda f: (sev.get(f["severity"], 3), rank.get(f["code"], len(PRIORITY))))[0]


def _words_for(finding: dict | None, criteria: list[dict], task: str) -> tuple[str, str, str]:
    title, head, means = WORDS.get(finding["code"], DEFAULT_WORDS) if finding else DEFAULT_WORDS
    head = head.replace("{task}", task)
    if finding is None:
        return title, head, "It was graded below the top mark; the record shows what ran and what did not."
    reason = ""
    if finding["code"] == "unexecuted":
        for c in criteria:
            if c.get("status") == "unexecuted" and c.get("reason"):
                reason = c["reason"]
                break
    means = means.replace("{reason}", _plain(reason).rstrip(".") or "it could not run on this computer")
    means = means.replace("{detail}", _plain(finding.get("message", "")))
    return title, head, means


def delivery_card(verdict: dict, action: str, nonce: str, task: str) -> tuple[dict, dict]:
    """The card that asks the person to accept a result below HIGH for
    *action*, and its two replies (``{"allow": ..., "deny": ...}``)."""
    verb = ACTIONS.get(action, action.capitalize())
    findings = [f for f in verdict.get("findings", []) if f.get("severity") != "info"]
    lead = lead_finding(findings)
    title, head, means = _words_for(lead, verdict.get("criteria", []), task)
    others = len(findings) - (1 if lead else 0)
    grade = verdict.get("grade", "MEDIUM")
    allow = f"{verb} it anyway ({nonce})"
    deny = f"Fix it first ({nonce})"
    ask = f"{verb} \u201c{task}\u201d anyway?"
    explained = (
        f"## What this is about\n\n“{task}” finished its work and asked to {action} the result. "
        f"Before anything is delivered, the result is graded by a program from what actually ran. "
        f"This one did not earn the top grade, so it waits for you.\n\n"
        f"## What happened\n\n{head} {means}"
        + (f" There {'is' if others == 1 else 'are'} {others} more finding{'s' if others != 1 else ''}, "
           f"listed under Why I’m asking." if others else "")
        + f"\n\n## What it means for you\n\nThe change may be fine, but the record does not prove it. "
          f"“Fix it first” sends the task back to close the gap and ask again; "
          f"“{verb} it anyway” accepts this exact version of the code as it is now. "
          f"If the code changes afterwards, this approval no longer applies.\n\n"
          f"## What you do next\n\nTap one of the two buttons. Not answering leaves the work undelivered; "
          f"nothing happens on its own."
    )
    why = means
    if others:
        names = []
        for f in findings:
            if f is lead:
                continue
            t = WORDS.get(f.get("code", ""), DEFAULT_WORDS)[0].lower()
            if t not in names:
                names.append(t)
        why += " Also: " + "; ".join(names[:4]) + "."
    card = {
        "type": "needs_you",
        "title": _clip(title, MAX_TITLE),
        "path": ["Code Pipeline", f"{verb} request"],
        "headline": head,
        "context": f"“{task}” is finished and wants to {action} the result.",
        "zoom": {"big": _clip(task, MAX_ZOOM), "task": _clip(f"{verb} the finished change", MAX_ZOOM),
                 "now": _clip(title, MAX_ZOOM)},
        "status": {"done": ["The work and its checks ran"], "remaining": [], "broken": []},
        "needs": {
            "kind": "approve",
            "ask": ask,
            "why": why,
            "next": f"It {action.rstrip('e')}s this exact version, or goes back to fix it.",
            "replies": [
                {"key": "A", "label": "Fix it first", "detail": "Send it back to close the gap", "reply": deny,
                 "recommended": True},
                {"key": "B", "label": f"{verb} it anyway", "detail": f"Accept this version, graded {grade}",
                 "reply": allow},
            ],
        },
        "explained": explained,
        # Every summary has Short / Plain words / Full (2026-10-30).
        "answer": f"Graded {grade}. {means}",
        "full_answer": (
            f"## The grade\n\n{grade}, from what actually ran.\n\n## Findings\n\n"
            + ("\n".join(f"- **{WORDS.get(f.get('code', ''), DEFAULT_WORDS)[0]}**: {_plain(f.get('message', ''))}"
                         for f in findings) or "- None recorded")
            + f"\n\n## Your two answers\n\n- **Fix it first**: the task goes back to close the gap and asks again.\n"
              f"- **{verb} it anyway**: this exact version is accepted; a later change needs a new approval."),
    }
    return card, {"allow": allow, "deny": deny}


def permission_card(operation: dict, nonce: str, task: str) -> tuple[dict, dict]:
    """The card that asks the person to let one verification command run."""
    cmd = str(operation.get("cmd") or "")
    crit = str(operation.get("criterion") or "a check")
    baseline = bool(operation.get("baseline"))
    allow = f"Run the tests ({nonce})"
    deny = f"Don’t run them ({nonce})"
    what = ("re-run its tests on the code before the change" if baseline
            else "run the project’s tests on your computer")
    card = {
        "type": "needs_you",
        "title": "Test run permission",
        "path": ["Code Pipeline", "Checks"],
        "headline": f"“{task}” wants to {what}.",
        "context": f"The check is called {crit}. Nothing runs until you say so.",
        "zoom": {"big": _clip(task, MAX_ZOOM), "task": _clip(f"Run the check {crit}", MAX_ZOOM),
                 "now": "Waiting for your OK to run it"},
        "status": {"done": [], "remaining": ["Run the check"], "broken": []},
        "needs": {
            "kind": "approve",
            "ask": "Run these tests on your computer?",
            "why": "Your permission setting asks before a command runs. Secrets are kept out of it.",
            # A step for his approval, not one for him to run: the words say
            # so (handoff_rules.handover_problems passes a step that needs
            # his authorization).
            "steps": [{"text": "The exact command it runs only if you authorize it", "command": cmd}],
            "next": "It runs the command and records the result, or does not run it.",
            "replies": [
                {"key": "A", "label": "Run the tests", "detail": "Run this exact command now", "reply": allow,
                 "recommended": True},
                {"key": "B", "label": "Don’t run them", "detail": "Nothing runs; the task is told", "reply": deny},
            ],
        },
        "explained": (
            f"## What this is about\n\n“{task}” is being checked by Code Pipeline, which runs the "
            f"project’s own tests and records what happened. Your permission setting is not the automatic "
            f"one, so each command waits for you.\n\n## What happened\n\nThe check called {crit} is ready to run. "
            f"The exact command is shown on the card; it runs in the project folder with secret-shaped "
            f"settings removed from its environment.\n\n## What it means for you\n\nAllowing it runs that one "
            f"command, once. If the code or the command changes before it runs, it asks again. Refusing it "
            f"runs nothing, and the task is told.\n\n## What you do next\n\nTap Run the tests, or Don’t "
            f"run them. Nothing runs until you do."
        ),
        "answer": f"The check {crit} is ready; it runs once, only if you allow it.",
        "full_answer": (
            f"## The command\n\n```\n{cmd}\n```\n\nIt runs in the project folder with secret-shaped settings "
            f"removed from its environment.\n\n## Your two answers\n\n- **Run the tests**: this exact command runs "
            f"once and its result is recorded.\n- **Don’t run them**: nothing runs, and the task is told."),
    }
    return card, {"allow": allow, "deny": deny}


def fenced(card: dict) -> str:
    """The card as the agent must end its turn with it: the fence, alone."""
    import json
    # ASCII escapes, so the card survives a cp1252 console and a copy through
    # the transcript byte for byte; the renderer parses JSON either way.
    return "```handoff\n" + json.dumps(card, ensure_ascii=True, indent=1) + "\n```"
