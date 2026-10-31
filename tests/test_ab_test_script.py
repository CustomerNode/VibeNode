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


CATALOG = [
    {"id": "claude-opus-5-5[1m]", "name": "Opus 5.5[1m]"},
    {"id": "claude-opus-5-5", "name": "Opus 5.5"},
    {"id": "claude-opus-4-7", "name": "Opus 4.7"},
    {"id": "claude-sonnet-5-5", "name": "Sonnet 5.5"},
    {"id": "claude-sonnet-5", "name": "Sonnet 5"},
    {"id": "claude-haiku-4-5", "name": "Haiku 4.5"},
]


def test_friendly_model_names_resolve_to_ids():
    r = lambda t: ab.resolve_model(t, CATALOG)["id"]
    assert r("opus") == "claude-opus-5-5"          # newest, without 1M
    assert r("Sonnet") == "claude-sonnet-5-5"
    assert r("sonnet 5") == "claude-sonnet-5"
    assert r("Haiku 4.5") == "claude-haiku-4-5"
    assert r("claude-opus-4-7") == "claude-opus-4-7"
    assert r("opus 5.5 [1m]") == "claude-opus-5-5[1m]"
    import pytest
    with pytest.raises(ValueError):
        r("gpt")


def test_models_pair_with_variants_one_for_all_or_one_each():
    v = [("A", "p"), ("B", "p"), ("C", "p")]
    each = ab.assign_models(v, ["opus", "sonnet", "haiku"], CATALOG)
    assert [m["id"] for _, _, m in each] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"]
    one = ab.assign_models(v, ["sonnet"], CATALOG)
    assert {m["id"] for _, _, m in one} == {"claude-sonnet-5-5"}
    assert [m for _, _, m in ab.assign_models(v, [], CATALOG)] == [None, None, None]
    import pytest
    with pytest.raises(ValueError):
        ab.assign_models(v, ["opus", "sonnet"], CATALOG)


def test_a_model_that_did_not_take_is_caught():
    assert ab._same_model("claude-opus-5-5", "claude-opus-5-5")
    assert ab._same_model("claude-opus-5-5[1m]", "claude-opus-5-5")
    assert not ab._same_model("claude-haiku-4-5", "claude-opus-5-5")


# -- Every Downloads file a variant saves is dated (owner's rule, 2026-10-31) --

_DELIVER = ("Run the tests. When finished, create archive-session-standard.zip in my Downloads folder "
            "containing the patch.\nRead notes.md before you start.")


def test_downloads_file_names_get_the_run_date():
    out = ab.date_downloads(_DELIVER, "2026-10-31")
    assert "archive-session-standard-2026-10-31.zip in my Downloads" in out
    assert "archive-session-standard.zip" not in out
    assert "Read notes.md before" in out          # not a Downloads sentence: left alone
    assert out.rstrip().endswith("must never overwrite a file already there.")
    assert "2026-10-31" in out.split("A/B test rule:")[1]


def test_an_already_dated_name_is_left_alone():
    p = "Save report-2026-10-31.pdf to Downloads."
    assert ab.download_names(p) == []
    assert ab.date_downloads(p, "2026-11-02").startswith(p)


def test_no_download_mentioned_still_gets_the_rule():
    out = ab.date_downloads("who is the president", "2026-10-31")
    assert out.startswith("who is the president") and "A/B test rule" in out


def test_a_taken_dated_name_adds_the_time(tmp_path):
    import time as _t
    now = _t.mktime((2026, 10, 31, 14, 5, 0, 0, 0, -1))
    names = ["x.zip"]
    assert ab.dated_stamp(names, tmp_path, now) == "2026-10-31"
    (tmp_path / "x-2026-10-31.zip").write_text("earlier run")
    assert ab.dated_stamp(names, tmp_path, now) == "2026-10-31-1405"


def test_undated_new_downloads_are_reported(tmp_path):
    import os, time as _t
    old = tmp_path / "old.zip"; old.write_text("o")
    os.utime(old, (1, 1))
    (tmp_path / "new.zip").write_text("n")
    (tmp_path / "new-2026-10-31.zip").write_text("n")
    assert ab.undated_new_downloads(tmp_path, _t.time() - 60) == ["new.zip"]
