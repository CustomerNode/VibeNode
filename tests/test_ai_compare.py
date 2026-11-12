"""AI Compare: launching, prompt integrity, endings, collisions and the report.

Every test drives the real runner (``features.experiments.compare``) and the real session
following code against transcript files written by a fake web server.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from features.experiments import compare as C  # noqa: E402
from features.experiments import sessions as S  # noqa: E402
from tests._fake_sessions import FakeClient, assistant, tool_result, user, write_transcript  # noqa: E402


def answers(table):
    """A behaviour that answers each variant by name with ``table[name]``."""
    def behave(name, prompt, model, cwd, launch_id, project_dir):
        write_transcript(project_dir / f"{launch_id}.jsonl",
                         [user(prompt), assistant(table[name], model=model or "claude-sonnet-5-5", mid=launch_id)])
    return behave


@pytest.fixture(autouse=True)
def _short_stop_retry(monkeypatch):
    # terminate() retries a refused stop three times; the wait between tries
    # is only a courtesy to a real engine.
    monkeypatch.setattr(S, "STOP_RETRY_WAIT", 0.01)


def run(variants, client, tmp_path, **kw):
    kw.setdefault("timeout", 5)
    kw.setdefault("stop_quiet", 0.05)
    return C.run(variants, client=client, root=tmp_path / "runs", settle=0, poll=0.01, out=lambda *_: None, **kw)


def test_multiple_sessions_launch_with_their_models_and_untouched_prompts(tmp_path):
    prompt = "Zip the notes and save archive.zip to Downloads. Then say done."
    catalog = FakeClient(tmp_path, None).catalog
    variants = C.assign_models([("A", prompt), ("B", prompt), ("C", prompt)], ["opus", "sonnet", "haiku"], catalog)
    fake = FakeClient(tmp_path / "proj", answers({"A": "a", "B": "b", "C": "c"}))
    rec = run(variants, fake, tmp_path)
    assert [l["model"] for l in fake.launches] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    # byte for byte: no date rule appended, no file renamed inside it
    assert all(l["prompt"] == prompt for l in fake.launches)
    assert all(l["permission_mode"] == "plan" for l in fake.launches)
    for v in rec["variants"]:
        assert v["original_prompt_sha256"] == v["submitted_prompt_sha256"] == v["received_prompt_sha256"]
        assert v["prompt_received_exactly"] is True
        assert Path(v["dir"], "prompt.txt").read_text(encoding="utf-8") == prompt
    assert rec["differs"] == ["model"] and rec["all_completed"]


def test_what_differs_is_recorded():
    assert C.what_differs([("A", "p", None), ("B", "q", None)]) == ["prompt"]
    assert C.what_differs([("A", "p", None), ("B", "p", None)]) == []


def test_a_session_id_switch_is_followed(tmp_path):
    def behave(name, prompt, model, cwd, launch_id, project_dir):
        real = "real-" + launch_id
        write_transcript(project_dir / f"{real}.jsonl", [user(prompt), assistant("final " + name, mid=real)])
        return real
    rec = run([("A", "p", None), ("B", "p", None)], FakeClient(tmp_path / "proj", behave), tmp_path)
    for v in rec["variants"]:
        assert v["status"] == S.COMPLETED
        assert v["session_id"].startswith("real-") and v["id_history"] == [v["launch_id"], v["session_id"]]
        assert v["text"] == "final " + v["name"]


def test_timeout_failure_and_partial_text_are_never_reported_as_success(tmp_path):
    def behave(name, prompt, model, cwd, launch_id, project_dir):
        recs = {"ok": [user(prompt), assistant("fine", mid=launch_id)],
                "slow": [user(prompt), assistant("half an answer", stop="tool_use", mid=launch_id, tools=["Read"])],
                "broken": [user(prompt), assistant("API Error: overloaded", mid=launch_id, isApiErrorMessage=True,
                                                   error="server_error")]}[name]
        write_transcript(project_dir / f"{launch_id}.jsonl", recs)
    rec = run([("ok", "p", None), ("slow", "p", None), ("broken", "p", None)],
              FakeClient(tmp_path / "proj", behave), tmp_path, timeout=0.3)
    by = {v["name"]: v for v in rec["variants"]}
    assert by["ok"]["status"] == S.COMPLETED
    assert by["slow"]["status"] == S.TIMEOUT and by["slow"]["partial"] and by["slow"]["text"] == "half an answer"
    assert by["broken"]["status"] == S.FAILED and "server_error" in by["broken"]["error"]
    assert not rec["all_completed"]
    report = Path(rec["report"]).read_text(encoding="utf-8")
    assert "**timeout**" in report and "partial text kept, not a finished answer" in report
    assert "| slow | (timeout) |" in report and "| broken | (failed) |" in report


def test_configurable_timeout_and_cancellation(tmp_path):
    def behave(name, prompt, model, cwd, launch_id, project_dir):
        write_transcript(project_dir / f"{launch_id}.jsonl", [user(prompt)])
    fake = FakeClient(tmp_path / "proj", behave)
    runs = [S.Run(key="a", name="a", prompt="p"), S.Run(key="b", name="b", prompt="p")]
    S.launch_all(fake, runs)
    cancel = tmp_path / "CANCEL"
    cancel.write_text("")
    S.wait_all(runs, timeout=60, settle=0, poll=0.01, cancel_file=cancel)
    assert {r.status for r in runs} == {S.CANCELLED}


def test_a_server_that_ignores_read_only_is_refused_not_trusted(tmp_path):
    fake = FakeClient(tmp_path / "proj", answers({"A": "a", "B": "b"}), applied="ignore_mode")
    rec = run([("A", "p", None), ("B", "p", None)], fake, tmp_path)
    assert all(v["status"] == S.FAILED and "did not confirm read-only" in v["error"] for v in rec["variants"])
    # it is running with full access, so it is stopped and the stop confirmed, not merely marked
    assert sorted(set(fake.closed)) == sorted(v["session_id"] for v in rec["variants"])
    assert all(v["termination"] == "confirmed" for v in rec["variants"])
    fake = FakeClient(tmp_path / "proj2", answers({"A": "a", "B": "b"}), applied=None)  # route predates the field
    rec = run([("A", "p", None), ("B", "p", None)], fake, tmp_path)
    assert not rec["all_completed"] and len(set(fake.closed)) == 2
    stuck = FakeClient(tmp_path / "proj3", answers({"A": "a", "B": "b"}), applied="ignore_mode", close_ok=False)
    rec = run([("A", "p", None), ("B", "p", None)], stuck, tmp_path)
    assert all(v["termination"].startswith("unconfirmed") for v in rec["variants"])
    assert "could not confirm the session stopped" in Path(rec["report"]).read_text(encoding="utf-8")


def test_a_change_that_went_through_is_flagged_and_a_refused_one_is_not(tmp_path):
    def behave(name, prompt, model, cwd, launch_id, project_dir):
        recs = [user(prompt), assistant("", stop="tool_use", mid=f"{launch_id}-1", tools=["Write"])]
        recs.append(tool_result(f"tu-{launch_id}-1-0", is_error=(name == "refused")))
        recs.append(assistant("answer", mid=f"{launch_id}-2"))
        write_transcript(project_dir / f"{launch_id}.jsonl", recs)
    rec = run([("wrote", "p", None), ("refused", "p", None)], FakeClient(tmp_path / "proj", behave), tmp_path)
    by = {v["name"]: v for v in rec["variants"]}
    assert by["wrote"]["read_only_breached"] and not by["refused"]["read_only_breached"]
    assert "read-only was not enforced" in Path(rec["report"]).read_text(encoding="utf-8")


def test_outputs_cannot_collide(tmp_path):
    fake = FakeClient(tmp_path / "proj", answers({"A b": "one", "a-b": "two"}))
    r1 = run([("A b", "p", None), ("a-b", "p", None)], fake, tmp_path)
    r2 = run([("A b", "p", None), ("a-b", "p", None)], fake, tmp_path)
    dirs = [v["dir"] for v in r1["variants"]] + [v["dir"] for v in r2["variants"]]
    assert len(set(dirs)) == 4 and r1["run_id"] != r2["run_id"]
    assert Path(r1["variants"][0]["reply_file"]).read_text(encoding="utf-8") == "one"
    assert Path(r1["variants"][1]["reply_file"]).read_text(encoding="utf-8") == "two"
    with pytest.raises(FileExistsError):
        run([("A", "p", None), ("B", "p", None)], fake, tmp_path, run_id=r1["run_id"])


def test_the_report_rows_belong_to_their_variants(tmp_path):
    fake = FakeClient(tmp_path / "proj", answers({"A": "Paris is the capital", "B": "Lyon, I think"}))
    rec = run([("A", "p", None), ("B", "p", None)], fake, tmp_path)
    report = Path(rec["report"]).read_text(encoding="utf-8")
    assert "| A | Paris is the capital |" in report and "| B | Lyon, I think |" in report
    by = {v["name"]: v for v in rec["variants"]}
    assert by["A"]["cost_usd"] == fake.costs[by["A"]["session_id"]]
    assert by["B"]["cost_usd"] == fake.costs[by["B"]["session_id"]]
    assert "## Judgment" in report and "judgment, not measurement" in report
    saved = json.loads((Path(rec["report"]).parent / "run.json").read_text(encoding="utf-8"))
    assert [v["name"] for v in saved["variants"]] == ["A", "B"]


def test_measure_counts_each_call_once_and_ignores_hook_feedback(tmp_path):
    u = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 1000}
    write_transcript(tmp_path / "s.jsonl", [
        user("the assignment"),
        assistant("", stop="tool_use", mid="m1", usage=u, tools=["Read"]),
        assistant("", stop="tool_use", mid="m1", usage=u, tools=["Grep"]),  # same API message, two records
        user("Stop hook feedback:\nSmart termination: sent back"),
        assistant("done", mid="m2", usage=u),
        user("actually, also check the README"),
        assistant("checked", mid="m3", usage=u),
    ])
    write_transcript(tmp_path / "s" / "subagents" / "agent-1.jsonl",
                     [assistant("sub", mid="x1", usage=u, isSidechain=True)])
    m = S.measure(tmp_path, "s")
    assert m["model_calls"] == 4 and m["subagent"]["model_calls"] == 1
    assert m["tokens"]["input"] == 400 and m["tokens"]["output"] == 80
    assert m["human_interventions"] == 1
    assert m["tools"] == {"Read": 1, "Grep": 1}


def test_old_ab_test_command_lines_still_work(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("ai_compare_cli", ROOT / "scripts" / "ai_compare.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(C, "ROOT", tmp_path / "runs")
    monkeypatch.setattr(C, "run", lambda variants, **kw: _fake_run(variants, tmp_path))
    seen = {}

    def _fake_run(variants, tmp):
        seen["variants"] = variants
        rep = tmp / "report.md"
        rep.write_text("r", encoding="utf-8")
        return {"report": str(rep), "all_completed": True}
    fake = FakeClient(tmp_path, None)
    assert cli.main(["--prompt", "who", "--models", "opus", "sonnet"], client=fake) == 0
    assert [(m["id"]) for _, _, m in seen["variants"]] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert cli.main(["short=one line", "long=a paragraph"], client=fake) == 0
    assert [(n, p) for n, p, _ in seen["variants"]] == [("short", "one line"), ("long", "a paragraph")]
    # the old script name forwards to AI Compare and keeps its helpers importable
    spec = importlib.util.spec_from_file_location("ab_test_compat", ROOT / "scripts" / "ab_test.py")
    ab = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ab)
    assert ab.build_variants([], "p", [], 3, []) == [("Compare A", "p"), ("Compare B", "p"), ("Compare C", "p")]
    assert ab.resolve_id is S.resolve_id and ab.short_answer is S.short_answer


def test_ai_compare_never_loads_the_controlled_experiment_machinery():
    src = (ROOT / "src" / "features" / "experiments" / "compare.py").read_text(encoding="utf-8") + \
          (ROOT / "src" / "features" / "experiments" / "sessions.py").read_text(encoding="utf-8") + \
          (ROOT / "scripts" / "ai_compare.py").read_text(encoding="utf-8")
    assert "controlled" not in src.replace("Controlled A/B Test", "")


ODD_PROMPTS = [
    "   leading spaces",
    "trailing spaces   ",
    "line one\n\n\n\nline five after blank lines",
    "Special: \u00e9\u00e8 \u2014 $HOME `tick` {braces} <tag> 100% \\back\\slash\t|pipe|",
    "  - an intentional\n    - indented\n  - list\n",
]


def test_exact_prompts_reach_the_session_and_are_checked_against_its_transcript(tmp_path):
    variants = [(f"V{i}", p, None) for i, p in enumerate(ODD_PROMPTS)]
    fake = FakeClient(tmp_path / "proj", answers({f"V{i}": "ok" for i in range(len(ODD_PROMPTS))}))
    rec = run(variants, fake, tmp_path)
    assert [l["prompt"] for l in fake.launches] == ODD_PROMPTS  # submitted byte for byte
    for v, original in zip(rec["variants"], ODD_PROMPTS):
        assert v["prompt_received_exactly"] is True
        assert Path(v["dir"], "prompt.txt").read_bytes().decode("utf-8") == original
        assert Path(v["dir"], "received_prompt.txt").read_bytes().decode("utf-8") == original
    assert "received its prompt exactly as written" in Path(rec["report"]).read_text(encoding="utf-8")


def test_a_prompt_changed_on_the_way_is_shown_not_assumed(tmp_path):
    def behave(name, prompt, model, cwd, launch_id, project_dir):
        got = prompt.strip() if name == "trimmed" else prompt  # as an older server would
        write_transcript(project_dir / f"{launch_id}.jsonl", [user(got), assistant("ok", mid=launch_id)])
    rec = run([("trimmed", "  spaced  ", None), ("exact", "  spaced  ", None)],
              FakeClient(tmp_path / "proj", behave), tmp_path)
    by = {v["name"]: v for v in rec["variants"]}
    assert by["trimmed"]["prompt_received_exactly"] is False and by["exact"]["prompt_received_exactly"] is True
    report = Path(rec["report"]).read_text(encoding="utf-8")
    assert "received a different prompt" in report


def test_a_timed_out_variant_is_stopped_through_vibenode(tmp_path):
    def behave(name, prompt, model, cwd, launch_id, project_dir):
        recs = [user(prompt)] + ([assistant("done", mid=launch_id)] if name == "quick" else
                                 [assistant("still going", stop="tool_use", mid=launch_id, tools=["Read"])])
        write_transcript(project_dir / f"{launch_id}.jsonl", recs)
    fake = FakeClient(tmp_path / "proj", behave)
    rec = run([("quick", "p", None), ("slow", "p", None)], fake, tmp_path, timeout=0.3)
    by = {v["name"]: v for v in rec["variants"]}
    assert fake.closed == [by["slow"]["session_id"]]  # only the unfinished one is stopped
    assert by["slow"]["termination"] == "confirmed" and by["quick"]["termination"] == ""
    stuck = FakeClient(tmp_path / "proj2", behave, close_ok=False)
    rec = run([("quick", "p", None), ("slow", "p", None)], stuck, tmp_path, timeout=0.3)
    slow = {v["name"]: v for v in rec["variants"]}["slow"]
    assert slow["termination"].startswith("unconfirmed")
    assert "could not confirm the session stopped" in Path(rec["report"]).read_text(encoding="utf-8")


def test_a_transcript_caught_mid_write_is_read_on_the_next_poll(tmp_path, monkeypatch):
    path = tmp_path / "s.jsonl"
    whole = (json.dumps(user("q")) + "\n" + json.dumps(assistant("café done", mid="m"), ensure_ascii=False)).encode("utf-8")
    path.write_bytes(whole[:-12])  # cut inside the last record
    assert S.read_outcome(path)["state"] == "running"
    cut_in_char = whole.index("é".encode("utf-8")) + 1
    path.write_bytes(whole[:cut_in_char])  # cut in the middle of a two-byte character
    assert S.read_outcome(path)["state"] == "running"
    path.write_bytes(whole)
    assert S.read_outcome(path)["text"] == "café done"

    def boom(self):
        raise MemoryError
    monkeypatch.setattr(Path, "read_bytes", boom)
    assert S.read_outcome(path)["state"] == "running"
