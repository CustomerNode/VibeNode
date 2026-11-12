"""A summary checked in one session never passes another session's stop.

Each session has its own draft folder, named by its session id
(``handoff_check.draft_path``), so one session never checks or sends a summary
another session wrote.  The stamp that says "the check passed this card"
carries the session id too: a stamp named by the card's content alone would
let a card checked in session B satisfy session A's stop.
"""
import json
from pathlib import Path

from engine import handoff_check as hc
from engine import smart_termination as st
from tests.test_handoff_check import GOOD, _msg, _on

REPO = Path(__file__).resolve().parents[1]
A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


def test_a_card_checked_by_another_session_is_refused(monkeypatch, tmp_path):
    _on(monkeypatch, tmp_path)
    assert hc.check(json.dumps(GOOD), session_id=B)["ok"]
    reason = st.review(_msg(GOOD), continued=False, sendbacks=0, session_id=A)
    assert reason and ("Not checked" in reason or "another session" in reason)


def test_a_card_checked_by_its_own_session_goes_through(monkeypatch, tmp_path):
    _on(monkeypatch, tmp_path)
    assert hc.check(json.dumps(GOOD), session_id=A)["ok"]
    assert st.review(_msg(GOOD), continued=False, sendbacks=0, session_id=A) is None
    assert st.card_checked(GOOD, A) and not st.card_checked(GOOD, B)


def test_the_stamp_is_named_by_session(monkeypatch, tmp_path):
    _on(monkeypatch, tmp_path)
    hc.check(json.dumps(GOOD), session_id=A)
    names = [p.name for p in (tmp_path / "checked").iterdir()]
    assert any(A in n for n in names)


def test_the_command_line_check_stamps_for_its_own_session(monkeypatch, tmp_path):
    import io
    _on(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", A)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(GOOD)))
    assert hc.main([]) == 0
    assert st.card_checked(GOOD, A) and not st.card_checked(GOOD, B)


def test_both_stops_name_the_session_they_judge():
    hook = (REPO / "src" / "engine" / "smart_termination_hook.py").read_text("utf-8")
    seg = hook[hook.index("reason = st.review("):][:400]
    assert "session_id=" in seg
    eng = (REPO / "src" / "engine" / "session_manager.py").read_text("utf-8")
    seg = eng[eng.index("reason = _st.review(message"):][:400]
    assert "cli_session_id or sid" in seg
    assert "session_id=" in seg


def test_the_engine_hands_the_stop_the_clis_own_session_id():
    cl = (REPO / "src" / "engine" / "backends" / "claude.py").read_text("utf-8")
    seg = cl[cl.index("async def _stop_wrap"):][:1500]
    assert "kw[\"cli_session_id\"] = str(data.get(\"session_id\")" in seg


# ── A summary another session already checked is that session's ──────────
# Two sessions never write the identical summary.  So when session A runs the
# check on a summary session B checked first (it picked up B's draft, as the
# shared file let it), the check refuses it, and so does A's stop.

def test_checking_a_summary_another_session_checked_first_is_refused(monkeypatch, tmp_path):
    _on(monkeypatch, tmp_path)
    assert hc.check(json.dumps(GOOD), session_id=B)["ok"]
    res = hc.check(json.dumps(GOOD), session_id=A)
    assert not res["ok"] and "another session" in res["reason"]
    assert not st.card_checked(GOOD, A)


def test_the_stop_refuses_a_summary_first_checked_elsewhere(monkeypatch, tmp_path):
    _on(monkeypatch, tmp_path)
    hc.check(json.dumps(GOOD), session_id=B)
    # Even with a stamp of its own (written by an older check), A does not own it.
    st.stamp_card(GOOD, A)
    reason = st.review(_msg(GOOD), continued=False, sendbacks=0, session_id=A)
    assert reason and "another session" in reason
    # B, which checked it first, still ends its turn on it, even after A's stamp.
    assert st.review(_msg(GOOD), continued=False, sendbacks=0, session_id=B) is None


def test_checking_your_own_summary_again_is_fine(monkeypatch, tmp_path):
    _on(monkeypatch, tmp_path)
    assert hc.check(json.dumps(GOOD), session_id=A)["ok"]
    assert hc.check(json.dumps(GOOD), session_id=A)["ok"]
    assert st.review(_msg(GOOD), continued=False, sendbacks=0, session_id=A) is None


# ── No draft file at all: nothing on disk for two sessions to share ──────

def test_no_draft_file_exists_to_share():
    assert not hasattr(hc, "draft_path") and not hasattr(hc, "foreign_draft")
