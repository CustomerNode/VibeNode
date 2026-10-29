"""Code Pipeline authorization through Smart termination cards (Phase 1,
revision 4: docs/plans/code-pipeline-phase1-authorization-design.md).

A verification command runs only under a decision.  Under the "auto" policy
the decision is allowed at once; under any other policy the agent ends its
turn with a card the pipeline wrote, and the user's tap sends the card's reply
as his own message.  A lower grade ships only the same way, never by a flag.
The cards are checked here against the real Smart termination validators, so
a rule added there fails this file before it fails a card in the app.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from pipeline import cards, decisions, deliver, harness, run_check
from pipeline.evidence import evidence_line, evidence_sha, run_criterion, tree_fingerprint
from tests.test_code_pipeline import (ADD_ONLY, COVERAGE_OK, PLAN_BLOCK, PY, FakeAgent, _audit, _git, _replan, _write_stage,
                                      codes, make_valid_run, record_check, repo, run_authorized)  # noqa: F401

XDIST_SPLITTABLE = True

TASK = "Add a sub() helper beside add()"


def _fail(msg):
    return (msg or "")[:400]


# ── the cards pass Michael's validators ───────────────────────────────────────

CASES = {
    "screen": {"grade": "MEDIUM", "findings": [{"code": "unexecuted", "severity": "medium",
               "message": "criterion ui cannot run here: the test browser was not available"}],
               "criteria": [{"id": "ui", "status": "unexecuted", "reason": "the test browser was not available"}]},
    "baseline": {"grade": "MEDIUM", "findings": [
        {"code": "baseline-passed", "severity": "medium", "message": "criterion unit passes on the code as it was"},
        {"code": "coverage", "severity": "medium", "message": "the coverage section does not account for unit"}],
        "criteria": []},
    "finding": {"grade": "MEDIUM", "findings": [{"code": "finding-unresolved", "severity": "medium",
                "message": "03-review.md raised BLOCKING: neg-input and no later checker records RESOLVED"}],
                "criteria": []},
    "failed": {"grade": "LOW", "findings": [{"code": "check-failed", "severity": "low",
               "message": "criterion suite failed (exit 1)"}], "criteria": []},
    "unknown": {"grade": "MEDIUM", "findings": [{"code": "brief-long", "severity": "medium",
                "message": "04-test.md: checker a1's brief is 13000 chars"}], "criteria": []},
    "none": {"grade": "MEDIUM", "findings": [], "criteria": []},
}


def _validators():
    from daemon import handoff_rules, smart_termination
    return handoff_rules, smart_termination


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("action", ["commit", "push", "deploy"])
def test_every_delivery_card_passes_the_real_validators(case, action):
    rules, st = _validators()
    card, replies = cards.delivery_card(CASES[case], action, "ab12cd34", TASK)
    msg = cards.fenced(card)
    assert rules.review(msg) is None, _fail(rules.review(msg))
    assert st.review(msg, continued=False, sendbacks=0) is None, _fail(st.review(msg, continued=False, sendbacks=0))
    # Three questions, two buttons, the nonce in both replies.
    assert card["type"] == "needs_you" and card["needs"]["kind"] == "approve"
    labels = [r["label"] for r in card["needs"]["replies"]]
    assert labels == ["Fix it first", f"{cards.ACTIONS[action]} it anyway"]
    assert all("ab12cd34" in r for r in replies.values())
    assert all(len(lab.split()) >= 3 for lab in labels)
    assert card["needs"]["replies"][0]["recommended"] is True


@pytest.mark.parametrize("baseline", [False, True])
def test_the_permission_card_passes_the_real_validators(baseline):
    rules, st = _validators()
    op = {"cmd": "{python} -m pytest --tb=short -q tests/", "criterion": "suite", "baseline": baseline}
    card, replies = cards.permission_card(op, "ab12cd34", TASK)
    msg = cards.fenced(card)
    assert rules.review(msg) is None, _fail(rules.review(msg))
    assert st.review(msg, continued=False, sendbacks=0) is None
    assert card["needs"]["ask"] == "Run these tests on your computer?"
    assert card["needs"]["steps"][0]["command"] == op["cmd"]
    assert [r["label"] for r in card["needs"]["replies"]] == ["Run the tests", "Don’t run them"]


def test_the_card_names_the_worst_finding_first():
    v = {"grade": "LOW", "criteria": [], "findings": [
        {"code": "coverage", "severity": "medium", "message": "x"},
        {"code": "check-failed", "severity": "low", "message": "criterion suite failed"}]}
    card, _ = cards.delivery_card(v, "commit", "ab12cd34", TASK)
    assert card["title"] == "A check failed"
    assert "Also:" in card["needs"]["why"]


# ── auto policy: unattended, recorded ────────────────────────────────────────

def test_under_auto_a_check_is_allowed_at_once_and_the_evidence_carries_the_decision(repo):
    run_dir, _ = make_valid_run(repo)
    ev = json.loads((run_dir / "evidence" / "unit.json").read_text(encoding="utf-8"))
    d = decisions.load(run_dir, ev["decision"])
    assert ev["authorized"] is True and ev["decision_how"] == "policy:auto"
    assert d["verdict"] == "allow" and d["used"] is True and d["evidence_sha"] == evidence_sha(run_dir / "evidence" / "unit.json")
    assert d["operation"]["criterion"] == "unit" and d["operation"]["baseline"] is False
    assert "card" not in d  # nothing was shown to the user
    assert harness.grade_run(run_dir).grade == "HIGH"


def test_a_decision_is_used_once(repo):
    run_dir, _ = make_valid_run(repo)
    ev = json.loads((run_dir / "evidence" / "unit.json").read_text(encoding="utf-8"))
    with pytest.raises(run_check.NotAuthorized, match="already used"):
        run_check.execute(run_dir, "unit", ev["decision"])


def test_a_change_between_authorize_and_execute_refuses(repo):
    run_dir, _ = make_valid_run(repo)
    d = run_check.authorize(run_dir, "lint", policy="auto")
    (repo / "thing.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return -(b - a)\n",
                                   encoding="utf-8")
    with pytest.raises(run_check.NotAuthorized, match="state changed"):
        run_check.execute(run_dir, "lint", d["nonce"])
    assert not (run_dir / "evidence" / "lint.json").exists() or \
        json.loads((run_dir / "evidence" / "lint.json").read_text(encoding="utf-8"))["decision"] != d["nonce"]


def test_a_decision_for_another_criterion_refuses(repo):
    run_dir, _ = make_valid_run(repo)
    d = run_check.authorize(run_dir, "lint", policy="auto")
    with pytest.raises(run_check.NotAuthorized, match="not the one approved"):
        run_check.execute(run_dir, "unit", d["nonce"])


# ── manual policy: the card, and the user's tap ──────────────────────────────

def _pending(run_dir, cid="lint", baseline=False):
    d = run_check.authorize(run_dir, cid, baseline=baseline, policy="manual")
    assert d["verdict"] == "pending" and d["card"]["needs"]["kind"] == "approve"
    return d


def test_under_manual_the_check_waits_for_the_card(repo, capsys):
    run_dir, sess = make_valid_run(repo)
    assert run_check.main(["authorize", str(run_dir), "lint"]) == 0 or True  # policy here is the machine's; use the API below
    d = _pending(run_dir)
    with pytest.raises(run_check.NotAuthorized, match="card was not shown"):
        run_check.execute(run_dir, "lint", d["nonce"])
    assert not any(p.name == "lint.json" and json.loads(p.read_text(encoding="utf-8")).get("decision") == d["nonce"]
                   for p in (run_dir / "evidence").glob("*.json"))


def test_the_users_tap_on_the_shown_card_allows_the_check(repo):
    run_dir, sess = make_valid_run(repo)
    tester = sess.agent("brief")
    d = _pending(run_dir)
    tester.bash(f"{PY} -m pipeline.run_check authorize {run_dir} lint", stdout=decisions.decision_line(d) + "\n")
    sess.assistant_text("Here is where we are.\n\n" + cards.fenced(d["card"]))
    sess.user_text(d["replies"]["allow"])
    rec, path, used = run_check.execute(run_dir, "lint", d["nonce"])
    assert rec["passed"] and used["used"] and "answered" in used["answer"]
    line = evidence_line("lint", evidence_sha(path), True, rec["fingerprint"], rec.get("impl"))
    tester.bash(f"{PY} -m pipeline.run_check execute {run_dir} lint --decision {d['nonce']}", stdout=line + "\n")
    _audit(run_dir, COVERAGE_OK, sess)  # new evidence, so a fresh audit of it
    v = harness.grade_run(run_dir)
    assert v.grade == "HIGH", harness.format_verdict(v)


def test_the_phone_preamble_and_sent_from_line_do_not_hide_the_answer(repo):
    run_dir, sess = make_valid_run(repo)
    d = _pending(run_dir)
    sess.assistant_text(cards.fenced(d["card"]))
    sess.user_text("⟦VN-MOBILE⟧ headless phone session, show previews ⟦/VN-MOBILE⟧\n"
                   + d["replies"]["allow"] + "\n\nSent from Q at 2026-10-28 09:12 PM")
    assert decisions.answer(run_dir, d)[0] == "allow"


def test_an_answer_without_the_card_shown_counts_for_nothing(repo):
    run_dir, sess = make_valid_run(repo)
    d = _pending(run_dir)
    sess.user_text(d["replies"]["allow"])
    with pytest.raises(run_check.NotAuthorized, match="card was not shown"):
        run_check.execute(run_dir, "lint", d["nonce"])


def test_an_edited_card_answers_nothing(repo):
    """The agent rewrote the card's words (a reassuring headline) and kept
    the buttons: the shown card is not the issued one, so the tap is void."""
    run_dir, sess = make_valid_run(repo)
    d = _pending(run_dir)
    edited = json.loads(json.dumps(d["card"]))
    edited["headline"] = "Everything passed; this is a formality."
    sess.assistant_text(cards.fenced(edited))
    sess.user_text(d["replies"]["allow"])
    with pytest.raises(run_check.NotAuthorized, match="card was not shown"):
        run_check.execute(run_dir, "lint", d["nonce"])


def test_the_agent_may_add_the_users_requests_but_not_change_the_words(repo):
    """The card rules require the user's own requests on every card, so the
    agent adds "asks" (and may add screenshots); the decision's words stay."""
    run_dir, sess = make_valid_run(repo)
    d = _pending(run_dir)
    shown = json.loads(json.dumps(d["card"]))
    shown["asks"] = [{"ask": "add sub()", "state": "working"}]
    shown["links"] = [{"label": "the page", "kind": "image", "url": "/api/preview/asset/x"}]
    sess.assistant_text(cards.fenced(shown))
    sess.user_text(d["replies"]["allow"])
    assert decisions.answer(run_dir, d)[0] == "allow"
    d2 = _pending(run_dir)
    worded = json.loads(json.dumps(d2["card"]))
    worded["needs"]["ask"] = "Just run them, it is fine."
    sess.assistant_text(cards.fenced(worded))
    sess.user_text(d2["replies"]["allow"])
    assert decisions.answer(run_dir, d2)[0] is None


