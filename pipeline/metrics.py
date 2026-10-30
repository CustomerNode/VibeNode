"""Where a run's time and tokens went, and what that effort produced.

    python -m pipeline.metrics <run-dir> [--json] [--prices FILE]

Computed from the same session records the harness grades (the root
transcript and its subagent transcripts), so nothing has to be logged by
hand and no agent writes a report about itself.  The harness computes it
after every grading and writes ``metrics.json`` beside ``verdict.json``;
it never changes a grade.

Per stage execution (the root session, and each subagent, named by the
stage file it wrote):

* start and finish (first and last recorded entry), elapsed wall clock;
* model calls (distinct ``message.id`` of assistant entries: the CLI splits
  one response into several entries that repeat the same usage), and tokens:
  input, output, cache writes, cache reads;
* API-equivalent cost, only when a price table is given (``--prices`` or the
  ``PIPELINE_PRICES`` environment variable: a JSON file of per-million-token
  prices per model, ``{"<model>": {"input": .., "output": .., "cache_write":
  .., "cache_write_1h": .., "cache_read": ..}}``).  Without one, cost is
  reported as not measured: prices are not guessed here;
* verification executions it ran, and the register items it raised and
  settled.

Per run: wall clock from the start line to the last recorded entry; the sum
of the stage executions' own elapsed time; the time subagents overlapped
each other; every repeated execution of a criterion with its cause, read
from the records (the tree changed since the previous run: revalidation;
the same tree after a pass: avoidable; the same tree after a failure: a
retry), and every ``run_check`` call that produced no evidence (an
orchestration error), with the error it printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pipeline.evidence import EVIDENCE_PREFIX
from pipeline.transcripts import _iter_entries, is_run_check_call

STAGE_FILE_RE = re.compile(r"(\d{2}-[a-z][a-z0-9-]*\.md)$")
_EVIDENCE_LINE_RE = re.compile(rf"{EVIDENCE_PREFIX} id=(\S+) sha=(\S+) passed=(true|false) fingerprint=(\S+)")
TOKEN_KEYS = ("input", "output", "cache_write", "cache_write_1h", "cache_read")


def _ts(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass
class StageRun:
    name: str
    agent: str
    start: datetime | None = None
    end: datetime | None = None
    calls: int = 0
    tokens: dict = field(default_factory=lambda: dict.fromkeys(TOKEN_KEYS, 0))
    by_model: dict = field(default_factory=dict)
    checks: int = 0
    raised: list = field(default_factory=list)
    settled: list = field(default_factory=list)

    @property
    def elapsed_s(self) -> float:
        return (self.end - self.start).total_seconds() if self.start and self.end else 0.0


def _usage_tokens(u: dict) -> dict:
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    write_1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    write_all = int(u.get("cache_creation_input_tokens") or 0)
    return {"input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0),
            "cache_write": max(write_all - write_1h, 0), "cache_write_1h": write_1h,
            "cache_read": int(u.get("cache_read_input_tokens") or 0)}


def read_usage(path: Path, stage: StageRun, since: datetime | None, until: datetime | None = None) -> None:
    """Fill *stage* with the timing and usage recorded in one transcript,
    counting only entries from *since* to *until* (a session can hold other
    work before the run starts and after it ends)."""
    seen: set[str] = set()
    for e in _iter_entries(path):
        t = _ts(e.get("timestamp") or "")
        if t is None or (since is not None and t < since) or (until is not None and t > until):
            continue
        stage.start = t if stage.start is None or t < stage.start else stage.start
        stage.end = t if stage.end is None or t > stage.end else stage.end
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") if isinstance(e.get("message"), dict) else {}
        mid = str(msg.get("id") or e.get("requestId") or e.get("uuid") or "")
        u = msg.get("usage")
        if not isinstance(u, dict) or mid in seen:
            continue
        seen.add(mid)
        stage.calls += 1
        tok = _usage_tokens(u)
        model = str(msg.get("model") or "unknown")
        per = stage.by_model.setdefault(model, dict.fromkeys(TOKEN_KEYS, 0))
        for k, n in tok.items():
            stage.tokens[k] += n
            per[k] += n


def load_prices(path: str | None) -> dict | None:
    path = path or os.environ.get("PIPELINE_PRICES")
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cost_of(by_model: dict, prices: dict | None) -> float | None:
    """USD at the given per-million-token prices; None when any model used
    has no price (a partial sum would understate the cost)."""
    if prices is None:
        return None
    total = 0.0
    for model, tok in by_model.items():
        p = prices.get(model)
        if not isinstance(p, dict):
            return None
        for k in TOKEN_KEYS:
            rate = p.get(k, p.get("cache_write") if k == "cache_write_1h" else None)
            if tok.get(k) and rate is None:
                return None
            total += tok.get(k, 0) * float(rate or 0) / 1_000_000
    return round(total, 4)


def _overlap_s(intervals: list[tuple[datetime, datetime]]) -> float:
    """Seconds during which two or more intervals ran at once."""
    events = sorted([(a, 1) for a, _ in intervals] + [(b, -1) for _, b in intervals], key=lambda x: (x[0], x[1]))
    depth, out, prev = 0, 0.0, None
    for t, step in events:
        if depth >= 2 and prev is not None:
            out += (t - prev).total_seconds()
        depth += step
        prev = t
    return out


def executions(transcripts, belongs=None) -> tuple[list[dict], list[dict]]:
    """Every recorded run_check execution that produced evidence, oldest
    first, and every run_check call that produced none (with its error).
    *belongs* filters commands to this run (one session can hold several)."""
    runs, errors = [], []
    for t in transcripts:
        who = t.agent_id if t.is_subagent else "root"
        for c in t.calls_named("Bash", "PowerShell"):
            # ``authorize`` only issues a decision; ``execute`` (and the older
            # one-step form) is what runs the check.
            if not is_run_check_call(c.command) or " authorize " in f" {c.command} ":
                continue
            if belongs is not None and not belongs(c.command):
                continue
            m = None
            for m in _EVIDENCE_LINE_RE.finditer(c.output):
                pass
            if m is None:
                err = next((ln.strip() for ln in c.output.splitlines() if ln.strip().startswith("run_check:")),
                           (c.output.strip().splitlines() or ["no output"])[-1])
                errors.append({"agent": who, "at": c.timestamp, "command": c.command[:200], "error": err[:300]})
                continue
            runs.append({"id": m.group(1), "sha": m.group(2), "passed": m.group(3) == "true",
                         "fingerprint": m.group(4), "agent": who, "at": c.timestamp})
    runs.sort(key=lambda r: r["at"])
    return runs, errors


def classify_repeats(runs: list[dict]) -> list[dict]:
    """Each execution after a criterion's first, with its cause."""
    prev: dict[str, dict] = {}
    out = []
    for r in runs:
        p = prev.get(r["id"])
        if p is not None:
            if r["id"].endswith("@baseline"):
                # A baseline always runs on the same starting snapshot; whether
                # its copied tests changed is not in the recorded line.
                cause = "baseline again: needed only if the copied tests or the plan changed"
            elif p["fingerprint"] != r["fingerprint"]:
                cause = "revalidation: the tree changed since the previous run"
            elif p["passed"]:
                cause = "avoidable: same tree, the previous run had passed"
            else:
                cause = "retry: same tree, the previous run had failed"
            out.append({"id": r["id"], "at": r["at"], "agent": r["agent"], "cause": cause,
                        "previous_passed": p["passed"]})
        prev[r["id"]] = r
    return out


