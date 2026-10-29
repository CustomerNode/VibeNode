"""VibeNode must know by itself when an approval page has been answered.

Reported 2026-10-12: "when my customernode sessions request approval for a
release, the tab name on the browser flashes. there's some edge cases where it
continues flashing after i approve, that should be made impossible".

A script that needs a person serves an approval page on 127.0.0.1 and pings
``POST /api/notify/approval``.  Nothing ever told VibeNode the page had been
answered, so a background tab flashed until that one tab was looked at.

``app/approval_watch.py`` is the server half of the fix: it fetches the page
the script announced and counts the approval as open only while that same page
is still being served.  These tests run it against real pages on real local
ports that behave the way such a page does:

* it is static and shows the announced title;
* an answer stops its server (the port stays bound until the process exits,
  then refuses), or puts another page on the same port;
* a ping can arrive after the page was already answered;
* a script can leave an earlier page up after a later one was answered.

The watch is stepped by hand with a fake clock, so no test waits on a timer.
The browser half is tested in ``tests/test_notify_flash.py``.
"""

from __future__ import annotations

import html
import json
import logging
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import approval_watch as aw

TITLE = "⛔ PRODUCTION RELEASE — 4.2"
FIRST_TITLE = "Release 4.2"                    # the page a pipeline opens before its checks run
MESSAGE = "Click Approve or Cancel in your browser to continue."
SOURCE = "pipeline"
PROGRESS_PAGE = ("<!doctype html><title>4.2 · Release progress</title>"
                 "<div>Production release</div><h1>4.2</h1><div id=steps></div>")


def card_page(title: str, token: str = "5f1c9a0be2d44c17") -> str:
    """An approval page: the title in an <h1>, escaped, and the links that
    answer it. Each serve has a token of its own, so no two are identical."""
    return ("<!doctype html><title>Production Release</title>"
            f"<h1>{html.escape(title)}</h1>"
            f'<a href="/approve?token={token}">Release to Production</a>'
            f'<a href="/cancel?token={token}">Cancel</a>')


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _serve(self):
        card = self.server.card
        card.requests.append((self.command, self.path))
        body = card.page.encode("utf-8")
        self.send_response(card.status)
        if card.location:
            self.send_header("Location", card.location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body) + (25 if card.cut_short else 0)))
        self.end_headers()
        if self.command == "HEAD":
            return
        if not card.trickle:
            self.wfile.write(body)
            return
        for byte in body[:60]:                 # one byte every 50ms: 3 seconds
            self.wfile.write(bytes([byte]))
            self.wfile.flush()
            time.sleep(0.05)

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _serve


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass     # a check that gave up on a silent page has hung up: expected


class Card:
    """An approval page on a real local port."""

    def __init__(self, page: str):
        self.page = page
        self.status = 200
        self.location = ""
        self.cut_short = False      # the connection drops before the announced length
        self.trickle = False        # the body arrives a byte at a time
        self.requests: list[tuple[str, str]] = []
        self.srv = _Server(("127.0.0.1", 0), _Handler)
        self.srv.card = self
        self.port = self.srv.server_address[1]
        _CLOSED_PORTS.discard(self.port)        # the OS may hand out a port a closed card had
        self.url = f"http://127.0.0.1:{self.port}/"
        self._serving = False
        self.serve()

    def serve(self):
        """(Re)start answering requests."""
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()
        self._serving = True

    def go_silent(self):
        """Stop answering but keep the port: what a script's server does
        between the tap and its process exiting, and what a hung page does."""
        if self._serving:
            self.srv.shutdown()
            self._serving = False

    def close(self):
        """The script has exited: nothing listens on the port."""
        self.go_silent()
        self.srv.server_close()
        _CLOSED_PORTS.add(self.port)


# Ports whose Card has been closed in the current test. On Windows a connect
# to a closed loopback port is retried by the OS and takes ~2s to be refused
# (REFUSAL_WAIT_S in app/approval_watch.py); nine tests here close a card and
# paid that for real on every check that followed. ``_fast_refusal`` below
# answers those ports with ConnectionRefusedError at once, which is exactly
# what the OS says, only sooner. One test (``test_what_one_check_can_tell``)
# opts out through ``real_refusal`` so the real refused-port path stays covered.
_CLOSED_PORTS: set[int] = set()
_FAKE_REFUSAL = [True]


class Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def _short_timeout(monkeypatch):
    # A check of a silent page waits this long for real.
    monkeypatch.setattr(aw, "PROBE_TIMEOUT_S", 0.3)


@pytest.fixture(autouse=True)
def _fast_refusal(monkeypatch):
    """A closed card's port is refused at once instead of after Windows' ~2s
    connect retry (see ``_CLOSED_PORTS``). Every other address connects for real."""
    _CLOSED_PORTS.clear()
    _FAKE_REFUSAL[0] = True
    real = socket.create_connection

    def create_connection(address, timeout=None, *args, **kwargs):
        if _FAKE_REFUSAL[0] and address[1] in _CLOSED_PORTS:
            raise ConnectionRefusedError(111, "closed card")
        return real(address, timeout, *args, **kwargs)

    monkeypatch.setattr(aw.socket, "create_connection", create_connection)


@pytest.fixture
def real_refusal():
    """Connect to closed ports for real: the one place the OS refusal, and
    REFUSAL_WAIT_S on Windows, are exercised."""
    _FAKE_REFUSAL[0] = False


@pytest.fixture
def cards():
    made = []

    def make(page=None, title=TITLE):
        card = Card(card_page(title) if page is None else page)
        made.append(card)
        return card

    yield make
    for card in made:
        try:
            card.close()
        except Exception:
            pass


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def events():
    return []


@pytest.fixture
def watch(clock, events):
    w = aw.ApprovalWatch(emit=lambda event, data: events.append((clock.now, event, data)),
                         clock=clock, threaded=False)
    yield w
    w.stop()


def listed(events):
    """The ids in every snapshot that was broadcast, in order."""
    return [[c["id"] for c in data["open"]] for _at, _event, data in events]


def check(watch, clock, after=aw.PROBE_FAST_S):
    clock.tick(after)
    watch.step()


# ── open ────────────────────────────────────────────────────────────────────

