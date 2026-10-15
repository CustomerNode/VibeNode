#!/usr/bin/env python3
"""vibenode — command-line entry points for VibeNode.

    vibenode job run --name NAME --cwd DIR --done REGEX --command "..." [options]
    vibenode job list
    vibenode job status JOB_ID
    vibenode job relaunch JOB_ID
    vibenode autostart install | remove | status

``job run`` is how a long job is launched (Durable Jobs,
docs/plans/durable-jobs-spec.md): it starts the command detached on this OS
and registers it in ``data/jobs.json`` in the same step, so the supervisor in
the Session Engine can bring it back after a restart.  The run-detached skill
calls it; there is no other launch path for a long job.  It refuses only a
missing done test.

``autostart`` makes VibeNode start when you sign in, so the supervisor is
always there after a reboot (Don, 2026-10-15: VibeNode is part of boot-up).

Run it as ``python vibenode.py ...`` from the repo, or from anywhere with the
absolute path.  No server needs to be running.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from daemon import durable_jobs as dj  # noqa: E402


# ── job ──────────────────────────────────────────────────────────────────

def _job_run(a) -> int:
    try:
        rec = dj.run_job(
            name=a.name, command=a.command, cwd=os.path.abspath(a.cwd),
            done_test={"pattern": a.done, "file": a.done_file or "", "last_line": a.done_last_line},
            irreversible_after=({"pattern": a.irreversible, "file": a.irreversible_file or "",
                                 "last_line": a.irreversible_last_line} if a.irreversible else None),
            progress=({"pattern": a.progress, "file": a.progress_file or ""} if a.progress else None),
            running_match=a.running_match or "", session_id=a.session or "",
            project=a.project or "", env_file=a.env_file or "", log_path=a.log or "",
            resumable=not a.not_resumable, job_id=a.id or "",
        )
    except dj.JobError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except dj.RegistryError as exc:
        print(f"registry problem: {exc}", file=sys.stderr)
        return 3
    if a.json:
        print(json.dumps(rec, indent=1))
    else:
        print(f"{rec['id']}|{rec['last_log']}|{rec['state']}")
        if rec["state"] == "failed":
            print(rec["reason"], file=sys.stderr)
    return 0 if rec["state"] != "failed" else 1


def _job_list(a) -> int:
    try:
        jobs = dj.list_jobs()
    except dj.RegistryError as exc:
        print(f"registry problem: {exc}", file=sys.stderr)
        return 3
    if a.json:
        print(json.dumps(jobs, indent=1))
        return 0
    if not jobs:
        print("no jobs registered")
    for j in jobs:
        p = j.get("progress_value") or {}
        pct = f" {p['pct']}%" if p.get("pct") is not None else ""
        print(f"{j['state']:13} {j['id']:40} {j['name']}{pct}")
    return 0


def _job_status(a) -> int:
    rec = dj.get_job(a.job_id)
    if rec is None:
        print(f"no job {a.job_id}", file=sys.stderr)
        return 1
    print(json.dumps(rec, indent=1))
    log = dj.resolve_log(rec, rec.get("done_test"))
    if log:
        print(f"--- {log} (tail)")
        print(dj.read_tail(log, 4000)[-4000:])
    return 0


def _job_relaunch(a) -> int:
    try:
        rec, res = dj.relaunch_job(a.job_id, by="user")
    except dj.JobError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{rec['id']}|{rec['last_log']}|{rec['state']}")
    return 0 if res.get("ok") else 1


def _job_import(a) -> int:
    """Migrate the laptop patch's resume_jobs.json into the registry WITHOUT
    launching anything (the jobs are running).  --dry-run prints the records."""
    try:
        records = dj.migrate_resume_jobs(Path(a.path), Path(a.rd_dir), Path(a.collect_dir), a.waiter_command)
    except (dj.JobError, OSError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    out = []
    for fields in records:
        if a.dry_run:
            out.append(dj.new_record(**fields))
        else:
            out.append(dj.register_existing(**fields))
    print(json.dumps(out, indent=1) if a.json else "\n".join(f"{r['id']}|{r['state']}|{r['name']}" for r in out))
    return 0


# ── autostart ────────────────────────────────────────────────────────────

def _autostart_entry() -> Path:
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/VibeNode.vbs"
    if sys.platform == "darwin":
        return Path.home() / "Library/LaunchAgents/com.vibenode.start.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "autostart/vibenode.desktop"


def _autostart_body(entry: Path) -> str:
    launcher = ROOT / "session_manager.py"
    if sys.platform == "win32":
        py = Path(sys.executable)
        pyw = py.with_name("pythonw.exe")
        exe = pyw if pyw.exists() else py
        # Same hidden-launch shape as the reviver's Startup entry.
        return ('CreateObject("WScript.Shell").Run """%s"" ""%s""", 0, False\r\n'
                % (exe, launcher))
    if sys.platform == "darwin":
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.vibenode.start</string>
  <key>ProgramArguments</key><array><string>{sys.executable}</string><string>{launcher}</string></array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>RunAtLoad</key><true/>
</dict></plist>
"""
    return f"""[Desktop Entry]
Type=Application
Name=VibeNode
Exec={sys.executable} {launcher}
Path={ROOT}
X-GNOME-Autostart-enabled=true
"""


