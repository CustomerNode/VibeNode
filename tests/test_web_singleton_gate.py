"""Regression tests for the web-server singleton gate.

run.py used to treat a held singleton mutex as proof the mutex was STALE:

    if not acquire_web_singleton():
        # Mutex held but we just killed the ports -- stale mutex. Proceed.
        print("  Stale singleton detected. Starting anyway.")

When the mutex was actually held by a LIVE VibeNode (``_kill_port`` missed it
because it was still booting and not yet listening, or the kill was refused),
that comment was wrong and the second server started anyway.  On Windows
SO_REUSEADDR lets a second live process bind a port that is already in use --
it does not merely reclaim TIME_WAIT the way it does on Unix -- so both servers
stayed up on 5050, and both opened an IPC client to the single daemon.  They
fought over it on a 2-second reconnect cycle and the user got a permanent
"VibeNode Engine Stopped" overlay that Restart could not clear.

The gate now distinguishes the two cases instead of assuming one, using a
connect probe as the liveness oracle.  These tests pin that oracle and the
retry semantics.  run.py itself is deliberately NOT imported here -- importing
it kills ports, spawns the daemon and binds 5050 -- which is precisely why the
logic was moved into app/singleton.py.
"""

import socket

import pytest

from app import singleton


# ---------------------------------------------------------------------------
# port_has_listener: the liveness oracle
# ---------------------------------------------------------------------------

def test_port_has_listener_true_while_something_listens():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert singleton.port_has_listener(port) is True
    finally:
        srv.close()


def test_port_has_listener_false_on_a_free_port():
    """A closed port must read as free, or the gate would refuse to ever boot."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()   # nothing is listening now

    assert singleton.port_has_listener(port) is False


def test_port_has_listener_false_after_the_listener_dies():
    """The exact transition the gate depends on.

    A killed incumbent must read as gone even though its mutex handle may not
    be released yet -- that gap is what separates 'stale mutex' from 'live
    incumbent'.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    assert singleton.port_has_listener(port) is True

    srv.close()
    assert singleton.port_has_listener(port) is False