def test_an_announced_page_is_open_once_it_shows_its_title(cards, watch, events):
    card = cards()
    rec = watch.register(TITLE, MESSAGE, SOURCE, card.url)
    assert rec["title"] == TITLE and rec["id"]
    assert watch.open_now() == [], "announced is not open: the page has not been seen yet"

    watch.step()
    assert watch.open_now() == [rec]
    at, event, data = events[-1]
    assert event == "approvals_open"
    assert data == {"open": [rec], "ttl_ms": int(aw.CLIENT_TTL_S * 1000)}
    assert data["open"][0]["message"] == MESSAGE and data["open"][0]["source"] == SOURCE

    assert "url" not in data["open"][0], "every browser gets the broadcast; none needs the address"
    assert watch.open_now(with_urls=True) == [dict(rec, url=card.url)]


def test_it_stays_open_for_as_long_as_the_page_does(cards, watch, clock, events):
    card = cards()
    rec = watch.register(TITLE, MESSAGE, SOURCE, card.url)
    for _ in range(40):                       # 2 hours
        check(watch, clock, 180)
        assert watch.open_now() == [rec]
    assert all(ids == [rec["id"]] for ids in listed(events))


@pytest.mark.parametrize("title", [
    "Release 4.2",
    "⛔ PROD GALLERY REFRESH — 12 templates",
    "Service notice to 5 users & <admins>",      # escaped in the page
    "Don't ship \"this\" yet",
    "  Release   4.2  ",                         # whitespace is not part of a name
])
def test_the_title_is_recognised_however_the_page_escapes_it(title, cards, watch):
    card = cards(title=title)
    assert watch.register(title, MESSAGE, SOURCE, card.url)
    watch.step()
    assert len(watch.open_now()) == 1


# ── answered ────────────────────────────────────────────────────────────────

def test_an_answer_that_closes_the_port_ends_it(cards, watch, clock, events):
    card = cards()
    rec = watch.register(TITLE, MESSAGE, SOURCE, card.url)
    watch.step()
    card.close()                               # tapped; the script has exited
    check(watch, clock)
    assert watch.open_now() == []
    assert listed(events) == [[rec["id"]], []], "exactly one snapshot says it is over"


def test_an_answer_that_puts_another_page_on_the_port_ends_it(cards, watch, clock, events):
    """After the tap the same port can go on serving something else for many
    minutes: a live progress page. The port is alive; the approval is not."""
    card = cards()
    rec = watch.register(TITLE, MESSAGE, SOURCE, card.url)
    watch.step()
    card.page = PROGRESS_PAGE
    check(watch, clock)
    assert watch.open_now() == []
    assert listed(events) == [[rec["id"]], []]

    before = len(card.requests)
    for _ in range(20):
        check(watch, clock, aw.PROBE_SLOW_S)
    assert len(card.requests) == before, "an answered page is not looked at again"
    assert listed(events)[-1] == []


def test_a_ping_that_arrives_after_the_answer_is_never_open(cards, watch, clock, events):
    """A script can ping from a thread it starts after opening the browser, and
    opening the browser can take seconds. A quick tap gets there first: by the
    time VibeNode looks, the port already serves the page that replaced the
    approval. That page must not be taken for the approval and watched for as
    long as it stays up."""
    card = cards(page=PROGRESS_PAGE)
    assert watch.register(TITLE, MESSAGE, SOURCE, card.url)
    for _ in range(10):
        check(watch, clock)
    assert watch.open_now() == [] and events == []
    assert card.requests == [("GET", "/")], "looked once, saw it was not the page, let it go"


def test_a_page_that_goes_silent_is_not_open_once_it_has_not_been_seen_for_the_ttl(
        cards, watch, clock, events):
    """Between the tap and its process exiting, a script's port can be bound
    with nobody answering on it. A check that cannot tell must never count as
    "still open": the approval runs out by itself."""
    card = cards()
    rec = watch.register(TITLE, MESSAGE, SOURCE, card.url)
    watch.step()
    card.go_silent()

    check(watch, clock)                        # cannot tell
    assert watch.open_now() == [rec], "one missed check is not an answer"
    clock.tick(aw.VERIFY_TTL_S)
    assert watch.open_now() == [], "not seen unchanged for the ttl: not open"
    watch.step()
    assert listed(events)[-1] == []

    card.close()                               # the process exits
    n = len(events)
    for _ in range(5):
        check(watch, clock, aw.PROBE_SLOW_S)
    assert watch.open_now() == [] and len(events) == n


def test_a_page_that_was_only_slow_is_open_again_under_the_same_id(cards, watch, clock, events):
    """A machine under heavy load can stall a page for a while. The approval
    is still waiting, and the user must not lose it."""
    card = cards()
    rec = watch.register(TITLE, MESSAGE, SOURCE, card.url)
    watch.step()
    card.go_silent()
    check(watch, clock)
    clock.tick(aw.VERIFY_TTL_S)
    watch.step()
    assert watch.open_now() == []

    card.serve()
    for _ in range(4):
        check(watch, clock, aw.PROBE_SLOW_S)
    assert watch.open_now() == [rec]
    assert listed(events)[0] == [rec["id"]] and [] in listed(events) and listed(events)[-1] == [rec["id"]]


def test_a_new_page_that_never_answers_is_given_up_on(cards, watch, clock, events):
    card = cards()
    card.go_silent()
    assert watch.register(TITLE, MESSAGE, SOURCE, card.url)
    watch.step()
    clock.tick(aw.CONFIRM_WINDOW_S + 1)
    watch.step()
    card.serve()
    checked = []
    watch._fetch = lambda url, timeout: checked.append(url) or aw.fetch_page(url, timeout)
    for _ in range(5):
        check(watch, clock, aw.PROBE_SLOW_S)
    assert watch.open_now() == [] and events == []
    assert checked == [], "it is no longer watched"


def test_no_approval_page_waits_longer_than_a_day(cards, watch, clock):
    card = cards()
    watch.register(TITLE, MESSAGE, SOURCE, card.url)
    watch.step()
    clock.tick(aw.MAX_AGE_S + 1)
    watch.step()
    assert watch.open_now() == []


