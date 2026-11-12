"""AI Compare: run two or more variants in fresh VibeNode sessions, side by side.

The successor of A/B Test (``scripts/ab_test.py``). What changed, and why:

- **The prompt is never touched.** A/B Test appended a Downloads date rule to
  every prompt and renamed file names inside it. The executed prompt is now the
  user's prompt, byte for byte; both are recorded and compared, and the run
  refuses to start if they ever differ. The interface has no separate
  instruction channel for a new session, so nothing operational rides along.
- **Read-only, enforced and checked.** Every variant runs in the CLI's plan
  mode (``permission_mode: "plan"``): it can read and answer, not edit files or
  run commands. The run refuses to start on a server that does not confirm it
  applied the mode, and afterwards flags any variant whose transcript shows a
  change that went through.
- **Nothing collides.** Every run has its own id and folder under
  ``data/ai_compare/<run id>/``; every variant has its own numbered subfolder
  with its reply, transcript copy and record. A folder is created once and never
  reused.
- **Honest endings.** Completed, failed, timeout and cancelled are reported as
  such, with partial text kept for the unfinished; only completed counts.
- **Measured, not judged.** Time, model calls, tokens and the CLI's reported
  cost are measurements; the comparison of quality is the AI's judgment and is
  labelled so. The runner computes no quality score.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from . import sessions as S

ROOT = S.REPO / "data" / "ai_compare"


def variant_name(i: int) -> str:
    """Variant A … Z, then AA, AB … (spreadsheet columns)."""
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return f"Compare {s}"


def build_variants(pairs, prompt, names, count, each):
    """``[(name, prompt), …]`` from the command line. Same prompt or not, two or more."""
    variants = [tuple(v.split("=", 1)) for v in pairs if "=" in v]
    if prompt:
        if names:
            variants += [(n, prompt) for n in names]
        elif count:
            variants += [(variant_name(i), prompt) for i in range(count)]
    variants += [(variant_name(len(variants) + i), p) for i, p in enumerate(each)]
    if len(variants) < 2:
        raise ValueError("give at least two variants")
    if len({n for n, _ in variants}) != len(variants):
        raise ValueError("every variant needs its own name")
    return variants


def assign_models(variants, models, catalog):
    """Pair each variant with a model entry (or None): one model for all, or one each."""
    if not models:
        return [(n, p, None) for n, p in variants]
    picked = [S.resolve_model(m, catalog) for m in models]
    if len(picked) == 1:
        picked = picked * len(variants)
    if len(picked) != len(variants):
        raise ValueError(f"{len(models)} models for {len(variants)} variants: give one model, or one per variant")
    return [(n, p, m) for (n, p), m in zip(variants, picked)]


def what_differs(variants) -> list:
    """Which of prompt and model differ between the variants. Nothing differing
    means a repeat run of one setup, which is said too."""
    out = []
    if len({p for _, p, _ in variants}) > 1:
        out.append("prompt")
    if len({(m or {}).get("id", "") for _, _, m in variants}) > 1:
        out.append("model")
    return out


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def run(variants, client=None, timeout: float = 600, settle: float = 8, root: Path = ROOT,
        out=print, run_id: str | None = None, poll: float = 1.0, cwd: str = "",
        stop_quiet: float = 3.0) -> dict:
    """Launch, follow, record and report. Returns the run record (also written
    to ``<run dir>/run.json``); its ``report`` path is the readable comparison."""
    client = client or S.VibeNodeClient()
    run_id = run_id or S.new_run_id("cmp")
    run_dir = S.make_run_dir(root, run_id)
    names = [n for n, _, _ in variants]
    vdirs = S.variant_dirs(run_dir, names)
    runs = []
    for i, (name, prompt, model) in enumerate(variants):
        r = S.Run(key=vdirs[name].name, name=name, prompt=prompt,
                  model=(model or {}).get("id", ""), permission_mode="plan", cwd=cwd)
        if r.prompt != prompt:  # the executed prompt IS the user's prompt
            raise RuntimeError(f"{name}: executed prompt differs from the original")
        runs.append(r)
        (vdirs[name] / "prompt.txt").write_bytes(prompt.encode("utf-8"))

    record = {"run_id": run_id, "kind": "ai-compare", "started_at": time.time(),
              "access": "read-only (plan mode)", "differs": what_differs(variants),
              "timeout_s": timeout, "variants": []}
    S.write_json(run_dir / "run.json", record)

    S.launch_all(client, runs)
    for r in runs:
        if r.status == "running" and (r.applied or {}).get("permission_mode") != "plan":
            # An older web server ignores the field: the session runs with full
            # access. Say so loudly and stop following it; never call it read-only.
            r.status, r.ended_at = S.FAILED, time.time()
            r.error = ("the web server did not confirm read-only mode (restart the web server to load "
                       "the AI Compare route); this variant was not compared")
            # It is running with full access: stop it now and confirm, do not just mark it.
            r.termination = S.terminate(client, r, quiet=stop_quiet)
        out(f"started {r.name!r} ({r.launch_id or r.error})")

    S.wait_all(runs, timeout=timeout, settle=settle, poll=poll, cancel_file=run_dir / "CANCEL",
               on_done=lambda r: out(f"{r.status} {r.name!r} in {round(r.ended_at - r.sent_at, 1)}s"),
               client=client, stop_quiet=stop_quiet)

    for r in runs:
        d = vdirs[r.name]
        m = S.measure(r.project_dir, r.session_id) if r.project_dir and r.session_id else {}
        cost = client.cost(r.session_id) if r.session_id else None
        kept = S.keep_transcripts(r, d)
        received = S.received_prompt(r.project_dir / f"{r.session_id}.jsonl") if r.project_dir and r.session_id else None
        if received is not None:
            (d / "received_prompt.txt").write_bytes(received.encode("utf-8"))
        (d / "reply.md").write_bytes((r.text or "").encode("utf-8"))
        v = r.as_dict()
        v.update({
            "dir": str(d), "reply_file": str(d / "reply.md"), "transcripts": kept,
            "original_prompt_sha256": _sha(r.prompt), "submitted_prompt_sha256": _sha(r.prompt),
            "received_prompt_sha256": _sha(received) if received is not None else None,
            # True / False once the session's transcript shows what it received;
            # None when it never recorded a first message.
            "prompt_received_exactly": (received == r.prompt) if received is not None else None,
            "partial": r.status != S.COMPLETED and bool(r.text),
            "metrics": m, "cost_usd": cost,
            "cost_basis": "CLI total_cost_usd (API-equivalent, measured)" if cost is not None else "not reported",
            "model_mismatch": bool(r.model and r.ran_model and not S.same_model(r.model, r.ran_model)),
            "read_only_breached": bool(m.get("mutating_tool_uses")),
        })
        S.write_json(d / "variant.json", v)
        record["variants"].append(v)
    record["ended_at"] = time.time()
    record["all_completed"] = all(v["status"] == S.COMPLETED for v in record["variants"])
    report = run_dir / "report.md"
    report.write_text(render_report(record), encoding="utf-8")
    record["report"] = str(report)
    S.write_json(run_dir / "run.json", record)
    return record


def _prompt_line(vs) -> str:
    seen = [v.get("prompt_received_exactly") for v in vs]
    if seen and all(x is True for x in seen):
        return "Every session received its prompt exactly as written (checked against its transcript)."
    if any(x is False for x in seen):
        return "**At least one session received a different prompt than was sent** (see Warnings)."
    return "Prompts were sent as written; not every session recorded what it received."


def render_report(rec: dict) -> str:
    vs = rec["variants"]
    differs = ", ".join(rec["differs"]) or "nothing (the same setup run more than once)"
    lines = [f"# AI Compare {rec['run_id']}", "",
             f"- What differs between variants: **{differs}**",
             f"- Access: {rec['access']}",
             "- " + _prompt_line(vs), "",
             "## Measured", "",
             "| Variant | Status | Model asked → ran | Time | Calls | Tokens | Cost | Tools |",
             "|---|---|---|---|---|---|---|---|"]
    for v in vs:
        m = v.get("metrics") or {}
        tools = ", ".join(f"{k}×{n}" for k, n in (m.get("tools") or {}).items()) or "none"
        model = f"{v.get('model') or 'default'} → {v.get('ran_model') or '?'}"
        secs = f"{v['seconds']}s" if v.get("seconds") is not None else "-"
        lines.append(f"| {S.md_cell(v['name'], 60)} | {v['status']} | {S.md_cell(model, 80)} | {secs} | "
                     f"{m.get('model_calls', '-')} | {S.fmt_tokens(m.get('tokens') or {})} | "
                     f"{S.fmt_cost(v.get('cost_usd'))} | {S.md_cell(tools, 120)} |")
    warns = []
    for v in vs:
        if v["status"] != S.COMPLETED:
            warns.append(f"- {v['name']}: **{v['status']}**: {v.get('error') or ''}"
                         + (" (partial text kept, not a finished answer)" if v.get("partial") else ""))
        if v.get("model_mismatch"):
            warns.append(f"- {v['name']}: asked for {v['model']} but it ran on {v['ran_model']}; "
                         "not a result for the model asked")
        if v.get("termination", "").startswith("unconfirmed"):
            warns.append(f"- {v['name']}: **could not confirm the session stopped** ({v['termination']}); "
                         "it may still be running")
        if v.get("prompt_received_exactly") is False:
            warns.append(f"- {v['name']}: **the session received a different prompt** than was sent; "
                         f"see `{Path(v['dir']) / 'received_prompt.txt'}`")
        if v.get("read_only_breached"):
            warns.append(f"- {v['name']}: **read-only was not enforced**: "
                         f"{', '.join(v['metrics']['mutating_tool_uses'])} went through")
    if warns:
        lines += ["", "## Warnings", ""] + warns
    lines += ["", "## Answers side by side", "", "| Variant | Short answer |", "|---|---|"]
    for v in vs:
        ans = S.short_answer(v.get("text") or "") if v["status"] == S.COMPLETED else f"({v['status']})"
        lines.append(f"| {S.md_cell(v['name'], 60)} | {S.md_cell(ans, 400)} |")
    lines += ["", "## Full replies and records", ""]
    for v in vs:
        lines.append(f"- {v['name']}: `{v['reply_file']}` (session {v.get('session_id') or '-'})")
    lines += ["", "## Judgment", "",
              "Written by the AI that ran this comparison, from the replies above: conclusions, "
              "reasoning, completeness, omissions, contradictions, clarity. It is judgment, not "
              "measurement, and proves nothing beyond these replies.", ""]
    return "\n".join(lines)
