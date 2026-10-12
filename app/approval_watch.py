"""Which approval pages are still waiting for an answer.

A script that needs a person (a release pipeline was the first) serves an
approval page on 127.0.0.1 and tells VibeNode about it with
``POST /api/notify/approval``.  Every open tab chimes, and a tab in the
background flashes its title.

Until 2026-10-12 nothing told VibeNode when the page was answered.  The flash
stopped only when that one tab was looked at, so it kept going after the
approval whenever the user stayed on the approval page, answered from another
device, or had a second VibeNode tab open.

This module is VibeNode's own knowledge of whether an approval is still open.
It needs nothing new from the script: it fetches the page the script announced
and treats the approval as open only while that same page is still being
served.

* **Confirmed**: the first fetch returns 200 and the page shows the announced
  title.  A page that does not show it is not the approval page (a ping that
  arrives after the page was already answered finds whatever replaced it), and
  is never tracked.
* **Open**: every later fetch returns the byte-identical page.  An approval
  page is static until it is answered; the answer either closes its port or
  puts another page on the same port.
* **Not open** the moment the port refuses, the page differs, or the page has
  not been seen unchanged for ``VERIFY_TTL_S``.  A check that cannot tell
  (timeout, reset, a 5xx, half a reply) never counts as "still open".

One more way an approval ends: **a later page from the same script goes
away**.  A script can leave an earlier page up after the one that mattered was
answered.  A pipeline that asks once before its checks run and again when they
pass does: answer the second page and the first is still being served,
unchanged.  Nothing on that page says it no longer matters, and the user has
answered.  So when a page is answered or closed, the pages the same ``source``
announced before it stop being watched.  A page announced after it is not
touched, and neither is another script's.

While at least one approval is open the snapshot is re-broadcast every
``HEARTBEAT_S`` as the Socket.IO event ``approvals_open``.  The browser treats
each snapshot as a lease of ``CLIENT_TTL_S`` (static/js/notify.js), so a tab
stops flashing when it is told the approval is gone AND when it stops hearing
that the approval is open: a dead socket, a restarted web server or a crash in
here ends the flash instead of leaving it on.

The only request this module ever makes is ``GET /`` on a port of this
machine: the request a browser makes to show the page.  A page is answered
through its own links, and no link, path or query is ever requested, so
nothing here can answer one.
"""

from __future__ import annotations

import hashlib
import html
import http.client
import io
import ipaddress
import logging
import re
import socket
import threading
import time
import urllib.parse
import uuid

# Under "app.routes" on purpose: run.py attaches the web log handler to that
# namespace only, and these lines are how a later "why did it (not) flash" is
# answered from logs/web_server.log.
log = logging.getLogger("app.routes.approval_watch")

EVENT = "approvals_open"

PROBE_FAST_S = 0.5        # a page someone was just told about: the answer is likely soon
PROBE_SLOW_S = 3.0        # an older page, or one whose last checks could not tell
FAST_FOR_S = 600.0        # how long a page counts as "just told about"
RETRY_S = 0.3             # after a check that could not tell, look again at once...
QUICK_RETRIES = 2         # ...this many times, then at the slow pace
# Above 2s: connecting to a closed port takes about that long to be refused on
# Windows, and a refusal is the clearest "answered" there is.
PROBE_TIMEOUT_S = 2.5
VERIFY_TTL_S = 8.0        # not seen unchanged for this long: not open
CONFIRM_WINDOW_S = 30.0   # a new page gets this long to answer its first check
HEARTBEAT_S = 3.0         # an open approval is said again this often
TICK_S = 1.0              # the thread looks at the clock at least this often
CLIENT_TTL_S = 10.0       # how long a browser may act on one snapshot
MAX_AGE_S = 24 * 3600.0   # the longest an approval page waits for its answer
MAX_TRACKED = 8           # pages watched at once; the oldest gives way
MAX_BODY = 4 * 1024 * 1024  # an approval page is some kilobytes; more is not one

PAGE, GONE, UNKNOWN = "page", "gone", "unknown"

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_REQUEST = ("GET / HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n"
            "Accept: text/html,*/*;q=0.5\r\nUser-Agent: VibeNode-approval-watch\r\n\r\n")
_CONTENT_LENGTH = re.compile(rb"(?im)^content-length:[ \t]*(\d+)[ \t]*\r?$")