def test_the_script_can_say_it_is_over(cards, watch, clock, events):
    one, two = cards(), cards(title="Release 4.3")
    a = watch.register(TITLE, MESSAGE, SOURCE, one.url)
    b = watch.register("Release 4.3", MESSAGE, SOURCE, two.url)
    watch.step()
    assert listed(events)[-1] == [a["id"], b["id"]]

    assert watch.resolve(approval_id=a["id"]) == 1
    assert listed(events)[-1] == [b["id"]], "told at once, not at the next check"
    assert watch.resolve(url=two.url) == 1
    assert listed(events)[-1] == []
    assert watch.resolve(approval_id=a["id"]) == 0 and watch.resolve(url=two.url) == 0
    assert watch.resolve() == 0 and watch.resolve(approval_id="nope", url="not a url") == 0

    before = len(one.requests) + len(two.requests)
    check(watch, clock, aw.PROBE_SLOW_S)
    assert len(one.requests) + len(two.requests) == before


# ── a page the script left behind ───────────────────────────────────────────

def _first_page_then_release_page(cards, watch):
    """A pipeline's two pages for one release. The first is opened before its
    checks run and stays up until it is answered; the second is the release's
    own, opened when the checks have passed."""
    first = cards(title=FIRST_TITLE)
    release = cards(title=TITLE)
    a = watch.register(FIRST_TITLE, MESSAGE, SOURCE, first.url)
    watch.step()
    b = watch.register(TITLE, MESSAGE, SOURCE, release.url)
    watch.step()
    assert [c["id"] for c in watch.open_now()] == [a["id"], b["id"]]
    return first, release, a, b


@pytest.mark.parametrize("answer", ["release", "cancel"])
def test_answering_the_later_page_ends_the_earlier_one_the_script_left_up(
        answer, cards, watch, clock, events):
    """Answer the release's own page, and the first page is still being
    served, unchanged, and no longer of any use. The tab must not go on
    flashing for it after the user has answered."""
    first, release, a, b = _first_page_then_release_page(cards, watch)
    if answer == "release":
        release.page = PROGRESS_PAGE
    else:
        release.close()
    check(watch, clock)
    assert watch.open_now() == []
    assert listed(events)[-1] == [], "both are over in the same snapshot"

    assert aw.fetch_page(first.url, 1.0)[0] == aw.PAGE, "the first page really is still up"
    asked = len(first.requests)
    for _ in range(10):
        check(watch, clock, aw.PROBE_SLOW_S)
    assert len(first.requests) == asked and watch.open_now() == []


def test_answering_the_earlier_page_leaves_the_later_one_open(cards, watch, clock, events):
    """"Ask me again when it is ready" on the first page, with the release's
    own page already waiting: that one still needs its answer."""
    first, release, a, b = _first_page_then_release_page(cards, watch)
    first.close()
    check(watch, clock)
    assert [c["id"] for c in watch.open_now()] == [b["id"]]
    assert listed(events)[-1] == [b["id"]]


def test_a_later_page_answered_before_its_ping_arrived_still_ends_the_earlier_one(
        cards, watch, clock, events):
    """A quick tap beats the ping. The page that was never seen open was
    answered all the same."""
    first = cards(title=FIRST_TITLE)
    a = watch.register(FIRST_TITLE, MESSAGE, SOURCE, first.url)
    watch.step()
    release = cards(page=PROGRESS_PAGE)
    watch.register(TITLE, MESSAGE, SOURCE, release.url)
    check(watch, clock)
    assert watch.open_now() == [] and listed(events) == [[a["id"]], []]


def test_a_script_saying_its_later_page_is_over_ends_the_earlier_one(cards, watch, events):
    first, release, a, b = _first_page_then_release_page(cards, watch)
    assert watch.resolve(approval_id=b["id"]) == 1
    assert watch.open_now() == [] and listed(events)[-1] == []


@pytest.mark.parametrize("first_source,later_source", [
    (SOURCE, "othertool"),                       # another script's page
    ("", ""),                                    # nobody said whose they are
])
def test_a_page_going_away_ends_no_one_elses(first_source, later_source, cards, watch, clock):
    first, later = cards(title=FIRST_TITLE), cards(title=TITLE)
    a = watch.register(FIRST_TITLE, MESSAGE, first_source, first.url)
    watch.step()
    watch.register(TITLE, MESSAGE, later_source, later.url)
    watch.step()
    later.close()
    check(watch, clock)
    assert [c["id"] for c in watch.open_now()] == [a["id"]]


def test_a_later_page_that_merely_never_answered_ends_nothing(cards, watch, clock):
    """Only a page that went away counts. One that could never be reached
    says nothing about what the user did."""
    first = cards(title=FIRST_TITLE)
    a = watch.register(FIRST_TITLE, MESSAGE, SOURCE, first.url)
    watch.step()
    silent = cards()
    silent.go_silent()
    watch.register(TITLE, MESSAGE, SOURCE, silent.url)
    for _ in range(3):
        check(watch, clock, aw.CONFIRM_WINDOW_S / 2)
    assert [c["id"] for c in watch.open_now()] == [a["id"]]
    assert aw.fetch_page(silent.url, 0.2)[0] == aw.UNKNOWN


# ── the heartbeat ───────────────────────────────────────────────────────────

def test_an_open_approval_is_repeated_and_nothing_is_sent_when_none_is(cards, watch, clock, events):
    """The browser acts on a snapshot for CLIENT_TTL_S only, so an open
    approval has to be said again well inside that."""
    watch.step()
    assert events == [], "nothing tracked, nothing sent"

    card = cards()
    rec = watch.register(TITLE, MESSAGE, SOURCE, card.url)
    for _ in range(60):                        # 30 seconds of checks
        check(watch, clock)
    times = [at for at, _e, _d in events]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert listed(events) == [[rec["id"]]] * len(events)
    assert 8 <= len(events) <= 12 and max(gaps) <= aw.HEARTBEAT_S + aw.PROBE_FAST_S
    assert 2 * max(gaps) < aw.CLIENT_TTL_S, "a browser survives one lost heartbeat"

    card.close()
    check(watch, clock)
    n = len(events)
    assert listed(events)[-1] == []
    for _ in range(20):
        check(watch, clock, aw.PROBE_SLOW_S)
    assert len(events) == n, "the empty list is said once"


