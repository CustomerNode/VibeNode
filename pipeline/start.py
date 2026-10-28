"""Create a Code Pipeline run: the run directory, ``run.json`` and ``00-request.md``.

    python -m pipeline.start --repo . --slug fix-thing --route a --request-file req.txt

Prints one line the Session Engine records as the command's output::

    PIPELINE-RUN nonce=<nonce> route=<a|b|c|d> request=<sha> pinned=<sha|none> protected=<sha|none> base=<rev> snapshot=<rev> site=<sha> dir=<run dir>

The directory comes last so a path with spaces reads to the end of the line.
That line is the run's fixed point.  The harness finds the run's root session
by the nonce, and takes from the line, never from a file an agent can edit:
the route (which stages are required), the hash of the user's request as
written to ``00-request.md``, the hash of the repository's ``.pipeline-verify``
(the pinned checks) and of its ``.pipeline-protected`` (the pinned protection).
Both declarations are copied into the run (``pinned-verify.txt``,
``pinned-protected.txt``) and enforced from the copies; editing or deleting
the originals during the run changes nothing the harness relies on.

The request file holds the user's request verbatim (``-`` reads stdin); it is
copied into ``00-request.md`` untouched and nothing is ever appended to that
file.  A light run with no Plan Team puts its verification block in
``00-plan.md``.  The baseline snapshot the regression demonstration runs
against is also fixed here.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.evidence import RUNS_SUBDIR, baseline_snapshot, head_rev, site_fingerprint, tree_fingerprint
from pipeline.plan import (PINNED_FILE, PINNED_PROTECTED_FILE, PROTECTED_FILE, REQUEST_MARKER, VERIFY_FILE,
                           PlanError, block_sha, check_pinnable, parse_criteria, request_sha)

PIPELINE_VERSION = "1.2.0"
ROUTES = {"a": "new feature or major change", "b": "bug with unknown cause",
          "c": "production issue where speed matters", "d": "internal cleanup, no behavior change"}


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "run"


def start_line(run: dict, run_dir: Path) -> str:
    """The ``PIPELINE-RUN`` line for *run*; the harness parses exactly this.
    ``base`` and ``snapshot`` pin the revision the diff is measured against
    and the code the regression demonstration runs on."""
    return (f"PIPELINE-RUN nonce={run['nonce']} route={run['route']} request={run['request_sha']} "
            f"pinned={run.get('pinned_sha') or 'none'} protected={run.get('protected_sha') or 'none'} "
            f"base={run.get('base_rev') or 'none'} snapshot={run.get('base_snapshot') or 'none'} "
            f"site={run.get('site_sha') or 'none'} dir={run_dir}")


def create_run(repo: Path, slug: str, route: str, request_text: str, *,
               project_cwd: Path | None = None, now: datetime | None = None) -> tuple[Path, dict]:
    """Create the run directory under ``docs/plans/runs`` with ``run.json`` and
    ``00-request.md``.  Returns (run_dir, run record).  The directory name uses
    local time; ``created_at`` is UTC."""
    repo = repo.resolve()
    if route not in ROUTES:
        raise ValueError(f"route must be one of {', '.join(ROUTES)}")
    if not request_text.strip():
        raise ValueError("the request is empty")
    now = now or datetime.now(timezone.utc)
    # A short random suffix: two sessions starting the same slug in the same
    # minute get two runs, not one error (external review, finding 10).
    run_id = f"{now.astimezone().strftime('%Y-%m-%d-%H%M')}-{_slug(slug)}-{secrets.token_hex(2)}"
    run_dir = repo / RUNS_SUBDIR / run_id
    if run_dir.exists():
        raise FileExistsError(f"{run_dir} already exists")
    declared = repo / VERIFY_FILE
    pinned_text = declared.read_text(encoding="utf-8") if declared.is_file() else None
    if pinned_text is not None:
        try:
            check_pinnable(parse_criteria(pinned_text))
        except PlanError as e:
            raise ValueError(f"{VERIFY_FILE} is not a valid verification list: {e}") from None
    protected = repo / PROTECTED_FILE
    protected_text = protected.read_text(encoding="utf-8") if protected.is_file() else None
    run_dir.mkdir(parents=True)
    if pinned_text is not None:
        (run_dir / PINNED_FILE).write_text(pinned_text, encoding="utf-8")
    if protected_text is not None:
        (run_dir / PINNED_PROTECTED_FILE).write_text(protected_text, encoding="utf-8")
    body = request_text.rstrip("\n") + "\n"
    nonce = secrets.token_hex(12)
    run = {
        "run_id": run_id,
        "pipeline": "code-pipeline",
        "pipeline_version": PIPELINE_VERSION,
        "created_at": now.isoformat(timespec="seconds"),
        "repo": str(repo),
        "project_cwd": str((project_cwd or repo).resolve()),
        "route": route,
        "route_name": ROUTES[route],
        "base_rev": head_rev(repo),
        "base_fingerprint": tree_fingerprint(repo),
        "base_snapshot": baseline_snapshot(repo, run_id),
        "request_sha": request_sha(body),
        "pinned_sha": block_sha(pinned_text) if pinned_text is not None else None,
        "protected_sha": block_sha(protected_text) if protected_text is not None else None,
        "site_sha": site_fingerprint(),
        "nonce": nonce,
    }
    (run_dir / "run.json").write_text(json.dumps(run, indent=2, sort_keys=True), encoding="utf-8")
    header = (
        f"# 00 Request\n\n"
        f"Run: `{run_id}`  \nRoute: ({route}) {ROUTES[route]}  \n"
        f"Repository: `{repo}`  \nBase revision: `{run['base_rev']}`  \n"
        f"Created: {run['created_at']}\n\n"
        f"{REQUEST_MARKER}\n\n"
    )
    (run_dir / "00-request.md").write_text(header + body, encoding="utf-8")
    return run_dir, run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.start")
    ap.add_argument("--repo", default=".", help="repository the work happens in")
    ap.add_argument("--slug", required=True, help="short kebab-case name for the run")
    ap.add_argument("--route", required=True, choices=sorted(ROUTES))
    ap.add_argument("--request-file", required=True, help="file with the request verbatim, or - for stdin")
    ap.add_argument("--project-cwd", default=None,
                    help="the session's working directory if it is not the repo (where its transcript lives)")
    a = ap.parse_args(argv)
    if a.request_file == "-":
        request = sys.stdin.read()
    else:
        request = Path(a.request_file).read_text(encoding="utf-8")
    if not request.strip():
        print("start: the request is empty", file=sys.stderr)
        return 2
    try:
        run_dir, run = create_run(Path(a.repo), a.slug, a.route, request,
                                  project_cwd=Path(a.project_cwd) if a.project_cwd else None)
    except (ValueError, FileExistsError, RuntimeError, OSError) as e:
        print(f"start: {e}", file=sys.stderr)
        return 2
    print(start_line(run, run_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
