"""The Claude side of the Session Host (spec: docs/plans/session-host-spec.md).

Everything here knows the SDK's internals, which is why it lives under
``daemon/backends/`` and the host process (``daemon/session_host.py``) and
the registry stay generic:

* :func:`launch_spec_from_options`: the CLI's exact command line, built in
  the ENGINE by the SDK's own ``SubprocessCLITransport._build_command()`` (so
  ``_find_cli`` and ``cli_path`` behave exactly as for an in-process session).
  The host only runs that argv: it never imports the SDK, which keeps each
  host small (no ``claude_code_sdk``/``mcp``/``pydantic``) and fast to start.
* :class:`HostTransport`: an SDK ``Transport`` that talks to a host over its
  TCP endpoint instead of to a child's pipes.
* :class:`HostedSDKClient`: ``ClaudeSDKClient`` with only ``connect()``
  overridden to build a ``HostTransport``.  ``Query``, the control protocol
  and ``sdk_patches`` patch 2 (the adapter injected in ``Query.__init__``) are
  untouched, so permission-response reformatting and ``keep_stdin_open`` work
  on top of the host exactly as on a pipe.

The stream-json vocabulary the host relays by lives in ``claude_wire.py``
(SDK-free) and is re-exported here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import replace
from typing import Any, Optional

from claude_code_sdk import ClaudeSDKClient, ClaudeCodeOptions
from claude_code_sdk._errors import CLIConnectionError, ProcessError
from claude_code_sdk._internal.transport import Transport

from daemon import session_host_protocol as shp
from daemon.backends.claude_wire import (  # noqa: F401  (re-exported)
    cached_init_answer, cli_request_id, dedup_key, init_request_id, is_turn_activity,
    is_turn_end, is_user_input, response_request_id,
)

logger = logging.getLogger(__name__)


# ── Launch spec ──────────────────────────────────────────────────────────

async def _empty_stream() -> AsyncIterator[dict[str, Any]]:
    return
    yield {}  # pragma: no cover  (makes this an async generator)


def launch_spec_from_options(options: ClaudeCodeOptions, cli_path: Optional[str] = None) -> dict:
    """What a host needs to start the CLI exactly as the SDK would.

    ``argv`` comes from the SDK's ``SubprocessCLITransport`` (streaming mode,
    ``--input-format stream-json``); ``env`` and ``cwd`` follow its
    ``connect()``: the host's environment (inherited from this engine) plus
    ``options.env``, ``CLAUDE_CODE_ENTRYPOINT`` and ``PWD``.  ``test_cli``
    marks an explicit ``cli_path`` (tests' fake CLI); under the test suite a
    host refuses anything else.  Callbacks and hooks are not in the spec: the
    SDK registers hooks through ``initialize`` and answers permissions over
    the control protocol, both of which still happen in the engine."""
    from claude_code_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    t = SubprocessCLITransport(prompt=_empty_stream(), options=options,
                               cli_path=cli_path or None)
    argv = [str(a) for a in t._build_command()]
    cwd = str(options.cwd) if options.cwd else ""
    env = {str(k): str(v) for k, v in (options.env or {}).items()}
    env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"
    if cwd:
        env["PWD"] = cwd
    return {"argv": argv, "cwd": cwd, "env": env, "test_cli": bool(cli_path)}


# ── Engine side: Transport over the host's endpoint ──────────────────────

class HostTransport(Transport):
    """An SDK ``Transport`` whose CLI lives in a Session Host.

    ``attach_record=None``: spawn a new host from ``launch_spec`` and attach.
    ``attach_record=<record>``: attach to a host a previous engine left
    running.  Then regular (non-control) lines are HELD until the session
    manager calls :meth:`release_replay` (after it reloaded the timeline from
    the transcript), so ``Query.initialize()`` is never stuck behind a
    replay larger than the Query's 100-message buffer, and replayed lines the
    transcript already holds are skipped by uuid.
    """

    ACK_EVERY = 64

    def __init__(self, sid: str, *, launch_spec: Optional[dict] = None,
                 attach_record: Optional[dict] = None, hosts_dir=None, logs_dir=None,
                 connect_timeout: float = 10.0):
        self._sid = sid
        self._launch_spec = launch_spec
        self._record = attach_record
        self._attaching = attach_record is not None
        self._hosts_dir = hosts_dir
        self._logs_dir = logs_dir
        self._connect_timeout = connect_timeout
        self._reader = None
        self._writer = None
        self._inbox: Optional[asyncio.Queue] = None
        self._pump_task = None
        self._ready = False
        self._closed = False
        self._mode = "shutdown"
        self._released = not self._attaching
        self._held: deque = deque()
        self._skip: set = set()
        self._unacked = 0
        self._last_seq = 0
        self._exit_code: Optional[int] = None
        self.hello: dict = {}

    # -- what the engine reads ------------------------------------------------

    @property
    def host_record(self) -> Optional[dict]:
        return self._record

    @property
    def cli_pid(self) -> int:
        """The CLI's pid while the host is connected and the CLI running,
        else 0 (the engine reads 0 as "the CLI is gone")."""
        if not self._ready or self._exit_code is not None or not self._record:
            return 0
        pid = self._record.get("cli_pid")
        return pid if type(pid) is int and pid > 1 else 0

    # -- Transport ABC ---------------------------------------------------------

    async def connect(self) -> None:
        from daemon import session_host_transport as sht
        self._inbox = asyncio.Queue()
        if self._record is None:
            self._record = await sht.spawn_and_wait(
                self._sid, self._launch_spec or {}, hosts_dir=self._hosts_dir,
                logs_dir=self._logs_dir, timeout=self._connect_timeout)
        try:
            reader, writer, hello = await sht.open_host(self._record, timeout=self._connect_timeout)
        except Exception as exc:
            raise CLIConnectionError(f"Session host for {self._sid} unreachable: {exc}") from exc
        if hello.get("t") != "hello_ok":
            try:
                writer.close()
            except Exception:
                pass
            raise CLIConnectionError(f"Session host for {self._sid} refused the attach: "
                                     f"{hello.get('reason') or hello}")
        self._reader, self._writer, self.hello = reader, writer, hello
        if hello.get("cli_exited"):
            self._exit_code = int(hello.get("exit_code") or 0)
        self._ready = True
        self._pump_task = asyncio.get_running_loop().create_task(self._pump())

    async def _pump(self) -> None:
        inbox = self._inbox
        try:
            while True:
                frame = await shp.read_frame(self._reader)
                if frame is None:
                    inbox.put_nowait(("eof",))
                    return
                t = frame.get("t")
                if t == "msg":
                    try:
                        msg = json.loads(frame.get("line") or "")
                    except Exception:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    inbox.put_nowait(("msg", msg, bool(frame.get("r"))))
                    seq = frame.get("seq") or 0
                    if seq:
                        self._last_seq = max(self._last_seq, seq)
                        self._unacked += 1
                        if is_turn_end(msg) or self._unacked >= self.ACK_EVERY:
                            self._unacked = 0
                            self._send_nowait({"t": "ack", "seq": seq})
                elif t == "cli_exit":
                    self._send_nowait({"t": "ack", "seq": self._last_seq})
                    self._exit_code = int(frame.get("code") or 0)
                    inbox.put_nowait(("exit", self._exit_code))
                elif t == "bye":
                    inbox.put_nowait(("eof",))
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("HostTransport pump for %s ended: %s", self._sid, exc)
            inbox.put_nowait(("eof",))

    def _send_nowait(self, frame: dict) -> None:
        w = self._writer
        if w is None or w.is_closing():
            return
        try:
            w.write(shp.encode(frame))
        except Exception:
            pass

    async def _send(self, frame: dict) -> None:
        w = self._writer
        if w is None or w.is_closing() or self._closed:
            raise CLIConnectionError("Session host is not connected")
        try:
            w.write(shp.encode(frame))
            await w.drain()
        except Exception as exc:
            self._ready = False
            raise CLIConnectionError(f"Writing to the session host failed: {exc}") from exc

    async def write(self, data: str) -> None:
        if not self._ready:
            raise CLIConnectionError("Session host transport is not ready for writing")
        await self._send({"t": "write", "line": data.rstrip("\n")})

    async def end_input(self) -> None:
        if self._ready:
            try:
                await self._send({"t": "end_input"})
            except CLIConnectionError:
                pass

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._read_impl()

    def _is_dup(self, msg: dict, replayed: bool) -> bool:
        if not replayed or not self._skip:
            return False
        key = dedup_key(msg)
        return key is not None and key in self._skip

    async def _read_impl(self) -> AsyncIterator[dict[str, Any]]:
        inbox = self._inbox
        while True:
            item = await inbox.get()
            kind = item[0]
            if kind == "release":
                self._released = True
                while self._held:
                    msg, replayed = self._held.popleft()
                    if not self._is_dup(msg, replayed):
                        yield msg
                continue
            if kind == "msg":
                msg, replayed = item[1], item[2]
                # Before release only answers to the engine's own requests
                # (the initialize) pass; a replayed permission request waits
                # until the session around it is set up.
                if not self._released and response_request_id(msg) is None:
                    self._held.append((msg, replayed))
                    continue
                if self._is_dup(msg, replayed):
                    continue
                yield msg
                continue
            # The stream ends: the CLI exited, or the host went away.
            self._ready = False
            while self._held:
                msg, replayed = self._held.popleft()
                if not self._is_dup(msg, replayed):
                    yield msg
            if kind == "exit":
                code = item[1]
                if code:
                    raise ProcessError(f"Command failed with exit code {code}",
                                       exit_code=code, stderr="Session host reported the CLI exit")
                return
            if self._closed:
                return
            raise CLIConnectionError("Session host connection lost")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        was_ready, self._ready = self._ready, False
        w = self._writer
        try:
            if w is not None and not w.is_closing():
                if self._mode == "detach":
                    w.write(shp.encode({"t": "detach"}))
                    await asyncio.wait_for(w.drain(), 2.0)
                else:
                    w.write(shp.encode({"t": "shutdown"}))
                    await asyncio.wait_for(w.drain(), 2.0)
                    # The host answers "bye" and closes once the CLI is gone.
                    if was_ready and self._pump_task is not None:
                        try:
                            await asyncio.wait_for(asyncio.shield(self._pump_task), 15.0)
                        except Exception:
                            pass
        except Exception:
            pass
        finally:
            if self._pump_task is not None and not self._pump_task.done():
                self._pump_task.cancel()
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass
            if self._inbox is not None:
                self._inbox.put_nowait(("eof",))

    def is_ready(self) -> bool:
        return self._ready and not self._closed

    # -- host-only operations ---------------------------------------------------

    def set_detach(self) -> None:
        """The next ``close()`` detaches (the CLI keeps running) instead of
        shutting the host down."""
        self._mode = "detach"

    def release_replay(self, skip_uuids=()) -> None:
        self._skip = {u for u in (skip_uuids or ()) if u}
        if self._inbox is not None:
            self._inbox.put_nowait(("release",))

    async def rename(self, new_sid: str) -> None:
        """The session adopted the CLI's id: the host renames its record."""
        if not new_sid or new_sid == self._sid:
            return
        self._sid = new_sid
        if self._record is not None:
            self._record["sid"] = new_sid
        try:
            await self._send({"t": "rename", "sid": new_sid})
        except CLIConnectionError:
            pass

    def abort_connection(self) -> None:
        """Drop the socket with no frame (what a ``kill -9`` of the engine
        does).  Tests use it to simulate an engine crash."""
        w = self._writer
        self._closed = True
        self._ready = False
        if w is not None:
            try:
                w.transport.abort()
            except Exception:
                pass


class HostedSDKClient(ClaudeSDKClient):
    """``ClaudeSDKClient`` whose CLI runs in a Session Host.  Only
    ``connect()`` differs (copied from SDK 0.0.25 ``client.py`` with the
    transport swapped); everything after it is the SDK's own code."""

    def __init__(self, options: Optional[ClaudeCodeOptions] = None, *, host_sid: str,
                 attach_record: Optional[dict] = None, cli_path: Optional[str] = None,
                 hosts_dir=None, logs_dir=None):
        super().__init__(options)
        self._host_sid = host_sid
        self._attach_record = attach_record
        self._cli_path = cli_path
        self._hosts_dir = hosts_dir
        self._logs_dir = logs_dir

    async def connect(self, prompt: str | AsyncIterable[dict[str, Any]] | None = None) -> None:
        from claude_code_sdk._internal.query import Query

        if self.options.can_use_tool:
            if isinstance(prompt, str):
                raise ValueError("can_use_tool callback requires streaming mode.")
            if self.options.permission_prompt_tool_name:
                raise ValueError("can_use_tool callback cannot be used with "
                                 "permission_prompt_tool_name.")
            options = replace(self.options, permission_prompt_tool_name="stdio")
        else:
            options = self.options

        self._transport = HostTransport(
            self._host_sid,
            launch_spec=None if self._attach_record else
            launch_spec_from_options(options, self._cli_path),
            attach_record=self._attach_record,
            hosts_dir=self._hosts_dir, logs_dir=self._logs_dir,
        )
        await self._transport.connect()

        sdk_mcp_servers = {}
        if self.options.mcp_servers and isinstance(self.options.mcp_servers, dict):
            for name, config in self.options.mcp_servers.items():
                if isinstance(config, dict) and config.get("type") == "sdk":
                    sdk_mcp_servers[name] = config["instance"]

        self._query = Query(
            transport=self._transport,
            is_streaming_mode=True,
            can_use_tool=self.options.can_use_tool,
            hooks=self._convert_hooks_to_internal_format(self.options.hooks)
            if self.options.hooks else None,
            sdk_mcp_servers=sdk_mcp_servers,
        )
        await self._query.start()
        await self._query.initialize()
        if prompt is not None and isinstance(prompt, AsyncIterable) and self._query._tg:
            self._query._tg.start_soon(self._query.stream_input, prompt)