def test_the_heartbeat_does_not_wait_for_a_page_to_be_due(cards, watch, clock, events):
    card = cards()
    watch.register(TITLE, MESSAGE, SOURCE, card.url)
    watch.step()
    clock.tick(aw.HEARTBEAT_S - 0.1)
    watch.step()                               # the page is checked; no heartbeat is due yet
    assert len(events) == 1
    clock.tick(0.2)
    watch.step()                               # the heartbeat is due; the page is not
    assert len(events) == 2 and len(card.requests) == 2


def test_the_thread_keeps_the_heartbeat_on_time_whatever_the_pages_are_doing(cards, watch, clock, events):
    """The thread's own loop, on the fake clock: one pass, then sleep for as
    long as it asks. An old page is checked every PROBE_SLOW_S, which is as
    long as a heartbeat; a loop that slept until the next check sent every
    other heartbeat late."""
    assert watch._pass() is None, "nothing watched: the thread ends"
    card = cards()
    rec = watch.register(TITLE, MESSAGE, SOURCE, card.url)
    clock.tick(aw.FAST_FOR_S + 1)              # an approval that has been waiting a while
    end = clock.now + 600
    while clock.now < end:
        nap = watch._pass()
        assert 0.05 <= nap <= aw.TICK_S
        clock.tick(nap + 0.013)                # a thread never wakes exactly on time
    times = [at for at, _e, data in events if data["open"]]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert listed(events) == [[rec["id"]]] * len(events)
    assert max(gaps) <= aw.HEARTBEAT_S + aw.TICK_S + 0.05
    assert 2 * max(gaps) < aw.CLIENT_TTL_S, "a browser survives one lost heartbeat"
    assert len(card.requests) <= 600 / aw.PROBE_SLOW_S + 2, "and the page is not checked more often for it"

    card.close()
    while watch._pass() is not None:
        clock.tick(aw.TICK_S)
    assert listed(events)[-1] == []


def test_silent_pages_cannot_starve_the_one_that_answers(cards, watch, clock, events, monkeypatch):
    """Each check of a silent page costs a full timeout, and every page is
    checked on one thread. Four pages going silent at once must not keep the
    one that answers from being seen, and said, in time: its tab would stop
    flashing for an approval still open."""
    monkeypatch.setattr(aw, "PROBE_TIMEOUT_S", 2.5)     # the real one; the silent checks below are simulated
    good = cards()
    silent = [cards(title=f"Release 9.{n}") for n in (1, 2, 3, 4)]
    urls = {c.url for c in silent}
    rec = watch.register(TITLE, MESSAGE, "", good.url)
    for n, card in zip((1, 2, 3, 4), silent):
        watch.register(f"Release 9.{n}", MESSAGE, "", card.url)
    watch.step()
    assert len(watch.open_now()) == 5

    def fetch(url, timeout):
        if url in urls:
            clock.tick(2.5)                    # the real cost of a check that times out
            return aw.UNKNOWN, b""
        return aw.fetch_page(url, timeout)

    watch._fetch = fetch
    start = clock.now
    while clock.now - start < 300:
        clock.tick(watch._pass())              # the thread's own loop
    mine = [at for at, _e, data in events if rec["id"] in [c["id"] for c in data["open"]]]
    gaps = [b - a for a, b in zip(mine, mine[1:])]
    assert all(rec["id"] in ids for ids in listed(events)), "the page that answers never drops out"
    assert listed(events)[-1] == [rec["id"]], "the silent ones ran out"
    assert max(gaps) <= aw.HEARTBEAT_S + 2.5 + 0.2, "one page going silent delays one heartbeat, once"
    late = [g for g in gaps[20:] if g > aw.HEARTBEAT_S + 0.2]
    assert late == [], "after that, a heartbeat due during a slow check is sent before it, not after"


def test_two_snapshots_never_leave_in_the_wrong_order(cards, clock):
    """A check ending and a script resolving at the same moment each publish.
    If the older snapshot could overtake the newer one, the last thing the
    browsers heard would be that an answered approval is open."""
    emitted, in_emit, release = [], threading.Event(), threading.Event()

    def emit(event, data):
        ids = [c["id"] for c in data["open"]]
        if ids and not in_emit.is_set():
            in_emit.set()
            release.wait(5)                    # the first broadcast is slow to leave
        emitted.append(ids)

    w = aw.ApprovalWatch(emit=emit, clock=clock, threaded=False)
    card = cards()
    rec = w.register(TITLE, MESSAGE, SOURCE, card.url)
    first = threading.Thread(target=w.step)
    first.start()
    assert in_emit.wait(5)
    second = threading.Thread(target=w.resolve, kwargs={"approval_id": rec["id"]})
    second.start()
    second.join(0.3)
    assert emitted == [], "the newer snapshot waits its turn"
    release.set()
    first.join(5)
    second.join(5)
    assert emitted == [[rec["id"]], []]


# ── what is watched, and how ────────────────────────────────────────────────

def test_only_the_front_page_is_requested_and_only_read(cards, watch, clock):
    """An approval page is answered through its own links. The watch reads
    the page and nothing else."""
    card = cards()
    watch.register(TITLE, MESSAGE, SOURCE, card.url)
    for _ in range(30):
        check(watch, clock)
    assert len(card.requests) == 30
    assert set(card.requests) == {("GET", "/")}


def test_a_redirect_is_not_followed(cards, watch, clock):
    target = cards()
    card = cards()
    card.status, card.location = 302, target.url
    watch.register(TITLE, MESSAGE, SOURCE, card.url)
    for _ in range(3):
        check(watch, clock)
    assert watch.open_now() == [] and target.requests == []


