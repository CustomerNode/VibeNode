"""Durable Jobs step 2: ``vibenode job run`` and ``POST /api/jobs/run``.

One command launches the job detached on this OS and registers it in the
same step (spec rule 2).  The CLI needs no server; the API refuses callers
that are not on this machine (it runs an arbitrary command) and refuses a job
with no done test, exactly like the CLI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from daemon import durable_jobs as dj

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "vibenode.py"


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    monkeypatch.setenv("VIBENODE_JOBS_FILE", str(path))
    return path


def _cli(*args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True,
                          text=True, env=e, timeout=60)


def _wait_for(path_fn, needle, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        p = path_fn()
        if p and needle in dj.read_tail(p):
            return True
        time.sleep(0.2)
    return False


def test_cli_launches_detached_and_registers_in_one_step(reg, tmp_path):
    cmd = f'"{sys.executable}" -c "print(\'working\'); print(\'ALL DONE\')"'
    r = _cli("job", "run", "--name", "cli demo", "--command", cmd, "--cwd", str(tmp_path),
             "--done", "ALL DONE", "--log", str(tmp_path / "cli-{ts}.log"),
             "--session", "sess-1", "--project", "Demo", "--progress", r"step (\d+)/(\d+)",
             env={"VIBENODE_JOBS_FILE": str(reg)})
    assert r.returncode == 0, r.stderr
    job_id, log, state = r.stdout.strip().split("|")
    assert state == "running" and job_id.startswith("cli-demo-")
    rec = dj.get_job(job_id, reg)
    assert rec["session_id"] == "sess-1" and rec["project"] == "Demo"
    assert rec["progress"]["pattern"] == r"step (\d+)/(\d+)"
    assert _wait_for(lambda: log, "ALL DONE"), dj.read_tail(log)
    assert dj.is_done(rec)
    # The list sees it without any server running.
    r2 = _cli("job", "list", "--json", env={"VIBENODE_JOBS_FILE": str(reg)})
    assert [j["id"] for j in json.loads(r2.stdout)] == [job_id]


def test_cli_refuses_a_missing_done_test_and_launches_nothing(reg, tmp_path):
    marker = tmp_path / "ran.txt"
    cmd = f'"{sys.executable}" -c "open(r\'{marker}\',\'w\').write(\'x\')"'
    r = _cli("job", "run", "--name", "nodone", "--command", cmd, "--cwd", str(tmp_path),
             env={"VIBENODE_JOBS_FILE": str(reg)})
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "--done" in r.stderr
    time.sleep(0.5)
    assert not marker.exists(), "a refused job must not run"
    assert not reg.exists() or dj.load_registry(reg)["jobs"] == {}
    # An empty pattern is refused by the library with the spec's reason.
    r = _cli("job", "run", "--name", "nodone", "--command", cmd, "--cwd", str(tmp_path),
             "--done", "", env={"VIBENODE_JOBS_FILE": str(reg)})
    assert r.returncode == 2 and "done test is required" in r.stderr


@pytest.fixture
def client(reg, monkeypatch):
    from app import create_app
    app = create_app(testing=True)
    return app.test_client()


def _run_body(tmp_path, **over):
    body = {"name": "api demo", "command": f'"{sys.executable}" -c "print(\'API DONE\')"',
            "cwd": str(tmp_path), "done_test": "API DONE",
            "log_path": str(tmp_path / "api-{ts}.log")}
    body.update(over)
    return body


def test_api_run_launches_and_registers_for_a_local_caller(client, reg, tmp_path):
    r = client.post("/api/jobs/run", json=_run_body(tmp_path), environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 200, r.get_json()
    job = r.get_json()["job"]
    assert job["state"] == "running"
    assert _wait_for(lambda: job["last_log"], "API DONE")
    listed = client.get("/api/jobs").get_json()["jobs"]
    assert [j["id"] for j in listed] == [job["id"]]
    tail = client.get(f"/api/jobs/{job['id']}/log").get_json()
    assert "API DONE" in tail["tail"]
    r = client.post(f"/api/jobs/{job['id']}/leave")
    assert r.get_json()["job"]["acknowledged"] is True


def test_api_run_refuses_remote_callers_and_missing_done_test(client, reg, tmp_path):
    body = _run_body(tmp_path)
    for headers in ({"X-Forwarded-For": "100.64.0.9"}, {"Tailscale-User-Login": "don@x"},
                    {"Origin": "https://evil.example"}):
        r = client.post("/api/jobs/run", json=body, headers=headers,
                        environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert r.status_code == 403, headers
    r = client.post("/api/jobs/run", json=body, environ_base={"REMOTE_ADDR": "10.0.0.5"})
    assert r.status_code == 403
    r = client.post("/api/jobs/run", json=_run_body(tmp_path, done_test=""),
                    environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 400 and "done test" in r.get_json()["error"]
    assert not reg.exists() or dj.load_registry(reg)["jobs"] == {}


def test_api_relaunch_runs_the_registered_command_again(client, reg, tmp_path, monkeypatch):
    launches = []
    def fake_launch(command, cwd, log_path, env):
        launches.append(log_path)
        return {"ok": True, "pid": 1, "launcher": "", "error": ""}
    monkeypatch.setattr(dj, "launch_detached", fake_launch)
    rec = dj.run_job(launch=fake_launch, name="r", command="x", cwd=str(tmp_path),
                     done_test="D", job_id="r1", log_path=str(tmp_path / "r-{ts}.log"))
    with dj.edit_registry(reg) as data:
        data["jobs"]["r1"]["state"] = "needs_person"
        data["jobs"]["r1"]["relaunches_without_progress"] = 3
        data["jobs"]["r1"]["acknowledged"] = True
    r = client.post("/api/jobs/r1/relaunch")
    assert r.status_code == 200 and r.get_json()["ok"], r.get_json()
    job = r.get_json()["job"]
    assert job["state"] == "relaunched" and job["relaunch_count"] == 1
    assert job["relaunches_without_progress"] == 0 and job["acknowledged"] is False
    assert len(launches) == 2
    assert client.post("/api/jobs/nope/relaunch").status_code == 404


def test_api_reports_a_corrupt_registry_without_touching_it(client, reg):
    reg.write_bytes(b"{broken")
    r = client.get("/api/jobs")
    assert r.status_code == 500 and r.get_json()["registry_error"]
    assert reg.read_bytes() == b"{broken"