def test_port_has_listener_uses_connect_not_bind():
    """A bind probe would be wrong, and this pins why.

    A listener that set SO_REUSEADDR makes a competing bind fail while it is
    very much alive; on Unix, leftover TIME_WAIT sockets do the same with
    nothing alive behind them.  Only a successful connect proves someone is
    serving.  Here: SO_REUSEADDR is set, a bind probe would report 'free', and
    the oracle must still say 'occupied'.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert singleton.port_has_listener(port) is True
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# wait_for_web_singleton: retry, then hand the decision back
# ---------------------------------------------------------------------------

def test_wait_for_web_singleton_returns_immediately_when_free(monkeypatch):
    calls = []
    monkeypatch.setattr(singleton, "acquire_web_singleton",
                        lambda: (calls.append(1), True)[1])

    assert singleton.wait_for_web_singleton() is True
    assert len(calls) == 1, "must not sleep when the mutex is free"


def test_wait_for_web_singleton_retries_a_slow_release(monkeypatch):
    """Case (a): the incumbent was just killed and the kernel is catching up.

    A single failed acquisition proves nothing, so the gate must retry rather
    than conclude anything.
    """
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        return attempts["n"] >= 3

    monkeypatch.setattr(singleton, "acquire_web_singleton", flaky)
    monkeypatch.setattr(singleton.time, "sleep", lambda _: None)

    assert singleton.wait_for_web_singleton(attempts=10, delay=0) is True
    assert attempts["n"] == 3


def test_wait_for_web_singleton_gives_up_when_genuinely_held(monkeypatch):
    """Case (b): a live incumbent.

    Returning False is the whole point -- it hands the decision to
    port_has_listener instead of starting a second server on an assumption.
    """
    monkeypatch.setattr(singleton, "acquire_web_singleton", lambda: False)
    monkeypatch.setattr(singleton.time, "sleep", lambda _: None)

    assert singleton.wait_for_web_singleton(attempts=5, delay=0) is False


def test_wait_for_web_singleton_bounds_its_retries(monkeypatch):
    """The retry window must terminate -- boot cannot hang here."""
    calls = {"n": 0}

    def never():
        calls["n"] += 1
        return False

    monkeypatch.setattr(singleton, "acquire_web_singleton", never)
    monkeypatch.setattr(singleton.time, "sleep", lambda _: None)

    singleton.wait_for_web_singleton(attempts=7, delay=0)
    assert calls["n"] == 8   # one upfront + 7 retries


# ---------------------------------------------------------------------------
# The gate wiring in run.py, checked as source (importing run.py has
# process-wide side effects: it kills ports, spawns the daemon, binds 5050).
# ---------------------------------------------------------------------------

def _run_py_source():
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "run.py")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_run_py_no_longer_starts_a_second_server_on_a_held_mutex():
    """The unconditional 'start anyway' must stay gone.

    Starting anyway is still correct for a genuinely abandoned mutex, but only
    after port_has_listener has ruled out a live incumbent.
    """
    src = _run_py_source()
    assert "if not wait_for_web_singleton():" in src
    assert "port_has_listener(_WEB_PORT)" in src
    # The old unconditional message no longer exists; the surviving one is
    # qualified by the listener check.
    assert "Stale singleton detected. Starting anyway." not in src


def test_run_py_binds_the_web_port_exclusively_on_windows():
    """Socket-level backstop for any path that bypasses the mutex.

    The daemon has always bound 5051 with SO_EXCLUSIVEADDRUSE, which is exactly
    why a second daemon can never exist.  The web port needs the equivalent so
    a duplicate bind fails loudly instead of silently sharing the port.
    """
    src = _run_py_source()
    assert "BaseWSGIServer.allow_reuse_address = 0" in src
    assert 'sys.platform == "win32"' in src


# ---------------------------------------------------------------------------
# reclaim_port: taking the web port back from the reviver at bind time
# ---------------------------------------------------------------------------
#
# The reviver parks a "Start VibeNode" page on the web port while the real
# server is down.  session_manager.py asks it to yield at LAUNCH, but a cold
# boot takes the better part of a minute to reach the bind, and the reviver's
# loop re-takes the free port in that window -- so the yield is stale by the
# time it matters.
#
# That race was invisible while the bind used SO_REUSEADDR: the web server just
# co-bound alongside the reviver.  Once the bind became exclusive, losing the
# race became fatal -- VibeNode exited and left the user tapping Start and
# landing back on the Start page.  Observed in the wild on 2026-08-31:
#
#   reviver.log  [14:40:00] serving Start page on 127.0.0.1:5050
#   _server.log  Port 5050 is in use by another program.
#
# These tests pin the re-ask.

def test_reclaim_port_returns_immediately_when_port_is_free(monkeypatch):
    """The common case must cost nothing -- no yield request, no sleeping."""
    monkeypatch.setattr(singleton, "port_has_listener", lambda p, **kw: False)
    monkeypatch.setattr(singleton.time, "sleep",
                        lambda _: pytest.fail("must not sleep on a free port"))

    assert singleton.reclaim_port(5050) is True


def test_reclaim_port_waits_for_the_holder_to_let_go(monkeypatch):
    """Occupied, then free after the yield lands."""
    states = iter([True, False, False, False])
    monkeypatch.setattr(singleton, "port_has_listener",
                        lambda p, **kw: next(states, False))
    monkeypatch.setattr(singleton.time, "sleep", lambda _: None)

    import urllib.request
    posted = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: posted.append(req) or None)

    assert singleton.reclaim_port(5050, timeout=5) is True
    assert posted, "must POST /yield to the reviver control port"
    assert posted[0].full_url.endswith("/yield")
    assert "127.0.0.1" in posted[0].full_url


def test_reclaim_port_gives_up_on_a_holder_that_will_not_yield(monkeypatch):
    """A non-reviver squatter must not hang boot forever.

    Returning False lets run.py print something specific instead of letting
    werkzeug die with a generic 'identify and stop that program' message.
    """
    monkeypatch.setattr(singleton, "port_has_listener", lambda p, **kw: True)
    monkeypatch.setattr(singleton.time, "sleep", lambda _: None)

    import urllib.request

    def refuse(req, timeout=None):
        raise OSError("connection refused")   # nothing on the control port

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    assert singleton.reclaim_port(5050, timeout=0.3) is False


def test_reclaim_port_survives_a_dead_control_port(monkeypatch):
    """No reviver running is normal (Mobile Command off) -- not an error.

    The port can still free up on its own if a dying process is closing it.
    """
    states = iter([True, True, False])
    monkeypatch.setattr(singleton, "port_has_listener",
                        lambda p, **kw: next(states, False))
    monkeypatch.setattr(singleton.time, "sleep", lambda _: None)

    import urllib.request
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(OSError("refused")))

    assert singleton.reclaim_port(5050, timeout=5) is True


def test_run_py_reclaims_the_port_before_binding():
    """The reclaim must sit between the exclusive-bind setup and socketio.run.

    Ordering is the whole point: asking earlier (as session_manager.py does) is
    what left the yield stale.
    """
    src = _run_py_source()
    assert "reclaim_port(_port)" in src
    reclaim_at = src.index("reclaim_port(_port)")
    bind_at = src.index("socketio.run(app")
    assert reclaim_at < bind_at, "reclaim must happen before the bind"