@pytest.mark.parametrize("title,url", [
    ("", "http://127.0.0.1:{port}/"),                    # nothing to recognise the page by
    ("   ", "http://127.0.0.1:{port}/"),
    (TITLE, ""),                                          # a ping with nothing to answer
    (TITLE, "https://127.0.0.1:{port}/"),
    # Another machine. An address reserved for examples (RFC 5737), because the
    # pre-push scan (app/git_scanner.py) reads a private-network URL as a leak.
    (TITLE, "http://203.0.113.20:{port}/"),
    (TITLE, "http://example.com/"),
    (TITLE, "http://user:pw@127.0.0.1:{port}/"),
    (TITLE, "http://127.0.0.1.evil.example:{port}/"),
    (TITLE, "file:///etc/passwd"),
    (TITLE, "http://127.0.0.1:notaport/"),
    (TITLE, "javascript:alert(1)"),
    # Anything but the front page could be one of the page's own answer links.
    (TITLE, "http://127.0.0.1:{port}/approve?token=5f1c9a0be2d44c17"),
    (TITLE, "http://127.0.0.1:{port}/approve"),
    (TITLE, "http://127.0.0.1:{port}/?approve=1"),
    (TITLE, "http://127.0.0.1:{port}/a/../"),
])
def test_what_is_not_watched(title, url, cards, clock, events):
    card = cards()
    fetched = []
    w = aw.ApprovalWatch(emit=lambda e, d: events.append((e, d)), clock=clock, threaded=False,
                         fetch=lambda u, t: fetched.append(u) or (aw.GONE, b""))
    assert w.register(title, MESSAGE, SOURCE, url.format(port=card.port)) is None
    w.step()
    assert fetched == [] and events == [] and card.requests == []


@pytest.mark.parametrize("given,watched", [
    ("http://127.0.0.1:{port}/", "http://127.0.0.1:{port}/"),
    ("http://127.0.0.1:{port}", "http://127.0.0.1:{port}/"),
    ("http://localhost:{port}/", "http://localhost:{port}/"),
    ("  HTTP://LocalHost:{port}/#top ", "http://localhost:{port}/"),
])
def test_a_front_page_on_this_machine_is_watched(given, watched, cards, watch):
    card = cards()
    assert watch.register(TITLE, MESSAGE, SOURCE, given.format(port=card.port))
    watch.step()
    assert [c["url"] for c in watch.open_now(with_urls=True)] == [watched.format(port=card.port)]
    assert set(card.requests) == {("GET", "/")}


def test_the_same_address_announced_again_is_a_new_page(cards, watch, clock, events):
    """A port can be reused by the next approval. The old record's fingerprint
    could never match the new page."""
    card = cards()
    old = watch.register(TITLE, MESSAGE, SOURCE, card.url)
    watch.step()
    card.page = card_page("Release 4.3", token="another")
    new = watch.register("Release 4.3", MESSAGE, SOURCE, card.url)
    watch.step()
    assert new["id"] != old["id"]
    assert [c["id"] for c in watch.open_now()] == [new["id"]]
    assert listed(events)[-1] == [new["id"]]


def test_the_number_watched_is_bounded(cards, watch, clock):
    made = [cards(title=f"Release 9.{n}") for n in range(aw.MAX_TRACKED + 3)]
    for n, card in enumerate(made):
        clock.tick(1)
        watch.register(f"Release 9.{n}", MESSAGE, "", card.url)
    # ``step()`` deliberately stops at the first UNKNOWN so one hung page cannot
    # hold the rest (see the comment in ``ApprovalWatch.step``), and the suite's
    # ``_short_timeout`` cuts PROBE_TIMEOUT_S to 0.3s. On an 8-worker parallel
    # run the local HTTP fetch of a tracked page can genuinely miss that budget,
    # so one ``step()`` is not guaranteed to confirm every page; the watcher is
    # built to pick them up on the next pass. Loop the way the real thread does.
    for _ in range(4 * aw.MAX_TRACKED):
        watch.step()
        if len(watch.open_now()) == aw.MAX_TRACKED:
            break
        clock.tick(aw.RETRY_S + 0.1)
    titles = [c["title"] for c in watch.open_now()]
    assert titles == [f"Release 9.{n}" for n in range(3, aw.MAX_TRACKED + 3)], "the oldest give way"


def test_every_page_that_stops_being_watched_leaves_a_line_in_the_log(cards, watch, clock, caplog, monkeypatch):
    """"Why did the tab stop flashing?" is answered from logs/web_server.log."""
    monkeypatch.setattr(aw, "MAX_TRACKED", 2)
    with caplog.at_level(logging.INFO, logger="app.routes.approval_watch"):
        one = cards()
        a = watch.register(TITLE, MESSAGE, SOURCE, one.url)
        watch.step()
        b = watch.register(TITLE, MESSAGE, SOURCE, one.url)          # announced again
        watch.step()
        two, three = cards(title="Release 4.3"), cards(title="Release 4.4")
        c = watch.register("Release 4.3", MESSAGE, SOURCE, two.url)
        watch.step()
        d = watch.register("Release 4.4", MESSAGE, SOURCE, three.url)   # one too many: b gives way
        watch.step()
        three.close()                                                # d answered: c was before it
        check(watch, clock)
    text = caplog.text
    for approval, why in ((a, "its address was announced again"), (b, "more than 2 pages are being watched"),
                          (d, "its page is gone (answered or closed)"), (c, "a later page from pipeline went away")):
        assert f"approval {approval['id']} " in text and why in text, why
    assert text.count("no longer watched") == 4 and text.count(": open at http://127.0.0.1:") == 4
    assert watch.open_now() == []


# ── one check ───────────────────────────────────────────────────────────────

def test_what_one_check_can_tell(cards, real_refusal):
    card = cards()
    assert aw.fetch_page(card.url, 1.0) == (aw.PAGE, card.page.encode("utf-8"))

    card.status = 404
    assert aw.fetch_page(card.url, 1.0) == (aw.GONE, b"")
    card.status = 409
    assert aw.fetch_page(card.url, 1.0) == (aw.GONE, b"")
    card.status = 503                          # a bad moment is not an answer
    assert aw.fetch_page(card.url, 1.0) == (aw.UNKNOWN, b"")

    card.status = 200
    card.cut_short = True                      # half a page is not a different page
    assert aw.fetch_page(card.url, 1.0) == (aw.UNKNOWN, b"")
    card.cut_short = False

    card.go_silent()
    assert aw.fetch_page(card.url, 0.3) == (aw.UNKNOWN, b"")
    card.close()
    assert aw.fetch_page(card.url, 1.0) == (aw.GONE, b"")


