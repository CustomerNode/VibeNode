#!/usr/bin/env python3
"""Prove which Code Pipeline ran a run, from OUTSIDE the pipeline package.

    python scripts/pipeline_provenance.py <run-dir>
    python scripts/pipeline_provenance.py --repo <worktree>              # newest run already there
    python scripts/pipeline_provenance.py --repo <worktree> --wait 600   # a NEW run only
    python scripts/pipeline_provenance.py --repo <worktree> --watch      # each new run, at start and delivery

A comparison run arms the watch with ``scripts/pipeline_experiment.py``.

Why this lives outside ``pipeline/``: a contestant worktree started from an
older commit carries an older copy of the pipeline, and ``python -m pipeline``
run from inside that worktree imports it.  Nothing added to the pipeline can
stop an older copy of itself from running, because the older copy does not
have the addition.  So the check is made here, by a program the contestant's
code never runs, against the pipeline this script's own repository holds
(the approved copy; ``--approved`` names another).

What it checks, and why each one fails closed:

- ``run.json`` must carry ``impl`` / ``impl_path`` (pipeline 1.3.0 and later
  write them at the start).  A run without them was started by an older
  launcher.  This is known seconds after the start, not at the end of the run.
- ``impl_path`` must be the approved pipeline directory, and ``impl`` must equal
  the approved directory's identity computed now.  A different location, or
  the approved code changed since, both fail.
- ``run.json`` must carry ``workflow`` (pipeline 1.4.0 and later), and it must
  equal the identity of the approved guides now: ``workforce/code-pipeline.md``
  and every team guide its ``depends_on`` names, beside the approved package.
  The guides steer the run as much as the code.  This proves which guides were
  on disk at the approved location when the run started; it cannot prove the
  agent read them rather than an old copy in its worktree.
- Every other record in the run that names an ``impl`` (evidence, check
  history) must name the same one, so a run started from the approved copy
  but checked from another is caught too.

The watch is bound to one run by exclusion, not by time: the run folders that
exist when it is armed are recorded and ignored, the first new one is THE run,
and a second new one fails the watch.  It ends only when the run's delivery
record says ``"delivered": true``; a record waiting for the person's
acceptance (``false``) keeps it watching, and every change to the record is
checked again.

Exit status: 0 the run is provably from the approved pipeline, 1 it is not,
2 the run folder could not be found or read.  This guards against running the
wrong program by accident, not against someone forging the record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

RUNS_SUBDIR = Path("docs") / "plans" / "runs"
DEFAULT_APPROVED = Path(__file__).resolve().parents[1] / "pipeline"


def pipeline_identity(pkg_dir: Path) -> str:
    """The identity ``pipeline.evidence.impl_identity`` computes for *pkg_dir*:
    a hash over every module's name and the sha256 of its bytes.  Reimplemented
    here so this script never imports a ``pipeline`` package (that import is
    exactly what can pick the wrong copy); a test keeps the two equal."""
    h = hashlib.sha256()
    for p in sorted(Path(pkg_dir).glob("*.py")):
        h.update(p.name.encode())
        try:
            h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode())
        except OSError:
            h.update(b"unreadable")
    return h.hexdigest()


WORKFLOW_GUIDE = "code-pipeline.md"


def workflow_identity(workforce: Path) -> str:
    """The identity ``pipeline.evidence.workflow_identity`` computes for the
    guides in *workforce* (reimplemented, never imported; a test holds the two
    equal)."""
    import re
    wf = Path(workforce)
    names = [WORKFLOW_GUIDE]
    try:
        text = (wf / WORKFLOW_GUIDE).read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^depends_on:\s*\[([^\]]*)\]", text, re.MULTILINE)
        if m:
            names += [f"{n.strip()}.md" for n in m.group(1).split(",") if n.strip()]
    except OSError:
        pass
    h = hashlib.sha256()
    for name in sorted(set(names)):
        h.update(name.encode())
        try:
            h.update(hashlib.sha256((wf / name).read_bytes()).hexdigest().encode())
        except OSError:
            h.update(b"unreadable")
    return h.hexdigest()


def _same_dir(a: str, b: Path) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except (OSError, ValueError):
        return False


def _impl_records(obj, where: str):
    """Every (where, impl, impl_path) in a JSON value, at any depth."""
    if isinstance(obj, dict):
        if "impl" in obj:
            yield where, obj.get("impl"), obj.get("impl_path")
        for k, v in obj.items():
            yield from _impl_records(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _impl_records(v, f"{where}[{i}]")


def check(run_dir: Path, approved: Path = DEFAULT_APPROVED) -> tuple[bool, list[str]]:
    """(ok, lines).  ``ok`` only when every rule above holds."""
    run_dir = Path(run_dir)
    approved = Path(approved).resolve()
    want = pipeline_identity(approved)
    guides = approved.parent / "workforce"
    want_wf = workflow_identity(guides)
    lines = [f"approved pipeline: {approved}", f"approved identity: {want[:12]}",
             f"approved guides: {want_wf[:12]}"]
    try:
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise FileNotFoundError(f"{run_dir} has no readable run.json ({e})") from None
    problems = []
    impl, impl_path = run.get("impl"), run.get("impl_path")
    lines.append(f"run started by: {impl_path or '(not recorded)'} "
                 f"identity {str(impl)[:12] if impl else '(not recorded)'} "
                 f"version {run.get('pipeline_version', '?')}")
    if not impl or not impl_path:
        problems.append("run.json does not record which pipeline started it: an outdated launcher "
                        "(before 1.3.0) created this run, most likely a copy inside the worktree")
    else:
        if not _same_dir(impl_path, approved):
            problems.append(f"the run was started by the pipeline in {impl_path}, not the approved one")
        if impl != want:
            problems.append(f"the starting pipeline's identity {impl[:12]} is not the approved "
                            f"identity {want[:12]} (different or changed code)")
        wf = run.get("workflow")
        if not wf:
            problems.append("run.json does not record which guides the run started on: a launcher before 1.4.0 "
                            "created this run")
        elif wf != want_wf:
            problems.append(f"the run started on guides {str(wf)[:12]}, not the approved guides {want_wf[:12]} "
                            f"(a guide in {guides} differs or changed since the start)")
    foreign: dict[str, list[str]] = {}   # "identity …, location …" -> records
    for f in sorted(run_dir.rglob("*.json")):
        if f.name == "run.json" and f.parent == run_dir:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rel = f.relative_to(run_dir).as_posix()
        for where, r_impl, r_path in _impl_records(data, rel):
            bad = []
            # Full identity, or the 12-character prefix an evidence line prints.
            if r_impl and (len(str(r_impl)) < 12 or not want.startswith(str(r_impl))):
                bad.append(f"identity {str(r_impl)[:12]}")
            if r_path and not _same_dir(r_path, approved):
                bad.append(f"location {r_path}")
            if bad:
                foreign.setdefault(", ".join(bad), []).append(where)
    for who, where in foreign.items():
        shown = ", ".join(where[:3]) + (f" and {len(where) - 3} more" if len(where) > 3 else "")
        problems.append(f"{len(where)} record(s) were produced by another pipeline ({who}): {shown}")
    if problems:
        lines.append("PROVENANCE FAIL")
        lines += [f"  - {p}" for p in problems]
        return False, lines
    lines.append("PROVENANCE OK: every record names the approved pipeline")
    return True, lines


def _runs(repo: Path) -> list[Path]:
    return [p for p in (Path(repo) / RUNS_SUBDIR).glob("*") if (p / "run.json").is_file()]


def _created(run_dir: Path) -> float | None:
    """The run's ``created_at`` (written by pipeline.start) as an epoch."""
    try:
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        return datetime.fromisoformat(str(run["created_at"])).timestamp()
    except (OSError, ValueError, KeyError, TypeError):
        return None


