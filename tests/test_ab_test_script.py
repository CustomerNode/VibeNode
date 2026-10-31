"""scripts/ab_test.py follows a session's id switch and knows when it is done."""

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "ab_test", Path(__file__).resolve().parents[1] / "scripts" / "ab_test.py")
ab = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ab)


def _write(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_resolve_follows_chained_remaps(tmp_path):
    (tmp_path / "_remapped_sessions.json").write_text(json.dumps({
        "launch-x": {"new_id": "mid-x", "ts": 1},
        "mid-x": {"new_id": "real-x", "ts": 2},
    }))
    assert ab.resolve_id(tmp_path, "launch-x") == "real-x"
    assert ab.resolve_id(tmp_path, "unknown-x") == "unknown-x"


def test_resolve_survives_a_cycle(tmp_path):
    (tmp_path / "_remapped_sessions.json").write_text(json.dumps({
        "p-x": {"new_id": "q-x"}, "q-x": {"new_id": "p-x"}}))
    assert ab.resolve_id(tmp_path, "p-x") in ("p-x", "q-x")


def test_done_only_on_a_main_thread_end_turn(tmp_path):
    f = tmp_path / "s.jsonl"
    user = {"type": "user", "message": {"content": "question"}}
    tool = {"type": "assistant", "message": {"stop_reason": "tool_use", "content": [
        {"type": "tool_use", "name": "WebSearch"}]}}
    _write(f, [user, tool])
    assert ab.read_result(f) is None

    side = {"type": "assistant", "isSidechain": True,
            "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "x"}]}}
    _write(f, [user, tool, side])
    assert ab.read_result(f) is None

    final = {"type": "assistant", "message": {"stop_reason": "end_turn", "model": "m",
             "content": [{"type": "text", "text": "the answer"}]}}
    _write(f, [user, tool, side, final])
    res = ab.read_result(f)
    assert res["text"] == "the answer" and res["tools"] == ["WebSearch"]

    sent_back = {"type": "user", "message": {"content": "Stop hook feedback: redo"}}
    _write(f, [user, tool, final, sent_back])
    assert ab.read_result(f) is None


def test_any_number_of_variants_same_or_different_prompts():
    same = ab.build_variants([], "q", [], 3, [])
    assert same == [("A/B test A", "q"), ("A/B test B", "q"), ("A/B test C", "q")]
    each = ab.build_variants([], None, [], 0, ["p1", "p2", "p3", "p4"])
    assert [n for n, _ in each] == ["A/B test A", "A/B test B", "A/B test C", "A/B test D"]
    assert [p for _, p in each] == ["p1", "p2", "p3", "p4"]
    named = ab.build_variants(["x=one", "y=two=still two"], None, [], 0, [])
    assert named == [("x", "one"), ("y", "two=still two")]
    assert ab.variant_name(25) == "A/B test Z" and ab.variant_name(26) == "A/B test AA"


def test_one_variant_or_a_repeated_name_is_refused():
    import pytest
    with pytest.raises(ValueError):
        ab.build_variants([], "q", [], 1, [])
    with pytest.raises(ValueError):
        ab.build_variants(["x=1", "x=2"], None, [], 0, [])


def test_short_answer_reads_the_card():
    text = 'intro\n```handoff\n{"headline": "h", "answer": "short one"}\n```'
    assert ab.short_answer(text) == "short one"
    assert ab.short_answer("plain reply") == "plain reply"