def collect(root, subs, *, start_at: str = "", end_at: str = "", register: list | None = None,
            prices: dict | None = None, writers=None, belongs=None) -> dict:
    """The metrics for one run.  *root* and *subs* are parsed transcripts
    (``pipeline.transcripts``); *start_at* is the timestamp of the recorded
    start line and *end_at* the last recorded step of the run (entries
    outside them belong to other work in the session); *register* is the
    harness's register outcome list; *writers* maps a subagent id to the
    stage files it wrote."""
    since, until = _ts(start_at), _ts(end_at)
    stages: list[StageRun] = []
    root_stage = StageRun("orchestration", "root")
    read_usage(root.path, root_stage, since, until)
    stages.append(root_stage)
    count: dict[str, int] = {}
    for t in subs:
        files = sorted((writers or {}).get(t.agent_id, []))
        base = files[-1] if files else "subagent:" + str(t.meta.get("agentType") or t.meta.get("description") or "agent")
        st = StageRun(base, t.agent_id)
        read_usage(t.path, st, since, until)
        if st.start is None:
            continue  # a subagent of other work in the same session
        count[base] = count.get(base, 0) + 1
        st.name = f"{base}#{count[base]}"
        stages.append(st)
    runs, errors = executions([root, *subs], belongs)
    for st in stages:
        st.checks = sum(1 for r in runs if r["agent"] == st.agent)
    for o in register or []:
        for st in stages:
            if st.name.split("#")[0] == o.get("stage"):
                st.raised.append(o["id"])
    starts = [s.start for s in stages if s.start]
    ends = [s.end for s in stages if s.end]
    sub_iv = [(s.start, s.end) for s in stages[1:] if s.start and s.end]
    total_tokens = {k: sum(s.tokens[k] for s in stages) for k in TOKEN_KEYS}
    stage_dicts = []
    for s in stages:
        stage_dicts.append({"stage": s.name, "agent": s.agent,
                            "start": s.start.isoformat() if s.start else None,
                            "end": s.end.isoformat() if s.end else None,
                            "elapsed_s": round(s.elapsed_s, 1), "model_calls": s.calls, "tokens": s.tokens,
                            "cost_usd": cost_of(s.by_model, prices), "checks_run": s.checks,
                            "register_raised": s.raised})
    by_model: dict = {}
    for s in stages:
        for m, tok in s.by_model.items():
            agg = by_model.setdefault(m, dict.fromkeys(TOKEN_KEYS, 0))
            for k in TOKEN_KEYS:
                agg[k] += tok[k]
    repeats = classify_repeats(runs)
    return {
        "wall_clock_s": round((max(ends) - min(starts)).total_seconds(), 1) if starts and ends else 0.0,
        "stage_time_s": round(sum(s.elapsed_s for s in stages), 1),
        "subagent_overlap_s": round(_overlap_s(sub_iv), 1),
        "model_calls": sum(s.calls for s in stages),
        "subagents": len(stages) - 1,
        "tokens": total_tokens,
        "tokens_by_model": by_model,
        "cost_usd": cost_of(by_model, prices),
        "cost_measured": prices is not None and cost_of(by_model, prices) is not None,
        "stages": stage_dicts,
        "executions": len(runs),
        "repeats": repeats,
        "orchestration_errors": errors,
        "register": {k: sum(1 for o in register or [] if o.get("state") == k)
                     for k in ("verified", "fixed", "not-a-defect", "unresolved", "open", "invalid")},
    }