def existing_runs(repo: Path) -> set:
    """The names of every run folder under *repo* now (what a watch ignores)."""
    runs = Path(repo) / RUNS_SUBDIR
    return {p.name for p in runs.glob("*") if p.is_dir()} if runs.is_dir() else set()


def newest_run(repo: Path, since: float | None = None, exclude: set | None = None) -> Path | None:
    """The newest run under *repo*, ignoring the folders in *exclude*; with
    *since*, only a run created at or after that moment (no tolerance), so a
    run left from earlier can never stand in for the one being watched."""
    runs = [p for p in _runs(repo) if not exclude or p.name not in exclude]
    if since is not None:
        runs = [p for p in runs if (_created(p) or 0) >= since]
    return max(runs, key=lambda p: (p / "run.json").stat().st_mtime) if runs else None


def delivery_state(run_dir: Path) -> tuple:
    """('none' | 'pending' | 'delivered', the record's mtime).  Only
    ``"delivered": true`` counts; an unreadable record is pending."""
    p = Path(run_dir) / "delivered.json"
    try:
        mt = p.stat().st_mtime
    except OSError:
        return "none", None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "pending", mt
    return ("delivered" if isinstance(rec, dict) and rec.get("delivered") is True else "pending"), mt


def _notify(title: str, message: str) -> None:
    """Chime every open VibeNode tab (best effort; never fails the check)."""
    try:
        import urllib.request
        body = json.dumps({"title": title, "message": message, "source": "code-pipeline"}).encode()
        req = urllib.request.Request(os.environ.get("VIBENODE_NOTIFY_URL", "http://127.0.0.1:5050/api/notify/approval"),
                                     data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def watch(repo: Path, approved: Path = DEFAULT_APPROVED, *, exclude: set | None = None,
          since: float | None = None, timeout: float, poll: float = 5.0, out: Path | None = None,
          notify=_notify) -> int:
    """Watch the ONE run started under *repo* after arming.  The folders in
    *exclude* (by default every run there when the watch starts) are ignored;
    the first other run is the one, and a second fails the watch.  It is
    checked when its ``run.json`` appears, again on every change to its
    delivery record, and a last time when that record says delivered.  Exit 0
    when the run is delivered and every check passed, 1 on a failed check or a
    second run, 2 when no run appeared or it was not delivered in time.
    Results go to *out* (JSON) as they happen."""
    exclude = set(existing_runs(repo) if exclude is None else exclude)
    deadline = time.monotonic() + timeout
    state: dict = {"repo": str(repo), "excluded": sorted(exclude), "run": None, "checks": []}

    def record():
        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def fail(title: str, message: str) -> int:
        state["result"] = "fail"
        record()
        notify(title, message)
        print("WATCH DONE: FAIL", flush=True)
        return 1

    run_dir = None
    seen_mtime: object = "unset"
    while True:
        new = sorted(p for p in _runs(repo) if p.name not in exclude
                     and (since is None or (_created(p) or 0) >= since))
        if run_dir is None and new:
            run_dir = new[0]
            state["run"] = run_dir.name
        if run_dir is not None and len(new) > 1:
            others = ", ".join(p.name for p in new if p != run_dir)
            state["checks"].append({"stage": "extra-run", "ok": False, "lines": [f"another run started: {others}"]})
            return fail("Two Code Pipeline runs in one test",
                        f"{repo}: {others} started after {run_dir.name}; a comparison is one run per side.")
        if run_dir is not None:
            dstate, mt = delivery_state(run_dir)
            if mt != seen_mtime:
                seen_mtime = mt
                stage = {"none": "start", "pending": "pending", "delivered": "final"}[dstate]
                ok, lines = check(run_dir, approved)
                state["checks"].append({"stage": stage, "ok": ok, "lines": lines, "at": time.time()})
                record()
                print(f"[{stage}] run: {run_dir}\n" + "\n".join(lines), flush=True)
                if not ok:
                    return fail("Wrong Code Pipeline running",
                                f"{run_dir.name}: {lines[-1].strip(' -')} The run is not a valid test of the "
                                f"approved pipeline.")
                if dstate == "delivered":
                    state["result"] = "ok"
                    record()
                    print("WATCH DONE: OK", flush=True)
                    return 0
        if time.monotonic() >= deadline:
            what = "no run appeared" if run_dir is None else "the run was not delivered"
            state["result"] = "timeout"
            record()
            notify("Code Pipeline provenance not proven", f"{repo}: {what} before the watch ended.")
            print(f"WATCH DONE: {what.upper()}", flush=True)
            return 2
        time.sleep(poll)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python scripts/pipeline_provenance.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir", nargs="?", help="the run folder (docs/plans/runs/<id>)")
    ap.add_argument("--repo", help="check the newest run under this repository instead")
    ap.add_argument("--approved", default=str(DEFAULT_APPROVED), help="the approved pipeline package directory")
    ap.add_argument("--wait", type=float, default=0,
                    help="seconds to wait for a NEW run under --repo (one created after this command started)")
    ap.add_argument("--watch", action="store_true",
                    help="with --repo: check each new run at its start and again at delivery (see watch())")
    ap.add_argument("--since", type=float, help="epoch: runs created before it are not the ones being watched")
    ap.add_argument("--exclude-file", help="JSON list of run folder names that existed when the test was armed")
    ap.add_argument("--timeout", type=float, default=6 * 3600, help="--watch: seconds before giving up")
    ap.add_argument("--out", help="--watch: write the results here as JSON")
    a = ap.parse_args(argv)
    if not a.run_dir and not a.repo:
        ap.error("give a run folder or --repo")
    exclude = None
    if a.exclude_file:
        try:
            exclude = set(json.loads(Path(a.exclude_file).read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            print(f"cannot read {a.exclude_file}: {e}", file=sys.stderr)
            return 2
    if a.watch:
        if not a.repo:
            ap.error("--watch needs --repo")
        return watch(Path(a.repo), Path(a.approved), exclude=exclude, since=a.since,
                     timeout=a.timeout, out=Path(a.out) if a.out else None)
    run_dir = Path(a.run_dir) if a.run_dir else None
    if run_dir is None:
        if exclude is None and a.wait and a.since is None:
            exclude = existing_runs(Path(a.repo))   # waiting: only a run started from now on
        deadline = time.monotonic() + a.wait
        while True:
            run_dir = newest_run(Path(a.repo), a.since, exclude)
            if run_dir or time.monotonic() >= deadline:
                break
            time.sleep(min(5, max(0.2, a.wait / 10)))
        if run_dir is None:
            print(f"no {'new ' if (exclude or a.since) else ''}run under {Path(a.repo) / RUNS_SUBDIR}", file=sys.stderr)
            return 2
    try:
        ok, lines = check(run_dir, Path(a.approved))
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"run: {run_dir}")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
