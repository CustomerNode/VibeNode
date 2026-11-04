"""The capability ledger counts only proven, finished work, and only the
owner's own attention (scripts/capability_ledger.py)."""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import capability_ledger as cl  # noqa: E402


def _card(**over):
    card = {"type": "done", "title": "Export button", "headline": "Export works",
            "verified": [{"check": "export test", "result": "passed", "ok": True}]}
    card.update(over)
    return "Finished.\n```handoff\n" + json.dumps(card) + "\n```"


def _user(ts, text, **extra):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}, **extra}


def _bot(ts, text):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _write(root, project, sid, entries):
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return d


def test_a_capability_needs_a_finished_card_with_a_passing_check():
    assert cl.is_capability(json.loads(_card().split("\n", 2)[2].rsplit("\n```", 1)[0]))
    assert not cl.is_capability({"type": "done", "verified": []})
    assert not cl.is_capability({"type": "done", "verified": [{"ok": True}, {"ok": False}]})
    assert not cl.is_capability({"type": "done", "verified": [{"ok": True}],
                                 "status": {"remaining": ["wire it up"]}})
    assert not cl.is_capability({"type": "explain", "verified": [{"ok": True}]})


def test_counts_his_messages_and_skips_refused_cards_and_automated_ones(tmp_path):
    _write(tmp_path, "C--Users-x-Documents-Proj", "s1", [
        _user("2026-11-01T10:00:00Z", "add an export button"),
        _bot("2026-11-01T10:05:00Z", _card(title="Too long version")),
        _user("2026-11-01T10:05:01Z", "Stop hook feedback: too long"),
        _user("2026-11-01T10:06:00Z", "[VibeNode watchdog] you stopped early"),
        _bot("2026-11-01T10:07:00Z", _card()),
        _user("2026-11-01T10:08:00Z", "Done"),
    ])
    [row] = cl.build(tmp_path)
    assert row["capability"] == "Export button"
    assert row["interventions"] == 1          # the watchdog and the refusal are not the owner's
    assert row["accepted"] is True            # the owner's next message said Done
    assert row["project"] == "Proj"


def test_attention_is_one_timeline_across_parallel_sessions(tmp_path):
    _write(tmp_path, "P", "a", [
        _user("2026-11-01T10:00:00Z", "task a"),
        _user("2026-11-01T10:10:00Z", "more on a"),
        _bot("2026-11-01T10:11:00Z", _card(title="A")),
    ])
    _write(tmp_path, "P", "b", [
        _user("2026-11-01T10:05:00Z", "task b"),
        _bot("2026-11-01T10:06:00Z", _card(title="B")),
    ])
    rows = {r["capability"]: r for r in cl.build(tmp_path)}
    # 10:00 first message (1 min), 10:05 in b (5 min), 10:10 in a (5 min):
    # eleven minutes in all, never the 10 + 5 of two separate clocks.
    assert rows["A"]["attention_min"] + rows["B"]["attention_min"] == 11
    assert rows["B"]["attention_min"] == 5


def test_his_done_mark_in_the_sidebar_counts_as_accepted(tmp_path):
    d = _write(tmp_path, "P", "s", [
        _user("2026-11-01T10:00:00Z", "fix it"),
        _bot("2026-11-01T10:01:00Z", _card()),
    ])
    (d / "_session_holds.json").write_text(json.dumps(
        {"s": {"state": "done", "at": 0, "set": 1893456000}}), encoding="utf-8")
    [row] = cl.build(tmp_path)
    assert row["accepted"] is True


def test_the_report_page_is_written(tmp_path):
    _write(tmp_path, "P", "s", [_user("2026-11-01T10:00:00Z", "go"),
                                _bot("2026-11-01T10:01:00Z", _card())])
    out, page = tmp_path / "ledger.jsonl", tmp_path / "ledger.html"
    assert cl.main(["--projects", str(tmp_path), "--out", str(out), "--html", str(page)]) == 0
    assert json.loads(out.read_text(encoding="utf-8").splitlines()[0])["capability"] == "Export button"
    assert "1 verified capabilities" in page.read_text(encoding="utf-8")