def _trickler(reply: bytes):
    """A port that answers one connection with ``reply``, a byte every 50ms."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        try:
            conn, _ = listener.accept()
            conn.recv(4096)
            for byte in reply:
                conn.sendall(bytes([byte]))
                time.sleep(0.05)
            conn.close()
        except OSError:
            pass                               # the check hung up first: the point

    threading.Thread(target=serve, daemon=True).start()
    return listener, f"http://127.0.0.1:{listener.getsockname()[1]}/"


@pytest.mark.parametrize("reply", [
    b"HTTP/1.0 200 OK\r\n" + b"X-Padding: " + b"p" * 400 + b"\r\nContent-Length: 4000\r\n\r\n",   # the headers, slowly
    b"HTTP/1.0 200 OK\r\nContent-Length: 4000\r\n\r\n" + b"x" * 4000,                # then the body
    b"HTTP/1.0 200 OK\r\n\r\n" + b"x" * 4000,                                         # with no length at all
], ids=["headers", "body", "no length"])
def test_a_check_has_one_deadline_whatever_the_page_sends(reply):
    """Each byte arrives in time; the page never does (every one of these
    takes more than 20 seconds to arrive). Every page is checked on one
    thread, so a check that waited for this one would stop the heartbeat of
    all the others."""
    listener, url = _trickler(reply)
    try:
        t0 = time.monotonic()
        assert aw.fetch_page(url, 0.3) == (aw.UNKNOWN, b"")
        took = time.monotonic() - t0
        assert took >= 0.5, "it waited out its deadline: twice the timeout, for the whole exchange"
        assert took < 6.0, "and not for the page"
    finally:
        listener.close()


def test_a_reply_is_complete_at_its_length_even_if_the_port_stays_open():
    """A server that keeps the connection open after a full reply has still
    sent the page."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    held = []

    def serve():
        conn, _ = listener.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-LENGTH: 5\r\n\r\nhello")
        held.append(conn)                      # and never closes it

    threading.Thread(target=serve, daemon=True).start()
    try:
        t0 = time.monotonic()
        assert aw.fetch_page(f"http://127.0.0.1:{listener.getsockname()[1]}/", 1.0) == (aw.PAGE, b"hello")
        assert time.monotonic() - t0 < 1.9, "without waiting for the port to close (the deadline is 2s)"
    finally:
        listener.close()
        for conn in held:
            conn.close()


def test_a_page_too_big_to_be_an_approval_page_is_not_one(cards, monkeypatch):
    monkeypatch.setattr(aw, "MAX_BODY", 1000)
    card = cards(page=card_page(TITLE) + "x" * 2000)
    assert aw.fetch_page(card.url, 1.0) == (aw.GONE, b"")
    card.page = card_page(TITLE) + "x" * 200000
    assert aw.fetch_page(card.url, 1.0) == (aw.GONE, b"")


def test_an_endless_page_is_given_up_on_at_once(monkeypatch):
    """A port that streams without end must not be read until the deadline:
    on loopback that is gigabytes held in memory for one check."""
    monkeypatch.setattr(aw, "MAX_BODY", 1000)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        conn, _ = listener.accept()
        try:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
            while True:
                conn.sendall(b"x" * 65536)
        except OSError:
            pass                               # the check hung up: the point
        finally:
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        t0 = time.monotonic()
        assert aw.fetch_page(f"http://127.0.0.1:{listener.getsockname()[1]}/", 1.0) == (aw.GONE, b"")
        assert time.monotonic() - t0 < 1.9, "before the two-second deadline, which would have said UNKNOWN"
    finally:
        listener.close()


def test_a_check_ignores_proxy_settings(cards, monkeypatch):
    """A proxy from the environment would send a request for 127.0.0.1 to
    another machine, and its error page would read as "the page changed"."""
    for var in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    card = cards()
    assert aw.fetch_page(card.url, 1.0)[0] == aw.PAGE


def test_nothing_is_sent_to_a_localhost_that_is_not_this_machine(monkeypatch):
    """"localhost" is a name, and a name can be made to point anywhere."""
    sent = []

    class Elsewhere:
        def getpeername(self):
            return ("203.0.113.7", 80)

        def sendall(self, data):
            sent.append(data)

        def close(self):
            pass

    monkeypatch.setattr(aw.socket, "create_connection", lambda address, timeout=None: Elsewhere())
    assert aw.fetch_page("http://localhost:8123/", 1.0) == (aw.GONE, b"")
    assert sent == []


@pytest.mark.parametrize("page,title,shown", [
    ("<h1>Release 4.2</h1>", "Release 4.2", True),
    ("<h1>A &amp; B &lt;now&gt;</h1>", "A & B <now>", True),
    ("<h1>Don&#x27;t &quot;ship&quot;</h1>", "Don't \"ship\"", True),
    ("<h1>Don&#39;t</h1>", "Don't", True),
    ("<h1>Release\n   4.2</h1>", "Release 4.2", True),
    ("<title>4.2 · Release progress</title><h1>4.2</h1>", "Release 4.2", False),
    ("<h1>Release 4.2</h1>", "", False),
    ("", "Release 4.2", False),
])
def test_shows_title(page, title, shown):
    assert aw.shows_title(page.encode("utf-8"), title) is shown


# ── the thread ──────────────────────────────────────────────────────────────