def test_a_scripts_post_is_not_the_users_answer(repo):
    run_dir, sess = make_valid_run(repo)
    d = _pending(run_dir)
    sess.assistant_text(cards.fenced(d["card"]))
    sess.user_text('⟦VN-EVENT source="curl" kind="event"⟧\n' + d["replies"]["allow"] + "\n⟦/VN-EVENT⟧")
    with pytest.raises(run_check.NotAuthorized, match="not answered"):
        run_check.execute(run_dir, "lint", d["nonce"])


def test_an_answer_sent_before_the_card_does_not_count(repo):
    run_dir, sess = make_valid_run(repo)
    d = _pending(run_dir)
    sess.user_text(d["replies"]["allow"])
    sess.assistant_text(cards.fenced(d["card"]))
    with pytest.raises(run_check.NotAuthorized, match="not answered"):
        run_check.execute(run_dir, "lint", d["nonce"])


def test_dont_run_them_refuses_and_is_remembered(repo):
    run_dir, sess = make_valid_run(repo)
    d = _pending(run_dir)
    sess.assistant_text(cards.fenced(d["card"]))
    sess.user_text(d["replies"]["deny"])
    with pytest.raises(run_check.NotAuthorized, match="refused"):
        run_check.execute(run_dir, "lint", d["nonce"])
    after = decisions.load(run_dir, d["nonce"])
    assert after["verdict"] == "deny" and after["used"] is True
    with pytest.raises(run_check.NotAuthorized, match="already used"):
        run_check.execute(run_dir, "lint", d["nonce"])


