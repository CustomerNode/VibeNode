"""The SDK client is disconnected in the same task that connected it.

Found 2026-10-18: the Session Engine sat at ~1 core of CPU with every session
idle.  The Claude SDK's ``connect()`` enters an anyio task group in the
calling task, and ``disconnect()`` exits it.  ``_close_session`` cancels the
session's driving task (where connect ran) and then disconnects from its own
task, so anyio refused the exit ("Attempted to exit cancel scope in a
different task than it was entered in", in the daemon log).  The scope was
left cancelled but never exited, and anyio re-delivered its cancellation
on every pass of the event loop forever: six such scopes after two days kept
the loop at 93% of a core.

``ClaudeAgentSDK.connect`` now runs the client in a dedicated owner task that
also performs the disconnect, so the task group is always exited by the task
that entered it, whoever calls ``disconnect``.
"""

from __future__ import annotations

import asyncio

import anyio
import pytest

from daemon.backends.claude import ClaudeAgentSDK


class FakeSDKClient:
    """Mirrors ClaudeSDKClient/Query: connect enters a task group in the
    calling task with a reader task inside; disconnect cancels and exits it."""

    def __init__(self):
        self.reader_finished = False
        self.disconnected = False

    async def connect(self):
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        self._tg.start_soon(self._read_messages)

    async def _read_messages(self):
        try:
            await anyio.sleep_forever()
        finally:
            self.reader_finished = True

    async def disconnect(self):
        self._tg.cancel_scope.cancel()
        await self._tg.__aexit__(None, None, None)
        self.disconnected = True


def _spinning_cancel_scopes(loop) -> int:
    return sum(1 for h in list(loop._ready)
               if getattr(getattr(h, "_callback", None), "__name__", "") == "_deliver_cancellation")


async def _close_like_the_daemon(sdk, client):
    """_drive_session connects inside the session task; _close_session
    cancels that task, then disconnects from a different task."""
    connected = asyncio.Event()

    async def drive():
        await sdk.connect(client)
        connected.set()
        await asyncio.sleep(3600)  # the session's turn loop

    driving = asyncio.ensure_future(drive())
    await connected.wait()
    driving.cancel()                      # _close_session step 2
    await asyncio.gather(driving, return_exceptions=True)
    await sdk.disconnect(client)          # _close_session step 4, another task
    for _ in range(5):
        await asyncio.sleep(0)            # let any re-delivery show up
    return _spinning_cancel_scopes(asyncio.get_running_loop())


def test_disconnect_from_another_task_exits_the_task_group():
    sdk = ClaudeAgentSDK()
    client = FakeSDKClient()
    spinning = asyncio.run(_close_like_the_daemon(sdk, client))
    assert client.disconnected
    assert client.reader_finished
    assert spinning == 0, "a cancel scope is left re-delivering cancellation every loop pass"


def test_disconnect_in_the_connecting_task_still_works():
    sdk = ClaudeAgentSDK()
    client = FakeSDKClient()

    async def run():
        await sdk.connect(client)
        await sdk.disconnect(client)
    asyncio.run(run())
    assert client.disconnected and client.reader_finished


def test_a_failed_connect_raises_to_the_caller():
    sdk = ClaudeAgentSDK()

    class Broken(FakeSDKClient):
        async def connect(self):
            raise ConnectionError("CLI did not start")

    with pytest.raises(ConnectionError):
        asyncio.run(sdk.connect(Broken()))


def test_a_disconnect_error_still_reaches_the_caller():
    """_close_session logs a failed disconnect; the error must not vanish."""
    sdk = ClaudeAgentSDK()

    class BadClose(FakeSDKClient):
        async def disconnect(self):
            await super().disconnect()
            raise OSError("pipe already closed")

    async def run():
        c = BadClose()
        await sdk.connect(c)
        await sdk.disconnect(c)
    with pytest.raises(OSError):
        asyncio.run(run())