def local_page_url(url) -> str:
    """``http://<host>:<port>/`` when ``url`` is the front page of a port on
    this machine, else "".

    Nothing else can be watched: no other host, no https, no credentials, and
    no path or query.  The page a person is sent to is the front page; a URL
    with more in it could be one of the page's own answer links.
    """
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
        port = parts.port
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if parts.scheme != "http" or host not in _LOCAL_HOSTS:
        return ""
    if parts.username or parts.password or parts.path not in ("", "/") or parts.query:
        return ""
    return "http://%s:%d/" % ("[%s]" % host if ":" in host else host, port or 80)


class _Reply:
    """A received reply, in the shape http.client parses one from."""

    def __init__(self, raw: bytes):
        self._raw = raw

    def makefile(self, *args, **kwargs):
        return io.BytesIO(self._raw)


def _whole_reply(raw) -> int | None:
    """How many bytes the reply is in all, once its headers say so; else None."""
    end = raw.find(b"\r\n\r\n")
    if end < 0:
        return None
    length = _CONTENT_LENGTH.search(raw[:end + 2])
    return end + 4 + int(length.group(1)) if length else None


def fetch_page(url: str, timeout: float = PROBE_TIMEOUT_S):
    """GET the front page at ``url`` once, in at most ``2 * timeout``.

    ``(PAGE, body)`` for a complete 200.  ``(GONE, b"")`` when that page is not
    being served: nothing listens there, or the answer is anything but a 200 of
    a sane size.  ``(UNKNOWN, b"")`` when this attempt could not tell.

    A plain socket and one deadline for the whole exchange, so a page that
    sends its reply a byte at a time cannot hold the watcher: every page is
    checked on one thread.  No proxy from the environment, no redirects.
    """
    parts = urllib.parse.urlsplit(url)
    deadline = time.monotonic() + 2 * timeout
    try:
        sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
    except ConnectionRefusedError:
        return GONE, b""
    except OSError:
        return UNKNOWN, b""
    try:
        # "localhost" is a name. Nothing is sent unless the other end really
        # is this machine.
        if not ipaddress.ip_address(sock.getpeername()[0]).is_loopback:
            return GONE, b""
        sock.sendall((_REQUEST % parts.netloc).encode("ascii"))
        raw, whole = bytearray(), None
        while whole is None or len(raw) < whole:
            left = deadline - time.monotonic()
            if left <= 0:
                return UNKNOWN, b""
            sock.settimeout(min(left, timeout))
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
            if len(raw) > MAX_BODY + 65536:
                return GONE, b""
            if whole is None:
                whole = _whole_reply(raw)
        reply = http.client.HTTPResponse(_Reply(bytes(raw)), method="GET")
        reply.begin()
        if reply.status >= 500:
            return UNKNOWN, b""          # a server having a bad moment still has its page
        if reply.status != 200:
            return GONE, b""
        body = reply.read()              # IncompleteRead when it was cut short
        return (PAGE, body) if len(body) <= MAX_BODY else (GONE, b"")
    except ConnectionRefusedError:
        return GONE, b""
    except (OSError, ValueError, http.client.HTTPException):
        return UNKNOWN, b""
    finally:
        sock.close()


def shows_title(body: bytes, title: str) -> bool:
    """True when the page's text contains ``title``, however it escaped it."""
    needle = " ".join(title.split())
    if not needle:
        return False
    text = body.decode("utf-8", "replace")
    return any(needle in " ".join(t.split()) for t in (text, html.unescape(text)))


class _Card:
    """One announced approval page."""

    __slots__ = ("id", "seq", "title", "message", "source", "url", "since", "born",
                 "fingerprint", "seen", "misses", "next_probe")

    def __init__(self, seq, title, message, source, url, now):
        self.id = uuid.uuid4().hex[:12]
        self.seq = seq              # the order pages were announced in
        self.title = title
        self.message = message
        self.source = source
        self.url = url
        self.since = time.time()
        self.born = now
        self.fingerprint = None     # set by the first check that confirms the page
        self.seen = 0.0             # when the page was last seen unchanged
        self.misses = 0             # checks in a row that could not tell
        self.next_probe = now

    def open_at(self, now) -> bool:
        return self.fingerprint is not None and now - self.seen <= VERIFY_TTL_S

    def public(self, with_url=False) -> dict:
        """What is said about this page. The address is left out of what is
        broadcast: every browser gets the broadcast, and none of them uses it."""
        record = {"id": self.id, "title": self.title[:200], "message": self.message,
                  "source": self.source, "since": self.since}
        if with_url:
            record["url"] = self.url
        return record