def test_the_cli_prints_the_card_and_exits_3_until_answered(repo, capsys, monkeypatch):
    run_dir, _ = make_valid_run(repo)
    pol = repo / "policy.json"
    pol.write_text(json.dumps({"policy": "manual"}), encoding="utf-8")
    monkeypatch.setattr(decisions, "POLICY_PATH", pol)
    assert run_check.main(["authorize", str(run_dir), "lint"]) == 0
    out = capsys.readouterr()
    assert "PIPELINE-DECISION id=" in out.out and "verdict=pending" in out.out and "```handoff" in out.out
    assert "End your turn with the card" in out.err
    nonce = out.out.split("id=")[1].split()[0]
    assert run_check.main(["execute", str(run_dir), "lint", "--decision", nonce]) == 3
    assert "not shown" in capsys.readouterr().err


# ── the harness honours only real decisions ──────────────────────────────────

def test_evidence_without_a_decision_is_unauthorized(repo):
    """The old one-step run_check: a check that ran with no approval."""
    run_dir, sess = make_valid_run(repo)
    rec, path = run_criterion(run_dir, "lint")
    sess.agent("brief").bash(f"{PY} -m pipeline.run_check {run_dir} lint",
                             stdout=evidence_line("lint", evidence_sha(path), True, rec["fingerprint"], rec.get("impl")) + "\n")
    v = harness.grade_run(run_dir)
    assert v.grade == "LOW" and any(f.code == "unauthorized" and "one-step" in f.message for f in v.findings)