def _autostart(a) -> int:
    entry = _autostart_entry()
    if a.action == "status":
        print(f"{'installed' if entry.exists() else 'not installed'}: {entry}")
        return 0
    if a.action == "remove":
        if entry.exists():
            entry.unlink()
            print(f"removed {entry}")
        else:
            print("not installed")
        return 0
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(_autostart_body(entry), encoding="utf-8")
    print(f"installed {entry}")
    return 0


# ── main ─────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vibenode")
    sub = p.add_subparsers(dest="cmd", required=True)

    job = sub.add_parser("job", help="durable long-running jobs")
    jsub = job.add_subparsers(dest="jcmd", required=True)

    run = jsub.add_parser("run", help="launch a job detached and register it (one step)")
    run.add_argument("--name", required=True, help="human name for the job")
    run.add_argument("--command", required=True, help="the shell command to run")
    run.add_argument("--cwd", required=True, help="folder to run it in")
    run.add_argument("--done", required=True, metavar="REGEX",
                     help="done test: regex that appears in the log when the job has finished")
    run.add_argument("--done-file", metavar="GLOB",
                     help="file the done test reads (glob next to the log; default: the job's log)")
    run.add_argument("--done-last-line", action="store_true", help="match only the file's last line")
    run.add_argument("--irreversible", metavar="REGEX",
                     help="after this appears, never relaunch blind (a person decides)")
    run.add_argument("--irreversible-file", metavar="GLOB")
    run.add_argument("--irreversible-last-line", action="store_true")
    run.add_argument("--progress", metavar="REGEX", help=r"progress pattern with two groups, e.g. 'cells (\d+)/(\d+)'")
    run.add_argument("--progress-file", metavar="GLOB")
    run.add_argument("--running-match", metavar="PATTERN",
                     help="substring (with * wildcards) that identifies the live process's command line; default: the command")
    run.add_argument("--session", help="session id that launched it")
    run.add_argument("--project", help="project name")
    run.add_argument("--env-file", help="KEY=VALUE file loaded into the job's environment (never in the command line)")
    run.add_argument("--log", help="log path; {ts} is replaced per launch (default: data/jobs/<id>-{ts}.log)")
    run.add_argument("--id", help="job id (default: from the name)")
    run.add_argument("--not-resumable", action="store_true", help="never relaunch this job automatically")
    run.add_argument("--json", action="store_true")
    run.set_defaults(fn=_job_run)

    ls = jsub.add_parser("list")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=_job_list)
    st = jsub.add_parser("status")
    st.add_argument("job_id")
    st.set_defaults(fn=_job_status)
    rl = jsub.add_parser("relaunch")
    rl.add_argument("job_id")
    rl.set_defaults(fn=_job_relaunch)

    imp = jsub.add_parser("import-resume-jobs", help="register the run-detached laptop patch's jobs (no launch)")
    imp.add_argument("path", help="resume_jobs.json")
    imp.add_argument("--rd-dir", required=True, help="the run-detached folder (logs, state files)")
    imp.add_argument("--collect-dir", required=True, help="cwd for 'collect' launches")
    imp.add_argument("--waiter-command", required=True, help="command behind the 'task' launch")
    imp.add_argument("--dry-run", action="store_true")
    imp.add_argument("--json", action="store_true")
    imp.set_defaults(fn=_job_import)

    auto = sub.add_parser("autostart", help="start VibeNode at sign-in")
    auto.add_argument("action", choices=("install", "remove", "status"))
    auto.set_defaults(fn=_autostart)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
