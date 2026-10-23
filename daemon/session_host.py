"""Session Host: one process per live session that owns the CLI's stdio.

Spec: docs/plans/session-host-spec.md.  Run as
``python -m daemon.session_host --sid <id> --hosts-dir <dir> --logs-dir <dir>``
(``daemon/session_host_transport.spawn_host`` does this, detached).

Why: the Session Engine used to be the CLI's parent, holding its pipes, so a
restart (a ``kill -9`` of the engine) closed them and every CLI exited, losing
the turn in flight, a pending permission and the timers.  The host holds the
pipes instead and relays them over a local TCP endpoint.  While no engine is
attached it keeps the CLI's stdin open, drains and buffers its stdout, and
hands everything to the next engine that attaches.

What the host does:

* Launches the CLI from ``<sid>.launch.json``: the argv the engine built with
  the SDK's own ``_build_command()`` (``daemon/backends/claude_host``), started
  as the SDK and ``sdk_patches`` 3/4 would (hidden window, own process group).
  The host never imports the SDK, so it starts fast and stays small.  Then it
  writes its record ``<sid>.json``: pid, CLI pid and create time, port, secret.
* Numbers every CLI stdout line (``seq``), buffers it, forwards it to the
  attached engine.  Acked lines are dropped only after a turn's ``result``
  (the whole current turn stays replayable).  64 MB in memory, then a spill
  file, 512 MB hard cap (``overflow``).
* Answers every ``initialize`` after the first from the CLI's cached answer:
  the CLI never sees a second initialize.
* On attach, replays the lines that arrived after the previous engine
  detached, plus any CLI control request (a permission prompt) still
  unanswered from before.
* Exits when the CLI exits (after the engine had up to 60 s to read the end),
  on ``shutdown`` (closes stdin, kills ITS OWN child tree, deletes its
  record), or as an orphan: no engine for ``VIBENODE_HOST_ORPHAN_MINUTES`` and
  no turn in flight.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Optional

from daemon import session_host_protocol as shp

logger = logging.getLogger("session_host")

MEM_CAP = 64 * 1024 * 1024
HARD_CAP = 512 * 1024 * 1024
CLI_EXIT_GRACE = 60.0
SHUTDOWN_GRACE = 1.5


class LineBuffer:
    """The CLI's output since the last finished, acknowledged turn.

    Entries are ``(seq, ts, line, turn_end)``.  When memory passes ``MEM_CAP``
    the oldest half is appended to a spill file and indexed by offset; a
    replay reads the spill first.  Past ``HARD_CAP`` the oldest lines are
    dropped and ``overflow`` is set (a replay is then incomplete; the
    transcript still has every message)."""

    def __init__(self, spill: Path):
        self.mem: deque = deque()
        self.mem_bytes = 0
        self.spill_path = spill
        self.spilled: deque = deque()   # (seq, ts, offset, length, turn_end)
        self.spill_bytes = 0
        self.overflow = False

    def append(self, seq: int, ts: float, line: str, turn_end: bool) -> None:
        self.mem.append((seq, ts, line, turn_end))
        self.mem_bytes += len(line)
        if self.mem_bytes > MEM_CAP:
            self._spill()

    def _spill(self) -> None:
        try:
            self.spill_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.spill_path, "ab") as fh:
                while self.mem and self.mem_bytes > MEM_CAP // 2:
                    seq, ts, line, end = self.mem.popleft()
                    data = line.encode("utf-8")
                    off = fh.tell()
                    fh.write(data + b"\n")
                    self.spilled.append((seq, ts, off, len(data), end))
                    self.mem_bytes -= len(line)
                    self.spill_bytes += len(data) + 1
        except Exception as exc:
            logger.warning("spill failed (%s); dropping oldest lines", exc)
            while self.mem and self.mem_bytes > MEM_CAP // 2:
                self.mem_bytes -= len(self.mem.popleft()[2])
                self.overflow = True
        while self.spill_bytes + self.mem_bytes > HARD_CAP and self.spilled:
            _, _, _, length, _ = self.spilled.popleft()
            self.spill_bytes -= length + 1
            self.overflow = True

    def trim_through(self, seq: int) -> None:
        while self.spilled and self.spilled[0][0] <= seq:
            _, _, _, length, _ = self.spilled.popleft()
            self.spill_bytes -= length + 1
        if not self.spilled:
            self.spill_bytes = 0
            try:
                self.spill_path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass
        while self.mem and self.mem[0][0] <= seq:
            self.mem_bytes -= len(self.mem.popleft()[2])

    def since(self, ts: float) -> list:
        """``(seq, line, turn_end)`` of every buffered line received after ``ts``."""
        out = []
        if self.spilled:
            try:
                with open(self.spill_path, "rb") as fh:
                    for seq, lts, off, length, end in self.spilled:
                        if lts > ts:
                            fh.seek(off)
                            out.append((seq, fh.read(length).decode("utf-8"), end))
            except Exception as exc:
                logger.warning("reading the spill failed: %s", exc)
                self.overflow = True
        out.extend((seq, line, end) for seq, lts, line, end in self.mem if lts > ts)
        return out

    def clear(self) -> None:
        self.mem.clear()
        self.mem_bytes = 0
        self.trim_through(1 << 62)


def check_launch_allowed(spec: dict) -> None:
    """Under the test suite (``VIBENODE_HOST_FORBID_REAL_CLI``, set by
    tests/conftest.py and inherited by every process a test starts) only a
    CLI the test supplied may run, never the real Claude CLI."""
    if os.environ.get("VIBENODE_HOST_FORBID_REAL_CLI") and not spec.get("test_cli"):
        raise RuntimeError("Session Host refused to launch the real CLI: "
                           "VIBENODE_HOST_FORBID_REAL_CLI is set and no test CLI was given")
    argv = spec.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise RuntimeError("launch spec has no argv")


class CLIProcess:
    """The CLI as the host runs it: the argv the engine built with the SDK,
    started the way the SDK's ``SubprocessCLITransport.connect()`` and
    ``sdk_patches`` 3/4 start it (stdin/stdout pipes, the engine's
    environment plus the spec's, a hidden window on Windows, its own process
    group on POSIX so the host's tree kill reaches only the CLI's group)."""

    def __init__(self, spec: dict):
        check_launch_allowed(spec)
        self.spec = spec
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.pid = 0

    async def start(self) -> None:
        import subprocess
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.spec.get("env") or {}).items()})
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        self.proc = await asyncio.create_subprocess_exec(
            *self.spec["argv"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=self.spec.get("cwd") or None, env=env,
            limit=shp.FRAME_LIMIT, **kwargs)
        self.pid = int(self.proc.pid)

    async def read_messages(self):
        """Each stdout line that parses as a JSON object (the CLI's
        stream-json); anything else is logged and skipped."""
        out = self.proc.stdout
        while True:
            try:
                raw = await out.readline()
            except ValueError as exc:      # a line over FRAME_LIMIT
                logger.warning("CLI line over the frame limit skipped: %s", exc)
                continue
            if not raw:
                return
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                logger.info("non-JSON CLI output skipped: %r", raw[:200])
                continue
            if isinstance(msg, dict):
                yield msg

    async def write(self, line: str) -> None:
        self.proc.stdin.write((line + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def end_input(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass

    @property
    def returncode(self) -> Optional[int]:
        return self.proc.returncode if self.proc is not None else None

    async def close(self) -> None:
        if self.proc is None:
            return
        await self.end_input()
        if self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
            except Exception:
                pass
        try:
            await asyncio.wait_for(self.proc.wait(), 5.0)
        except Exception:
            pass


class Host:
    def __init__(self, sid: str, hosts_dir: Path, logs_dir: Path):
        self.sid = sid
        self.hosts_dir = hosts_dir
        self.logs_dir = logs_dir
        self.spec: dict = {}
        self.secret = ""
        self.cli = None
        self.cli_pid = 0
        self.cli_create_time = 0.0
        self.cli_name = ""
        self.started_at = time.time()
        self.seq = 0
        self.buf = LineBuffer(shp.spill_path(sid, hosts_dir))
        self.last_turn_end = 0
        self.writer: Optional[asyncio.StreamWriter] = None
        self.detached_at = self.started_at
        self.turn_in_flight = False
        self.init_req_id: Optional[str] = None
        self.init_cached: Optional[dict] = None
        self.pending: dict = {}          # CLI -> engine request id -> (seq, ts, line)
        self.cli_exited = False
        self.exit_code: Optional[int] = None
        self.exit_delivered = False
        self.last_ack = 0
        self.done = asyncio.Event()
        self.server = None
        self._shutting = False
        self.port = 0
        self.engine_boot_id = ""
        self.host_create_time = shp.process_create_time(os.getpid()) or self.started_at
        self.host_name = shp.process_name(os.getpid())

    # ── record ───────────────────────────────────────────────────────────
    def record(self) -> dict:
        return {
            "sid": self.sid, "proto": shp.PROTO,
            "host_pid": os.getpid(),
            "host_create_time": self.host_create_time,
            "host_name": self.host_name,
            "cli_pid": self.cli_pid, "cli_create_time": self.cli_create_time,
            "cli_name": self.cli_name,
            "port": self.port, "secret": self.secret,
            "cwd": self.spec.get("cwd") or "",
            "started_at": self.started_at,
            "turn_in_flight": self.turn_in_flight,
            "detached_at": 0.0 if self.writer is not None else self.detached_at,
            "attached": self.writer is not None,
            "engine_boot_id": self.engine_boot_id,
            "cli_exited": self.cli_exited,
        }

    def save_record(self) -> None:
        if self._shutting and self.done.is_set():
            return
        try:
            shp.write_json_atomic(shp.record_path(self.sid, self.hosts_dir), self.record())
        except Exception as exc:
            logger.warning("record write failed: %s", exc)

    # ── lifecycle ────────────────────────────────────────────────────────
    async def run(self) -> int:
        lp = shp.launch_path(self.sid, self.hosts_dir)
        spec = shp.read_json(lp)
        try:
            lp.unlink()      # it carries the secret; the record keeps it from here
        except Exception:
            pass
        if not spec or spec.get("sid") != self.sid:
            logger.error("no launch spec for %s", self.sid)
            return 2
        self.spec = spec
        self.secret = spec.get("secret") or ""
        self.engine_boot_id = spec.get("engine_boot_id") or ""
        if int(spec.get("proto") or 0) != shp.PROTO or not self.secret:
            logger.error("launch spec proto/secret mismatch")
            return 2
        try:
            self.cli = CLIProcess(spec)
            await self.cli.start()
        except Exception as exc:
            logger.exception("CLI launch failed: %s", exc)
            return 3
        self.cli_pid = self.cli.pid
        self.cli_create_time = shp.process_create_time(self.cli_pid) or time.time()
        self.cli_name = shp.process_name(self.cli_pid)
        self.server = await asyncio.start_server(self._on_conn, "127.0.0.1", 0,
                                                 limit=shp.FRAME_LIMIT)
        self.port = self.server.sockets[0].getsockname()[1]
        self.save_record()
        logger.info("host up: sid=%s pid=%d cli_pid=%d port=%d", self.sid,
                    os.getpid(), self.cli_pid, self.port)
        pump = asyncio.create_task(self._pump_cli())
        orphan = asyncio.create_task(self._orphan_watch())
        owner = asyncio.create_task(self._test_owner_watch())
        await self.done.wait()
        for t in (pump, orphan, owner):
            t.cancel()
        try:
            self.server.close()
        except Exception:
            pass
        shp.delete_host_files(self.sid, self.hosts_dir)
        logger.info("host exit: sid=%s cli exit code=%s", self.sid, self.exit_code)
        return 0

    # ── CLI -> engine ────────────────────────────────────────────────────
    async def _pump_cli(self) -> None:
        from daemon.backends import claude_wire as wire
        try:
            async for msg in self.cli.read_messages():
                self._on_cli_message(msg, wire)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("CLI stream ended: %s", exc)
        self.cli_exited = True
        self.exit_code = self.cli.returncode
        if self.exit_code is None:
            for _ in range(50):
                await asyncio.sleep(0.1)
                if self.cli.returncode is not None:
                    break
            self.exit_code = self.cli.returncode
        logger.info("CLI exited with code %s", self.exit_code)
        self.turn_in_flight = False
        self.save_record()
        if self._shutting:
            return
        if self.writer is not None:
            self._send({"t": "cli_exit", "code": self.exit_code or 0})
            self.exit_delivered = True
        # Give an engine (attached now, or one about to attach) the end of the
        # stream before the host goes away: it has read everything, or it was
        # told and has left.
        end = time.monotonic() + CLI_EXIT_GRACE
        while time.monotonic() < end and not self.done.is_set():
            if self.last_ack >= self.seq or (self.exit_delivered and self.writer is None):
                break
            await asyncio.sleep(0.2)
        await self._finish()

    def _on_cli_message(self, msg: dict, wire) -> None:
        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
        rid = wire.response_request_id(msg)
        if rid is not None and rid == self.init_req_id:
            self.init_cached = dict(msg.get("response") or {})
            self.init_req_id = None
        self.seq += 1
        now = time.time()
        turn_end = wire.is_turn_end(msg)
        creq = wire.cli_request_id(msg)
        if creq:
            self.pending[creq] = (self.seq, now, line)
        if turn_end:
            self.last_turn_end = self.seq
            if self.turn_in_flight:
                self.turn_in_flight = False
                self.save_record()
        elif wire.is_turn_activity(msg) and not self.turn_in_flight:
            self.turn_in_flight = True
            self.save_record()
        self.buf.append(self.seq, now, line, turn_end)
        self._send({"t": "msg", "seq": self.seq, "line": line})

    def _send(self, frame: dict) -> None:
        w = self.writer
        if w is None or w.is_closing():
            return
        try:
            w.write(shp.encode(frame))
        except Exception:
            pass

    # ── engine -> host ───────────────────────────────────────────────────
    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await asyncio.wait_for(shp.read_frame(reader), 5.0)
        except Exception:
            first = None
        if not first or first.get("t") != "hello" or not shp.secret_ok(first.get("secret"), self.secret):
            logger.warning("refused a connection: bad or missing hello")
            try:
                writer.write(shp.encode({"t": "hello_err", "reason": "auth"}))
                await writer.drain()
            except Exception:
                pass
            writer.close()
            return
        try:
            proto = int(first.get("proto") or 0)
        except (TypeError, ValueError):
            proto = 0
        if proto != shp.PROTO:
            # An engine speaking another protocol may only retire this host.
            writer.write(shp.encode({"t": "hello_err", "reason": "proto",
                                     "host_proto": shp.PROTO,
                                     "turn_in_flight": self.turn_in_flight}))
            await writer.drain()
            try:
                nxt = await asyncio.wait_for(shp.read_frame(reader), 10.0)
            except Exception:
                nxt = None
            if nxt and nxt.get("t") == "shutdown":
                await self._shutdown(writer)
            else:
                writer.close()
            return
        await self._attach(reader, writer)

    async def _attach(self, reader, writer) -> None:
        from daemon.backends import claude_wire as wire
        old = self.writer
        if old is not None:
            logger.info("a newer engine attached; dropping the previous one")
            try:
                old.close()
            except Exception:
                pass
        # Replay: unanswered CLI requests from before the detach, then every
        # line received after it.  Computed and written with no await in
        # between, so no live line can slip ahead of the replay.
        since = self.detached_at
        lines = self.buf.since(since)
        later = {seq for seq, _, _ in lines}
        early = sorted((v for v in self.pending.values() if v[0] not in later and v[1] <= since),
                       key=lambda v: v[0])
        replay = [(seq, line, False) for seq, _, line in early] + lines
        hello_ok = {
            "t": "hello_ok", "cli_pid": self.cli_pid, "turn_in_flight": self.turn_in_flight,
            "init_cached": self.init_cached is not None, "detached_at": since,
            "replay_from": replay[0][0] if replay else 0,
            "replay_to": replay[-1][0] if replay else 0,
            "replay_count": len(replay),
            "replay_has_result": any(end for _, _, end in replay),
            "overflow": self.buf.overflow, "cli_exited": self.cli_exited,
            "exit_code": self.exit_code, "sid": self.sid,
        }
        writer.write(shp.encode(hello_ok))
        for seq, line, _ in replay:
            writer.write(shp.encode({"t": "msg", "seq": seq, "line": line, "r": 1}))
        if self.cli_exited:
            writer.write(shp.encode({"t": "cli_exit", "code": self.exit_code or 0}))
            self.exit_delivered = True
        self.writer = writer
        self.detached_at = 0.0
        self.save_record()
        logger.info("engine attached (replayed %d line(s), turn_in_flight=%s)",
                    len(replay), self.turn_in_flight)
        try:
            await writer.drain()
            while True:
                frame = await shp.read_frame(reader)
                if frame is None:
                    break
                t = frame.get("t")
                if t == "write":
                    await self._on_write(frame.get("line") or "", wire)
                elif t == "ack":
                    self._on_ack(int(frame.get("seq") or 0))
                elif t == "end_input":
                    try:
                        await self.cli.end_input()
                    except Exception:
                        pass
                elif t == "detach":
                    logger.info("engine detached")
                    break
                elif t == "shutdown":
                    await self._shutdown(writer)
                    return
                elif t == "rename":
                    self._rename(str(frame.get("sid") or ""))
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.exception("engine connection error")
        if self.writer is writer:
            self.writer = None
            self.detached_at = time.time()
            self.save_record()
            logger.info("engine gone; buffering (turn_in_flight=%s)", self.turn_in_flight)
        try:
            writer.close()
        except Exception:
            pass

    async def _on_write(self, line: str, wire) -> None:
        try:
            msg = json.loads(line)
        except Exception:
            msg = None
        if isinstance(msg, dict):
            rid = wire.init_request_id(msg)
            if rid is not None and self.init_cached is not None:
                # A reattaching engine's Query.initialize(): the CLI already
                # answered one and must not see a second.
                answer = wire.cached_init_answer(self.init_cached, rid)
                self._send({"t": "msg", "seq": 0,
                            "line": json.dumps(answer, separators=(",", ":"))})
                return
            if rid is not None:
                self.init_req_id = rid
            answered = wire.response_request_id(msg)
            if answered:
                self.pending.pop(answered, None)
            if wire.is_user_input(msg) and not self.turn_in_flight:
                self.turn_in_flight = True
                self.save_record()
        if self.cli_exited:
            return
        try:
            await self.cli.write(line)
        except Exception as exc:
            logger.warning("write to CLI failed: %s", exc)

    def _on_ack(self, seq: int) -> None:
        self.last_ack = max(self.last_ack, seq)
        cut = min(self.last_ack, self.last_turn_end)
        if cut > 0:
            self.buf.trim_through(cut)

    def _rename(self, new_sid: str) -> None:
        if not new_sid or new_sid == self.sid or shp.safe_id(new_sid) != new_sid:
            return
        old = self.sid
        self.sid = new_sid
        self.buf.spill_path = shp.spill_path(new_sid, self.hosts_dir)
        self.save_record()
        shp.delete_host_files(old, self.hosts_dir)
        logger.info("renamed %s -> %s", old, new_sid)

    # ── endings ──────────────────────────────────────────────────────────
    async def _orphan_watch(self) -> None:
        limit = shp.orphan_seconds()
        tick = max(0.2, min(30.0, limit / 4.0))
        while not self.done.is_set():
            await asyncio.sleep(tick)
            if self.writer is None and not self.turn_in_flight \
                    and time.time() - self.detached_at > limit:
                logger.info("orphaned for %.0fs with no turn in flight; exiting", limit)
                await self._shutdown(None)
                return

    async def _test_owner_watch(self) -> None:
        """Test suite only.  ``VIBENODE_HOST_TEST_OWNER`` (``<pid>:<create
        time>``, set by tests/conftest.py and inherited by every host a test
        starts) names the pytest process that owns this host.  A host outlives
        its engine by design, so a pytest run that is stopped or killed would
        otherwise leave it, and its fake CLI, running.  When the owner is gone
        the host shuts down exactly as on ``shutdown``.  Unset in production:
        this task returns at once."""
        raw = os.environ.get("VIBENODE_HOST_TEST_OWNER") or ""
        try:
            pid_s, ct_s = raw.split(":", 1)
            pid, ct = int(pid_s), float(ct_s)
        except ValueError:
            return
        while not self.done.is_set():
            await asyncio.sleep(1.0)
            if not shp.same_process(pid, ct):
                logger.info("test owner pid %d is gone; shutting down", pid)
                await self._shutdown(None)
                return

    async def _shutdown(self, writer) -> None:
        """Close the CLI's stdin, then kill this host's OWN child tree, delete
        the record and exit.  The kill reuses ``SessionManager._kill_process_tree``
        and its guards; its target is the pid this process spawned."""
        if self._shutting:
            return
        self._shutting = True
        logger.info("shutting down the CLI (pid %d)", self.cli_pid)
        try:
            await self.cli.end_input()
        except Exception:
            pass
        end = time.monotonic() + SHUTDOWN_GRACE
        while time.monotonic() < end and self.cli.returncode is None:
            await asyncio.sleep(0.05)
        try:
            from daemon.session_manager import SessionManager
            await asyncio.get_running_loop().run_in_executor(
                None, SessionManager._kill_process_tree, self.cli_pid)
        except Exception as exc:
            logger.warning("tree kill failed: %s", exc)
        await self.cli.close()
        self.exit_code = self.cli.returncode
        for w in (writer, self.writer):
            if w is not None:
                try:
                    w.write(shp.encode({"t": "bye"}))
                    await asyncio.wait_for(w.drain(), 2.0)
                    w.close()
                except Exception:
                    pass
        self.writer = None
        await self._finish()

    async def _finish(self) -> None:
        shp.delete_host_files(self.sid, self.hosts_dir)
        self.done.set()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="session_host")
    ap.add_argument("--sid", required=True)
    ap.add_argument("--hosts-dir", required=True)
    ap.add_argument("--logs-dir", required=True)
    args = ap.parse_args(argv)
    sid = args.sid
    if shp.safe_id(sid) != sid:
        return 2
    logs = Path(args.logs_dir)
    logs.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(shp.log_path(sid, logs)), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = Host(sid, Path(args.hosts_dir), logs)
    try:
        return asyncio.run(host.run())
    except Exception:
        logger.exception("host crashed")
        shp.delete_host_files(host.sid, host.hosts_dir)
        return 1


if __name__ == "__main__":
    sys.exit(main())