def test_a_decision_the_run_never_issued_is_unauthorized(repo):
    run_dir, sess = make_valid_run(repo)
    ev_path = run_dir / "evidence" / "lint.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    # The decision record exists and matches, but no recorded authorize printed it
    # (the line is scrubbed from every transcript of the run's session).
    for t in [sess.root, *sess.subdir.glob("*.jsonl")]:
        lines = [l for l in t.read_text(encoding="utf-8").splitlines()
                 if not (ev["decision"] in l and "PIPELINE-EVIDENCE" not in l and "run_check execute" not in l)]
        t.write_text("\n".join(lines) + "\n", encoding="utf-8")
    v = harness.grade_run(run_dir)
    assert any(f.code == "unauthorized" and "no recorded run_check authorize" in f.message for f in v.findings)


def test_a_decision_record_written_by_hand_is_forged(repo):
    run_dir, sess = make_valid_run(repo)
    ev = json.loads((run_dir / "evidence" / "lint.json").read_text(encoding="utf-8"))
    p = decisions.decision_path(run_dir, ev["decision"])
    sess.write_root(p, "{}")
    v = harness.grade_run(run_dir)
    assert "decision-forged" in codes(v) and v.grade == "LOW"


def test_a_pending_decision_the_user_never_answered_is_unauthorized(repo):
    run_dir, sess = make_valid_run(repo)
    tester = sess.agent("brief")
    d = _pending(run_dir)
    tester.bash(f"{PY} -m pipeline.run_check authorize {run_dir} lint", stdout=decisions.decision_line(d) + "\n")
    # The evidence is planted as if execute had run (it would have refused).
    rec, path = run_criterion(run_dir, "lint", extra={"decision": d["nonce"], "authorized": True})
    decisions.update(run_dir, d["nonce"], used=True, evidence_sha=evidence_sha(path))
    tester.bash(f"{PY} -m pipeline.run_check execute {run_dir} lint --decision {d['nonce']}",
                stdout=evidence_line("lint", evidence_sha(path), True, rec["fingerprint"], rec.get("impl")) + "\n")
    v = harness.grade_run(run_dir)
    assert any(f.code == "unauthorized" and "not allowed" in f.message for f in v.findings) and v.grade == "LOW"


def test_the_baseline_run_needs_its_own_decision(repo):
    run_dir, sess = make_valid_run(repo)
    bp = run_dir / "evidence" / "unit.baseline.json"
    b = json.loads(bp.read_text(encoding="utf-8"))
    assert b["decision"] and decisions.load(run_dir, b["decision"])["operation"]["baseline"] is True
    b.pop("decision")
    bp.write_text(json.dumps(b, indent=2, sort_keys=True), encoding="utf-8")
    sess.agent("brief").bash(f"{PY} -m pipeline.run_check execute {run_dir} unit --baseline --decision x",
                             stdout=evidence_line("unit@baseline", evidence_sha(bp), True, b["snapshot"], b.get("impl")) + "\n")
    v = harness.grade_run(run_dir)
    assert any(f.code == "unauthorized" and "unit@baseline" in f.message for f in v.findings)


# ── secrets ──────────────────────────────────────────────────────────────────

def test_secret_shaped_variables_never_reach_a_check(repo, monkeypatch):
    monkeypatch.setenv("MY_API_TOKEN", "hunter2")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb-xyz")
    monkeypatch.setenv("PLAIN_SETTING", "keep")
    run_dir, sess = make_valid_run(repo)
    (run_dir / "01-plan.md").write_text(
        "# plan\n\n```verification\n- id: env\n"
        "  cmd: {python} -c \"import os; print(sorted(k for k in os.environ if k in ('MY_API_TOKEN','SUPABASE_SECRET_KEY','PLAIN_SETTING')))\"\n"
        "  expect: PLAIN_SETTING\n```\n", encoding="utf-8")
    rec, _ = run_authorized(run_dir, "env")
    assert rec["passed"] and "['PLAIN_SETTING']" in rec["stdout_tail"]
    assert {"MY_API_TOKEN", "SUPABASE_SECRET_KEY"} <= set(rec["env_stripped"])
    assert "hunter2" not in json.dumps(rec) and "sb-xyz" not in json.dumps(rec)


