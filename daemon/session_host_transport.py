"""Spawning and reaching Session Hosts (spec: docs/plans/session-host-spec.md).

Generic: nothing here knows the CLI.  ``daemon/backends/claude_host.py``
builds its SDK transport on top of :func:`spawn_and_wait` and
:func:`open_host`; the registry (``daemon/session_host_registry.py``) uses the
synchronous helpers to retire hosts.

The host is spawned DETACHED and directly (no wscript/nohup in between): the
engine restart kills the engine by pid, never by tree or group, and a direct
child gives the record a true ``host_pid``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from common.paths import REPO_ROOT
from daemon import session_host_protocol as shp

logger = logging.getLogger(__name__)

SPAWN_TIMEOUT = 10.0


class HostSpawnError(RuntimeError):
    pass


def _host_env() -> dict:
    env = dict(os.environ)
    try:
        import process_label
        env.update(process_label.marker_env("session-host"))
    except Exception:
        pass
    # The host imports the repo's packages by module name.
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + env["PYTHONPATH"]
                                          if env.get("PYTHONPATH") else "")
    return env


def spawn_host(sid: str, hosts_dir=None, logs_dir=None) -> subprocess.Popen:
    """Start ``python -m daemon.session_host`` for ``sid``, detached from this
    process so it outlives an engine kill.

    Windows: CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW |
    CREATE_BREAKAWAY_FROM_JOB.  A terminal-launched engine can sit in a job
    with kill-on-close; breakaway takes the host out of it.  A job that does
    not permit breakaway refuses the flag, so the spawn is retried without it
    (logged).  POSIX: a new session, so the host is in no group the engine's
    restart could signal."""
    hosts = shp.hosts_dir(hosts_dir)
    logs = Path(logs_dir) if logs_dir else shp.LOGS_DIR
    hosts.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "daemon.session_host", "--sid", sid,
           "--hosts-dir", str(hosts), "--logs-dir", str(logs)]
    common = dict(cwd=str(REPO_ROOT), env=_host_env(), close_fds=True,
                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL)
    if os.name == "nt":
        # No DETACHED_PROCESS: Windows ignores CREATE_NO_WINDOW alongside it, so
        # the venv python.exe launcher starts consoleless and the interpreter it
        # spawns opens a NEW VISIBLE terminal. CREATE_NO_WINDOW alone gives the
        # host its own hidden console, separate from the engine's.
        base = (subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        try:
            return subprocess.Popen(cmd, creationflags=base | breakaway, **common)
        except OSError as exc:
            logger.warning("Session host %s: job refused breakaway (%s); "
                           "spawning inside the job", sid, exc)
            return subprocess.Popen(cmd, creationflags=base, **common)
    return subprocess.Popen(cmd, start_new_session=True, **common)


def _log_tail(sid: str, logs_dir=None, n: int = 15) -> str:
    try:
        lines = shp.log_path(sid, logs_dir).read_text(encoding="utf-8", errors="replace").splitlines()
        return " | ".join(lines[-n:])
    except Exception:
        return ""


async def spawn_and_wait(sid: str, launch_spec: dict, hosts_dir=None, logs_dir=None,
                         timeout: float = SPAWN_TIMEOUT) -> dict:
    """Write the launch spec, spawn the host, and wait for its record to name
    a port.  Returns the record."""
    spec = dict(launch_spec)
    spec.update({"sid": sid, "proto": shp.PROTO, "secret": shp.new_secret(),
                 "created_at": time.time()})
    shp.delete_host_files(sid, hosts_dir)
    shp.write_json_atomic(shp.launch_path(sid, hosts_dir), spec)
    proc = spawn_host(sid, hosts_dir, logs_dir)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = shp.read_record(sid, hosts_dir)
        if rec and rec.get("port") and rec.get("secret") == spec["secret"]:
            rec["spawn_pid"] = proc.pid
            return rec
        if proc.poll() is not None and not shp.read_record(sid, hosts_dir):
            # A Windows venv python.exe is a launcher that exits only with its
            # child, so an early exit means the host itself is gone.
            raise HostSpawnError(f"Session host for {sid} exited (code {proc.returncode}) "
                                 f"before it was ready: {_log_tail(sid, logs_dir)}")
        await asyncio.sleep(0.05)
    raise HostSpawnError(f"Session host for {sid} did not come up within {timeout:.0f}s: "
                         f"{_log_tail(sid, logs_dir)}")


async def open_host(record: dict, timeout: float = 5.0, proto=None):
    """Connect to a host and say hello (``proto`` defaults to this engine's
    ``PROTO``, read at call time).  Returns ``(reader, writer, reply)`` where
    ``reply`` is the host's ``hello_ok`` or ``hello_err`` frame."""
    if proto is None:
        proto = shp.PROTO
    port = record.get("port")
    if type(port) is not int or not (0 < port < 65536):
        raise ConnectionError(f"bad port in host record: {port!r}")
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port, limit=shp.FRAME_LIMIT), timeout)
    writer.write(shp.encode({"t": "hello", "secret": record.get("secret") or "",
                             "proto": proto, "sid": record.get("sid")}))
    await writer.drain()
    reply = await asyncio.wait_for(shp.read_frame(reader), timeout)
    if reply is None:
        writer.close()
        raise ConnectionError("host closed the connection during hello")
    return reader, writer, reply


# ── Synchronous helpers (registry, reconcile) ────────────────────────────

def _run(coro, timeout: float):
    """Run ``coro`` on a private loop (callers are plain threads)."""
    return asyncio.run(asyncio.wait_for(coro, timeout))


async def _shutdown(record: dict, wait: float) -> bool:
    reader, writer, reply = await open_host(record, timeout=5.0)
    try:
        if reply.get("t") not in ("hello_ok", "hello_err"):
            return False
        writer.write(shp.encode({"t": "shutdown"}))
        await writer.drain()
        # The host says "bye" and closes once its CLI is gone.
        end = time.monotonic() + wait
        while time.monotonic() < end:
            frame = await asyncio.wait_for(shp.read_frame(reader), max(0.1, end - time.monotonic()))
            if frame is None or frame.get("t") == "bye":
                return True
        return False
    finally:
        try:
            writer.close()
        except Exception:
            pass


def shutdown_host_sync(record: dict, wait: float = 15.0) -> bool:
    """Tell a host to close its CLI and exit.  True once it confirmed."""
    try:
        return bool(_run(_shutdown(record, wait), wait + 6.0))
    except Exception as exc:
        logger.info("Session host %s: shutdown request failed: %s", record.get("sid"), exc)
        return False


def wait_host_gone(record: dict, timeout: float = 5.0) -> bool:
    pid = record.get("host_pid")
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not shp.same_process(pid, record.get("host_create_time"), record.get("host_name") or ""):
            return True
        time.sleep(0.1)
    return False
