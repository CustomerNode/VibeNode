"""Shared helpers for the Session Host tests: a fake-CLI launcher, a raw
protocol client, and guarded cleanup.  Every host these tests start runs
``tests/fake_cli.py``, never the real Claude CLI."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from daemon import session_host_protocol as shp
from daemon import session_host_transport as sht

FAKE_CLI = Path(__file__).resolve().parent / "fake_cli.py"


def make_fake_cli(folder: Path, **env) -> str:
    """A launcher for ``fake_cli.py`` usable as the SDK's ``cli_path`` (the
    SDK runs ``[cli_path, flags...]``).  ``env`` is baked into the launcher."""
    folder.mkdir(parents=True, exist_ok=True)
    py = getattr(sys, "_base_executable", None) or sys.executable
    if os.name == "nt":
        p = folder / "fake-claude.bat"
        lines = ["@echo off"] + [f'set "{k}={v}"' for k, v in env.items()]
        lines.append(f'"{py}" "{FAKE_CLI}" %*')
        p.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    else:
        p = folder / "fake-claude"
        exports = "".join(f"export {k}={json.dumps(str(v))}\n" for k, v in env.items())
        p.write_text(f"#!/bin/sh\n{exports}exec {json.dumps(py)} {json.dumps(str(FAKE_CLI))} \"$@\"\n",
                     encoding="utf-8")
        p.chmod(0o755)
    return str(p)


def read_log(path) -> list:
    try:
        return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    except FileNotFoundError:
        return []


def wait_until(fn, timeout: float = 10.0, interval: float = 0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return fn()


def launch_spec(cwd, cli_path, **options) -> dict:
    """The spec the engine writes, built the engine's way (the SDK's argv)."""
    from claude_code_sdk import ClaudeCodeOptions
    from daemon.backends.claude_host import launch_spec_from_options
    opts = {"cwd": str(cwd), "permission_prompt_tool_name": "stdio",
            "include_partial_messages": False}
    opts.update(options)
    return launch_spec_from_options(ClaudeCodeOptions(**opts), cli_path)


def launch_host(sid: str, cwd, cli_path, **options) -> dict:
    return asyncio.run(sht.spawn_and_wait(sid, launch_spec(cwd, cli_path, **options), timeout=20))


class RawClient:
    """The engine's side of the wire, by hand."""

    def __init__(self, record: dict):
        self.record = record
        self.reader = self.writer = None
        self.hello = None

    async def connect(self, proto=None, secret=None):
        rec = dict(self.record)
        if secret is not None:
            rec["secret"] = secret
        self.reader, self.writer, self.hello = await sht.open_host(rec, timeout=10, proto=proto)
        return self.hello

    async def send(self, frame: dict):
        self.writer.write(shp.encode(frame))
        await self.writer.drain()

    async def write_line(self, msg: dict):
        await self.send({"t": "write", "line": json.dumps(msg)})

    async def user(self, text: str):
        await self.write_line({"type": "user", "message": {"role": "user", "content": text},
                               "parent_tool_use_id": None, "session_id": "default"})

    async def frame(self, timeout=10.0):
        return await asyncio.wait_for(shp.read_frame(self.reader), timeout)

    async def until(self, pred, timeout=15.0) -> list:
        """Frames up to and including the first one ``pred`` accepts."""
        got = []
        end = time.monotonic() + timeout
        while True:
            f = await self.frame(max(0.1, end - time.monotonic()))
            if f is None:
                raise AssertionError(f"connection closed; frames so far: {got}")
            got.append(f)
            if pred(f):
                return got

    def abort(self):
        self.writer.transport.abort()


def msg_of(frame):
    return json.loads(frame["line"]) if frame.get("t") == "msg" else None


def is_type(t, **match):
    def pred(f):
        m = msg_of(f)
        if not m or m.get("type") != t:
            return False
        return all(m.get(k) == v for k, v in match.items())
    return pred


def texts(frames) -> list:
    out = []
    for f in frames:
        m = msg_of(f)
        if m and m.get("type") == "assistant":
            out += [b.get("text") for b in m["message"]["content"] if b.get("type") == "text"]
    return out


def kill_if_same(pid, create_time, name=""):
    """Kill a test-started process tree, only if it is provably that process."""
    if shp.same_process(pid, create_time, name):
        from daemon.session_manager import SessionManager
        SessionManager._kill_process_tree(pid)


def reap_hosts(base=None):
    """Shut down every host left in the (sandboxed) hosts folder."""
    for rec in shp.list_records(base):
        if shp.process_alive(rec.get("host_pid")):
            sht.shutdown_host_sync(rec, wait=10)
        if not sht.wait_host_gone(rec, 5):
            kill_if_same(rec.get("cli_pid"), rec.get("cli_create_time"), rec.get("cli_name") or "")
            kill_if_same(rec.get("host_pid"), rec.get("host_create_time"), rec.get("host_name") or "")
        shp.delete_host_files(rec["sid"], base)
