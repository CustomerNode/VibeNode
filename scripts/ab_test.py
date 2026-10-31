"""A/B test: start several VibeNode sessions, wait for each answer, compare.

Each variant is a ``name=prompt`` pair. Every variant becomes a real session in
the active VibeNode project (it shows in the sidebar under its name), started
through ``POST /api/new-session``. The script then waits for each session's
final reply and prints them side by side.

Why it follows remaps: a new session starts under a temporary id, and the CLI
announces its real id a second later and writes the transcript under THAT id.
VibeNode records the switch in the project's ``_remapped_sessions.json`` (and
the engine's alias file). Watching the launch id's file finds nothing, so every
poll re-resolves the id before reading.

A session is finished when its last main-thread record is an assistant message
with ``stop_reason: end_turn`` and the transcript has been quiet for
``--settle`` seconds (so a Smart termination send-back, which lands within a
second or two and starts another turn, is not mistaken for the end).

Usage (any number of variants from two up, same prompt or different):
    python scripts/ab_test.py --prompt "who is the president" --count 3      # A/B test A, B, C
    python scripts/ab_test.py --each "say it in one line" "say it in a paragraph"
    python scripts/ab_test.py --prompt "who is the president" --names "test a" "test b"
    python scripts/ab_test.py "short=answer in one line" "long=explain in detail"
    python scripts/ab_test.py ... --json results.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.paths import claude_projects_dir  # noqa: E402
from common.session_names import load_engine_aliases, load_remaps_at, remaps_path  # noqa: E402

BASE = "http://localhost:5050"


def _http(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def active_project() -> str:
    for p in _http("GET", "/api/projects"):
        if p.get("active"):
            return p.get("encoded") or p.get("name")
    raise SystemExit("No active project in VibeNode.")


def resolve_id(project_dir: Path, sid: str) -> str:
    """Follow launch-id -> real-id switches (chained, cycle-safe)."""
    seen = set()
    while sid not in seen:
        seen.add(sid)
        nxt = (load_remaps_at(remaps_path(project_dir)).get(sid) or {}).get("new_id") \
            or (load_engine_aliases().get(sid) or {}).get("new_id")
        if not nxt or nxt == sid:
            break
        sid = nxt
    return sid


def read_result(path: Path) -> dict | None:
    """The final reply, or None while the session is still going."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    last, tools, first_ts, last_ts = None, [], None, None
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("isSidechain") or e.get("type") not in ("user", "assistant"):
            continue
        first_ts = first_ts or e.get("timestamp")
        last_ts = e.get("timestamp") or last_ts
        last = e
        msg = e.get("message") or {}
        for c in msg.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                tools.append(c.get("name"))
    if not last or last.get("type") != "assistant":
        return None
    msg = last.get("message") or {}
    if msg.get("stop_reason") != "end_turn":
        return None
    text = "".join(c.get("text", "") for c in msg.get("content") or []
                   if isinstance(c, dict) and c.get("type") == "text")
    return {"text": text, "tools": tools, "model": msg.get("model", ""),
            "started": first_ts, "ended": last_ts}


def short_answer(text: str) -> str:
    """The card's answer (or headline) when the reply ends on a handoff card."""
    if "```handoff" in text:
        body = text.split("```handoff", 1)[1].split("```", 1)[0]
        try:
            card = json.loads(body)
            return card.get("answer") or card.get("headline") or text
        except ValueError:
            pass
    return text.strip()


def variant_name(i: int) -> str:
    """A/B test A … Z, then AA, AB … (spreadsheet columns)."""
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return f"A/B test {s}"


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("variants", nargs="*", help='"name=prompt" pairs')
    ap.add_argument("--prompt", help="one prompt for every variant (--names or --count)")
    ap.add_argument("--names", nargs="*", default=[])
    ap.add_argument("--count", type=int, default=0,
                    help="with --prompt: run it this many times, named A/B test A, B, C…")
    ap.add_argument("--each", nargs="*", default=[],
                    help="a different prompt per variant, named A/B test A, B, C…")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--settle", type=float, default=8)
    ap.add_argument("--json", help="also write the results here")
    a = ap.parse_args()
    try:
        variants = build_variants(a.variants, a.prompt, a.names, a.count, a.each)
    except ValueError as e:
        ap.error(str(e))

    project = active_project()
    project_dir = claude_projects_dir() / project
    runs = []
    for name, prompt in variants:
        r = _http("POST", "/api/new-session", {"name": name, "prompt": prompt})
        if not r.get("ok"):
            raise SystemExit(f"{name}: {r.get('error')}")
        runs.append({"name": name, "prompt": prompt, "launch_id": r["new_id"],
                     "sent_at": time.time()})
        print(f"started {name!r} ({r['new_id']})", flush=True)

    deadline = time.time() + a.timeout
    pending = list(runs)
    while pending and time.time() < deadline:
        for run in list(pending):
            run["id"] = resolve_id(project_dir, run["launch_id"])
            path = project_dir / f"{run['id']}.jsonl"
            res = read_result(path)
            if res is None:
                continue
            if time.time() - path.stat().st_mtime < a.settle:
                continue  # just ended a turn; a send-back may still follow
            run.update(res)
            run["seconds"] = round(path.stat().st_mtime - run["sent_at"], 1)
            pending.remove(run)
            print(f"finished {run['name']!r} in {run['seconds']}s", flush=True)
        time.sleep(1)

    print()
    for run in runs:
        print(f"===== {run['name']}  ({run.get('id', run['launch_id'])})")
        if run in pending:
            print("  no final reply before the timeout\n")
            continue
        print(f"  {run['seconds']}s | model {run['model']} | tools: "
              f"{', '.join(run['tools']) or 'none'}")
        print("  " + short_answer(run["text"]).replace("\n", "\n  ") + "\n")
    if a.json:
        Path(a.json).write_text(json.dumps(runs, indent=2), encoding="utf-8")
    return 1 if pending else 0


if __name__ == "__main__":
    raise SystemExit(main())