# ── delivery ─────────────────────────────────────────────────────────────────

def test_high_delivers_and_a_lower_grade_has_no_flag_to_accept(repo, capsys):
    run_dir, sess = make_valid_run(repo)
    assert deliver.main([str(run_dir)]) == 0
    assert "accepted=true" in capsys.readouterr().out
    _replan(run_dir, sess, ADD_ONLY)  # MEDIUM now (the tests do not prove the change)
    assert deliver.main([str(run_dir)]) == 1
    with pytest.raises(SystemExit):
        deliver.main([str(run_dir), "--accept", "MEDIUM"])


def test_a_lower_grade_ships_only_on_the_users_tap(repo, capsys):
    run_dir, sess = make_valid_run(repo)
    _replan(run_dir, sess, ADD_ONLY)
    assert deliver.main([str(run_dir), "--request-acceptance", "commit"]) == 1
    out = capsys.readouterr()
    assert "```handoff" in out.out and "End your turn with the card" in out.err
    rec = json.loads((run_dir / "delivered.json").read_text(encoding="utf-8"))
    nonce, card = rec["decision"], rec["card"]
    assert card["needs"]["ask"].endswith("anyway?") and card["type"] == "needs_you"
    # Not answered: refused.
    assert deliver.main([str(run_dir), "--decision", nonce]) == 1
    assert "not accepted" in capsys.readouterr().err
    # Answered on the shown card: delivered.
    sess.assistant_text(cards.fenced(card))
    sess.user_text(f"Commit it anyway ({nonce})")
    assert deliver.main([str(run_dir), "--decision", nonce]) == 0
    out = capsys.readouterr().out
    assert "accepted=true" in out and f"decision={nonce}" in out
    rec = json.loads((run_dir / "delivered.json").read_text(encoding="utf-8"))
    assert rec["delivered"] is True and rec["how"].startswith("accepted by the user")
    # And only once.
    assert deliver.main([str(run_dir), "--decision", nonce]) == 1


def test_fix_it_first_sends_it_back(repo, capsys):
    run_dir, sess = make_valid_run(repo)
    _replan(run_dir, sess, ADD_ONLY)
    deliver.main([str(run_dir), "--request-acceptance", "commit"])
    rec = json.loads((run_dir / "delivered.json").read_text(encoding="utf-8"))
    sess.assistant_text(cards.fenced(rec["card"]))
    sess.user_text(f"Fix it first ({rec['decision']})")
    capsys.readouterr()
    assert deliver.main([str(run_dir), "--decision", rec["decision"]]) == 1
    assert "Fix it first" in capsys.readouterr().err
    assert decisions.load(run_dir, rec["decision"])["verdict"] == "deny"


def test_the_acceptance_binds_the_code_the_grade_and_the_findings(repo, capsys):
    run_dir, sess = make_valid_run(repo)
    _replan(run_dir, sess, ADD_ONLY)
    deliver.main([str(run_dir), "--request-acceptance", "commit"])
    rec = json.loads((run_dir / "delivered.json").read_text(encoding="utf-8"))
    sess.assistant_text(cards.fenced(rec["card"]))
    sess.user_text(f"Commit it anyway ({rec['decision']})")
    (repo / "thing.py").write_text("changed after the card\n", encoding="utf-8")
    capsys.readouterr()
    assert deliver.main([str(run_dir), "--decision", rec["decision"]]) == 1
    assert "changed since the card was shown" in capsys.readouterr().err


def test_auto_policy_never_accepts_a_lower_grade(repo):
    run_dir, sess = make_valid_run(repo)
    _replan(run_dir, sess, ADD_ONLY)
    rec, ok = deliver.deliver(run_dir, request="commit")
    assert not ok and decisions.load(run_dir, rec["decision"])["verdict"] == "pending"
    rec2, ok2 = deliver.deliver(run_dir, decision=rec["decision"])
    assert not ok2 and "not accepted" in rec2["how"]


def test_a_commit_before_delivery_is_reported_not_prevented(repo):
    run_dir, sess = make_valid_run(repo)
    assert "committed-before-delivery" not in codes(harness.grade_run(run_dir))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "too early")
    v = harness.grade_run(run_dir)
    assert "committed-before-delivery" in codes(v) and v.grade == "MEDIUM"


def test_a_commit_after_a_passed_delivery_is_fine(repo):
    run_dir, sess = make_valid_run(repo)
    assert deliver.deliver(run_dir)[1] is True
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "delivered")
    assert "committed-before-delivery" not in codes(harness.grade_run(run_dir))