def _dur(s: float) -> str:
    s = int(round(s))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def _tok(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def format_metrics(m: dict) -> str:
    """The compact summary: one line for the run, one per stage execution,
    then the repeats and errors with their causes."""
    tot = sum(m["tokens"].values())
    cost = f"${m['cost_usd']:.2f}" if m.get("cost_usd") is not None else "not measured"
    lines = [f"PIPELINE-METRICS wall={_dur(m['wall_clock_s'])} stage-time={_dur(m['stage_time_s'])} "
             f"overlap={_dur(m['subagent_overlap_s'])} calls={m['model_calls']} tokens={_tok(tot)} cost={cost} "
             f"subagents={m['subagents']} checks={m['executions']} repeats={len(m['repeats'])} "
             f"errors={len(m['orchestration_errors'])}"]
    for s in m["stages"]:
        c = f" ${s['cost_usd']:.2f}" if s.get("cost_usd") is not None else ""
        raised = f" raised={','.join(s['register_raised'])}" if s["register_raised"] else ""
        lines.append(f"  {s['stage']:<24} {_dur(s['elapsed_s']):>8} calls={s['model_calls']:<4} "
                     f"tokens={_tok(sum(s['tokens'].values())):<7}{c} checks={s['checks_run']}{raised}")
    per: dict[str, dict[str, int]] = {}
    for r in m["repeats"]:
        kind = r["cause"].split(":")[0]
        per.setdefault(r["id"], {}).setdefault(kind, 0)
        per[r["id"]][kind] += 1
    for cid, kinds in per.items():
        lines.append(f"  repeated {cid} x{sum(kinds.values())}: "
                     + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())))
    for e in m["orchestration_errors"]:
        lines.append(f"  error ({e['agent']}): {e['error']}")
    reg = {k: n for k, n in m["register"].items() if n}
    if reg:
        lines.append("  register: " + ", ".join(f"{n} {k}" for k, n in reg.items()))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from pipeline import harness  # the harness computes metrics while grading
    ap = argparse.ArgumentParser(prog="python -m pipeline.metrics")
    ap.add_argument("run_dir")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--prices", default=None, help="JSON price table (per million tokens, per model)")
    a = ap.parse_args(argv)
    prices = load_prices(a.prices)
    if a.prices and prices is None:
        print(f"metrics: cannot read the price table {a.prices}", file=sys.stderr)
        return 2
    v = harness.grade_run(Path(a.run_dir), prices=prices)
    m = v.metrics
    if not m:
        print(f"metrics: none for this run ({v.facts.get('metrics_error') or 'no session record'})", file=sys.stderr)
        return 2
    print(json.dumps(m, indent=2) if a.json else format_metrics(m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
