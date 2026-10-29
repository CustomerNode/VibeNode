"""The delivery gate: recompute the grade; below HIGH, only the user accepts.

    python -m pipeline.deliver <run-dir>
    python -m pipeline.deliver <run-dir> --request-acceptance <commit|merge|push|package|deploy>
    python -m pipeline.deliver <run-dir> --decision <id>

Run immediately before any delivery action.  It recomputes the verdict on
the tree as it is now (never trusts ``verdict.json``), writes
``delivered.json`` into the run, and prints::

    PIPELINE-DELIVERED grade=<grade> fingerprint=<12> accepted=<true|false> [decision=<id>]

Exit 0 only when the grade is HIGH, or when the USER accepted the computed
lower grade: ``--request-acceptance`` issues a decision bound to this run,
this grade, this source fingerprint, these findings and this action, and
prints the handoff card the agent ends its turn with (``pipeline.cards``);
the user's tap on "Commit it anyway" sends the card's reply as his message,
and ``--decision`` passes only when the run's transcript holds that message
(``pipeline.decisions.answer``) and nothing has changed since.  There is no
flag an agent can type to accept a grade for the user.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline import cards, decisions
from pipeline.harness import Verdict, grade_run, write_verdict
from pipeline.plan import request_text_of

DELIVERED_PREFIX = "PIPELINE-DELIVERED"
ACTIONS = ("commit", "merge", "push", "package", "deploy")


def _operation(v: Verdict, action: str) -> dict:
    return {
        "run_id": v.facts.get("run_id"),
        "grade": v.grade,
        "fingerprint": v.facts.get("fingerprint"),
        "findings": sorted({f.code for f in v.findings if f.severity != "info"}),
        "action": action,
    }


def _task(run_dir: Path) -> str:
    p = run_dir / "00-request.md"
    text = request_text_of(p.read_text(encoding="utf-8", errors="replace")) if p.is_file() else ""
    return cards.task_name(text or "this task")


def deliver(run_dir: Path, *, request: str | None = None, decision: str | None = None) -> tuple[dict, bool]:
    """Returns (record, delivered).  The record is also written to
    ``delivered.json``; a pending request's record carries the card."""
    run_dir = run_dir.resolve()
    v = grade_run(run_dir)
    if run_dir.is_dir():
        write_verdict(run_dir, v)
    rec = {
        "grade": v.grade,
        "fingerprint": v.facts.get("fingerprint"),
        "delivered": v.grade == "HIGH",
        "how": "grade" if v.grade == "HIGH" else "",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "findings": [f.code for f in v.findings if f.severity != "info"],
    }
    if v.grade != "HIGH" and v.grade != "BLOCKED":
        if decision:
            d = decisions.load(run_dir, decision)
            op = _operation(v, str((d.get("operation") or {}).get("action") or ""))
            rec["decision"] = decision
            if d.get("kind") != "delivery":
                rec["how"] = f"decision {decision} is not a delivery decision"
            elif d.get("used"):
                rec["how"] = f"decision {decision} was already used"
            elif decisions.operation_sha(op) != d.get("operation_sha"):
                rec["how"] = "the result changed since the card was shown (code, grade or findings); request again"
            else:
                ans, detail = decisions.answer(run_dir, d)
                if ans == "allow":
                    rec["delivered"] = True
                    rec["how"] = f"accepted by the user: {detail}"
                    rec["action"] = op["action"]
                    decisions.update(run_dir, decision, verdict="allow", used=True, used_at=decisions._now(),
                                     answer=detail)
                elif ans == "deny":
                    rec["how"] = f"the user chose Fix it first: {detail}"
                    decisions.update(run_dir, decision, verdict="deny", used=True, used_at=decisions._now(),
                                     answer=detail)
                else:
                    rec["how"] = f"not accepted: {detail}"
        elif request:
            op = _operation(v, request)
            task = _task(run_dir)
            verdict_dict = v.to_dict()
            d = decisions.issue(run_dir, "delivery", op,
                                build=lambda nonce: cards.delivery_card(verdict_dict, request, nonce, task))
            rec["decision"] = d["nonce"]
            rec["card"] = d["card"]
            rec["how"] = "waiting for the user's answer on the card"
    if run_dir.is_dir():
        (run_dir / "delivered.json").write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    return rec, bool(rec["delivered"])


def delivered_line(rec: dict) -> str:
    line = (f"{DELIVERED_PREFIX} grade={rec['grade']} fingerprint={str(rec.get('fingerprint') or '')[:12]} "
            f"accepted={'true' if rec['delivered'] else 'false'}")
    return line + (f" decision={rec['decision']}" if rec.get("decision") else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.deliver")
    ap.add_argument("run_dir")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--request-acceptance", choices=ACTIONS, default=None, metavar="ACTION",
                   help="below HIGH: ask the user, on a card, to accept this exact result for ACTION")
    g.add_argument("--decision", default=None, help="the decision id of the card the user answered")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        rec, ok = deliver(Path(a.run_dir), request=a.request_acceptance, decision=a.decision)
    except decisions.DecisionError as e:
        print(f"deliver: {e}", file=sys.stderr)
        return 2
    print(delivered_line(rec))
    if rec.get("card"):
        print()
        print(cards.fenced(rec["card"]))
        print("deliver: the grade is below HIGH. End your turn with the card above, alone; when the user "
              "answers it, run deliver again with --decision and this id.", file=sys.stderr)
    elif not ok:
        print(f"deliver: grade is {rec['grade']}, not HIGH; {rec.get('how') or 'findings: ' + (', '.join(rec['findings']) or 'none')}. "
              f"Not delivered as verified.", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