class ApprovalWatch:
    """The approval pages being watched, and the thread that watches them.

    The thread exists only while something is tracked.  ``emit``, ``clock`` and
    ``fetch`` are parameters so the tests can drive ``step()`` by hand against
    real local pages.
    """

    def __init__(self, emit, clock=time.monotonic, fetch=fetch_page, threaded=True):
        self._emit = emit
        self._clock = clock
        self._fetch = fetch
        self._threaded = threaded
        self._lock = threading.Lock()            # _cards, _announced and _thread
        self._publish_lock = threading.Lock()    # snapshots leave in the order they were taken
        self._cards: dict[str, _Card] = {}
        self._announced = 0
        self._thread = None
        self._wake = threading.Event()
        self._sent_ids: tuple = ()
        self._sent_at = 0.0
        self._emit_failing = False

    # ── what the routes call ────────────────────────────────────────────

    def register(self, title, message="", source="", url=""):
        """Start watching an announced page.  Returns its public record, or
        None when it cannot be watched (no title to recognise it by, or the
        URL is not the front page of a port on this machine).  It is open
        only once a check has confirmed it."""
        title = " ".join(str(title or "").split())
        page_url = local_page_url(url)
        if not title or not page_url:
            return None
        with self._lock:
            self._announced += 1
            card = _Card(self._announced, title, str(message or "")[:500], str(source or "")[:60],
                         page_url, self._clock())
            # The same address announced again is a new page: the old record's
            # fingerprint could never match it.
            for old in [c for c in self._cards.values() if c.url == page_url]:
                self._drop(old, "its address was announced again")
            while len(self._cards) >= MAX_TRACKED:
                self._drop(min(self._cards.values(), key=lambda c: c.seq),
                           "more than %d pages are being watched" % MAX_TRACKED)
            self._cards[card.id] = card
            # is_alive, not "is there one": a thread that died, or never
            # started, must not leave every later page unwatched.
            if self._threaded and (self._thread is None or not self._thread.is_alive()):
                thread = threading.Thread(target=self._run, name="approval-watch", daemon=True)
                thread.start()
                self._thread = thread
        self._wake.set()
        return card.public()

    def resolve(self, approval_id="", url="") -> int:
        """The script says an approval is over.  Returns how many were dropped."""
        approval_id = str(approval_id or "")
        page_url = local_page_url(url)
        with self._lock:
            hit = [c for c in self._cards.values()
                   if (approval_id and c.id == approval_id) or (page_url and c.url == page_url)]
            for card in hit:
                if card.id in self._cards:       # not already ended by an earlier one in this list
                    self._drop(card, "resolved by its script")
                    self._supersede(card)
        if hit:
            self._publish()
            self._wake.set()
        return len(hit)

    def open_now(self, with_urls=False) -> list:
        """The approvals that are open right now, oldest first."""
        now = self._clock()
        with self._lock:
            cards = sorted((c for c in self._cards.values() if c.open_at(now)), key=lambda c: c.seq)
            return [c.public(with_urls) for c in cards]

    def stop(self) -> None:
        """Forget everything and let the thread end (tests)."""
        with self._lock:
            self._cards.clear()
            thread = self._thread
        self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)

    # ── the watching ────────────────────────────────────────────────────

    def step(self) -> None:
        """Check the pages that are due, then publish."""
        now = self._clock()

        def likeliest_to_answer_first(card):
            if card.misses:
                return (2, card.next_probe)      # did not answer last time
            if card.fingerprint is None:
                return (1, card.next_probe)      # new, never checked
            return (0, -card.seen)               # answered, most recently first

        with self._lock:
            due = sorted((c for c in self._cards.values() if c.next_probe <= now),
                         key=likeliest_to_answer_first)
            todo = [(c.id, c.url, c.misses) for c in due]
        for card_id, url, misses in todo:
            if misses:
                # This page did not answer last time, so this check will
                # probably take a full timeout. A heartbeat that would fall
                # due meanwhile is sent now instead of late.
                self._publish(due_within=PROBE_TIMEOUT_S)
            status, body = self._fetch(url, PROBE_TIMEOUT_S)
            self._apply(card_id, status, body)
            self._publish()
            if status == UNKNOWN:
                # A page that does not answer costs a full timeout. One per
                # pass: the rest wait for the next pass, which starts again
                # with the pages that do answer. However many pages hang, the
                # ones that answer are seen again well inside VERIFY_TTL_S.
                break
        self._publish()

    def _apply(self, card_id, status, body) -> None:
        """Take in the result of one check of one page: confirm it, keep it
        open, or stop watching it, and set when it is checked next."""
        now = self._clock()
        with self._lock:
            card = self._cards.get(card_id)
            if card is None:
                return                   # resolved or replaced while the check was in flight
            ended, went_away = "", False
            if status == PAGE:
                card.misses = 0
                digest = hashlib.sha256(body).hexdigest()
                if card.fingerprint is None:
                    if shows_title(body, card.title):
                        card.fingerprint, card.seen = digest, now
                        log.info("approval %s (%s): open at %s", card.id, card.title[:80], card.url)
                    else:
                        ended, went_away = "the page at its address does not show its title", True
                elif digest == card.fingerprint:
                    card.seen = now
                else:
                    ended, went_away = "its page was replaced (answered)", True
            elif status == GONE:
                ended, went_away = "its page is gone (answered or closed)", True
            else:
                card.misses += 1
                if card.fingerprint is None and now - card.born > CONFIRM_WINDOW_S:
                    ended = "its page never answered"
            if not ended and now - card.born > MAX_AGE_S:
                ended = "older than any approval page waits"
            if ended:
                self._drop(card, ended)
                if went_away:
                    self._supersede(card)
            elif card.misses:
                card.next_probe = now + (RETRY_S if card.misses <= QUICK_RETRIES else PROBE_SLOW_S)
            else:
                card.next_probe = now + (PROBE_FAST_S if now - card.born < FAST_FOR_S else PROBE_SLOW_S)

    def _drop(self, card: _Card, why: str) -> None:
        """Stop watching ``card`` and say why in the log. Call with the lock."""
        self._cards.pop(card.id, None)
        log.info("approval %s (%s): no longer watched, %s", card.id, card.title[:80], why)

    def _supersede(self, gone: _Card) -> None:
        """``gone`` was answered or closed: stop watching the pages its script
        announced before it (see the module docstring). Call with the lock."""
        if not gone.source:
            return
        for card in [c for c in self._cards.values() if c.source == gone.source and c.seq < gone.seq]:
            self._drop(card, "a later page from %s went away" % gone.source)

    def _publish(self, due_within=0.0) -> None:
        """Broadcast the open approvals when they changed, or when there is
        something to keep alive and the last broadcast will be ``HEARTBEAT_S``
        old within ``due_within`` seconds."""
        with self._publish_lock:
            now = self._clock()
            snapshot = self.open_now()
            ids = tuple(c["id"] for c in snapshot)
            if ids == self._sent_ids and (not ids or now - self._sent_at < HEARTBEAT_S - due_within):
                return
            self._sent_ids, self._sent_at = ids, now
            try:
                self._emit(EVENT, {"open": snapshot, "ttl_ms": int(CLIENT_TTL_S * 1000)})
            except Exception:  # noqa: BLE001 - a browser that misses one stops on its lease
                if not self._emit_failing:       # once, not every three seconds
                    log.exception("approval watch: broadcast failed (said once until it works again)")
                self._emit_failing = True
            else:
                self._emit_failing = False

    def _run(self) -> None:
        while True:
            self._wake.clear()
            nap = self._pass()
            if nap is None:
                return
            self._wake.wait(nap)

    def _pass(self):
        """One turn of the thread: a step, then how long to sleep. None when
        nothing is tracked any more and the thread should end."""
        failed = False
        try:
            self.step()
        except Exception:  # noqa: BLE001 - never let the watcher die with pages tracked
            log.exception("approval watch: step failed")
            failed = True
        with self._lock:
            if not self._cards:
                self._thread = None
                return None
            wait = min(c.next_probe for c in self._cards.values()) - self._clock()
        # Never longer than TICK_S, even when no page is due for seconds: the
        # heartbeat is sent from step(), and a late one costs every browser
        # part of its lease.
        return 1.0 if failed else max(0.05, min(wait, TICK_S))


def _broadcast(event, data) -> None:
    from . import socketio
    socketio.emit(event, data)


watch = ApprovalWatch(emit=_broadcast)
