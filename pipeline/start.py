"""Create a Code Pipeline run: the run directory, ``run.json`` and ``00-request.md``.

    python -m pipeline.start --repo . --slug fix-thing --route a --request-file req.txt

Prints ``PIPELINE-RUN nonce=<nonce> dir=<run dir>``.  The nonce is also in
``run.json``; the Session Engine records this line as the command's output,
and that is how the harness finds the run's root session among a project's
transcripts.  The request file holds the user's request verbatim (``-`` reads
stdin); it is copied into ``00-request.md`` untouched.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.evidence import RUNS_SUBDIR, head_rev, tree_fingerprint

PIPELINE_VERSION = "1.0.0"
ROUTES = {"a": "new feature or major change", "b": "bug with unknown cause",
          "c": "production issue where speed matters", "d": "internal cleanup, no behavior change"}


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "run"


def create_run(repo: Path, slug: str, route: str, request_text: str, *,
               project_cwd: Path | None = None, now: datetime | None = None) -> tuple[Path, dict]:
    """Create the run directory under ``docs/plans/runs`` with ``run.json`` and
    ``00-request.md``.  Returns (run_dir, run record).  The directory name uses
    local time; ``created_at`` is UTC."""
    repo = repo.resolve()
    if route not in ROUTES:
        raise ValueError(f"route must be one of {', '.join(ROUTES)}")
    now = now or datetime.now(timezone.utc)
    run_id = f"{now.astimezone().strftime('%Y-%m-%d-%H%M')}-{_slug(slug)}"
    run_dir = repo / RUNS_SUBDIR / run_id
    if run_dir.exists():
        raise FileExistsError(f"{run_dir} already exists")
    run_dir.mkdir(parents=True)
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
        "nonce": nonce,
    }
    (run_dir / "run.json").write_text(json.dumps(run, indent=2, sort_keys=True), encoding="utf-8")
    header = (
        f"# 00 Request\n\n"
        f"Run: `{run_id}`  \nRoute: ({route}) {ROUTES[route]}  \n"
        f"Repository: `{repo}`  \nBase revision: `{run['base_rev']}`  \n"
        f"Created: {run['created_at']}\n\n"
        f"## Original request (verbatim)\n\n"
    )
    (run_dir / "00-request.md").write_text(header + request_text.rstrip("\n") + "\n", encoding="utf-8")
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
    print(f"PIPELINE-RUN nonce={run['nonce']} dir={run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