def _wait_for(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_the_thread_lives_only_while_something_is_watched(cards):
    """End to end on the real clock: announced, open, answered, over, and no
    thread left behind."""
    events = []
    w = aw.ApprovalWatch(emit=lambda event, data: events.append(data))
    assert w._thread is None
    card = cards()
    try:
        rec = w.register(TITLE, MESSAGE, SOURCE, card.url)
        assert _wait_for(lambda: events and events[-1]["open"] == [rec])
        thread = w._thread
        assert thread is not None and thread.daemon and thread.is_alive()

        card.page = PROGRESS_PAGE
        assert _wait_for(lambda: events[-1]["open"] == [])
        assert _wait_for(lambda: w._thread is None), "nothing watched, no thread"
        thread.join(timeout=5)
        assert not thread.is_alive()

        again = cards(title="Release 4.3")
        rec2 = w.register("Release 4.3", MESSAGE, SOURCE, again.url)
        assert _wait_for(lambda: events[-1]["open"] == [rec2]), "a later approval starts a new thread"
    finally:
        w.stop()
    assert w._thread is None


def test_a_thread_that_died_is_replaced(cards):
    """Whatever killed it, the next page must still be watched."""
    events = []
    w = aw.ApprovalWatch(emit=lambda event, data: events.append(data))
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    w._thread = dead
    card = cards()
    try:
        rec = w.register(TITLE, MESSAGE, SOURCE, card.url)
        assert _wait_for(lambda: events and events[-1]["open"] == [rec])
        assert w._thread is not dead and w._thread.is_alive()
    finally:
        w.stop()


def test_a_thread_that_cannot_start_is_tried_again(cards, monkeypatch):
    events = []
    w = aw.ApprovalWatch(emit=lambda event, data: events.append(data))
    one, two = cards(), cards(title="Release 4.3")
    real_start = threading.Thread.start
    monkeypatch.setattr(threading.Thread, "start",
                        lambda self: (_ for _ in ()).throw(RuntimeError("can't start new thread")))
    with pytest.raises(RuntimeError):
        w.register(TITLE, MESSAGE, SOURCE, one.url)
    assert w._thread is None
    monkeypatch.setattr(threading.Thread, "start", real_start)
    try:
        w.register("Release 4.3", MESSAGE, SOURCE, two.url)
        assert _wait_for(lambda: events and len(events[-1]["open"]) == 2), "both pages, the first one too"
    finally:
        w.stop()


def test_a_broadcast_that_fails_does_not_stop_the_watching(cards, clock, caplog):
    calls = []

    def emit(event, data):
        calls.append(data)
        raise RuntimeError("no clients")

    w = aw.ApprovalWatch(emit=emit, clock=clock, threaded=False)
    card = cards()
    rec = w.register(TITLE, MESSAGE, SOURCE, card.url)
    with caplog.at_level(logging.ERROR, logger="app.routes.approval_watch"):
        for _ in range(20):                    # ten seconds: several heartbeats fail
            check(w, clock)
        card.close()
        check(w, clock)
    assert len(calls) >= 4 and [c["id"] for c in calls[0]["open"]] == [rec["id"]] and calls[-1]["open"] == []
    assert caplog.text.count("broadcast failed") == 1, "said once, not at every heartbeat"


# ── the routes ──────────────────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch, clock):
    """(client, emitted, watch): the real routes over a hand-stepped watch,
    and every Socket.IO event the web process would have broadcast."""
    from app import create_app
    from app.routes import main as main_routes

    emitted = []
    w = aw.ApprovalWatch(emit=lambda event, data: emitted.append((event, data)),
                         clock=clock, threaded=False)
    monkeypatch.setattr(aw, "watch", w)
    monkeypatch.setattr(main_routes.socketio, "emit",
                        lambda event, data=None, **kw: emitted.append((event, data)))
    app = create_app(testing=True)
    with app.test_client() as client:
        yield client, emitted, w
    w.stop()


def test_a_ping_for_an_approval_page_chimes_and_is_watched(api, cards):
    client, emitted, w = api
    card = cards()
    body = {"title": TITLE, "message": MESSAGE, "source": SOURCE, "url": card.url}
    resp = client.post("/api/notify/approval", json=body)
    assert resp.status_code == 200
    approval_id = resp.get_json()["id"]
    assert resp.get_json() == {"ok": True, "id": approval_id} and approval_id
    assert emitted == [("external_notice", {
        "title": TITLE, "message": MESSAGE, "source": SOURCE, "id": approval_id})]

    w.step()
    assert emitted[-1][0] == "approvals_open"
    assert [c["id"] for c in emitted[-1][1]["open"]] == [approval_id]
    assert card.url not in json.dumps(emitted), "the page's address is not broadcast"

    listing = client.get("/api/notify/approvals").get_json()
    assert listing == {"ok": True, "ttl_ms": 10000,
                       "open": [dict(emitted[-1][1]["open"][0], url=card.url)]}


@pytest.mark.parametrize("body", [
    {"title": "4.2: live", "message": "Released in 9m 12s", "source": SOURCE, "url": ""},
    {"title": "4.2: first red", "message": "backend: test_x. Fixing starts now.", "source": SOURCE},
])
def test_a_ping_with_nothing_to_answer_chimes_and_is_not_watched(body, api):
    """Scripts use the same endpoint for "a test went red" and "the release is
    live". Those flashed "Approval needed" until the tab was looked at, with
    nothing to approve."""
    client, emitted, w = api
    resp = client.post("/api/notify/approval", json=body)
    assert resp.get_json() == {"ok": True, "id": ""}
    assert emitted == [("external_notice", {
        "title": body["title"], "message": body["message"], "source": SOURCE, "id": ""})]
    w.step()
    assert len(emitted) == 1 and w.open_now() == []


def test_a_ping_that_names_a_page_but_no_title_is_not_watched(api, cards):
    """The page is recognised by the script's own title. The default one
    VibeNode puts in the ping is not something the page was asked to show."""
    client, emitted, w = api
    card = cards(title="Approval needed")
    resp = client.post("/api/notify/approval", json={"url": card.url, "source": SOURCE})
    assert resp.get_json() == {"ok": True, "id": ""}
    assert emitted[0][1]["title"] == "Approval needed"
    w.step()
    assert card.requests == [] and w.open_now() == []


def test_the_old_event_is_not_sent(api, cards):
    """A tab that has not been reloaded since the fix still runs the old
    listener, which starts a flash nothing can stop. It must hear nothing."""
    client, emitted, w = api
    client.post("/api/notify/approval", json={"title": TITLE, "url": cards().url})
    client.post("/api/notify/approval", json={"title": "4.2: live"})
    w.step()
    assert "approval_needed" not in [event for event, _ in emitted]


@pytest.mark.parametrize("kwargs", [
    {"json": {}},
    {"json": []},
    {"json": [1, 2]},
    {"json": "text"},
    {"json": {"title": 7, "message": None, "url": ["x"]}},
    {"data": "not json", "content_type": "text/plain"},
    {"data": b"\xff\xfe", "content_type": "application/json"},
    {},
])
def test_a_broken_ping_never_fails_the_script_that_sent_it(kwargs, api):
    client, emitted, w = api
    resp = client.post("/api/notify/approval", **kwargs)
    assert resp.status_code == 200 and resp.get_json()["ok"] is True and resp.get_json()["id"] == ""
    assert [event for event, _ in emitted] == ["external_notice"], "it still chimes"
    assert emitted[0][1]["id"] == "" and w.open_now() == []


def test_a_ping_survives_anything_going_wrong_behind_it(api, monkeypatch, cards, caplog):
    from app.routes import main as main_routes
    client, emitted, w = api

    def boom(*args, **kwargs):
        raise RuntimeError("out of threads")

    monkeypatch.setattr(w, "register", boom)
    with caplog.at_level(logging.ERROR, logger="app.routes.main"):
        resp = client.post("/api/notify/approval", json={"title": TITLE, "url": cards().url})
    assert resp.status_code == 200 and resp.get_json() == {"ok": True, "id": ""}
    assert [event for event, _ in emitted] == ["external_notice"], "the ping itself still goes out"
    assert "could not start watching" in caplog.text, "and the failure is in the log, not swallowed"

    monkeypatch.setattr(main_routes.socketio, "emit", boom)
    resp = client.post("/api/notify/approval", json={"title": "x"})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True


@pytest.mark.parametrize("kwargs", [
    {"headers": {"Tailscale-User-Login": "someone@example.com"}},          # the phone, through the tailnet proxy
    {"headers": {"Host": "box.tail1234.ts.net"}},
    {"headers": {"X-Forwarded-For": "100.64.0.5"}},
    {"headers": {"Origin": "http://evil.example"}},                         # a page in a browser
    {"environ_base": {"REMOTE_ADDR": "192.168.1.20"}},                      # another machine
])
def test_only_a_process_on_this_machine_can_have_a_page_watched(kwargs, api, cards):
    """Watching means this server fetches the address it was given. Anyone
    else still gets the chime, and no request is made on their say-so."""
    client, emitted, w = api
    card = cards()
    resp = client.post("/api/notify/approval", json={"title": TITLE, "url": card.url}, **kwargs)
    assert resp.status_code == 200 and resp.get_json() == {"ok": True, "id": ""}
    assert [event for event, _ in emitted] == ["external_notice"]
    w.step()
    assert card.requests == [] and w.open_now() == []


def test_the_resolve_route(api, cards):
    client, emitted, w = api
    card = cards()
    approval_id = client.post("/api/notify/approval", json={"title": TITLE, "url": card.url}).get_json()["id"]
    w.step()
    assert len(w.open_now()) == 1

    for body in ({}, [], {"id": "unknown"}, {"url": "http://127.0.0.1:1/"}, {"id": ["x"], "url": 5}):
        resp = client.post("/api/notify/approval/resolve", json=body)
        assert resp.status_code == 200 and resp.get_json() == {"ok": True, "resolved": 0}
    resp = client.post("/api/notify/approval/resolve", data="x", content_type="text/plain")
    assert resp.status_code == 200 and resp.get_json() == {"ok": True, "resolved": 0}
    assert len(w.open_now()) == 1

    resp = client.post("/api/notify/approval/resolve", json={"id": approval_id},
                       headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403 and resp.get_json()["resolved"] == 0 and len(w.open_now()) == 1

    resp = client.post("/api/notify/approval/resolve", json={"url": card.url})
    assert resp.get_json() == {"ok": True, "resolved": 1}
    assert emitted[-1] == ("approvals_open", {"open": [], "ttl_ms": 10000})
    assert client.get("/api/notify/approvals").get_json()["open"] == []


def test_the_preview_proxy_does_not_make_a_phone_a_local_script(api, cards, tmp_path, monkeypatch):
    """``/api/preview/proxy`` fetches a URL for a phone on the tailnet. Pointed
    back at VibeNode it used to arrive as a request from this machine with
    nothing to say who asked, and so passed for a local script: the phone
    could have any local address fetched and watched, and could resolve
    approvals. Run for real: a live server, so the proxy really calls back.

    Since the pairing gate (app/mobile_auth.py) an unpaired phone is refused
    at the proxy itself; the phone here is PAIRED, so it gets through the
    proxy and the notify routes' own local-script rule is what is tested."""
    from werkzeug.serving import make_server

    from app import mobile_auth

    monkeypatch.setattr(mobile_auth, "PAIR_FILE", tmp_path / "data" / "mobile_pair.json")
    mobile_auth._state_cache.update(mtime=None, state={})
    monkeypatch.setattr(mobile_auth, "auth_enabled", lambda: True)
    phone = {"Content-Type": "application/json", "Tailscale-User-Login": "phone@example.com"}
    paired = dict(phone, Cookie=f"{mobile_auth.COOKIE_NAME}={mobile_auth.cookie_value()}")

    client, emitted, w = api
    card = cards()
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def through_the_proxy(path, body, headers=paired):
        target = urllib.request.quote(f"{base}{path}", safe="")
        request = urllib.request.Request(
            f"{base}/api/preview/proxy?u={target}", data=json.dumps(body).encode(), method="POST",
            headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    try:
        mine = client.post("/api/notify/approval", json={"title": TITLE, "url": card.url}).get_json()["id"]
        w.step()
        asked = len(card.requests)

        status, reply = through_the_proxy("/api/notify/approval", {"title": TITLE, "url": card.url}, phone)
        assert (status, reply["error"]) == (401, "pairing_required"), "an unpaired phone stops at the proxy"

        status, reply = through_the_proxy("/api/notify/approval", {"title": TITLE, "url": card.url})
        assert (status, reply) == (200, {"ok": True, "id": ""}), "pinged, but nothing is watched for it"
        w.step()
        assert len(card.requests) == asked and [c["id"] for c in w.open_now()] == [mine]

        status, reply = through_the_proxy("/api/notify/approval/resolve", {"id": mine})
        assert status == 403 and reply["resolved"] == 0
        assert [c["id"] for c in w.open_now()] == [mine]
    finally:
        server.shutdown()
        server.server_close()


def test_the_real_watch_broadcasts_through_socketio(monkeypatch):
    """The module's own watch, as the routes use it, reaches Socket.IO."""
    import app as app_pkg
    sent = []
    monkeypatch.setattr(app_pkg.socketio, "emit", lambda event, data=None, **kw: sent.append((event, data)))
    aw._broadcast("approvals_open", {"open": [], "ttl_ms": 10000})
    assert sent == [("approvals_open", {"open": [], "ttl_ms": 10000})]
    assert aw.watch._emit is aw._broadcast and aw.watch._threaded is True
